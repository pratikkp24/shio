"""Run every use case offline (scripted model answers) and report which pass.

    python examples/usecases/run_all.py                 # all .shio files here
    python examples/usecases/run_all.py refund kyc      # some of them
    python examples/usecases/run_all.py --show refund   # and print what each case decided

A use case passes when `shio check` accepts it, `shio run` exits cleanly, and its main prints at least three cases
("== title" sections). No model is called unless SHIO_MODEL is set.
"""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent


def run(path, traces, show=False):
    env = {**os.environ, "PYTHONPATH": str(ROOT) + os.pathsep + os.environ.get("PYTHONPATH", "")}
    t = time.time()
    chk = subprocess.run([sys.executable, "-m", "shio.cli", "check", str(path)], cwd=path.parent, env=env,
                         capture_output=True, text=True, timeout=60)
    if chk.returncode:
        return False, (chk.stderr or chk.stdout).strip().splitlines()[-2:], time.time() - t
    limit = int(os.getenv("SHIO_TIMEOUT", 3600 if os.getenv("SHIO_MODEL") else 60))   # a real model is slow
    try:
        r = subprocess.run([sys.executable, "-m", "shio.cli", "run", str(path), "--traces", traces], cwd=path.parent,
                           env=env, capture_output=True, text=True, timeout=limit)
    except subprocess.TimeoutExpired:
        return False, [f"timed out after {limit}s (set SHIO_TIMEOUT)"], time.time() - t
    out = r.stdout + r.stderr
    if show:
        print(f"---- {path.stem}\n{out.rstrip()}\n", flush=True)
    cases = out.count("\n== ") + out.startswith("== ")
    if r.returncode:
        return False, out.strip().splitlines()[-3:], time.time() - t
    if cases < 3:
        return False, [f"only {cases} case(s) printed; a use case shows at least three"], time.time() - t
    return True, [f"{cases} cases"], time.time() - t


def main(names):
    show = "--show" in names            # also print each use case's output, to read every case's `why`
    names = [n for n in names if n != "--show"]
    files = [HERE / (n if n.endswith(".shio") else n + ".shio") for n in names] or sorted(HERE.glob("*.shio"))
    failed = 0
    with tempfile.TemporaryDirectory() as traces:
        for f in files:
            ok, note, secs = run(f, traces, show)
            failed += not ok
            print(f"{'ok  ' if ok else 'FAIL'} {f.stem:<28} {secs:5.1f}s  {' | '.join(note)}", flush=True)
    print(f"\n{len(files) - failed} of {len(files)} use cases pass")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
