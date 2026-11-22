#!/usr/bin/env python3
"""Bind isolated corpus and layer contracts to native evidence and an image."""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import posixpath
import pwd
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request

SCHEMA = "aisis-corpus-offload/v1"
RESULT_SCHEMA = "aisis-corpus-result/v1"
RECEIPT_SCHEMA = "aisis-corpus-receipt/v1"
MARKER = "AISIS_CORPUS_RESULT_V1="
TOTALS = {"corpus": 469, "dialogs": 195}
LABELS = {"corpus": "ИТОГ", "dialogs": "ДИАЛОГИ"}
ADAPTER_URL = "http://127.0.0.1:9/intent/decide"
CLOSED_ENDPOINTS = {
    "AISIS_ADAPTER_MUSIC_URL": "http://127.0.0.1:9/music/play",
    "AISIS_ADAPTER_LAYA_URL": "http://127.0.0.1:9/laya",
    "AISIS_ADAPTER_ASR_URL": "http://127.0.0.1:9/asr/correct",
    "AISIS_ADAPTER_INTENT_URL": ADAPTER_URL,
    "AISIS_SETTINGS_URL": "http://127.0.0.1:9/api/settings",
}
DEFAULT_FLAGS = {"ASR_CORRECT": None, "SEMANTIC_INTENTS": None, "AISIS_FAST_EARCON": None}
TEST_RUNTIME_INPUTS = {"NODE_ENV": "production", "TSX_DISABLE_CACHE": "1",
                       "NODE_OPTIONS": "--no-global-search-paths"}
NODE_URL = "https://nodejs.org/dist/v25.9.0/node-v25.9.0-linux-x64.tar.xz"
NODE_ARCHIVE_SHA = "1d8db7d6e291d167e8c467ae4094be175e1a0b3969c7ae1f8955b9f7824f7b2e"
NODE_SHA = "7fd113446e9171a28885227b3c275ff965b2fd1f45ccf1fb3a1c20308f34e627"
FIXTURES = {
    "intents-ru.jsonl": "249a0fd34e3e4c5d697bd1e34465d62cf356de5e513ba8e13b8f5d6b506c2280",
    "run_dialogs.mts": "5f02421d3f21d18a3158535aab03c404fd2ebc80e85b34d5ce403b89f7e66a1e",
    "run_corpus.mts": "0be3f4c146eef11839fae25e85d328cd1d04842d8dd4e85d4b242dd701078830",
    "expected_kind.mjs": "e7da6f96298f1158a9535595be3905be15db7efa5416d4e677987a0702ad62ec",
    "check_reminder_layers.py": "7a3e1a4ad8721ae5a76d799d6cb168786924bfb5de033dbeaf9ddadcfc2d597a",
}
MAINTAINED_RUNNERS = {"run_corpus.mts", "expected_kind.mjs", "check_reminder_layers.py"}
ADAPTER_REVISION = {"source_commit": "98b2b08e08b1169b2da7629652ac5bdd9b94e942",
                    "source_tree": "88380d40c45fc364b366f09b9dd2be75d9097469"}
ADAPTER_FILES = {
    "intent_grammar/__init__.py": "eacc35556edec256ac9c029ae1f2af467d7fc19bdb6a66358e9836455382f0c9",
    "intent_grammar/grammar.py": "14c4d4bc93b36f604bf839b66224ba325874ea1e2d0efe51e7e106e15c25a9d9",
    "intent_grammar/normalize.py": "fa6a18abc69edee2b5cc611c0b1fd98b093713c1db0c4a647f29bcbcc6d10483",
    "intent_grammar/pattern.py": "8ced88ef290369c6dfe62cd218eb9f4faf494633e4537dbb89f3e2e514df87ed",
    "intent_grammar/numerals.py": "c1f292178318a2ee8e3d0fbb454c89d8280f43929c52fc0d98017442f87e1e9c",
    "intent_grammar/timewords.py": "008f82b0cd9641a6be127e8aa4d457775cfa6080a29b4657d6f92e90fa5e1d7b",
    "intent_grammar/grammar/ru.json": "74a2211ca7eb3e618c62e562c06ad5a8c91c181d74018670af2585fba7261c87",
    "intent_grammar/grammar/ru.yaml": "8be6cd5086e7bfe90310175adbfe759548365c886b1594818e251f400ca2cee2",
}
SOURCE_PATHS = {
    "/src/backend/ha-intents.ts", "/src/backend/turn-memory.ts",
    "/src/routers/alice/base-provider.ts", "/src/routers/alice/directives.ts",
    "/src/backend/tv-app-intents.ts", "/src/logger.ts", "/src/protobuf.ts",
    "/src/protos/protos.js", "/src/backend/asr-correct.ts", "/src/backend/end-dialog.ts",
    "/src/backend/entity-index.ts", "/src/backend/fuzzy-ru.ts", "/src/backend/grammar-hook.ts",
    "/src/backend/intent-catalog.ts", "/src/backend/music-intents.ts",
    "/src/backend/music-parse.ts", "/src/backend/semantic-shadow.ts",
    "/src/backend/settings-voice.ts", "/src/backend/speaker-room.ts", "/src/backend/negation.ts",
    "/package.json", "/pnpm-lock.yaml", "/pnpm-workspace.yaml", "/tsconfig.json",
}
PACKAGE_NAMES = {
    "tsx", "tslog", "protobufjs", "zod", "esbuild", "@esbuild/linux-x64",
    "get-tsconfig", "resolve-pkg-maps", "long", "@types/node", "undici-types",
}


