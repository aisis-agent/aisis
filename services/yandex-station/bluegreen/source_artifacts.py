"""Verified source files mounted over an existing image; no local compilation.

The import verifies integrity, not release acceptance. The normal stage and idle
corpus gates still bind their own exported effective runtime before any flip.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tarfile
import tempfile

MODE = 'read-only-source-artifact-over-existing-image'
FILES = ('backend/ha-intents.ts', 'backend/tv-app-intents.ts')
HASH = re.compile(r'[0-9a-f]{64}')


def digest(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def checked_proof(path: Path) -> dict:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
        raise ValueError('missing or unsafe artifact proof')
    proof = json.loads(path.read_text())
    if not isinstance(proof, dict) or proof.get('deployment_mode') != MODE:
        raise ValueError('unsupported artifact deployment mode')
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', str(proof.get('image_id', ''))):
        raise ValueError('artifact needs an immutable existing image identity')
    hashes = proof.get('overlay_sha256', {})
    if not isinstance(hashes, dict) or set(hashes) != {Path(name).name for name in FILES}:
        raise ValueError('artifact must contain exactly the two reviewed source files')
    for value in [proof.get('artifact_sha256', ''), proof.get('input_digest', ''), *hashes.values()]:
        if not isinstance(value, str) or HASH.fullmatch(value) is None:
            raise ValueError('invalid artifact digest')
    if not re.fullmatch(r'[0-9a-f]{40}', str(proof.get('source_commit', ''))):
        raise ValueError('artifact source commit is missing')
    return proof


def validate_artifact(directory: Path, image: str | None = None) -> dict:
    if not directory.is_absolute() or directory.is_symlink() or not directory.is_dir():
        raise ValueError('artifact directory must be a persistent absolute directory')
    proof = checked_proof(directory / 'artifact.json')
    if image is not None and proof['image_id'] != image:
        raise ValueError('source artifact belongs to a different image')
    if (directory / 'backend').is_symlink():
        raise ValueError('source artifact parent cannot be a link')
    for name in FILES:
        path = directory / name
        if path.is_symlink() or not path.is_file() or digest(path) != proof['overlay_sha256'][path.name]:
            raise ValueError('installed source artifact changed')
    return proof


def extract_sources(archive: Path, directory: Path, proof: dict) -> None:
    with tarfile.open(archive, 'r:') as bundle:
        members = bundle.getmembers()
        if len(members) != len(FILES) or {member.name for member in members} != set(FILES):
            raise ValueError('unexpected artifact archive inventory')
        for member in members:
            if not member.isfile() or member.size > 1_000_000:
                raise ValueError('artifact source must be a bounded regular file')
            path = directory / member.name
            path.parent.mkdir(exist_ok=True)
            with bundle.extractfile(member) as source, path.open('xb') as target:
                shutil.copyfileobj(source, target)
            if digest(path) != proof['overlay_sha256'][path.name]:
                raise ValueError('artifact source hash does not match its proof')


def make_read_only(directory: Path) -> None:
    for name in (*FILES, 'artifact.json'):
        (directory / name).chmod(0o444)
    (directory / 'backend').chmod(0o555)
    directory.chmod(0o555)


def remove_temporary(directory: Path) -> None:
    if directory.exists():
        directory.chmod(0o700)
        if (directory / 'backend').is_dir():
            (directory / 'backend').chmod(0o700)
        shutil.rmtree(directory)


def import_artifact(source: Path, store: Path) -> Path:
    proof = checked_proof(source / 'proof.json')
    archive = source / 'fireplace-artifact.tar'
    if archive.is_symlink() or not archive.is_file() or digest(archive) != proof['artifact_sha256']:
        raise ValueError('remote artifact archive hash changed')
    identity = proof['image_id'] + ':' + proof['artifact_sha256']
    destination = store.resolve() / hashlib.sha256(identity.encode()).hexdigest()
    if destination.exists():
        existing = validate_artifact(destination, proof['image_id'])
        if existing['overlay_sha256'] != proof['overlay_sha256']:
            raise ValueError('immutable artifact destination differs')
        return destination
    store.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = Path(tempfile.mkdtemp(prefix='.import-', dir=store))
    try:
        extract_sources(archive, temporary, proof)
        (temporary / 'artifact.json').write_text(json.dumps(proof, sort_keys=True, indent=2) + '\n')
        validate_artifact(temporary.resolve(), proof['image_id'])
        make_read_only(temporary)
        os.rename(temporary, destination)
        return destination
    finally:
        remove_temporary(temporary)


def compose_overlay(state: dict[str, str]) -> dict:
    services = {}
    for environment in ('prod', 'stage'):
        for color in ('blue', 'green'):
            prefix = environment.upper()
            path = state.get(f'{prefix}_ARTIFACT_{color.upper()}', '')
            if not path:
                continue
            directory = Path(path)
            validate_artifact(directory, state.get(f'{prefix}_IMG_{color.upper()}', ''))
            services[f'quasar-{environment}-{color}'] = {'volumes': [
                {'type': 'bind', 'source': str(directory / name), 'target': '/src/' + name,
                 'read_only': True, 'bind': {'create_host_path': False}}
                for name in FILES
            ]}
    return {'services': services}
