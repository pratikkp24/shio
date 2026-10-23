# Security review brief: Shio 1.0.0rc1

Thank you for reviewing Shio. This brief says what to review, how to build and test it, what we already know is
wrong, and the ten questions we most want attacked. Read [THREAT-MODEL.md](THREAT-MODEL.md) first: it states the
claims, the attackers, the trust boundaries and the mechanism behind each guarantee, with file and line references.

Prepared 20 October 2026. An outside review is one of three conditions for 1.0.0 final (with a production deployment
and benchmark results on weaker models).

## 1. Scope

**Version.** `shio-lang` 1.0.0rc1 on PyPI (`pip install --pre shio-lang==1.0.0rc1`), git commit `d00dc3a` on
`main` of https://github.com/pratikkp24/shio. Python 3.10 to 3.13.

**In scope.** About 3,500 lines:

| File | Lines | What to look at |
|---|---|---|
| `shio/compiler.py` | 1,011 | The name check (`scan`), check purity (`pure`), `_reflection_scan`, `_regulated_scan`, `calls` |
| `shio/core.py` | 774 | `Reading`, `_SEALED`, `_gate`, `_open`, `check`, `rule`, `agent`, regulated start-up checks |
| `shio/guard.py` + `shio/guard.shio` | 725 + 94 | Provenance `Ledger`, plan, gate, approvals, four eyes, `tool_call` |
| `shio/ledger.py` | 943 | Record hashing, the chain, signatures, vault encryption, erasure, sign-off, `verify` |
| `shio/schema.py` | | `parse`: how a completion becomes a value |

**Attackers in scope:** injected content and malicious model output (A1, A2); a trace-folder writer without the
signing key (A4); approvers and requesters (A6); and, for `regulated` programs, a program author who evades the
boundary in `.shio` source (A3). See THREAT-MODEL.md §3.

## 2. Out of scope

- **Imported Python and Python callers.** Python is not a sandbox. Code that imports `shio` and calls
  `shio.core._sealed()` or sets `shio.core._open` is deliberate Python. The same routes written in `.shio` source are
  in scope.
- **A malicious operator or host**, or anyone holding the signing key, except where an anchored head should catch it.
- **Whether a rule or check is correct.** Shio forces decisions into rules; it does not verify them.
- **Model quality**: whether a model is fooled. Shio assumes it will be.
- **The user's request** to Guard is trusted. A hostile request is out of scope.
- **Denial of service** (huge completions, slow models), **supply chain** (pydantic, cryptography), the VS Code
  extension (`editors/`), `shio train`, the docs site.
- **The AgentDojo numbers** in the README. Opus 5.5 refused every tool-call attack on its own, so that run did not
  test the gate; the benchmark harness is not published yet.

## 3. Build and test

```bash
git clone https://github.com/pratikkp24/shio && cd shio && git checkout d00dc3a
python -m venv .venv && . .venv/bin/activate
pip install -e ".[regulated,pdf]" pytest
pytest -q
SHIO_FUZZ_N=20000 SHIO_FUZZ_SEED=1 pytest -q tests/test_fuzz_boundary.py
SHIO_FUZZ_N=20000 SHIO_FUZZ_SEED=1 python docs/review/probes/fuzz_counts.py     # prints the fuzz outcome counts
python docs/review/probes/boundary_probes.py
python docs/review/probes/ledger_probes.py
python docs/review/probes/guard_probes.py
```

The probes call no model (they use `replay`) and need no network or API keys. Useful entry points:
`shio check FILE.shio` compiles only; `shio build FILE.shio` prints the generated Python;
`shio verify TRACES --pubkey shio-signing.pub [--head HASH]` checks a ledger.

### Results

Run on 20 October 2026 at commit `d00dc3a`. Four `examples/usecases/*.shio` files had uncommitted edits from other
work at the time; nothing under `shio/` or `tests/` did.

| | |
|---|---|
| Environment | macOS 26.5.2 (Darwin 25.5.0), arm64; CPython 3.13.3; pytest 9.1.1, pydantic 2.14.1, cryptography 50.0.2, pypdf 6.20.1 |
| Full suite (`pytest -q`) | **358 passed, 369 subtests passed, 0 skipped, 0 failed**, 59.9 s |
| Long fuzz, `SHIO_FUZZ_N=20000`, seeds 1, 2, 3 | **2 passed** for each seed, 0 leaks, about 8 s per seed |
| `Fuzz` (.shio programs), each seed | 20,000 sampled, 672 distinct; refused at compile time 20,000, blocked 0, ran 0 |
| `FuzzPythonAPI` (live reading), seed 1 / 2 / 3 | 6,710 evaluations; BoundaryError 5,529 / 5,506 / 5,540; other exception 428 / 442 / 421; ran with no value in the result 753 / 762 / 749 |

