"""shio monitor: the evidence pack's label-free flags over a rolling window, for alerts while a period runs. A check's
pass rate falling in the window against the weeks before it, the model's weights changing, model calls failing and
sign-offs left waiting are flagged; the result can be posted as JSON to a webhook, and --exit-code fails for cron."""
import contextlib
import datetime as dt
import io
import json
import textwrap
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import shio
from shio import ledger
from shio.cli import main
from shio.evidence import monitor

PROGRAM = """
LIMIT = 100

type Slip:
    grams: float

agent weigh(slip: str, llm):
    read s: Slip by llm:
        Read the weight.

        {slip}
    check on_slip: str(int(s.grams)) in slip, "{s.grams} g is not on the slip"
    rule decision:
        if s is None:
            return {"status": "REVIEW"}
        return {"status": "OK" if not checks.failures() else "REVIEW"}
    return decision

agent pay(slip: str, submitted_by: str, llm):
    read s: Slip by llm:
        Read the weight.

        {slip}
    signoff PAY when amount > LIMIT by role "finance_controller" maker submitted_by
    rule decide:
        return {"action": "PAY", "amount": s.grams * 10, "submitted_by": submitted_by}
    return decide
"""

TODAY = dt.date(2026, 10, 16)       # a Friday; the default window is 2026-09-19 to 2026-10-16


class Model:
    def __init__(self, grams, digest="aaaa1111", fail=False):
        self.grams, self.digest, self.fail, self.name = grams, digest, fail, "valuer"

    def identity(self):
        return {"kind": "ollama", "model": "valuer:8b", "digest": self.digest, "pinned": self.digest}

    def propose(self, req):
        if self.fail:
            raise TimeoutError("no answer")
        return json.dumps({"grams": self.grams})


def on(day):
    return mock.patch("shio.core.time.time", return_value=dt.datetime(day.year, day.month, day.day, 9).timestamp())


class Hook(BaseHTTPRequestHandler):
    received = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        Hook.received.append((self.headers["Content-Type"], json.loads(body)))
        self.send_response(204)
        self.end_headers()

    def log_message(self, *a):
        pass


