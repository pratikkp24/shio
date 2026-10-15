"""Composing use cases: a workflow loads other .shio programs from this folder and calls their agents as steps.
A step's runs nest inside the workflow's trace, so one trace shows the whole process."""
from pathlib import Path

import shio

HERE = Path(__file__).resolve().parent
_loaded = {}


def use(name):
    """The compiled module for <name>.shio in this folder (loaded once)."""
    if name not in _loaded:
        _loaded[name] = shio.load(HERE / f"{name}.shio")
    return _loaded[name]


def step_ok(outcome, *good):
    """True when a step's outcome dict says one of the `good` statuses (action/status/decision/verdict…)."""
    if not isinstance(outcome, dict):
        return False
    for k in ("action", "status", "decision", "verdict", "result", "route"):
        if k in outcome:
            return outcome[k] in good
    return False


def why(outcome):
    w = outcome.get("why") if isinstance(outcome, dict) else None
    return w if isinstance(w, list) else ([w] if w else [])
