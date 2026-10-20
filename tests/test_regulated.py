"""Regulated mode and decision records: the escape hatches are compile errors; a regulated program needs a signing
key, recorded runs and pinned models; records are hash-chained and signed, and verify catches every edit, deletion,
insertion and reordering; personal data is hashed in records and erasable; replay reproduces a decision."""
import json
import os
import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import shio
from shio import ledger
from shio.compiler import ShioError, compile_source
from shio.core import RegulatedError
from shio.models import Ollama
from shio.replay import replay

PROGRAM = """
regulated

type Applicant:
    name: str pii          # the applicant's full name
    income: float          # monthly income
    quote: str pii         # the line the income comes from, copied exactly

model local = replay(lambda req: {"name": "Asha Rao", "income": 58000, "quote": "Net salary 58,000"})

agent assess(statement: str, llm):
    read a: Applicant by llm:
        Read the applicant and their monthly income.

        {statement}
    check grounded: a.quote in statement, "income quote not in the statement"
    check enough: a.income >= 50000, "income {a.income} is below 50,000"
    rule decision:
        if a is None:
            return {"status": "REFER", "why": ["the model gave no usable answer"]}
        if checks.failures():
            return {"status": "REFER", "why": [c["detail"] for c in checks.failures()]}
        return {"status": "ELIGIBLE"}
    return decision
"""
STATEMENT = "Asha Rao\nNet salary 58,000 credited 01-10-2026"


class Compile(unittest.TestCase):
    def refuses(self, line, *words, extra=""):
        src = textwrap.dedent(PROGRAM).replace("    rule decision:", f"    {line}\n    rule decision:") + extra
        with self.assertRaises(ShioError) as e:
            compile_source(src, "t.shio")
        self.assertIn("regulated:", str(e.exception))
        for w in words:
            self.assertIn(w, str(e.exception))

    def test_escape_hatches_are_compile_errors(self):
        self.refuses('v = locals()["a"]', "'locals'")
        self.refuses('v = vars()', "'vars'")
        self.refuses('v = eval("a")', "'eval'")
        self.refuses('v = getattr(statement, "upper")', "'getattr'")
        self.refuses('v = statement.__class__', "'.__class__'")
        self.refuses('v = __builtins__', "'__builtins__'")

    def test_private_attributes_and_internal_modules(self):
        src = textwrap.dedent(PROGRAM)
        for bad, word in (("import shio.core", "shio"), ("import sys", "sys"), ("from operator import attrgetter",
                                                                                 "operator"), ("import pickle", "pickle")):
            with self.assertRaises(ShioError) as e:
                compile_source(src.replace("regulated\n", f"regulated\n{bad}\n"), "t.shio")
            self.assertIn(word, str(e.exception))
        with self.assertRaises(ShioError):
            compile_source(src + "\ndef peek(x):\n    return x._value\n", "t.shio")

    def test_a_clean_regulated_program_compiles(self):
        compile_source(textwrap.dedent(PROGRAM), "t.shio")

    def test_outside_regulated_mode_only_the_reflection_routes_are_refused(self):
        plain = textwrap.dedent(PROGRAM).replace("regulated\n", "")
        with self.assertRaises(ShioError) as e:            # reaches a reading without naming it: refused everywhere
            compile_source(plain.replace("    rule decision:", '    v = locals()["a"]\n    rule decision:'), "t.shio")
        self.assertIn("'locals'", str(e.exception))
        # the wider bans are regulated mode's: ordinary programs may still use getattr and sys
        compile_source(plain.replace("    rule decision:", '    v = getattr(statement, "upper")\n    rule decision:'),
                       "t.shio")
        compile_source("import sys\n" + plain, "t.shio")


