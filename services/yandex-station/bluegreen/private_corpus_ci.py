"""Explicit private Actions transport for the unchanged full corpus gate.

Only a fresh runtime is uploaded. The remote command and repository are pinned.
"""
from __future__ import annotations
import hashlib
import json
import logging
from pathlib import Path
import re
import stat
import subprocess
import tarfile
import tempfile
import time
import uuid
import zipfile

REPOSITORY = 'alex-mextner/aisis-fireplace-private-ci-20261115'
REPOSITORY_ID = 1458973050
WORKFLOW_PATH = '.github/workflows/verify.yml'
# Filled from the separately reviewed private harness before this backend is enabled.
WORKFLOW_ID = 0
WORKFLOW_REF = ''
WORKFLOW_COMMIT = ''
GATE_HELPER_SHA256 = '95690e7016c886feffc61409993135ea12986548270f53fa00de1904c9f2ae55'
MAX_ARTIFACT_BYTES = 8_000_000
MAX_INPUT_BYTES = 512_000_000
WAIT_SECONDS = 1200


class GitHubFailure(RuntimeError):
    pass


class CredentialUnavailable(GitHubFailure):
    pass


def sha256(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def require_configuration() -> None:
    if type(WORKFLOW_ID) is not int or WORKFLOW_ID <= 0:
        raise ValueError('private workflow identity is not pinned')
    if not re.fullmatch(r'[0-9a-f]{40}', WORKFLOW_COMMIT) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,100}', WORKFLOW_REF):
        raise ValueError('private workflow commit and release ref are not pinned')


def validate_repository(value: dict) -> None:
    if not isinstance(value, dict) or value.get('private') is not True:
        raise ValueError('corpus input requires the private repository')
    if value.get('id') != REPOSITORY_ID or value.get('full_name') != REPOSITORY:
        raise ValueError('private repository identity differs')


def validate_run(value: dict, request: str, *, completed: bool = False) -> None:
    if not re.fullmatch(r'[0-9a-f]{32}', request) or not isinstance(value, dict):
        raise ValueError('invalid private workflow request')
    expected = {'event': 'workflow_dispatch', 'workflow_id': WORKFLOW_ID,
                'head_sha': WORKFLOW_COMMIT, 'head_branch': WORKFLOW_REF,
                'run_attempt': 1, 'display_title': 'AISIS corpus ' + request}
    if any(value.get(key) != item for key, item in expected.items()):
        raise ValueError('workflow run is not the pinned first attempt for this request')
    if type(value.get('id')) is not int or value['id'] <= 0 or type(value.get('run_attempt')) is not int:
        raise ValueError('invalid workflow run identity')
    validate_repository(value.get('repository'))
    validate_repository(value.get('head_repository'))
    if completed and (value.get('status') != 'completed' or value.get('conclusion') != 'success'):
        raise ValueError('private workflow did not complete successfully')


def validate_artifact(value: dict, run: dict, request: str) -> None:
    if not isinstance(value, dict) or value.get('name') != 'corpus-proof-' + request:
        raise ValueError('expected the exact corpus artifact name')
    if value.get('expired') is not False or type(value.get('id')) is not int or value['id'] <= 0:
        raise ValueError('artifact is expired or has invalid identity')
    size = value.get('size_in_bytes')
    if type(size) is not int or not 0 < size <= MAX_ARTIFACT_BYTES:
        raise ValueError('corpus artifact exceeds its bound')
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', str(value.get('digest', ''))):
        raise ValueError('authoritative artifact digest is required')
    binding = value.get('workflow_run')
    expected = {'id': run['id'], 'repository_id': REPOSITORY_ID,
                'head_repository_id': REPOSITORY_ID, 'head_sha': WORKFLOW_COMMIT}
    if not isinstance(binding, dict) or any(binding.get(key) != item for key, item in expected.items()):
        raise ValueError('artifact belongs to another workflow run or source')


def validate_provenance(value: dict, expected: dict, run: dict) -> None:
    fields = {'schema': 'aisis-private-ci-provenance-v1', 'backend': 'github-actions',
              'mode': 'corpus', 'repository_id': REPOSITORY_ID, 'repository': REPOSITORY,
              'run_id': run['id'], 'run_attempt': 1, 'head_sha': WORKFLOW_COMMIT,
              'gate_helper_sha256': GATE_HELPER_SHA256, **expected}
    if not isinstance(value, dict) or any(value.get(key) != item for key, item in fields.items()):
        raise ValueError('private CI provenance does not match this runtime request')
    if type(value.get('runner_exit')) is not int or value['runner_exit'] != 0:
        raise ValueError('private CI runner did not succeed')
    if value.get('verification_passed') is not True:
        raise ValueError('private CI did not verify the complete corpus')
    if any(type(value.get(key)) is not int for key in ('repository_id', 'run_id', 'run_attempt')):
        raise ValueError('private CI provenance has malformed numeric identities')


