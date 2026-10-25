"""Model client: forced tool calls with schema validation, repair, retries, a disk cache and a usage ledger.

Two providers behind one interface, standard library only (urllib, nothing to install):
  * anthropic - Messages API, `tool_choice` forces the tool
  * gemini    - generateContent, `functionCallingConfig` mode ANY with one allowed function forces the call
The provider is `AUDIT_PROVIDER`, else whichever key is set (ANTHROPIC_API_KEY, then GEMINI_API_KEY / GOOGLE_API_KEY).

Every call
  * is a *forced tool call*: the model can only answer by filling the tool's schema, so free text never reaches the
    pipeline, and the schema's enums bound what an injected instruction could make it say;
  * is validated here against the same schema (types, enums, required fields, ranges, lengths, patterns), whatever
    subset the provider accepts on the wire; an invalid answer is sent back as a tool error and retried up to `repairs`
    times;
  * is retried on rate limits, 5xx and network errors with exponential back-off (Retry-After honoured), and raises
    `LLMError` when attempts run out, so callers always have a deterministic fallback path;
  * is cached on disk by a hash of (provider, model, system, content, schema), and every live call or cache hit is
    appended to the usage ledger that evaluation/usage_report.md is generated from.

Keys come from the environment (or an uncommitted .env at the repository root), never from code.
"""

from collections.abc import Callable
from typing import Any
import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import paths
from .shapes import JsonObject, JsonValue

API_VERSION = "2023-06-01"
RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}
DEFAULT_MODELS = {
    "anthropic": {"fast": "claude-haiku-5-5", "reasoning": "claude-sonnet-5-5"},
    "gemini": {"fast": "gemini-3.1-flash-lite", "reasoning": "gemini-3.1-flash-lite"},
}
BASE_URLS = {"anthropic": "https://api.anthropic.com", "gemini": "https://generativelanguage.googleapis.com"}
# schema keywords the Gemini function-declaration format rejects; they are still enforced locally by validate()
GEMINI_UNSUPPORTED = {"additionalProperties", "pattern", "maxLength", "minLength"}


_LEDGER_LOCK = threading.Lock()


class LLMError(Exception):
    """The model could not give a valid answer; callers fall back to the deterministic path."""


# ------------------------------------------------------------------ schema validation
def validate(schema: dict, value: JsonValue, path: str = "$") -> list[str]:
    """Return a list of problems (empty when valid). Supports the JSON-Schema subset used here."""
    errs = []
    t = schema.get("type")
    kinds = {"object": dict, "array": list, "string": str, "boolean": bool}
    if t in kinds and (not isinstance(value, kinds[t]) or (t != "boolean" and isinstance(value, bool))):
        return [f"{path}: expected {t}"]
    if t == "number" and (isinstance(value, bool) or not isinstance(value, (int, float))):
        return [f"{path}: expected number"]
    if t == "integer" and (isinstance(value, bool) or not isinstance(value, int)):
        return [f"{path}: expected integer"]
    if "enum" in schema and value not in schema["enum"]:
        errs.append(f"{path}: {value!r} not in {schema['enum']}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errs.append(f"{path}: below minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errs.append(f"{path}: above maximum {schema['maximum']}")
    if isinstance(value, str):
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errs.append(f"{path}: longer than {schema['maxLength']}")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errs.append(f"{path}: does not match {schema['pattern']}")
    if isinstance(value, list):
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errs.append(f"{path}: more than {schema['maxItems']} items")
        if "items" in schema:
            for i, v in enumerate(value):
                errs += validate(schema["items"], v, f"{path}[{i}]")
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for req in schema.get("required", []):
            if req not in value:
                errs.append(f"{path}: missing {req}")
        if schema.get("additionalProperties") is False:
            errs += [f"{path}: unexpected field {k}" for k in value if k not in props]
        for k, v in value.items():
            if k in props:
                errs += validate(props[k], v, f"{path}.{k}")
    return errs


def _gemini_schema(schema: Any) -> Any:
    """The schema as Gemini's function declaration accepts it (upper-case types, no unsupported keywords)."""
    if isinstance(schema, dict):
        out = {k: _gemini_schema(v) for k, v in schema.items() if k not in GEMINI_UNSUPPORTED}
        if isinstance(out.get("type"), str):
            out["type"] = out["type"].upper()
        return out
    if isinstance(schema, list):
        return [_gemini_schema(v) for v in schema]
    return schema


# ------------------------------------------------------------------ transport
def _http_post(url: str, headers: dict[str, str], body: dict[str, Any], timeout: float) -> tuple[int, dict[str, str], dict[str, Any]]:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode())
        except Exception:  # noqa: BLE001
            payload = {}
        return e.code, dict(e.headers or {}), payload


