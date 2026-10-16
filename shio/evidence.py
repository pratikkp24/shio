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

The same figures drive live alerts while a period runs (shio monitor): see monitor().
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


def collect(folder, start, end, store=None, public_key=None, secret=None, today=None, jurisdiction=None,
            integrity=True):
    """The figures for decisions made from start to end (inclusive). jurisdiction: only decisions made for it.
    integrity=False skips the ledger verification (monitor runs often; verify and evidence do that)."""
    folder = Path(folder)
    entries = _entries(folder)
    ok, vreport, head = verify(folder, public_key, secret) if integrity else (None, [], None)
    data = {"period": [start.isoformat(), end.isoformat()], "folder": str(folder), "jurisdiction": jurisdiction,
            "integrity": {"verified": ok, "report": vreport, "head": head, "entries": len(entries)},
            "decisions": 0, "by_program": Counter(), "outcomes": Counter(), "by_agent": Counter(),
            "reasons": Counter(), "models": Counter(), "model_failures": 0, "reads": 0,
            "checks": defaultdict(lambda: defaultdict(lambda: [0, 0])), "events": Counter(),
            "pending": [], "signoffs": Counter(), "tables": Counter(), "policies": Counter(), "programs": {},
            "by_jurisdiction": Counter()}
    in_period, scope_of = set(), {}
    for e in entries:
        when = dt.date.fromtimestamp(e.get("time", 0))
        if e.get("kind") != "record":
            if jurisdiction and scope_of.get(e.get("file")) != jurisdiction:
                continue
            if start <= when <= end:
                data["events"][e.get("kind")] += 1
                if e.get("kind") == "signoff":
                    data["signoffs"][f"{e.get('role')}: {e.get('decision', 'approved')}"] += 1
            continue
        path = folder / e["file"]
        if not path.exists():
            continue
        rec = json.loads(path.read_text())
        scope_of[e["file"]] = rec.get("jurisdiction")
        if jurisdiction and rec.get("jurisdiction") != jurisdiction:
            continue
        day = dt.date.fromtimestamp(rec.get("started") or e.get("time", 0))
        if not start <= day <= end:
            continue
        in_period.add(e["file"])
        data["decisions"] += 1
        data["by_jurisdiction"][rec.get("jurisdiction") or "(none)"] += 1
        prog = rec.get("program") or {}
        key = f"{Path(prog.get('path', '?')).name} {str(prog.get('sha256', ''))[:12]}"
        data["by_program"][key] += 1
        data["programs"][prog.get("sha256")] = prog
        data["by_agent"][rec.get("agent")] += 1
        data["outcomes"][f"{rec.get('agent')}: {_outcome_label(rec.get('outcome'))}"] += 1
        where = f" ({rec['jurisdiction']})" if rec.get("jurisdiction") else ""
        for s in (rec.get("data_policy") or {}).get("in_force", []):
            data["policies"][f"{s['name']} v{s['version']}{where}"] += 1
        for t in rec.get("tables") or []:
            data["tables"][f"{t['name']} v{t['version']}{where}"] += 1
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
    data["pending"] = sorted(p["file"] for p in pending_signoffs(folder, entries) if p["file"] in in_period)
    if store is not None:
        from .policy import audit
        a_ok, a_report, _ = audit(folder, store, today or dt.date.today(), jurisdiction)
        relevant = [line for line in a_report if not line.startswith("  FINDING") or
                    any(f in line for f in in_period)]
        data["audit"] = {"compliant": all(not x.startswith("  FINDING") for x in relevant), "report": relevant}
    data["monitoring"] = _monitor(data["checks"], data["models"], data["model_failures"], data["reads"])
    return data


def pending_signoffs(folder, entries=None, today=None):
    """Decisions still waiting for a person: [{file, agent, roles, requested, age_days, jurisdiction}]."""
    from .ledger import signoff_state
    folder = Path(folder)
    entries = _entries(folder) if entries is None else entries
    today = today or dt.date.today()
    answers = defaultdict(list)
    for e in entries:
        if e.get("kind") == "signoff":
            answers[e.get("file")].append(e)
    out = []
    for e in entries:
        if e.get("kind") != "record" or not (folder / e["file"]).exists():
            continue
        rec = json.loads((folder / e["file"]).read_text())
        req = rec.get("signoff")
        if not (req or {}).get("roles") or signoff_state(req, answers[e["file"]])[0] != "PENDING":
            continue
        asked = dt.date.fromtimestamp(rec.get("ended") or rec.get("started") or e.get("time", 0))
        out.append({"file": e["file"], "agent": rec.get("agent"), "roles": req["roles"], "requested": asked.isoformat(),
                    "age_days": (today - asked).days, "jurisdiction": rec.get("jurisdiction")})
    return out


def _flag(kind, message, **detail):
    return {"kind": kind, "message": message, **detail}


