# Shio for regulated decisions

Shio programs already separate what a model proposes from what code decides. For banks, insurers, pharma and the
public sector, that has to become evidence. This page covers what Shio records, how to check it, and where its
guarantees stop.

```bash
shio keygen keys/                                   # an Ed25519 key pair: keep the .key secret, give the .pub to auditors
export SHIO_SIGNING_KEY=keys/shio-signing.key
shio run loan.shio --traces records/                # every decision becomes a signed record in records/ledger.jsonl
shio verify records/ --pubkey keys/shio-signing.pub # chain, signatures, vaults: VERIFIED or every problem found
shio replay records/assess-…json                    # re-run one decision from its recorded model answers
shio erase records/assess-…json --reason "erasure request #4411"   # delete its personal data; the erasure is recorded
shio signoff records/pay-…json --as "Priya Nair" --role finance_controller --meaning "approved for payment"
shio reasons records/assess-…json                   # the reason codes and the text the customer is given
shio explain loan.shio -o loan-decision-map.html    # the decision map for the model-risk file
```

## A regulated program

```shio
regulated

type Applicant:
    name: str pii          # personal data: hashed in the record, kept whole only in the vault
    income: float          # monthly income

model bank_llm = ollama("qwen3:8b", digest="500a1f067a9f")   # refused unless the served weights are exactly these
```

The `regulated` line changes three things:

1. **Compile time: the escape hatches are errors.** Shio stops a reading from deciding anything except through a
   check or a rule. But Shio code is Python underneath, and Python offers routes around that, such as
   `locals()["x"]`, `getattr`, private attributes like `x._value`, `eval`, or importing Shio's own internals. In a
   regulated program each of these is a compile error, reported at its `.shio` line.
2. **Run time: no unrecorded decisions.** A regulated program refuses to run if trace files are turned off or no
   signing key is set (`SHIO_SIGNING_KEY`).
3. **Run time: only pinned models.** A model call is refused unless the model is pinned to an exact version:
   - Ollama: `digest=`, which is checked against the weights actually being served;
   - another provider: `pinned="<dated model id>"`, which is recorded but not checked.

   A refused call gives a null reading, so the program fails closed: nothing is approved on an unverified model.

## What a record proves

Each top-level run is written as one record and entered in its folder's `ledger.jsonl`.

| The record holds | So an auditor can see |
|---|---|
| `program`: the sha256 of the `.shio` source, the Shio version, `regulated`, and the sha256 of each local Python module it imports | Exactly which logic decided. Imported Python modules are trusted code and must be reviewed too |
| For each read: the model's identity (Ollama digest or pinned id), the prompt's sha256, the type, and the sha256 of each source document | Which model saw what |
| The parsed value, with `pii` fields replaced by their sha256 | What the model proposed, without exposing personal data |
| Each check (name, key, passed, skipped) and each rule's outcome | Why the outcome was reached |
| The outcome | What was decided |
| `integrity`: its position in the chain, the previous entry's hash, its own sha256, the signing key's id and the signature | That the record is unchanged, complete and in order |

Free text goes in a **vault** file beside the record: prompts, source text, the model's raw answers, check
explanations (which interpolate values), check arguments and the agent's inputs. The record keeps a sha256 of each
item. Regulated programs always use a vault; others can opt in with `shio.configure(vault=True)`.

### What `shio verify` catches

- A record whose content was edited (TAMPERED).
- A record deleted from disk (MISSING).
- A record added outside the ledger ("not in the ledger").
- Ledger entries reordered, removed or inserted ("chain broken").
- A signature made by a different key, or forged (BAD SIGNATURE).
- A vault edited, or deleted without an erasure entry.
- With `--head`: any entry added or replaced after the head hash was anchored.
- A sign-off that breaks maker-checker, asks for the wrong role, was edited, or is for another version of
  its record; and every decision still PENDING sign-off (a note, not a failure).

