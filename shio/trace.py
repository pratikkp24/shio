"""Traces: every run of an agent saved as JSON (readings, checks, rules, outcome). They are the audit trail in
production and the training data later."""
import glob
import json
import logging
import queue
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger("shio.trace")
_SINKS = []


def write(run, trace_dir, vault=False, signer=None, handling=None):
    """Write a top-level run as a record entered in the folder's ledger (see shio.ledger), under the data-handling
    policies in force on its date (see shio.policy)."""
    from .ledger import write as ledger_write
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(run.started))
    path = ledger_write(run.to_dict(), trace_dir, f"{run.agent}-{stamp}-{run.id}", use_vault=vault, signer=signer,
                        handling=handling)
    if _SINKS:
        _fan_out(path)
    return path


# -- sinks: copies of every record, elsewhere -----------------------------------------------------------------------
#
# The signed, hash-chained folder written above stays the source of truth (regulated mode, `shio verify`). A sink gets
# a copy of each record as written: signed, and without its vault, so personal data stays in the local vault and
# erasure keeps working. A sink that fails is logged and counted; it never fails the run or touches the chain.

def add_sink(sink):
    """Send a copy of every record from now on to `sink` (anything with emit(record, name)). Returns the sink."""
    _SINKS.append(sink)
    return sink


def remove_sink(sink):
    if sink in _SINKS:
        _SINKS.remove(sink)


def clear_sinks():
    for s in list(_SINKS):
        getattr(s, "close", lambda: None)()
    _SINKS.clear()


def _fan_out(path):
    record = json.loads(Path(path).read_text())
    for sink in list(_SINKS):
        try:
            sink.emit(record, Path(path).name)
        except Exception as e:                       # a sink never breaks a run
            sink.failures = getattr(sink, "failures", 0) + 1
            log.warning("shio trace sink %s failed on %s: %s: %s", type(sink).__name__, Path(path).name,
                        type(e).__name__, e)


def _summary_fields(record):
    claims = [c for c in record.get("checks", []) if c.get("reward", True)]
    return {"id": record.get("id"), "agent": record.get("agent"), "started": record.get("started"),
            "ended": record.get("ended"), "error": record.get("error"),
            "checks_passed": sum(1 for c in claims if c.get("passed")),
            "checks_failed": sum(1 for c in claims if not c.get("passed")),
            "readings": len(record.get("readings", [])),
            "sha256": (record.get("integrity") or {}).get("sha256")}


class Background:
    """Wraps a slow sink (S3, a remote database) so runs don't wait for it: records queue and a thread sends them.
    flush() waits until the queue is empty; close() flushes and stops the thread."""

    def __init__(self, sink, maxsize=10000):
        self.sink, self.failures, self.dropped = sink, 0, 0
        self.q = queue.Queue(maxsize)
        self.t = threading.Thread(target=self._loop, daemon=True, name="shio-trace-sink")
        self.t.start()

    def emit(self, record, name):
        try:
            self.q.put_nowait((record, name))
        except queue.Full:
            self.dropped += 1
            log.warning("shio trace sink queue full: dropped %s", name)

    def _loop(self):
        while True:
            item = self.q.get()
            try:
                if item is None:
                    return
                try:
                    self.sink.emit(*item)
                except Exception as e:
                    self.failures += 1
                    log.warning("shio trace sink %s failed: %s: %s", type(self.sink).__name__, type(e).__name__, e)
            finally:
                self.q.task_done()

    def flush(self):
        self.q.join()

    def close(self):
        self.flush()
        self.q.put(None)
        self.t.join(5)


class JsonlSink:
    """One JSON line per record, appended to a file (for log shippers: Vector, Fluent Bit, CloudWatch agent)."""

    def __init__(self, path):
        self.path, self._lock = Path(path), threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, record, name):
        line = json.dumps({"file": name, **record}, ensure_ascii=False, default=str)
        with self._lock, open(self.path, "a") as f:
            f.write(line + "\n")


