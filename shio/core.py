"""Readings, checks, rules and agents: the boundary between what a model proposes and what code decides.

A model call returns a Reading. A Reading's value can only be looked at inside a @check or a @rule, so the
model can never settle an outcome on its own: an @agent that returns a Reading is an error.
"""
import contextvars
import datetime
import dataclasses
import builtins
import functools
import getpass
import hashlib
import inspect
import itertools
import json
import os
import time
import uuid
import weakref
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .schema import parse, schema_ref, to_json_schema, to_jsonable

_run = contextvars.ContextVar("shio_run", default=None)
_open = contextvars.ContextVar("shio_open", default=False)   # True inside a @check or @rule

CHECKS = {}   # "module:qualname" -> the plain check function, so training can re-run a check on a new completion


class BoundaryError(TypeError):
    """A model proposal was used where only code may decide."""


class RegulatedError(RuntimeError):
    """A regulated program was asked to run without what regulated mode requires."""


def _blocked(what):
    def method(self, *a, **k):
        raise BoundaryError(f"{self!r} is a model proposal: {what} it only inside a @check or @rule.")
    return method


@dataclasses.dataclass(frozen=True)
class _Sealed:
    value: object
    raw: object
    request: object
    error: object


_SEALED = {}          # id(reading) -> _Sealed; an entry is removed when its reading is collected (see Reading)


def _sealed(r):
    """Shio's own access to a reading's sealed contents (tracing, uncovering for a check or rule). Not for programs."""
    return _SEALED[id(r)]


