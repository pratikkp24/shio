"""An evidence pack for an audit period: one folder an auditor can take away.

    shio evidence records/ --from 2026-07-01 --to 2026-09-30 --policies policies/ --pubkey keys/shio-signing.pub -o pack/

The pack holds:
  evidence.md     integrity, decisions, oversight, monitoring and policy for the period, in one readable report
  programs/       the decision map (shio explain) of every program version that decided in the period
  data.json       the figures behind the report
  manifest.json   the sha256 of every file in the pack, signed with the signing key if one is configured

Monitoring needs no labels: every check is a test of the model's work against something it cannot argue with, so
a check's pass rate falling from one week to the next, more model calls failing, or the model's weights changing
mid-period are all visible from the records alone.
"""
import datetime as dt
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path

from .ledger import _entries, sha256, verify

STATUS_KEYS = ("status", "action", "decision", "verdict", "result", "route", "release", "merge", "flag")
DROP = 0.10           # a check whose weekly pass rate falls this far below its rate in the weeks before is flagged


def _outcome_label(outcome):
    if isinstance(outcome, dict):
        for k in STATUS_KEYS:
            if isinstance(outcome.get(k), str):
                return outcome[k]
        return "(no status)"
    return str(outcome)[:40] if outcome is not None else "(none)"


def _runs(run):
    yield run
    for c in run.get("children", []):
        yield from _runs(c)


def _week(day):
    y, w, _ = day.isocalendar()
    return f"{y}-W{w:02d}"


def collect(folder, start, end, store=None, public_key=None, secret=None, today=None):
    folder = Path(folder)
    entries = _entries(folder)
    ok, vreport, head = verify(folder, public_key, secret)
    data = {"period": [start.isoformat(), end.isoformat()], "folder": str(folder),
            "integrity": {"verified": ok, "report": vreport, "head": head, "entries": len(entries)},
            "decisions": 0, "by_program": Counter(), "outcomes": Counter(), "by_agent": Counter(),
            "reasons": Counter(), "models": Counter(), "model_failures": 0, "reads": 0,
            "checks": defaultdict(lambda: defaultdict(lambda: [0, 0])), "events": Counter(),
            "pending": [], "signoffs": Counter(), "tables": Counter(), "policies": Counter(), "programs": {}}
    in_period = set()
    for e in entries:
        when = dt.date.fromtimestamp(e.get("time", 0))
        if e.get("kind") != "record":
            if start <= when <= end:
                data["events"][e.get("kind")] += 1
                if e.get("kind") == "signoff":
                    data["signoffs"][f"{e.get('role')}: {e.get('decision', 'approved')}"] += 1
            continue
        path = folder / e["file"]
        if not path.exists():
            continue
        rec = json.loads(path.read_text())
        day = dt.date.fromtimestamp(rec.get("started") or e.get("time", 0))
        if not start <= day <= end:
            continue
        in_period.add(e["file"])
        data["decisions"] += 1
        prog = rec.get("program") or {}
        key = f"{Path(prog.get('path', '?')).name} {str(prog.get('sha256', ''))[:12]}"
        data["by_program"][key] += 1
        data["programs"][prog.get("sha256")] = prog
        data["by_agent"][rec.get("agent")] += 1
        data["outcomes"][f"{rec.get('agent')}: {_outcome_label(rec.get('outcome'))}"] += 1
        if (rec.get("signoff") or {}).get("roles"):
            data["pending"].append(e["file"])
        for s in (rec.get("data_policy") or {}).get("in_force", []):
            data["policies"][f"{s['name']} v{s['version']}"] += 1
        for t in rec.get("tables") or []:
            data["tables"][f"{t['name']} v{t['version']}"] += 1
        for run in _runs(rec):
            for r in run.get("readings", []):
                data["reads"] += 1
                mid = r.get("model_id") or {}
                data["models"][" ".join(str(x) for x in (mid.get("kind"), mid.get("model") or mid.get("name"),
                                                         (mid.get("digest") or mid.get("pinned") or "")[:12]) if x)] += 1
                if r.get("raw") is None:          # no answer at all (an error or refusal; its text may be in the vault)
                    data["model_failures"] += 1
            for c in run.get("checks", []):
                if c.get("skipped") or not c.get("reward", True):
                    continue
                bucket = data["checks"][f"{run.get('agent')}.{c['name']}"][_week(day)]
                bucket[0] += bool(c.get("passed"))
                bucket[1] += 1
            for code in [c.get("code") for c in run.get("checks", []) if c.get("code") and not c.get("passed")
                         and not c.get("skipped")]:
                data["reasons"][code] += 1
    data["pending"] = sorted(set(data["pending"]))
    if store is not None:
        from .policy import audit
        a_ok, a_report, _ = audit(folder, store, today or dt.date.today())
        relevant = [line for line in a_report if not line.startswith("  FINDING") or
                    any(f in line for f in in_period)]
        data["audit"] = {"compliant": all(not x.startswith("  FINDING") for x in relevant), "report": relevant}
    data["monitoring"] = _monitor(data["checks"], data["models"], data["model_failures"], data["reads"])
    return data


