"""Images and PDFs: sent as files to models that read them, refused by models that can't.

    python -m pytest tests/test_media_sources.py
"""
import base64
import importlib.util
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import shio
from shio import Source
from shio.models import anthropic, openai_compat

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


class FakeAnthropic:
    def __init__(self):
        self.calls = []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        block = type("B", (), {"type": "text", "text": '{"value": 98.7}'})()
        return type("R", (), {"content": [block], "stop_reason": "end_turn"})()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        shio.configure(str(self.dir / "traces"))
        self.png = self.dir / "receipt.png"
        self.png.write_bytes(PNG)

    def tearDown(self):
        shio.configure("shio-traces")
        self.tmp.cleanup()

    def ask(self, model, source):
        @shio.agent
        def a():
            x = shio.read(float, "What is the total?", by=model, sources=[source])
            known(x)
            return settle(x)

        @shio.check
        def known(v):
            return v is not None

        @shio.rule
        def settle(checks, v):
            return v if checks.passed("known") else "HELD"
        return a(), a.last_trace


class Sources(Base):
    def test_image_sources_know_their_type(self):
        s = Source.image(self.png)
        self.assertEqual((s.name, s.media_type, s.text), ("receipt.png", "image/png", None))
        with self.assertRaises(ValueError):
            Source.image(self.dir / "notes.txt")

    def test_the_trace_records_the_file_hash(self):
        fake = FakeAnthropic()
        _, path = self.ask(anthropic(client=fake), Source.image(self.png))
        src = next(iter(json.loads(path.read_text())["sources"].values()))
        import hashlib
        self.assertEqual((src["media_type"], src["sha256"]), ("image/png", hashlib.sha256(PNG).hexdigest()))


class Claude(Base):
    def test_an_image_goes_as_an_image_block_before_the_prompt(self):
        fake = FakeAnthropic()
        out, _ = self.ask(anthropic(client=fake), Source.image(self.png))
        self.assertEqual(out, 98.7)
        content = fake.calls[-1]["messages"][-1]["content"]
        self.assertEqual(content[0]["type"], "image")
        self.assertEqual(content[0]["source"], {"type": "base64", "media_type": "image/png",
                                                "data": base64.standard_b64encode(PNG).decode()})
        self.assertEqual(content[-1]["type"], "text")

    @unittest.skipUnless(importlib.util.find_spec("pypdf"), "needs the [pdf] extra")
    def test_a_pdf_goes_as_a_document_and_not_also_as_text(self):
        from pypdf import PdfWriter
        w = PdfWriter()
        w.add_blank_page(width=200, height=200)
        pdf = self.dir / "bill.pdf"
        with open(pdf, "wb") as f:
            w.write(f)
        fake = FakeAnthropic()
        self.ask(anthropic(client=fake), Source.from_pdf(pdf))
        content = fake.calls[-1]["messages"][-1]["content"]
        self.assertEqual((content[0]["type"], content[0]["title"], content[0]["source"]["media_type"]),
                         ("document", "bill.pdf", "application/pdf"))
        self.assertNotIn('<source name="bill.pdf">', content[-1]["text"])


class OpenAICompatible(Base):
    def test_a_text_only_model_given_an_image_fails_the_read(self):
        out, path = self.ask(openai_compat("m", "http://127.0.0.1:9/v1", retries=0), Source.image(self.png))
        self.assertEqual(out, "HELD")
        self.assertIn("can't read receipt.png", path.read_text())

    def test_a_vision_model_gets_the_image_as_a_data_url(self):
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer
        seen = []

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                seen.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({"choices": [{"message": {"content": '{"value": 12.5}'}}]}).encode())
        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            out, _ = self.ask(openai_compat("llava", f"http://127.0.0.1:{srv.server_port}/v1", vision=True),
                              Source.image(self.png))
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertEqual(out, 12.5)
        parts = seen[-1]["messages"][-1]["content"]
        self.assertEqual(parts[0]["type"], "text")
        self.assertTrue(parts[1]["image_url"]["url"].startswith("data:image/png;base64,"))


class InShio(Base):
    def test_image_is_available_in_programs(self):
        from shio.compiler import compile_source
        compile_source('model m = replay(lambda req: {"value": 1.0})\n\nagent a(path):\n'
                       '    read x: float from image(path) by m:\n        Total?\n'
                       '    rule r:\n        return x\n    return r\n', "t.shio")
