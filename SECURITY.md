# Security

## What Shio guarantees in v0.1

Shio limits what a model's output can do. It does not stop a model from being fooled.

- **A model has no tools.** A `read` returns data. It cannot call a function, send a message or touch a database.
- **A reading cannot reach plain code.** Inside an agent, a reading's name may appear only in a `check`, a `when`, a
  `rule`, or a later read's `seeing` clause. Anything else (an `if`, an assignment, a function call, a return, a
  prompt) is a compile error.
- **The runtime enforces it again.** A reading is an opaque object: branching on it, comparing it, formatting it,
  converting it or reading its fields outside a check or rule raises `BoundaryError`.
- **Every decision is a rule you can read.** If model output leads to an action, that action is in a `rule`, next
  to the checks it depends on, and the trace records the readings, the check results and the outcome.

## What it does not guarantee

- **A rule can still do the wrong thing.** Shio forces the decision into a rule; it cannot force the rule to check
  the right thing. `rule send: return outbox.send(act.to, act.what)` compiles. Review rules the way you review any
  code that handles untrusted input.
- **A rule's result is trusted.** Whatever a rule returns is ordinary data afterwards. That is the point of a rule,
  and also why rules deserve review.
- **The boundary is checked by name, not by full information-flow analysis.** v0.1 tracks reading names inside an
  agent and blocks the reading object at runtime. It does not yet track data through every container, closure or
  C extension. Full information-flow tracking is on the roadmap (see SPEC.md).
- **Traces contain model inputs and outputs.** Treat `shio-traces/` as sensitive.

## Reporting a vulnerability

If you find a way for a model's output to reach plain code without a check or rule, that is a bug we want to know
about. Please report it privately through GitHub's private vulnerability reporting on this repository rather than in
a public issue, with the smallest `.shio` program that shows it.
