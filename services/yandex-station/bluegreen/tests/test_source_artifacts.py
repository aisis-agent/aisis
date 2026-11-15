"""Persistent source artifact identity and the existing deployment gates."""
import argparse
import contextlib
import hashlib
import io
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
import bgctl

IMAGE = 'sha256:' + '1' * 64
FILES = ('backend/ha-intents.ts', 'backend/tv-app-intents.ts')


def prepared(directory):
    directory.mkdir()
    contents = {name: ('// remote artifact ' + name + '\n').encode() for name in FILES}
    archive = directory / 'fireplace-artifact.tar'
    with tarfile.open(archive, 'w') as bundle:
        for name, data in contents.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o444
            bundle.addfile(info, io.BytesIO(data))
    proof = {'deployment_mode': 'read-only-source-artifact-over-existing-image',
             'deployment_verified': False, 'source_commit': '2' * 40, 'image_id': IMAGE,
             'artifact_sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
             'input_digest': '3' * 64,
             'counts': {'corpus': {'passed': 2, 'total': 2}, 'dialogs': {'passed': 1, 'total': 1}},
             'overlay_sha256': {Path(name).name: hashlib.sha256(data).hexdigest() for name, data in contents.items()}}
    (directory / 'proof.json').write_text(json.dumps(proof))
    return proof


