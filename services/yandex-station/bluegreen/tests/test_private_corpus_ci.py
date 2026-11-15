"""Bindings required before a private Actions result can satisfy a fresh runtime gate."""
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import stat
import tempfile
import unittest
import zipfile

HERE = Path(__file__).resolve().parents[1]


class PrivateCorpusCIContracts(unittest.TestCase):
    def setUp(self):
        path = HERE / 'private_corpus_ci.py'
        self.assertTrue(path.is_file(), 'the explicit private native-CI corpus backend is required')
        spec = importlib.util.spec_from_file_location('private_corpus_ci', path)
        self.api = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.api)
        self.request = 'a' * 32
        self.repository = {'id': self.api.REPOSITORY_ID, 'full_name': self.api.REPOSITORY, 'private': True}
        self.run = {
            'id': 12345, 'event': 'workflow_dispatch', 'workflow_id': self.api.WORKFLOW_ID,
            'head_sha': self.api.WORKFLOW_COMMIT, 'head_branch': self.api.WORKFLOW_REF,
            'run_attempt': 1, 'display_title': 'AISIS corpus ' + self.request,
            'repository': self.repository, 'head_repository': self.repository,
            'status': 'completed', 'conclusion': 'success',
        }
        self.artifact = {'id': 67890, 'name': 'corpus-proof-' + self.request,
            'expired': False, 'size_in_bytes': 1000, 'digest': 'sha256:' + 'b' * 64,
            'workflow_run': {'id': self.run['id'], 'repository_id': self.api.REPOSITORY_ID,
                             'head_repository_id': self.api.REPOSITORY_ID, 'head_sha': self.api.WORKFLOW_COMMIT}}
        self.expected = {
            'request_id': self.request, 'bundle_sha256': 'c' * 64, 'input_digest': 'd' * 64,
            'input_asset_id': '998877', 'input_manifest_sha256': 'e' * 64, 'archive_sha256': 'f' * 64,
            'counts': {'corpus': {'passed': 466, 'total': 466}, 'dialogs': {'passed': 195, 'total': 195}},
        }
        self.provenance = {
            'schema': 'aisis-private-ci-provenance-v1', 'backend': 'github-actions', 'mode': 'corpus',
            'repository_id': self.api.REPOSITORY_ID, 'repository': self.api.REPOSITORY,
            'run_id': self.run['id'], 'run_attempt': 1, 'head_sha': self.api.WORKFLOW_COMMIT,
            'runner_exit': 0, 'verification_passed': True,
            'gate_helper_sha256': self.api.GATE_HELPER_SHA256, **self.expected,
        }

    def test_repository_must_match_both_private_flag_and_numeric_identity(self):
        self.api.validate_repository(self.repository)
        for change in ({'private': False}, {'private': 1}, {'id': 9}, {'full_name': 'someone/another-repository'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.api.validate_repository({**self.repository, **change})

    def test_run_must_bind_request_pinned_workflow_and_first_successful_attempt(self):
        self.api.validate_run(self.run, self.request, completed=True)
        changes = [
            {'head_sha': '0' * 40}, {'head_branch': 'main'}, {'workflow_id': 99},
            {'event': 'push'}, {'run_attempt': 2}, {'conclusion': 'failure'},
            {'display_title': 'AISIS candidate ' + self.request},
            {'display_title': 'AISIS corpus ' + 'b' * 32},
            {'repository': {**self.repository, 'id': 1}},
            {'head_repository': {**self.repository, 'id': 1}},
        ]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.api.validate_run({**self.run, **change}, self.request, completed=True)

    def test_artifact_must_belong_to_that_run_and_have_the_exact_corpus_name(self):
        self.api.validate_artifact(self.artifact, self.run, self.request)
        changes = [
            {'name': 'candidate-proof-' + self.request}, {'expired': True},
            {'size_in_bytes': self.api.MAX_ARTIFACT_BYTES + 1},
            {'workflow_run': {**self.artifact['workflow_run'], 'id': 98765}},
            {'workflow_run': {**self.artifact['workflow_run'], 'head_sha': '0' * 40}},
            {'digest': None},
        ]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.api.validate_artifact({**self.artifact, **change}, self.run, self.request)

    def test_provenance_rejects_other_input_candidate_mode_or_unverified_results(self):
        self.api.validate_provenance(self.provenance, self.expected, self.run)
        for key in ('request_id', 'bundle_sha256', 'input_digest', 'input_asset_id',
                    'input_manifest_sha256', 'archive_sha256', 'gate_helper_sha256',
                    'run_id', 'head_sha', 'repository_id'):
            changed = copy.deepcopy(self.provenance)
            changed[key] = 'different'
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.api.validate_provenance(changed, self.expected, self.run)
        for change in ({'mode': 'candidate'}, {'runner_exit': 1}, {'verification_passed': False},
                       {'counts': {'corpus': {'passed': 1, 'total': 1}}}, {'run_attempt': 2}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.api.validate_provenance({**self.provenance, **change}, self.expected, self.run)

    def archive(self, entries):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as archive:
            for name, content in entries:
                archive.writestr(name, content)
        return stream.getvalue()

    def read_archive(self, payload, *, digest=None):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'proof.zip'
            path.write_bytes(payload)
            metadata = {**self.artifact, 'size_in_bytes': len(payload),
                        'digest': digest or 'sha256:' + hashlib.sha256(payload).hexdigest()}
            return self.api.read_artifact(path, metadata)

    def test_only_the_two_bounded_regular_json_members_are_read_without_extraction(self):
        entries = [('corpus-receipt.json', '{"schema":"receipt"}'),
                   ('provenance.json', json.dumps(self.provenance))]
        receipt, provenance = self.read_archive(self.archive(entries))
        self.assertEqual(receipt, {'schema': 'receipt'})
        self.assertEqual(provenance, self.provenance)
        variants = [
            [*entries, ('extra.json', '{}')],
            [*entries, ('../../escaped', '{}')],
            [('corpus-receipt.json', 'null'), entries[1]],
            [entries[0], ('provenance.json', '[]')],
            [entries[0], ('provenance.json', '{invalid')],
        ]
        link = zipfile.ZipInfo('provenance.json')
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        variants.append([entries[0], (link, 'elsewhere')])
        for number, variant in enumerate(variants):
            with self.subTest(number=number), self.assertRaises(ValueError):
                self.read_archive(self.archive(variant))
        with self.assertRaises(ValueError):
            self.read_archive(self.archive(entries), digest='sha256:' + '0' * 64)


if __name__ == '__main__':
    unittest.main()
