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

    def test_quarantine_blocks_normal_recreation_config_selection_and_promotion_but_allows_teardown(self):
        state = bgctl.load_state()
        state['PROD_QUARANTINE_BLUE'] = 'failed-deployment'
        bgctl.save_state(state)
        for arguments in [('config',), ('up', '-d', 'quasar-prod-blue')]:
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                bgctl.compose_cmd('prod', *arguments)
        with self.assertRaises(ValueError):
            bgctl.cmd_set_image(argparse.Namespace(env='prod', color='blue', image=IMAGE, artifact=''))
        with patch.object(bgctl, 'reload_switch') as reload_switch:
            with self.assertRaises(RuntimeError):
                bgctl.flip('prod', 'blue')
            reload_switch.assert_not_called()
        bgctl.compose_cmd('prod', 'down')
        bgctl.compose_cmd('stage', 'config')

    def test_expired_owner_can_quarantine_only_its_captured_inactive_container(self):
        directory = self.imported()
        owner = bgctl.maintenance_lease.acquire(self.state, os.getpid())
        marker = self.state / 'maintenance'
        lease = json.loads(marker.read_text())
        lease['deadline_monotonic'] = 0
        marker.write_text(json.dumps(lease))
        expected_id = 'a' * 64
        with patch.object(bgctl, 'container_id', return_value=expected_id), \
                patch.object(bgctl, '_docker', return_value=subprocess.CompletedProcess([], 0, '', '')) as docker:
            bgctl.quarantine_candidate('prod', 'blue', expected_id, owner, IMAGE, str(directory))
            self.assertTrue(any(call.args == ('rm', '-f', expected_id) for call in docker.call_args_list))
        self.assertTrue(bgctl.quarantined('prod', 'blue'))
        self.assertEqual(json.loads(marker.read_text())['deadline_monotonic'], 0)
        bgctl.maintenance_lease.release(self.state, owner)
        recovery_owner = bgctl.maintenance_lease.acquire(self.state, os.getpid())
        source = directory / FILES[0]
        source.chmod(0o644)
        with self.assertRaises(ValueError):
            bgctl.recover_selection('prod', 'blue', recovery_owner)
        self.assertTrue(bgctl.quarantined('prod', 'blue'))
        source.chmod(0o444)
        with patch.object(bgctl, 'identity_query', return_value=IMAGE):
            bgctl.recover_selection('prod', 'blue', recovery_owner)
        self.assertFalse(bgctl.quarantined('prod', 'blue'))
        self.assertEqual(bgctl.load_state()['PROD_ARTIFACT_BLUE'], str(directory))

    def test_abort_never_changes_state_for_a_new_owner_active_color_or_container(self):
        owner = bgctl.maintenance_lease.acquire(self.state, os.getpid())
        marker = self.state / 'maintenance'
        original = json.loads(marker.read_text())
        before = bgctl.load_state()
        expected_id = 'a' * 64
        for change in ('owner', 'active', 'container'):
            with self.subTest(change=change):
                lease = {**original, 'owner': 'f' * 32} if change == 'owner' else original
                marker.write_text(json.dumps(lease))
                active = 'blue' if change == 'active' else 'green'
                actual = 'b' * 64 if change == 'container' else expected_id
                with patch.object(bgctl, 'active_color', return_value=active), \
                        patch.object(bgctl, 'container_id', return_value=actual), \
                        patch.object(bgctl, '_docker') as docker:
                    with self.assertRaises(RuntimeError):
                        bgctl.quarantine_candidate('prod', 'blue', expected_id, owner, IMAGE, '')
                    docker.assert_not_called()
                self.assertEqual(bgctl.load_state(), before)

    def test_post_switch_rejection_blocks_future_promotion_without_removing_an_active_container(self):
        reject = getattr(bgctl, 'reject_candidate', None)
        self.assertTrue(callable(reject), 'post-switch failure requires persistent rejection that preserves the active runtime')
        owner = bgctl.maintenance_lease.acquire(self.state, os.getpid())
        marker = self.state / 'maintenance'
        lease = json.loads(marker.read_text())
        lease['deadline_monotonic'] = 0
        marker.write_text(json.dumps(lease))
        expected_id = 'a' * 64
        with patch.object(bgctl, 'container_id', return_value=expected_id), patch.object(bgctl, '_docker') as docker:
            reject('prod', 'green', expected_id, owner, 'previous-green', '')
            docker.assert_not_called()
        self.assertTrue(bgctl.quarantined('prod', 'green'))
        self.assertEqual(json.loads(marker.read_text())['deadline_monotonic'], 0)
        with self.assertRaises(ValueError):
            bgctl.compose_cmd('prod', 'config')

    def test_guarded_reverse_switch_rechecks_owner_active_color_and_container_before_reload(self):
        rollback = getattr(bgctl, 'rollback_candidate', None)
        self.assertTrue(callable(rollback), 'rollback must check the captured candidate under the maintenance lock')
        owner = bgctl.maintenance_lease.acquire(self.state, os.getpid())
        marker = self.state / 'maintenance'
        original = json.loads(marker.read_text())
        expected_id = 'a' * 64
        for change in ('owner', 'expired', 'active', 'container'):
            with self.subTest(change=change):
                lease = dict(original)
                if change == 'owner':
                    lease['owner'] = 'f' * 32
                if change == 'expired':
                    lease['deadline_monotonic'] = 0
                marker.write_text(json.dumps(lease))
                active = 'blue' if change == 'active' else 'green'
                actual = 'b' * 64 if change == 'container' else expected_id
                with patch.object(bgctl, 'active_color', return_value=active), \
                        patch.object(bgctl, 'container_id', return_value=actual), \
                        patch.object(bgctl, 'flip_locked') as flip:
                    with self.assertRaises(RuntimeError):
                        rollback('prod', 'green', expected_id, 'blue', owner)
                    flip.assert_not_called()
        marker.write_text(json.dumps(original))
        with patch.object(bgctl, 'container_id', return_value=expected_id), \
                patch.object(bgctl, 'flip_locked', return_value=('green', 'blue', 0.1)) as flip:
            self.assertEqual(rollback('prod', 'green', expected_id, 'blue', owner), ('green', 'blue', 0.1))
            flip.assert_called_once_with('prod', 'blue')

    def test_post_switch_rejection_refuses_new_ownership_or_a_replacement_container(self):
        reject = getattr(bgctl, 'reject_candidate', None)
        self.assertTrue(callable(reject))
        owner = bgctl.maintenance_lease.acquire(self.state, os.getpid())
        marker = self.state / 'maintenance'
        original = json.loads(marker.read_text())
        before = bgctl.load_state()
        expected_id = 'a' * 64
        for change in ('owner', 'container'):
            with self.subTest(change=change):
                marker.write_text(json.dumps({**original, 'owner': 'f' * 32} if change == 'owner' else original))
                actual = 'b' * 64 if change == 'container' else expected_id
                with patch.object(bgctl, 'container_id', return_value=actual), patch.object(bgctl, '_docker') as docker:
                    with self.assertRaises(RuntimeError):
                        reject('prod', 'green', expected_id, owner, 'previous-green', '')
                    docker.assert_not_called()
                self.assertEqual(bgctl.load_state(), before)

    def test_watchdog_keeps_expiry_and_other_color_recovery_while_skipping_quarantine(self):
        import failover
        state = bgctl.load_state()
        state['PROD_QUARANTINE_BLUE'] = 'failed-deployment'
        bgctl.save_state(state)
        marker = self.state / 'maintenance'
        marker.touch()
        os.utime(marker, (0, 0))
        watchdog = failover.Watchdog('prod', 1.5, 3, 6, 1.0, False, False, self.state)
        self.assertFalse(watchdog.maintenance())
        watchdog.trackers['quasar-green'] = failover.Tracker()
        with patch.object(failover.threading, 'Thread') as thread:
            watchdog.restart_container(bgctl.container('prod', 'blue'), 'quasar-blue')
            thread.assert_not_called()
            watchdog.restart_container(bgctl.container('prod', 'green'), 'quasar-green')
            thread.assert_called_once()

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
    def run_deploy(self, *, fail_idle=False, fail_first=False, fail_restore=False, fail_before=False, fail_selection=False, require_maintenance=False, change_active=False, lease_loss='', overlay=False, active='green', post_failure='', post_change='', rollback_refused=False):
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
                'FAIL_BEFORE_RECREATE': str(int(fail_before)), 'FAIL_RESTORE_SELECTION': str(int(fail_selection)),
                'REQUIRE_MAINTENANCE': str(int(require_maintenance)), 'CHANGE_ACTIVE_AFTER_STAGE': str(int(change_active)),
                'AISIS_TEST_HELPERS_DIR': str(HERE), 'LEASE_LOSS_DURING_IDLE': lease_loss,
                'FAIL_SWITCH': str(int(post_failure == 'smoke')), 'FAIL_SPEAKER': str(int(post_failure == 'speaker')),
                'POST_FLIP_CHANGE': post_change, 'REFUSE_ROLLBACK': str(int(rollback_refused)),
                'AISIS_SPEAKER_WAIT_S': '0'}
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
        self.assertTrue(any(item[:5] == ['bg', 'quarantine-inactive', 'prod', 'blue', 'fixture-container-1'] for item in calls))
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

    def test_maintenance_is_acquired_before_reading_the_active_color(self):
        run, _calls, _state, logged = self.run_deploy(require_maintenance=True)
        self.assertEqual(run.returncode, 0, logged)

    def test_changed_active_color_is_rechecked_before_any_prod_replacement(self):
        run, calls, state, logged = self.run_deploy(change_active=True)
        self.assertNotEqual(run.returncode, 0)
        self.assertFalse(any(item[:3] == ['bg', 'set-image', 'prod'] for item in calls))
        self.assertEqual(state['actual']['image'], 'previous-blue')
        self.assertIn('active color changed', logged)

    def test_failed_selection_restore_is_reported_before_cleanup(self):
        run, _calls, state, logged = self.run_deploy(fail_first=True, fail_selection=True)
        self.assertNotEqual(run.returncode, 0)
        self.assertIsNone(state['actual'])
        self.assertIn('previous idle selection is not restored', logged)
        self.assertNotEqual(state['images']['blue'], 'previous-blue')
        self.assertTrue(state.get('quarantine'), 'normal recreation must remain blocked after failed recovery')

    def test_expired_lease_during_idle_gate_quarantines_and_removes_its_candidate(self):
        run, _calls, state, logged = self.run_deploy(lease_loss='expire')
        self.assertNotEqual(run.returncode, 0)
        self.assertIsNone(state['actual'], logged)
        self.assertTrue(state.get('quarantine'))
        self.assertEqual(state['active'], 'green')

    def test_lease_loss_preserves_a_replacement_owner_container_or_new_active_color(self):
        for change in ('owner', 'container', 'active'):
            with self.subTest(change=change):
                run, calls, state, _log = self.run_deploy(lease_loss=change)
                self.assertNotEqual(run.returncode, 0)
                self.assertIsNotNone(state['actual'])
                self.assertFalse(any(item[:2] == ['bg', 'remove-inactive'] for item in calls))
                self.assertFalse(state.get('quarantine'))
                if change == 'container':
                    self.assertEqual(state['actual_id'], 'another-owners-container')
                if change == 'active':
                    self.assertEqual(state['active'], 'blue')

    def test_post_switch_failures_confirm_rollback_and_quarantine_the_rejected_standby(self):
        for failure in ('smoke', 'speaker'):
            with self.subTest(failure=failure):
                run, _calls, state, logged = self.run_deploy(post_failure=failure)
                self.assertNotEqual(run.returncode, 0)
                self.assertEqual(state['active'], 'green', logged)
                self.assertIsNone(state['actual'], 'a rejected standby must not remain available for later promotion')
                self.assertTrue(state.get('quarantine'))
                self.assertIn('rollback confirmed', logged.lower())

    def test_refused_reverse_switch_is_not_reported_as_a_completed_rollback(self):
        run, _calls, state, logged = self.run_deploy(post_failure='smoke', rollback_refused=True)
        self.assertNotEqual(run.returncode, 0)
        self.assertEqual(state['active'], 'blue')
        self.assertIsNotNone(state['actual'], 'the active container must not be removed')
        self.assertTrue(state.get('quarantine'), 'rejected active bytes must not become a future unblocked standby')
        self.assertNotIn('rolled back to', logged.lower())
        self.assertNotIn('rollback confirmed', logged.lower())
        self.assertIn('rollback unconfirmed', logged.lower())

    def test_expired_post_switch_lease_rejects_candidate_without_stopping_the_active_container(self):
        run, _calls, state, logged = self.run_deploy(post_failure='smoke', post_change='expire')
        self.assertNotEqual(run.returncode, 0)
        self.assertEqual(state['active'], 'blue')
        self.assertIsNotNone(state['actual'])
        self.assertTrue(state.get('quarantine'))
        self.assertIn('rollback unconfirmed', logged.lower())

    def test_post_switch_abort_preserves_a_new_owner_or_replacement_container(self):
        for change in ('owner', 'container'):
            with self.subTest(change=change):
                run, _calls, state, logged = self.run_deploy(post_failure='smoke', post_change=change)
                self.assertNotEqual(run.returncode, 0)
                self.assertEqual(state['active'], 'blue')
                self.assertIsNotNone(state['actual'])
                self.assertFalse(state.get('quarantine'))
                self.assertNotIn('rollback confirmed', logged.lower())
                if change == 'container':
                    self.assertEqual(state['actual_id'], 'another-owners-container')

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
