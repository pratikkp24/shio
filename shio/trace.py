"""Traces: every run of an agent saved as JSON (readings, checks, rules, outcome). They are the audit trail in
production and the training data later."""
import glob
import json
import time
from pathlib import Path


def write(run, trace_dir):
    d = Path(trace_dir)
    d.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(run.started))
    path = d / f"{run.agent}-{stamp}-{run.id}.json"
    path.write_text(json.dumps(run.to_dict(), indent=1, default=str))
    return path


def load(patterns):
    """Yield every run (children included) from trace files matching the patterns."""
    files = sorted({f for p in ([patterns] if isinstance(patterns, str) else patterns)
                    for f in (glob.glob(p, recursive=True) if any(c in p for c in "*?[") else [p])})
    for f in files:
        if Path(f).is_dir():
            yield from load(str(Path(f) / "*.json"))
            continue
        yield from _flatten(json.loads(Path(f).read_text()), None)


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
