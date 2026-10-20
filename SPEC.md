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
- Regulated mode (docs/REGULATED.md): signed, hash-chained decision records, `pii` fields and an erasable vault,
  pinned models, `replay`.
- `signoff OUTCOME [when COND] by role "R" [and "S"] [maker FIELD]`: an outcome that needs a person. The condition is
  over the decided outcome (its fields by name) and module constants, never a reading; one that can't be evaluated
  needs sign-off. The agent returns `{"status": "PENDING", "signoff": ..., "decided": outcome}`, the request goes
  into the top-level run's record (a wrapping agent can't skip it), and `shio signoff` answers it in the ledger.
- `check NAME as CODE:` and a `codes:` table of customer-facing text. Failed checks' codes are recorded in clear.
- `shio explain` generates a decision map from the program.
- `regulated` programs: signed, hash-chained decision records, `shio verify | replay | erase`, pinned models, `pii`
  fields with an erasable vault, and the boundary's Python escape hatches as compile errors (docs/REGULATED.md).

## Left out of v0.1, on purpose

- Batching many fields into one model call. The port reads one field per call, where the original batched 36; a
  `read many` form is the next step.
- Multimodal sources. `Source.from_pdf` extracts text; models that take PDFs or images directly are not used that way yet.
- Streaming. (Async agents arrived in 0.8.0: `async agent`, and `aread` from Python.)
- Static types for plain values. Shio checks the read/decide boundary, not general types; Python type hints still
  work.
- Its own package manager or runtime. Shio uses Python's.

## Next

1. **Full information-flow tracking.** Track model data through containers, closures and calls, with rules as the
   only place it is approved for use. Today it is tracked by reading name inside an agent, reflection is refused, the
   reading's contents are sealed at runtime, and the boundary is fuzzed in CI (0.3.0).
2. ~~Tools as typed effects.~~ Done in 0.7.0 (below). Shio Guard (0.3.0) is the library form.
3. ~~A prompt-injection benchmark.~~ Done: AgentDojo v1.2.2 on Opus 5.5, all 949 attack cases (README, "Benchmark").
4. **A `read many` form** for batched extraction, and native PDF and image sources for models that take them.
5. **A language server**, so errors show as you type.
6. ~~A `label` command.~~ Done in 0.8.0: `shio label RECORD --right|--wrong --by NAME`, entered in the chain;
   `shio train` leaves decisions labelled wrong out of the fine-tune set.
7. **One real GRPO run** on a GPU, comparing a small fine-tuned open model with a large hosted one on held-out data,
   with the program's checks as the only reward.

## Tools as typed effects (0.7.0)

Shio Guard gives a Python agent this protection as a library. In the language it should be a declaration, so the
compiler can refuse a call that isn't declared, isn't made from a rule, or passes a value of the wrong kind.

```shio
tool read_file(path: name) reads untrusted = files.read
tool vendor_master() reads trusted = erp.vendors
tool get_webpage(url: url) reads untrusted opens = web.get
tool send_money(recipient: iban, amount: float, memo: text) acts = bank.send
tool send_email(to: email, body: text) acts = mail.send

agent pay(request: Source, llm, approver):
    guard request by llm approved by approver      # the blind plan, from the request alone (as shio.guard)
    read bill: Bill from call read_file(path=request.file) by llm
    check on_record: bill.iban in vendor_master().ibans, "{bill.iban} is not the vendor's account on record"
    rule pay:
        if checks.failures():
            return hold(bill)
        return call send_money(recipient=bill.iban, amount=bill.total, memo=bill.number)
    return pay
```

- **Declarations.** `tool NAME(ARG: KIND, ...) reads [trusted|untrusted|mixed] [opens] | acts = python.callable`.
  Kinds are `email`, `url`, `iban`, `name` (control values, provenance-checked), `text` (free text: addresses inside
  it are checked) and plain types (`float`, `int`, `bool`, `str`: not checked).
- **Calls.** `call NAME(args)` is an expression. A `reads` tool may be called anywhere; its result is untrusted or
  trusted text as declared, and is recorded in the task's ledger. An `acts` tool may be called **only inside a
  rule**; anywhere else is a compile error, as is calling an undeclared tool or a tool's Python callable directly.
- **Every `acts` call goes through the gate:** planned by the blind read of the request, every control value from
  trusted text or a place the request pointed at, no address the agent never read. It runs; or it is held, and the
  `approved by` approver is asked, and if the answer isn't yes the call raises `GateHeld`; or it is denied and raises
  `GateDenied`. A rule catches either to choose the outcome (`except GateDenied as d: return ...`); uncaught, the
  agent's run fails without acting. Calling an `acts` tool from plain Python, outside a rule or without `guard`, is
  refused at run time too (`BoundaryError`).
- **`guard`** belongs to the agent that runs it: its rules, and agents it calls, use that task. An agent without
  `guard` may still call `reads` tools.
- **Traces.** The plan, every gated call and every approval are records in the same chain as the agent's run.
- **Compatibility.** Programs without `tool` declarations compile as today. `shio.guard` stays the way to protect
  agents written in Python.

Still open: how a rule that calls several `acts` tools reports a partial hold (today the first hold or denial
raises, and earlier calls in the rule have run); and whether reads should be cached per task. Example:
[`examples/tools/mail.shio`](examples/tools/mail.shio).

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
