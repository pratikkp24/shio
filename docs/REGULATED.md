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

## How this maps to common requirements

This mapping is a starting point for your compliance team, not legal advice. Check each obligation with your
counsel and regulator.

| Requirement | Shio |
|---|---|
| Automatic logging of each decision (EU AI Act Art. 12) | A record per run, chained and signed |
| Human oversight (EU AI Act Art. 14; GDPR Art. 22) | Outcomes route to people (HUMAN, REVIEW); a language-level sign-off primitive is next on the roadmap |
| Accuracy and robustness evidence (EU AI Act Art. 15; SR 11-7 validation) | Checks per decision, the fail-closed test, replay, and live-model results |
| Audit trails that can't be altered (21 CFR Part 11; SOX) | Hash chain, signatures, `verify`, anchored heads |
| Reasons for a decision (GDPR transparency; adverse-action notices) | Failed checks' explanations (vault) and check names (record); stable reason codes are on the roadmap |
| Erasure and data minimisation (GDPR Art. 17; DPDP Act) | `pii` fields hashed, vault, `shio erase` |
| Change management | `replay --allow-changed` impact analysis; the program hash in every record |

## Next

Planned next:
- sign-off as a language form, with maker-checker enforced by the compiler;
- stable reason codes on checks;
- `shio explain` to generate a decision map, the model documentation regulators ask for;
- versioned policy tables;
- monitoring of check pass rates;
- an evidence pack per audit period.