class Reading:
    """What a model proposed. Opaque outside a @check or @rule.

    What the model said (the parsed value, its raw text, the request it answered, any parse error) is not stored on the
    object, where plain code could reach it as an attribute: it is sealed in a private registry keyed by the reading.
    Code reads it through .value (and .raw, .request, .error) only inside a @check or @rule; Shio's own tracing reads it
    through _sealed()."""
    __slots__ = ("id", "name", "model", "schema", "run_id", "item", "seq", "model_id", "__weakref__")

    def __init__(self, name, model, request, raw, value, error, schema, run_id, item=None):
        self.id, self.name, self.model = uuid.uuid4().hex[:12], name, model
        self.schema, self.run_id = schema, run_id
        _SEALED[id(self)] = _Sealed(value, raw, request, error)
        weakref.finalize(self, _SEALED.pop, id(self), None)
        self.item = item         # (path, index) for one item of a list inside another reading
        self.seq, self.model_id = None, None

    def _gate(self, attr):
        if not _open.get():
            raise BoundaryError(f"{self!r} is a model proposal: read .{attr} only inside a @check or @rule.")
        return getattr(_SEALED[id(self)], attr)

    value = property(lambda self: self._gate("value"))
    raw = property(lambda self: self._gate("raw"))
    request = property(lambda self: self._gate("request"))
    error = property(lambda self: self._gate("error"))

    def __reduce_ex__(self, protocol):
        raise BoundaryError(f"{self!r} is a model proposal: it cannot be pickled or copied.")

    __copy__ = __deepcopy__ = __reduce__ = _blocked("copy")

    def __repr__(self):
        return f"<Reading {self.name} by {self.model}>"

    def _part(self, path, index, value):
        """One item of a list inside this reading: still a proposal, and traced back to this reading."""
        r = Reading(f"{self.name}.{path}[{index}]" if path else f"{self.name}[{index}]", self.model, _sealed(self).request,
                    None, value, None, None, self.run_id, (path, index))
        r.id = self.id
        return r

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
    """A document the model reads. `text` is sent to text-only models; `path` and `media_type` let a model that takes
    files (Claude: images and PDFs; an OpenAI-compatible vision model: images) read the file itself."""
    name: str
    text: str = None
    path: str = None
    media_type: str = None

    @classmethod
    def from_pdf(cls, path, name=None):
        """A PDF: its text for text-only models, and the file itself for models that read PDFs."""
        from pypdf import PdfReader
        pages = [f"[page {i}]\n{p.extract_text() or ''}" for i, p in enumerate(PdfReader(path).pages, 1)]
        return cls(name or str(path).rsplit("/", 1)[-1], "\n\n".join(pages), str(path), "application/pdf")

    @classmethod
    def image(cls, path, name=None):
        """An image (PNG, JPEG, GIF or WebP) for a model that sees images. A text-only model given one fails the read,
        so the reading is null and its checks fail, instead of a guess."""
        suffix = str(path).rsplit(".", 1)[-1].lower()
        kinds = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}
        if suffix not in kinds:
            raise ValueError(f"{path}: an image source must be one of {', '.join(sorted(kinds))}")
        return cls(name or str(path).rsplit("/", 1)[-1], None, str(path), kinds[suffix])

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
    def __init__(self, agent, parent=None, program=None):
        self.id, self.agent, self.parent = uuid.uuid4().hex[:12], agent, parent
        self.root = parent.root if parent else self
        self.program = program or (parent.program if parent else None)
        self.started, self.ended = time.time(), None
        self.inputs, self.readings, self.checks, self.rules, self.children = {}, [], [], [], []
        self.sources, self.outcome, self.error = {}, None, None
        self.codes = {}          # the program's reason-code table
        self.signoff = None      # set on the top-level run when an outcome needs a person
        self.jurisdiction = None # the top-level run's: which regulator's rules govern this decision
        if parent is None:
            self._seq = itertools.count()
            self.tables = {}       # decision tables read in this decision: name -> {stamp, used values}

    def next_seq(self):
        return next(self.root._seq)

    @property
    def regulated(self):
        return bool((self.root.program or {}).get("regulated"))

    def to_dict(self):
        d = {"id": self.id, "agent": self.agent, "started": self.started, "ended": self.ended, "inputs": self.inputs,
             "sources": self.sources,
             "readings": [{"id": r.id, "seq": r.seq, "name": r.name, "model": r.model, "model_id": r.model_id,
                           "schema": schema_ref(r.schema), "pii": sorted(_pii_fields(r.schema)),
                           "classes": _classes(r.schema),
                           "prompt_sha256": _prompt_hash(_sealed(r).request),
                           "request": {**dataclasses.asdict(_sealed(r).request),
                                       "sources": [s.id for s in _sealed(r).request.sources]},
                           "raw": _sealed(r).raw, "value": to_jsonable(_sealed(r).value), "error": _sealed(r).error}
                          for r in self.readings],
             "checks": self.checks, "rules": self.rules, "outcome": to_jsonable(self.outcome), "error": self.error,
             "children": [c.to_dict() for c in self.children]}
        reasons = self.reasons()
        if reasons:              # in clear, not in the vault: the reasons for a decision survive erasure
            d["reasons"] = reasons
        if self.signoff:
            d["signoff"] = self.signoff
        if self.parent is None or (self.program or {}).get("sha256") != (self.parent.program or {}).get("sha256"):
            d["program"] = self.program
        if self.parent is None and self.jurisdiction:
            d["jurisdiction"] = self.jurisdiction
        if self.parent is None and self.tables:
            d["tables"] = [{**t["stamp"], "used": dict(sorted(t["used"].items()))} for t in self.tables.values()]
        return d


    def reasons(self):
        """The reason codes of this run's failed checks, with the customer-facing text the program gives each."""
        out = []
        for c in self.checks:
            if c.get("code") and not c["passed"] and not c.get("skipped"):
                out.append({"code": c["code"], "text": self.codes.get(c["code"], ""), "check": c["name"],
                            "key": to_jsonable(c["key"])})
        return out


def _classes(schema):
    from .policy import classes_of
    try:
        return classes_of(schema)
    except Exception:
        return {}


def _pii_fields(schema):
    return set(getattr(schema, "__shio_pii__", ()) or ())


def _prompt_hash(req):
    from .models import messages_for
    return hashlib.sha256(json.dumps(messages_for(req), sort_keys=True, default=str).encode()).hexdigest()


