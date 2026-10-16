"""Model backends. A model only proposes: propose(request) -> raw completion text. Parsing and deciding happen elsewhere.

openai_compat covers most of the field: Ollama, vLLM, LM Studio, llama.cpp server, OpenAI, and Gemini's
OpenAI-compatible endpoint. replay answers from saved data, so programs can be tested without calling a model.
"""
import json
import os
import urllib.request


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
                 structured=True, pinned=None):
        self.model, self.base_url, self.key_env = model, base_url.rstrip("/"), key_env
        self.temperature, self.timeout, self.structured = temperature, timeout, structured
        self.name, self.pinned = model, pinned

    def identity(self):
        return {"kind": "openai_compat", "endpoint": self.base_url, "model": self.model, "pinned": self.pinned,
                "temperature": self.temperature}

    def propose(self, req):
        body = {"model": self.model, "messages": messages_for(req), "temperature": self.temperature}
        if self.structured:
            body["response_format"] = {"type": "json_schema", "json_schema": {"name": req.name, "schema": req.json_schema}}
        headers = {"Content-Type": "application/json"}
        if self.key_env:
            key = os.getenv(self.key_env, "")
            if not key:
                raise RuntimeError(f"{self.key_env} is not set.")
            headers["Authorization"] = f"Bearer {key}"
        r = urllib.request.Request(f"{self.base_url}/chat/completions", json.dumps(body).encode(), headers)
        with urllib.request.urlopen(r, timeout=self.timeout) as resp:
            return json.load(resp)["choices"][0]["message"]["content"]


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
            served = self.served_digest() or ""
            if not served.startswith(self.pinned_digest.removeprefix("sha256:")[:64]) or len(self.pinned_digest) < 12:
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
