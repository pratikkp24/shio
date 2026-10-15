"""Build catalog.json and README.md from every use case: what it is, what each of its cases decided, which Shio
forms it uses, and whether it is one of the curated 100 (curation.json holds the domains and what was left out).

    python examples/usecases/catalog.py            # writes catalog.json and README.md next to this file

Runs every program offline (scripted answers), like run_all.py, so the README always shows what the programs
actually decide.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
STATUS_KEYS = ("action", "status", "decision", "verdict", "result", "route", "release", "merge", "flag")


def header(src):
    lines = []
    for line in src.splitlines():
        if not line.startswith("#"):
            break
        lines.append(line.lstrip("#").strip())
    return " ".join(x for x in lines if x)


def features(src):
    count = lambda pat: len(re.findall(pat, src, re.M))
    return {
        "reads": count(r"^\s*read\s+\w+"),
        "checks": count(r"^\s*check\s+\w+"),
        "whens": count(r"^\s*when\s+\w+"),
        "rules": count(r"^\s*rule\s+\w+"),
        "blind": count(r"^\s*read\s+.*\bblind\s*:"),
        "seeing": count(r"^\s*read\s+.*\bseeing\b"),
        "after": count(r"^\s*(?:check|when)\s+\w+(?:\[[^\]]*\])?\s+after\s"),
        "per_item": count(r"^\s*for\s+(?:\w+\s*,\s*)?\w+\s+in\s+(?:enumerate\()?\w+\.\w+"),
        "keyed": count(r"^\s*(?:read|check|when|rule)\s+\w+\["),
        "visible_text": "visible_text(" in src,
        "steps": re.findall(r'use\("(\w+)"\)', src),
    }


def cases(out):
    """[(title, outcome status, why)] from the program's `show` output."""
    found = []
    for block in re.split(r"\n(?=== )", "\n" + out):
        block = block.strip()
        if not block.startswith("== "):
            continue
        title, *rest = block.split("\n")
        fields = {}
        for line in rest:
            m = re.match(r"\s{3}(\w+): (.*)", line)
            if m:
                fields[m.group(1)] = m.group(2)
        status = next((fields[k] for k in STATUS_KEYS if k in fields), None)
        if status is None and "reply" in fields:
            status = "REPLIED"
        if status is None and "answer" in fields:
            status = "ANSWERED" if not fields["answer"].startswith("I don't know") else "WITHHELD"
        found.append({"title": title[3:].strip(), "outcome": status, "why": fields.get("why", "")[:300]})
    return found


def main():
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    env.pop("SHIO_MODEL", None)
    items = []
    with tempfile.TemporaryDirectory() as traces:
        for f in sorted(HERE.glob("*.shio")):
            src = f.read_text()
            r = subprocess.run([sys.executable, "-m", "shio.cli", "run", str(f), "--traces", traces], cwd=HERE,
                               env=env, capture_output=True, text=True, timeout=120)
            items.append({"name": f.stem, "kind": "workflow" if f.stem.startswith("x_") else "use case",
                          "summary": header(src), "lines": src.count("\n"), "runs": r.returncode == 0,
                          "cases": cases(r.stdout), **features(src)})
    cur = json.loads((HERE / "curation.json").read_text())
    domain_of = {n: d for d, names in cur["domains"].items() for n in names}
    for i in items:
        i["domain"] = domain_of.get(i["name"], "Other")
        i["curated"] = i["name"] not in cur["left_out"]
        if not i["curated"]:
            i["left_out"] = cur["left_out"][i["name"]]
    (HERE / "catalog.json").write_text(json.dumps(items, indent=1, ensure_ascii=False))
    (HERE / "README.md").write_text(readme(items, list(cur["domains"])))
    ok = sum(i["runs"] for i in items)
    print(f"{len(items)} programs, {ok} run, {sum(i['curated'] for i in items)} curated, "
          f"{sum(len(i['cases']) for i in items)} cases -> catalog.json, README.md")


def first_sentences(text, limit=240, at_least=90):
    """Whole sentences up to `limit` characters; a short first sentence always gets the next one (cut with …)."""
    out = ""
    for part in re.split(r"(?<=[.!?])\s+", text):
        if out and len(out) >= at_least and len(out) + len(part) > limit:
            break
        out = f"{out} {part}".strip()
    if len(out) > limit + 120:
        out = out[:limit + 120].rsplit(" ", 1)[0] + "…"
    return out