class SuiteFailure(ValueError):
    """Preserve the real process status separately from summary validation."""

    def __init__(self, exit_code):
        super().__init__("suite process exit was not zero")
        self.exit_code = exit_code


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def file_digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "duplicate JSON key")
        result[key] = value
    return result


def load_json(path):
    return json.loads(Path(path).read_text(), object_pairs_hook=unique_object)


def valid_hash(value, length=64):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{%d}" % length, value) is not None


def image_id(value):
    return isinstance(value, str) and value.startswith("sha256:") and valid_hash(value[7:])


def suite_result(name, exit_code, output):
    """A success-looking summary never hides the actual process failure."""
    require(type(exit_code) is int, "invalid suite exit")
    if exit_code:
        raise SuiteFailure(exit_code)
    pattern = rf"^{LABELS[name]}:\s*(\d+)/(\d+)(?:\s+\([^\n]*\))?\s*$"
    matches = re.findall(pattern, output, flags=re.MULTILINE)
    require(len(matches) == 1, "missing or duplicate complete suite summary")
    passed, total = map(int, matches[0])
    require(passed == total == TOTALS[name], "suite total or passed count differs")
    return {"exit_code": exit_code, "passed": passed, "total": total}


def validate_results(modes, prod_mode):
    require(isinstance(modes, list) and len(modes) == 2, "both tested modes are required")
    expected = {"stage": "off", "prod": prod_mode}
    require({m.get("name") for m in modes} == set(expected), "duplicate or missing mode")
    for mode in modes:
        require(mode.get("grammar_hook") == expected[mode["name"]], "untested grammar mode")
        for name, count in TOTALS.items():
            result = mode.get(name)
            require(isinstance(result, dict), "missing suite result")
            wanted = {"exit_code": 0, "passed": count, "total": count}
            require(result == wanted and all(type(v) is int for v in result.values()),
                    "failed or incomplete suite result")


def safe_path(value):
    require(isinstance(value, str) and value.startswith("/"), "inventory path is not absolute")
    path = PurePosixPath(value)
    require(".." not in path.parts and str(path) == value and value != "/", "unsafe inventory path")
    return value.lstrip("/")


def package_rows(package):
    rows = []
    for entry in package["entries"]:
        if entry["type"] == "file":
            rows.append([entry["path"], "file", entry["mode"], entry["sha256"]])
        else:
            require(entry["type"] == "symlink", "unsupported package entry")
            rows.append([entry["path"], "symlink", entry["target"]])
    # Retain the collector's explicit localeCompare(path, 'en') ordering.
    return json.dumps(rows, separators=(",", ":"), ensure_ascii=False).encode()


def inventory_entries(manifest, include_fixtures=True):
    inventory = manifest["inventory"]
    entries = {}

    def add(path, item):
        key = safe_path(path)
        require(key not in entries, "duplicate inventory path")
        entries[key] = dict(item)

    for item in inventory["source_files"]:
        add(item["path"], {**item, "type": "file"})
    for package in inventory["packages"]:
        require(digest(package_rows(package)) == package["tree_sha256"], "package tree digest differs")
        for item in package["entries"]:
            require(not item["path"].startswith("/"), "absolute package member")
            add(package["root"] + "/" + item["path"], item)
    for link in inventory["links"]:
        add(link["path"], {**link, "type": "symlink"})
    if include_fixtures:
        for item in manifest["fixtures"]:
            add("/src/backend/corpus/" + item["name"], {**item, "type": "file"})
        for item in manifest["adapter_layer"]["files"]:
            add("/" + item["archive_path"], {**item, "type": "file"})
    return entries


def validate_adapter_layer(manifest):
    adapter = manifest["adapter_layer"]
    require(adapter.get("schema") == "aisis-adapter-layer-source/v1"
            and adapter.get("source_repo") == "https://github.com/alex-mextner/aisis",
            "adapter source authority differs")
    require(all(adapter.get(key) == value for key, value in ADAPTER_REVISION.items()),
            "adapter source revision differs")
    files = adapter["files"]
    expected = {"verification/adapter/" + name: checksum for name, checksum in ADAPTER_FILES.items()}
    require(len(files) == len(expected)
            and {item["archive_path"]: item["sha256"] for item in files} == expected,
            "adapter source closure differs")
    for item in files:
        name = item["archive_path"].removeprefix("verification/adapter/")
        require(item["repo_path"] == "services/yandex-station/code/" + name
                and item["mode"] == 0o644 and valid_hash(item["git_blob"], 40),
                "adapter source path or metadata differs")


def verify_adapter_bundle(root, manifest):
    grammar = Path(root) / "verification/adapter/intent_grammar/grammar"
    require(load_json(grammar / "ru.json").get("source_sha256") == file_digest(grammar / "ru.yaml"),
            "compiled adapter grammar provenance differs")
    for item in manifest["adapter_layer"]["files"]:
        data = (Path(root) / item["archive_path"]).read_bytes()
        git_object = b"blob " + str(len(data)).encode() + b"\0" + data
        require(hashlib.sha1(git_object, usedforsecurity=False).hexdigest() == item["git_blob"],
                "adapter Git blob identity differs")


