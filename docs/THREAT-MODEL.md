# Shio threat model

For Shio 1.0.0rc1 (commit `d00dc3a`), written 20 October 2026 for an outside security review. It describes what Shio
tries to protect, from whom, where the trust boundaries are, which mechanism enforces each guarantee, which test covers
it, and where each guarantee stops. Everything here was checked against the code. Where the code does less than the
docs suggest, this document says so; those findings are numbered (B1, L1, G1...) and listed with reproductions in
[REVIEW-BRIEF.md](REVIEW-BRIEF.md).

## 1. What Shio claims

Shio is a language that compiles to Python (`shio/compiler.py`) plus a runtime (`shio/core.py`). It makes three
claims. The rest of this document tests each one.

1. **The boundary.** In a `.shio` program, a value a model proposed (a *reading*) can affect what the program does
   only through a `check` or `when` (which reduce it to a yes or no) or a `rule` (written, recorded code). It cannot
   reach plain code: a branch, an assignment, a return, a prompt, or a call.
2. **The gate.** A tool that acts on the world (`acts`, or `side_effect=True` in Shio Guard) runs only if a plan
   made from the user's request alone calls for it, and only if each value that picks its target (a recipient, a URL,
   an IBAN) came from trusted text or from a place the request pointed at. Otherwise the call is held for a person or
   denied.
3. **The record.** Every top-level run is written as a record in a hash-chained ledger, optionally signed. Editing,
   removing, inserting or reordering a record is detected by `shio verify`. Sign-offs, labels, erasures and
   migrations are entries in the same chain.

## 2. Assets

| Asset | Why it matters | Where it lives |
|---|---|---|
| **Control flow and plain-code state of a `.shio` program** | This is what model output must not reach except through a check or rule. If it can, an injection can steer the program without a reviewable decision point. | The compiled module's agent bodies, `main:`, top-level code and module globals |
| **Acting tools** | Sending money or email, writing data. One wrong call is the harm. | `tool ... acts = impl` declarations; `guard.Tool(side_effect=True)` |
| **Sealed reading contents** | The parsed value, raw completion, request and parse error of each reading. Plain code must not read them. | `core._SEALED`, keyed by `id(reading)` (`shio/core.py:53`) |
| **Signed records and the ledger** | The evidence an auditor relies on: what was read, checked, decided and signed off. | `<trace_dir>/*.json`, `<trace_dir>/ledger.jsonl` |
| **The vault** | Free text and personal data split out of a record so it can be erased; optionally AES-256-GCM encrypted. | `<trace_dir>/vault/*.vault.json` or `*.vault.enc` |
| **Keys** | The signing key (Ed25519 or HMAC secret), the vault key, approvers' personal keys. | `SHIO_SIGNING_KEY`, `SHIO_VAULT_KEY`, files from `shio keygen` / `shio vaultkey` |
| **Sign-off and approval decisions** | Who let a held outcome or tool call through. | Ledger `signoff` entries; Guard `approval` runs |

## 3. Attackers

| | Attacker | Capability | In scope? |
|---|---|---|---|
| A1 | **Injected content** | Writes text the model reads: an email, a web page, a file, a tool result, a document source. | **Yes.** The main attacker. |
| A2 | **A malicious model output** | Returns any string as a completion, including one that parses into the declared type with hostile values. Covers a fooled model, a compromised provider and a model that is simply wrong. | **Yes.** Equivalent to A1 from Shio's point of view. |
| A3 | **A curious or careless program author** | Writes the `.shio` program. | **Partly.** Not an adversary: the author can import any Python and do anything. The compiler's job is to stop an honest author from letting a reading reach plain code *by mistake*, and, in `regulated` mode, to make a `.shio` file reviewable: a reviewer reading it should be able to trust that model output reaches decisions only through checks and rules, unless the program uses imported Python. Deliberate evasion through imported Python is out of scope; evasion written in `.shio` source is in scope in regulated mode and is treated as a bug (findings B2, B3). |
| A4 | **Trace-folder writer without keys** | Can read and write `<trace_dir>` (a shared disk, a backup, an S3 copy) but holds no signing key. | **Yes**, for the ledger. Findings L1 and L2 are against this attacker. |
| A5 | **Signing-key holder** | Can sign anything. | **Only through an anchored head.** With the key, history can be rewritten; `shio verify --head` detects it if the head hash was anchored outside the folder first. |
| A6 | **An approver or requester** | Approves held calls or signs off pending decisions. | **Yes**, for maker-checker / four eyes as configured. Identities are names the operator supplies, not authenticated principals (§7). |
| A7 | **The operator / host** | Runs the process, sets environment variables, holds the keys. | **No.** Fully trusted. |

