"""Inert contracts for the source-equivalent corpus evidence boundary."""

import importlib.util
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import shutil
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("offloaded_corpus", HERE / "offloaded_corpus.py")
gate = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = gate
SPEC.loader.exec_module(gate)


class SummaryTests(unittest.TestCase):
    def test_complete_summary_requires_real_zero_exit(self):
        self.assertEqual(
            gate.suite_result("corpus", 0, "noise\nИТОГ: 469/469 (100.0%)\n"),
            {"exit_code": 0, "passed": 469, "total": 469},
        )
        with self.assertRaisesRegex(ValueError, "exit"):
            gate.suite_result("corpus", 7, "ИТОГ: 469/469 (100.0%)\n")

    def test_dialog_total_is_fixed_not_merely_equal(self):
        self.assertEqual(
            gate.suite_result("dialogs", 0, "ДИАЛОГИ: 195/195\n"),
            {"exit_code": 0, "passed": 195, "total": 195},
        )
        with self.assertRaisesRegex(ValueError, "total"):
            gate.suite_result("dialogs", 0, "ДИАЛОГИ: 1/1\n")

    def test_missing_duplicate_partial_and_embedded_summaries_fail(self):
        bad = (
            "", "ИТОГ: 468/469\n", "ИТОГ: 469/469\nИТОГ: 469/469\n",
            "fake ИТОГ: 469/469\n", "ИТОГ: 0/0\n",
        )
        for text in bad:
            with self.subTest(text=text), self.assertRaises(ValueError):
                gate.suite_result("corpus", 0, text)

    def test_both_modes_and_both_suites_are_required(self):
        modes = [
            {"name": name, "grammar_hook": mode,
             "corpus": {"exit_code": 0, "passed": 469, "total": 469},
             "dialogs": {"exit_code": 0, "passed": 195, "total": 195}}
            for name, mode in (("stage", "off"), ("prod", "shadow"))
        ]
        gate.validate_results(modes, "shadow")
        for change in ("missing", "duplicate", "mode", "exit", "count"):
            rows = json.loads(json.dumps(modes))
            if change == "missing":
                rows.pop()
            elif change == "duplicate":
                rows[1] = rows[0]
            elif change == "mode":
                rows[1]["grammar_hook"] = "on"
            elif change == "exit":
                rows[1]["corpus"]["exit_code"] = 9
            else:
                rows[1]["dialogs"]["total"] = 1
            with self.subTest(change=change), self.assertRaises(ValueError):
                gate.validate_results(rows, "shadow")


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.archive = self.root / "payload.tar.gz"
        self.destination = self.root / "output"
        self.destination.mkdir()
        self.entries = {"src/example.ts": {"type": "file", "mode": 0o664,
                         "bytes": 3, "sha256": gate.digest(b"one")}}

    def archive_file(self, name="src/example.ts", data=b"one", mode=0o664, kind=tarfile.REGTYPE):
        with tarfile.open(self.archive, "w:gz") as stream:
            member = tarfile.TarInfo(name)
            member.type = kind
            member.mode = mode
            member.size = len(data) if kind == tarfile.REGTYPE else 0
            member.linkname = "../../outside"
            stream.addfile(member, io.BytesIO(data) if member.isfile() else None)

    def test_private_parent_preserves_the_declared_original_file_mode(self):
        self.archive_file()
        gate.extract_payload(self.archive, self.destination, self.entries)

        self.assertEqual((self.destination / "src/example.ts").read_bytes(), b"one")
        self.assertEqual((self.destination / "src/example.ts").stat().st_mode & 0o777, 0o664)

    def test_paths_links_and_bytes_cannot_escape_the_declared_payload(self):
        cases = [("../outside", b"one", tarfile.REGTYPE, "unsafe archive path"),
                 ("src/undeclared.ts", b"one", tarfile.REGTYPE, "unexpected archive member"),
                 ("src/example.ts", b"two", tarfile.REGTYPE, "file content differs"),
                 ("src/example.ts", b"", tarfile.LNKTYPE, "unsafe archive member type")]
        for index, (name, data, kind, error) in enumerate(cases):
            with self.subTest(name=name, kind=kind):
                self.archive_file(name, data, kind=kind)
                destination = self.root / str(index)
                destination.mkdir()
                with self.assertRaisesRegex(ValueError, error):
                    gate.extract_payload(self.archive, destination, self.entries)

    def test_archive_mode_mismatch_is_not_silently_normalized(self):
        self.entries["src/example.ts"]["mode"] = 0o644
        self.archive_file(mode=0o664)
        with self.assertRaisesRegex(ValueError, "mode"):
            gate.extract_payload(self.archive, self.destination, self.entries)