def validate_images(manifest):
    require(image_id(manifest.get("candidate_image")) and image_id(manifest.get("baseline_image")),
            "immutable candidate and base image IDs required")
    base, candidate = manifest["baseline_layers"], manifest["candidate_layers"]
    require(base and all(image_id(x) for x in base + candidate), "invalid image layer identity")
    require(candidate[:-1] == base, "candidate is not the base plus one overlay layer")


def validate_revisions(manifest):
    for name in ("overlay_revision", "runner_revision"):
        revision = manifest[name]
        require(valid_hash(revision.get("commit"), 40) and valid_hash(revision.get("tree"), 40),
                "source commit and tree identities required")
    require(valid_hash(manifest["runner_revision"].get("sha256")), "runner digest required")
    require(manifest["runtime"] == {"version": "v25.9.0", "archive_sha256": NODE_ARCHIVE_SHA,
                                    "binary_sha256": NODE_SHA}, "runtime identity differs")
    require(manifest["prod_grammar_hook"] in ("off", "shadow", "on"), "unrecognized production mode")
    require(manifest["default_flags"] == DEFAULT_FLAGS, "untested nonsecret runtime flags")


def validate_package_roots(inventory):
    packages = inventory["packages"]
    require(len(packages) == len(PACKAGE_NAMES)
            and {x["name"] for x in packages} == PACKAGE_NAMES, "dependency closure differs")
    roots = {}
    for package in packages:
        name, version = package["name"], package["version"]
        require(re.fullmatch(r"[0-9]+[.][0-9]+[.][0-9]+", version), "unexpected package version")
        root = "/node_modules/.pnpm/" + name.replace("/", "+") + "@" + version + "/node_modules/" + name
        require(package["root"] == root, "dependency root differs")
        roots[name] = root
        for entry in package["entries"]:
            require("node_modules" not in PurePosixPath(entry["path"]).parts,
                    "undeclared package-local dependency")
    return roots


def validate_links(inventory, roots):
    edges = (("tsx", "esbuild", "required"), ("tsx", "get-tsconfig", "required"),
             ("protobufjs", "@types/node", "required"), ("protobufjs", "long", "required"),
             ("esbuild", "@esbuild/linux-x64", "optional"),
             ("get-tsconfig", "resolve-pkg-maps", "required"),
             ("@types/node", "undici-types", "required"))
    expected_edges = [{"from": roots[source], "request": target, "kind": kind, "to": roots[target]}
                      for source, target, kind in edges]
    require(sorted(inventory["dependency_edges"], key=canonical) == sorted(expected_edges, key=canonical),
            "dependency edges differ")
    expected = {"/node_modules/" + name: roots[name]
                for name in ("tsx", "tslog", "protobufjs", "zod", "@types/node", "long")}
    for source, target, _kind in edges:
        prefix = roots[source].split("/node_modules/")[:-1]
        expected["/node_modules/".join(prefix) + "/node_modules/" + target] = roots[target]
    links = inventory["links"]
    require(len(links) == len(expected), "dependency link count differs")
    require({link["path"]: link["resolved"] for link in links} == expected, "dependency links differ")
    for link in links:
        require(not link["target"].startswith("/"), "absolute dependency link")
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(link["path"]), link["target"]))
        require(resolved == link["resolved"], "literal dependency link resolves differently")


def validate_inventory(manifest):
    inventory = manifest["inventory"]
    require(inventory.get("schema") == "aisis-candidate-source-metadata/v1", "inventory schema differs")
    require(not inventory.get("forbidden_paths"), "forbidden export path")
    require(inventory["baseline_image_id"] == manifest["baseline_image"], "inventory base differs")
    for field, part in (("maintained_overlay_merge", "commit"), ("maintained_overlay_tree", "tree")):
        require(inventory[field] == manifest["overlay_revision"][part], "inventory overlay revision differs")
    require(len(inventory["source_files"]) == len(SOURCE_PATHS)
            and {x["path"] for x in inventory["source_files"]} == SOURCE_PATHS, "source closure differs")
    provider = next(x for x in inventory["source_files"] if x["path"] == "/src/routers/alice/base-provider.ts")
    require(provider["sha256"] == inventory["provider_postimage_sha256"], "provider postimage differs")
    roots = validate_package_roots(inventory)
    validate_links(inventory, roots)
    expected_exclusions = {roots[name] + "/node_modules/.bin/esbuild" for name in ("tsx", "esbuild")}
    excluded = inventory["excluded_package_paths"]
    require(len(excluded) == 2 and {item["path"] for item in excluded} == expected_exclusions,
            "unexpected package exclusion")
    require(all(valid_hash(item["sha256"]) and type(item["bytes"]) is int
                and item["bytes"] > 0 for item in excluded), "excluded shim identity missing")
    require(len(manifest["fixtures"]) == len(FIXTURES)
            and {x["name"]: x["sha256"] for x in manifest["fixtures"]} == FIXTURES,
            "exact original fixture and maintained runner identities required")


