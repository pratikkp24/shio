"""python -m pytest tests  (or: python -m unittest discover tests)"""
import json
import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import shio
from shio.compiler import ShioError, compile_source

HEAD = """
type Amount:
    value: float?          # the amount
    as_printed: str

model m = replay(lambda req: {"value": 10.0, "as_printed": "10"})
"""


def program(body, head=HEAD):
    return textwrap.dedent(head) + textwrap.dedent(body)


def compiles(src):
    compile_source(src, "t.shio")


class Boundary(unittest.TestCase):
    """The compiler refuses programs in which a model reading decides something."""

    def refuses(self, body, words, at=None):
        src = program(body)
        with self.assertRaises(ShioError) as e:
            compiles(src)
        if at:   # the error points at the .shio line holding this text
            self.assertIn(at, src.splitlines()[e.exception.line - 1])
        for w in words:
            self.assertIn(w, str(e.exception))
        return e.exception

    def test_branch_on_reading(self):
        self.refuses("""
            agent a():
                read x: Amount by m:
                    How much?
                if x.value > 5:
                    return "big"
                return "small"
            """, ["'x' is a model reading", "plain code"], at="if x.value > 5")

    def test_return_reading(self):
        self.refuses("""
            agent a():
                read x: Amount by m:
                    How much?
                return x
            """, ["'x' is a model reading", "return value"])

    def test_assign_from_reading(self):
        self.refuses("""
            agent a():
                read x: Amount by m:
                    How much?
                y = x.value
                return 1
            """, ["'x' is a model reading"])

    def test_reading_in_prompt(self):
        self.refuses("""
            agent a():
                read x: Amount by m:
                    How much?
                read y: Amount by m:
                    Is it {x.value}?
                return 1
            """, ["'x' is a model reading", "seeing"])

    def test_reading_passed_to_function(self):
        self.refuses("""
            agent a():
                read x: Amount by m:
                    How much?
                print(x)
                return 1
            """, ["'x' is a model reading"])

    def test_read_outside_agent(self):
        self.refuses("""
            main:
                read x: Amount by m:
                    How much?
            """, ["only be used inside an agent"])

    def test_rule_must_return(self):
        self.refuses("""
            agent a():
                read x: Amount by m:
                    How much?
                rule r:
                    y = x.value
                return r
            """, ["must return"])

    def test_blind_cannot_see(self):
        self.refuses("""
            agent a():
                read x: Amount by m:
                    How much?
                read y: Amount by m blind seeing x:
                    How much?
                return 1
            """, ["blind"])

    def test_seeing_unknown(self):
        self.refuses("""
            agent a():
                read y: Amount by m seeing z:
                    How much?
                return 1
            """, ["not a reading"])

    def test_train_unknown_agent(self):
        self.refuses('\ntrain nobody on "t/*.json" with model "Qwen/Qwen3-0.6B"\n', ["not defined"])

    def test_syntax_error_points_at_shio_line(self):
        self.refuses("""
            agent a():
                read x: Amount by m:
                    How much?
                check ok: x.value >
                return 1
            """, ["invalid syntax"], at="check ok")

    def test_allowed_uses(self):
        compiles(program("""
            agent a():
                read x: Amount by m:
                    How much?
                read y: Amount by m seeing x:
                    Look again.
                check agree: x.value == y.value, "{x.value} vs {y.value}"
                when big: x.value is not None and x.value > 5
                if big and agree:
                    rule r:
                        return x.value
                    return r
                return None
            """))


