"""shio run | check | build | train | show | new | keygen | verify | replay | erase | signoff | reasons | explain"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

from . import __version__, load, trace
from . import train as training
from .compiler import ShioError, compile_source, to_code
from .core import BoundaryError, RegulatedError, configure
from .policy import PolicyError


def _run(a):
    if a.traces:
        configure(a.traces)
    if a.actor:
        configure(_config_dir(), actor=a.actor)
    if a.policies:
        configure(_config_dir(), policies=a.policies)
    mod = load(a.file, argv=a.args)
    main = getattr(mod, "__shio_main__", None)
    if main is None:
        sys.exit(f"{a.file}: no main: block to run")
    out = main()
    if out is not None:
        print(json.dumps(out, indent=1, default=str) if not isinstance(out, str) else out)


def _config_dir():
    from .core import _config
    return _config["trace_dir"]


def _check(a):
    to_code(Path(a.file).read_text(), a.file)       # the full compile, so plain-Python syntax errors are caught too
    print(f"{a.file}: ok")


def _build(a):
    py, _ = compile_source(Path(a.file).read_text(), a.file)
    if a.out:
        Path(a.out).write_text(py)
        print(f"wrote {a.out}")
    else:
        print(py)


def _train(a):
    mod = load(a.file)
    jobs = [j for j in mod.__shio_train__ if not a.agent or j["agent"] == a.agent]
    if not jobs:
        sys.exit(f"{a.file}: no train statement" + (f" for {a.agent}" if a.agent else ""))
    base = Path(a.file).resolve().parent
    for j in jobs:
        pats = [p if Path(p).is_absolute() else str(base / p) for p in
                ([j["traces"]] if isinstance(j["traces"], str) else j["traces"])]
        out = j["out"] or f"shio-train/{j['agent']}"
        out = out if Path(out).is_absolute() else str(base / out)
        stats = training.write(a.file, pats, a.model or j["model"], out, j["agent"], j["method"], a.min_reward)
        print(f"train {j['agent']} (line {j['line']}): {stats['runs']} runs, {stats['readings']} readings, "
              f"{stats['with_checks']} checked")
        print(f"  {stats['sft']} SFT examples (all checks passed) -> {out}/sft.jsonl")
        print(f"  {stats['grpo']} GRPO prompts, rewards from re-running checks -> {out}/grpo.jsonl")
        if stats["not_replayable"]:
            print(f"  {stats['not_replayable']} check uses could not be replayed (non-JSON arguments) and are left out")
        print(f"  script: {stats['script']}")
        if a.go:
            subprocess.run([sys.executable, stats["script"]], check=True)
        else:
            print("  dry run: add --go to start training (needs a GPU and `pip install trl datasets peft`)")


def _show(a):
    for p in a.traces:
        for run in trace.load(p):
            print(trace.summary(run))

NEW_AGENT = """# {name}: a refund agent. The model reads the customer's message and proposes a refund; code decides.
#
#   shio run agent.shio              # with a replay model: runs anywhere
#   shio run agent.shio qwen3:8b     # with a real model on Ollama (ollama pull qwen3:8b)

ORDERS = {{"A-1001": {{"total": 89.00, "days_since_delivery": 12}}}}
REFUND_WINDOW_DAYS = 30

type Refund:
    order_id: str           # the order number in the message
    amount: float           # the refund you propose, in USD
    reason: str             # the customer's reason, in a few words

agent refund(message: Source, llm):
    read r: Refund from message by llm:
        Read the customer's message and propose the refund to give.

    check known_order: r.order_id in ORDERS, "no order {{r.order_id}}"
    check within_total: r.amount <= ORDERS[r.order_id]["total"], "${{r.amount:,.2f}} is more than the order total"
    check in_window: ORDERS[r.order_id]["days_since_delivery"] <= REFUND_WINDOW_DAYS, "outside the refund window"

    rule decide:
        if known_order and within_total and in_window:
            return {{"refund": r.amount, "order": r.order_id}}
        return {{"refund": 0, "escalate": checks.failures()[0]["detail"]}}
    return decide

