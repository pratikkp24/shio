"""Robustness: model backends over HTTP (no real model: a local stub server), digest pinning, keys, odd replies,
threads, memory, and reading traces back.

    python -m pytest tests/test_robustness.py
"""
import gc
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

import shio
from shio import core, trace
from shio.models import messages_for, ollama, openai_compat


class Stub:
    """A local HTTP server that answers like an OpenAI-compatible endpoint (and Ollama's /api/tags)."""

    def __init__(self, reply='{"value": 7.5}', status=200, digest="sha256:" + "ab" * 32):
        self.reply, self.status, self.digest, self.requests = reply, status, digest, []
        stub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                body = json.dumps({"models": [{"name": "m:1", "model": "m:1", "digest": stub.digest}]}).encode()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                stub.requests.append({"path": self.path, "headers": dict(self.headers),
                                      "body": json.loads(self.rfile.read(n))})
                self.send_response(stub.status)
                self.end_headers()
                if stub.status == 200:
                    self.wfile.write(json.dumps({"choices": [{"message": {"content": stub.reply}}]}).encode())

        self.server = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        shio.configure(self.tmp.name)

    def tearDown(self):
        shio.configure("shio-traces")
        self.tmp.cleanup()

    def ask(self, model, schema=float):
        """Run one read through a check and a rule; -> (outcome, the trace text)."""
        @shio.agent
        def a():
            x = shio.read(schema, "How much?", by=model)
            known(x)
            return settle(x)

        @shio.check
        def known(v):
            return v is not None

        @shio.rule
        def settle(checks, v):
            return v if checks.passed("known") else "HELD"
        out = a()
        return out, a.last_trace.read_text()


class Backends(Base):
    def test_structured_output_is_requested_by_default_and_can_be_turned_off(self):
        s = Stub()
        try:
            self.assertEqual(self.ask(openai_compat("m:1", s.url + "/v1"))[0], 7.5)
            self.assertEqual(s.requests[-1]["body"]["response_format"]["type"], "json_schema")
            self.assertEqual(s.requests[-1]["path"], "/v1/chat/completions")
            self.ask(openai_compat("m:1", s.url + "/v1", structured=False))
            self.assertNotIn("response_format", s.requests[-1]["body"])
        finally:
            s.close()

    def test_the_key_is_sent_but_never_traced(self):
        s = Stub()
        import os
        os.environ["SHIO_TEST_KEY"] = "sk-test-do-not-log-123456"
        try:
            out, text = self.ask(openai_compat("m:1", s.url + "/v1", key_env="SHIO_TEST_KEY"))
            self.assertEqual(out, 7.5)
            self.assertEqual(s.requests[-1]["headers"]["Authorization"], "Bearer sk-test-do-not-log-123456")
            self.assertNotIn("sk-test-do-not-log", text)
            for f in Path(self.tmp.name).rglob("*"):
                if f.is_file():
                    self.assertNotIn(b"sk-test-do-not-log", f.read_bytes(), f.name)
        finally:
            del os.environ["SHIO_TEST_KEY"]
            s.close()

    def test_a_missing_key_gives_a_null_reading_not_a_crash(self):
        out, text = self.ask(openai_compat("m:1", "http://127.0.0.1:9/v1", key_env="SHIO_TEST_KEY_UNSET"))
        self.assertEqual(out, "HELD")
        self.assertIn("SHIO_TEST_KEY_UNSET is not set", text)

    def test_server_errors_and_unreachable_hosts_hold(self):
        s = Stub(status=500)
        try:
            self.assertEqual(self.ask(openai_compat("m:1", s.url + "/v1"))[0], "HELD")
        finally:
            s.close()
        self.assertEqual(self.ask(openai_compat("m:1", "http://127.0.0.1:9/v1", timeout=2))[0], "HELD")


class DigestPinning(Base):
    # Ollama reports a bare hex digest; the stub reports "sha256:<hex>", as some proxies do. Both must match a pin.
    def test_the_pinned_weights_answer(self):
        s = Stub()
        try:
            m = ollama("m:1", host=s.url, digest="sha256:" + "ab" * 32)
            out, text = self.ask(m)
            self.assertEqual(out, 7.5)
            self.assertIn("ab" * 32, text)            # the served digest is recorded with the decision
        finally:
            s.close()

    def test_other_weights_are_refused(self):
        s = Stub(digest="sha256:" + "cd" * 32)
        try:
            out, text = self.ask(ollama("m:1", host=s.url, digest="ab" * 32))
            self.assertEqual(out, "HELD")
            self.assertIn("refused", text)
            self.assertEqual(s.requests, [])           # the model was never asked
        finally:
            s.close()

    def test_a_too_short_pin_or_an_unknown_digest_is_refused(self):
        s = Stub()
        try:
            self.assertEqual(self.ask(ollama("m:1", host=s.url, digest="abab"))[0], "HELD")
        finally:
            s.close()
        self.assertEqual(self.ask(ollama("m:1", host="http://127.0.0.1:9", digest="ab" * 32))[0], "HELD")