def _monitor(checks, models, failures, reads, from_week=None, baseline_models=None):
    """The label-free flags. from_week: flag only drops in that week or later (earlier weeks are the baseline).
    baseline_models: the model identities seen before the window, so weights that changed at its start show."""
    alerts = []
    rates = {}
    for name, weeks in checks.items():
        total = sum(n for _, n in weeks.values())
        passed = sum(p for p, _ in weeks.values())
        avg = passed / total if total else 1.0
        series = {w: (p / n if n else None) for w, (p, n) in sorted(weeks.items())}
        rates[name] = {"pass_rate": round(avg, 3), "n": total, "weekly": series}
        before_p = before_n = 0
        for w, (p, n) in sorted(weeks.items()):     # each week against the weeks before it
            if before_n >= 3 and n >= 3 and before_p / before_n - p / n > DROP and (from_week is None or w >= from_week):
                alerts.append(_flag("check_drop", f"{name}: pass rate {p / n:.0%} in {w}, down from "
                                                  f"{before_p / before_n:.0%} before it", check=name, week=w,
                                    pass_rate=round(p / n, 3), before=round(before_p / before_n, 3), n=n))
            before_p, before_n = before_p + p, before_n + n
    by_model, before = _digests(models), _digests(baseline_models or {})
    for m, digests in by_model.items():
        if len(digests) > 1:
            alerts.append(_flag("model_weights", f"{m}: served with {len(digests)} different weights in the period "
                                                 f"({', '.join(sorted(digests))})", model=m, digests=sorted(digests)))
        elif before.get(m) and not digests <= before[m]:
            alerts.append(_flag("model_weights", f"{m}: now served with weights {', '.join(sorted(digests))}, "
                                                 f"not {', '.join(sorted(before[m]))} as before", model=m,
                                digests=sorted(digests), before=sorted(before[m])))
    if reads and failures / reads > 0.05:
        alerts.append(_flag("model_failures", f"{failures} of {reads} model calls gave no usable answer "
                                              f"({failures / reads:.0%})", failures=failures, reads=reads))
    return {"rates": rates, "flags": [a["message"] for a in alerts], "alerts": alerts}


def _digests(models):
    """{model: {digest, ...}} from the identity strings collect() counts (replays left out)."""
    out = defaultdict(set)
    for m in models:
        if m.startswith("replay"):
            continue
        parts = m.split(" ")
        out[" ".join(parts[:2])].add(parts[2] if len(parts) > 2 else "")
    return out


def _table(headers, rows):
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    out += ["| " + " | ".join(str(c).replace("|", "\\|") for c in r) + " |" for r in rows]
    return out


def report(data):
    s, e = data["period"]
    integ = data["integrity"]
    lines = [f"# Evidence pack: {s} to {e}" + (f", {data['jurisdiction']}" if data.get("jurisdiction") else ""), "",
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
    if set(data["by_jurisdiction"]) - {"(none)"}:
        lines += _table(["Jurisdiction", "Decisions"], sorted(data["by_jurisdiction"].items())) + [""]
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


def build(folder, out, start, end, store=None, public_key=None, secret=None, signer=None, today=None,
          jurisdiction=None):
    from .explain import explain
    out = Path(out)
    (out / "programs").mkdir(parents=True, exist_ok=True)
    data = collect(folder, start, end, store, public_key, secret, today, jurisdiction)
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


# ---------------- live monitoring ----------------
WINDOW_DAYS = 28      # shio monitor looks at the last four weeks by default...
BASELINE_WEEKS = 8    # ...against the eight weeks before them
PENDING_DAYS = 3      # a sign-off waiting longer than this is flagged


def monitor(folder, since=None, today=None, baseline_weeks=BASELINE_WEEKS, pending_days=PENDING_DAYS,
            jurisdiction=None):
    """The evidence pack's monitoring flags over a rolling window, for alerting while a period runs.

    Window: since (default: the last WINDOW_DAYS days) to today. A check's weekly pass rate in the window is compared
    with the weeks before it, starting `baseline_weeks` before the window; weights are compared with those served in
    the baseline; model failures are counted in the window. Also flags every decision whose sign-off has waited more
    than `pending_days` days. -> a JSON-ready dict: {window, flags, alerts, pending, rates, decisions, ...}."""
    today = today or dt.date.today()
    since = since or today - dt.timedelta(days=WINDOW_DAYS - 1)
    base_start = since - dt.timedelta(weeks=baseline_weeks)
    full = collect(folder, base_start, today, jurisdiction=jurisdiction, integrity=False)
    win = collect(folder, since, today, jurisdiction=jurisdiction, integrity=False)
    mon = _monitor(full["checks"], win["models"], win["model_failures"], win["reads"], from_week=_week(since),
                   baseline_models=full["models"] - win["models"])
    alerts = list(mon["alerts"])
    pending = [p for p in pending_signoffs(folder, today=today)
               if not jurisdiction or p["jurisdiction"] == jurisdiction]
    for p in pending:
        if p["age_days"] > pending_days:
            alerts.append(_flag("pending_signoff", f"{p['file']}: waiting {p['age_days']} days for "
                                                   f"{' and '.join(p['roles'])} sign-off (since {p['requested']})",
                                file=p["file"], roles=p["roles"], age_days=p["age_days"]))
    window_weeks = {w for w in (_week(since + dt.timedelta(days=d)) for d in range((today - since).days + 1))}
    rates = {name: {"pass_rate": r["pass_rate"], "n": r["n"],
                    "weekly": {w: v for w, v in r["weekly"].items() if w in window_weeks}}
             for name, r in mon["rates"].items()}
    return {"source": "shio monitor", "folder": str(folder), "jurisdiction": jurisdiction,
            "window": [since.isoformat(), today.isoformat()], "baseline_from": base_start.isoformat(),
            "generated": dt.datetime.now().isoformat(timespec="seconds"),
            "decisions": win["decisions"], "reads": win["reads"], "model_failures": win["model_failures"],
            "flags": [a["message"] for a in alerts], "alerts": alerts, "pending": pending, "rates": rates}


def post(url, payload, timeout=10):
    """POST the monitor result as JSON to a webhook. -> the HTTP status. Raises OSError (URLError) on failure."""
    import urllib.request
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"webhook must be an http:// or https:// URL, not {url!r}")
    body = json.dumps(payload, default=str).encode()
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "shio-monitor"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status