## 4. Trust boundaries

```
 untrusted                                 │ trusted
───────────────────────────────────────────┼──────────────────────────────────────────────────────
 sources, tool outputs, web pages ──► model│
 model completion ──► schema.parse ──► Reading (sealed) ──► check / when ──► bool ──► plain code
                                           │                rule ──► return value ──► plain code
                                           │ .shio source (compiler-checked)
                                           │ imported Python modules, helpers (NOT checked)
                                           │ model adapters (shio/models.py and your own)
                                           │ trace sinks (SQL, S3, OTel), approver callbacks
                                           │ trace folder ◄── only as trusted as its anchored head
```

- **`.shio` code vs imported Python.** The compiler checks `.shio` source only. A module the program imports, and a
  function defined in plain Python at the top of a `.shio` file, are trusted code: if a check or rule passes them a
  value, they can do anything with it. A function, lambda or method alias defined in the `.shio` file itself is
  Shio code: a check that uses one has it checked for purity too (finding B2, fixed).
  In regulated mode the hashes of imported local modules are recorded with each decision.
- **Rules are trusted code.** A rule receives readings' values and the check results. Whatever it returns is ordinary
  data afterwards. Shio forces the decision into a rule; it cannot make the rule check the right thing.
- **Checks are trusted to be pure, and the compiler tries to enforce it.** A check sees the values; it is meant to
  return only a yes or no. Purity is enforced by a deny-list (§5, G3), so it is a guard rail, not a proof.
- **Model adapters** (`shio/models.py`: Ollama, OpenAI-compatible, Claude, replay) are trusted. They see the full
  request. Their output is untrusted and passes through `schema.parse` (pydantic validation,
  `shio/schema.py:97`); a completion that does not parse is a null reading, never an exception.
- **The `seeing` clause** passes earlier readings to a later model as context. That is a model-to-model flow and is
  allowed by design: an injection that fools the first read can influence the second. Checks still decide.
- **Trace sinks** receive a copy of each record after it is written and chained. They are trusted code. When the
  vault is off (the default outside regulated mode), the copy includes raw completions and values. The SQL sink uses
  parameterised queries and a validated table name (`shio/trace.py:125`).
- **Approvers** (`approved by` in `.shio`, `Guard(approver=...)` in Python) are trusted callbacks. The name an approver
  returns is recorded as given.
- **Untrusted tool output that a model does not read is not a reading.** A `reads` tool's result is ordinary data in
  plain code. The boundary is about model output, not about all untrusted input; Guard's provenance checks cover what
  such text can make an acting tool do.

## 5. Guarantees, mechanisms and tests

Each row is a guarantee as the code enforces it today, with its limit. "Tests" are files under `tests/`.

### G1. A reading's name cannot appear in plain code (compile time)

- **Mechanism.** `Compiler.scan` (`shio/compiler.py:312`) tokenises every plain-code line inside an agent and refuses
  any `NAME` token that is a reading in that agent, except as an attribute name (`.x`), a keyword argument name
  (`f(x=...)`), or a name bound by a comprehension or lambda on the same line. Checks, whens, rules and `seeing` are
  compiled separately. Prompts, read sources, keys and guard statements are scanned too. Sign-off conditions may not
  name a reading (`compiler.py:844`).