class OddReplies(Base):
    def test_replies_that_are_not_the_value_hold(self):
        for raw in ("not json", "[]", '{"value": "seven"}', '{"wrong": 1}', "", '{"value": null}', "{" * 5000,
                    '{"value": 1e999}'):
            with self.subTest(raw=raw[:30]):
                out, _ = self.ask(shio.models.replay(lambda req, r=raw: r))
                self.assertIn(out, ("HELD", float("inf")))   # 1e999 parses as inf: a value, which the program's checks judge
                if raw != '{"value": 1e999}':
                    self.assertEqual(out, "HELD")

    def test_a_model_that_raises_holds(self):
        def boom(req):
            raise ConnectionError("down")
        self.assertEqual(self.ask(shio.models.replay(boom))[0], "HELD")

    def test_identity_errors_are_recorded_not_fatal(self):
        class Odd:
            name = "odd"

            def identity(self):
                raise ValueError("no identity")

            def propose(self, req):
                return '{"value": 2.0}'
        out, text = self.ask(Odd())
        self.assertEqual(out, 2.0)
        self.assertIn("identity_error", text)


class Prompts(unittest.TestCase):
    def test_messages_carry_context_and_sources_but_not_empty_sources(self):
        req = core.ReadRequest(name="r", ask="Ask?", system="sys", json_schema={"type": "number"}, blind=False, meta={},
                               sources=[shio.Source("doc", text="the text"), shio.Source("empty", text="")],
                               context={"earlier": 1})
        msgs = messages_for(req)
        self.assertEqual(msgs[0], {"role": "system", "content": "sys"})
        user = msgs[1]["content"]
        self.assertIn('<source name="doc">\nthe text\n</source>', user)
        self.assertNotIn('name="empty"', user)
        self.assertIn('"earlier": 1', user)
        self.assertTrue(user.rstrip().endswith('{"type": "number"}'))


class Threads(Base):
    def test_a_check_open_in_one_thread_does_not_open_readings_in_another(self):
        m = shio.models.replay(lambda req: {"value": 3.0})
        inside, outside, results = threading.Event(), threading.Event(), {}

        @shio.agent
        def slow():
            x = shio.read(float, "?", by=m)
            hold(x)
            return 1

        @shio.check
        def hold(v):
            inside.set()
            outside.wait(5)          # stay inside the check while the other thread pokes its own reading
            return True

        @shio.agent
        def poke():
            y = shio.read(float, "?", by=m)
            inside.wait(5)
            try:
                y.value
                results["poke"] = "LEAKED"
            except shio.BoundaryError:
                results["poke"] = "blocked"
            finally:
                outside.set()
            return 1

        t = threading.Thread(target=slow)
        t.start()
        poke()
        t.join(10)
        self.assertEqual(results["poke"], "blocked")

    def test_parallel_agents_keep_their_own_readings(self):
        out = {}

        def run(i):
            m = shio.models.replay(lambda req, i=i: {"value": float(i)})

            @shio.agent
            def a():
                x = shio.read(float, "?", by=m)
                return settle(x)

            @shio.rule
            def settle(checks, v):
                return v
            out[i] = a()
        ts = [threading.Thread(target=run, args=(i,)) for i in range(16)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(30)
        self.assertEqual(out, {i: float(i) for i in range(16)})


class Memory(Base):
    def test_memory_does_not_grow_across_runs(self):
        """Shio keeps the most recent run (for `shio replay`); every earlier run's sealed readings are released."""
        m = shio.models.replay(lambda req: {"value": 1.0})

        @shio.agent
        def a():
            for _ in range(200):
                shio.read(float, "?", by=m)
            return 1
        a()
        gc.collect()
        after_one = len(core._SEALED)
        for _ in range(5):
            a()
        gc.collect()
        self.assertLessEqual(len(core._SEALED), after_one)


class Escapes(Base):
    def test_a_reading_kept_after_its_agent_ends_stays_sealed(self):
        kept = []
        m = shio.models.replay(lambda req: {"value": 9.0})

        @shio.agent
        def a():
            kept.append(shio.read(float, "?", by=m))     # the opaque object may be stored; its value may not be read
            return 1
        a()
        with self.assertRaises(shio.BoundaryError):
            kept[0].value

    def test_an_agent_cannot_return_a_reading_inside_a_container(self):
        m = shio.models.replay(lambda req: {"value": 9.0})
        for wrap in (lambda x: [x], lambda x: {"k": x}, lambda x: (1, (x,)), lambda x: {x}):
            @shio.agent
            def a(wrap=wrap):
                return wrap(shio.read(float, "?", by=m))
            with self.subTest(wrap=wrap):
                with self.assertRaises(shio.BoundaryError):
                    a()


class Traces(Base):
    def test_load_flattens_children_skips_other_json_and_summarises(self):
        m = shio.models.replay(lambda req: "not json")

        @shio.agent
        def child():
            x = shio.read(float, "?", by=m)
            known(x)
            return 1

        @shio.check
        def known(v):
            return v is not None, "no value"

        @shio.agent
        def parent():
            return child()
        parent()
        (Path(self.tmp.name) / "other.json").write_text('{"not": "a run"}')
        runs = list(trace.load(self.tmp.name))
        self.assertEqual([r["agent"].rsplit(".", 1)[-1] for r in runs], ["parent", "child"])
        text = trace.summary(runs[1])
        self.assertIn("1 failed", text)
        self.assertIn("x known: no value", text)
        self.assertIn("unparseable readings: 1", text)