def _source_hash(s):
    if s.text is not None:
        return hashlib.sha256(s.text.encode()).hexdigest()
    try:
        return hashlib.sha256(Path(s.path).read_bytes()).hexdigest()
    except (OSError, TypeError):
        return None


def model_identity(by):
    try:
        return by.identity() if hasattr(by, "identity") else {"kind": type(by).__name__}
    except Exception as e:
        return {"kind": type(by).__name__, "identity_error": str(e)[:200]}


def _encode_input(x):
    """An agent input as JSON that `shio replay` can turn back into the same input."""
    if isinstance(x, Source):
        return {"$source": dataclasses.asdict(x)}
    if hasattr(x, "propose"):
        return {"$model": model_identity(x)}
    if isinstance(x, tuple):
        return {"$tuple": [_encode_input(v) for v in x]}
    if isinstance(x, list):
        return [_encode_input(v) for v in x]
    if isinstance(x, dict):
        if all(isinstance(k, str) for k in x):
            return {k: _encode_input(v) for k, v in x.items()}
        return {"$dict": [[_encode_input(k), _encode_input(v)] for k, v in x.items()]}
    if x is None or isinstance(x, (str, int, float, bool)):
        return x
    return {"$opaque": repr(x)[:300]}


def _current(what):
    run = _run.get()
    if run is None:
        raise BoundaryError(f"{what} must be called inside an @agent.")
    return run


def _uncover(x):
    """Replace readings by their values (used when code, not a model, is about to look)."""
    if isinstance(x, Reading):
        return _sealed(x).value
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
    run, req, ident = _prepare_read(schema, ask, by, sources, context, system, name, blind, key, meta)
    try:
        _check_pinned(run, ident)
        raw = by.propose(req)
    except Exception as e:  # a model that times out, errors or is refused gives a null reading
        return _finish_read(run, req, ident, by, schema, None, e)
    return _finish_read(run, req, ident, by, schema, raw, None)


async def aread(schema, ask, *, by, sources=(), context=None, system=None, name=None, blind=False, key=None, meta=None):
    """read() for async agents: the model call doesn't block the event loop. A model with `async apropose(request)`
    is awaited; any other runs its propose() in a worker thread."""
    import asyncio
    run, req, ident = _prepare_read(schema, ask, by, sources, context, system, name, blind, key, meta)
    try:
        _check_pinned(run, ident)
        raw = await (by.apropose(req) if hasattr(by, "apropose") else asyncio.to_thread(by.propose, req))
    except Exception as e:
        return _finish_read(run, req, ident, by, schema, None, e)
    return _finish_read(run, req, ident, by, schema, raw, None)


def _prepare_read(schema, ask, by, sources, context, system, name, blind, key, meta):
    run = _current("read()")
    if blind and context:
        raise BoundaryError("A blind read cannot see other readings; drop context= or blind=True.")
    sources = [s if isinstance(s, Source) else Source(str(s), path=str(s)) for s in _flat(sources)]
    meta = {**(meta or {}), **({"key": key} if key is not None else {})}
    for s in sources:
        run.sources.setdefault(s.id, {"name": s.name, "path": s.path, "text": s.text, "media_type": s.media_type,
                                      "sha256": _source_hash(s)})
    req = ReadRequest(name or getattr(schema, "__name__", "reading"), ask, system or "", sources,
                      to_jsonable(_uncover(context or {})), to_json_schema(schema), blind, meta)
    return run, req, model_identity(by)


def _check_pinned(run, ident):
    if run.regulated and ident.get("kind") not in ("replay",) and not ident.get("pinned"):
        raise RegulatedError(f"model {ident.get('model', ident.get('kind'))} is not pinned; a regulated program "
                             "only calls a model pinned to an exact version (ollama(..., digest=...) or pinned=...)")


