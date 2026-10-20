"""Model backends. A model only proposes: propose(request) -> raw completion text. Parsing and deciding happen elsewhere.

openai_compat covers most of the field: Ollama, vLLM, LM Studio, llama.cpp server, OpenAI, and Gemini's
OpenAI-compatible endpoint. replay answers from saved data, so programs can be tested without calling a model.
"""
import dataclasses
import json
import os
from pathlib import Path
import random
import threading
import time
import urllib.error
import urllib.request

RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}


class Pacer:
    """At most `per_minute` calls a minute, shared by every thread using the model, and retries with backoff."""

    def __init__(self, per_minute=None, retries=2):
        self.gap = 60.0 / per_minute if per_minute else 0.0
        self.retries, self._lock, self._next = retries, threading.Lock(), 0.0

    def wait(self):
        if not self.gap:
            return
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next)
            self._next = start + self.gap
        if start > now:
            time.sleep(start - now)

    def run(self, fn, retryable, retry_after=lambda e: None):
        """fn() with retries: a retryable error waits (Retry-After if the server gave one, else 1s, 2s, 4s... with
        jitter, at most 60s) and tries again; the last error is raised, so the reading is null and fails its checks."""
        for attempt in range(self.retries + 1):
            self.wait()
            try:
                return fn()
            except Exception as e:
                if attempt == self.retries or not retryable(e):
                    raise
                delay = retry_after(e)
                time.sleep(min(60.0, delay if delay is not None else 2 ** attempt + random.uniform(0, 0.5)))


def _http_retryable(e):
    if isinstance(e, urllib.error.HTTPError):
        return e.code in RETRY_STATUS
    return isinstance(e, (urllib.error.URLError, TimeoutError, ConnectionError))


def _http_retry_after(e):
    try:
        return float(e.headers.get("Retry-After")) if isinstance(e, urllib.error.HTTPError) else None
    except (TypeError, ValueError):
        return None


MAX_FILE = 20 * 1024 * 1024        # bytes per file sent inline; larger files fail the read with a clear error


def _media(req, kinds):
    """[(source, media_type, base64 data)] for sources a backend will send as files: those with a media_type in `kinds`
    and a readable path."""
    import base64
    out = []
    for s in req.sources:
        if s.media_type in kinds and s.path:
            data = Path(s.path).read_bytes()
            if len(data) > MAX_FILE:
                raise RuntimeError(f"{s.name} is {len(data) // 1024 // 1024} MB; files over {MAX_FILE // 1024 // 1024} MB "
                                   "can't be sent inline")
            out.append((s, s.media_type, base64.standard_b64encode(data).decode()))
    return out


def _unreadable(req, kinds):
    """Sources this backend can neither read as text nor send as files: a read that can't see its input fails."""
    bad = [s.name for s in req.sources if s.text is None and s.media_type and s.media_type not in kinds]
    if bad:
        raise RuntimeError(f"this model can't read {', '.join(bad)} ({'images' if len(bad) > 1 else 'an image'} or "
                           "files without text); use a model that sees them, e.g. anthropic(...) or "
                           "openai_compat(..., vision=True)")


def messages_for(req, sources_text=True):
    """The chat messages a request becomes. Training rebuilds prompts with this same function."""
    parts = [req.ask.strip()]
    if req.context:
        parts.append("Other readings you may use:\n" + json.dumps(req.context, indent=1, default=str))
    if sources_text:
        for s in req.sources:
            if s.text:
                parts.append(f"<source name=\"{s.name}\">\n{s.text}\n</source>")
    parts.append("Answer with one JSON object matching this schema, and nothing else:\n" + json.dumps(req.json_schema))
    msgs = [{"role": "system", "content": req.system}] if req.system else []
    return msgs + [{"role": "user", "content": "\n\n".join(parts)}]


