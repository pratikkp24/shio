# Contributing

Shio is young, and the most useful contributions right now are:

- **Programs that break the boundary.** A `.shio` file where model output reaches plain code without a check or
  rule. See SECURITY.md for how to report one.
- **Examples.** A real task in under 40 lines, with checks that mean something and a `replay` model so it runs
  without a GPU or an API key.
- **Model backends and editor support.**

## Setup

```bash
git clone <this repo> && cd shio
python -m venv .venv && . .venv/bin/activate
pip install -e . pytest
pytest -q                                  # the language and runtime
cd editors/vscode && npm install && npm test   # the VS Code extension
```

## Ground rules

- Keep the boundary sound: a change that lets a reading reach plain code needs a very good reason and a test.
- Every compiler error points at a `.shio` line and says what to write instead.
- New syntax goes in SPEC.md first, with the problem it solves.
