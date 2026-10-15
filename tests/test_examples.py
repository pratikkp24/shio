"""The example programs, run as a user would: every incident is acted on by the plain Python agent and held by the
Shio one (replay model, so no GPU or key), and the inbox example holds the injected email."""
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
INCIDENTS = ROOT / "examples" / "incidents"
ANSI = re.compile(r"\x1b\[[0-9;]*m")
SHIO = [sys.executable, "-m", "shio.cli"]


def run(cmd, cwd, tmp_path):
    out = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=120,
                         env={"PYTHONPATH": str(ROOT), "PATH": "/usr/bin:/bin"})
    assert out.returncode == 0, out.stderr
    return ANSI.sub("", out.stdout)


@pytest.mark.parametrize("name", ["dealer", "airline", "court", "devops", "github"])
def test_incident_naive_acts_and_shio_holds(name, tmp_path):
    naive = run([sys.executable, f"{name}_naive.py", "replay"], INCIDENTS, tmp_path)
    assert "DONE" in naive
    held = run(SHIO + ["run", f"{name}.shio", "replay", "--traces", str(tmp_path)], INCIDENTS, tmp_path)
    assert "HELD" in held and "OK" not in held


def test_inbox_holds_the_injected_email_and_sends_the_clean_one(tmp_path):
    cwd = ROOT / "examples" / "injection"
    assert "HELD" in run(SHIO + ["run", "inbox.shio", "replay", "--traces", str(tmp_path)], cwd, tmp_path)


@pytest.mark.parametrize("path", sorted(p for p in (ROOT / "examples").rglob("*.shio") if not p.stem.endswith("_unsafe")))
def test_every_example_compiles(path):
    out = subprocess.run(SHIO + ["check", str(path)], capture_output=True, text=True, env={"PYTHONPATH": str(ROOT)})
    assert out.returncode == 0, out.stderr


@pytest.mark.parametrize("path", sorted((ROOT / "examples").rglob("*_unsafe.shio")))
def test_unsafe_examples_do_not_compile(path):
    out = subprocess.run(SHIO + ["check", str(path)], capture_output=True, text=True, env={"PYTHONPATH": str(ROOT)})
    assert out.returncode != 0 and "model reading" in out.stderr


def test_new_project_runs_and_holds(tmp_path):
    run(SHIO + ["new", "starter"], tmp_path, tmp_path)
    out = run(SHIO + ["run", "agent.shio", "--traces", str(tmp_path / "t")], tmp_path / "starter", tmp_path)
    assert '"refund": 0' in out and "more than the order total" in out