**Anchor the head.** `verify` prints the chain's head hash. Someone holding the signing key could rebuild a whole
chain, so record the head somewhere they can't rewrite: a WORM bucket (S3 Object Lock), a change ticket, an email
to audit. Do this each day or each batch. The chain can only be trusted as far as its last anchored head.

### Replay and impact analysis

`shio replay RECORD` loads the program, checks that its source hash matches the record, and rebuilds the inputs.
It answers every model call with the raw answer the record holds, then compares every check, rule and the outcome:

```
replayed assess-20261016-144416-4a94….json with loan.shio
  1 recorded model answers used, 2 checks, 1 rules
  REPRODUCED: same checks, same rules, same outcome
```

The model isn't asked again, so model randomness doesn't matter: the decision logic is what gets reproduced.

With `--allow-changed`, a changed program is replayed against the old answers, and the output shows which checks
and outcomes would differ. Run that over last quarter's records before changing a threshold, and you have an
impact analysis of the change.

### Personal data and erasure

`shio erase RECORD --reason "..."` deletes the record's vault and adds a signed erasure entry to the chain. The
folder still verifies, with a note. The record still shows what was decided and why, through the check names,
results and outcome. The personal data and the model's answers are gone, so that decision can no longer be
replayed.

**Outcomes are recorded in clear**, because the decision itself must stay auditable. Don't put personal data into
an outcome unless the decision needs it.

## Human sign-off

An agent declares which outcomes need a person:

```shio
LIMIT = 10000

agent pay(invoice: Source, submitted_by: str, llm):
    read inv: Invoice from invoice by llm:
        Read the invoice.
    check known as UNKNOWN_SUPPLIER: inv.supplier in SUPPLIERS, "unknown supplier {inv.supplier}"
    signoff PAY when amount > LIMIT by role "finance_controller" maker submitted_by
    rule decide:
        if checks.failures():
            return {"action": "HOLD"}
        return {"action": "PAY", "amount": inv.amount, "submitted_by": submitted_by}
    return decide
```

- **What it matches.** `PAY` matches an outcome that is the string `"PAY"`, or a dict whose `status`, `action`,
  `decision` or `outcome` is `"PAY"`. `signoff * ...` matches any outcome. Without `when`, every such outcome
  needs sign-off.
- **The condition is over the decided outcome**, its fields by name, and the program's constants. It can't name a
  reading, so a model can't talk its way past a sign-off. A condition that can't be evaluated (a field the outcome
  doesn't have, an error) needs sign-off: it fails closed.
- **What the agent returns.** The agent returns
  `{"status": "PENDING", "signoff": {"roles": [...], "because": [...]}, "decided": <the outcome>}`. The record gets a
  sign-off request with the roles, why it's needed (with the values, e.g. `amount > LIMIT (amount=25000.0,
  LIMIT=10000)`), who requested it, and the makers. A sign-off in a nested agent puts the request on the top-level
  record, and the top-level outcome is PENDING whatever the outer agent returns, so wrapping can't skip it.
- **Two-person rules.** `by role "finance_controller" and "treasury"` needs one sign-off per role, from different
  people.

`shio signoff RECORD` shows what a decision is waiting for. With `--as NAME --role ROLE --meaning "..."` it appends
a sign-off entry to the ledger, signed by the ledger key like every entry. The entry holds who signed, in which role,
when, the stated meaning of the signature, approval or rejection (`--reject`), and the sha256 of the record they
saw. `--key` adds the person's own Ed25519 signature (from `shio keygen`), so the sign-off is tied to them and not
only to the system.

**Maker-checker is enforced.** A sign-off is refused, and `shio verify` reports it as a PROBLEM if one gets into the
ledger another way, when the signer is:
- the requester (`shio run --as`, `SHIO_ACTOR`, or the login);
- a maker the record names (the outcome's `maker` field and the fields after `maker`, compared ignoring case);
- someone who already signed the decision.

A sign-off is also refused or reported when the role isn't one the request asks for, the role was already signed,
the decision was already approved or rejected, or the record has changed since it was entered in the ledger.
`shio verify` lists every decision still PENDING, checks each person's own signature against `--signer-pubkey`, and
reports a sign-off entry that was edited or that refers to a different version of its record.

## Reason codes

```shio
codes:
    INCOME_NOT_VERIFIED: "We could not verify your income from the documents you sent."
    DTI_TOO_HIGH: "Your existing repayments are too high for this loan."