class PersistentArtifacts(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state = self.root / 'state'
        self.patches = [patch.object(bgctl, 'STATE_DIR', self.state),
                        patch.object(bgctl, 'STATE_FILE', self.state / 'state.env'),
                        patch.object(bgctl, 'RUNTIME_ENV', self.root / 'absent-private.env')]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        bgctl.save_state({'PROD_ACTIVE': 'green', 'PROD_IMG_BLUE': IMAGE,
                          'PROD_IMG_GREEN': 'rollback-image', 'STAGE_ACTIVE': 'blue'})
        self.input = self.root / 'remote-output'
        self.proof = prepared(self.input)

    def imported(self):
        command = getattr(bgctl, 'cmd_import_artifact', None)
        self.assertTrue(callable(command), 'the normal deployment must import a verified remote artifact')
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(command(argparse.Namespace(source=str(self.input))), 0)
        destination = Path(output.getvalue().strip())
        self.assertTrue(destination.is_absolute())
        self.assertTrue(destination.is_relative_to(self.state))
        return destination

    def select(self, artifact, image=IMAGE, color='blue'):
        return bgctl.cmd_set_image(argparse.Namespace(env='prod', color=color, image=image, artifact=str(artifact)))

    def test_import_is_immutable_and_survives_the_download_directory(self):
        destination = self.imported()
        self.assertEqual(self.imported(), destination)
        shutil.rmtree(self.input)
        self.select(destination)
        self.assertEqual(bgctl.load_state()['PROD_ARTIFACT_BLUE'], str(destination))
        self.assertEqual(bgctl.load_state()['PROD_IMG_GREEN'], 'rollback-image')
        self.assertEqual(bgctl.load_state()['PROD_ACTIVE'], 'green')
        self.assertEqual((destination / 'artifact.json').stat().st_mode & 0o222, 0)
        for name in FILES:
            self.assertEqual((destination / name).stat().st_mode & 0o222, 0)

    def test_every_normal_compose_invocation_retains_only_the_selected_color_mounts(self):
        destination = self.imported()
        self.select(destination)
        for arguments in [('config',), ('up', '-d', '--force-recreate', 'quasar-prod-blue')]:
            command = bgctl.compose_cmd('prod', *arguments)
            files = [Path(command[index + 1]) for index, value in enumerate(command) if value == '-f']
            self.assertEqual(len(files), 2, 'normal recreation must retain the artifact configuration')
            config = json.loads(files[-1].read_text())
            self.assertEqual(set(config['services']), {'quasar-prod-blue'})
            mounts = config['services']['quasar-prod-blue']['volumes']
            self.assertEqual({item['target'] for item in mounts}, {'/src/' + name for name in FILES})
            self.assertTrue(all(item['read_only'] and not item['bind']['create_host_path'] for item in mounts))

    def test_new_image_clears_its_artifact_without_touching_rollback_color(self):
        destination = self.imported()
        self.select(destination)
        bgctl.cmd_set_image(argparse.Namespace(env='prod', color='blue', image='new-image', artifact=''))
        self.assertFalse(bgctl.load_state().get('PROD_ARTIFACT_BLUE'))
        self.assertEqual(bgctl.load_state()['PROD_IMG_GREEN'], 'rollback-image')

    def test_wrong_base_or_changed_source_is_rejected_before_state_or_compose_changes(self):
        destination = self.imported()
        before = bgctl.load_state()
        with self.assertRaises((ValueError, RuntimeError)):
            self.select(destination, image='wrong-image')
        self.assertEqual(bgctl.load_state(), before)
        self.select(destination)
        source = destination / FILES[0]
        source.chmod(0o644)
        source.write_text('// changed after acceptance\n')
        source.chmod(0o444)
        with self.assertRaisesRegex(ValueError, 'installed source artifact changed'):
            bgctl.compose_cmd('prod', 'up', '-d')

    def test_changed_archive_cannot_be_imported(self):
        with (self.input / 'fireplace-artifact.tar').open('ab') as stream:
            stream.write(b'changed')
        command = getattr(bgctl, 'cmd_import_artifact', None)
        self.assertTrue(callable(command))
        with self.assertRaises((ValueError, RuntimeError)):
            command(argparse.Namespace(source=str(self.input)))
        self.assertFalse(list(self.state.glob('artifacts/*')))

    def test_unexpected_archive_members_are_rejected_even_with_an_updated_archive_digest(self):
        archive = self.input / 'fireplace-artifact.tar'
        with tarfile.open(archive, 'a') as bundle:
            member = tarfile.TarInfo('backend/unrelated.ts')
            member.size = 1
            bundle.addfile(member, io.BytesIO(b'x'))
        self.proof['artifact_sha256'] = hashlib.sha256(archive.read_bytes()).hexdigest()
        (self.input / 'proof.json').write_text(json.dumps(self.proof))
        with self.assertRaises((ValueError, RuntimeError)):
            bgctl.cmd_import_artifact(argparse.Namespace(source=str(self.input)))
        self.assertFalse(list(self.state.glob('artifacts/*')))

    def test_reused_artifacts_reject_writable_files_or_directories(self):
        destination = self.imported()
        for relative in ['backend/ha-intents.ts', 'artifact.json', 'backend', '.']:
            path = destination / relative
            original = path.stat().st_mode & 0o777
            path.chmod(original | 0o200)
            with self.subTest(path=relative), self.assertRaises((ValueError, RuntimeError)):
                bgctl.cmd_import_artifact(argparse.Namespace(source=str(self.input)))
            path.chmod(original)

    def test_missing_stage_artifact_does_not_block_prod_only_recreation(self):
        destination = self.imported()
        self.select(destination)
        state = bgctl.load_state()
        state.update({'STAGE_IMG_BLUE': IMAGE, 'STAGE_ARTIFACT_BLUE': str(self.root / 'deleted-stage-artifact')})
        bgctl.save_state(state)
        command = bgctl.compose_cmd('prod', 'up', '-d', 'quasar-prod-blue')
        files = [Path(command[index + 1]) for index, value in enumerate(command) if value == '-f']
        self.assertEqual(set(json.loads(files[-1].read_text())['services']), {'quasar-prod-blue'})

    def test_stage_teardown_does_not_require_an_intact_source_artifact(self):
        state = bgctl.load_state()
        state.update({'STAGE_IMG_BLUE': IMAGE, 'STAGE_ARTIFACT_BLUE': str(self.root / 'missing-stage-artifact')})
        bgctl.save_state(state)
        for arguments in [('down', '--remove-orphans'), ('rm', '-sf', 'quasar-stage-blue'), ('stop', 'quasar-stage-blue')]:
            command = bgctl.compose_cmd('stage', *arguments)
            files = [Path(command[index + 1]) for index, value in enumerate(command) if value == '-f']
            self.assertEqual(json.loads(files[-1].read_text()), {'services': {}})
        with self.assertRaises((ValueError, OSError)):
            bgctl.compose_cmd('stage', 'up', '-d')

    def test_runtime_identity_failures_include_safe_operation_and_error_category(self):
        result = subprocess.CompletedProcess([], 1, '', 'Error response from daemon: No such container: private-secret-canary')
        with patch.object(bgctl, '_docker', return_value=result):
            with self.assertRaises(RuntimeError) as caught:
                bgctl.container_id('prod', 'blue')
        message = str(caught.exception)
        self.assertIn('inspect', message)
        self.assertIn('exit=1', message)
        self.assertIn('no such container', message.lower())
        self.assertNotIn('private-secret-canary', message)

    def test_runtime_identity_checks_actual_image_and_readonly_mounts(self):
        directory = self.imported()
        self.select(directory)
        verify = getattr(bgctl, 'verify_runtime', None)
        self.assertTrue(callable(verify))
        mounts = [{'Type': 'bind', 'Source': str(directory / name), 'Destination': '/src/' + name, 'RW': False} for name in FILES]
        observed = {'image': IMAGE, 'mounts': mounts}
        def docker(*args, **_kwargs):
            output = IMAGE if args[0] == 'image' else observed['image']
            if any('.Mounts' in value for value in args):
                output = json.dumps(observed['mounts'])
            return subprocess.CompletedProcess(args, 0, output, '')
        with patch.object(bgctl, '_docker', side_effect=docker):
            verify('prod', 'blue')
            observed['image'] = 'wrong-image'
            with self.assertRaises(RuntimeError):
                verify('prod', 'blue')
            observed['image'] = IMAGE
            observed['mounts'] = [{**item, 'RW': True} for item in mounts]
            with self.assertRaises(RuntimeError):
                verify('prod', 'blue')
            observed['mounts'] = []
            with self.assertRaises(RuntimeError):
                verify('prod', 'blue')

    def test_cleanup_rechecks_inactive_color_and_removes_only_the_captured_container_id(self):
        remove = getattr(bgctl, 'remove_inactive', None)
        self.assertTrue(callable(remove))
        expected = 'a' * 64
        def docker(*args, **_kwargs):
            return subprocess.CompletedProcess(args, 0, expected, '')
        with patch.object(bgctl, '_docker', side_effect=docker) as calls:
            remove('prod', 'blue', expected)
            self.assertTrue(any(call.args == ('rm', '-f', expected) for call in calls.call_args_list))
        with patch.object(bgctl, '_docker', return_value=subprocess.CompletedProcess([], 0, 'b' * 64, '')) as calls:
            with self.assertRaises(RuntimeError):
                remove('prod', 'blue', expected)
            self.assertFalse(any(call.args[:1] == ('rm',) for call in calls.call_args_list))
        with patch.object(bgctl, 'active_color', side_effect=['green', 'blue']), patch.object(bgctl, '_docker', side_effect=docker) as calls:
            with self.assertRaises(RuntimeError):
                remove('prod', 'blue', expected)
            self.assertFalse(any(call.args[:1] == ('rm',) for call in calls.call_args_list))


class ArtifactDeploy(unittest.TestCase):
    def run_deploy(self, *, fail_idle=False, fail_first=False, fail_restore=False, fail_before=False, overlay=False, active='green'):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shutil.copy2(HERE / 'deploy.sh', root / 'deploy.sh')
            for source, destination in [('fake_bgctl.py', 'bgctl.py'), ('fake_docker.py', 'docker'), ('fake_corpus.py', 'corpus_runpod.py')]:
                shutil.copy2(HERE / 'tests/fixtures' / source, root / destination)
            (root / 'docker').chmod(0o755)
            state = root / 'state'
            state.mkdir()
            (state / 'state.env').write_text('PROD_ACTIVE=' + active + '\n')
            idle = 'blue' if active == 'green' else 'green'
            previous = {'image': 'previous-' + idle, 'artifact': '/previous/artifact'}
            (root / 'state.json').write_text(json.dumps({'active': active, 'idle_color': idle,
                'images': {idle: previous['image'], active: 'rollback-image'},
                'artifacts': {idle: '/previous/artifact', active: '/active/source-artifact' if overlay else ''},
                'actual': previous, 'actual_id': 'original-idle-container', 'recreate_attempts': 0}))
            environment = {**os.environ, 'PATH': str(root) + os.pathsep + os.environ['PATH'],
                'AISIS_BG_STATE': str(state), 'FAIL_IDLE': str(int(fail_idle)),
                'FAIL_FIRST_RECREATE': str(int(fail_first)), 'FAIL_RESTORE': str(int(fail_restore)),
                'FAIL_BEFORE_RECREATE': str(int(fail_before))}
            source = root / 'remote-output'
            source.mkdir()
            arguments = ['bash', str(root / 'deploy.sh'), 'deploy', '--overlay' if overlay else '--artifact', str(source)]
            run = subprocess.run(arguments, env=environment, text=True, capture_output=True, timeout=20)
            calls = [json.loads(line) for line in (root / 'calls.jsonl').read_text().splitlines()]
            match = re.search(r'startup log=(/tmp/aisis-bluegreen-deploy\.[^\s]+)', run.stdout + run.stderr)
            self.assertIsNotNone(match)
            logfile = Path(match[1])
            logged = logfile.read_text()
            logfile.unlink()
            return run, calls, json.loads((root / 'state.json').read_text()), logged

    def test_artifact_deploy_keeps_all_stage_and_idle_gates_without_local_build(self):
        run, calls, state, _log = self.run_deploy()
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertFalse(any(item[:2] == ['docker', 'build'] for item in calls))
        flip = calls.index(['bg', 'flip', 'prod', 'blue'])
        for color in ('stage-blue', 'stage-green', 'prod-blue'):
            check = ['corpus', '--container', 'aisis-quasar-' + color]
            self.assertTrue(any(item[:3] == check for item in calls[:flip]), color)
        self.assertEqual(state['active'], 'blue')
        self.assertEqual(state['images']['green'], 'rollback-image')
        self.assertEqual(state['actual']['artifact'], '/installed/new-artifact')

    def test_failed_idle_checks_restore_actual_container_and_artifact_without_flip(self):
        run, calls, state, _log = self.run_deploy(fail_idle=True)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn(['bg', 'smoke', 'prod', 'blue'], calls, run.stderr)
        self.assertNotIn(['bg', 'flip', 'prod', 'blue'], calls)
        self.assertEqual(state['actual'], {'image': 'previous-blue', 'artifact': '/previous/artifact'})
        self.assertIn(['bg', 'verify-runtime', 'prod', 'blue'], calls)
        self.assertEqual(state['active'], 'green')

    def test_partial_first_recreation_is_recreated_back_to_the_previous_runtime(self):
        run, calls, state, logged = self.run_deploy(fail_first=True)
        self.assertNotEqual(run.returncode, 0)
        self.assertEqual(state['actual'], {'image': 'previous-blue', 'artifact': '/previous/artifact'})
        self.assertGreaterEqual(state['recreate_attempts'], 2)
        self.assertIn(['bg', 'verify-runtime', 'prod', 'blue'], calls)
        self.assertNotIn(['bg', 'flip', 'prod', 'blue'], calls)
        self.assertIn('candidate recreation failed after replacement', logged)

    def test_failed_restoration_is_explicit_and_cannot_leave_an_unapproved_warm_color(self):
        run, calls, state, logged = self.run_deploy(fail_first=True, fail_restore=True)
        self.assertNotEqual(run.returncode, 0)
        self.assertIsNone(state['actual'])
        self.assertIn(['bg', 'remove-inactive', 'prod', 'blue', 'fixture-container-1'], calls)
        self.assertIn('restoration failed', logged.lower())
        self.assertEqual(state['active'], 'green')

    def test_failed_compose_before_replacement_preserves_the_original_warm_container(self):
        run, calls, state, logged = self.run_deploy(fail_before=True)
        self.assertNotEqual(run.returncode, 0)
        self.assertEqual(state['actual'], {'image': 'previous-blue', 'artifact': '/previous/artifact'})
        self.assertEqual(state['actual_id'], 'original-idle-container')
        self.assertFalse(any(item[:2] == ['bg', 'remove-inactive'] for item in calls))
        self.assertIn('original idle container', logged)
        self.assertEqual(state['active'], 'green')

    def test_deployment_targets_green_when_blue_is_active(self):
        run, calls, state, _log = self.run_deploy(active='blue')
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        self.assertIn(['bg', 'flip', 'prod', 'green'], calls)
        self.assertEqual(state['active'], 'green')
        self.assertEqual(state['images']['blue'], 'rollback-image')
        self.assertEqual(state['actual']['artifact'], '/installed/new-artifact')

    def test_implicit_overlay_refuses_before_changing_an_active_source_artifact(self):
        run, calls, state, logged = self.run_deploy(overlay=True)
        self.assertNotEqual(run.returncode, 0)
        self.assertIn('active source artifact', logged.lower())
        self.assertFalse(any(item[0] == 'docker' or item[1] in {'compose', 'set-image', 'flip'} for item in calls))
        self.assertEqual(state['active'], 'green')

    def test_automatic_log_captures_stdout_and_stderr(self):
        run, _calls, _state, logged = self.run_deploy()
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn('fixture corpus stdout is retained', logged)
        self.assertIn('completion exit=0', logged)


class DeploymentFixtureContracts(unittest.TestCase):
    def test_unsupported_commands_and_wrong_target_colors_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / 'bgctl.py'
            shutil.copy2(HERE / 'tests/fixtures/fake_bgctl.py', script)
            state = {'active': 'green', 'idle_color': 'blue', 'recreate_attempts': 0}
            for arguments in [['unsupported'], ['compose', 'prod', 'up', '-d', '--no-deps', '--force-recreate', 'quasar-prod-green'],
                              ['compose', 'prod', 'up', '--wrong-option', 'quasar-prod-blue']]:
                (root / 'state.json').write_text(json.dumps(state))
                run = subprocess.run([sys.executable, str(script), *arguments], capture_output=True, text=True, timeout=5)
                self.assertEqual(run.returncode, 2, run.stdout + run.stderr)
                self.assertEqual(json.loads((root / 'state.json').read_text()), state)


if __name__ == '__main__':
    unittest.main()