def _load_dotenv() -> None:
    env = paths.REPO_ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            m = re.match(r"\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.*?)\s*$", line)
            if m and m.group(1) not in os.environ:
                os.environ[m.group(1)] = m.group(2).strip("'\"")


def _detect_provider() -> str:
    forced = os.environ.get("AUDIT_PROVIDER", "").lower()
    if forced in BASE_URLS:
        return forced
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
        return "gemini"
    return "anthropic"


class LLMClient:
    def __init__(self, provider: str | None = None, api_key: str | None = None, base_url: str | None = None, cache_dir: Path | None = None, ledger: Path | None = None, transport: Callable | None = None, sleep: Callable[[float], None] = time.sleep, max_retries: int = 5, timeout: float = 120) -> None:
        _load_dotenv()
        self.provider = provider or _detect_provider()
        if api_key is not None:
            self.api_key = api_key
        elif self.provider == "gemini":
            self.api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY", "")
        else:
            self.api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        env_base = os.environ.get("GEMINI_BASE_URL" if self.provider == "gemini" else "ANTHROPIC_BASE_URL")
        self.base = (base_url or env_base or BASE_URLS[self.provider]).rstrip("/")
        self.cache_dir = Path(cache_dir) if cache_dir else paths.CACHE_DIR / "llm"
        self.ledger = Path(ledger) if ledger else paths.CACHE_DIR / "usage_ledger.jsonl"
        self.transport, self.sleep = transport or _http_post, sleep
        self.max_retries, self.timeout = max_retries, timeout

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def model(self, role: str) -> str:
        """Model for a role ('fast' or 'reasoning'): AUDIT_FAST_MODEL / AUDIT_MODEL, else the provider default."""
        override = os.environ.get("AUDIT_FAST_MODEL" if role == "fast" else "AUDIT_MODEL")
        return override or DEFAULT_MODELS[self.provider][role]

    # -------------------------------------------------------------- ledger / cache
    def _log(self, entry: dict[str, Any]) -> None:
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        with _LEDGER_LOCK, open(self.ledger, "a", encoding="utf-8") as fh:  # safe when calls run in threads
            fh.write(json.dumps({"provider": self.provider, **entry}, ensure_ascii=False) + "\n")

    def _key(self, model: str, system: str, content: list[dict[str, Any]], schema: dict[str, Any], tool_name: str) -> str:
        raw = json.dumps([self.provider, model, system, content, schema, tool_name], sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode()).hexdigest()

    # -------------------------------------------------------------- provider formats
    def _start(self, model: str, system: str, content: list[dict[str, Any]], tool_name: str, tool_description: str, schema: dict[str, Any], max_tokens: int) -> dict[str, Any]:
        """The request state: a body plus whatever the URL needs."""
        body: dict[str, Any]
        if self.provider == "gemini":
            parts = []
            for b in content:
                if b["type"] == "text":
                    parts.append({"text": b["text"]})
                else:
                    parts.append({"inlineData": {"mimeType": b["source"]["media_type"], "data": b["source"]["data"]}})
            body = {"systemInstruction": {"parts": [{"text": system}]},
                    "contents": [{"role": "user", "parts": parts}],
                    "tools": [{"functionDeclarations": [{"name": tool_name, "description": tool_description,
                                                         "parameters": _gemini_schema(schema)}]}],
                    "toolConfig": {"functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": [tool_name]}},
                    "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0}}
        else:
            body = {"model": model, "max_tokens": max_tokens, "system": system,
                    "messages": [{"role": "user", "content": content}],
                    "tools": [{"name": tool_name, "description": tool_description, "input_schema": schema}],
                    "tool_choice": {"type": "tool", "name": tool_name}}
        return {"model": model, "body": body}

    def _request(self, state: dict[str, Any]) -> tuple[str, dict[str, str]]:
        if self.provider == "gemini":
            url = f"{self.base}/v1beta/models/{state['model']}:generateContent"
            headers = {"x-goog-api-key": self.api_key, "content-type": "application/json"}
        else:
            url = f"{self.base}/v1/messages"
            headers = {"x-api-key": self.api_key, "anthropic-version": API_VERSION, "content-type": "application/json"}
        return url, headers

    def _parse(self, data: dict[str, Any], tool_name: str) -> tuple[JsonObject | None, str | None, int, int, Any]:
        """-> (input dict or None, call id, input tokens, output tokens, assistant message to echo on repair)"""
        if self.provider == "gemini":
            u = data.get("usageMetadata", {})
            cand = (data.get("candidates") or [{}])[0].get("content", {})
            call = next((p["functionCall"] for p in cand.get("parts", []) if p.get("functionCall", {}).get("name") == tool_name), None)
            return (call.get("args", {}) if call else None, tool_name, u.get("promptTokenCount", 0),
                    u.get("candidatesTokenCount", 0), cand if cand else None)
        u = data.get("usage", {})
        block = next((b for b in data.get("content", []) if b.get("type") == "tool_use" and b.get("name") == tool_name), None)
        return (block.get("input") if block else None, block.get("id") if block else None, u.get("input_tokens", 0),
                u.get("output_tokens", 0), data.get("content"))

    def _repair(self, state: dict[str, Any], assistant: Any, call_id: Any, tool_name: str, problem: str) -> None:
        msg = f"Invalid input: {problem}. Call the tool again with a valid input."
        if self.provider == "gemini":
            state["body"]["contents"].append({"role": "model", "parts": assistant["parts"]})
            state["body"]["contents"].append({"role": "user", "parts": [{"functionResponse": {
                "name": tool_name, "response": {"error": msg}}}]})
        else:
            state["body"]["messages"].append({"role": "assistant", "content": assistant})
            state["body"]["messages"].append({"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": call_id, "is_error": True, "content": msg}]})

    # -------------------------------------------------------------- the call
    def call_tool(self, *, purpose: str, model: str, system: str, content: str | list[dict[str, Any]], tool_name: str, tool_description: str, schema: dict[str, Any], max_tokens: int = 1024, repairs: int = 2, use_cache: bool = True,
                  check: Callable[[JsonObject], list[str]] | None = None) -> JsonObject:
        """Force `tool_name`, validate its input against `schema` (and `check`, a semantic guardrail that returns problems),
        and return it as a dict. Problems of either kind are sent back to the model as the tool's error, up to `repairs` times."""
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        key = self._key(model, system, content, schema, tool_name)
        cache_file = self.cache_dir / f"{key}.json"
        if use_cache and cache_file.exists():
            cached: JsonObject = json.loads(cache_file.read_text(encoding="utf-8"))
            self._log({"purpose": purpose, "model": model, "cached": True, "input_tokens": 0, "output_tokens": 0, "attempts": 0})
            return cached
        if not self.available:
            raise LLMError("no API key (set ANTHROPIC_API_KEY or GEMINI_API_KEY)")

        state = self._start(model, system, content, tool_name, tool_description, schema, max_tokens)
        in_tok = out_tok = attempts = 0
        started = time.time()
        problem = "no tool call"
        for _ in range(repairs + 1):
            data, used = self._post_with_retries(state)
            attempts += used
            result, call_id, i, o, assistant = self._parse(data, tool_name)
            in_tok += i
            out_tok += o
            if result is None:  # text instead of the tool call: nothing to answer, just ask again
                problem = "the answer contained no call to the required tool"
                continue
            errs = validate(schema, result)
            if not errs and check is not None:
                errs = check(result)
            if not errs:
                if use_cache:
                    self.cache_dir.mkdir(parents=True, exist_ok=True)
                    cache_file.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
                self._log({"purpose": purpose, "model": model, "cached": False, "input_tokens": in_tok,
                           "output_tokens": out_tok, "attempts": attempts, "seconds": round(time.time() - started, 2)})
                return result
            problem = "; ".join(errs[:5])
            self._repair(state, assistant, call_id, tool_name, problem)  # send the error back as the tool's result
        self._log({"purpose": purpose, "model": model, "cached": False, "failed": True, "input_tokens": in_tok,
                   "output_tokens": out_tok, "attempts": attempts})
        raise LLMError(f"{purpose}: no valid answer after {repairs + 1} tries ({problem})")

    def session(self, **kw: Any) -> "AgentSession":
        """A multi-turn tool-calling conversation (see AgentSession)."""
        return AgentSession(self, **kw)

    def _post_with_retries(self, state: dict[str, Any]) -> tuple[dict[str, Any], int]:
        url, headers = self._request(state)
        delay = 2.0
        for attempt in range(1, self.max_retries + 1):
            try:
                status, hdrs, data = self.transport(url, headers, state["body"], self.timeout)
            except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
                status, hdrs, data = 0, {}, {"error": {"message": str(exc)}}
            if status == 200:
                return data, attempt
            if status != 0 and status not in RETRY_STATUS:
                msg = (data.get("error") or {}).get("message", "")
                raise LLMError(f"API error {status}: {msg}")
            if attempt == self.max_retries:
                raise LLMError(f"API unavailable after {attempt} attempts (last status {status})")
            wait = delay
            ra = {k.lower(): v for k, v in hdrs.items()}.get("retry-after")
            if ra and str(ra).replace(".", "", 1).isdigit():
                wait = max(wait, float(ra))
            self.sleep(min(wait, 60))
            delay *= 2
        raise LLMError("unreachable")


