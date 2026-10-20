"""Async agents (async def / `async agent`, awaited reads) and `shio label` (reviewers' verdicts in the chain).

    python -m pytest tests/test_async_and_labels.py
"""
import asyncio
import contextlib
import io
import json
import textwrap
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import shio
from shio import cli, ledger, train, trace

PROGRAM = """
import time

def slow_answer(req):
    time.sleep(0.2)
    return {"value": 5.0}

type Amount:
    value: float?

model slow = replay(slow_answer)

async agent price(item: str):
    read p: Amount by slow:
        Price of {item}?
    check known: p.value is not None, "no price"
    rule settle:
        return p.value if not checks.failures() else None
    return settle
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        shio.configure(str(self.dir / "traces"))

    def tearDown(self):
        shio.configure("shio-traces")
        self.tmp.cleanup()


class Async(Base):
    def load(self):
        (self.dir / "p.shio").write_text(textwrap.dedent(PROGRAM))
        return shio.load(self.dir / "p.shio", name="shio_async_p")

    def test_async_agents_run_concurrently_and_each_is_traced(self):
        mod = self.load()

        async def go():
            t = time.time()
            out = await asyncio.gather(*[mod.price(f"item{i}") for i in range(5)])
            return out, time.time() - t
        out, dt = asyncio.run(go())
        self.assertEqual(out, [5.0] * 5)
        self.assertLess(dt, 0.8)                         # five 0.2 s reads, not one after another
        runs = list(trace.load(str(self.dir / "traces")))
        self.assertEqual(len([r for r in runs if r["agent"].endswith("price")]), 5)
        self.assertTrue(all(len(r["readings"]) == 1 for r in runs))     # each task kept its own reading

    def test_the_boundary_holds_in_async_agents(self):
        from shio.compiler import ShioError, compile_source
        with self.assertRaises(ShioError):
            compile_source(textwrap.dedent(PROGRAM).replace("    return settle", "    return p"), "t.shio")

    def test_python_api_async_agent_and_apropose(self):
        class AsyncModel:
            name = "async-model"

            async def apropose(self, req):
                await asyncio.sleep(0.01)
                return '{"value": 2.5}'

            def propose(self, req):
                raise AssertionError("an async agent should await apropose")

        @shio.agent
        async def a():
            x = await shio.aread(float, "How much?", by=AsyncModel())
            return settle(x)

        @shio.rule
        def settle(checks, v):
            return v
        self.assertEqual(asyncio.run(a()), 2.5)

    def test_a_failing_async_model_gives_a_null_reading(self):
        class Down:
            async def apropose(self, req):
                raise ConnectionError("down")

        @shio.agent
        async def a():
            x = await shio.aread(float, "?", by=Down())
            known(x)
            return settle(x)

        @shio.check
        def known(v):
            return v is not None

        @shio.rule
        def settle(checks, v):
            return "HELD" if checks.failed("known") else v
        self.assertEqual(asyncio.run(a()), "HELD")


class Labels(Base):
    def decide(self, value):
        m = shio.models.replay(lambda req: {"value": value})

        @shio.agent
        def pay():
            x = shio.read(float, "How much?", by=m)
            positive(x)
            return settle(x)

        @shio.check
        def positive(v):
            return v is not None and v > 0

        @shio.rule
        def settle(checks, v):
            return v
        pay()
        return pay.last_trace

    def test_a_label_is_a_verified_entry_bound_to_the_record(self):
        rec = self.decide(7.0)
        e = ledger.label(rec, "wrong", "omar", "paid twice")
        self.assertEqual((e["kind"], e["verdict"], e["by"]), ("label", "wrong", "omar"))
        ok, report, _ = ledger.verify(rec.parent)
        self.assertTrue(ok, report)
        self.assertEqual(ledger.labels(rec.parent)[rec.name][-1]["note"], "paid twice")
        with self.assertRaises(ValueError):
            ledger.label(rec, "maybe", "omar")
        with self.assertRaises(ValueError):
            ledger.label(rec.parent / "not-a-record.json", "right", "omar")

    def test_an_edited_label_fails_verification(self):
        rec = self.decide(7.0)
        ledger.label(rec, "right", "omar")
        led = rec.parent / ledger.LEDGER
        lines = led.read_text().splitlines()
        last = json.loads(lines[-1])
        last["verdict"] = "wrong"
        lines[-1] = json.dumps(last, sort_keys=True)
        led.write_text("\n".join(lines) + "\n")
        ok, report, _ = ledger.verify(rec.parent)
        self.assertFalse(ok)
        self.assertTrue(any("label entry edited" in r for r in report))

    def test_traces_carry_labels_and_training_leaves_out_wrong_decisions(self):
        good, bad = self.decide(7.0), self.decide(9.0)
        ledger.label(bad, "wrong", "omar", "wrong vendor")
        runs = {r["outcome"]: r for r in trace.load(str(good.parent))}
        self.assertEqual(runs[9.0]["labels"][-1]["verdict"], "wrong")
        self.assertEqual(runs[7.0]["labels"], [])
        sft, _, stats = train.build(str(good.parent / "*.json"))
        self.assertEqual((len(sft), stats["labelled_wrong"]), (1, 1))

    def test_the_cli(self):
        rec = self.decide(7.0)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["label", str(rec), "--right", "--by", "omar", "--note", "checked"])
        self.assertIn("labelled", out.getvalue())
        self.assertEqual(ledger.labels(rec.parent)[rec.name][-1]["verdict"], "right")
