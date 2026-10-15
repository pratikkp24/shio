"""Readings, checks, rules and agents: the boundary between what a model proposes and what code decides.

A model call returns a Reading. A Reading's value can only be looked at inside a @check or a @rule, so the
model can never settle an outcome on its own: an @agent that returns a Reading is an error.
"""
import contextvars
import dataclasses
import functools
import json
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from .schema import parse, schema_ref, to_json_schema, to_jsonable

_run = contextvars.ContextVar("shio_run", default=None)
_open = contextvars.ContextVar("shio_open", default=False)   # True inside a @check or @rule

CHECKS = {}   # "module:qualname" -> the plain check function, so training can re-run a check on a new completion


class BoundaryError(TypeError):
    """A model proposal was used where only code may decide."""


def _blocked(what):
    def method(self, *a, **k):
        raise BoundaryError(f"{self!r} is a model proposal: {what} it only inside a @check or @rule.")
    return method


class Reading:
    """What a model proposed. Opaque outside a @check or @rule."""
    __slots__ = ("id", "name", "model", "request", "raw", "_value", "error", "schema", "run_id")

    def __init__(self, name, model, request, raw, value, error, schema, run_id):
        self.id, self.name, self.model, self.request, self.raw = uuid.uuid4().hex[:12], name, model, request, raw
        self._value, self.error, self.schema, self.run_id = value, error, schema, run_id

    @property
    def value(self):
        if not _open.get():
            raise BoundaryError(f"{self!r} is a model proposal: read .value only inside a @check or @rule.")
        return self._value

    def __repr__(self):
        return f"<Reading {self.name} by {self.model}>"

    __bool__ = _blocked("branch on")
    __eq__ = __ne__ = __lt__ = __le__ = __gt__ = __ge__ = _blocked("compare")
    __float__ = __int__ = __index__ = _blocked("convert")
    __iter__ = __len__ = __getitem__ = _blocked("unpack")
    __str__ = __format__ = _blocked("format")
    __hash__ = object.__hash__

    def __getattr__(self, attr):
        if attr.startswith("__"):
            raise AttributeError(attr)
        raise BoundaryError(f"{self!r} is a model proposal: read .{attr} only inside a @check or @rule "
                            f"(as reading.value.{attr}).")


@dataclasses.dataclass
class Source:
    """A document the model reads. `text` is sent to text-only models; `path` lets a model upload the file itself."""
    name: str
    text: str = None
    path: str = None

    @classmethod
    def from_pdf(cls, path, name=None):
        from pypdf import PdfReader
        pages = [f"[page {i}]\n{p.extract_text() or ''}" for i, p in enumerate(PdfReader(path).pages, 1)]
        return cls(name or str(path).rsplit("/", 1)[-1], "\n\n".join(pages), str(path))

    @property
    def id(self):
        return self.name


@dataclasses.dataclass
class ReadRequest:
    name: str
    ask: str
    system: str
    sources: list
    context: dict
    json_schema: dict
    blind: bool
    meta: dict


class Run:
    def __init__(self, agent, parent=None):
        self.id, self.agent, self.parent = uuid.uuid4().hex[:12], agent, parent
        self.started, self.ended = time.time(), None
        self.inputs, self.readings, self.checks, self.rules, self.children = {}, [], [], [], []
        self.sources, self.outcome, self.error = {}, None, None

    def to_dict(self):
        return {"id": self.id, "agent": self.agent, "started": self.started, "ended": self.ended, "inputs": self.inputs,
                "sources": self.sources,
                "readings": [{"id": r.id, "name": r.name, "model": r.model, "schema": schema_ref(r.schema),
                              "request": {**dataclasses.asdict(r.request), "sources": [s.id for s in r.request.sources]},
                              "raw": r.raw, "value": to_jsonable(r._value), "error": r.error} for r in self.readings],
                "checks": self.checks, "rules": self.rules, "outcome": to_jsonable(self.outcome), "error": self.error,
                "children": [c.to_dict() for c in self.children]}