def validate_entry_identities(entries):
    for path, item in entries.items():
        require(not any(str(parent) in entries for parent in PurePosixPath(path).parents),
                "file or link is an inventory parent")
        if item["type"] == "file":
            require(valid_hash(item.get("sha256")) and type(item.get("bytes")) is int
                    and item["bytes"] >= 0, "file identity missing")
            require(type(item.get("mode")) is int and 0 <= item["mode"] <= 0o777,
                    "file mode missing or unsafe")
        else:
            target = item["target"]
            require(not target.startswith("/"), "absolute dependency link")
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
            require(not resolved.startswith("../") and resolved.startswith("node_modules/"),
                    "dependency link escapes closure")


def validate_manifest(manifest):
    require(manifest.get("schema") == SCHEMA, "unsupported manifest schema")
    require(manifest.get("execution_kind") == "runpod-source", "wrong execution kind")
    require(manifest.get("image_executed_remote") is False, "source proof is not image execution")
    validate_images(manifest)
    validate_revisions(manifest)
    validate_inventory(manifest)
    validate_adapter_layer(manifest)
    entries = inventory_entries(manifest)
    validate_entry_identities(entries)
    return entries


def verify_files(root, entries):
    """Read bytes and metadata only, without importing inspected source."""
    root = Path(root).resolve()
    for relative, item in entries.items():
        path = root / relative
        require(path.parent.resolve() == path.parent, "symlinked payload ancestor")
        info = path.lstat()
        if item["type"] == "symlink":
            require(stat.S_ISLNK(info.st_mode) and os.readlink(path) == item["target"], "link differs")
            require(path.resolve().is_relative_to(root), "resolved dependency link escapes")
            continue
        require(stat.S_ISREG(info.st_mode) and info.st_size == item["bytes"], "file type or size differs")
        require(stat.S_IMODE(info.st_mode) == item["mode"], "file mode differs")
        require(file_digest(path) == item["sha256"], "file content differs")


def archive_members(source, entries):
    """Validate the complete archive before creating its first file."""
    allowed_dirs = set()
    for path in entries:
        allowed_dirs.update(str(p) for p in PurePosixPath(path).parents if str(p) != ".")
    seen = set()
    members = source.getmembers()
    for member in members:
        name = member.name.rstrip("/")
        require(not name.startswith("/") and ".." not in PurePosixPath(name).parts
                and str(PurePosixPath(name)) == name, "unsafe archive path")
        require(name not in seen, "duplicate archive path")
        seen.add(name)
        if member.isdir():
            require(name in allowed_dirs, "unexpected archive directory")
        else:
            require(name in entries, "unexpected archive member")
            check_archive_member(member, entries[name])
    require(set(entries).issubset(seen), "archive is missing declared members")
    require(not set(entries).intersection(allowed_dirs), "file or link is an archive parent")
    return members


def check_archive_member(member, expected):
    if member.issym():
        require(expected["type"] == "symlink" and member.linkname == expected["target"],
                "archive dependency link differs")
        return
    require(member.isfile() and expected["type"] == "file", "unsafe archive member type")
    require(member.size == expected["bytes"], "archive member size differs")
    require(member.mode == expected["mode"], "archive member mode differs")


def extract_payload(archive, destination, entries):
    """Extract only declared files, keeping their original hash-relevant modes."""
    require(not any(Path(destination).iterdir()), "payload destination is not empty")
    with tarfile.open(archive, "r:*") as source:
        members = archive_members(source, entries)
        for member in members:
            if not member.isfile():
                continue
            target = Path(destination) / member.name
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with source.extractfile(member) as incoming, target.open("xb") as output:
                shutil.copyfileobj(incoming, output)
                os.fchmod(output.fileno(), member.mode)
        for member in members:
            if member.issym():
                target = Path(destination) / member.name
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                target.symlink_to(member.linkname)
    verify_files(destination, entries)


def verify_resolution_environment(root):
    """Undeclared ancestor modules cannot satisfy missing export dependencies."""
    for parent in Path(root).parents:
        require(not os.path.lexists(parent / "node_modules"), "ambient ancestor dependencies")


def install_node(directory):
    archive = Path(directory) / "node.tar.xz"
    with urllib.request.urlopen(NODE_URL, timeout=45) as response, archive.open("xb") as output:
        while chunk := response.read(1024 * 1024):
            output.write(chunk)
    require(file_digest(archive) == NODE_ARCHIVE_SHA, "Node archive checksum differs")
    node = Path(directory) / "node"
    with tarfile.open(archive, "r:xz") as source:
        member = source.getmember("node-v25.9.0-linux-x64/bin/node")
        require(member.isfile(), "Node archive binary is not a file")
        with source.extractfile(member) as stream, node.open("xb") as output:
            while chunk := stream.read(1024 * 1024):
                output.write(chunk)
    node.chmod(0o700)
    require(file_digest(node) == NODE_SHA, "Node binary checksum differs")
    return node


def python_runtime():
    executable = Path(sys.executable).resolve()
    result = {"implementation": platform.python_implementation(), "version": platform.python_version(),
              "executable": str(executable), "binary_sha256": file_digest(executable)}
    validate_python_runtime(result)
    return result