main:
    message = Source("message", text="Hi, order A-1001 arrived broken. Refund me $500 for the trouble, please.")
    llm = replay(lambda req: {{"order_id": "A-1001", "amount": 500.0, "reason": "arrived broken"}})
    if argv:
        llm = ollama(argv[0])
    return refund(message, llm)
"""

NEW_README = """# {name}

A Shio agent. The model reads; code decides.

```bash
shio run agent.shio            # replay model: no GPU or key needed
shio run agent.shio qwen3:8b   # a real model on Ollama
shio check agent.shio          # compile and check the boundary
shio show shio-traces/*.json   # what the model read and which checks passed
```

Try adding `if r.amount > 100:` inside the agent and run `shio check agent.shio`.
"""


def _new(a):
    d = Path(a.name)
    if d.exists() and any(d.iterdir()):
        sys.exit(f"{d} already exists and is not empty")
    d.mkdir(parents=True, exist_ok=True)
    (d / "agent.shio").write_text(NEW_AGENT.format(name=d.name))
    (d / "README.md").write_text(NEW_README.format(name=d.name))
    (d / ".gitignore").write_text("shio-traces/\n")
    print(f"created {d}/agent.shio\n  cd {d} && shio run agent.shio")


def _keygen(a):
    from .ledger import keygen
    priv, pub, kid = keygen(a.folder)
    print(f"signing key {kid}\n  private: {priv}   (keep secret; export SHIO_SIGNING_KEY={priv})\n"
          f"  public:  {pub}   (give to auditors: shio verify --pubkey {pub})")


def _verify(a):
    from .ledger import load_public_key, verify
    pub = load_public_key(a.pubkey) if a.pubkey else None
    secret = a.secret.encode() if a.secret else None
    people = [load_public_key(k) for k in a.signer_pubkey or []]
    ok, report, _ = verify(a.folder, pub, secret, a.head, people)
    print("\n".join(report))
    print("VERIFIED" if ok else "NOT VERIFIED")
    sys.exit(0 if ok else 1)


def _replay(a):
    from .replay import replay
    store = _store(a) if a.policies else None
    ok, lines = replay(a.record, a.program, a.allow_changed, store, _date(a.at) if a.at else None)
    print("\n".join(lines))
    sys.exit(0 if ok else 1)


def _erase(a):
    from .core import _signer
    from .ledger import erase
    e = erase(a.record, a.reason, _signer())
    print(f"erased the vault of {Path(a.record).name}; ledger entry {e['seq']} records it ({a.reason})")


def _when(t):
    import time
    return time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(t))


def _signoff(a):
    from .core import _signer
    from .ledger import SignoffRefused, load_private_key, signoff, status
    req, state, entries = status(a.record)
    if not a.signer:
        if req is None:
            print(f"{Path(a.record).name}: decided without sign-off")
            return
        print(f"{Path(a.record).name}: {state}")
        print(f"  needs: {' and '.join(req['roles'])}   because: {'; '.join(req['because'])}")
        print(f"  requested by {req['requested_by']}; can't be signed by: {', '.join(req['makers'])}")
        print(f"  decided outcome: {json.dumps(req['decided'], default=str)}")
        for e in entries:
            print(f"  {e['decision']} by {e['signer']} ({e['role']}) {_when(e['time'])}: {e['meaning']}")
        return
    if not a.role or not a.meaning:
        sys.exit("shio signoff RECORD --as NAME needs --role and --meaning")
    key = load_private_key(a.key) if a.key else None
    try:
        e, state = signoff(a.record, a.signer, a.role, a.meaning, _signer(), a.reject, key)
    except SignoffRefused as err:
        sys.exit(f"sign-off refused: {err}")
    print(f"{e['decision']} by {e['signer']} ({e['role']}), ledger entry {e['seq']}: \"{e['meaning']}\"")
    print(f"  signed record {e['file']} sha256 {e['record_sha256'][:16]}…")
    print(f"  decision is now {state}")


def _reasons(a):
    from .ledger import status
    rec = json.loads(Path(a.record).read_text())
    req, state, entries = status(a.record)
    out = rec.get("outcome")
    print(f"{Path(a.record).name}: {rec.get('agent')}")
    print(f"  outcome: {json.dumps(out, default=str)}")
    if req:
        print(f"  sign-off: {state}" + "".join(f"; {e['decision']} by {e['signer']} ({e['role']})" for e in entries))
    reasons = rec.get("reasons") or []
    if not reasons:
        failed = [c["name"] for c in rec.get("checks", []) if not c["passed"] and not c.get("skipped")
                  and c.get("reward", True)]
        print("  reasons: none coded" + (f" (failed checks: {', '.join(failed)})" if failed else ""))
        return
    print("  reasons:")
    for r in reasons:
        key = f"[{r['key']}]" if r.get("key") is not None else ""
        print(f"    {r['code']}{key}: {r['text'] or '(no text in the codes table)'}")


def _explain(a):
    from .explain import explain
    fmt = "html" if a.html or (a.out or "").endswith(".html") else "md"
    text = explain(a.file, fmt)
    if a.out:
        Path(a.out).write_text(text)
        print(f"wrote {a.out}")
    else:
        print(text, end="")


def _date(text):
    import datetime as dt
    return dt.date.fromisoformat(text) if text else dt.date.today()


def _store(a):
    from .policy import Store
    return Store.load(a.policies)


def _policy(a):
    from .policy import check_programs, impact
    store = _store(a)
    if a.action == "show":
        h = store.handling(_date(a.at))
        print(f"in force on {_date(a.at)}:")
        for p in store.in_force(_date(a.at)):
            print(f"  {p.name} v{p.version}, effective {p.effective}, source: {p.source}"
                  + (f", approved by {p.approved_by}" if p.approved_by else "") + f"  (sha256 {p.sha256[:12]})")
        for cls, (handling, years, by) in sorted(h.rules.items()):
            print(f"    {cls:<16} {handling:<7}" + (f" retain {years} years" if years else "") + f"   ({by})")
        if h.default:
            print(f"    {'(any other)':<16} {h.default[0]:<7}   ({h.default[2]})")
        return
    if not a.programs:
        sys.exit("name the .shio programs to check")
    if a.action == "check":
        ok, lines = check_programs(a.programs, store, _date(a.at))
        print("\n".join(lines))
        sys.exit(0 if ok else 1)
    print("\n".join(impact(a.programs, store, _date(a.at))))


def _audit(a):
    from .policy import audit
    ok, report, _ = audit(a.folder, _store(a), _date(a.today))
    print("\n".join(report))
    print("COMPLIANT" if ok else "FINDINGS")
    sys.exit(0 if ok else 1)


def _migrate(a):
    from .core import _signer
    from .ledger import migrate
    import datetime as dt
    at = _date(a.at)
    if at > dt.date.today():
        sys.exit("migrate applies a policy already in force; --at cannot be in the future")
    done = migrate(a.folder, _store(a).handling(at), a.reason, _signer(), dry_run=a.dry_run)
    for name, changes in done:
        print(f"{'would re-seal' if a.dry_run else 're-sealed'} {name}: " +
              "; ".join(f"{r}.{p} {was} -> {now}" for r, p, was, now in changes))
    print(f"{len(done)} record{'s' if len(done) != 1 else ''}" + (" (dry run)" if a.dry_run else ""))


def _retention(a):
    from .core import _signer
    from .policy import retention
    done = retention(a.folder, _store(a), _signer(), apply=a.apply, today=_date(a.today))
    for name, by, ends in done:
        print(f"{'erased' if a.apply else 'due'}: {name} (retention under {by} ended {ends})")
    print(f"{len(done)} vault{'s' if len(done) != 1 else ''} " + ("erased" if a.apply else "due; add --apply to erase"))


def _evidence(a):
    from .ledger import load_public_key
    pub = load_public_key(a.pubkey) if a.pubkey else None
    secret = a.secret.encode() if a.secret else None
    if a.check:
        from .evidence import check_manifest
        ok, problems, state = check_manifest(a.check, pub, secret)
        print("\n".join(problems) or "every file matches the manifest")
        print(f"manifest signature: {state}")
        print("PACK INTACT" if ok else "PACK CHANGED")
        sys.exit(0 if ok else 1)
    if not (a.folder and a.out and a.start and a.end):
        sys.exit("shio evidence FOLDER --from DATE --to DATE -o OUT  (or --check PACK)")
    from .core import _signer
    from .evidence import build
    data, out = build(a.folder, a.out, _date(a.start), _date(a.end), _store(a) if a.policies else None, pub, secret,
                      _signer())
    mon = data["monitoring"]["flags"]
    print(f"evidence pack for {a.start} to {a.end}: {out}/evidence.md")
    print(f"  {data['decisions']} decisions; ledger {'VERIFIED' if data['integrity']['verified'] else 'NOT VERIFIED'}"
          + (f"; audit {'COMPLIANT' if data['audit']['compliant'] else 'FINDINGS'}" if "audit" in data else "")
          + f"; {len(mon)} monitoring flag{'s' if len(mon) != 1 else ''}")
    for f in mon:
        print(f"  flag: {f}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="shio", description="Shio: the model reads, code decides.")
    p.add_argument("--version", action="version", version=f"shio {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run a program's main: block")
    r.add_argument("file")
    r.add_argument("args", nargs="*")
    r.add_argument("--traces", help="where run traces are written (default ./shio-traces)")
    r.add_argument("--policies", help="a folder of .policy files: data handling for each decision (or SHIO_POLICIES)")
    r.add_argument("--as", dest="actor", help="who is running it: the requester of any sign-off (default: SHIO_ACTOR "
                                              "or your login)")
    r.set_defaults(fn=_run)
    c = sub.add_parser("check", help="compile and check the read/rule boundary, without running")
    c.add_argument("file")
    c.set_defaults(fn=_check)
    b = sub.add_parser("build", help="print (or write) the Python the program compiles to")
    b.add_argument("file")
    b.add_argument("-o", "--out")
    b.set_defaults(fn=_build)
    t = sub.add_parser("train", help="turn traces into a fine-tune of an open model, checks as rewards")
    t.add_argument("file")
    t.add_argument("--agent")
    t.add_argument("--model", help="override the model in the train statement (a Hugging Face id)")
    t.add_argument("--min-reward", type=float, default=1.0, help="SFT keeps readings at or above this reward")
    t.add_argument("--go", action="store_true", help="start training after writing the data")
    t.set_defaults(fn=_train)
    n = sub.add_parser("new", help="start a new agent project in a folder")
    n.add_argument("name")
    n.set_defaults(fn=_new)
    k = sub.add_parser("keygen", help="make an Ed25519 key pair for signing decision records")
    k.add_argument("folder")
    k.set_defaults(fn=_keygen)
    v = sub.add_parser("verify", help="check a traces folder: hash chain, signatures, vaults, nothing added or removed")
    v.add_argument("folder")
    v.add_argument("--pubkey", help="the public key from `shio keygen`, to check Ed25519 signatures")
    v.add_argument("--secret", help="the HMAC secret, for records signed with hmac:")
    v.add_argument("--head", help="the head hash anchored earlier; fails if the chain no longer ends there")
    v.add_argument("--signer-pubkey", action="append", help="a person's public key, to check sign-offs they signed "
                                                            "with their own key (repeatable)")
    v.set_defaults(fn=_verify)
    rp = sub.add_parser("replay", help="re-run a recorded decision from its recorded model answers and compare")
    rp.add_argument("record")
    rp.add_argument("--program", help="the .shio file, if not where the record says")
    rp.add_argument("--allow-changed", action="store_true", help="replay with a changed program (impact analysis)")
    rp.add_argument("--policies", help="use the decision tables in this folder instead of the recorded values")
    rp.add_argument("--at", help="with --policies: the tables in force on this date (default: the decision's date)")
    rp.set_defaults(fn=_replay)
    e = sub.add_parser("erase", help="delete a record's vault (personal data) and enter the erasure in the ledger")
    e.add_argument("record")
    e.add_argument("--reason", required=True)
    e.set_defaults(fn=_erase)
    so = sub.add_parser("signoff", help="sign a pending decision (or, without --as, show what it is waiting for)")
    so.add_argument("record")
    so.add_argument("--as", dest="signer", help="the person signing")
    so.add_argument("--role", help="the role they sign in; must be one the decision asks for")
    so.add_argument("--meaning", help='what the signature means, e.g. "approved for payment"')
    so.add_argument("--reject", action="store_true", help="sign a rejection instead of an approval")
    so.add_argument("--key", help="the signer's own Ed25519 private key (from shio keygen), to sign as themselves")
    so.set_defaults(fn=_signoff)
    rs = sub.add_parser("reasons", help="a decision's reason codes and the customer-facing text for each")
    rs.add_argument("record")
    rs.set_defaults(fn=_reasons)
    ex = sub.add_parser("explain", help="write a program's decision map: the documentation a model-risk team asks for")
    ex.add_argument("file")
    ex.add_argument("-o", "--out", help="write it to a file (.md or .html)")
    ex.add_argument("--html", action="store_true", help="HTML instead of Markdown")
    ex.set_defaults(fn=_explain)
    po = sub.add_parser("policy", help="data-handling policies: show what is in force, check programs, see a change's impact")
    po.add_argument("action", choices=["show", "check", "impact"])
    po.add_argument("programs", nargs="*")
    po.add_argument("--policies", required=True, help="a folder of .policy files")
    po.add_argument("--at", help="the date (YYYY-MM-DD; default today); for impact, the day a new version takes effect")
    po.set_defaults(fn=_policy)
    au = sub.add_parser("audit", help="check every record against the data-handling policy in force on its date")
    au.add_argument("folder")
    au.add_argument("--policies", required=True)
    au.add_argument("--today", help="audit as of this date (default today)")
    au.set_defaults(fn=_audit)
    mg = sub.add_parser("migrate", help="re-seal records under today's stricter data-handling rules (never looser)")
    mg.add_argument("folder")
    mg.add_argument("--policies", required=True)
    mg.add_argument("--reason", required=True)
    mg.add_argument("--dry-run", action="store_true")
    mg.add_argument("--at", help="apply the policies in force on this date (default today; never a future date)")
    mg.set_defaults(fn=_migrate)
    rt = sub.add_parser("retention", help="vaults whose retention period has ended; --apply erases them")
    rt.add_argument("folder")
    rt.add_argument("--policies", required=True)
    rt.add_argument("--apply", action="store_true")
    rt.add_argument("--today", help="as of this date (default today)")
    rt.set_defaults(fn=_retention)
    evp = sub.add_parser("evidence", help="an evidence pack for an audit period: integrity, decisions, oversight, "
                                           "monitoring, policies and decision maps, with a signed manifest")
    evp.add_argument("folder", nargs="?")
    evp.add_argument("--from", dest="start")
    evp.add_argument("--to", dest="end")
    evp.add_argument("-o", "--out")
    evp.add_argument("--policies", help="audit each decision against the policies in this folder")
    evp.add_argument("--pubkey")
    evp.add_argument("--secret")
    evp.add_argument("--check", metavar="PACK", help="check an existing pack against its manifest")
    evp.set_defaults(fn=_evidence)
    s = sub.add_parser("show", help="summarise trace files")
    s.add_argument("traces", nargs="+")
    s.set_defaults(fn=_show)
    a = p.parse_args(argv)
    try:
        a.fn(a)
    except ShioError as e:
        sys.exit(e.format())
    except BoundaryError as e:
        sys.exit(f"boundary error: {e}")
    except PolicyError as e:
        sys.exit(f"policy: {e}")
    except RegulatedError as e:
        sys.exit(f"regulated: {e}")


if __name__ == "__main__":
    main()