- **Tests.** `test_boundary_escapes.py`, `test_shio.py`, `test_language_more.py`; `test_fuzz_boundary.py::Fuzz`.
- **Limit.** It is a name check, not information-flow analysis. A comprehension or lambda that rebinds the reading's
  name hides every use of that name on the line (`y = [x for x in [0]] + [x]` compiles). The runtime (G2) still holds
  in that case: plain code gets the sealed `Reading` object, not its value (probes P7, P8).

### G2. A reading is opaque at run time outside a check or rule

- **Mechanism.** `Reading` (`shio/core.py:61`) stores no model content. The value, raw text, request and error are in
  a module-level registry `_SEALED[id(reading)]` (`core.py:73`), removed by `weakref.finalize` when the reading is
  collected. `.value`, `.raw`, `.request`, `.error` go through `_gate` (`core.py:78`), which raises `BoundaryError`
  unless the context variable `_open` is true. `_open` is set only while a `@check` or `@rule` body runs
  (`core.py:415`, `:425`, `:482`). Branching, comparing, converting, iterating, formatting, copying and pickling raise
  `BoundaryError`. An agent or rule that returns a reading, or a container holding one, raises (`core.py:487`, `:713`).
- **Tests.** `test_boundary_escapes.py::SealedReading`; `test_fuzz_boundary.py::FuzzPythonAPI` (every fuzz
  expression and pairs of them against a live reading).
- **Limits.** Python is not a sandbox: from Python, `shio.core._sealed(r)` or `shio.core._open.set(True)` opens
  everything; that is out of scope. From `.shio` code the same internals are refused: every private name the shio
  package defines, as an attribute or an import, and `getattr` in `.shio` code is `core.checked_getattr`, which
  refuses computed private names at run time (finding B1, fixed in e23ce33). `_open` means "inside a check *or* a
  rule", so anything a check calls can read values; helpers defined in the `.shio` file are checked for purity (B2),
  imported Python is trusted. `_SEALED` is keyed by `id()`; the finalizer runs before
  the id can be reused in CPython, which is assumed rather than tested.

### G3. A check or `when` only decides

- **Mechanism.** `Compiler.pure` (`compiler.py:336`) parses the check expression (and its failure message) and refuses
  `:=`, `setattr`/`delattr`, and any call to a method whose name is in `MUTATORS` (`compiler.py:332`: `append`,
  `update`, `write`, `send`, `__setitem__`...). The compiled check returns `bool(expr)`. A failure message is
  evaluated only when the check fails, also with `_open` set.
- **Tests.** `test_boundary_escapes.py::ChecksOnlyDecide`; `test_fuzz_boundary.py::Fuzz` (the `SIDE` forms).
- **Since e23ce33 (finding B2, probes P1-P3).** A check may not take a mutating method at all (called or passed
  as a value), or any dunder attribute; `getattr` with a literal mutating name is refused at compile time and with a
  computed one at run time (`core.checked_getattr` knows it is inside a check). Functions, lambdas, class methods and
  method aliases (`push = LEAK.append`, `partial(...)`) defined in the same `.shio` file are checked when a check
  uses them, in every mode: no `global`/`nonlocal`, no mutating method or assignment through anything but a
  container the function built itself (`out = []`).
- **Limit.** Still a deny-list of names. Imported Python is trusted and not looked into, and a library function that
  changes its argument and is not in the list (`heapq.heappush` and `bisect.insort` are) carries a value out. File
  writes through `print(..., file=...)` are not checked.

### G4. Reflection is refused in `.shio` code

