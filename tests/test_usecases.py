"""Every program in examples/usecases compiles, runs offline with its scripted answers, and shows at least three cases;
every curated case still decides what catalog.json says it decides."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
USECASES = ROOT / "examples" / "usecases"
sys.path.insert(0, str(USECASES))
from catalog import cases  # noqa: E402


class UseCases(unittest.TestCase):
    def test_every_program_runs_and_decides_as_catalogued(self):
        catalog = {i["name"]: i for i in json.loads((USECASES / "catalog.json").read_text())}
        env = {**os.environ, "PYTHONPATH": str(ROOT)}
        env.pop("SHIO_MODEL", None)
        programs = sorted(USECASES.glob("*.shio"))
        traces = tempfile.mkdtemp(prefix="shio-usecases-")
        self.assertGreaterEqual(len(programs), 100)
        self.assertEqual(sum(i["curated"] for i in catalog.values()), 100)
        for f in programs:
            with self.subTest(program=f.stem):
                r = subprocess.run([sys.executable, "-m", "shio.cli", "run", str(f), "--traces", traces],
                                   cwd=USECASES, env=env, capture_output=True, text=True, timeout=120)
                self.assertEqual(r.returncode, 0, r.stderr[-600:])
                got = [(c["title"], c["outcome"]) for c in cases(r.stdout)]
                self.assertGreaterEqual(len(got), 3)
                want = [(c["title"], c["outcome"]) for c in catalog[f.stem]["cases"]]
                self.assertEqual(got, want, "a case now decides differently; rerun catalog.py if that is intended")


if __name__ == "__main__":
    unittest.main()