def validate_python_runtime(value):
    require(isinstance(value, dict) and value.get("implementation") == "CPython",
            "actual CPython runtime identity required")
    version = re.fullmatch(r"3[.](\d+)[.](\d+)", str(value.get("version")))
    require(version is not None and int(version[1]) >= 11, "unsupported test Python runtime")
    require(valid_hash(value.get("binary_sha256"))
            and isinstance(value.get("executable"), str) and value["executable"].startswith("/"),
            "test Python executable identity missing")


def run_modes(root, node, prod_mode, log):
    results = []
    cwd = root / "src/backend/corpus"
    for name, grammar in (("stage", "off"), ("prod", prod_mode)):
        mode = {"name": name, "grammar_hook": grammar}
        env = {"PATH": str(node.parent) + ":/usr/bin:/bin", "HOME": str(root.parent),
               "GRAMMAR_HOOK": grammar, **TEST_RUNTIME_INPUTS, **CLOSED_ENDPOINTS,
               "AISIS_CORPUS_PYTHON": str(Path(sys.executable).resolve()),
               "AISIS_CORPUS_ADAPTER_DIR": str(root / "verification/adapter")}
        for suite in TOTALS:
            command = [str(node), str(root / "node_modules/tsx/dist/cli.mjs"), "run_" + suite + ".mts"]
            log("suite_start", mode=name, suite=suite)
            process = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, timeout=180)
            log("suite_output", mode=name, suite=suite, exit_code=process.returncode,
                output=process.stdout + process.stderr)
            mode[suite] = suite_result(suite, process.returncode, process.stdout + process.stderr)
            log("suite_complete", mode=name, suite=suite, **mode[suite])
        results.append(mode)
    validate_results(results, prod_mode)
    return results


def fixed_command(manifest_sha, bundle_sha, runner_sha):
    return ["python3", "offloaded_corpus.py", "run", "--manifest", "manifest.json",
            "--bundle", "payload.tar.gz", "--manifest-sha256", manifest_sha,
            "--bundle-sha256", bundle_sha, "--runner-sha256", runner_sha]


def run_remote(args, log):
    require(os.environ.get("RUNPOD_RUN") == "1" and os.geteuid() != 0,
            "run requires the native unprivileged RunPod execution context")
    require(file_digest(__file__) == args.runner_sha256, "runner source checksum differs")
    require(file_digest(args.manifest) == args.manifest_sha256, "manifest checksum differs")
    require(file_digest(args.bundle) == args.bundle_sha256, "payload archive checksum differs")
    manifest = load_json(args.manifest)
    entries = validate_manifest(manifest)
    require(manifest["runner_revision"]["sha256"] == args.runner_sha256, "manifest runner differs")
    with tempfile.TemporaryDirectory(prefix="aisis-corpus-input-", dir=Path.cwd()) as work:
        root = Path(work) / "payload"
        root.mkdir(mode=0o700)
        extract_payload(args.bundle, root, entries)
        verify_resolution_environment(root)
        verify_adapter_bundle(root, manifest)
        actual_python = python_runtime()
        node = install_node(work)
        result = {"schema": RESULT_SCHEMA, "execution_kind": "runpod-source",
                  "image_executed_remote": False, "manifest_sha256": args.manifest_sha256,
                  "bundle_sha256": args.bundle_sha256, "runner_sha256": args.runner_sha256,
                  "node_sha256": file_digest(node), "test_runtime_inputs": TEST_RUNTIME_INPUTS,
                  "python_runtime": actual_python,
                  "modes": run_modes(root, node, manifest["prod_grammar_hook"], log)}
        verify_files(root, entries)
        verify_adapter_bundle(root, manifest)
        require(python_runtime() == actual_python, "test Python runtime changed")
    print(MARKER + canonical(result), flush=True)


def private_bytes(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid(), "evidence ownership differs")
        require(not info.st_mode & 0o077, "evidence must be private")
        return stream.read()


def native_state_root():
    # Same passwd-home authority as runpod-run; environment cannot relocate it.
    return Path(pwd.getpwuid(os.getuid()).pw_dir) / ".cache/runpod-run"


def native_result(log_data, manifest_sha, bundle_sha, runner_sha, prod_mode):
    rows = [line[len(MARKER):] for line in log_data.decode().splitlines() if line.startswith(MARKER)]
    require(len(rows) == 1, "missing or duplicate native result marker")
    result = json.loads(rows[0], object_pairs_hook=unique_object)
    required = {"schema": RESULT_SCHEMA, "execution_kind": "runpod-source", "image_executed_remote": False,
                "manifest_sha256": manifest_sha, "bundle_sha256": bundle_sha,
                "runner_sha256": runner_sha, "node_sha256": NODE_SHA,
                "test_runtime_inputs": TEST_RUNTIME_INPUTS}
    require(all(result.get(k) == v and type(result.get(k)) is type(v) for k, v in required.items()),
            "native result identity differs")
    validate_python_runtime(result.get("python_runtime"))
    validate_results(result.get("modes"), prod_mode)
    return result