def _current(what):
    run = _run.get()
    if run is None:
        raise BoundaryError(f"{what} must be called inside an @agent.")
    return run


def _uncover(x):
    """Replace readings by their values (used when code, not a model, is about to look)."""
    if isinstance(x, Reading):
        return x._value
    if not _has_reading(x):      # plain data passes through as the same object, so a rule can update it in place
        return x
    if isinstance(x, (list, tuple)):
        return type(x)(_uncover(v) for v in x)
    if isinstance(x, dict):
        return {k: _uncover(v) for k, v in x.items()}
    return x


def _has_reading(x):
    if isinstance(x, Reading):
        return True
    if isinstance(x, (list, tuple, set)):
        return any(_has_reading(v) for v in x)
    if isinstance(x, dict):
        return any(_has_reading(v) for v in x.values())
    if dataclasses.is_dataclass(x) and not isinstance(x, type):
        return any(_has_reading(getattr(x, f.name)) for f in dataclasses.fields(x))
    return False


def read(schema, ask, *, by, sources=(), context=None, system=None, name=None, blind=False, key=None, meta=None):
    """Ask a model for a value of `schema`. Returns a Reading: a proposal, not an answer.

    context: earlier readings (or plain values) the model may see. A blind read sees none, so it is an independent
    second opinion. meta: anything a model or a replay needs to route the request (never shown to the model).
    """
    run = _current("read()")
    if blind and context:
        raise BoundaryError("A blind read cannot see other readings; drop context= or blind=True.")
    sources = [s if isinstance(s, Source) else Source(str(s), path=str(s)) for s in _flat(sources)]
    meta = {**(meta or {}), **({"key": key} if key is not None else {})}
    for s in sources:
        run.sources.setdefault(s.id, {"name": s.name, "path": s.path, "text": s.text})
    req = ReadRequest(name or getattr(schema, "__name__", "reading"), ask, system or "", sources,
                      to_jsonable(_uncover(context or {})), to_json_schema(schema), blind, meta)
    raw = by.propose(req)
    value, error = parse(schema, raw)
    r = Reading(req.name, getattr(by, "name", type(by).__name__), req, raw, value, error, schema, run.id)
    run.readings.append(r)
    return r


def _flat(x):
    if x is None:
        return []
    if isinstance(x, (list, tuple)):
        return [s for v in x for s in _flat(v)]
    return [x]


def _arg_spec(a):
    if isinstance(a, Reading):
        return {"reading": a.id}
    try:
        frozen = json.loads(json.dumps(a))     # a snapshot: later changes to the object do not rewrite history
    except (TypeError, ValueError):
        frozen = None
    if frozen == a and type(frozen) is type(a):
        return {"const": frozen}
    return {"opaque": repr(a)[:200]}           # would not come back the same from JSON, so it cannot be replayed


def check(fn=None, *, name=None, reward=True):
    """A deterministic test over readings. Returns True/False to the agent, and is recorded in the trace, where it
    doubles as a training reward (reward=False: a `when`, which only steers control flow). The function may return a
    bool or (bool, detail)."""
    def deco(fn):
        ref = f"{fn.__module__}:{fn.__qualname__}"
        cname = name or fn.__name__
        CHECKS[ref] = fn

        @functools.wraps(fn)
        def wrapper(*args, key=None, **kwargs):
            run = _current(f"check {cname}")
            tok = _open.set(True)
            try:
                out = fn(*[_uncover(a) for a in args], **{k: _uncover(v) for k, v in kwargs.items()})
            except Exception as e:   # a check that cannot be evaluated (a null reading, say) has failed
                out = (False, f"could not be evaluated: {type(e).__name__}: {e}")
            finally:
                _open.reset(tok)
            passed, detail = out if isinstance(out, tuple) else (out, "")
            passed = bool(passed)
            if callable(detail):   # compiled checks explain themselves only when they fail
                tok = _open.set(True)
                try:
                    detail = "" if passed else str(detail())
                except Exception as e:
                    detail = f"(detail failed: {type(e).__name__}: {e})"
                finally:
                    _open.reset(tok)
            run.checks.append({"name": cname, "fn": ref, "key": key, "passed": passed, "detail": detail, "reward": reward,
                               "args": [_arg_spec(a) for a in args],
                               "kwargs": {k: _arg_spec(v) for k, v in kwargs.items()},
                               "readings": [a.id for a in [*args, *kwargs.values()] if isinstance(a, Reading)]})
            return passed
        wrapper.check_fn = fn
        return wrapper
    return deco(fn) if fn else deco