**How to read the fuzz numbers.** "20,000 programs" is 20,000 samples from 672 distinct programs, and none of them
reached the runtime: the compile-time name check refused them all. The `.shio` fuzz therefore tests the compiler only,
on the forms in its grammar. The Python-API fuzz does test the runtime seal. Neither generates the routes in §4
(now fixed, with their own regression tests). Treat the fuzz as a regression net, not as evidence of coverage.

## 4. Known findings (found while preparing this brief in 1.0.0rc1; fixed on main for 1.0.0)

Each has a reproduction in `docs/review/probes/`. We list them so you don't spend time rediscovering them, and because
the fixes moved the boundary you're reviewing: please attack the fixes too. Severity is our estimate. Each probe now
shows the issue stopped, and each fix has a regression test in `tests/test_review_findings.py`.

| Id | Severity | Finding | Probe | Status |
|---|---|---|---|---|
| L1 | High | `ledger.verify` (and `shio verify --pubkey`) treats an **unsigned** entry as fine; only `BAD SIGNATURE` is a problem (`ledger.py:834`). An attacker who can write the trace folder appends an unsigned `signoff` entry and a PENDING decision becomes APPROVED; verify reports `ok`, with "1 unsigned" in the report. | `ledger_probes.py` L1 | **Fixed** in 8603f62: verify with a key: an unsigned or unverifiable entry is a PROBLEM |
| L2 | High | Event entries (sign-off, label, erasure, migration) hash and sign their own fields but **not `seq` or `prev`**. A signed rejection followed only by other events can be removed and the later entries renumbered and re-linked; verify passes and the decision reads PENDING again. | `ledger_probes.py` L2 | **Fixed** in 8603f62: events are `v: 2`, hashed over seq and prev; pre-1.0 events verify with a note; old after new is a problem |
| B1 | Medium | Outside regulated mode, `.shio` code can `import shio` and use `shio.core._open.set(True)`, `shio.core._run.get().readings`, or `getattr(shio.core, "_SE" + "ALED")` to read every reading's value without naming it. `_reflection_scan` refuses `_sealed`, `_SEALED` and `_gate` as literal attribute names only. | `boundary_probes.py` P5, P6 | **Fixed** in e23ce33: shio's private names refused as attributes and imports; `getattr` in `.shio` is `core.checked_getattr` |
| B2 | Medium | Check purity is a deny-list on the check's own text. `getattr(LEAK, "append")(x.value)` and `LEAK.__iadd__([x.value])` pass outside regulated mode; a helper function the check calls passes in **every** mode, including one defined at the top of the same `.shio` file. | P1, P2, P3 | **Fixed** in e23ce33: no mutating method or dunder in a check; same-file helpers checked for purity |
| B3 | Medium | In regulated mode, names starting with `__shio` are exempt from the dunder rule (`compiler.py:979`) so the prelude works. `__shio.core.last_run["run"].to_dict()` then gives plain code the previous run's readings in clear. | P9 | **Fixed** in e23ce33: regulated `.shio` text may not contain a `__shio` identifier |
| G1 | Medium | Guard provenance is substring matching: a control value is "grounded" if its normalised form is a substring of any trusted text. With `bob@acme.com` in the request, an injected `bob@acme.co` runs. | `guard_probes.py` G1 | **Fixed** in 07d547d: grounded = whole token or address in trusted text |
| G2 | Medium | A value of fewer than 3 characters, or made only of digits, is never tainted or ungrounded (`guard.py:299`, `:307`). A digits-only account number taken from an injected invoice runs. | G2 | **Fixed** in 07d547d: digits-only values judged for iban/account arguments |
| G3 | Low | An approval is stored per session by call hash and reused: after one approval, every identical later call runs without asking. | G3 | **Fixed** in 07d547d: an approval covers one run |
| G4 | Low | Four eyes is enforced only when `requester=` is passed, which the `.shio` form `guard ... approved by` never does (`guard.py:683`). The comparison is exact, so `"Maya "` approves `"maya"`'s task; the ledger's maker-checker case-folds. | G4 | **Fixed** in 07d547d: requester defaults to the actor; `guard ... approved by` passes it; names compared like maker-checker |