def _monitor(checks, models, failures, reads):
    flags = []
    rates = {}
    for name, weeks in checks.items():
        total = sum(n for _, n in weeks.values())
        passed = sum(p for p, _ in weeks.values())
        avg = passed / total if total else 1.0
        series = {w: (p / n if n else None) for w, (p, n) in sorted(weeks.items())}
        rates[name] = {"pass_rate": round(avg, 3), "n": total, "weekly": series}
        before_p = before_n = 0
        for w, (p, n) in sorted(weeks.items()):     # each week against the weeks before it
            if before_n >= 3 and n >= 3 and before_p / before_n - p / n > DROP:
                flags.append(f"{name}: pass rate {p / n:.0%} in {w}, down from {before_p / before_n:.0%} before it")
            before_p, before_n = before_p + p, before_n + n
    real = [m for m in models if not m.startswith("replay")]
    by_model = defaultdict(set)
    for m in real:
        parts = m.split(" ")
        by_model[" ".join(parts[:2])].add(parts[2] if len(parts) > 2 else "")
    for m, digests in by_model.items():
        if len(digests) > 1:
            flags.append(f"{m}: served with {len(digests)} different weights in the period ({', '.join(sorted(digests))})")
    if reads and failures / reads > 0.05:
        flags.append(f"{failures} of {reads} model calls gave no usable answer ({failures / reads:.0%})")
    return {"rates": rates, "flags": flags}


def _table(headers, rows):
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    out += ["| " + " | ".join(str(c).replace("|", "\\|") for c in r) + " |" for r in rows]
    return out