def unique_object(pairs: list) -> dict:
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate JSON field in private CI evidence')
        value[key] = item
    return value


def read_artifact(path: Path, metadata: dict) -> tuple[dict, dict]:
    if path.stat().st_size > MAX_ARTIFACT_BYTES or metadata['digest'] != 'sha256:' + sha256(path):
        raise ValueError('downloaded artifact differs from its authoritative digest')
    limits = {'corpus-receipt.json': 6_000_000, 'provenance.json': 65_536}
    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if len(members) != 2 or {item.filename for item in members} != set(limits):
                raise ValueError('private CI artifact has unexpected members')
            documents = {}
            for item in members:
                mode = stat.S_IFMT(item.external_attr >> 16)
                if mode not in (0, stat.S_IFREG) or item.flag_bits & 1 or item.file_size > limits[item.filename]:
                    raise ValueError('private CI artifact member is not a bounded regular file')
                document = json.loads(archive.read(item), object_pairs_hook=unique_object)
                if not isinstance(document, dict):
                    raise ValueError('private CI evidence must be a JSON object')
                documents[item.filename] = document
    except (zipfile.BadZipFile, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError('private CI artifact is unreadable') from exc
    return documents['corpus-receipt.json'], documents['provenance.json']


class GitHub:
    def __init__(self, logger: logging.Logger):
        self.logger = logger
        self.credentials_blocked = False

    def command(self, arguments: list[str], *, payload: dict | None = None,
                destination: Path | None = None, timeout: int = 60) -> bytes:
        # The installed gh credential guard remains in control; tokens stay private.
        data = json.dumps(payload).encode() if payload is not None else None
        self.logger.info('private_ci_api operation=%s', arguments[0])
        handle = destination.open('xb') if destination is not None else None
        try:
            result = subprocess.run(['gh', *arguments], input=data,
                                    stdout=handle if handle else subprocess.PIPE,
                                    stderr=subprocess.PIPE, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise GitHubFailure('private GitHub command timed out; delivery may be unknown') from exc
        finally:
            if handle:
                handle.close()
        if result.returncode:
            if b'keyring locked' in result.stderr.lower() or b'keyring unreachable' in result.stderr.lower():
                self.credentials_blocked = True
                raise CredentialUnavailable('private GitHub credential store is unavailable; no retry or fallback')
            raise GitHubFailure('private GitHub command failed (exit=' + str(result.returncode) + ')')
        return result.stdout or b''

    def api(self, path: str, *, method: str = 'GET', payload: dict | None = None) -> dict:
        arguments = ['api', '--hostname', 'github.com', '--method', method, path]
        if payload is not None:
            arguments += ['--input', '-']
        output = self.command(arguments, payload=payload)
        if not output:
            return {}
        if len(output) > MAX_ARTIFACT_BYTES:
            raise ValueError('private GitHub metadata exceeded its bound')
        value = json.loads(output)
        if not isinstance(value, dict):
            raise ValueError('private GitHub metadata is not an object')
        return value

    def download(self, artifact: dict, destination: Path) -> None:
        self.command(['api', '--hostname', 'github.com',
                      f'repos/{REPOSITORY}/actions/artifacts/{artifact["id"]}/zip'],
                     destination=destination, timeout=120)


def validate_pin(client: GitHub) -> None:
    require_configuration()
    validate_repository(client.api('repos/' + REPOSITORY))
    workflow = client.api(f'repos/{REPOSITORY}/actions/workflows/{WORKFLOW_ID}')
    if workflow.get('id') != WORKFLOW_ID or workflow.get('path') != WORKFLOW_PATH or workflow.get('state') != 'active':
        raise ValueError('reviewed private workflow is not the active fixed workflow')
    reference = client.api(f'repos/{REPOSITORY}/git/ref/tags/{WORKFLOW_REF}').get('object', {})
    if reference.get('type') != 'commit' or reference.get('sha') != WORKFLOW_COMMIT:
        raise ValueError('private workflow release ref moved or is not the pinned commit')


def pack_input(bundle: Path, source: dict, destination: Path) -> dict:
    manifest, archive = bundle / 'manifest.json', bundle / 'rootfs.tar.gz'
    for path in (manifest, archive):
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_INPUT_BYTES:
            raise ValueError('private CI input must be bounded regular files')
    if json.loads(manifest.read_text()) != source or sha256(archive) != source.get('archive_sha256'):
        raise ValueError('fresh private CI input changed before upload')
    with tarfile.open(destination, 'w', format=tarfile.USTAR_FORMAT) as output:
        for path in (manifest, archive):
            info = tarfile.TarInfo(path.name)
            info.size, info.mode, info.mtime = path.stat().st_size, 0o600, 0
            with path.open('rb') as stream:
                output.addfile(info, stream)
    if destination.stat().st_size > MAX_INPUT_BYTES:
        raise ValueError('private CI input bundle exceeded its bound')
    return {'bundle_sha256': sha256(destination), 'input_manifest_sha256': sha256(manifest),
            'archive_sha256': source['archive_sha256']}


def create_input(client: GitHub, request: str) -> tuple[dict, str]:
    tag = 'corpus-input-' + request
    body = 'Owned temporary AISIS corpus request ' + request
    client.logger.info('private_ci_input_create_request tag=%s request=%s', tag, request)
    try:
        release = client.api(f'repos/{REPOSITORY}/releases', method='POST', payload={
            'tag_name': tag, 'target_commitish': WORKFLOW_COMMIT, 'name': tag,
            'body': body, 'draft': False, 'prerelease': True, 'make_latest': 'false',
        })
    except CredentialUnavailable:
        raise
    except GitHubFailure:
        client.logger.warning('private_ci_input_create_ack_unknown tag=%s; reconciling without creation retry', tag)
        release = client.api(f'repos/{REPOSITORY}/releases/tags/{tag}')
    if type(release.get('id')) is not int or release.get('tag_name') != tag or release.get('body') != body:
        raise ValueError('private input release identity is invalid')
    client.logger.info('private_ci_input_created release_id=%s tag=%s', release['id'], tag)
    return release, tag


def upload_input(client: GitHub, release: dict, tag: str, archive: Path) -> str:
    client.command(['release', 'upload', tag, str(archive), '--repo', REPOSITORY], timeout=180)
    current = client.api(f'repos/{REPOSITORY}/releases/{release["id"]}')
    assets = [item for item in current.get('assets', []) if item.get('name') == archive.name]
    if current.get('tag_name') != tag or len(assets) != 1:
        raise ValueError('private input upload is not uniquely bound to its release')
    asset = assets[0]
    if type(asset.get('id')) is not int or asset['id'] <= 0 or asset.get('size') != archive.stat().st_size:
        raise ValueError('private input asset identity or size differs')
    return str(asset['id'])


def delete_input(client: GitHub, release: dict, tag: str, request: str) -> None:
    if client.credentials_blocked:
        client.logger.error('private_ci_cleanup_pending release_id=%s tag=%s reason=credential_store_unavailable',
                            release['id'], tag)
        return
    current = client.api(f'repos/{REPOSITORY}/releases/{release["id"]}')
    if current.get('tag_name') != tag or current.get('body') != 'Owned temporary AISIS corpus request ' + request:
        raise ValueError('input release ownership changed; cleanup refused')
    reference = client.api(f'repos/{REPOSITORY}/git/ref/tags/{tag}').get('object', {})
    if reference.get('type') != 'commit' or reference.get('sha') != WORKFLOW_COMMIT:
        raise ValueError('input release tag changed; cleanup refused')
    client.api(f'repos/{REPOSITORY}/releases/{release["id"]}', method='DELETE')
    client.api(f'repos/{REPOSITORY}/git/refs/tags/{tag}', method='DELETE')
    client.logger.info('private_ci_input_removed release_id=%s tag=%s', release['id'], tag)


def dispatch_once(client: GitHub, expected: dict) -> None:
    payload = {'ref': WORKFLOW_REF, 'inputs': {
        'mode': 'corpus', 'request_id': expected['request_id'],
        'input_asset_id': expected['input_asset_id'],
        'bundle_sha256': expected['bundle_sha256'], 'input_digest': expected['input_digest'],
    }}
    try:
        client.api(f'repos/{REPOSITORY}/actions/workflows/{WORKFLOW_ID}/dispatches',
                   method='POST', payload=payload)
    except CredentialUnavailable:
        raise
    except GitHubFailure:
        # A lost acknowledgement must never send the same launch again.
        client.logger.warning('private_ci_dispatch_ack_unknown request=%s; observing without redispatch',
                              expected['request_id'])


def observe_run(client: GitHub, request: str) -> dict | None:
    response = client.api(f'repos/{REPOSITORY}/actions/workflows/{WORKFLOW_ID}/runs?event=workflow_dispatch&per_page=100')
    matches = [item for item in response.get('workflow_runs', [])
               if item.get('display_title') == 'AISIS corpus ' + request]
    if len(matches) > 1:
        raise ValueError('more than one workflow run has this request identity')
    if not matches:
        return None
    value = client.api(f'repos/{REPOSITORY}/actions/runs/{matches[0]["id"]}')
    validate_run(value, request)
    return value


def wait_for_run(client: GitHub, request: str, owned: dict) -> dict:
    deadline = time.monotonic() + WAIT_SECONDS
    previous = None
    while time.monotonic() < deadline:
        value = observe_run(client, request)
        if value is not None:
            owned.update(id=value['id'], status=value['status'])
            observed = (value['id'], value['status'])
            if observed != previous:
                client.logger.info('private_ci_run request=%s run_id=%s status=%s', request, *observed)
                previous = observed
            if value['status'] == 'completed':
                validate_run(value, request, completed=True)
                return value
        time.sleep(10)
    raise GitHubFailure('private CI request exceeded its bounded completion deadline')


def fetch_proof(client: GitHub, run: dict, request: str, directory: Path) -> tuple[dict, dict, dict]:
    response = client.api(f'repos/{REPOSITORY}/actions/runs/{run["id"]}/artifacts?per_page=100')
    matches = [item for item in response.get('artifacts', []) if item.get('name') == 'corpus-proof-' + request]
    if len(matches) != 1:
        raise ValueError('private CI must return exactly one corpus artifact')
    artifact = matches[0]
    validate_artifact(artifact, run, request)
    path = directory / 'corpus-proof.zip'
    client.download(artifact, path)
    receipt, provenance = read_artifact(path, artifact)
    return receipt, provenance, artifact


def run(bundle: Path, source: dict, logger: logging.Logger) -> tuple[dict, dict]:
    # Import the local verifier, which also verifies before/after effective runtime.
    # The private workflow independently executes its immutable reviewed helper.
    import corpus_runpod as gate
    client = GitHub(logger)
    validate_pin(client)
    request = uuid.uuid4().hex
    logger.info('private_ci_start request=%s input_digest=%s', request, gate.input_digest(source))
    release, tag, owned = None, 'corpus-input-' + request, {}
    with tempfile.TemporaryDirectory(prefix='private-ci-', dir=bundle.parent) as temporary:
        directory = Path(temporary)
        archive = directory / 'private-input.tar'
        expected = {'request_id': request, 'input_digest': gate.input_digest(source),
                    **pack_input(bundle, source, archive)}
        try:
            release, tag = create_input(client, request)
            expected['input_asset_id'] = upload_input(client, release, tag, archive)
            dispatch_once(client, expected)
            completed = wait_for_run(client, request, owned)
            receipt, provenance, artifact = fetch_proof(client, completed, request, directory)
            expected['counts'] = gate.verify_receipt(before=source, after=source, receipt=receipt, runner_exit=0)
            validate_provenance(provenance, expected, completed)
            authority = {
                'backend': 'github-actions', 'repository': REPOSITORY, 'repository_id': REPOSITORY_ID,
                'workflow_id': WORKFLOW_ID, 'workflow_commit': WORKFLOW_COMMIT, 'workflow_ref': WORKFLOW_REF,
                'run_id': completed['id'], 'run_attempt': completed['run_attempt'],
                'run_url': f'https://github.com/{REPOSITORY}/actions/runs/{completed["id"]}',
                'artifact_id': artifact['id'], 'artifact_sha256': artifact['digest'],
                'request_id': request, 'provenance': provenance,
            }
            logger.info('private_ci_verified run_id=%s artifact_id=%s counts=%s',
                        completed['id'], artifact['id'], expected['counts'])
            return receipt, authority
        finally:
            try:
                if owned.get('id') and owned.get('status') != 'completed' and not client.credentials_blocked:
                    client.api(f'repos/{REPOSITORY}/actions/runs/{owned["id"]}/cancel', method='POST')
                    logger.warning('private_ci_cancel_requested run_id=%s', owned['id'])
            except GitHubFailure:
                logger.error('private_ci_cancel_unconfirmed run_id=%s', owned.get('id'))
            finally:
                if release is not None:
                    delete_input(client, release, tag, request)
