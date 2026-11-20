"""The request and reply rules of the adapter's POST /laya proxy (aisis#247).

The proxy sits between the speaker container's semantic shadow and the Laya classify server of task-cli. The server speaks
protocol version 2 (task-cli classify_server `api_version` 2): a `predict` line may carry `model` ("english" or
"multilingual", chosen only by NAME) and `max_len` (an integer 512..2048); such a request gets a `meta` object in the reply
that names the checkpoint really used and whether the input was truncated. A line without those fields is the legacy request
and runs on the English checkpoint, whatever language the text is in. The protocol has no `language` field: the language is a
caller hint that this module turns into a `model`.

Every request is sent with an explicit `model`, chosen by the first rule that applies:
1. an explicit `model` from the caller;
2. a `language` hint: English (`en`, `en-US`, ...) selects the English checkpoint unless the text contains Cyrillic letters, which
   contradicts the hint (mixed or ambiguous input); any other language, and `mul`/`und`, the multilingual one;
3. no hint: the multilingual checkpoint. The script of the text is never a reason for English: Latin letters are also German,
   Serbian, Polish, transliterated Russian, so only an explicit English hint (or model) can select the English checkpoint.
The multilingual checkpoint is the one measured for Russian in docs/research/fast-llm-bench-2026-11.md. A model that cannot
be served is an error, never an answer from another model: the server refuses to answer in another model's place and so does
the proxy, which also rejects a reply whose `meta` does not name the model that was asked for (an older server ignores the
unknown fields and would silently answer in English). The reply, `meta` included, is passed through unchanged.

Every failure is a JSON body `{"ok": false, "error": <code>, ...}` with its own status, so the caller can tell a missing
socket from a slow server from a nonsense answer. The proxy never invents a verdict: when it has no answer it says so and the
caller keeps its own decision.
"""
from __future__ import annotations

import json
import re

MODELS = ("english", "multilingual")
MAX_LEN_MIN = 512
MAX_LEN_MAX = 2048
MESSAGE_MAX = 2000
CONNECT_TIMEOUT_S = 5.0
EXCHANGE_TIMEOUT_S = 30.0  # sending the request and reading the reply line together
READ_LIMIT_BYTES = 1 << 20  # one reply line; asyncio's default 64 KiB would turn a long answer into a stream error

_CYRILLIC = re.compile("[\u0400-\u04ff]")
_LANGUAGE_TAG = re.compile(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8}){0,4}")
_FIELD_WORD = re.compile(r"[A-Za-z0-9_]{1,40}")


class LayaProblem(Exception):
    """A request the proxy refuses or an upstream failure it reports: `status`, a fixed `code` and optional string fields."""

    def __init__(self, status: int, code: str, **fields: str):
        super().__init__(code)
        self.status = status
        self.code = code
        self.fields = fields

    def body(self) -> dict:
        return {"ok": False, "error": self.code, **self.fields}


def _invalid(field: str, detail: str) -> LayaProblem:
    return LayaProblem(400, "invalid_request", field=field, detail=detail)


def build_request(body) -> tuple[dict, str]:
    """`(the JSON line for Laya, the model its reply must confirm)`; raises a 400 `LayaProblem` for an invalid field."""
    if not isinstance(body, dict):
        raise _invalid("body", "a JSON object")
    message = body.get("message")
    if not isinstance(message, str) or not message.strip() or len(message) > MESSAGE_MAX:
        raise _invalid("message", f"a non-empty string of at most {MESSAGE_MAX} characters")
    questions = body.get("questions", {})
    if not isinstance(questions, dict):
        raise _invalid("questions", "an object")
    model = None
    if "model" in body:
        model = body["model"]
        if type(model) is not str or model not in MODELS:
            raise _invalid("model", "one of: " + ", ".join(MODELS))
    max_len = None
    if "max_len" in body:
        max_len = body["max_len"]
        if type(max_len) is not int or not MAX_LEN_MIN <= max_len <= MAX_LEN_MAX:
            raise _invalid("max_len", f"an integer from {MAX_LEN_MIN} to {MAX_LEN_MAX}")
    language = None
    if "language" in body:
        language = body["language"]
        if type(language) is not str or not _LANGUAGE_TAG.fullmatch(language):
            raise _invalid("language", "a language tag such as ru or en-US")
    if model is None:
        english = language is not None and language.split("-")[0].lower() == "en"
        model = "english" if english and not _CYRILLIC.search(message) else "multilingual"
    line = {"op": "predict", "message": message, "questions": questions, "model": model}
    if max_len is not None:
        line["max_len"] = max_len
    return line, model


def _word(value) -> str | None:
    return value if isinstance(value, str) and _FIELD_WORD.fullmatch(value) else None


def check_reply(raw: bytes, model: str) -> str:
    """The reply line as text when it is a success that confirms `model`; otherwise raises the `LayaProblem` that describes it."""
    if not raw.strip():
        raise LayaProblem(502, "laya_empty_response")
    try:
        text = raw.decode("utf-8")  # json.loads on bytes would also accept UTF-16/32, which must not pass through as a 200
        reply = json.loads(text)
    except (ValueError, RecursionError):  # UnicodeDecodeError is a ValueError; deeply nested JSON raises RecursionError
        raise LayaProblem(502, "laya_bad_response") from None
    if not isinstance(reply, dict) or not isinstance(reply.get("ok"), bool):
        raise LayaProblem(502, "laya_bad_response")
    if reply["ok"]:
        meta = reply.get("meta")
        if not isinstance(meta, dict) or meta.get("model") != model:
            raise LayaProblem(502, "laya_model_not_confirmed", model=model)
        return text
    error = _word(reply.get("error"))
    if error == "invalid_request":
        fields = {key: reply[key] for key in ("field", "detail") if isinstance(reply.get(key), str) and len(reply[key]) <= 200}
        raise LayaProblem(400, "invalid_request", **fields)
    if error == "model_unavailable":
        fields = {key: word for key in ("model", "reason") if (word := _word(reply.get(key)))}
        raise LayaProblem(503, "laya_model_unavailable", **fields)
    if error == "unsupported":
        raise LayaProblem(502, "laya_route_unsupported")
    raise LayaProblem(502, "laya_upstream_error", **({"upstream": error} if error else {}))