def matching_ledger(ledger, command, pod_id=None, line_sha=None):
    matches = []
    for line in ledger.splitlines(keepends=True):
        # The shared append-only ledger may end in another writer's partial row.
        # Only the explicitly selected native row is evidence for this receipt.
        if line_sha is not None and digest(line) != line_sha:
            continue
        row = json.loads(line, object_pairs_hook=unique_object)
        if row.get("event") != "run" or shlex.split(row.get("cmd", "")) != command:
            continue
        if pod_id is not None and row.get("pod_id") != pod_id:
            continue
        require(type(row.get("exit")) is int and row["exit"] == 0, "native run exit differs")
        require(type(row.get("command_s")) is int and row["command_s"] >= 0, "native command did not run")
        require(re.fullmatch(r"[A-Za-z0-9_-]+", row.get("pod_id", "")), "native pod identity missing")
        matches.append((row, digest(line)))
    require(len(matches) == 1, "native ledger record missing or ambiguous")
    return matches[0]


def validate_lifecycle(data, row, command, run_path, run_data):
    """Bind cleanup to the selected native invocation's actual complete transcript."""
    text = data.decode()
    waiting = "runpod-run: waiting for pod " + row["pod_id"] + " to answer ssh"
    running = "runpod-run: running as runner: " + row["cmd"]
    require(shlex.split(row["cmd"]) == command, "lifecycle command differs")
    waiting_at = text.find(waiting)
    running_at = text.find(running)
    output_at = text.find(run_data.decode(), running_at + len(running))
    pattern = r"(?m)^runpod-run: exit 0 after [0-9]+s \(log: " + re.escape(str(run_path)) + r"\)$"
    exits = list(re.finditer(pattern, text))
    require(len(exits) == 1, "native lifecycle exit/log identity missing or duplicated")
    ended_at = exits[0].start()
    endings = (" terminated", " is already gone")
    terminations = [text.find("runpod-run: pod " + row["pod_id"] + end, ended_at)
                    for end in endings]
    require(0 <= waiting_at < running_at < output_at < ended_at
            and any(at > ended_at for at in terminations), "native lifecycle order or output differs")
    require(output_at + len(run_data.decode()) <= ended_at, "native output overlaps exit record")
    require("COULD NOT terminate pod " + row["pod_id"] not in text, "native termination failed")


def validate_native(receipt, manifest):
    native = receipt["native"]
    command = fixed_command(receipt["manifest_sha256"], receipt["bundle_sha256"],
                            manifest["runner_revision"]["sha256"])
    root = native_state_root()
    row, _line_sha = matching_ledger(private_bytes(root / "ledger.jsonl"), command,
                                     native["pod_id"], native["ledger_line_sha256"])
    name = native["run_log_name"]
    require(Path(name).name == name and name.endswith("-" + row["repo_tag"] + ".log"),
            "native run log identity differs")
    data = private_bytes(root / "runs" / name)
    require(digest(data) == native["run_log_sha256"], "native run log checksum differs")
    result = native_result(data, receipt["manifest_sha256"], receipt["bundle_sha256"],
                           manifest["runner_revision"]["sha256"], manifest["prod_grammar_hook"])
    lifecycle = private_bytes(native["termination_log"])
    require(digest(lifecycle) == native["termination_log_sha256"], "lifecycle log checksum differs")
    validate_lifecycle(lifecycle, row, command, root / "runs" / name, data)
    return result


def evidence(receipt_path):
    receipt_path = Path(receipt_path)
    receipt = json.loads(private_bytes(receipt_path), object_pairs_hook=unique_object)
    require(receipt.get("schema") == RECEIPT_SCHEMA, "unsupported receipt schema")
    data = private_bytes(receipt_path.parent / "manifest.json")
    require(digest(data) == receipt["manifest_sha256"], "receipt manifest checksum differs")
    manifest = json.loads(data, object_pairs_hook=unique_object)
    validate_manifest(manifest)
    require(file_digest(__file__) == manifest["runner_revision"]["sha256"], "installed verifier differs")
    validate_native(receipt, manifest)
    return manifest


def collect(args, log):
    require(Path(args.manifest).name == "manifest.json", "collector requires manifest.json")
    manifest = load_json(args.manifest)
    validate_manifest(manifest)
    require(file_digest(__file__) == manifest["runner_revision"]["sha256"], "collector source differs")
    manifest_sha, bundle_sha = file_digest(args.manifest), file_digest(args.bundle)
    root = native_state_root()
    name = Path(args.run_log).name
    require(Path(args.run_log).resolve() == (root / "runs" / name).resolve(), "non-native run log path")
    data = private_bytes(root / "runs" / name)
    native_result(data, manifest_sha, bundle_sha, manifest["runner_revision"]["sha256"],
                  manifest["prod_grammar_hook"])
    command = fixed_command(manifest_sha, bundle_sha, manifest["runner_revision"]["sha256"])
    row, line_sha = matching_ledger(private_bytes(root / "ledger.jsonl"), command,
                                     args.pod_id, args.ledger_line_sha256)
    result = {"schema": RECEIPT_SCHEMA, "manifest_sha256": manifest_sha, "bundle_sha256": bundle_sha,
              "native": {"pod_id": row["pod_id"], "ledger_line_sha256": line_sha,
                         "run_log_name": name, "run_log_sha256": digest(data),
                         "termination_log": str(Path(args.termination_log).resolve()),
                         "termination_log_sha256": digest(private_bytes(args.termination_log))}}
    validate_native(result, manifest)
    require(Path(args.output).parent.resolve() == Path(args.manifest).parent.resolve(),
            "receipt must remain beside its original manifest")
    with Path(args.output).open("x") as stream:
        stream.write(canonical(result) + "\n")
    log("receipt_collected", pod_id=row["pod_id"], receipt=str(args.output))