def _finish_read(run, req, ident, by, schema, raw, exc):
    if exc is not None:
        value, error = None, f"model call failed: {type(exc).__name__}: {exc}"
    else:
        value, error = parse(schema, raw)
    r = Reading(req.name, getattr(by, "name", type(by).__name__), req, raw, value, error, schema, run.id)
    r.seq, r.model_id = run.next_seq(), ident
    run.readings.append(r)
    return r


def _flat(x):
    if x is None:
        return []
    if isinstance(x, (list, tuple)):
        return [s for v in x for s in _flat(v)]
    return [x]


def each(reading, path=""):
    """The items of a list the model returned (reading.path), each still a reading. For `for x in r.items:`."""
    if not isinstance(reading, Reading):
        raise BoundaryError(f"each() takes a reading, got {type(reading).__name__}")
    v = _sealed(reading).value
    for p in path.split(".") if path else []:
        v = None if v is None else (v.get(p) if isinstance(v, dict) else getattr(v, p, None))
    if v is None:
        return []
    return [reading._part(path, i, x) for i, x in enumerate(v)]


def _arg_spec(a):
    if isinstance(a, Reading) and a.item:
        return {"item": [a.id, a.item[0], a.item[1]]}
    if isinstance(a, Reading):
        return {"reading": a.id}
    try:
        frozen = json.loads(json.dumps(a))     # a snapshot: later changes to the object do not rewrite history
    except (TypeError, ValueError):
        frozen = None
    if frozen == a and type(frozen) is type(a):
        return {"const": frozen}
    return {"opaque": repr(a)[:200]}           # would not come back the same from JSON, so it cannot be replayed


