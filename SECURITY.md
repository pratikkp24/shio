# Security

## What Shio guarantees in v0.1

Shio limits what a model's output can do. It does not stop a model from being fooled.

- **A model has no tools.** A `read` returns data. It cannot call a function, send a message or touch a database.
- **A reading cannot reach plain code.** Inside an agent, a reading's name may appear only in a `check`, a `when`, a
  `rule`, or a later read's `seeing` clause. Anything else (an `if`, an assignment, a function call, a return, a
  prompt) is a compile error.
- **Routes around the name are refused too.** Code can reach a value without naming it, through `locals()`,
  `vars()`, `eval`, `exec`, stack frames, or a `gc`, `inspect` or `ctypes` memory scan. Those are compile errors in Shio
  code. A `check` or `when` only decides, so it can't assign (`:=`) or call a method that changes something, like
  `.append`.
- **The runtime enforces it again.** A reading is an opaque object: branching on it, comparing it, formatting it,
  converting it, copying or pickling it, or reading its fields outside a check or rule raises `BoundaryError`. What
  the model said isn't stored on the object at all: its value, raw text, request and errors are sealed in a private
  registry. So `reading._value` or `reading.raw` from plain Python is refused as well.
- **Every decision is a rule you can read.** If model output leads to an action, that action is in a `rule`, next
  to the checks it depends on, and the trace records the readings, the check results and the outcome.

## What it does not guarantee

- **A rule can still do the wrong thing.** Shio forces the decision into a rule; it cannot force the rule to check
  the right thing. `rule send: return outbox.send(act.to, act.what)` compiles. Review rules the way you review any
  code that handles untrusted input.
- **A rule's result is trusted.** Whatever a rule returns is ordinary data afterwards. That is the point of a rule,
  and also why rules deserve review.
- **The boundary is checked by name, not by full information-flow analysis.** v0.1 tracks reading names inside an
  agent, refuses the reflective routes above, and blocks the reading object at runtime. It does not yet track data
  through every container, closure or C extension. Full information-flow tracking is on the roadmap (see SPEC.md).
- **Imported Python modules are trusted code.** The checks above apply to `.shio` source. A Python module the program
  imports, or a helper a check calls, receives values and can do anything with them, just like a rule.
  `tests/test_boundary_escapes.py` lists every escape route we know of and what stops it, and
  `tests/test_fuzz_boundary.py` generates thousands more on every CI run, both in `.shio` code and against a live
  reading from Python.
- **Traces contain model inputs and outputs.** Treat `shio-traces/` as sensitive.

## Reporting a vulnerability

If you find a way for a model's output to reach plain code without a check or rule, that is a bug we want to know
about. Please report it privately through GitHub's private vulnerability reporting on this repository rather than in
a public issue, with the smallest `.shio` program that shows it.