class Monitor(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "m.shio").write_text(textwrap.dedent(PROGRAM))
        self.records = self.dir / "records"
        self.signer = ledger.load_signer("hmac:k")
        shio.configure(str(self.records), signer=self.signer, policies=False, actor="meera")
        self.mod = shio.load(self.dir / "m.shio")

    def tearDown(self):
        shio.configure("shio-traces", vault=False, signer=False, policies=False, actor="")
        self.tmp.cleanup()

    def week(self, monday, models):
        for i, m in enumerate(models):
            with on(monday + dt.timedelta(days=i % 5)):
                self.mod.weigh("Net 40 g", m)

    def weeks(self, first_monday, n, models):
        for w in range(n):
            self.week(first_monday + dt.timedelta(weeks=w), models)

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as e:
            main(["monitor", str(self.records), "--today", TODAY.isoformat(), *argv])
            raise SystemExit(0)
        return e.exception.code, out.getvalue(), err.getvalue()

    def test_quiet_window(self):
        self.weeks(dt.date(2026, 8, 3), 11, [Model(40)] * 4)
        result = monitor(self.records, today=TODAY)
        self.assertEqual(result["flags"], [])
        self.assertEqual(result["window"], ["2026-09-19", "2026-10-16"])
        self.assertGreater(result["decisions"], 0)
        self.assertEqual(self.cli("--exit-code")[0], 0)

    def test_a_drop_in_the_window_is_flagged_but_one_in_the_baseline_is_not(self):
        self.weeks(dt.date(2026, 8, 3), 3, [Model(40)] * 4)
        self.week(dt.date(2026, 8, 24), [Model(48)] * 4)             # W35: a drop before the window
        self.weeks(dt.date(2026, 8, 31), 5, [Model(40)] * 4)
        self.assertEqual(monitor(self.records, today=TODAY)["flags"], [])
        self.week(dt.date(2026, 10, 12), [Model(48)] * 4)            # W42: in the window
        result = monitor(self.records, today=TODAY)
        self.assertEqual(len(result["alerts"]), 1)
        alert = result["alerts"][0]
        self.assertEqual((alert["kind"], alert["check"], alert["week"], alert["pass_rate"]),
                         ("check_drop", "weigh.on_slip", "2026-W42", 0.0))
        self.assertIn("weigh.on_slip: pass rate 0% in 2026-W42", alert["message"])

    def test_new_weights_and_failures(self):
        self.weeks(dt.date(2026, 8, 3), 5, [Model(40)] * 4)
        self.weeks(dt.date(2026, 9, 21), 4, [Model(40, digest="bbbb2222")] * 4)    # changed at the window's start
        self.week(dt.date(2026, 10, 12), [Model(40, digest="bbbb2222", fail=True)] * 2)
        result = monitor(self.records, today=TODAY)
        kinds = {a["kind"]: a for a in result["alerts"]}
        self.assertEqual(kinds["model_weights"]["digests"], ["bbbb2222"])
        self.assertEqual(kinds["model_weights"]["before"], ["aaaa1111"])
        self.assertIn("now served with weights bbbb2222, not aaaa1111", kinds["model_weights"]["message"])
        self.assertEqual((kinds["model_failures"]["failures"], kinds["model_failures"]["reads"]), (2, 18))

    def test_old_pending_signoffs(self):
        with on(dt.date(2026, 10, 2)):
            self.mod.pay("Net 40 g", "meera", Model(40))            # 400 > LIMIT: waits for a person
        old = Path(self.mod.pay.last_trace)
        with on(dt.date(2026, 10, 3)):
            self.mod.pay("Net 40 g", "meera", Model(40))
        signed = Path(self.mod.pay.last_trace)
        ledger.signoff(signed, "Priya Nair", "finance_controller", "approved for payment", self.signer)
        with on(dt.date(2026, 10, 15)):
            self.mod.pay("Net 40 g", "meera", Model(40))            # pending, but only for a day
        result = monitor(self.records, today=TODAY)
        self.assertEqual(len(result["pending"]), 2)
        flagged = [a for a in result["alerts"] if a["kind"] == "pending_signoff"]
        self.assertEqual([(a["file"], a["age_days"], a["roles"]) for a in flagged],
                         [(old.name, 14, ["finance_controller"])])
        self.assertEqual(monitor(self.records, today=TODAY, pending_days=20)["alerts"], [])

    def test_webhook_payload_and_exit_code(self):
        self.weeks(dt.date(2026, 9, 21), 3, [Model(40)] * 4)
        server = HTTPServer(("127.0.0.1", 0), Hook)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{server.server_port}/alerts"
        Hook.received.clear()
        try:
            code, _, _ = self.cli("--webhook", url, "--exit-code")
            self.assertEqual((code, Hook.received), (0, []))          # nothing flagged: nothing posted
            self.week(dt.date(2026, 10, 12), [Model(48)] * 4)
            code, out, err = self.cli("--webhook", url, "--exit-code")
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(code, 1)
        self.assertIn("FLAG: weigh.on_slip: pass rate 0% in 2026-W42", out)
        self.assertIn("posted to webhook (204)", err)
        ctype, payload = Hook.received[0]
        self.assertEqual(ctype, "application/json")
        self.assertEqual(payload["source"], "shio monitor")
        self.assertEqual(payload["window"], ["2026-09-19", "2026-10-16"])
        self.assertEqual(payload["alerts"][0]["kind"], "check_drop")
        self.assertEqual(payload["flags"], [a["message"] for a in payload["alerts"]])
        self.assertIn("weigh.on_slip", payload["rates"])

    def test_a_failed_webhook_fails_the_run(self):
        self.week(dt.date(2026, 9, 21), [Model(40)] * 4)
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("connection refused")) as hook:
            code, _, err = self.cli("--webhook", "https://alerts.example.com/x", "--always-post")
        self.assertEqual(code, 2)
        self.assertIn("webhook failed", err)
        req = hook.call_args[0][0]
        self.assertEqual((req.get_method(), req.full_url), ("POST", "https://alerts.example.com/x"))
        self.assertEqual(json.loads(req.data)["flags"], [])


if __name__ == "__main__":
    unittest.main()