class Checks:
    """The checks recorded so far in this run, as a rule sees them."""

    def __init__(self, records):
        self.records = records

    def _match(self, name, key):
        return [c for c in self.records if c["name"] == name and (key is None or c["key"] == key)]

    def passed(self, name, key=None):
        m = self._match(name, key)
        return bool(m) and all(c["passed"] for c in m)

    def failed(self, name, key=None):
        return any(not c["passed"] for c in self._match(name, key))

    def ran(self, name, key=None):
        return bool(self._match(name, key))

    def failures(self, key=None):
        return [c for c in self.records if not c["passed"] and c.get("reward", True) and (key is None or c["key"] == key)]


def rule(fn=None, *, name=None):
    """Code that settles an outcome. Called as rule(*inputs); the function receives (checks, *values)."""
    if fn is None:
        return lambda f: rule(f, name=name)
    ref = f"{fn.__module__}:{fn.__qualname__}"
    rname = name or fn.__name__

    @functools.wraps(fn)
    def wrapper(*args, key=None, **kwargs):
        run = _current(f"rule {rname}")
        tok = _open.set(True)
        try:
            out = fn(Checks(run.checks), *[_uncover(a) for a in args], **{k: _uncover(v) for k, v in kwargs.items()})
        finally:
            _open.reset(tok)
        if _has_reading(out):
            raise BoundaryError(f"rule {rname} returned a Reading; return the value (reading.value) instead.")
        run.rules.append({"name": rname, "fn": ref, "key": key, "outcome": to_jsonable(out),
                          "readings": [a.id for a in [*args, *kwargs.values()] if isinstance(a, Reading)]})
        return out
    return wrapper


_config = {"trace_dir": "shio-traces"}


def configure(trace_dir=None):
    """trace_dir=None turns trace files off."""
    _config["trace_dir"] = trace_dir


def agent(fn=None, *, record=True, name=None):
    """A unit of work whose readings, checks and rules form one trace. Agents nest; the outermost writes the file."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            parent = _run.get()
            run = Run(name or fn.__qualname__, parent)
            run.inputs = to_jsonable({"args": [a for a in args if not _has_reading(a)], "kwargs": {
                k: v for k, v in kwargs.items() if not _has_reading(v)}})
            tok = _run.set(run)
            try:
                out = fn(*args, **kwargs)
                if _has_reading(out):
                    raise BoundaryError(f"agent {fn.__name__} returned a model proposal; settle it with a @rule first.")
                run.outcome = out
                return out
            except Exception as e:
                run.error = f"{type(e).__name__}: {e}"
                raise
            finally:
                _run.reset(tok)
                run.ended = time.time()
                if parent is not None:
                    parent.children.append(run)
                elif record and _config["trace_dir"]:
                    from .trace import write
                    wrapper.last_trace = write(run, _config["trace_dir"])
        wrapper.last_trace = None
        return wrapper
    return deco(fn) if fn else deco


def parallel(fn, items, workers=4):
    """map(fn, items) on threads, each call inside the current run (contextvars do not follow threads by default)."""
    ctx = contextvars.copy_context()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(lambda x: ctx.copy().run(fn, x), items))
