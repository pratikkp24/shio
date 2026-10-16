"""Re-run a recorded decision and show it comes out the same.

The program is loaded again (its sha256 must match the record, unless changed programs are allowed, which turns
replay into impact analysis: "what would the current rules have decided?"). Every model call is answered with the
raw answer the record holds, matched by the read's name and key in order, and every input is rebuilt from the
record. Then the checks, rules and outcome are compared with what was recorded.
"""
import hashlib
import json
from collections import defaultdict
from pathlib import Path

import datetime as dt

from . import core
from .core import Source
from .ledger import load_vault, merge
from .policy import PolicyError


class Divergence(Exception):
    pass


class RecordedModel:
    """Answers each read with the raw answer the record holds for it."""

    def __init__(self, record):
        self.name = "replay of record"
        self.queues = defaultdict(list)
        for r in sorted(_all(record, "readings"), key=lambda r: (r.get("seq") is None, r.get("seq") or 0)):
            self.queues[(r["name"], json.dumps(r["request"].get("meta", {}).get("key"), default=str))].append(r)
        self.used = 0

    def identity(self):
        return {"kind": "replay", "name": self.name}

    def propose(self, req):
        q = self.queues.get((req.name, json.dumps(req.meta.get("key"), default=str)))
        if not q:
            raise Divergence(f"the program asked for a read '{req.name}' (key {req.meta.get('key')!r}) "
                             "that the record does not have")
        r = q.pop(0)
        self.used += 1
        if r.get("raw") is None:            # the model call failed when recorded: fail the same way
            raise RuntimeError(r.get("error") or "no answer was recorded")
        return r["raw"]


def _all(run, field):
    out = list(run.get(field, []))
    for c in run.get("children", []):
        out += _all(c, field)
    return out


def _decode(x, model):
    if isinstance(x, list):
        return [_decode(v, model) for v in x]
    if isinstance(x, dict):
        if "$source" in x:
            return Source(**x["$source"])
        if "$model" in x:
            return model
        if "$tuple" in x:
            return tuple(_decode(v, model) for v in x["$tuple"])
        if "$dict" in x:
            return {_decode(k, model): _decode(v, model) for k, v in x["$dict"]}
        if "$opaque" in x:
            raise Divergence(f"an input was not recordable ({x['$opaque'][:80]}), so this decision cannot be replayed")
        if "$vault" in x:
            raise Divergence("the record's inputs are in a vault that has been erased")
        return {k: _decode(v, model) for k, v in x.items()}
    return x


def _summary(run):
    checks = [(c["name"], json.dumps(c.get("key"), default=str), c["passed"], bool(c.get("skipped")))
              for c in _all(run, "checks")]
    rules = [(r["name"], json.dumps(r.get("key"), default=str), json.dumps(r["outcome"], sort_keys=True, default=str))
             for r in _all(run, "rules")]
    return checks, rules, json.dumps(run.get("outcome"), sort_keys=True, default=str)


def replay(record_path, program=None, allow_changed=False, policies=None, at=None, jurisdiction=None):
    """-> (reproduced: bool, report lines). Decision tables come from the record (the values it used), unless
    `policies` (a policy.Store) is given: then the tables in force on `at` (default: the decision's date) are used,
    for the record's jurisdiction (or `jurisdiction`, to see what another regulator's limits would have decided),
    which shows what a proposed or new limit would have decided."""
    from . import load
    record_path = Path(record_path)
    record = json.loads(record_path.read_text())
    if record.get("vault"):
        vault = load_vault(record_path, record)
        if vault is None:
            return False, [f"{record_path.name}: its vault was erased, so the model's answers are gone; "
                           "the record still shows what was decided and why"]
        record = merge(record, vault)
    prog = record.get("program") or {}
    path = Path(program or prog.get("path", ""))
    if not path.exists():
        return False, [f"program {path} not found; pass --program"]
    sha = hashlib.sha256(path.read_text().encode()).hexdigest()
    changed = sha != prog.get("sha256")
    if changed and not allow_changed:
        return False, [f"{path.name} has changed since this decision (sha256 {sha[:12]} now, {str(prog.get('sha256'))[:12]} "
                       "recorded); pass --allow-changed to see what the current program would decide"]
    mod = load(path)
    fn = getattr(mod, record["agent"], None)
    if fn is None:
        return False, [f"agent {record['agent']} is not in {path.name}"]
    model = RecordedModel(record)
    try:
        args = _decode(record["inputs"].get("args", []), model)
        kwargs = _decode(record["inputs"].get("kwargs", {}), model)
    except Divergence as e:
        return False, [str(e)]
    saved = {k: core._config.get(k) for k in ("table_override", "as_of", "policies", "jurisdiction")}
    scope = jurisdiction or record.get("jurisdiction")
    if policies is not None:
        try:
            scope = policies.require_jurisdiction(scope, f"replaying {record_path.name} under {policies.where or 'them'}")
        except PolicyError as e:
            return False, [str(e)]
    core._config["jurisdiction"] = scope
    if policies is None:
        core._config["table_override"] = {t["name"]: {"stamp": {k: v for k, v in t.items() if k != "used"},
                                                      "rows": t.get("used") or {}}
                                          for t in record.get("tables") or []}
    else:
        core._config["table_override"] = None
        core._config["policies"] = policies
        core._config["as_of"] = at or dt.date.fromtimestamp(record.get("started") or 0)
    as_of = core._config["as_of"]
    fn.replaying = True
    try:
        fn(*args, **kwargs)
    except Divergence as e:
        return False, [f"diverged: {e}"]
    except PolicyError as e:
        return False, [f"diverged: {e} (the program now reads a table value the decision did not)"]
    finally:
        fn.replaying = False
        core._config.update(saved)
    now = json.loads(json.dumps(core.last_run["run"].to_dict(), default=str))
    (c0, r0, o0), (c1, r1, o1) = _summary(record), _summary(now)
    used = {t["name"]: t for t in now.get("tables") or []}
    table_note = [f"  table {t['name']!r}: recorded v{t['version']}, now v{used[t['name']]['version']}"
                  for t in record.get("tables") or [] if t["name"] in used and used[t["name"]]["version"] != t["version"]]
    lines = [f"replayed {record_path.name} with {path.name}" + (" (CHANGED since the decision)" if changed else "")
             + (f" under the policies in force on {as_of}" + (f" for {scope}" if scope else "")
                if policies is not None else ""),
             f"  {model.used} recorded model answers used, {len(c1)} checks, {len(r1)} rules"] + table_note
    diffs = []
    if c0 != c1:
        for a, b in zip(c0, c1):
            if a != b:
                diffs.append(f"  check {a[0]}[{a[1]}]: recorded passed={a[2]}, now passed={b[2]}")
        if len(c0) != len(c1):
            diffs.append(f"  {len(c0)} checks recorded, {len(c1)} now")
    if r0 != r1:
        for a, b in zip(r0, r1):
            if a != b:
                diffs.append(f"  rule {a[0]}[{a[1]}]: recorded {a[2][:120]}, now {b[2][:120]}")
    if o0 != o1:
        diffs.append(f"  outcome: recorded {o0[:200]}, now {o1[:200]}")
    leftover = sum(len(q) for q in model.queues.values())
    if leftover:
        diffs.append(f"  {leftover} recorded model answers were not asked for")
    lines += diffs or ["  REPRODUCED: same checks, same rules, same outcome"]
    return not diffs, lines
