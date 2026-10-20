# Traces in production

Every run of an agent is saved as a record: what each model read and answered, which checks passed, what each rule
decided. Records are hash-chained in their folder, and signed when a key is configured; `shio verify` checks the chain.

## Reviewers' verdicts: labels

```bash
shio label shio-traces/pay-20261020-101500-ab12cd34.json --wrong --by omar --note "paid the wrong vendor"
```

A label is entered in the same chain as the decision, bound to the record's hash, and signed when a key is set;
`shio verify` checks it. Labels travel with traces (`trace.load` gives each run its `labels`), and `shio train` leaves
decisions labelled wrong out of the fine-tune set, so a reviewer's verdict counts as much as a check.

## Copies elsewhere: sinks

The signed folder stays the source of truth. Sinks send a copy of each record wherever you watch production:

```python
from shio import trace

trace.add_sink(trace.SqlSink(lambda: psycopg.connect(DSN)))                     # one row per run, any DB-API driver
trace.add_sink(trace.Background(trace.S3Sink("audit-bucket", prefix="shio/")))   # pip install 'shio-lang[s3]'
trace.add_sink(trace.OtelSink())                                                  # pip install 'shio-lang[otel]'
trace.add_sink(trace.JsonlSink("/var/log/shio/records.jsonl"))                   # for log shippers
```

| Sink | What it writes |
|---|---|
| `SqlSink` | A row per run: agent, times, checks passed and failed, readings, the record's hash, and the record as JSON |
| `S3Sink` | One object per record at `prefix/agent/YYYY/MM/DD/file.json`; extra arguments go to `put_object` (e.g. `ServerSideEncryption="aws:kms"`) |
| `OtelSink` | A span per run with a child span per read, check and rule. Names, outcomes and hashes only, never model text |
| `JsonlSink` | One JSON line per record, for Vector, Fluent Bit or a cloud logging agent |

- A copy is the record **as signed, without its vault**: personal data stays in the local, erasable vault.
- A sink that fails is logged and counted, and never fails a run or touches the chain.
- `Background(sink)` sends from a thread, so a slow sink doesn't slow the agent. Call `flush()` before exiting.
- A sink is anything with `emit(record, name)`.
