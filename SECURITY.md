# Security

## What Shio guarantees

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
- **The boundary is checked by name, not by full information-flow analysis.** Shio tracks reading names inside an
  agent, refuses the reflective routes above, and blocks the reading object at runtime. It does not yet track data
  through every container, closure or C extension. Full information-flow tracking is on the roadmap (see SPEC.md).
- **Imported Python modules are trusted code.** The checks above apply to `.shio` source. A Python module the program
  imports, or a helper a check calls, receives values and can do anything with them, just like a rule.
  `tests/test_boundary_escapes.py` lists the escape routes we know of and what stops each, and
  `tests/test_fuzz_boundary.py` runs generated programs and expressions on every CI run, both in `.shio` code and
  against a live reading from Python. The fuzz covers a fixed grammar of forms (672 distinct `.shio` programs); it is a
  regression net, not proof that no other route exists.
- **Traces contain model inputs and outputs.** Treat `shio-traces/` as sensitive.

[docs/THREAT-MODEL.md](docs/THREAT-MODEL.md) states the attackers, trust boundaries and the mechanism and test behind
each guarantee, and lists the known limits, including issues found while preparing the outside review that are not
fixed in 1.0.0rc1. [docs/REVIEW-BRIEF.md](docs/REVIEW-BRIEF.md) is the brief for reviewers.

## Supported versions

Fixes land on `main` and ship in the next release. The latest release is 1.0.0rc1 (`pip install --pre shio-lang`).

## Reporting a vulnerability

If you find a way for a model's output to reach plain code without a check or rule, to run an acting tool without the
gate, or to change a decision record without `shio verify` noticing, that is a bug we want to know about. Please
report it privately, not in a public issue:

- GitHub private vulnerability reporting: https://github.com/pratikkp24/shio/security/advisories/new
- If that page is not available to you, open an issue titled "Security contact request" with no details, and we will
  reply with a private channel.

Include the smallest `.shio` program or Python snippet that shows it, the Shio version and the Python version.