agent assess(statement: str, llm):
    ...
    check income_ok as INCOME_NOT_VERIFIED: a.quote in statement, "quote {a.quote} not in the statement"
```

A check's code is recorded in clear in every result for that check. A failed check's code also goes into the
record's `reasons`, with the text from the `codes:` table. The check's explanation interpolates values, so it stays in
the vault; the code and the table's text are the same for everyone, so they hold no personal data and stay in the
record. After `shio erase`, `shio reasons RECORD` still prints:

```
  reasons:
    INCOME_NOT_VERIFIED: We could not verify your income from the documents you sent.
```

The text is taken when the decision is made, so a later change to the table doesn't rewrite what a customer was
told. In a regulated program every code must be in the `codes:` table, codes are in capitals, the text must be a
plain string (never an f-string), and a `when` can't have a code, because it only routes control flow.

## The decision map: `shio explain`

`shio explain program.shio` writes the program's technical documentation in Markdown (`--html` or `-o map.html` for
a page). It is generated from the source, so it can't drift from what runs, and it names the source's sha256, which
every decision record also names. For each agent it lists:

- every read: its type, sources, model, what the model sees (blind, `seeing`), the prompt, and which fields are
  personal data;
- every check and when, with its reason code, condition, explanation and `after` dependencies;
- every outcome each rule can return, with the `if`/`elif`/`else` conditions on the way to it, the checks it depends
  on, and whether it needs sign-off;
- the sign-offs: outcome, condition, roles, and who can't sign;
- the models and how each is pinned;
- what the model is trusted with (proposing values) and what it isn't (deciding anything), with the reviewer's
  warnings: fields a rule uses that no check verifies, and readings nothing checks.

## When the rules change: data-handling policies

Regulations about personal data change. Today a circular may require email addresses to be hashed; next year
another may allow them in clear but require phone numbers to be hashed. Shio keeps three things apart so that a
change like this is a policy update, not a code change, and every past decision can still be shown to have
followed the rule in force on its date.

**1. Programs say what the data is,** never how to store it:

```shio
type Customer:
    email: str as Email
    phone: str as PhoneNumber
    pan: str as PAN
```

In a regulated program, a field whose name looks like personal data (email, phone, PAN, Aadhaar, account number,
date of birth, address…) and has no class is a compile error. Use `as Public` for a field that isn't personal.

**2. Dated policy versions say how each class is stored,** and where the rule comes from. They're written by
compliance, in `.policy` files:

```
policy "RBI data handling" version 4:
    effective 2027-01-01
    source "RBI circular 2026-27/112, para 5"
    approved_by "Chief Compliance Officer"
    Email:       clear
    PhoneNumber: hashed
    PAN:         vault, retain 8 years
    *:           vault                       # any class not listed