- **Mechanism.** `_reflection_scan` (`compiler.py:935`) runs on every program: names `locals`, `vars`, `globals`,
  `eval`, `exec`, `compile`, `__import__`, `breakpoint`; attributes such as `_getframe`, `f_locals`, `__globals__`,
  `__dict__`, `__closure__`, `_sealed`, `_SEALED`, `_gate`; imports of `gc`, `inspect`, `ctypes`.
  `_regulated_scan` (`compiler.py:968`) replaces it in regulated programs and is much wider: also `getattr`, `setattr`,
  `delattr`, `memoryview`, every dunder name, every attribute starting with `_`, and imports of `shio`, `importlib`,
  `sys`, `operator`, `pickle`, `marshal`, `copyreg`, `types`, `builtins` and others.
- **Tests.** `test_boundary_escapes.py::Reflection` (7 builtins, 3 frame/internal routes, 3 modules);
  `test_regulated.py::Regulated` (6 escape hatches, 4 module imports, a private attribute in a helper).
- **Since e23ce33.** Outside regulated mode the scan also refuses every private name the shio package defines
  (`_open`, `_run`, `_current`...) as an attribute or a `from shio... import`, `last_run`, `__getattribute__`,
  `__builtins__`, `attrgetter`, `methodcaller`, `import builtins`, and rebinding `getattr`; `getattr` itself is
  `core.checked_getattr`, which refuses a computed private or dunder name at run time (finding B1, P5, P6). In
  regulated mode the program's own text may not contain a `__shio` identifier (NFKC-normalised, as Python reads it),
  so the dunder exemption covers only what the compiler emits (finding B3, P9).
- **Limits.** Outside regulated mode the lists remain attribute-name based and Python is not a sandbox (for example,
  `().__class__.__base__.__subclasses__()` is not refused). Reading the program's own trace file with `open()` works
  in every mode (P10); see §7.

### G5. Acting tools run only inside a rule, and only through the gate

- **Mechanism.** At compile time, `call NAME(...)` of an `acts` tool outside a rule is an error (`compiler.py:451`).
  At run time, `tool_call` (`shio/guard.py:700`) refuses an acting tool unless `_open` is set and the agent (or an
  ancestor) started a guarded task with `guard REQUEST by MODEL`; then `Session.decide` must return `run`. Held calls
  raise `GateHeld`, denied ones `GateDenied`; neither runs the tool. An undeclared tool counts as acting
  (`guard.py:235`).
- **Tests.** `test_typed_tools.py` (acts outside a rule, in a check, from plain Python at run time, injected recipient,
  approver), `test_guard.py`, `test_guard_adapters.py`.
- **Limit.** The run-time test is `_open`, which is also true inside a check, so a helper called from a check can call
  an acting tool. The gate still applies to it.

### G6. The gate: plan plus provenance, with run, hold or deny

- **Mechanism.** `plan_actions` in `shio/guard.shio` is a blind read of the user's request alone (no tool output, no
  other readings), settled by a rule; an unreadable plan becomes "delegated", so every action is held. `gate` makes
  each proposed call a reading and decides with five checks: `known`, `intended` (in the plan), `clean_values` (no
  control value found only in untrusted text), `grounded` (no address the agent never read), `unambiguous`.
  Provenance is by value, in `Ledger` (`guard.py:208`): a value is *tainted* if its normalised form occurs in
  untrusted text and not in trusted text (`guard.py:297`); text longer than 120 characters in a trusted record counts
  as untrusted. A value from a place the request pointed at is endorsed only if it matches the request's own word
  for it (`_endorse`).
- **Tests.** `test_guard.py` (17 cases: unplanned action, injected recipient, built address, tampered bill, long
  text in a trusted record...), `test_guard_adapters.py`, `test_typed_tools.py`. External evidence: the AgentDojo run
  in the README (Opus 5.5 refused every tool-call attack on its own, so that run did not test the gate).
