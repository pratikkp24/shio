"""Fuzzing the boundary: generated programs put a model reading into random expressions in plain code, and into
checks and whens with side effects. Each must be refused by the compiler, blocked at run time, or run without the
reading's value reaching plain code. (Rules may hand a value on: that is their job, so they are not fuzzed here.)

    python -m pytest tests/test_fuzz_boundary.py           # SHIO_FUZZ_N=5000 SHIO_FUZZ_SEED=7 for a longer run
"""
import json
import os
import random
import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import shio
from shio.compiler import ShioError

HEAD = """
LEAK = []

type Amount:
    value: float?
    as_printed: str

model m = replay(lambda req: {"value": 4242.5, "as_printed": "MARK-7f3"})

agent a():
    read x: Amount by m:
        How much?
"""

# expressions that hold the reading, its value, or something built from it
ATOMS = ["x", "x.value", "x.as_printed", "[x]", "(x,)", "{'k': x}", "str(x)", "f'{x}'", "f'{x.value}'", "repr(x)",
         "x if True else 0", "getattr(x, 'value')", "(lambda: x)()", "(lambda v=x: v)()", "[r for r in [x]]",
         "{r: 1 for r in [x.value]}", "dict(a=x)", "max([x.value])", "sorted([x.value])", "list(map(str, [x.value]))",
         "x.value + 0", "-x.value", "x.value * 1", "'%s' % x.value", "'{}'.format(x.value)", "[*[x]]", "{**{'k': x}}",
         "next(iter([x]))", "(x.value,)[0]", "x.__dict__", "type(x)", "x.value.__class__", "hash(x)", "id(x)",
         "x == x", "x and 1", "not x", "bool(x)", "len(x)", "x[0]", "x.raw", "x._value", "x._gate('value')",
         "__import__('copy').copy(x)", "eval('x')", "locals()['x']", "vars()['x']", "globals()"]

# plain statements: each must be refused or blocked, or leak nothing
PLAIN = ["return {e}", "y = {e}\n    return y", "LEAK.append({e})\n    return 1", "print({e})\n    return 1",
         "if {e}:\n        return 1\n    return 0", "for _ in [{e}]:\n        LEAK.append(_)\n    return 1",
         "y, *z = [{e}, 0]\n    return y", "assert {e}\n    return 1", "while {e}:\n        break\n    return 1",
         "try:\n        raise ValueError({e})\n    except ValueError as err:\n        return err.args",
         "LEAK.extend([{e}])\n    return 1", "return [{e}][0]", "y = 0\n    y += {e}\n    return y"]

# checks and whens: they may look, but must not carry the value out
SIDE = ["LEAK.append({v}) or True", "(got := {v}) is not None", "LEAK.extend([{v}]) or True",
        "setattr(LEAK, 'v', {v}) or True", "LEAK.insert(0, {v}) or True", "[LEAK.append(v) for v in [{v}]] or True"]
VALUES = ["x.value", "x.as_printed", "str(x.value)", "[x.value]"]


def leaked(obj):
    try:
        s = json.dumps(obj, default=repr)
    except Exception:
        s = repr(obj)
    return "4242.5" in s or "MARK-7f3" in s


def programs(n, seed):
    rng = random.Random(seed)
    for i in range(n):
        if rng.random() < 0.75:
            body = rng.choice(PLAIN).format(e=rng.choice(ATOMS))
        else:
            kw = rng.choice(["check c:", "when c:"])
            expr = rng.choice(SIDE).format(v=rng.choice(VALUES))
            body = f"{kw} {expr}" + (', "ok"' if kw == "check c:" else "") + "\n    return 1"
        yield i, textwrap.dedent(HEAD) + "    " + body + "\n"


class Fuzz(unittest.TestCase):
    def test_no_generated_program_leaks_a_reading(self):
        n = int(os.environ.get("SHIO_FUZZ_N", 600))
        seed = int(os.environ.get("SHIO_FUZZ_SEED", 1))
        outcomes, leaks = {"refused": 0, "blocked": 0, "ran": 0, "error": 0}, []
        with TemporaryDirectory() as tmp:
            shio.configure(tmp)
            try:
                for i, src in programs(n, seed):
                    p = Path(tmp) / f"p{i}.shio"
                    p.write_text(src)
                    try:
                        mod = shio.load(p, name=f"shio_fuzz_{seed}_{i}")
                    except ShioError:
                        outcomes["refused"] += 1
                        continue
                    try:
                        out = mod.a()
                    except shio.BoundaryError:
                        outcomes["blocked"] += 1
                        out = None
                    except Exception:
                        outcomes["error"] += 1
                        out = None
                    else:
                        outcomes["ran"] += 1
                    if leaked(out) or leaked(getattr(mod, "LEAK", [])):
                        leaks.append(src.split("How much?\n", 1)[1].strip())
            finally:
                shio.configure("shio-traces")
        self.assertEqual(leaks, [], f"{len(leaks)} of {n} programs leaked (seed {seed}); outcomes {outcomes}")
        self.assertGreater(outcomes["refused"], n // 2)        # most routes are stopped at compile time


class FuzzPythonAPI(unittest.TestCase):
    """From Python there is no compiler: the sealed reading itself must hold. Every expression above, and compositions
    of two, run against a live reading in plain code inside an agent: each must raise BoundaryError, fail some other
    way, or produce something without the value in it. (Reaching into shio's private modules, such as
    shio.core._SEALED, is deliberate Python and out of scope: Python is not a sandbox.)"""

    def test_no_expression_on_a_live_reading_leaks(self):
        exprs = [a for a in ATOMS if not a.startswith(("locals", "vars", "globals", "eval"))]
        rng = random.Random(int(os.environ.get("SHIO_FUZZ_SEED", 1)))
        exprs += [f"({rng.choice(exprs)}, {rng.choice(exprs)})" for _ in range(int(os.environ.get("SHIO_FUZZ_N", 600)) // 3)]
        m = shio.models.replay(lambda req: {"value": 4242.5, "as_printed": "MARK-7f3"})
        from pydantic import BaseModel

        class Amount(BaseModel):
            value: float | None
            as_printed: str
        outcomes, leaks = {"blocked": 0, "other": 0, "clean": 0}, []
        with TemporaryDirectory() as tmp:
            shio.configure(tmp)
            try:
                @shio.agent
                def a():
                    x = shio.read(Amount, "How much?", by=m)
                    for e in exprs:
                        try:
                            out = eval(e, {"__builtins__": __builtins__}, {"x": x})
                        except shio.BoundaryError:
                            outcomes["blocked"] += 1
                            continue
                        except Exception:
                            outcomes["other"] += 1
                            continue
                        if leaked(out):
                            leaks.append(e)
                        else:
                            outcomes["clean"] += 1
                    return 1
                a()
            finally:
                shio.configure("shio-traces")
        self.assertEqual(leaks, [], f"leaked: {leaks[:10]}; outcomes {outcomes}")
        self.assertGreater(outcomes["blocked"], len(exprs) // 3)
