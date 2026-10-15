"""Every program in examples/usecases compiles, runs offline with its scripted answers, and shows at least three cases;
every curated case still decides what catalog.json says it decides; and with a model whose every answer is unusable,
no program crashes and none reaches any program's happy outcome (fail closed)."""
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

    # outcomes that are safe even with no usable model answer: paging from code's own metrics, "couldn't find it" replies
    SAFE_WITHOUT_MODEL = {"PAGE primary", "REPLIED", "REPLY"}

    def test_fail_closed_when_every_read_fails(self):
        catalog = json.loads((USECASES / "catalog.json").read_text())
        happy = {i["cases"][0]["outcome"] for i in catalog if i["cases"]} - self.SAFE_WITHOUT_MODEL
        env = {**os.environ, "PYTHONPATH": str(ROOT), "SHIO_MODEL": "null"}
        traces = tempfile.mkdtemp(prefix="shio-usecases-null-")
        for f in sorted(USECASES.glob("*.shio")):
            with self.subTest(program=f.stem):
                r = subprocess.run([sys.executable, "-m", "shio.cli", "run", str(f), "--traces", traces],
                                   cwd=USECASES, env=env, capture_output=True, text=True, timeout=120)
                self.assertEqual(r.returncode, 0, r.stderr[-600:])
                for c in cases(r.stdout):
                    self.assertNotIn(c["outcome"], happy, f"{c['title']}: approved with no usable model answer")


if __name__ == "__main__":
    unittest.main()
