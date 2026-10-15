"""Runs every incident twice, as plain Python and as Shio, and prints what each one did.

    python run_all.py                 # replay: what the model in each incident said (no GPU, no key)
    python run_all.py llama3.2:3b     # the same, with a real model on Ollama
"""
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
ANSI = re.compile(r"\x1b\[[0-9;]*m")
INCIDENTS = [
    ("dealer", "Chevy dealer bot sells a Tahoe for $1 (2023)"),
    ("airline", "Air Canada bot invents a refund policy (2024)"),
    ("court", "Lawyers file ChatGPT's invented case law (2023)"),
    ("devops", "Replit agent deletes a production database (2025)"),
    ("github", "GitHub MCP agent leaks private repos (2025)"),
]


def last(cmd, timeout=300):
    try:
        out = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return f"(no answer from the model within {timeout} s)"
    lines = [ANSI.sub("", l).strip() for l in (out.stdout + out.stderr).splitlines() if l.strip()]
    return lines[-1] if lines else f"(no output, exit {out.returncode})"


model = sys.argv[1] if len(sys.argv) > 1 else "replay"
shio = str(Path(sys.executable).with_name("shio"))
print(f"model: {model}\n")
for name, title in INCIDENTS:
    print(title, flush=True)
    print("  plain Python  " + last([sys.executable, f"{name}_naive.py", model]), flush=True)
    print("  Shio          " + last([shio, "run", f"{name}.shio", model, "--traces", str(HERE / ".traces")]), flush=True)
    print(flush=True)