class AdapterLayerTests(unittest.TestCase):
    def setUp(self):
        files = []
        self.source = HERE.parent / "code"
        for name in gate.ADAPTER_FILES:
            data = (self.source / name).read_bytes()
            git_object = b"blob " + str(len(data)).encode() + b"\0" + data
            files.append({"repo_path": "services/yandex-station/code/" + name,
                          "archive_path": "verification/adapter/" + name,
                          "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data), "mode": 0o644,
                          "git_blob": hashlib.sha1(git_object, usedforsecurity=False).hexdigest()})
        self.manifest = {"adapter_layer": {"schema": "aisis-adapter-layer-source/v1",
                         "source_repo": "https://github.com/alex-mextner/aisis",
                         **gate.ADAPTER_REVISION, "files": files},
                         "inventory": {"source_files": [], "packages": [], "links": []}, "fixtures": []}

    def test_pinned_revision_matches_current_adapter_files(self):
        # ADAPTER_REVISION names the commit whose tree the manifest must come from; if the
        # adapter files change (new grammar bundle) without a re-pin, the pinned commit no
        # longer contains the bytes in ADAPTER_FILES.
        commit = gate.ADAPTER_REVISION["source_commit"]
        root = HERE.parents[2]
        probe = subprocess.run(["git", "cat-file", "-e", commit + "^{commit}"], cwd=root,
                               capture_output=True)
        self.assertEqual(probe.returncode, 0, "pinned adapter commit is missing; fetch full history")
        tree = subprocess.run(["git", "rev-parse", commit + "^{tree}"], cwd=root, check=True,
                              capture_output=True, text=True).stdout.strip()
        self.assertEqual(tree, gate.ADAPTER_REVISION["source_tree"])
        for name, checksum in gate.ADAPTER_FILES.items():
            data = subprocess.run(
                ["git", "show", commit + ":services/yandex-station/code/" + name], cwd=root,
                check=True, capture_output=True).stdout
            with self.subTest(name=name):
                self.assertEqual(hashlib.sha256(data).hexdigest(), checksum)
                self.assertEqual((self.source / name).read_bytes(), data)

    def test_reviewed_adapter_files_are_not_claimed_as_quasar_image_files(self):
        gate.validate_adapter_layer(self.manifest)
        self.assertEqual(gate.inventory_entries(self.manifest, include_fixtures=False), {})
        exported = gate.inventory_entries(self.manifest)
        self.assertEqual(set(exported), {"verification/adapter/" + name for name in gate.ADAPTER_FILES})

    def test_changed_adapter_authority_paths_or_bytes_are_rejected(self):
        for change in ("revision", "path", "hash", "extra"):
            manifest = json.loads(json.dumps(self.manifest))
            adapter = manifest["adapter_layer"]
            if change == "revision":
                adapter["source_commit"] = "0" * 40
            elif change == "path":
                adapter["files"][0]["archive_path"] = "../outside.py"
            elif change == "hash":
                adapter["files"][0]["sha256"] = "0" * 64
            else:
                adapter["files"].append(dict(adapter["files"][0]))
            with self.subTest(change=change), self.assertRaises(ValueError):
                gate.validate_adapter_layer(manifest)

    def test_compiled_grammar_and_git_blob_provenance_are_both_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for item in self.manifest["adapter_layer"]["files"]:
                path = root / item["archive_path"]
                path.parent.mkdir(parents=True, exist_ok=True)
                name = item["repo_path"].removeprefix("services/yandex-station/code/")
                path.write_bytes((self.source / name).read_bytes())
            gate.verify_adapter_bundle(root, self.manifest)
            changed = json.loads(json.dumps(self.manifest))
            changed["adapter_layer"]["files"][0]["git_blob"] = "0" * 40
            with self.assertRaisesRegex(ValueError, "Git blob"):
                gate.verify_adapter_bundle(root, changed)
            bundle = root / "verification/adapter/intent_grammar/grammar/ru.json"
            data = json.loads(bundle.read_text())
            data["source_sha256"] = "0" * 64
            bundle.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, "grammar provenance"):
                gate.verify_adapter_bundle(root, self.manifest)

