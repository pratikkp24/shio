"""shio run | check | build | train | show | new"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

from . import __version__, load, trace
from . import train as training
from .compiler import ShioError, compile_source, to_code
from .core import BoundaryError, configure


def _run(a):
    if a.traces:
        configure(a.traces)
    mod = load(a.file, argv=a.args)
    main = getattr(mod, "__shio_main__", None)
    if main is None:
        sys.exit(f"{a.file}: no main: block to run")
    out = main()
    if out is not None:
        print(json.dumps(out, indent=1, default=str) if not isinstance(out, str) else out)


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


def main(argv=None):
    p = argparse.ArgumentParser(prog="shio", description="Shio: the model reads, code decides.")
    p.add_argument("--version", action="version", version=f"shio {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run a program's main: block")
    r.add_argument("file")
    r.add_argument("args", nargs="*")
    r.add_argument("--traces", help="where run traces are written (default ./shio-traces)")
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


if __name__ == "__main__":
    main()
