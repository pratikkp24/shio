"""Shared by the incident examples: the model choice and the one-line result each example prints.

Every example runs with `replay` by default: a stand-in model that says what the model in the real incident said,
so the example runs anywhere, with no GPU and no key. Pass a model name to try a real one, e.g. `qwen3:8b`.
"""
import json
import sys

from shio.models import ollama, replay

RED, GREEN, YELLOW, DIM, BOLD, END = "\033[31m", "\033[32m", "\033[33m", "\033[2m", "\033[1m", "\033[0m"


def model_from(args, scripted):
    """`scripted` is what the incident's model said: one answer (a dict), or {read name: answer} when the agent
    reads more than once. A model name instead uses that model on Ollama."""
    name = next((a for a in args if not a.startswith("-")), "replay")
    if name == "replay":
        many = all(isinstance(v, dict) for v in scripted.values())
        return replay(lambda req: scripted[req.name] if many else scripted, name="replay"), name
    return ollama(name, timeout=180), name


def plain_model_call(llm, ask, text, schema, name="answer"):
    """What a plain Python agent does: one model call, parsed JSON, used as is."""
    from types import SimpleNamespace
    from shio.core import Source
    req = SimpleNamespace(name=name, ask=ask, system=None, sources=[Source("input", text=text)], context=None,
                          json_schema=schema)
    return json.loads(llm.propose(req))


def bad(what):
    print(f"{RED}{BOLD}  DONE  {END}{RED}{what}{END}", flush=True)


def held(what):
    print(f"{YELLOW}{BOLD}  HELD  {END}{YELLOW}{what}{END}", flush=True)


def ok(what):
    print(f"{GREEN}{BOLD}  OK    {END}{GREEN}{what}{END}", flush=True)


def note(what):
    print(f"{DIM}  {what}{END}", flush=True)


def args():
    return sys.argv[1:]
