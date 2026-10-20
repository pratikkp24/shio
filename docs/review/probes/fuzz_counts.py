"""Runs tests/test_fuzz_boundary.py and prints its outcome counts, which the test asserts on but does not print.
Run from the repository root:  SHIO_FUZZ_N=20000 SHIO_FUZZ_SEED=1 python docs/review/probes/fuzz_counts.py"""
import os
import sys
import unittest

sys.path.insert(0, "tests")
import test_fuzz_boundary as T  # noqa: E402

_equal = unittest.TestCase.assertEqual


def _print_message(self, a, b, msg=None):
    if msg:
        print(msg)
    return _equal(self, a, b, msg)


unittest.TestCase.assertEqual = _print_message
n, seed = int(os.environ.get("SHIO_FUZZ_N", 600)), int(os.environ.get("SHIO_FUZZ_SEED", 1))
print(f"N={n} seed={seed}: {len({src for _, src in T.programs(n, seed)})} distinct .shio programs generated")
for cls, name in ((T.Fuzz, "test_no_generated_program_leaks_a_reading"),
                  (T.FuzzPythonAPI, "test_no_expression_on_a_live_reading_leaks")):
    result = unittest.TextTestRunner(verbosity=0, stream=open(os.devnull, "w")).run(cls(name))
    print(f"{cls.__name__}: {'passed' if result.wasSuccessful() else 'FAILED'}")