def check(fn=None, *, name=None, reward=True, after=(), code=None):
    """A deterministic test over readings. Returns True/False to the agent, and is recorded in the trace, where it
    doubles as a training reward (reward=False: a `when`, which only steers control flow). The function may return a
    bool or (bool, detail). code: a stable reason code (`check NAME as CODE`), recorded with the result."""
    def deco(fn):
        ref = f"{fn.__module__}:{fn.__qualname__}"
        cname = name or fn.__name__
        CHECKS[ref] = fn

        @functools.wraps(fn)
        def wrapper(*args, key=None, **kwargs):
            run = _current(f"check {cname}")
            blocked = [c["name"] for c in run.checks if c["name"] in after and not c["passed"]
                       and (c["key"] is None or key is None or c["key"] == key)]
            if blocked:      # `check b after a`: when a failed, b says nothing either way
                run.checks.append({"name": cname, "fn": ref, "key": key, "code": code, "passed": False, "skipped": True,
                                   "detail": f"not checked: {', '.join(dict.fromkeys(blocked))} failed",
                                   "reward": False, "args": [], "kwargs": {}, "readings": []})
                return False
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
                    detail = f"{cname} failed (its explanation could not be written: {type(e).__name__}: {e})"
                finally:
                    _open.reset(tok)
            run.checks.append({"name": cname, "fn": ref, "key": key, "code": code, "passed": passed, "detail": detail,
                               "reward": reward,
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
        """The check ran and failed (a check skipped because an earlier one failed does not count)."""
        return any(not c["passed"] and not c.get("skipped") for c in self._match(name, key))

    def ran(self, name, key=None):
        return bool(self._match(name, key))

    def failures(self, key=None):
        """Failed checks (not whens, not skipped), optionally only those with this key."""
        return [c for c in self.records if not c["passed"] and c.get("reward", True) and not c.get("skipped")
                and (key is None or c["key"] == key)]

    def skipped(self, key=None):
        return [c for c in self.records if c.get("skipped") and (key is None or c["key"] == key)]


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


_config = {"trace_dir": "shio-traces", "vault": False, "signer": None, "actor": None, "policies": None,
           "table_override": None, "as_of": None, "jurisdiction": None}
last_run = {"run": None}        # the most recent top-level run (shio replay reads it)


def configure(trace_dir=None, vault=None, signer=None, actor=None, policies=None, jurisdiction=None, as_of=None, vault_key=None):
    """trace_dir=None turns trace files off (not allowed for a regulated program). vault=True keeps free text and
    personal data in a separate, erasable vault file. signer: a ledger.Signer (default: SHIO_SIGNING_KEY).
    actor: who is running the program, recorded as the requester of any sign-off (default: SHIO_ACTOR, or the
    operating-system user). jurisdiction: whose rules govern each decision, e.g. "IN" (default: SHIO_JURISDICTION;
    "" clears it); policy and table versions with `applies_to` are resolved for it. as_of: the date decisions are made
    under (a datetime.date or "YYYY-MM-DD"); in_force() then reads the tables in force on that date, so a September close
    run in October uses September's rates and limits. Default: the day each decision runs ("" clears it).
    vault_key: encrypt every vault at rest with AES-256-GCM (a key file from `shio vaultkey`, or "hex:..."); default
    SHIO_VAULT_KEY."""
    _config["trace_dir"] = trace_dir
    if vault_key is not None:              # encrypt vaults at rest (AES-256-GCM): a key file, "hex:...", or "" to stop
        from .ledger import set_vault_key
        set_vault_key(vault_key)
    if as_of is not None:
        _config["as_of"] = (datetime.date.fromisoformat(as_of) if isinstance(as_of, str) and as_of else as_of) or None
    if jurisdiction is not None:
        from .policy import jurisdiction_code
        _config["jurisdiction"] = jurisdiction_code(jurisdiction) if jurisdiction else None
    if policies is not None:      # a folder of .policy files, a policy.Store, or False for none
        from .policy import Store
        _config["policies"] = policies if policies is False or isinstance(policies, Store) else Store.load(policies)
    if actor is not None:
        _config["actor"] = actor or None
    if vault is not None:
        _config["vault"] = vault
    if signer is not None:
        _config["signer"] = signer


def _policies():
    """The data-handling policy store: configure(policies=...) or SHIO_POLICIES (a folder of .policy files)."""
    if _config["policies"] is None:
        path = os.getenv("SHIO_POLICIES")
        from .policy import Store
        _config["policies"] = Store.load(path) if path else False
    return _config["policies"] or None


def _jurisdiction(run):
    """The jurisdiction a top-level run decides for. With jurisdiction-scoped policies configured, a run without one
    (or with one no version is scoped to) is refused rather than resolved by guessing."""
    from .policy import PolicyError, jurisdiction_code
    j = _config["jurisdiction"] or os.getenv("SHIO_JURISDICTION") or None
    store = _policies()
    try:
        if store is None:
            return jurisdiction_code(j) if j else None
        return store.require_jurisdiction(j, f"a decision by {run.agent}")
    except PolicyError as e:
        if run.regulated:
            raise RegulatedError(f"{run.program['path']} is regulated: {e}") from None
        raise


class InForce:
    """The values of a decision table in force for this decision. Each value read is recorded with the table's
    version, so the record shows exactly which limit applied. A name the table does not have is an error."""

    def __init__(self, name, stamp, rows, used):
        self._name, self._stamp, self._rows, self._used = name, stamp, rows, used

    def __getitem__(self, key):
        if key not in self._rows:
            from .policy import PolicyError
            raise PolicyError(f"table {self._name!r} v{self._stamp.get('version')} has no {key!r}")
        self._used[key] = self._rows[key]
        return self._rows[key]

    def __getattr__(self, key):
        if key.startswith("_"):
            raise AttributeError(key)
        return self[key]

    def __contains__(self, key):
        return key in self._rows

    def __repr__(self):
        return f"<{self._name} v{self._stamp.get('version')}>"


def in_force(name):
    """A decision table's values as in force on the date of this decision (see shio.policy.Table)."""
    from .policy import PolicyError
    run = _current("in_force()")
    root = run.root
    if name not in root.tables:
        override = (_config.get("table_override") or {}).get(name)
        if override is not None:               # shio replay: the values the record says were used
            stamp, rows = override["stamp"], override["rows"]
        else:
            on = _config.get("as_of") or datetime.date.fromtimestamp(root.started)
            store = _policies()
            t = store.table(name, on, root.jurisdiction) if store else None
            if t is None:
                raise PolicyError(f"no version of table {name!r} is in force on {on}"
                                  + (f" for {root.jurisdiction}" if root.jurisdiction else "")
                                  + ("" if store else " (no policies configured: --policies or SHIO_POLICIES)"))
            stamp, rows = t.stamp(), t.rows
        root.tables[name] = {"stamp": stamp, "rows": rows, "used": {}}
    t = root.tables[name]
    return InForce(name, t["stamp"], t["rows"], t["used"])


def _signer():
    if _config["signer"] is None:
        from .ledger import load_signer
        _config["signer"] = load_signer() or False
    return _config["signer"] or None


def actor():
    return _config["actor"] or os.getenv("SHIO_ACTOR") or getpass.getuser()


def _labels(outcome):
    """What an outcome is called, for `signoff LABEL`: the outcome itself if it is a string, else its status,
    action, decision or outcome field."""
    if isinstance(outcome, str):
        return {outcome}
    if isinstance(outcome, dict):
        return {v for k, v in outcome.items() if k in ("status", "action", "decision", "outcome") and isinstance(v, str)}
    return set()


def _needs_signoff(decl, outcome, env):
    """-> None, or why this outcome needs a person. A condition that cannot be evaluated needs one (fail closed)."""
    if decl["label"] != "*" and decl["label"] not in _labels(outcome):
        return None
    if not decl.get("fn"):
        return f"every {decl['label']} needs sign-off"
    fn = env.get(decl["fn"])
    names = fn.__code__.co_varnames[:fn.__code__.co_argcount]
    fields = outcome if isinstance(outcome, dict) else {}
    args, missing = [], []
    for n in names:
        if n in fields:
            args.append(fields[n])
        elif n in env:
            args.append(env[n])
        elif hasattr(builtins, n):
            args.append(getattr(builtins, n))
        else:
            missing.append(n)
    if missing:
        return f"{decl['when']}: could not be evaluated (the outcome has no {', '.join(missing)}), so a person decides"
    try:
        hit = fn(*args)
    except Exception as e:
        return f"{decl['when']}: could not be evaluated ({type(e).__name__}: {e}), so a person decides"
    if not hit:
        return None
    shown = [f"{n}={v!r}" for n, v in zip(names, args) if not callable(v)]
    return decl["when"] + (f" ({', '.join(shown)})" if shown else "")


def _makers(decls, outcome):
    names = [actor()]
    fields = outcome if isinstance(outcome, dict) else {}
    for f in ["maker"] + [m for d in decls for m in d.get("makers", [])]:
        v = fields.get(f)
        for x in (v if isinstance(v, (list, tuple)) else [v]):
            if x not in (None, ""):
                names.append(str(x))
    return list(dict.fromkeys(names))


def _apply_signoffs(run, signoffs, outcome, env):
    """An outcome a `signoff` names comes back as PENDING; the request goes to the top-level run's record, where
    `shio signoff` answers it. From a nested agent the parent sees PENDING too, so wrapping cannot skip a sign-off."""
    hits = [(d, why) for d in signoffs for why in [_needs_signoff(d, outcome, env)] if why]
    if not hits:
        return outcome
    roles = list(dict.fromkeys(r for d, _ in hits for r in d["roles"]))
    request = {"agent": run.agent, "labels": list(dict.fromkeys(d["label"] for d, _ in hits)), "roles": roles,
               "because": [why for _, why in hits], "lines": [d["line"] for d, _ in hits],
               "requested_by": actor(), "makers": _makers([d for d, _ in hits], outcome),
               "decided": to_jsonable(outcome)}
    root = run.root
    if root.signoff:             # several agents in one run need sign-off: one request, every role
        prev = root.signoff
        request = {**prev, "roles": list(dict.fromkeys(prev["roles"] + roles)),
                   "labels": list(dict.fromkeys(prev["labels"] + request["labels"])),
                   "because": prev["because"] + request["because"], "lines": prev["lines"] + request["lines"],
                   "makers": list(dict.fromkeys(prev["makers"] + request["makers"])),
                   "decided": request["decided"] if run is root else prev["decided"]}
    root.signoff = request
    return {"status": "PENDING", "signoff": {"roles": roles, "because": [why for _, why in hits]},
            "decided": to_jsonable(outcome)}


def agent(fn=None, *, record=True, name=None, signoffs=()):
    """A unit of work whose readings, checks and rules form one trace. Agents nest; the outermost writes the file.
    signoffs: the outcomes that need a person (`signoff` in Shio); such an outcome is returned as PENDING."""
    def deco(fn):
        def begin(args, kwargs):
            parent = _run.get()
            run = Run(name or fn.__qualname__, parent, fn.__globals__.get("__shio_program__"))
            run.codes = fn.__globals__.get("__shio_codes__") or {}
            if parent is None and run.regulated and not wrapper.replaying:
                if not (record and _config["trace_dir"]):
                    raise RegulatedError(f"{run.program['path']} is regulated: every run must be recorded "
                                         "(trace files cannot be turned off)")
                if not _signer():
                    raise RegulatedError(f"{run.program['path']} is regulated: set SHIO_SIGNING_KEY to a key from "
                                         "`shio keygen` (or hmac:<secret>) so every record is signed")
            if parent is None:
                run.jurisdiction = _config["jurisdiction"] if wrapper.replaying else _jurisdiction(run)
            run.inputs = {"args": [_encode_input(a) for a in args if not _has_reading(a)],
                          "kwargs": {k: _encode_input(v) for k, v in kwargs.items() if not _has_reading(v)}}
            return run, parent, _run.set(run)

        def settle(run, parent, out):
            if _has_reading(out):
                raise BoundaryError(f"agent {fn.__name__} returned a model proposal; settle it with a @rule first.")
            if signoffs:
                out = _apply_signoffs(run, signoffs, out, fn.__globals__)
            if parent is None and run.signoff and not (isinstance(out, dict) and out.get("status") == "PENDING"):
                # a nested agent needed sign-off and this one decided anyway: it still waits for the person
                out = {"status": "PENDING", "signoff": {"roles": run.signoff["roles"],
                                                        "because": run.signoff["because"]},
                       "decided": to_jsonable(out)}
                run.signoff["decided"] = out["decided"]
            run.outcome = out
            return out

        def end(run, parent, tok):
            _run.reset(tok)
            run.ended = time.time()
            if parent is not None:
                parent.children.append(run)
            else:
                last_run["run"] = run
                if record and _config["trace_dir"] and not wrapper.replaying:
                    from .trace import write
                    store = _policies()
                    handling = store.handling(datetime.date.fromtimestamp(run.started),
                                              run.jurisdiction) if store else None
                    wrapper.last_trace = write(run, _config["trace_dir"],
                                               vault=_config["vault"] or run.regulated or handling is not None,
                                               signer=_signer(), handling=handling)

        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def wrapper(*args, **kwargs):
                run, parent, tok = begin(args, kwargs)
                try:
                    return settle(run, parent, await fn(*args, **kwargs))
                except Exception as e:
                    run.error = f"{type(e).__name__}: {e}"
                    raise
                finally:
                    end(run, parent, tok)
        else:
            @functools.wraps(fn)
            def wrapper(*args, **kwargs):
                run, parent, tok = begin(args, kwargs)
                try:
                    return settle(run, parent, fn(*args, **kwargs))
                except Exception as e:
                    run.error = f"{type(e).__name__}: {e}"
                    raise
                finally:
                    end(run, parent, tok)
        wrapper.last_trace = None
        wrapper.replaying = False
        return wrapper
    return deco(fn) if fn else deco


def parallel(fn, items, workers=4):
    """map(fn, items) on threads, each call inside the current run (contextvars do not follow threads by default)."""
    ctx = contextvars.copy_context()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(lambda x: ctx.copy().run(fn, x), items))
