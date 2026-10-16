"""Evidence packs: one folder per audit period with integrity, decisions, oversight, monitoring, policies and
decision maps, and a manifest that shows whether anything in it changed. Monitoring needs no labels: a check's
weekly pass rate falling, a model's weights changing mid-period and model calls failing are all flagged."""
import datetime as dt
import json
import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import shio
from shio import ledger
from shio.evidence import build, check_manifest

PROGRAM = """
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
"""


class Model:
    """A stand-in for a served model: answers from a function, reports a given identity."""

    def __init__(self, grams, digest="aaaa1111", fail=False):
        self.grams, self.digest, self.fail, self.name = grams, digest, fail, "valuer"

    def identity(self):
        return {"kind": "ollama", "model": "valuer:8b", "digest": self.digest, "pinned": self.digest}

    def propose(self, req):
        if self.fail:
            raise TimeoutError("no answer")
        return json.dumps({"grams": self.grams})


class Evidence(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "weigh.shio").write_text(textwrap.dedent(PROGRAM))
        self.signer = ledger.load_signer("hmac:k")
        shio.configure(str(self.dir / "records"), signer=self.signer, policies=False)
        self.mod = shio.load(self.dir / "weigh.shio")

    def tearDown(self):
        shio.configure("shio-traces", vault=False, signer=False, policies=False)
        self.tmp.cleanup()

    def week(self, start, models):
        for i, m in enumerate(models):
            day = start + dt.timedelta(days=i % 7)
            with mock.patch("shio.core.time.time", return_value=dt.datetime(day.year, day.month, day.day, 9).timestamp()):
                self.mod.weigh("Net 40 g", m)

    def pack(self, **kw):
        return build(self.dir / "records", self.dir / "pack", dt.date(2026, 7, 1), dt.date(2026, 9, 30),
                     secret=b"k", signer=self.signer, **kw)

    def test_a_quiet_quarter(self):
        for w in range(4):
            self.week(dt.date(2026, 7, 6) + dt.timedelta(weeks=w), [Model(40)] * 5)
        data, out = self.pack()
        self.assertEqual(data["decisions"], 20)
        self.assertTrue(data["integrity"]["verified"])
        self.assertEqual(data["monitoring"]["flags"], [])
        report = (out / "evidence.md").read_text()
        self.assertIn("VERIFIED", report)
        self.assertIn("weigh.on_slip | 100%", report)
        self.assertTrue(list((out / "programs").glob("weigh-*.md")))     # the decision map of the program version
        ok, problems, state = check_manifest(out, secret=b"k")
        self.assertTrue(ok, problems)
        self.assertEqual(state, "ok")

    def test_drift_weights_and_failures_are_flagged(self):
        for w in range(3):
            self.week(dt.date(2026, 7, 6) + dt.timedelta(weeks=w), [Model(40)] * 5)
        self.week(dt.date(2026, 7, 27), [Model(48, digest="bbbb2222")] * 4 + [Model(40, fail=True)] * 2)
        data, _ = self.pack()
        flags = "\n".join(data["monitoring"]["flags"])
        self.assertIn("weigh.on_slip: pass rate 0% in 2026-W31, down from 100% before it", flags)
        self.assertIn("2 different weights", flags)
        self.assertIn("gave no usable answer", flags)

    def test_a_changed_pack_is_detected(self):
        self.week(dt.date(2026, 7, 6), [Model(40)] * 3)
        _, out = self.pack()
        report = out / "evidence.md"
        report.write_text(report.read_text().replace("VERIFIED", "VERIFIED, no issues"))
        ok, problems, _ = check_manifest(out, secret=b"k")
        self.assertFalse(ok)
        self.assertIn("evidence.md: changed", problems)

    def test_only_the_period_counts(self):
        self.week(dt.date(2026, 6, 1), [Model(40)] * 3)
        self.week(dt.date(2026, 7, 6), [Model(40)] * 2)
        data, _ = self.pack()
        self.assertEqual(data["decisions"], 2)


if __name__ == "__main__":
    unittest.main()
