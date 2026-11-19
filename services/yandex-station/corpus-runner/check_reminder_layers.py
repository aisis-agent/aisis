"""Check three original reminder cases against the maintained, pure adapter grammar.

The private corpus is unchanged. Its reminder execute expectations belong to
the adapter; the same cases must safely fall through the separate fast layer.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import platform
import stat
import sys
import tempfile
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CORPUS_SHA256 = "249a0fd34e3e4c5d697bd1e34465d62cf356de5e513ba8e13b8f5d6b506c2280"
ORIGINAL_EXPECTATION = {"kind": "cmd", "domain": "timer", "tier": "execute"}
SOURCE_FILES = (
    "__init__.py", "grammar.py", "normalize.py", "pattern.py",
    "numerals.py", "timewords.py", "grammar/ru.json", "grammar/ru.yaml",
)
NAMESPACE = "_aisis_corpus_adapter_grammar"


class ContractError(ValueError):
    """A required layer assertion did not hold."""


@dataclass(frozen=True)
class CaseContract:
    """Independent, phrase-free expectations for one exact private row."""

    row: int
    row_sha256: str
    text_sha256: str
    seconds: int
    note_sha256: str


# Semantics independently read from the original cases, not from Grammar.decide.
# Rows467/468 are the PR153 reminder variants; row469 uses the general reminder rule.
CONTRACTS = (
    CaseContract(
        467,
        "de61b1f93b07fb146c0c5c17667400a26c9016e52efa4fb40bac77fbaaf2e33a",
        "01842a32c696742495a406d0418c4f5222229ac8b8ce68f01ed5b59d259f0b42",
        600,
        "4285b385ce172af3fc2de1a276f2c5b91787db940faa127fe48c89bac6e1ed63",
    ),
    CaseContract(
        468,
        "79fef92be375e76d872a2a69d5f5a9987c0a5e454a5c2068c6439c71930060d3",
        "600d7e42f5413531502a1298bd998f5e1e4a677e117d517ae0fdb0c69d4bfbb8",
        600,
        "4285b385ce172af3fc2de1a276f2c5b91787db940faa127fe48c89bac6e1ed63",
    ),
    CaseContract(
        469,
        "5abc1643903dc186cd0c69abab2b1a0ca0934abb17d85d465d46e9511cb7ccd5",
        "1f8d27ae951b53cef190a668bc52cddc00ac097d284f17166e14f32a5a4b27dd",
        7200,
        "c33e92dd5db371f23eddd7adb1ec9ff1ba5801e3640df1aa129550befdda41c7",
    ),
)


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise ContractError(reason)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def source_inventory(adapter_dir: Path) -> dict[str, dict[str, Any]]:
    """Bind the exact eight-file namespace without loading site packages."""
    records = {}
    for name in SOURCE_FILES:
        path = adapter_dir / "intent_grammar" / name
        info = path.lstat()
        require(stat.S_ISREG(info.st_mode) and path.resolve() == path,
                "adapter source must be a regular file without symlink ancestors")
        raw = path.read_bytes()
        records["intent_grammar/" + name] = {
            "sha256": digest(raw), "bytes": len(raw), "mode": stat.S_IMODE(info.st_mode),
        }
    return records


def load_adapter(adapter_dir: Path) -> tuple[Any, dict[str, dict[str, Any]]]:
    """Load the maintained matcher explicitly, never an installed namesake."""
    adapter_dir = adapter_dir.absolute()
    records = source_inventory(adapter_dir)
    package = adapter_dir / "intent_grammar"
    bundle = package / "grammar/ru.json"
    data = json.loads(bundle.read_text(encoding="utf-8"))
    require(data.get("source_sha256") == records["intent_grammar/grammar/ru.yaml"]["sha256"],
            "compiled grammar does not match its maintained YAML provenance")
    for name in tuple(sys.modules):
        if name == NAMESPACE or name.startswith(NAMESPACE + "."):
            del sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        NAMESPACE, package / "__init__.py", submodule_search_locations=[str(package)]
    )
    require(spec is not None and spec.loader is not None, "adapter namespace is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[NAMESPACE] = module
    spec.loader.exec_module(module)
    for name in SOURCE_FILES[:6]:
        key = NAMESPACE if name == "__init__.py" else NAMESPACE + "." + name[:-3]
        loaded = sys.modules.get(key)
        require(loaded is not None and Path(loaded.__file__).resolve() == package / name,
                "adapter import escaped the declared namespace")
    return module.load(bundle), records


def original_row(raw: bytes, contract: CaseContract) -> dict[str, Any]:
    """Require original bytes and positive execute assertions before matching."""
    prefix = f"row {contract.row}: "
    require(digest(raw) == contract.row_sha256, prefix + "row hash differs")
    try:
        row = json.loads(raw)
    except (ValueError, UnicodeError):
        raise ContractError(prefix + "invalid JSON") from None
    require(isinstance(row, dict) and isinstance(row.get("text"), str),
            prefix + "invalid row shape")
    require(row.get("expect") == ORIGINAL_EXPECTATION, prefix + "original expectation differs")
    require(digest(row["text"].encode()) == contract.text_sha256, prefix + "text hash differs")
    return row


def check_case(grammar: Any, raw: bytes, contract: CaseContract) -> dict[str, Any]:
    """Check the real pure grammar; never call a timer or a device executor."""
    row = original_row(raw, contract)
    prefix = f"row {contract.row}: "
    decision = grammar.decide(row["text"], phase="pre")
    require(decision is not None and decision.intent == "reminder.in"
            and decision.tier == "execute", prefix + "adapter reminder decision differs")
    require(decision.fuzzy is False and decision.confidence == 1.0 and not decision.parts,
            prefix + "adapter decision is fuzzy or compound")
    require(set(decision.slots) == {"duration", "what"}, prefix + "adapter slots differ")
    duration, note = decision.slots["duration"], decision.slots["what"]
    require(type(duration) is int and duration == contract.seconds, prefix + "duration differs")
    require(isinstance(note, str), prefix + "note is not text")
    normalized = " ".join(unicodedata.normalize("NFC", note).casefold().split())
    require(digest(normalized.encode()) == contract.note_sha256, prefix + "note differs")
    require(decision.action == {
        "executor": "timer.start", "args": {"seconds": contract.seconds, "note": note},
    }, prefix + "action is not the exact deferred timer note")
    return {
        "row": contract.row, "row_sha256": contract.row_sha256,
        "text_sha256": contract.text_sha256, "original_expectation": row["expect"],
        "adapter_intent": decision.intent, "adapter_tier": decision.tier,
        "seconds": duration, "note_sha256": digest(normalized.encode()),
        "action_executor": "timer.start", "action_keys": ["note", "seconds"],
        "fuzzy": False, "parts": 0, "no_immediate_device_action": True,
        "fast_expectation": {"kind": "llm", "tier": "llm"},
    }


def check_corpus(corpus: Path, adapter_dir: Path) -> dict[str, Any]:
    raw = corpus.read_bytes()
    require(digest(raw) == CORPUS_SHA256, "original corpus hash differs")
    rows = raw.splitlines()
    require(len(rows) == 469, "original corpus must contain all 469 rows")
    grammar, sources = load_adapter(adapter_dir)
    cases = [check_case(grammar, rows[contract.row - 1], contract) for contract in CONTRACTS]
    return {
        "schema": "aisis-reminder-layer-contract/v1", "corpus_sha256": digest(raw),
        "original_rows": len(rows), "adapter_passed": len(cases), "cases": cases,
        "adapter_sources": sources,
        "python": {
            "version": sys.version, "implementation": platform.python_implementation(),
            "executable_sha256": digest(Path(sys.executable).resolve().read_bytes()),
        },
    }


def main() -> int:
    os.umask(0o077)
    directory = Path(tempfile.mkdtemp(prefix="aisis-reminder-layers-", dir="/tmp"))
    log = directory / "execution.jsonl"
    print(f"private_log={log}", file=sys.stderr, flush=True)

    def record(stage: str, **fields: Any) -> None:
        with log.open("a", encoding="utf-8") as stream:
            json.dump({"at": datetime.now(timezone.utc).isoformat(), "stage": stage, **fields}, stream)
            stream.write("\n")

    record("start")
    try:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--corpus", type=Path, required=True)
        parser.add_argument("--adapter-dir", type=Path, required=True)
        args = parser.parse_args()
        result = check_corpus(args.corpus, args.adapter_dir)
        record("adapter_verified", corpus_sha256=result["corpus_sha256"],
               adapter_passed=result["adapter_passed"], cases=result["cases"])
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0
    except ContractError as error:
        record("contract_failed", reason=str(error))
        print(str(error), file=sys.stderr)
        return 1
    except Exception as error:
        record("failed", error_class=type(error).__name__)
        print(f"adapter checker failed: {type(error).__name__}", file=sys.stderr)
        return 1
    finally:
        record("complete")
        print(f"private_log={log}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
