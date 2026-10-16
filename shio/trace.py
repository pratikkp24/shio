"""Traces: every run of an agent saved as JSON (readings, checks, rules, outcome). They are the audit trail in
production and the training data later."""
import glob
import json
import time
from pathlib import Path


def write(run, trace_dir, vault=False, signer=None, handling=None):
    """Write a top-level run as a record entered in the folder's ledger (see shio.ledger), under the data-handling
    policies in force on its date (see shio.policy)."""
    from .ledger import write as ledger_write
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(run.started))
    return ledger_write(run.to_dict(), trace_dir, f"{run.agent}-{stamp}-{run.id}", use_vault=vault, signer=signer,
                        handling=handling)


def load(patterns):
    """Yield every run (children included) from trace files matching the patterns."""
    files = sorted({f for p in ([patterns] if isinstance(patterns, str) else patterns)
                    for f in (glob.glob(p, recursive=True) if any(c in p for c in "*?[") else [p])})
    from .ledger import load_vault, merge
    for f in files:
        if Path(f).is_dir():
            yield from load(str(Path(f) / "*.json"))
            continue
        rec = json.loads(Path(f).read_text())
        if "agent" not in rec:          # not a run record (a vault or another JSON file)
            continue
        vault = load_vault(f, rec)
        if vault:                       # free text kept apart: put it back for training and inspection
            rec = merge(rec, vault)
        yield from _flatten(rec, None)


def _flatten(run, sources):
    sources = {**(sources or {}), **run.get("sources", {})}
    run["sources"] = sources
    yield run
    for c in run.get("children", []):
        yield from _flatten(c, sources)


def summary(run):
    claims = [c for c in run["checks"] if c.get("reward", True)]
    lines = [f"{run['agent']}  run {run['id']}  {len(run['readings'])} readings, {len(claims)} checks, "
             f"{len(run['rules'])} rules" + (f"  ERROR {run['error']}" if run.get("error") else "")]
    failed = [c for c in claims if not c["passed"]]
    passed = len(claims) - len(failed)
    lines.append(f"  checks: {passed} passed, {len(failed)} failed")
    for c in failed[:10]:
        lines.append(f"    x {c['name']}" + (f"[{c['key']}]" if c["key"] is not None else "") +
                     (f": {c['detail']}" if c["detail"] else ""))
    bad = [r for r in run["readings"] if r["error"]]
    if bad:
        lines.append(f"  unparseable readings: {len(bad)}")
    return "\n".join(lines)