class Records(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        _, self.pub_path, _ = ledger.keygen(self.dir / "keys")
        self.pub = ledger.load_public_key(self.pub_path)
        self.signer = ledger.load_signer(str(self.dir / "keys" / "shio-signing.key"))
        self.traces = self.dir / "traces"
        shio.configure(str(self.traces), signer=self.signer)
        (self.dir / "loan.shio").write_text(textwrap.dedent(PROGRAM))
        self.mod = shio.load(self.dir / "loan.shio")

    def tearDown(self):
        shio.configure("shio-traces", vault=False, signer=False)
        self.tmp.cleanup()

    def run_once(self, statement=STATEMENT):
        out = self.mod.assess(statement, self.mod.local)
        return out, Path(self.mod.assess.last_trace)

    def verify(self, **kw):
        return ledger.verify(self.traces, self.pub, **kw)

    def test_records_chain_and_verify(self):
        for _ in range(3):
            self.run_once()
        ok, report, head = self.verify()
        self.assertTrue(ok, report)
        self.assertIn("signatures: 3 ok", "\n".join(report))
        ok, report, _ = self.verify(head=head)
        self.assertTrue(ok)

    def test_record_identifies_program_model_and_sources(self):
        _, path = self.run_once()
        rec = json.loads(path.read_text())
        self.assertTrue(rec["program"]["regulated"])
        self.assertEqual(len(rec["program"]["sha256"]), 64)
        r = rec["readings"][0]
        self.assertEqual(r["model_id"]["kind"], "replay")
        self.assertEqual(len(r["prompt_sha256"]), 64)
        self.assertEqual(r["pii"], ["name", "quote"])
        self.assertIn("$pii", r["value"]["name"])
        self.assertEqual(r["value"]["income"], 58000.0)          # not personal: auditable in the record
        self.assertIn("$vault", r["raw"])
        self.assertIn("$vault", rec["inputs"])

    def test_edit_delete_insert_reorder_are_caught(self):
        paths = [self.run_once()[1] for _ in range(3)]
        rec = json.loads(paths[1].read_text())
        rec["outcome"]["status"] = "ELIGIBLE (edited)"
        original = paths[1].read_text()
        paths[1].write_text(json.dumps(rec))
        self.assertIn("TAMPERED", "\n".join(self.verify()[1]))
        paths[1].write_text(original)
        paths[2].rename(self.dir / "gone.json")
        self.assertIn("MISSING", "\n".join(self.verify()[1]))
        (self.dir / "gone.json").rename(paths[2])
        (self.traces / "forged.json").write_text(json.dumps({"agent": "assess", "outcome": {"status": "ELIGIBLE"}}))
        self.assertIn("not in the ledger", "\n".join(self.verify()[1]))
        (self.traces / "forged.json").unlink()
        lines = (self.traces / ledger.LEDGER).read_text().splitlines()
        (self.traces / ledger.LEDGER).write_text("\n".join([lines[1], lines[0], lines[2]]) + "\n")
        self.assertIn("chain broken", "\n".join(self.verify()[1]))

    def test_wrong_key_and_hmac(self):
        self.run_once()
        _, other, _ = ledger.keygen(self.dir / "other")
        ok, report, _ = ledger.verify(self.traces, ledger.load_public_key(other))
        self.assertFalse(ok)
        self.assertIn("BAD SIGNATURE", "\n".join(report))
        shio.configure(str(self.dir / "hmac"), signer=ledger.load_signer("hmac:s3cret"))
        self.mod.assess(STATEMENT, self.mod.local)
        self.assertTrue(ledger.verify(self.dir / "hmac", secret=b"s3cret")[0])
        self.assertFalse(ledger.verify(self.dir / "hmac", secret=b"wrong")[0])

    def test_erasure_is_recorded_and_verifies(self):
        _, path = self.run_once()
        ledger.erase(path, "erasure request #4411", self.signer)
        ok, report, _ = self.verify()
        self.assertTrue(ok, report)
        self.assertIn("vault erased", "\n".join(report))
        self.assertFalse(replay(path)[0])                         # the model's answers are gone with the vault

    def test_vault_deleted_without_erasure_is_a_problem(self):
        _, path = self.run_once()
        rec = json.loads(path.read_text())
        (self.traces / rec["vault"]["file"]).unlink()
        ok, report, _ = self.verify()
        self.assertFalse(ok)
        self.assertIn("vault MISSING", "\n".join(report))

    def test_replay_reproduces_and_detects_changed_programs(self):
        out, path = self.run_once()
        self.assertEqual(out["status"], "ELIGIBLE")
        ok, lines = replay(path)
        self.assertTrue(ok, lines)
        prog = self.dir / "loan.shio"
        prog.write_text(prog.read_text().replace("a.income >= 50000", "a.income >= 60000"))
        ok, lines = replay(path)
        self.assertFalse(ok)
        self.assertIn("has changed", lines[0])
        ok, lines = replay(path, allow_changed=True)          # impact analysis: what the new rule would decide
        self.assertFalse(ok)
        self.assertIn("check enough", "\n".join(lines))

    def test_training_reads_through_the_vault(self):
        self.run_once()
        runs = list(shio.trace.load(str(self.traces / "*.json")))
        self.assertEqual(runs[0]["readings"][0]["value"]["name"], "Asha Rao")


class RuntimeRules(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "loan.shio").write_text(textwrap.dedent(PROGRAM))
        self.mod = shio.load(self.dir / "loan.shio")
        self.key = os.environ.pop("SHIO_SIGNING_KEY", None)

    def tearDown(self):
        if self.key:
            os.environ["SHIO_SIGNING_KEY"] = self.key
        shio.configure("shio-traces", vault=False, signer=False)
        self.tmp.cleanup()

    def test_needs_a_signing_key_and_recorded_runs(self):
        shio.configure(str(self.dir / "t"), signer=False)
        with self.assertRaises(RegulatedError):
            self.mod.assess(STATEMENT, self.mod.local)
        shio.configure(None, signer=ledger.load_signer("hmac:x"))
        with self.assertRaises(RegulatedError):
            self.mod.assess(STATEMENT, self.mod.local)

    def test_unpinned_model_is_refused_and_fails_closed(self):
        shio.configure(str(self.dir / "t"), signer=ledger.load_signer("hmac:x"))
        unpinned = shio.models.openai_compat("some-model", "http://127.0.0.1:9/v1")
        out = self.mod.assess(STATEMENT, unpinned)
        self.assertEqual(out["status"], "REFER")
        rec = json.loads(Path(self.mod.assess.last_trace).read_text())
        vault = ledger.load_vault(self.mod.assess.last_trace, rec)
        self.assertIn("not pinned", json.dumps(vault))

    def test_ollama_digest_pin(self):
        m = Ollama("qwen3:8b", digest="deadbeef0000")
        m._digest = "500a1f067a9f782620b40bee"
        with self.assertRaises(RuntimeError):
            m.propose(None)
        ok = Ollama("qwen3:8b", digest="500a1f067a9f")
        ok._digest = "500a1f067a9f782620b40bee"
        self.assertEqual(ok.identity()["pinned"], "500a1f067a9f")


if __name__ == "__main__":
    unittest.main()