```

| Handling | In the record | In the vault |
|---|---|---|
| `clear` | as it is | — |
| `vault` | its sha256 | kept whole, until erased or its retention ends |
| `hashed` | its sha256 | nowhere; it is also scrubbed from prompts, raw answers, explanations and the outcome |

Where several policies cover a class, the most protective rule applies. A class that no policy in force covers is
kept in the vault.

**3. Each decision follows the versions in force on its date, and says which.** Run with
`--policies policies/` (or `SHIO_POLICIES`). The record carries `data_policy`: each version's name, number,
effective date, source, approver and sha256, plus the handling applied to every field.

```bash
shio policy show   --policies policies/ --at 2027-02-01          # what is in force on a date
shio policy check  app.shio --policies policies/                  # every class has an explicit rule (fails if not)
shio policy impact app.shio --policies policies/ --at 2027-01-01  # what changes when a new version takes effect
shio audit     records/ --policies policies/                      # every record against the rule of its date
shio migrate   records/ --policies policies/ --reason "circular 112"   # re-seal older records under a stricter rule
shio retention records/ --policies policies/ [--apply]            # vaults past their retention period
```

- **`impact`** lists every program field whose handling changes, and says which changes can be applied to
  existing records (tightening) and which apply to new records only (loosening).
- **`audit`** reports any record:
  - written with no policy, or under an older version than the one in force on its date;
  - whose policy version was **edited after the record used it** (versions are immutable once in force; add a
    new one);
  - that keeps a field less protected than its rule required;
  - whose vault has outlived its retention period.
- **`migrate`** never loosens. It re-seals a record under today's stricter rule and enters a signed migration
  entry chaining the old hash to the new one. `verify` follows that chain, the record keeps the policy it was
  decided under, and the migration lists exactly what was tightened. Sign-offs given on the original remain valid.
- **`retention --apply`** erases expired vaults, each erasure citing the policy that set the period.

### Limits and thresholds: decision tables

Regulators change numbers too: loan-to-value caps, approval limits, KYC refresh periods. Put them in the same
`.policy` files as dated, sourced tables instead of constants in the program:

```
table "Gold loan limits" version 2:
    effective 2027-04-01
    source "RBI circular 2026-27/150, para 2"
    approved_by "Head of Credit Risk"
    max_ltv: 0.75
    max_amount: 500000
```

```shio
agent sanction(slip: str, asked: float, llm):
    limits = in_force("Gold loan limits")         # the version in force on the date of this decision
    ...
    rule decision:
        cap = min(value * limits.max_ltv, limits.max_amount)