class NativeEvidenceTests(unittest.TestCase):
    def test_only_the_selected_raw_ledger_line_is_parsed(self):
        command = ["python3", "offloaded_corpus.py", "run"]
        row = {"event": "run", "cmd": "python3 offloaded_corpus.py run",
               "pod_id": "chosen", "exit": 0, "command_s": 12}
        chosen = (json.dumps(row) + "\n").encode()
        duplicate_keys = b'{"event":"other","event":"duplicate"}\n'
        torn_tail = b'{"event":"run","cmd":'
        found, line_sha = gate.matching_ledger(
            duplicate_keys + chosen + torn_tail, command, "chosen", gate.digest(chosen))
        self.assertEqual(found, row)
        self.assertEqual(line_sha, gate.digest(chosen))
        for invalid in (duplicate_keys, torn_tail):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                gate.matching_ledger(invalid, command, "chosen", gate.digest(invalid))

    def test_explicit_native_identity_selects_one_success_after_a_failed_attempt(self):
        command = ["python3", "offloaded_corpus.py", "run"]
        base = {"event": "run", "cmd": "python3 offloaded_corpus.py run", "command_s": 12}
        failed = (json.dumps({**base, "pod_id": "previous", "exit": 124}) + "\n").encode()
        chosen = (json.dumps({**base, "pod_id": "chosen", "exit": 0}) + "\n").encode()
        another = (json.dumps({**base, "pod_id": "other", "exit": 0}) + "\n").encode()
        row, line_sha = gate.matching_ledger(failed + chosen + another, command, "chosen", gate.digest(chosen))
        self.assertEqual(row["pod_id"], "chosen")
        self.assertEqual(line_sha, gate.digest(chosen))
        with self.assertRaises(ValueError):
            gate.matching_ledger(failed + chosen, command, "previous", gate.digest(failed))

    def test_lifecycle_requires_the_whole_ordered_selected_native_execution(self):
        row = {"pod_id": "chosen", "cmd": "python3 offloaded_corpus.py run"}
        command = ["python3", "offloaded_corpus.py", "run"]
        run_path = Path("/owner/.cache/runpod-run/runs/real.log")
        output = b"real command output\n"
        pieces = ["runpod-run: waiting for pod chosen to answer ssh…\n",
                  "runpod-run: running as runner: python3 offloaded_corpus.py run\n",
                  output.decode(),
                  "runpod-run: exit 0 after 25s (log: /owner/.cache/runpod-run/runs/real.log)\n",
                  "runpod-run: pod chosen terminated\n"]
        gate.validate_lifecycle("".join(pieces).encode(), row, command, run_path, output)
        invalid = [pieces[-1], "".join(reversed(pieces)),
                   "".join(pieces).replace("real.log", "another.log"),
                   "".join(pieces).replace("real command output", "copied success"),
                   "".join(pieces) + "runpod-run: COULD NOT terminate pod chosen\n"]
        for text in invalid:
            with self.subTest(text=text), self.assertRaises(ValueError):
                gate.validate_lifecycle(text.encode(), row, command, run_path, output)

    def test_native_record_must_be_real_success_and_unambiguous(self):
        command = ["python3", "offloaded_corpus.py", "run"]
        row = {"event": "run", "cmd": "python3 offloaded_corpus.py run", "pod_id": "pod-one",
               "command_s": 12, "exit": 0}
        encoded = (json.dumps(row) + "\n").encode()
        found, line_hash = gate.matching_ledger(encoded, command)
        self.assertEqual(found["pod_id"], "pod-one")
        self.assertEqual(line_hash, gate.digest(encoded))
        invalid = [b"", encoded + encoded,
                   (json.dumps({**row, "exit": 7}) + "\n").encode(),
                   (json.dumps({**row, "event": "dedup", "command_s": 0}) + "\n").encode(),
                   (json.dumps({**row, "command_s": None}) + "\n").encode()]
        for data in invalid:
            with self.subTest(data=data), self.assertRaises(ValueError):
                gate.matching_ledger(data, command)

    def test_receipt_and_selected_native_logs_must_be_private(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.jsonl"
            path.write_bytes(b"evidence")
            path.chmod(0o600)
            self.assertEqual(gate.private_bytes(path), b"evidence")
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                gate.private_bytes(path)


class RunnerEnvironmentTests(unittest.TestCase):
    def test_host_credentials_and_reachable_default_endpoints_are_not_inherited(self):
        captured = []

        def run(command, **kwargs):
            captured.append(kwargs["env"])
            name = Path(command[-1]).stem
            output = "ИТОГ: 469/469\n" if name == "run_corpus" else "ДИАЛОГИ: 195/195\n"
            return type("Process", (), {"returncode": 0, "stdout": output, "stderr": ""})()

        with patch.dict(os.environ, {"HA_TOKEN": "never-copy", "ASR_CORRECT": "off"}), \
                patch.object(gate.subprocess, "run", run):
            gate.run_modes(Path("/payload"), Path("/runtime/node"), "on", lambda *a, **k: None)
        self.assertEqual(len(captured), 4)
        for env in captured:
            self.assertNotIn("HA_TOKEN", env)
            self.assertNotIn("ASR_CORRECT", env)
            self.assertEqual(env["NODE_ENV"], "production")
            self.assertEqual(env["TSX_DISABLE_CACHE"], "1")
            self.assertEqual(env["NODE_OPTIONS"], "--no-global-search-paths")
            self.assertEqual(env["AISIS_CORPUS_PYTHON"], str(Path(sys.executable).resolve()))
            self.assertEqual(env["AISIS_CORPUS_ADAPTER_DIR"], "/payload/verification/adapter")
            self.assertEqual(env["AISIS_ADAPTER_MUSIC_URL"], "http://127.0.0.1:9/music/play")
            self.assertEqual(env["AISIS_ADAPTER_LAYA_URL"], "http://127.0.0.1:9/laya")
            self.assertEqual(env["AISIS_ADAPTER_ASR_URL"], "http://127.0.0.1:9/asr/correct")
            self.assertEqual(env["AISIS_ADAPTER_INTENT_URL"], "http://127.0.0.1:9/intent/decide")
            self.assertEqual(env["AISIS_SETTINGS_URL"], "http://127.0.0.1:9/api/settings")


class ContainerBoundaryTests(unittest.TestCase):
    def test_existing_fillers_and_state_mounts_remain_compatible(self):
        gate.verify_mounts([{"Destination": "/src/fillers"}, {"Destination": "/state"}])
        for path in ("/", "/src", "/src/backend", "/src/fillers/other", "/node_modules", "/pnpm-workspace.yaml"):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "mount"):
                gate.verify_mounts([{"Destination": path}])

    def test_source_parent_mount_including_root_is_rejected_before_exec(self):
        data = {"State": {"Running": True}, "Image": "candidate", "Mounts": [{"Destination": "/"}]}
        with patch.object(gate, "command_json", return_value=[data]), \
                patch.object(gate, "verify_image"), patch.object(gate.subprocess, "run") as run:
            with self.assertRaisesRegex(ValueError, "mount"):
                gate.verify_container({}, "aisis-quasar-stage-blue")
            run.assert_not_called()