class AgentSession:
    """A multi-turn conversation in which the model must call one of several tools every turn.

    The caller runs each tool and feeds the result back; the session only keeps the provider's message format right
    (assistant turn echoed verbatim, results returned as tool results / function responses), counts tokens and HTTP
    attempts, and can force one particular tool for the next turn so a loop is guaranteed to end. Calls made in the
    same turn after the first are answered with an error, so the model learns to call one tool at a time.
    """

    def __init__(self, client: LLMClient, *, model: str, system: str, tools: list[dict[str, Any]], max_tokens: int = 800) -> None:
        """tools: [{"name", "description", "schema"}]"""
        self.c, self.model, self.tools = client, model, {t["name"]: t for t in tools}
        self.in_tok = self.out_tok = self.attempts = self.turns = 0
        self.pending: list[tuple[str, dict[str, Any]]] = []   # (name, args) of the last turn
        self.state: dict[str, Any]
        self._ids: list[str] = []
        if client.provider == "gemini":
            decls = []
            for t in tools:
                d = {"name": t["name"], "description": t["description"]}
                if t["schema"].get("properties"):   # Gemini rejects an object with no properties
                    d["parameters"] = _gemini_schema(t["schema"])
                decls.append(d)
            self.state = {"model": model, "body": {
                "systemInstruction": {"parts": [{"text": system}]}, "contents": [],
                "tools": [{"functionDeclarations": decls}],
                "toolConfig": {"functionCallingConfig": {"mode": "ANY"}},
                "generationConfig": {"maxOutputTokens": max_tokens, "temperature": 0}}}
        else:
            self.state = {"model": model, "body": {
                "model": model, "max_tokens": max_tokens, "system": system, "messages": [],
                "tools": [{"name": t["name"], "description": t["description"], "input_schema": t["schema"]} for t in tools],
                "tool_choice": {"type": "any", "disable_parallel_tool_use": True}}}

    def begin(self, text: str) -> None:
        if self.c.provider == "gemini":
            self.state["body"]["contents"].append({"role": "user", "parts": [{"text": text}]})
        else:
            self.state["body"]["messages"].append({"role": "user", "content": [{"type": "text", "text": text}]})

    def force(self, name: str) -> None:
        """Make `name` the only tool the model may call on the next turn."""
        if self.c.provider == "gemini":
            self.state["body"]["toolConfig"]["functionCallingConfig"] = {"mode": "ANY", "allowedFunctionNames": [name]}
        else:
            self.state["body"]["tool_choice"] = {"type": "tool", "name": name}

    def next_call(self) -> tuple[str, dict[str, Any]]:
        """Ask the model for its next tool call: returns (name, args). Raises LLMError if it makes none."""
        data, used = self.c._post_with_retries(self.state)
        self.attempts += used
        self.turns += 1
        if self.c.provider == "gemini":
            u = data.get("usageMetadata", {})
            self.in_tok += u.get("promptTokenCount", 0)
            self.out_tok += u.get("candidatesTokenCount", 0)
            content = (data.get("candidates") or [{}])[0].get("content", {})
            calls = [(p["functionCall"]["name"], p["functionCall"].get("args", {})) for p in content.get("parts", [])
                     if "functionCall" in p]
            if calls:
                self.state["body"]["contents"].append({"role": "model", "parts": content["parts"]})
        else:
            u = data.get("usage", {})
            self.in_tok += u.get("input_tokens", 0)
            self.out_tok += u.get("output_tokens", 0)
            blocks = [b for b in data.get("content", []) if b.get("type") == "tool_use"]
            calls = [(b["name"], b.get("input", {})) for b in blocks]
            self._ids = [b["id"] for b in blocks]
            if calls:
                self.state["body"]["messages"].append({"role": "assistant", "content": data["content"]})
        if not calls:
            raise LLMError("the model made no tool call")
        self.pending = calls
        return calls[0]

    def reply(self, result: Any, is_error: bool = False) -> None:
        """Answer the pending calls: the first with `result`, any others with an error."""
        extra = "Only the first call of a turn is run; call one tool at a time."
        if self.c.provider == "gemini":
            parts = []
            for i, (name, _) in enumerate(self.pending):
                body = {"error": str(result)} if (is_error and i == 0) else ({"result": result} if i == 0 else {"error": extra})
                parts.append({"functionResponse": {"name": name, "response": body}})
            self.state["body"]["contents"].append({"role": "user", "parts": parts})
        else:
            blocks = []
            for i, tid in enumerate(self._ids):
                text = json.dumps(result, ensure_ascii=False, default=str) if (i == 0 and not is_error) else (str(result) if i == 0 else extra)
                blocks.append({"type": "tool_result", "tool_use_id": tid, "content": text, "is_error": bool(is_error or i)})
            self.state["body"]["messages"].append({"role": "user", "content": blocks})