class Runtime(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        shio.configure(self.tmp.name)

    def tearDown(self):
        shio.configure("shio-traces")
        self.tmp.cleanup()

    def load(self, src):
        p = Path(self.tmp.name) / "prog.shio"
        p.write_text(src)
        return shio.load(p)

    def test_run_and_trace(self):
        mod = self.load(program("""
            agent a(n):
                read x: Amount by m:
                    How much is {n}?
                read y: Amount by m blind:
                    How much?
                check agree: x.value == y.value
                when big: x.value > 5
                rule r:
                    return {"value": x.value, "ok": agree, "big": big}
                return r
            """))
        self.assertEqual(mod.a(3), {"value": 10.0, "ok": True, "big": True})
        t = json.loads(Path(mod.a.last_trace).read_text())
        self.assertEqual([r["name"] for r in t["readings"]], ["x", "y"])
        self.assertEqual(t["readings"][0]["request"]["ask"], "How much is 3?")
        self.assertTrue(t["readings"][1]["request"]["blind"])
        self.assertEqual([(c["name"], c["reward"]) for c in t["checks"]], [("agree", True), ("big", False)])

    def test_check_that_raises_fails(self):
        mod = self.load(program("""
            model none = replay(lambda req: {"value": None, "as_printed": ""})
            agent a():
                read x: Amount by none:
                    How much?
                check positive: x.value > 0
                rule r:
                    return positive
                return r
            """))
        self.assertIs(mod.a(), False)
        t = json.loads(Path(mod.a.last_trace).read_text())
        self.assertIn("could not be evaluated", t["checks"][0]["detail"])

    def test_loop_readings_do_not_leak(self):
        mod = self.load(program("""
            agent a():
                out = []
                for i in range(3):
                    when first: i == 0
                    if first:
                        read x: Amount by m:
                            How much?
                    rule r:
                        return x is not None
                    out.append(r)
                return out
            """))
        self.assertEqual(mod.a(), [True, False, False])

    def test_reading_is_opaque_at_runtime(self):
        @shio.agent
        def a():
            x = shio.read(float, "How much?", by=shio.models.replay(lambda r: {"value": 1.5}))
            return x.value
        with self.assertRaises(shio.BoundaryError):
            a()

        @shio.agent
        def b():
            x = shio.read(float, "How much?", by=shio.models.replay(lambda r: {"value": 1.5}))
            if x:
                return 1
        with self.assertRaises(shio.BoundaryError):
            b()

    def test_unparseable_reading_is_null(self):
        mod = self.load(program("""
            model bad = replay(lambda req: "not json")
            agent a():
                read x: Amount by bad:
                    How much?
                rule r:
                    return x is None
                return r
            """))
        self.assertTrue(mod.a())


class Check(unittest.TestCase):
    """`shio check` catches every compile-time error `shio run` would hit, as PATH:LINE: error: MSG."""

    def check(self, src):
        from shio.cli import main
        with TemporaryDirectory() as tmp:
            p = Path(tmp) / "t.shio"
            p.write_text(src)
            with self.assertRaises(SystemExit) as e:
                main(["check", str(p)])
            return str(e.exception.code).replace(str(p), "t.shio")

    def test_python_syntax_error_in_main(self):
        src = program("""
            main:
                x = = 2
            """)
        out = self.check(src)
        line = next(i for i, s in enumerate(src.splitlines(), 1) if "x = = 2" in s)
        self.assertTrue(out.startswith(f"t.shio:{line}: error: invalid syntax"), out)
        self.assertIn("x = = 2", out)

    def test_python_syntax_error_at_top_level(self):
        out = self.check("y = 1 +\n")
        self.assertTrue(out.startswith("t.shio:1: error: invalid syntax"), out)
        self.assertIn("y = 1 +", out)


class Training(unittest.TestCase):
    def test_checks_are_rewards(self):
        with TemporaryDirectory() as tmp:
            shio.configure(tmp)
            try:
                p = Path(tmp) / "inv.shio"
                p.write_text(program("""
                    agent a():
                        read x: Amount by m:
                            How much?
                        read lines: list[float] by m2:
                            Line items?
                        check adds_up: abs(sum(lines) - x.value) < 0.01
                        check printed: x.as_printed != ""
                        rule r:
                            return adds_up
                        return r
                    """, HEAD + 'model m2 = replay(lambda req: {"value": [4.0, 6.0]})\n'))
                mod = shio.load(p)
                self.assertTrue(mod.a())
                sft, grpo, stats = shio.train.build(str(Path(tmp) / "*.json"))
                self.assertEqual((stats["sft"], stats["grpo"]), (2, 2))
                spec = next(json.loads(g["shio"]) for g in grpo if g["shio"].count('"name"') == 2)
                score = lambda c: shio.train.score(spec, c)
                self.assertEqual(score('{"value": 10, "as_printed": "10"}'), 1.0)
                self.assertEqual(score('{"value": 11, "as_printed": "11"}'), 0.5)
                self.assertEqual(score('{"value": 10, "as_printed": ""}'), 0.5)
                self.assertEqual(score("ten"), 0.0)
            finally:
                shio.configure("shio-traces")


if __name__ == "__main__":
    unittest.main()