- **Since 07d547d.** A value is grounded only where trusted text holds it as a whole token or address (`within`,
  `guard.py`): `bob@acme.co` is not grounded by `bob@acme.com` (finding G1). Being seen in untrusted text stays a
  substring test, the cautious direction. A digits-only value is judged when an `iban`/account argument wants it
  (G2). On shio-bench's recorded AgentDojo run the gate reaches the same verdict on all 3,058 calls as before, and the
  offline obedient attacker still gets 0 of 249 through, with the benign tasks unchanged.
- **Limits.** Values of fewer than three characters, and numbers in non-account arguments, are not judged. Amounts and other non-control arguments are not checked at all. The plan
  is a model reading of the request: a request that is itself hostile is out of scope (the request is trusted).

### G7. Approvals and four eyes (Guard)

- **Mechanism.** A held call gets an id bound to `sha256([tool, args])`. `Session.approve(id, by)` re-runs the gate
  (an approved call that is now denied stays denied) and records the verdict as a Shio run in the chain. With
  `four_eyes=True` (the default) the requester cannot approve. Since 07d547d: the requester defaults to the actor
  (`shio.configure(actor=)`, `SHIO_ACTOR`, the OS user), and `guard ... approved by` passes it; names compare like
  the ledger's maker-checker (case and spacing ignored); an approval covers one run of exactly that call (G3, G4).
- **Tests.** `test_guard_approvals.py` (bound to exact arguments, four eyes, re-check, approver callbacks).
- **Limits.** `by` and the requester are names, not authenticated identities: whatever string the approver returns.

### G8. The record: hash chain, signatures, vault, erasure, sign-off

- **Mechanism.** `ledger.write` (`shio/ledger.py:549`) computes a record's sha256 over canonical JSON including its
  `integrity.seq` and `integrity.prev`, signs that digest (Ed25519 or HMAC-SHA256), writes the record and appends a
  ledger entry under a POSIX file lock. Events (erasure, label, sign-off, migration) are appended with a sha256 over
  their own fields and, since 8603f62 (format `v: 2`), their `seq` and `prev` (`ledger._event_hash`). Vaults are AES-256-GCM with the record's file name as
  associated data (`ledger.py:190`), so a vault cannot be moved to another record. `verify` (`ledger.py:808`) checks
  sequence numbers, the `prev` chain, signatures, every record's hash, vault hashes, erasures, sign-off maker-checker
  and roles, files on disk that are not in the ledger, and an optional anchored `--head`.
- **Tests.** `test_regulated.py::Records` (edit, delete, insert, reorder, wrong key, HMAC, erasure, vault deleted
  without erasure), `test_oversight.py` (maker-checker, two roles, rejection, edited sign-off, sign-off around the
  checks, personal signer keys), `test_vault_encryption.py`, `test_async_and_labels.py`.
- **Limits.**
  - Fixed in 8603f62 (L1): with a public key or secret, `verify` (and the evidence manifest check) treats every
    entry that is not verifiably signed by that key (unsigned, or signed in another scheme) as a problem. Without a
    key, signatures are only counted.
  - Fixed in 8603f62 (L2): events now bind their chain position. Events written before 1.0 (no `v`) still verify,
    with a note that their position is not signed, so in such a ledger an old event followed only by old events can
    still be removed undetected; anchor the head. An old-form event after a current one is a problem (downgrade).
  - Entries at the end can be removed unless the head was anchored (L3, documented).
  - The signing key is on the host that writes; with it, everything can be rewritten (A5).

### G9. Regulated mode refuses to run unless records are signed and models pinned

- **Mechanism.** At the start of a top-level regulated run (`core.py:699`), Shio refuses if traces are off or no
  signer is configured; each read refuses a model that is not pinned (`_check_pinned`, `core.py:345`); a vault is
  always used. The wider compile-time scan is G4.
- **Tests.** `test_regulated.py` (`test_needs_a_signing_key_and_recorded_runs`, `test_unpinned_model_is_refused...`,
  `test_ollama_digest_pin`).