class DependencyInspectionTests(unittest.TestCase):
    """Run only the metadata inspector against inert temporary files in CI."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.package = self.root / "package"
        self.package.mkdir()
        (self.package / "index.js").write_bytes(b"unchanged")
        self.want = {"type": "file", "mode": 0o644, "bytes": 9, "sha256": gate.digest(b"unchanged")}
        (self.package / "index.js").chmod(0o644)
        self.spec = {"entries": {str(self.package / "index.js").lstrip("/"): self.want},
                     "packages": [{"root": str(self.package), "entries": [{"path": "index.js"}]}],
                     "excluded": [], "sources": []}

    def inspect(self):
        node = shutil.which("node")
        self.assertIsNotNone(node, "CI must provide the existing Node test runtime")
        return subprocess.run([node, "-e", gate.CONTAINER_CHECK], input=json.dumps(self.spec),
                              env={"PATH": os.environ.get("PATH", ""), "HOME": str(self.root)},
                              capture_output=True, text=True, timeout=5)

    def test_positive_declared_package_and_explicit_mode(self):
        result = self.inspect()
        self.assertEqual(result.returncode, 0, result.stderr)
        # Missing mode must stay missing, never masquerade as stage 'off'.
        self.assertIsNone(json.loads(result.stdout)["grammar_hook"])

    def test_undeclared_nested_dependency_fails_even_when_original_files_match(self):
        hidden = self.package / "node_modules/long"
        hidden.mkdir(parents=True)
        (hidden / "index.js").write_bytes(b"shadow")
        self.assertNotEqual(self.inspect().returncode, 0)

    def test_symlinked_package_root_is_not_a_matching_dependency(self):
        alias = self.root / "alias"
        alias.symlink_to(self.package, target_is_directory=True)
        self.spec["packages"][0]["root"] = str(alias)
        self.spec["entries"] = {str(alias / "index.js").lstrip("/"): self.want}
        self.assertNotEqual(self.inspect().returncode, 0)

    def test_only_the_declared_generated_shim_is_allowed(self):
        shim = self.package / "node_modules/.bin/esbuild"
        shim.parent.mkdir(parents=True)
        shim.write_bytes(b"not executed")
        self.spec["excluded"] = [{"path": str(shim), "bytes": 12, "sha256": gate.digest(b"not executed")}]
        self.assertEqual(self.inspect().returncode, 0)
        shim.write_bytes(b"changed shim")
        self.assertNotEqual(self.inspect().returncode, 0)

    def test_source_ancestor_cannot_supply_an_undeclared_dependency(self):
        source = self.root / "source/code.js"
        source.parent.mkdir()
        source.write_bytes(b"unchanged")
        source.chmod(0o644)
        self.spec["entries"][str(source).lstrip("/")] = self.want
        self.spec["sources"] = [str(source)]
        self.assertEqual(self.inspect().returncode, 0)
        (source.parent / "node_modules").mkdir()
        self.assertNotEqual(self.inspect().returncode, 0)

    def test_source_ancestor_cannot_change_module_or_typescript_configuration(self):
        source = self.root / "source/code.js"
        source.parent.mkdir()
        source.write_bytes(b"unchanged")
        source.chmod(0o644)
        self.spec["entries"][str(source).lstrip("/")] = self.want
        self.spec["sources"] = [str(source)]
        self.assertEqual(self.inspect().returncode, 0)
        for name in ("package.json", "tsconfig.json"):
            with self.subTest(name=name):
                config = source.parent / name
                config.write_text("{}")
                self.assertNotEqual(self.inspect().returncode, 0)
                config.unlink()


class DeploymentIntegrationTests(unittest.TestCase):
    def test_missing_evidence_aborts_before_creating_deployment_state(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            env = {**os.environ, "AISIS_BG_STATE": str(state)}
            env.pop("AISIS_CORPUS_RECEIPT", None)
            process = subprocess.run(
                ["bash", str(HERE.parent / "bluegreen/deploy.sh"), "deploy",
                 "--image", "sha256:" + "1" * 64],
                env=env, capture_output=True, text=True, timeout=5,
            )
            self.assertNotEqual(process.returncode, 0)
            self.assertIn("corpus-receipt", process.stderr)
            self.assertFalse(state.exists())

    def test_missing_receipt_cannot_fall_back_to_a_build_or_service_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state"
            process = subprocess.run(
                ["bash", str(HERE.parent / "bluegreen/deploy.sh"), "deploy",
                 "--image", "sha256:" + "1" * 64,
                 "--corpus-receipt", str(Path(directory) / "missing.json")],
                env={**os.environ, "AISIS_BG_STATE": str(state)},
                capture_output=True, text=True, timeout=5,
            )
            self.assertNotEqual(process.returncode, 0)
            self.assertIn("no containers changed", process.stderr)
            self.assertFalse(state.exists())


class ResultIdentityTests(unittest.TestCase):
    def test_copied_success_cannot_change_the_exact_execution_identity(self):
        result = {
            "schema": "aisis-corpus-result/v1", "execution_kind": "runpod-source",
            "image_executed_remote": False, "manifest_sha256": "a" * 64,
            "bundle_sha256": "b" * 64, "runner_sha256": "c" * 64,
            "node_sha256": gate.NODE_SHA,
            "test_runtime_inputs": gate.TEST_RUNTIME_INPUTS,
            "python_runtime": {"implementation": "CPython", "version": "3.12.0",
                               "executable": "/usr/bin/python3.12", "binary_sha256": "e" * 64},
            "modes": [{"name": name, "grammar_hook": grammar,
                       "corpus": {"exit_code": 0, "passed": 469, "total": 469},
                       "dialogs": {"exit_code": 0, "passed": 195, "total": 195}}
                      for name, grammar in (("stage", "off"), ("prod", "on"))],
        }
        raw = ("AISIS_CORPUS_RESULT_V1=" + json.dumps(result) + "\n").encode()
        self.assertEqual(gate.native_result(raw, "a" * 64, "b" * 64, "c" * 64, "on")["modes"][1]["name"], "prod")
        for field in ("manifest_sha256", "bundle_sha256", "runner_sha256", "node_sha256", "test_runtime_inputs"):
            changed = {**result, field: "d" * 64}
            data = ("AISIS_CORPUS_RESULT_V1=" + json.dumps(changed) + "\n").encode()
            with self.subTest(field=field), self.assertRaises(ValueError):
                gate.native_result(data, "a" * 64, "b" * 64, "c" * 64, "on")

    def test_actual_python_identity_is_required_in_native_evidence(self):
        valid = {"implementation": "CPython", "version": "3.12.0",
                 "executable": "/usr/bin/python3.12", "binary_sha256": "e" * 64}
        gate.validate_python_runtime(valid)
        invalid = [None, {}, {**valid, "version": "3.10.0"},
                   {**valid, "executable": "python3"}, {**valid, "binary_sha256": None}]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                gate.validate_python_runtime(value)


class SourceClosureTests(unittest.TestCase):
    def manifest(self, paths):
        provider_hash = "a" * 64
        files = [{"path": path, "sha256": provider_hash, "bytes": 1, "mode": 0o644} for path in sorted(paths)]
        shims = ["/node_modules/.pnpm/tsx/node_modules/.bin/esbuild",
                 "/node_modules/.pnpm/esbuild/node_modules/.bin/esbuild"]
        return {"baseline_image": "image", "overlay_revision": {"commit": "c" * 40, "tree": "d" * 40},
                "fixtures": [{"name": name, "sha256": checksum} for name, checksum in gate.FIXTURES.items()],
                "inventory": {"schema": "aisis-candidate-source-metadata/v1", "forbidden_paths": [],
                              "baseline_image_id": "image", "maintained_overlay_merge": "c" * 40,
                              "maintained_overlay_tree": "d" * 40, "source_files": files,
                              "provider_postimage_sha256": provider_hash, "packages": [], "links": [],
                              "excluded_package_paths": [{"path": path, "sha256": "b" * 64, "bytes": 1}
                                                         for path in shims]}}

    def validate(self, paths):
        roots = {"tsx": "/node_modules/.pnpm/tsx", "esbuild": "/node_modules/.pnpm/esbuild"}
        with patch.object(gate, "validate_package_roots", return_value=roots), \
                patch.object(gate, "validate_links"):
            gate.validate_inventory(self.manifest(paths))

    def test_negation_module_is_part_of_the_declared_source_closure(self):
        self.assertIn("/src/backend/negation.ts", gate.SOURCE_PATHS)
        self.validate(gate.SOURCE_PATHS)

    def test_an_image_without_or_with_an_extra_source_file_is_rejected(self):
        for paths in (gate.SOURCE_PATHS - {"/src/backend/negation.ts"},
                      gate.SOURCE_PATHS | {"/src/backend/not-declared.ts"}):
            with self.subTest(count=len(paths)), self.assertRaisesRegex(ValueError, "source closure differs"):
                self.validate(paths)


if __name__ == "__main__":
    unittest.main()
