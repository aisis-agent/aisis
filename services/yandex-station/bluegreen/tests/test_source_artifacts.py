"""Persistent source artifact identity and the existing deployment gates."""
import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import os
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
        with self.assertRaises((ValueError, RuntimeError)):
            bgctl.compose_cmd('prod', 'up', '-d')

    def test_changed_archive_and_unexpected_tar_members_cannot_be_imported(self):
        with (self.input / 'fireplace-artifact.tar').open('ab') as stream:
            stream.write(b'changed')
        command = getattr(bgctl, 'cmd_import_artifact', None)
        self.assertTrue(callable(command))
        with self.assertRaises((ValueError, RuntimeError)):
            command(argparse.Namespace(source=str(self.input)))
        self.assertFalse(list(self.state.glob('artifacts/*')))


class ArtifactDeploy(unittest.TestCase):
    def run_deploy(self, fail_idle=False):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ('deploy.sh',):
                shutil.copy2(HERE / name, root / name)
            state = root / 'state'
            state.mkdir()
            (state / 'state.env').write_text('PROD_ACTIVE=green\n')
            (root / 'state.json').write_text(json.dumps({'active': 'green', 'images': {'blue': 'previous-blue', 'green': 'rollback-image'}, 'artifacts': {'blue': '/previous/artifact', 'green': ''}}))
            helper = '''import json,os,pathlib,sys
root=pathlib.Path(__file__).parent
args=sys.argv[1:]
with (root/'calls.jsonl').open('a') as log:log.write(json.dumps(['bg',*args])+'\\n')
state=json.loads((root/'state.json').read_text()); cmd=args[0]
if cmd=='get':
 key=args[2]
 if key=='active':print(state['active'])
 elif key.startswith('IMG_'):print(state['images'][key[4:].lower()])
 elif key.startswith('ARTIFACT_'):print(state['artifacts'][key[9:].lower()])
elif cmd=='import-artifact':print('/installed/new-artifact')
elif cmd=='artifact-image':print('sha256:'+64*'1')
elif cmd=='set-image' and args[1]=='prod':
 state['images'][args[2]]=args[3];state['artifacts'][args[2]]=args[5] if len(args)>5 else ''
elif cmd=='flip':state['active']=args[2]
elif cmd=='smoke' and args[1:] == ['prod','blue'] and os.environ.get('FAIL_IDLE')=='1':sys.exit(1)
(root/'state.json').write_text(json.dumps(state))
'''
            (root / 'bgctl.py').write_text(helper)
            (root / 'corpus_runpod.py').write_text("import json,pathlib,sys\np=pathlib.Path(__file__).parent/'calls.jsonl'\nwith p.open('a') as log:log.write(json.dumps(['corpus',*sys.argv[1:]])+'\\n')\n")
            docker = root / 'docker'
            docker.write_text("#!/usr/bin/env python3\nimport json,pathlib,sys\np=pathlib.Path(__file__).parent/'calls.jsonl'\nwith p.open('a') as log:log.write(json.dumps(['docker',*sys.argv[1:]])+'\\n')\n")
            docker.chmod(0o755)
            environment = {**os.environ, 'PATH': str(root) + os.pathsep + os.environ['PATH'],
                           'AISIS_BG_STATE': str(state), 'FAIL_IDLE': str(int(fail_idle))}
            run = subprocess.run(['bash', str(root / 'deploy.sh'), 'deploy', '--artifact', str(root / 'remote-output')],
                                 env=environment, text=True, capture_output=True, timeout=20)
            calls = [json.loads(line) for line in (root / 'calls.jsonl').read_text().splitlines()] if (root / 'calls.jsonl').exists() else []
            return run, calls, json.loads((root / 'state.json').read_text())

    def test_artifact_deploy_keeps_all_stage_and_idle_gates_without_local_build(self):
        run, calls, state = self.run_deploy()
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertFalse(any(item[:2] == ['docker', 'build'] for item in calls))
        flip = calls.index(['bg', 'flip', 'prod', 'blue'])
        for color in ('stage-blue', 'stage-green', 'prod-blue'):
            check = ['corpus', '--container', 'aisis-quasar-' + color]
            self.assertTrue(any(item[:3] == check for item in calls[:flip]), color)
        self.assertEqual(state['active'], 'blue')
        self.assertEqual(state['images']['green'], 'rollback-image')
        self.assertEqual(state['artifacts']['blue'], '/installed/new-artifact')

    def test_failed_idle_checks_restore_image_and_artifact_without_flip(self):
        run, calls, state = self.run_deploy(fail_idle=True)
        self.assertNotEqual(run.returncode, 0)
        self.assertFalse(any(item[:3] == ['bg', 'flip', 'prod'] for item in calls))
        self.assertEqual(state['images']['blue'], 'previous-blue')
        self.assertEqual(state['artifacts']['blue'], '/previous/artifact')
        self.assertEqual(state['active'], 'green')


if __name__ == '__main__':
    unittest.main()