def command_json(command):
    process = subprocess.run(command, capture_output=True, text=True, timeout=30)
    require(process.returncode == 0, "metadata command failed")
    return json.loads(process.stdout)


def image_metadata(image):
    data = command_json(["docker", "image", "inspect", image])[0]
    return {"id": data["Id"], "layers": data["RootFS"]["Layers"]}


def verify_image(manifest, image):
    require(image == manifest["candidate_image"], "candidate image identity differs")
    require(image_metadata(image) == {"id": image, "layers": manifest["candidate_layers"]},
            "candidate image layers differ")
    require(image_metadata(manifest["baseline_image"]) == {
        "id": manifest["baseline_image"], "layers": manifest["baseline_layers"]}, "base image layers differ")


def verify_host_inputs(manifest, corpus_dir):
    runner_dir = Path(__file__).resolve().parent
    for fixture in manifest["fixtures"]:
        name = fixture["name"]
        directory = runner_dir if name in MAINTAINED_RUNNERS else Path(corpus_dir)
        require(file_digest(directory / name) == fixture["sha256"], "current fixture or runner differs")
    repo = runner_dir.parents[2]
    for item in manifest["adapter_layer"]["files"]:
        require(file_digest(repo / item["repo_path"]) == item["sha256"],
                "current maintained adapter source differs")
    current = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "HEAD^{tree}"], text=True, timeout=10).strip()
    require(current == manifest["runner_revision"]["tree"], "current maintained source tree differs")
    dirty = subprocess.check_output(
        ["git", "-C", str(repo), "status", "--porcelain=v1", "--untracked-files=all"],
        text=True, timeout=10)
    require(not dirty, "current maintained working tree differs")



# This hashes bytes in the existing container. It imports no application, performs
# no corpus or API/device call, and prints only a fixed metadata/result object.
CONTAINER_CHECK = r'''
const fs = require('fs'), crypto = require('crypto'), path = require('path');
const spec = JSON.parse(fs.readFileSync(0, 'utf8'));
async function sha(p) {
 const hash = crypto.createHash('sha256');
 for await (const bytes of fs.createReadStream(p)) hash.update(bytes);
 return hash.digest('hex');
}
function directParent(p) {
 if (fs.realpathSync(path.dirname(p)) !== path.dirname(p)) throw Error('symlinked source ancestor');
}
async function inspectEntry(p, want) {
 directParent(p);
 const info = fs.lstatSync(p);
 if (want.type === 'symlink') {
  const expected = path.resolve(path.dirname(p), want.target);
  if (!info.isSymbolicLink() || fs.readlinkSync(p) !== want.target ||
      fs.realpathSync(p) !== expected) throw Error('dependency link differs');
 } else if (!info.isFile() || info.size !== want.bytes ||
            (info.mode & 511) !== want.mode || await sha(p) !== want.sha256) {
  throw Error('source bytes or metadata differ');
 }
}
async function inspectPackage(pkg) {
 if (!fs.lstatSync(pkg.root).isDirectory() || fs.realpathSync(pkg.root) !== pkg.root)
  throw Error('symlinked package root');
 const actual = [], excluded = new Map(spec.excluded.map(item => [item.path, item]));
 async function walk(directory, relative = '') {
  for (const name of fs.readdirSync(directory)) {
   const local = relative ? relative + '/' + name : name;
   const p = directory + '/' + name, info = fs.lstatSync(p);
   if (local.split('/').includes('node_modules')) {
    if (info.isDirectory()) {
     if (![...excluded.keys()].some(item => item.startsWith(p + '/')))
      throw Error('undeclared nested dependency directory');
    } else {
     const allowed = excluded.get(p);
     if (!allowed || !info.isFile() || info.size !== allowed.bytes ||
         await sha(p) !== allowed.sha256) throw Error('undeclared nested dependency');
     continue;
    }
   }
   if (info.isDirectory()) await walk(p, local); else actual.push(local);
  }
 }
 await walk(pkg.root);
 if (JSON.stringify(actual.sort()) !== JSON.stringify(pkg.entries.map(item => item.path).sort()))
  throw Error('dependency closure differs');
}
function inspectSourceResolution(sources) {
 for (const source of sources) {
  for (let ancestor = path.dirname(source); ancestor !== '/'; ancestor = path.dirname(ancestor)) {
   if (fs.existsSync(ancestor + '/node_modules')) throw Error('source dependency shadow');
   if (fs.existsSync(ancestor + '/package.json') || fs.existsSync(ancestor + '/tsconfig.json'))
    throw Error('source configuration shadow');
  }
 }
}
(async () => {
 for (const [relative, want] of Object.entries(spec.entries)) await inspectEntry('/' + relative, want);
 inspectSourceResolution(spec.sources);
 for (const pkg of spec.packages) await inspectPackage(pkg);
 const flags = {};
 for (const key of ['ASR_CORRECT', 'SEMANTIC_INTENTS', 'AISIS_FAST_EARCON']) flags[key] = process.env[key] ?? null;
 console.log(JSON.stringify({ok: true, node_sha256: await sha(process.execPath),
   grammar_hook: process.env.GRAMMAR_HOOK ?? null, default_flags: flags}));
})().catch(() => { console.error('candidate source inspection failed'); process.exit(1); });
'''


