# AGENTS.md

Guidance for AI coding agents (and people) working in this repository. Shio is a language for agents in which the
model reads, code decides and people approve; it compiles `.shio` files to Python.

## Setup and checks

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[regulated,pdf]" pytest
pytest -q                                            # language, runtime, guard, boundary fuzzing (about a minute)
SHIO_FUZZ_N=5000 pytest -q tests/test_fuzz_boundary.py   # a longer fuzz run before touching the boundary
for f in examples/*.shio examples/*/*.shio; do case "$f" in *_unsafe.shio) ;; *) shio check "$f";; esac; done
cd editors/vscode && npm install && npm test         # the VS Code extension
```

CI runs all of this on Python 3.10, 3.12 and 3.13, plus a plain `pip install shio-lang` (the extra-only tests skip).

## Where things are

| Path | What it is |
|---|---|
| `shio/compiler.py` | `.shio` -> Python. The boundary's compile-time half: reading names, reflection, pure checks |
| `shio/core.py` | The runtime: readings (sealed), checks, rules, agents, traces, sign-off |
| `shio/guard.py`, `shio/guard.shio` | Shio Guard: plan, provenance ledger and gate for any agent's tool calls |
| `shio/models.py` | Model backends (Ollama, OpenAI-compatible, Anthropic, replay), retries and pacing |
| `shio/ledger.py`, `shio/policy.py` | Regulated mode: signed hash-chained records, the vault, policy tables |
| `tests/test_boundary_escapes.py`, `tests/test_fuzz_boundary.py` | Every known escape route, and generated ones |
| `SPEC.md`, `SECURITY.md` | The design, and what Shio does and does not guarantee |

## Rules

- **Keep the boundary sound.** A model's reading may be looked at only in a `check`, a `when`, a `rule` or a later
  read's `seeing` clause. Any change that lets a reading's value reach plain code without one of those is a bug.
  Add a test to `tests/test_boundary_escapes.py` for every route you close, and never weaken an existing one.
- **Checks only decide.** A `check` or `when` must not assign or mutate. Actions belong in rules.
- **Fail safe.** A model that fails, times out or answers badly gives a null reading, which fails its checks. Never
  turn a model failure into a crash or into a pass.
- **Errors point at the `.shio` line** and say what to write instead.
- **No secrets in traces.** API keys come from the environment and never appear in a trace (there is a test).
- **No client names or data** in examples, tests or docs. Use fictional companies.
- **Numbers in docs come from runs.** Benchmark figures in the README must match a results file; state the setup and
  the limits next to them.
- **New syntax goes in SPEC.md first**, with the problem it solves. Record user-visible changes in CHANGELOG.md.
- Commit messages say what changed and why. Don't push or release without the maintainer's go-ahead.