class OpenAICompat:
    """pinned: the exact model version this program was validated with (a provider's dated model id, say). Recorded
    in every decision; required in regulated programs."""

    def __init__(self, model, base_url="http://localhost:11434/v1", key_env=None, temperature=0.0, timeout=600,
                 structured=True, pinned=None, retries=2, per_minute=None, vision=False):
        self.model, self.base_url, self.key_env = model, base_url.rstrip("/"), key_env
        self.temperature, self.timeout, self.structured = temperature, timeout, structured
        self.name, self.pinned = model, pinned
        self.pacer = Pacer(per_minute, retries)
        self.vision = vision          # send image sources as data URLs (OpenAI vision, Ollama llava / qwen2.5-vl...)

    def identity(self):
        return {"kind": "openai_compat", "endpoint": self.base_url, "model": self.model, "pinned": self.pinned,
                "temperature": self.temperature}

    def propose(self, req):
        kinds = {"image/png", "image/jpeg", "image/gif", "image/webp"} if self.vision else set()
        _unreadable(req, kinds)
        msgs = messages_for(req)
        images = _media(req, kinds)
        if images:                    # the user turn becomes text plus one image part per image source
            msgs[-1] = {"role": "user", "content": [{"type": "text", "text": msgs[-1]["content"]}] + [
                {"type": "image_url", "image_url": {"url": f"data:{mt};base64,{data}"}} for _, mt, data in images]}
        body = {"model": self.model, "messages": msgs, "temperature": self.temperature}
        if self.structured:
            body["response_format"] = {"type": "json_schema", "json_schema": {"name": req.name, "schema": req.json_schema}}
        headers = {"Content-Type": "application/json"}
        if self.key_env:
            key = os.getenv(self.key_env, "")
            if not key:
                raise RuntimeError(f"{self.key_env} is not set.")
            headers["Authorization"] = f"Bearer {key}"
        r = urllib.request.Request(f"{self.base_url}/chat/completions", json.dumps(body).encode(), headers)

        def once():
            with urllib.request.urlopen(r, timeout=self.timeout) as resp:
                return json.load(resp)["choices"][0]["message"]["content"]
        return self.pacer.run(once, _http_retryable, _http_retry_after)


class Ollama(OpenAICompat):
    """A model served by Ollama. Its content digest (the sha256 of the weights Ollama reports) is recorded in every
    decision; with digest="..." the model is refused unless the served weights are exactly those."""

    def __init__(self, model, host="http://localhost:11434", digest=None, **kw):
        super().__init__(model, f"{host}/v1", **kw)
        self.host, self.pinned_digest, self._digest = host.rstrip("/"), digest, None

    def served_digest(self):
        if self._digest is None:
            try:
                with urllib.request.urlopen(f"{self.host}/api/tags", timeout=5) as resp:
                    tags = json.load(resp).get("models", [])
                self._digest = next((m.get("digest") for m in tags if m.get("name") == self.model or
                                     m.get("model") == self.model), "") or ""
            except Exception:
                self._digest = ""
        return self._digest or None

    def identity(self):
        return {"kind": "ollama", "host": self.host, "model": self.model, "digest": self.served_digest(),
                "pinned": self.pinned_digest, "temperature": self.temperature}

    def propose(self, req):
        if self.pinned_digest:
            served = (self.served_digest() or "").removeprefix("sha256:")   # some proxies report "sha256:<hex>"
            pinned = self.pinned_digest.removeprefix("sha256:")
            if not served or not served.startswith(pinned[:64]) or len(pinned) < 12:
                raise RuntimeError(f"model {self.model} refused: served digest {served[:16] or 'unknown'} is not "
                                   f"the pinned {self.pinned_digest[:16]}")
        return super().propose(req)


def ollama(model, host="http://localhost:11434", digest=None, **kw):
    return Ollama(model, host, digest, **kw)


def openai_compat(model, base_url, key_env=None, **kw):
    return OpenAICompat(model, base_url, key_env, **kw)


