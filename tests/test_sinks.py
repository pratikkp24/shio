"""Trace sinks: copies of every signed record to a JSONL file, SQL, S3 or OpenTelemetry. The local chain stays the
source of truth; a sink never breaks a run.

    python -m pytest tests/test_sinks.py
"""
import importlib.util
import json
import sqlite3
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import shio
from shio import ledger, trace


def run_agent(answer=7.0, name="pay"):
    m = shio.models.replay(lambda req: {"value": answer})

    @shio.agent
    def pay():
        x = shio.read(float, "How much?", by=m)
        positive(x)
        return settle(x)

    @shio.check
    def positive(v):
        return v is not None and v > 0, "no amount"

    @shio.rule
    def settle(checks, v):
        return v if checks.passed("positive") else None
    return pay(), pay.last_trace


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        shio.configure(str(self.dir / "traces"))

    def tearDown(self):
        trace.clear_sinks()
        shio.configure("shio-traces")
        self.tmp.cleanup()


class Jsonl(Base):
    def test_every_record_is_copied_as_written(self):
        s = trace.add_sink(trace.JsonlSink(self.dir / "out" / "records.jsonl"))
        run_agent(7.0)
        run_agent(-1.0)
        lines = [json.loads(x) for x in (self.dir / "out" / "records.jsonl").read_text().splitlines()]
        self.assertEqual(len(lines), 2)
        local = json.loads((self.dir / "traces" / lines[0]["file"]).read_text())
        self.assertEqual(lines[0]["integrity"]["sha256"], local["integrity"]["sha256"])   # the signed record itself
        self.assertEqual(getattr(s, "failures", 0), 0)


class Sql(Base):
    def test_rows_with_summary_columns_and_the_record(self):
        db = self.dir / "shio.db"
        trace.add_sink(trace.SqlSink(lambda: sqlite3.connect(db, check_same_thread=False)))
        run_agent(7.0)
        run_agent(-1.0)
        rows = sqlite3.connect(db).execute(
            "SELECT agent, checks_passed, checks_failed, readings, sha256, record FROM shio_records ORDER BY started").fetchall()
        self.assertEqual([(r[1], r[2], r[3]) for r in rows], [(1, 0, 1), (0, 1, 1)])
        self.assertTrue(rows[0][0].endswith("pay"))
        self.assertEqual(json.loads(rows[0][5])["integrity"]["sha256"], rows[0][4])

    def test_table_names_are_plain(self):
        with self.assertRaises(ValueError):
            trace.SqlSink(lambda: sqlite3.connect(":memory:"), table="x; DROP TABLE y")


class FakeS3:
    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body, ContentType, **kw):
        self.objects[(Bucket, Key)] = (json.loads(Body), kw)


class S3(Base):
    def test_one_object_per_record_under_agent_and_day(self):
        fake = FakeS3()
        trace.add_sink(trace.S3Sink("audit-bucket", prefix="prod/shio", client=fake, ServerSideEncryption="aws:kms"))
        run_agent()
        (bucket, key), (body, kw) = next(iter(fake.objects.items()))
        self.assertEqual(bucket, "audit-bucket")
        self.assertRegex(key, r"^prod/shio/[\w.<>]*pay/\d{4}/\d{2}/\d{2}/.+\.json$")
        self.assertEqual(kw, {"ServerSideEncryption": "aws:kms"})
        self.assertIn("integrity", body)


class FakeSpan:
    def __init__(self, tracer, name, context, attributes):
        self.name, self.parent, self.attributes, self.ended = name, context, dict(attributes or {}), False
        tracer.spans.append(self)

    def end(self, end_time=None):
        self.ended = True


class FakeTracer:
    def __init__(self):
        self.spans = []

    def start_span(self, name, context=None, attributes=None, start_time=None):
        return FakeSpan(self, name, context, attributes)