def report(data):
    s, e = data["period"]
    integ = data["integrity"]
    lines = [f"# Evidence pack: {s} to {e}", "",
             f"Records folder: `{data['folder']}`. Generated by `shio evidence` on {dt.date.today()}.", "",
             "## Integrity", "",
             f"- Ledger verification: **{'VERIFIED' if integ['verified'] else 'NOT VERIFIED'}** over "
             f"{integ['entries']} entries.",
             f"- Chain head: `{integ['head']}`. Compare it with the head anchored at the end of the period.", ""]
    lines += ["```", *integ["report"], "```", ""]
    ev = data["events"]
    lines += ["## Decisions", "",
              f"{data['decisions']} decisions in the period; {ev.get('signoff', 0)} sign-offs, "
              f"{ev.get('erasure', 0)} erasures, {ev.get('migration', 0)} re-seals under a stricter data policy.", ""]
    lines += _table(["Program (sha256)", "Decisions"], data["by_program"].most_common()) + [""]
    lines += _table(["Agent: outcome", "Count"], data["outcomes"].most_common()) + [""]
    lines += ["## Human oversight", ""]
    lines += (_table(["Role: decision", "Count"], data["signoffs"].most_common()) if data["signoffs"]
              else ["No sign-offs in the period."]) + [""]
    lines += ([f"Decisions still waiting for sign-off: {len(data['pending'])} (see `shio verify`)."]
              if data["pending"] else ["No decision in the period is waiting for sign-off."]) + [""]
    if data["reasons"]:
        lines += ["## Reasons given", ""] + _table(["Reason code", "Times"], data["reasons"].most_common()) + [""]
    mon = data["monitoring"]
    lines += ["## Monitoring", "",
              "Each check tests the model's work against something it cannot argue with, so its pass rate is a "
              "quality measure that needs no human labels.", ""]
    lines += ([f"- **{f}**" for f in mon["flags"]] or ["- No drops, weight changes or failure spikes flagged."]) + [""]
    rows = [[name, f"{r['pass_rate']:.0%}", r["n"], "  ".join(f"{w[-3:]} {v:.0%}" for w, v in r["weekly"].items()
                                                           if v is not None)]
            for name, r in sorted(mon["rates"].items())]
    lines += _table(["Check", "Pass rate", "Runs", "By week"], rows) + [""]
    lines += _table(["Model (identity)", "Reads"], data["models"].most_common()) + [""]
    lines += [f"Model calls with no usable answer: {data['model_failures']} of {data['reads']}.", ""]
    lines += ["## Policies and limits", ""]
    if data["policies"] or data["tables"]:
        lines += _table(["Data policy or table version", "Decisions"],
                        (data["policies"] + data["tables"]).most_common()) + [""]
    else:
        lines += ["No data-handling policy or decision table was in force for these decisions.", ""]
    if "audit" in data:
        lines += [f"Audit against the policies in force on each decision's date: "
                  f"**{'COMPLIANT' if data['audit']['compliant'] else 'FINDINGS'}**", "", "```",
                  *data["audit"]["report"], "```", ""]
    lines += ["## Programs", "", "The decision map of each program version that decided in the period is in "
              "`programs/`. A version whose source is no longer at its recorded path is listed without a map.", ""]
    return "\n".join(lines)


def build(folder, out, start, end, store=None, public_key=None, secret=None, signer=None, today=None):
    from .explain import explain
    out = Path(out)
    (out / "programs").mkdir(parents=True, exist_ok=True)
    data = collect(folder, start, end, store, public_key, secret, today)
    maps = []
    for sha, prog in data["programs"].items():
        p = Path(prog.get("path", ""))
        if p.exists() and hashlib.sha256(p.read_text().encode()).hexdigest() == sha:
            target = out / "programs" / f"{p.stem}-{sha[:12]}.md"
            target.write_text(explain(p))
            maps.append(target.name)
    (out / "evidence.md").write_text(report(data) + "\n" + "\n".join(f"- [{m}](programs/{m})" for m in maps) + "\n")
    plain = json.loads(json.dumps(data, default=lambda o: dict(o) if hasattr(o, "items") else str(o)))
    (out / "data.json").write_text(json.dumps(plain, indent=1, ensure_ascii=False))
    files = {str(f.relative_to(out)): hashlib.sha256(f.read_bytes()).hexdigest()
             for f in sorted(out.rglob("*")) if f.is_file() and f.name != "manifest.json"}
    manifest = {"period": data["period"], "head": data["integrity"]["head"], "files": files,
                "generated": dt.datetime.now().isoformat(timespec="seconds")}
    manifest["sha256"] = sha256(manifest)
    if signer:
        manifest.update(key_id=signer.key_id, signature=signer.sign(manifest["sha256"]))
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1))
    return data, out


def check_manifest(pack, public_key=None, secret=None):
    """An evidence pack is unchanged since it was built: every file's hash, and the manifest's signature."""
    from .ledger import _check_signature
    pack = Path(pack)
    m = json.loads((pack / "manifest.json").read_text())
    problems = [f"{name}: changed" for name, h in m["files"].items()
                if not (pack / name).exists() or hashlib.sha256((pack / name).read_bytes()).hexdigest() != h]
    body = {k: m[k] for k in ("period", "head", "files", "generated")}
    if sha256(body) != m.get("sha256"):
        problems.append("manifest.json: edited")
    state = _check_signature(m, public_key, secret) if m.get("signature") else "unsigned"
    if state.startswith("BAD"):
        problems.append(f"manifest.json: {state}")
    return not problems, problems, state