class Replay:
    """Answers from saved data: fn(request) -> dict (or JSON text). For tests, demos and re-running old jobs."""

    def __init__(self, fn, name="replay"):
        self.fn, self.name = fn, name

    def identity(self):
        return {"kind": "replay", "name": self.name}

    def propose(self, req):
        out = self.fn(req)
        return out if isinstance(out, str) else json.dumps(out, default=str)


def replay(fn, name="replay"):
    return Replay(fn, name)


def _strict(schema):
    """A JSON schema as structured outputs need it: every object closed (additionalProperties false) and every
    property listed in required (optional ones are already nullable in Shio's schemas)."""
    if isinstance(schema, list):
        return [_strict(s) for s in schema]
    if not isinstance(schema, dict):
        return schema
    out = {k: _strict(v) for k, v in schema.items()}
    if out.get("type") == "object" or "properties" in out:
        out["additionalProperties"] = False
        out["required"] = list(out.get("properties", {}))
    return out


class Anthropic:
    """Claude through the official SDK (pip install 'shio-lang[anthropic]'). The reading is asked for as structured
    output (output_config.format with the reading's JSON schema), so the reply is JSON matching it. Credentials come
    from the environment as the SDK finds them (ANTHROPIC_API_KEY, or an `ant auth login` profile); they are never
    traced. The SDK retries 408/409/429/5xx itself (`retries`); `per_minute` paces calls across threads.
    structured=False sends the schema in the prompt instead, for a model or gateway without structured outputs."""

    def __init__(self, model="claude-opus-5-5", effort=None, max_tokens=16000, structured=True, retries=2,
                 per_minute=None, timeout=600, client=None):
        self.model, self.effort, self.max_tokens, self.structured = model, effort, max_tokens, structured
        self.name, self.pinned, self.timeout = model, model, timeout
        self.pacer = Pacer(per_minute, retries=0)          # the SDK does the retrying
        self._client, self._retries = client, retries

    def client(self):
        if self._client is None:
            try:
                import anthropic
            except ImportError:
                raise RuntimeError("the Anthropic backend needs the SDK: pip install 'shio-lang[anthropic]'") from None
            self._client = anthropic.Anthropic(max_retries=self._retries, timeout=self.timeout)
        return self._client

    def identity(self):
        return {"kind": "anthropic", "model": self.model, "effort": self.effort, "structured": self.structured}

    FILE_KINDS = {"image/png", "image/jpeg", "image/gif", "image/webp", "application/pdf"}

    def propose(self, req):
        _unreadable(req, self.FILE_KINDS)
        files = _media(req, self.FILE_KINDS)
        sent = {s.name for s, _, _ in files}
        # a file sent as a document or image needn't also go as extracted text
        msgs = messages_for(dataclasses.replace(req, sources=[s for s in req.sources if s.name not in sent]))
        system = next((m["content"] for m in msgs if m["role"] == "system"), None)
        turns = [m for m in msgs if m["role"] != "system"]
        if files:                     # documents and images first, then the prompt text
            blocks = []
            for s, mt, data in files:
                block = {"type": "document", "title": s.name} if mt == "application/pdf" else {"type": "image"}
                block["source"] = {"type": "base64", "media_type": mt, "data": data}
                blocks.append(block)
            turns[-1] = {"role": "user", "content": blocks + [{"type": "text", "text": turns[-1]["content"]}]}
        kw = {"model": self.model, "max_tokens": self.max_tokens, "messages": turns}
        if system:
            kw["system"] = system
        config = {}
        if self.structured:
            config["format"] = {"type": "json_schema", "schema": _strict(req.json_schema)}
        if self.effort:
            config["effort"] = self.effort
        if config:
            kw["output_config"] = config
        response = self.pacer.run(lambda: self.client().messages.create(**kw), lambda e: False)
        if getattr(response, "stop_reason", None) == "refusal":
            raise RuntimeError("the model declined to answer (stop_reason: refusal)")
        return next(b.text for b in response.content if b.type == "text")


def anthropic(model="claude-opus-5-5", **kw):
    return Anthropic(model, **kw)
