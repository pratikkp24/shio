"""Ways to get a model reading into plain code without naming it, or out of a check. Each one used to work.

    python -m pytest tests/test_boundary_escapes.py
"""
import copy
import pickle
import textwrap
import unittest
from tempfile import TemporaryDirectory

import shio
from shio.compiler import ShioError, compile_source

HEAD = """
type Amount:
    value: float?
    as_printed: str

model m = replay(lambda req: {"value": 4242.5, "as_printed": "4242.5"})
LEAK = []

agent a():
    read x: Amount by m:
        How much?
"""


def program(body, imports=""):
    return imports + textwrap.dedent(HEAD) + textwrap.indent(textwrap.dedent(body), "    ")


class Reflection(unittest.TestCase):
    """The compiler checks a reading's name; these reach it without the name, so they are refused in Shio code."""

    def refuses(self, body, word, imports=""):
        with self.assertRaises(ShioError) as e:
            compile_source(program(body, imports), "t.shio")
        self.assertIn(word, str(e.exception))

    def test_reflective_builtins(self):
        for body, word in (("return locals()['x']", "'locals'"), ("return vars()['x']", "'vars'"),
                           ("return eval('x')", "'eval'"), ("exec('LEAK.append(x)')\nreturn 1", "'exec'"),
                           ("return globals()", "'globals'"), ("return compile('x', 'f', 'eval')", "'compile'"),
                           ("return __import__('gc')", "'__import__'")):
            with self.subTest(body=body):
                self.refuses(body, word)

    def test_frames_and_internals(self):
        self.refuses("return sys._getframe().f_locals['x']", "can reach a model reading", imports="import sys\n")
        self.refuses("f = (lambda: 0)\nreturn f.__globals__", "'.__globals__'")
        self.refuses("return shio.core._sealed", "'._sealed'", imports="import shio\n")

    def test_modules_that_scan_memory(self):
        for mod in ("gc", "inspect", "ctypes"):
            with self.subTest(mod=mod):
                self.refuses("return 1", f"importing {mod}", imports=f"import {mod}\n")

    def test_ordinary_python_still_compiles(self):
        compile_source(program("""
            import sys
            n = len(sys.argv)
            label = getattr(str, "upper")("ok")
            check positive: x.value is not None and x.value > 0, "{x.value}"
            rule r:
                return x.value
            return r
            """), "t.shio")


class ChecksOnlyDecide(unittest.TestCase):
    """A check or when computes a yes or no; it cannot carry a reading's value out into plain code."""

    def refuses(self, line):
        with self.assertRaises(ShioError) as e:
            compile_source(program(line + "\nreturn 1"), "t.shio")
        self.assertIn("only decides", str(e.exception))

    def test_mutating_calls_and_assignment(self):
        for line in ('check c: LEAK.append(x.value) or True, "ok"',
                     "when w: LEAK.append(x.value) or True",
                     'check c: (got := x.value) is not None, "ok"',
                     "when w: (got := x.value) is not None",
                     'check c: True, "{LEAK.append(x.value)}"',
                     'check c: setattr(LEAK, "v", x.value) or True, "ok"',
                     "when w: LEAK.update(v=x.value) or True"):
            with self.subTest(line=line):
                self.refuses(line)

    def test_a_rule_may_still_act_on_it(self):
        compile_source(program("""
            check known: x.value is not None, "no amount"
            rule r:
                LEAK.append(x.value)
                return x.value
            return r
            """), "t.shio")


class SealedReading(unittest.TestCase):
    """From Python (no compiler), what the model said is not an attribute of the reading."""

    def setUp(self):
        self.tmp = TemporaryDirectory()
        shio.configure(self.tmp.name)

    def tearDown(self):
        shio.configure("shio-traces")
        self.tmp.cleanup()

    def test_value_raw_request_error_copy_pickle(self):
        m = shio.models.replay(lambda req: {"value": 4242.5})

        @shio.agent
        def a():
            x = shio.read(float, "How much?", by=m)
            got = []
            for poke in (lambda: x._value, lambda: x.raw, lambda: x.request, lambda: x.error, lambda: x.value,
                         lambda: copy.copy(x), lambda: copy.deepcopy(x), lambda: pickle.dumps(x),
                         lambda: x._gate("value")):
                try:
                    got.append(poke())
                except shio.BoundaryError:
                    got.append("blocked")
            return got
        self.assertEqual(a(), ["blocked"] * 9)

    def test_checks_and_traces_still_see_it(self):
        m = shio.models.replay(lambda req: {"value": 4242.5})

        @shio.agent
        def a():
            x = shio.read(float, "How much?", by=m)
            known(x)
            return settle(x)

        @shio.check
        def known(v):
            return v == 4242.5

        @shio.rule
        def settle(checks, v):
            return v if checks.passed("known") else None
        self.assertEqual(a(), 4242.5)
        self.assertIn("4242.5", a.last_trace.read_text())
