# Writing a Shio use case

Every file in this folder is one real business process written in Shio, runnable offline. Read
[`refund.shio`](refund.shio), [`kyc.shio`](kyc.shio) and [`cited_answer.shio`](cited_answer.shio) first: they are
the reference style.

## What a use case must show

1. **A real process someone pays people to do today.** A one-line header comment says what the model reads and what
   code decides.
2. **Ground truth that is not the model.** Every decision rests on something deterministic: a record (a dict
   standing in for a database), a policy table, arithmetic (totals tie out), a checksum or format (IBAN, GSTIN,
   ISO 6346, Luhn), dates against `TODAY`, or a quote that must appear in a text that *code* chose. Hidden markup is
   stripped with `visible_text()` before grounding.
3. **At least three cases in `main:`**, each printed with `show(title, outcome)`:
   - one clean case that goes through (paid, approved, sent…);
   - at least two failures of different kinds, chosen from: the model misreads (a wrong digit, a wrong date), the
     model hallucinates (an invented number or citation), the input is manipulated (prompt injection, hidden text,
     a forged document), or the input is genuinely bad (over limit, expired, mismatched).
   The scripted answer for a failure is what a model would plausibly say in that situation, including a fooled
   model obeying an injection.
4. **An outcome a person can act on**: a dict with an `action` or `status` (APPROVE / REVIEW / REJECT / PAY / HOLD /
   HUMAN…) and, when it is not the happy path, `why`: the failed checks' details.

Be honest: Shio does not stop a model from being fooled. It stops a fooled model's output from deciding. Never
write comments or outputs that claim otherwise.

## The language, in short

```shio
from common import model, grounded, visible_text, numbers, money_eq, parse_date, domain, TODAY, show

TABLE = {...}                      # plain Python is fine anywhere

type Thing:
    "What the model is asked to fill in."
    amount: float?                 # trailing comment = field description the model sees; T? = may be null
    kind: "a" | "b" | "c"          # a fixed choice
    quote: str                     # the words it came from, copied exactly
    items: list[Item]              # Item must be a type declared above

agent name(text: str, llm):        # the model is a parameter, so main can pass scripted answers
    seen = visible_text(text)      # plain code may compute from inputs, never from readings
    read r: Thing by llm:          # also: read r[key]: T by llm blind:   /   read r2: T by llm seeing r:
        Prompt text, indented. {text} interpolates a plain value (never a reading).

    check ok: r.amount is not None and r.amount <= TABLE["limit"], "asks {r.amount}, limit {TABLE['limit']}"
    when needs_second: r.kind == "b"     # a condition that only steers control flow (not a training reward)
    if needs_second:
        read r2: Thing by llm blind:
            Independently read ...
    rule outcome:                  # code that decides; must return; sees readings, check booleans, `checks`
        if checks.failures():
            return {"status": "REVIEW", "why": [c["detail"] for c in checks.failures()]}
        return {"status": "APPROVE", "amount": r.amount}
    return outcome

main:
    show("Clean case", name("...", model({"r": {"amount": 10, "kind": "a", "quote": "...", "items": []}})))
```

Rules the compiler enforces (it will tell you the line):

- A reading's name (`r` above) may appear **only** in a `check`, a `when`, a `rule`, or a later read's `seeing`.
  Never in an `if`, an assignment, a `print`, a function call, a `return`, or a prompt's `{...}`.
- `check name: EXPR, "detail"`: the detail is an f-string (it may use readings), shown only when the check fails.
  One line only. A check that raises (say, a null reading) has failed.
- In a loop, key repeated checks: `check paid[i]: ...` and ask `checks.failed("paid", i)` in the rule, or
  `checks.failures(i)` for every failed check with that key (each record is a dict with `name`, `key`, `detail`). Readings
  taken inside a loop are reset to null at the top of each iteration.
- **Per item of a list the model returned:** `for line in inv.lines:` (or `for i, line in enumerate(inv.lines):`)
  gives each item as a reading too, so `check price[i]: line.unit_price == PO[line.sku], "..."` works per line and
  training re-runs it over every item of a new answer. The loop variable is a reading: only checks and rules may
  look at it.
- **One check depending on another:** `check stock[i] after known: ...` is skipped (not failed) when `known` failed
  for the same key, so one unknown value does not set off a pile of misleading failures. Several are allowed:
  `check currency after deal_ok, account_ok: ...`. `checks.failures()` leaves
  skipped checks out; `checks.skipped()` lists them.
- Literal braces in a prompt are `{{` and `}}`. A `{name}` that is not defined anywhere is a compile error, and so
  is an undefined name in a check.
- A model reply that does not fit the type gives a null reading: checks on it fail, and a rule should handle
  `r is None`.
- `rule` bodies cannot contain `read`, `check`, `when` or another `rule`.
- Pass plain values (strings, numbers, dicts) to checks, not objects, so traces can be replayed for training.

## `common.py`

`model(answers)` gives scripted answers by reading name (`{"r": {...}}`). For a keyed read in a loop use
`{"r": {key1: {...}, key2: {...}}}`. A plain-typed read (`read n: float by llm`) takes the bare value. With
`SHIO_MODEL=qwen3:8b` the same program runs on a real model through Ollama.

Helpers: `grounded(quote, text)`, `visible_text(html)`, `numbers(s)`, `money_eq(a, b, tol)`, `parse_date(s)`,
`domain(email_or_url)`, `norm(s)`, `TODAY` (2026-10-16, fixed), `show(title, outcome)`.

**Do not edit `common.py`.** Other people are writing use cases at the same time. Put any helper you need, such as
a checksum, in your own `.shio` file as a plain `def`.

## Checklist before you are done

- [ ] `python3 examples/usecases/run_all.py <name>` prints `ok` (run from the repo root); add `--show` to see
      what each case decided.
- [ ] The output makes sense case by case: the clean case goes through, and each failure is caught **by the check
      you meant**. Read the `why`.
- [ ] Names, companies, IDs and documents are fictional. Format-valid fake IDs are fine (and checksums should
      pass where the case is meant to pass).
- [ ] 50 to 110 lines, with a header comment of 2 to 4 lines.
- [ ] No network, no files outside this folder, and no new dependencies.
- [ ] Medical, legal and financial cases are administrative (completeness, matching, limits, routing) and always
      route judgment calls to a qualified person. Shio flags; it does not diagnose or advise.

## Workflows (files named `x_*.shio`)

A workflow chains two to four use cases into one end-to-end process. See
[`x_hire_onboarding.shio`](x_hire_onboarding.shio).

- Load steps with `from uses import use, step_ok, why` and `kyc = use("kyc")`, then call `kyc.kyc(...)` inside your
  agent. Each step's run nests in the workflow's trace.
- Give each step its own scripted model (`model({...})` with that step's reading names), because steps reuse names
  like `r`.
- A step returns a plain dict, so the workflow can check it directly: `check identity_ok: step_ok(identity, "APPROVE")`.
- **The point of a workflow is a cross-step check that no single step can make**: the person on the ID is the one
  who applied, the invoice's bank account is the one the vendor onboarding approved, the shipment's HS code matches
  the tax invoice's. At least one of the three cases must pass every step and fail only that cross-step check.
- The final rule decides for the whole process (OFFER / HOLD, SHIP / STOP…) and lists the reasons it stopped.