Also observed, not a bug: P7 and P8 (a comprehension or lambda that rebinds a reading's name hides it from the name
check) compile, but the runtime holds; P10 (plain code reading its own trace file) works in every mode and is listed
as a limit.

## 5. Ten questions to attack

1. **Reflection in `.shio` code.** Beyond B1 and B3, can plain `.shio` code reach a reading's value, or set `_open`,
   without naming the reading? In regulated mode especially: the refused lists are in `compiler.py:928` and `:958`.
   Think `type(x).__mro__`-free routes: descriptors on public classes, `functools`, `str.format` field access
   (`"{0.value}".format(...)`), pydantic model internals, `__shio.core`'s public attributes, `__shio.guard`,
   `shio.models`, exception tracebacks.
2. **The name check.** Can a reading's *name* reach plain code past `Compiler.scan`? Look at tokenisation of
   multi-line statements, f-strings (PEP 701 in 3.12+ tokenises them differently), the `prev == "."` and keyword-
   argument exemptions, `match`/`case`, decorators, default arguments, nested `def` and `class`, `for ... in each`,
   the `seeing` clause, a reading name reused in a nested agent.
3. **The seal at run time.** With a live `Reading` in plain code (`_open` false), can any expression get its value:
   `__class__` tricks, `object.__getattribute__`, `__slots__` descriptors, `weakref`, `__reduce__` via `copyreg`,
   `_part`/`each()`, exceptions raised from a check whose message holds the value, a check's `detail` lambda, a value
   kept by a pydantic validator? Is `_open` reachable from async tasks, threads or generators started inside a check
   or rule after it returns?
4. **Check purity.** Beyond B2: how should a check be made pure? Is an allow-list of calls (pure builtins plus
   declared helpers) workable, or evaluating checks on a deep copy with `_open` scoped to the copy? What can a
   check's failure message (evaluated with `_open` set) do?
5. **Guard provenance.** Beyond G1 and G2: normalisation (`norm` strips scheme, `www.`, whitespace, trailing `/` and
   `.`; is it reversible into a different target?), Unicode confusables and case in emails and URLs, IBAN with
   spaces, an attacker value that is a substring of a long trusted record, URLs on a site the request names
   (`ungrounded` accepts any path on a named site), the one-hop endorsement through a designated page, the
   `about` word match.
6. **The plan.** The planner sees only the request. Can tool output reach it anyway (a `Source` that is not the
   request, a delegated request, a planner that fails)? Does "delegated → every action held" hold in every path?
7. **Ledger tampering without the key.** Beyond L1 and L2: what can someone who can write the trace folder do that
   `verify` doesn't report? Records' `time` and `kind` fields in ledger entries, the vault's non-encrypted mode,
   migrations (`migrate` rewrites a record in place and chains old hash to new), erasure of a vault that is then put
   back, `labels`, two writers on Windows, a record whose `integrity` field is removed.
8. **Sign-offs and maker-checker.** Can a maker sign their own decision (name normalisation, a maker field the
   outcome omits, a nested agent's maker, `SHIO_ACTOR`)? Can a sign-off be replayed onto another record or another
   version of the same file? Does a REJECTED decision stay rejected?
9. **Approvals and four eyes in Guard.** Beyond G3 and G4: can a held call be approved for different arguments
   (the id is the first 12 hex digits of the hash; collisions within one session)? Can an approval made in one
   session be used in another? What does an approver callback that raises do?
10. **Regulated mode as a whole.** Does a regulated program refuse to run without a signer, recorded traces and
    pinned models in every entry point (`shio run`, `shio.load`, async agents, nested agents, replay)? Can a
    regulated program be made to write a record outside the vault, or to skip the record?

## 6. How to report

Please report privately, not in a public issue:

- GitHub private vulnerability reporting: https://github.com/pratikkp24/shio/security/advisories/new
- If that page is not available to you, open an issue titled "Security contact request" with no details, and we
  will reply with a private channel.

Include the smallest `.shio` program or Python snippet that shows the problem, the commit, and the Python version.
Findings that let model output reach plain code, act without the gate, or change the record without `verify`
noticing are what we most want.