class Otel(Base):
    def test_a_span_per_run_with_children_and_no_model_text(self):
        t = FakeTracer()
        trace.add_sink(trace.OtelSink(tracer=t))
        run_agent(-1.0)
        names = [s.name for s in t.spans]
        root = next(s for s in t.spans if s.name.startswith("shio.agent"))
        self.assertTrue(any(n.startswith("shio.read") for n in names))
        self.assertTrue(any(n == "shio.check positive" for n in names))
        self.assertTrue(all(s.ended for s in t.spans))
        self.assertEqual((root.attributes["shio.checks_passed"], root.attributes["shio.checks_failed"]), (0, 1))
        self.assertNotIn("-1.0", json.dumps([s.attributes for s in t.spans]))     # names and outcomes, not model text
        children = [s for s in t.spans if s is not root]
        self.assertTrue(all(s.parent is not None for s in children))


class Failures(Base):
    def test_a_failing_sink_never_breaks_a_run_or_the_chain(self):
        class Broken:
            def emit(self, record, name):
                raise ConnectionError("database down")
        broken = trace.add_sink(Broken())
        good = trace.add_sink(trace.JsonlSink(self.dir / "ok.jsonl"))
        with self.assertLogs("shio.trace", level="WARNING"):
            out, _ = run_agent(7.0)
        self.assertEqual(out, 7.0)
        self.assertEqual(broken.failures, 1)
        self.assertEqual(len((self.dir / "ok.jsonl").read_text().splitlines()), 1)
        ok, report, _ = ledger.verify(self.dir / "traces")
        self.assertTrue(ok, report)

    def test_background_delivers_everything_off_the_run_thread(self):
        seen, threads = [], set()

        class Slow:
            def emit(self, record, name):
                threads.add(threading.current_thread().name)
                seen.append(name)
        b = trace.add_sink(trace.Background(Slow()))
        for _ in range(5):
            run_agent()
        b.flush()
        self.assertEqual(len(seen), 5)
        self.assertEqual(threads, {"shio-trace-sink"})

    def test_missing_extras_say_which_to_install(self):
        import sys
        for mod, make, extra in (("boto3", lambda: trace.S3Sink("b"), "shio-lang[s3]"),
                                 ("opentelemetry", lambda: trace.OtelSink(), "shio-lang[otel]")):
            saved = sys.modules.get(mod)
            sys.modules[mod] = None
            try:
                with self.assertRaises(RuntimeError) as e:
                    make()
                self.assertIn(extra, str(e.exception))
            finally:
                if saved is None:
                    del sys.modules[mod]
                else:
                    sys.modules[mod] = saved


@unittest.skipUnless(importlib.util.find_spec("cryptography"), "needs the [regulated] extra (cryptography)")
class Vault(Base):
    def test_personal_data_stays_in_the_local_vault(self):
        shio.configure(str(self.dir / "traces"), vault=True)
        trace.add_sink(trace.JsonlSink(self.dir / "copy.jsonl"))
        m = shio.models.replay(lambda req: {"value": 1.0})

        @shio.agent
        def a(note):
            x = shio.read(float, "How much?", by=m, sources=[shio.Source("note", text=note)])
            return 1
        a("Customer Jane Roe, account GB29NWBK60161331926819")
        copy = json.loads((self.dir / "copy.jsonl").read_text().splitlines()[0])
        self.assertNotIn("Jane Roe", json.dumps(copy))           # the free text is in the vault, not the copy
        self.assertIn("vault", copy)                              # the copy only points at the local, erasable vault
        self.assertTrue((self.dir / "traces" / copy["vault"]["file"]).exists())


class PublicAPI(unittest.TestCase):
    def test_every_documented_name_exists(self):
        from shio import guard, models
        for n in shio.__all__:
            self.assertTrue(hasattr(shio, n), n)
        for mod, names in ((models, ["ollama", "openai_compat", "anthropic", "replay", "messages_for"]),
                           (guard, ["Guard", "Tool", "Decision", "Session"]),
                           (trace, ["add_sink", "remove_sink", "clear_sinks", "JsonlSink", "SqlSink", "S3Sink",
                                    "OtelSink", "Background", "load", "summary"])):
            for n in names:
                self.assertTrue(hasattr(mod, n), f"{mod.__name__}.{n}")