def verify_mounts(mounts):
    protected = ("/src", "/node_modules", "/package.json", "/pnpm-lock.yaml", "/pnpm-workspace.yaml", "/tsconfig.json")
    for mount in mounts:
        target = mount["Destination"].rstrip("/") or "/"
        # Every existing color mounts this reviewed non-source asset directory.
        # It is absent from the fixed SOURCE_PATHS closure and private export.
        if target == "/src/fillers":
            continue
        require(target != "/" and not any(p == target or p.startswith(target + "/") or target.startswith(p + "/")
                        for p in protected), "source-altering container mount")


def verify_container(manifest, name):
    match = re.fullmatch(r"aisis-quasar-(stage|prod)-(blue|green)", name)
    require(match is not None, "unexpected deployment container")
    data = command_json(["docker", "inspect", name])[0]
    require(data["State"]["Running"] is True, "candidate container is not running")
    verify_image(manifest, data["Image"])
    verify_mounts(data.get("Mounts", []))
    process = subprocess.run(["docker", "exec", "-i", name, "node", "-e", CONTAINER_CHECK],
                             input=canonical({"entries": inventory_entries(manifest, False),
                                              "packages": manifest["inventory"]["packages"],
                                              "excluded": manifest["inventory"]["excluded_package_paths"],
                                              "sources": [item["path"] for item in manifest["inventory"]["source_files"]
                                                          if item["path"].startswith("/src/")]}),
                             capture_output=True, text=True, timeout=45)
    require(process.returncode == 0, "candidate source inspection failed")
    observed = json.loads(process.stdout)
    require(observed.get("ok") is True and observed.get("node_sha256") == NODE_SHA, "candidate runtime differs")
    mode = "off" if match[1] == "stage" else manifest["prod_grammar_hook"]
    require(observed.get("grammar_hook") == mode, "current container grammar mode was not tested")
    require(observed.get("default_flags") == manifest["default_flags"], "current runtime flags differ")


def verify(args, log):
    manifest = evidence(args.receipt)
    verify_host_inputs(manifest, args.corpus_dir)
    if args.container:
        verify_container(manifest, args.container)
    else:
        verify_image(manifest, args.image)
    log("verified", candidate=manifest["candidate_image"], container=args.container,
        corpus_total=469, dialog_total=195, image_executed_remote=False)


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command")
    run = commands.add_parser("run", help="fixed suites inside an admitted native RunPod job")
    for key in ("manifest", "bundle", "manifest-sha256", "bundle-sha256", "runner-sha256"):
        run.add_argument("--" + key, required=True)
    capture = commands.add_parser("collect", help="bind native ledger, run log and termination evidence")
    for key in ("manifest", "bundle", "run-log", "termination-log", "pod-id", "ledger-line-sha256", "output"):
        capture.add_argument("--" + key, required=True)
    check = commands.add_parser("verify", help="read-only evidence and current candidate checks")
    check.add_argument("--receipt", required=True)
    check.add_argument("--corpus-dir", required=True)
    target = check.add_mutually_exclusive_group(required=True)
    target.add_argument("--image")
    target.add_argument("--container")
    return result


def main():
    os.umask(0o077)
    directory = Path(tempfile.mkdtemp(prefix="aisis-corpus-evidence-"))
    logfile = directory / "run.jsonl"
    started = time.monotonic()

    def log(stage, terminal=True, **fields):
        row = {"at": time.time(), "stage": stage, **fields}
        with logfile.open("a") as stream:
            stream.write(canonical(row) + "\n")
        if terminal:
            print(canonical(row), file=sys.stderr, flush=True)

    log("start", log=str(logfile))
    code = 1
    try:
        args = parser().parse_args()
        actions = {"run": run_remote, "collect": collect, "verify": verify}
        if args.command is None:
            log("inactive", message="Choose a documented command; no operational work performed")
        else:
            actions[args.command](args, log)
        code = 0
    except SuiteFailure as error:
        code = error.exit_code if error.exit_code > 0 else 128 - error.exit_code
        log("failed", error_type=type(error).__name__, actual_process_exit=error.exit_code)
    except subprocess.TimeoutExpired:
        code = 124
        log("failed", error_type="TimeoutExpired")
    except SystemExit as error:
        code = error.code if isinstance(error.code, int) else 1
    except KeyboardInterrupt:
        code = 130
        log("failed", error_type="KeyboardInterrupt")
    except Exception as error:
        # CLI boundary: report failure truthfully without leaking arbitrary input.
        code = 1
        log("failed", error_type=type(error).__name__,
            reason=str(error) if isinstance(error, ValueError) else "operation failed")
    finally:
        log("complete", exit_code=code, elapsed_seconds=round(time.monotonic() - started, 3), log=str(logfile))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
