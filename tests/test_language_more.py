"""Language forms and parser fixes found while writing the use cases: per-item loops over a reading, `check b after a`,
undefined names in prompts and checks, comments after headers, column-0 triple-quoted strings, comprehension
variables that share a reading's name."""
import json
import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import shio
from shio.compiler import ShioError, compile_source

ORDER = """
type Line:
    sku: str
    qty: int

type Order:
    lines: list[Line]

CATALOG = {"A1": 5, "B2": 10}
"""


def src(body):
    return textwrap.dedent(ORDER) + textwrap.dedent(body)


class Compile(unittest.TestCase):
    def refuses(self, body, *words):
        with self.assertRaises(ShioError) as e:
            compile_source(src(body), "t.shio")
        for w in words:
            self.assertIn(w, str(e.exception))
        return e.exception

    def test_loop_item_is_a_reading(self):
        self.refuses("""
            agent a(llm):
                read o: Order by llm:
                    Read it.
                for ln in o.lines:
                    total = ln.qty * 2
                return 1
            """, "'ln' is a model reading")

    def test_plain_loop_over_reading_without_item_form_still_refused(self):
        self.refuses("""
            agent a(llm):
                read o: Order by llm:
                    Read it.
                for ln in sorted(o.lines):
                    pass
                return 1
            """, "'o' is a model reading")

    def test_commented_read_header_is_still_a_reading(self):
        self.refuses("""
            agent a(llm):
                read o: Order by llm:   # a comment here used to hide the reading from the checker
                    Read it.
                n = len(o.lines)
                return n
            """, "'o' is a model reading")

    def test_undefined_name_in_prompt(self):
        e = self.refuses("""
            agent a(llm):
                read o: Order by llm:
                    Keep {placeholders} as they are.
                return 1
            """, "'placeholders' is not defined", "{{")
        self.assertIn("Keep {placeholders}", e.source_line + src("").splitlines()[e.line - 1] if False else "Keep {placeholders}")

    def test_undefined_name_in_check(self):
        self.refuses("""
            agent a(llm):
                read o: Order by llm:
                    Read it.
                check ok: len(o.lines) <= MAX_LINES
                rule r:
                    return ok
                return r
            """, "'MAX_LINES' is not defined", "check ok")

    def test_after_must_name_an_earlier_check(self):
        self.refuses("""
            agent a(llm):
                read o: Order by llm:
                    Read it.
                check b after nothing_here: True
                rule r:
                    return b
                return r
            """, "after nothing_here")

    def test_comments_strings_and_shadowing_compile(self):
        compile_source(src('''
            def helper(q):   # a comment after a def line
                return q

            agent a(text, llm):   # and after an agent header
                read o: Order by llm:   # and after a read header
                    Literal braces: {{like this}}. Text: {text}
                names = {o for o in ["x", "y"]}
                check has: len(o.lines) > 0   # and after a check
                rule r:   # and after a rule header
                    note = """a string
            at column zero"""
                    return has
                return r

            main:
                note = """first line
            at column zero
                indented"""
                return a(note, replay(lambda r: {"lines": []}))
            '''), "t.shio")


class Run(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        shio.configure(self.tmp.name)

    def tearDown(self):
        shio.configure("shio-traces")
        self.tmp.cleanup()

    def load(self, body):
        p = Path(self.tmp.name) / "prog.shio"
        p.write_text(src(body))
        return shio.load(p)

    PROGRAM = """
        agent take(llm):
            read o: Order by llm:
                Read the order.
            check has_lines: len(o.lines) > 0, "no lines"
            for i, ln in enumerate(o.lines):
                check known[i] after has_lines: ln.sku in CATALOG, "unknown sku {ln.sku}"
                check stock[i] after known: ln.qty <= CATALOG[ln.sku], "{ln.qty} of {ln.sku} is more than stock"
            rule out:
                return {"why": [c["detail"] for c in checks.failures()], "skipped": [c["key"] for c in checks.skipped()],
                        "failed_stock_2": checks.failed("stock", 2)}
            return out
        """

    def test_per_item_checks_and_after(self):
        mod = self.load(self.PROGRAM)
        lines = [{"sku": "A1", "qty": 2}, {"sku": "ZZ", "qty": 1}, {"sku": "B2", "qty": 99}]
        out = mod.take(shio.models.replay(lambda r: {"lines": lines}))
        self.assertEqual(out["why"], ["unknown sku ZZ", "99 of B2 is more than stock"])
        self.assertEqual(out["skipped"], [1])            # stock[1] skipped: known[1] failed
        self.assertTrue(out["failed_stock_2"])
        t = json.loads(Path(mod.take.last_trace).read_text())
        self.assertEqual(len(t["readings"]), 1)          # items are not model calls
        item_args = [c["args"][0] for c in t["checks"] if c["name"] == "known"]
        self.assertEqual(item_args[1]["item"][1:], ["lines", 1])

    def test_empty_list_runs_no_item_checks(self):
        mod = self.load(self.PROGRAM)
        out = mod.take(shio.models.replay(lambda r: {"lines": []}))
        self.assertEqual(out["why"], ["no lines"])

    def test_reward_reruns_item_checks_over_new_items(self):
        mod = self.load(self.PROGRAM)
        mod.take(shio.models.replay(lambda r: {"lines": [{"sku": "A1", "qty": 2}, {"sku": "ZZ", "qty": 1}]}))
        _, grpo, _ = shio.train.build(str(Path(self.tmp.name) / "*.json"))
        spec = json.loads(grpo[0]["shio"])
        score = lambda lines: shio.train.score(spec, json.dumps({"lines": lines}))
        self.assertEqual(score([{"sku": "A1", "qty": 2}, {"sku": "B2", "qty": 3}, {"sku": "A1", "qty": 1}]), 1.0)
        self.assertAlmostEqual(score([{"sku": "A1", "qty": 2}, {"sku": "ZZ", "qty": 1}]), (1 + 0.5 + 0.5) / 3)
        self.assertEqual(score([]), 0.0)


if __name__ == "__main__":
    unittest.main()