def cell(text):
    return str(text).replace("|", "\\|").replace("\n", " ")


def readme(items, domains):
    cur = [i for i in items if i["curated"]]
    base = [i for i in cur if i["kind"] == "use case"]
    flows = [i for i in cur if i["kind"] == "workflow"]
    n_cases = sum(len(i["cases"]) for i in cur)
    out = [
        "# 100 Shio use cases",
        "",
        f"{len(base)} business processes and {len(flows)} workflows that chain them, each a runnable `.shio` program "
        f"with {n_cases} cases between them. In every one the model reads, and code decides against something the "
        "model cannot talk its way past: a record, a policy table, arithmetic, a checksum, a date, or a quote that "
        "must appear in a text that code chose. Every program has a clean case and failures of different kinds: the "
        "model misreads, invents, or is manipulated, or the input is simply bad. Shio does not stop a model from "
        "being fooled; it stops a fooled model's output from deciding.",
        "",
        "```bash",
        "python3 examples/usecases/run_all.py                # every program, offline, with scripted model answers",
        "python3 examples/usecases/run_all.py --show refund  # and what each case decided",
        "SHIO_MODEL=qwen3:8b python3 examples/usecases/run_all.py refund   # the same program on a real model (Ollama)",
        "```",
        "",
        "Writing one: [AUTHORING.md](AUTHORING.md). This page is generated by `catalog.py` from the programs' own runs.",
        "",
        "## What the checks rest on",
        "",
        "| Ground truth | Examples |",
        "|---|---|",
        "| A record the model cannot edit | [refund](refund.shio) (order ledger), [order_status_bot](order_status_bot.shio) (replies built only from the order DB), [access_request](access_request.shio) (RBAC table) |",
        "| Arithmetic that must tie out | [three_way_match](three_way_match.shio), [bank_statement_income](bank_statement_income.shio) (running balance re-added), [freight_audit](freight_audit.shio), [lease_abstract](lease_abstract.shio) |",
        "| Checksums and formats | [payroll_change](payroll_change.shio) (IBAN mod-97), [gst_invoice](gst_invoice.shio) (GSTIN check character), [shipment_eta](shipment_eta.shio) (ISO 6346), [prior_auth_packet](prior_auth_packet.shio) (NPI Luhn) |",
        "| A quote that must be in text code chose | [cited_answer](cited_answer.shio), [screening](screening.shio) (hidden text stripped first), [contract](contract.shio), [nda_review](nda_review.shio) |",
        "| Independent reads that must agree | [kyc](kyc.shio) (blind second read; a third read *seeing* both; two of three must agree), [lab_flag](lab_flag.shio), [building_permit](building_permit.shio) |",
        "| Code's own signals the model may raise but never lower | [phishing_triage](phishing_triage.shio), [alert_triage](alert_triage.shio) |",
        "| A check across steps that no single step can make | every `x_` workflow, e.g. [x_procure_to_pay](x_procure_to_pay.shio), [x_release_gate](x_release_gate.shio), [x_bec_defense](x_bec_defense.shio) |",
        "",
    ]
    for d in domains:
        group = [i for i in cur if i["domain"] == d]
        if not group:
            continue
        out += [f"## {d}", ""]
        for kind, title in (("use case", "Use cases"), ("workflow", "Workflows")):
            rows = [i for i in group if i["kind"] == kind]
            if not rows:
                continue
            out += [f"**{title}** ({len(rows)})", "", "| Program | What the model reads, what code decides | Cases |",
                    "|---|---|---|"]
            for i in rows:
                summary = first_sentences(i["summary"])
                if i["steps"]:
                    summary += "<br>Steps: " + ", ".join(f"[{s}]({s}.shio)" for s in dict.fromkeys(i["steps"]))
                cases_md = "<br>".join(f"{cell(c['title'])} → **{cell(c['outcome'] or '-')}**" for c in i["cases"])
                out.append(f"| [{i['name']}]({i['name']}.shio) | {cell(summary)} | {cases_md} |")
            out.append("")
    left = [i for i in items if not i["curated"]]
    out += ["## Not in the 100", "", f"These {len(left)} also run and pass; they were left out of the curated set.", "",
            "| Program | Why |", "|---|---|"]
    out += [f"| [{i['name']}]({i['name']}.shio) | {cell(i['left_out'])} |" for i in left]
    out.append("")
    return "\n".join(out)


if __name__ == "__main__":
    main()