```

- Each record lists the table version (name, version, sha256, source, approver) and **the values the decision
  actually read**.
- A name the table doesn't have is an error. In a regulated program the table's name must be a literal, so
  `policy check` and `shio explain` can see every table a program depends on.
- **Replay** uses the recorded values by default, so the decision reproduces exactly.
- `shio replay RECORD --policies DIR --at 2027-04-01` re-runs a past decision under the limits in force on that
  date. Run it over last quarter's records before a new cap takes effect, and you see which customers' outcomes
  change ("SANCTION 2,10,000" becomes "COUNTER-OFFER 1,92,360").
- **`policy impact`** lists every table value that changes on a date, and the programs that read it.
- **`audit`** flags a decision made with a table version other than the one in force on its date, and a table
  version edited after use.

See `examples/regulated/gold.shio`.

The language doesn't read a circular. A person turns it into a new policy version (a model can draft it, and a
person approves). Shio provides one place for the rule, proof that it's applied everywhere, and a signed record
of which rule governed each decision.

## Evidence for an audit period

```bash
shio evidence records/ --from 2026-07-01 --to 2026-09-30 --policies policies/ --pubkey keys/shio-signing.pub -o pack/
shio evidence --check pack/ --pubkey keys/shio-signing.pub     # has anything in the pack changed since?
```

One folder an auditor can take away:

- **`evidence.md`** covers, in one readable report:
  - integrity: `verify` over the whole ledger, the chain's head hash, signature states;
  - decisions in the period, by program version and by outcome;
  - human oversight: sign-offs by role, rejections, decisions still pending;
  - the reason codes given most often;
  - the data-policy and table versions in force, and the `audit` result;
  - monitoring.
- **`programs/`** holds the decision map (`shio explain`) of every program version that decided in the period.
  A version whose source has since changed is listed without one.
- **`data.json`** holds the figures behind the report.
- **`manifest.json`** holds the sha256 of every file, signed with the signing key, so `--check` shows whether
  anything in the pack was edited after it was built.

**Monitoring needs no labels.** Every check tests the model's work against something it can't argue with, so a
check's pass rate is a measure of quality in its own right. The pack flags:
- a week in which a check's pass rate falls more than ten points below its rate in the weeks before
  ("on_slip: 29% in W33, down from 100% before it");
- a model served with different weights during the period (its digest changed);
- more than 5% of model calls giving no usable answer.

## Where the guarantees stop

- **Python is not a sandbox.** Regulated mode makes every route around the boundary a compile error *in Shio code*.
  Imported Python modules run unrestricted. That's why their hashes are recorded and why they must be reviewed as
  part of the program. A stronger isolation, with model input and output in a separate process, is future work.
- **The signing key is the root of trust.** Keep it in a KMS or HSM in production. If it's stolen, anchored heads
  still expose any rewritten history.
- **HMAC signatures** (`SHIO_SIGNING_KEY=hmac:<secret>`) can only be checked by holders of the secret. Use Ed25519
  wherever records go to a third party.
- **A pin other than an Ollama digest is a promise, not a check.** For other providers the pinned id is recorded
  but can't be verified against the served model.
- **One writer per folder on Windows.** File locking is POSIX only.
- **A role is a claim.** Shio checks that the signer is not a maker and that the role is the one asked for. It
  can't check that the person holds the role. Run `shio signoff` behind your identity provider, or give each
  approver their own key (`--key`) and check it with `--signer-pubkey`.
- **Maker-checker compares names.** Use the same identifiers for `--as`, `SHIO_ACTOR` and the outcome's maker
  fields (an employee id is better than a display name).
- **Scrubbing a hashed value is exact-match.** Its literal text is removed wherever it appears. A reformatted
  copy (`+91 98200 12345` written as `9820012345`) is not; values shorter than four characters are hashed in place
  but not scrubbed from free text.
- **A policy is only as good as its classes.** Shio checks that classified fields follow the policy; it relies on
  the program (and the regulated name check) to classify every personal field in the first place.
- **The decision map is static.** It shows conditions as written in the `.shio` file. It doesn't follow into
  imported Python modules or work out values computed at run time.

## How this maps to common requirements

This mapping is a starting point for your compliance team, not legal advice. Check each obligation with your
counsel and regulator.

| Requirement | Shio |
|---|---|
| Automatic logging of each decision (EU AI Act Art. 12) | A record per run, chained and signed |
| Human oversight (EU AI Act Art. 14; GDPR Art. 22) | `signoff`: outcomes held as PENDING until a person in the named role signs; maker-checker enforced and verified |
| Electronic signatures (21 CFR Part 11 §11.50, §11.70) | Sign-off entries hold the signer, time and stated meaning, signed and chained to the exact record hash; optional personal keys |
| Accuracy and robustness evidence (EU AI Act Art. 15; SR 11-7 validation) | Checks per decision, the fail-closed test, replay, and live-model results |
| Audit trails that can't be altered (21 CFR Part 11; SOX) | Hash chain, signatures, `verify`, anchored heads |
| Reasons for a decision (GDPR transparency; adverse-action notices) | Reason codes in clear with customer-facing text (`shio reasons`), surviving erasure; explanations in the vault |
| Technical documentation (EU AI Act Art. 11, Annex IV; SR 11-7 documentation) | `shio explain`: the decision map generated from the program, tied to its hash |
| Erasure and data minimisation (GDPR Art. 17; DPDP Act) | Data classes handled per policy (`clear`, `vault`, `hashed`), `shio erase`, `shio retention` |
| Rules that change over time (RBI, SEBI, DPDP directions) | Dated, sourced policy versions; the version in force stamped into each record; `shio audit`, `policy impact`, `migrate` |
| Change management | `replay --allow-changed` and `policy impact`; the program and policy hashes in every record |

## Next

Planned next:
- jurisdiction-scoped policies (one program serving several regulators);
- monitoring alerts while a period runs (today they come with the evidence pack), and dashboards.