class SqlSink:
    """One row per record in a SQL table, through any DB-API connection: sqlite3, psycopg, mysqlclient...

        SqlSink(lambda: psycopg.connect(DSN))            # a connection factory; one connection per sink
    The table (created if missing) holds the summary columns and the whole record as JSON text."""

    COLUMNS = ("file", "id", "agent", "started", "ended", "error", "checks_passed", "checks_failed", "readings",
               "sha256", "record")

    def __init__(self, connect, table="shio_records", paramstyle=None):
        if not table.replace("_", "").isalnum():
            raise ValueError("table must be a plain name")
        self.conn, self.table, self._lock = connect(), table, threading.Lock()
        mod = sys.modules.get(type(self.conn).__module__.split(".")[0])
        style = paramstyle or getattr(mod, "paramstyle", "qmark")
        self.mark = "?" if style == "qmark" else "%s"
        cur = self.conn.cursor()
        cur.execute(f"CREATE TABLE IF NOT EXISTS {table} (file TEXT PRIMARY KEY, id TEXT, agent TEXT, started REAL, "
                    f"ended REAL, error TEXT, checks_passed INTEGER, checks_failed INTEGER, readings INTEGER, "
                    f"sha256 TEXT, record TEXT)")
        self.conn.commit()

    def emit(self, record, name):
        row = {"file": name, **_summary_fields(record), "record": json.dumps(record, ensure_ascii=False, default=str)}
        sql = (f"INSERT INTO {self.table} ({', '.join(self.COLUMNS)}) "
               f"VALUES ({', '.join([self.mark] * len(self.COLUMNS))})")
        with self._lock:
            cur = self.conn.cursor()
            cur.execute(sql, [row[c] for c in self.COLUMNS])
            self.conn.commit()

    def close(self):
        self.conn.close()


class S3Sink:
    """Each record as one object: s3://bucket/prefix/agent/YYYY/MM/DD/file. Needs boto3 (pip install
    'shio-lang[s3]'); credentials come from boto3's usual chain, never from Shio. Wrap it in Background()."""

    def __init__(self, bucket, prefix="shio/", client=None, **put_args):
        self.bucket, self.prefix, self.put_args = bucket, prefix.rstrip("/") + "/", put_args
        if client is None:
            try:
                import boto3
            except ImportError:
                raise RuntimeError("S3Sink needs boto3: pip install 'shio-lang[s3]'") from None
            client = boto3.client("s3")
        self.client = client

    def key(self, record, name):
        day = time.strftime("%Y/%m/%d", time.gmtime(record.get("started") or time.time()))
        agent = str(record.get("agent") or "run").replace("/", "_")
        return f"{self.prefix}{agent}/{day}/{name}"

    def emit(self, record, name):
        self.client.put_object(Bucket=self.bucket, Key=self.key(record, name), ContentType="application/json",
                               Body=json.dumps(record, ensure_ascii=False, default=str).encode(), **self.put_args)


