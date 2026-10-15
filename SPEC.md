# Shio, v0.1: design on one page

## The idea

An agent is a program in which some values come from a model. Those values are guesses. Shio makes that a property
of the language rather than a convention:

1. **A model call returns a reading, never a value.** Code cannot branch on it, store it, format it, pass it on or
   return it. The compiler rejects such a use with the `.shio` line, and the runtime rejects it again if Python
   reaches a reading another way.
2. **Only code decides.** A reading is looked at in exactly four places: a `check` (a claim that should hold), a
   `when` (a condition that routes control flow), a `rule` (code that returns an outcome), and a later read's
   `seeing` clause (another model may look at it). An agent that returns a reading is an error.
3. **Every run is a trace.** Each trace records the readings (prompt, raw completion, parsed value), the checks
   (function, arguments and result) and the rules (outcome). It is the audit trail in production.
4. **Checks are rewards.** Re-running an agent's checks on a new completion scores that completion. The code that
   guards production becomes the reward function for training an open model, with no separate eval to write.

## What exists already (October 2026)

| Project | Overlap | What Shio adds |
|---|---|---|
| DSPy 3.4 (Stanford) | Programs over LLM calls. Optimizes prompts, with an experimental `dspy.GRPO` for weights. | A boundary the compiler enforces. Checks from production double as the reward. A language, not a library. |
| Jac / byLLM | A Python superset with `by llm` functions | Nothing stops model output from deciding. No traces, no training. |
| IBM PDL | A declarative prompt language | No read/decide boundary, and no training from production checks. |
| LMQL | Constrained decoding | Shio constrains outputs through types, but its focus is who decides, not how tokens are sampled. |
| TRL, Unsloth, ART | Training loops | Shio writes their data and reward function from traces. It calls TRL rather than replacing it. |

## Semantics worth stating

- **Null, not crash.** A completion that does not fit its type is a reading with value null and an error recorded.
  A check that raises (for example, arithmetic on a null) has failed, with the exception as its detail.
- **Blind reads** see no other reading, which the compiler enforces. `seeing` passes earlier readings' values to
  the model as context. Model-to-model passing is allowed, because a model only ever produces another reading.
- **Iteration scope.** Readings taken inside a loop are reset at the top of each iteration.
- **Keys.** `check agree[row]` records the key, so `checks.failed("agree", row)` and the traces can tell repeated
  checks apart.
- **Compilation.** `.shio` → Python AST, with line numbers rewritten to the `.shio` file, so tracebacks show `.shio`
  lines. Each check and rule becomes a module-level function whose parameters are the agent locals it uses. That is
  what lets training call it again later from the trace alone.
- **Trace files are data.** Schema references in a trace are resolved by import path or a restricted type
  expression, never as arbitrary code.

## Added since the first cut

- `for item in reading.field:` loops over a list a model read; each item is a reading of its own, checked per item.
- `check b after a, c:` runs only when the named checks passed, and is recorded as skipped (not failed) otherwise.
- Undefined names in prompts and checks are compile errors.
- A model call that times out or errors gives a null reading, like a reply that does not fit its type.
- JSON schemas sent to models inline nested types (no `$ref`), which local models' structured output handles better.
- `shio new NAME` starts a project that runs with no model.

## Left out of v0.1, on purpose

- Batching many fields into one model call. The port reads one field per call, where the original batched 36; a
  `read many` form is the next step.
- Multimodal sources. `Source.from_pdf` extracts text; models that take PDFs or images directly are not used that way yet.
- Async agents and streaming.
- Static types for plain values. Shio checks the read/decide boundary, not general types; Python type hints still
  work.
- Its own package manager or runtime. Shio uses Python's.

## Next

1. **Full information-flow tracking.** Track model data through containers, closures and calls, with rules as the
   only place it is approved for use, and fuzz the boundary. Today it is tracked by reading name inside an agent and
   blocked at runtime by the reading object.
2. **Tools as typed effects.** `tool send_email(to: Email)` declarations, so an agent can only call declared tools,
   and only from a rule.
3. **A prompt-injection benchmark.** AgentDojo tasks rewritten in Shio: attack success rate and task success.
4. **A `read many` form** for batched extraction, and native PDF and image sources for models that take them.
5. **A language server**, so errors show as you type.
6. **A `label` command** that lets reviewers mark a trace's outcome right or wrong, so human verdicts become checks
   and rewards too.
7. **One real GRPO run** on a GPU, comparing a small fine-tuned open model with a large hosted one on held-out data,
   with the program's checks as the only reward.

## Evidence so far

- Tests for every boundary error, iteration scope, null readings (including a model that times out), checks that
  raise, reward replay, schemas without `$ref`, and every example program (each incident is acted on by the plain
  agent and held by the Shio one).
- `examples/usecases`: 100 programs (60 business processes, 40 workflows) with 372 cases, all run in the test suite.
- `examples/incidents`: five real incidents. With `qwen3:8b` and `llama3.2:3b` on Ollama, the models were fooled in
  three of five (the $1 Tahoe, uncheckable case citations, the private-repo leak) and the Shio agent held every one;
  in the other two the models answered safely and Shio let the answer through.
- `examples/injection`: with `llama3.2:3b` and `qwen3:8b` on Ollama, the plain Python agent forwards an invoice to
  the address in the injected text; the same agent in Shio does not compile until the decision is a rule, and then
  holds the email for review. A clean email from the same supplier is still sent.
- `examples/sql`: `qwen2.5-coder:7b` answers questions with a single SELECT; a request to delete rows is refused by
  the checks.
- `examples/invoice.shio` runs end to end against qwen3:8b on Ollama, with all three checks passing.
