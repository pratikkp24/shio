"""shio run | check | build | train | show"""
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