class OtelSink:
    """Each run as an OpenTelemetry span (with a child span per reading, check and rule), for Datadog, Honeycomb,
    Grafana Tempo, Jaeger... Needs opentelemetry-api (pip install 'shio-lang[otel]') and your SDK setup; pass
    tracer= to use your own. Attributes carry names, outcomes and hashes, not model text."""

    def __init__(self, tracer=None):
        if tracer is None:
            try:
                from opentelemetry import trace as ot
            except ImportError:
                raise RuntimeError("OtelSink needs opentelemetry-api: pip install 'shio-lang[otel]'") from None
            tracer = ot.get_tracer("shio")
        self.tracer = tracer

    def emit(self, record, name):
        self._run(record, None)

    def _run(self, run, parent_ctx):
        ns = lambda t: int((t or time.time()) * 1e9)              # noqa: E731
        f = _summary_fields(run)
        attrs = {"shio.agent": str(f["agent"]), "shio.run_id": str(f["id"]), "shio.checks_passed": f["checks_passed"],
                 "shio.checks_failed": f["checks_failed"], "shio.readings": f["readings"]}
        if f["sha256"]:
            attrs["shio.sha256"] = f["sha256"]
        if f["error"]:
            attrs["shio.error"] = str(f["error"])[:500]
        kw = {"context": parent_ctx} if parent_ctx is not None else {}
        span = self.tracer.start_span(f"shio.agent {f['agent']}", start_time=ns(run.get("started")), attributes=attrs, **kw)
        ctx = self._ctx(span)
        for r in run.get("readings", []):
            self._leaf(f"shio.read {r.get('name')}", {"shio.model": str(r.get("model")),
                                                      "shio.parsed": not r.get("error")}, run, ctx)
        for c in run.get("checks", []):
            self._leaf(f"shio.{'check' if c.get('reward', True) else 'when'} {c.get('name')}",
                       {"shio.passed": bool(c.get("passed"))}, run, ctx)
        for r in run.get("rules", []):
            self._leaf(f"shio.rule {r.get('name')}", {}, run, ctx)
        for child in run.get("children", []):
            self._run(child, ctx)
        span.end(end_time=ns(run.get("ended")))

    def _ctx(self, span):
        try:
            from opentelemetry import trace as ot
            return ot.set_span_in_context(span)
        except ImportError:
            return span                                  # a test tracer: hand the span itself on

    def _leaf(self, name, attrs, run, ctx):
        s = self.tracer.start_span(name, context=ctx, attributes=attrs, start_time=int((run.get("started") or 0) * 1e9))
        s.end(end_time=int((run.get("ended") or run.get("started") or 0) * 1e9))


def load(patterns):
    """Yield every run (children included) from trace files matching the patterns."""
    files = sorted({f for p in ([patterns] if isinstance(patterns, str) else patterns)
                    for f in (glob.glob(p, recursive=True) if any(c in p for c in "*?[") else [p])})
    from .ledger import labels as ledger_labels, load_vault, merge
    folder_labels = {}
    for f in files:
        if Path(f).is_dir():
            yield from load(str(Path(f) / "*.json"))
            continue
        rec = json.loads(Path(f).read_text())
        if "agent" not in rec:          # not a run record (a vault or another JSON file)
            continue
        vault = load_vault(f, rec)
        if vault:                       # free text kept apart: put it back for training and inspection
            rec = merge(rec, vault)
        folder = str(Path(f).parent)
        if folder not in folder_labels:
            try:
                folder_labels[folder] = ledger_labels(folder)
            except Exception:
                folder_labels[folder] = {}
        rec["labels"] = [{k: e.get(k) for k in ("verdict", "by", "note", "time", "record_sha256")}
                         for e in folder_labels[folder].get(Path(f).name, [])]
        yield from _flatten(rec, None)


def _flatten(run, sources, labels=None):
    sources = {**(sources or {}), **run.get("sources", {})}
    run["sources"] = sources
    run["labels"] = run.get("labels") or labels or []     # a decision's labels cover the agents it called
    yield run
    for c in run.get("children", []):
        yield from _flatten(c, sources, run["labels"])


def summary(run):
    claims = [c for c in run["checks"] if c.get("reward", True)]
    lines = [f"{run['agent']}  run {run['id']}  {len(run['readings'])} readings, {len(claims)} checks, "
             f"{len(run['rules'])} rules" + (f"  ERROR {run['error']}" if run.get("error") else "")]
    failed = [c for c in claims if not c["passed"]]
    passed = len(claims) - len(failed)
    lines.append(f"  checks: {passed} passed, {len(failed)} failed")
    for c in failed[:10]:
        lines.append(f"    x {c['name']}" + (f"[{c['key']}]" if c["key"] is not None else "") +
                     (f": {c['detail']}" if c["detail"] else ""))
    bad = [r for r in run["readings"] if r["error"]]
    if bad:
        lines.append(f"  unparseable readings: {len(bad)}")
    return "\n".join(lines)