- **Limit.** A pin other than an Ollama digest is the operator's statement, not a check (docs/REGULATED.md).

## 6. How the guarantees are tested, and how well

- **Full suite.** 358 tests and 369 subtests pass (results and environment in [REVIEW-BRIEF.md](REVIEW-BRIEF.md#results)).
- **Escape suite.** `tests/test_boundary_escapes.py` names 29 routes: 13 reflection routes, 7 impure check forms and
  9 pokes at a live reading from Python. `tests/test_regulated.py` adds 11 for regulated mode.
- **Fuzzing.** `tests/test_fuzz_boundary.py` has two parts, and a reviewer should know their limits:
  - `Fuzz` samples `.shio` programs from a fixed space of **672 distinct programs** (13 statement forms × 48
    expressions, plus 48 check/when forms). `SHIO_FUZZ_N` sets the number of samples, not distinct programs: at
    N=20000 each program is generated about 30 times. In our runs **every one was refused at compile time**; none
    reached the runtime, so this part tests the compiler's name check and purity check only.
  - `FuzzPythonAPI` evaluates 44 expressions and N/3 random pairs of them against a live reading from Python (no
    compiler). At N=20000 that is 6,710 evaluations, about 82% of which raise `BoundaryError`. This part does test
    the runtime seal.
  - Neither part generates the routes in findings B1-B3. The fuzz shows that the listed forms are stopped; it is not
    evidence about forms outside its grammar.

## 7. Known limits

Documented before this review (SECURITY.md, docs/REGULATED.md):

- Python is not a sandbox. Imported modules are trusted and unrestricted.
- The boundary is tracked by name, not by full information-flow analysis.
- A rule can do the wrong thing, and its result is trusted.
- Traces contain model inputs and outputs. Any code in the process, including plain `.shio` code with `open()`, can
  read the trace folder, so the seal is about how the program is *structured*, not a confidentiality boundary
  against the program itself (P10).
- The signing key is the root of trust; anchor the head outside the folder.
- HMAC signatures are verifiable only by holders of the secret.
- A role and a person's name are claims. Maker-checker and Guard's four eyes compare names (case and spacing
  ignored), not identities.
- File locking is POSIX only.

Found while preparing this review (20 October 2026, in 1.0.0rc1; details in REVIEW-BRIEF.md §4). All nine are
fixed on main for 1.0.0; the regression tests are in `tests/test_review_findings.py`:

| Id | Area | Finding | Mode | Status |
|---|---|---|---|---|
| L1 | Ledger | `verify` with the public key passes an unsigned, forged sign-off; the decision reads APPROVED | all | Fixed in 8603f62 |
| L2 | Ledger | An event's `seq` and `prev` are not covered by its hash or signature; a signed rejection followed only by events can be deleted undetected | all | Fixed in 8603f62 |
| B1 | Compiler | `shio.core._open`, `shio.core._run` and `getattr` with a computed name reach every reading's value from plain `.shio` code | non-regulated | Fixed in e23ce33 |
| B2 | Compiler | Check purity misses `getattr(o, "append")(v)`, `o.__iadd__(...)` and helper functions, including helpers defined in the same `.shio` file | B2 helpers: all; others: non-regulated | Fixed in e23ce33 |
| B3 | Compiler | The prelude alias `__shio` is exempt from the dunder rule: `__shio.core.last_run` exposes the previous run's values | regulated | Fixed in e23ce33 |
| G1 | Guard | Provenance by substring: a value is "grounded" if it is a substring of trusted text | all | Fixed in 07d547d |
| G2 | Guard | Digits-only control values are never tainted or ungrounded | all | Fixed in 07d547d |
| G3 | Guard | One approval covers every later identical call in the session | all | Fixed in 07d547d |
| G4 | Guard | Four eyes is off unless `requester=` is passed (never, from `.shio`), and compares exact strings | all | Fixed in 07d547d |
