"""Regulated mode, phase 2: human sign-off as a language form with maker-checker, stable reason codes that survive
erasure, and `shio explain`, the decision map a model-risk team asks for."""
import contextlib
import importlib.util
import io
import json
import textwrap
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import shio
from shio import cli, ledger
from shio.compiler import ShioError, compile_source
from shio.explain import explain
from shio.replay import replay

PROGRAM = """
regulated

LIMIT = 10000

codes:
    UNKNOWN_SUPPLIER: "The supplier is not on our approved list."
    AMOUNT_NOT_IN_INVOICE: "We could not confirm the amount on the invoice."

type Invoice:
    supplier: str          # the supplier's name
    amount: float          # the total due
    contact: str pii       # the person who sent it
    iban: str as BankAccount   # where to pay

model llm = replay(lambda req: {"supplier": "Acme", "amount": 25000, "contact": "Ravi Iyer", "iban": "X1"})
model small = replay(lambda req: {"supplier": "Acme", "amount": 2500, "contact": "Ravi Iyer", "iban": "X1"})
model odd = replay(lambda req: {"supplier": "Initech", "amount": 2500, "contact": "Ravi Iyer", "iban": "X1"})

agent pay(invoice: str, submitted_by: str, llm):
    read inv: Invoice by llm:
        Read the invoice.

        {invoice}
    check known as UNKNOWN_SUPPLIER: inv.supplier in ("Acme", "Globex"), "unknown supplier {inv.supplier}"
    check grounded as AMOUNT_NOT_IN_INVOICE: f"{inv.amount:,.0f}" in invoice, "amount not in the invoice"
    signoff PAY when amount > LIMIT by role "finance_controller" maker submitted_by
    rule decide:
        if checks.failures():
            return {"action": "HOLD", "codes": [c["code"] for c in checks.failures()]}
        return {"action": "PAY", "amount": inv.amount, "iban": inv.iban, "submitted_by": submitted_by}
    return decide

agent batch(invoice: str, submitted_by: str, llm):
    out = pay(invoice, submitted_by, llm)
    return {"action": "PAY", "paid": 1}
"""
BIG, SMALL = "Acme invoice, total 25,000", "Acme invoice, total 2,500"


def program(**changes):
    src = textwrap.dedent(PROGRAM)
    for a, b in changes.items():
        assert a in src, a
        src = src.replace(a, b)
    return src


def compile_error(src):
    with unittest.TestCase().assertRaises(ShioError) as e:
        compile_source(src, "t.shio")
    return str(e.exception)


class Setup(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        _, self.pub_path, _ = ledger.keygen(self.dir / "keys")
        self.pub = ledger.load_public_key(self.pub_path)
        self.signer = ledger.load_signer(str(self.dir / "keys" / "shio-signing.key"))
        self.traces = self.dir / "traces"
        shio.configure(str(self.traces), signer=self.signer, actor="meera")
        (self.dir / "pay.shio").write_text(textwrap.dedent(PROGRAM))
        self.mod = shio.load(self.dir / "pay.shio")

    def tearDown(self):
        shio.configure("shio-traces", vault=False, signer=False, actor="")
        self.tmp.cleanup()

    def pay(self, invoice=BIG, by="ops-bot", model="llm", agent="pay"):
        fn = getattr(self.mod, agent)
        out = fn(invoice, by, getattr(self.mod, model))
        return out, Path(fn.last_trace)

    def verify(self, **kw):
        return ledger.verify(self.traces, self.pub, **kw)

    def sign(self, path, who="priya", role="finance_controller", meaning="approved for payment", **kw):
        return ledger.signoff(path, who, role, meaning, self.signer, **kw)


class ReasonCodes(Setup):
    @unittest.skipUnless(importlib.util.find_spec("cryptography"), "needs the [regulated] extra (cryptography)")
    def test_codes_are_recorded_in_clear_with_their_text(self):
        out, path = self.pay(SMALL, model="odd")
        self.assertEqual(out["codes"], ["UNKNOWN_SUPPLIER"])
        rec = json.loads(path.read_text())
        self.assertEqual(rec["reasons"], [{"code": "UNKNOWN_SUPPLIER", "check": "known", "key": None,
                                           "text": "The supplier is not on our approved list."}])
        self.assertEqual([c["code"] for c in rec["checks"]], ["UNKNOWN_SUPPLIER", "AMOUNT_NOT_IN_INVOICE"])
        self.assertIn("$vault", json.dumps(rec["checks"][0]["detail"]))    # the explanation stays in the vault

    def test_reasons_survive_erasure(self):
        _, path = self.pay(SMALL, model="odd")
        ledger.erase(path, "erasure request #7", self.signer)
        self.assertTrue(self.verify()[0])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.main(["reasons", str(path)])
        self.assertIn("UNKNOWN_SUPPLIER: The supplier is not on our approved list.", buf.getvalue())
        self.assertNotIn("Ravi", path.read_text())

    def test_passing_checks_give_no_reasons(self):
        _, path = self.pay(SMALL, model="small")
        self.assertNotIn("reasons", json.loads(path.read_text()))

    def test_compile_errors(self):
        self.assertIn("not in the program's codes", compile_error(program(**{
            "check known as UNKNOWN_SUPPLIER": "check known as NO_SUCH_CODE"})))
        self.assertIn("must declare them in a codes: table", compile_error(program(**{
            'codes:\n    UNKNOWN_SUPPLIER: "The supplier is not on our approved list."\n'
            '    AMOUNT_NOT_IN_INVOICE: "We could not confirm the amount on the invoice."\n': ""})))
        self.assertIn("plain string", compile_error(program(**{
            '"The supplier is not on our approved list."': 'f"{LIMIT}"'})))
        self.assertIn("capitals", compile_error(program(**{
            "check known as UNKNOWN_SUPPLIER": "check known as unknown"})))
        self.assertIn("a when only steers", compile_error(program(**{
            "check known as UNKNOWN_SUPPLIER": "when known as UNKNOWN_SUPPLIER"})))

    def test_codes_without_a_table_outside_regulated_mode(self):
        src = program(**{"regulated\n": "", 'codes:\n    UNKNOWN_SUPPLIER: "The supplier is not on our approved list."\n'
                         '    AMOUNT_NOT_IN_INVOICE: "We could not confirm the amount on the invoice."\n': ""})
        compile_source(src, "t.shio")


class Signoff(Setup):
    def test_over_the_limit_is_pending_and_under_is_not(self):
        out, path = self.pay()
        self.assertEqual(out["status"], "PENDING")
        self.assertEqual(out["decided"]["action"], "PAY")
        self.assertEqual(out["signoff"]["roles"], ["finance_controller"])
        self.assertIn("amount=25000.0", out["signoff"]["because"][0])
        req = json.loads(path.read_text())["signoff"]
        self.assertEqual(req["requested_by"], "meera")
        self.assertEqual(req["makers"], ["meera", "ops-bot"])
        out, _ = self.pay(SMALL, model="small")
        self.assertEqual(out["action"], "PAY")
        out, _ = self.pay(SMALL, model="odd")
        self.assertEqual(out["action"], "HOLD")              # the label does not match: no sign-off

    def test_maker_checker(self):
        _, path = self.pay()
        for who, role, why in (("meera", "finance_controller", "maker-checker"),       # the requester
                               ("OPS-BOT", "finance_controller", "maker-checker"),     # the maker, any case
                               ("priya", "clerk", "asks for finance_controller")):
            with self.assertRaises(ledger.SignoffRefused) as e:
                self.sign(path, who, role)
            self.assertIn(why, str(e.exception))
        e, state = self.sign(path)
        self.assertEqual((e["signer"], e["decision"], state), ("priya", "approved", "APPROVED"))
        self.assertEqual(e["record_sha256"], json.loads(path.read_text())["integrity"]["sha256"])
        with self.assertRaises(ledger.SignoffRefused):
            self.sign(path, "arjun")                         # already decided
        ok, report, _ = self.verify()
        self.assertTrue(ok, report)
        self.assertIn("1 sign-offs, 0 pending", report[0])

    def test_two_roles_need_two_people(self):
        src = program(**{'by role "finance_controller"': 'by role "finance_controller" and "treasury"'})
        (self.dir / "pay.shio").write_text(src)
        self.mod = shio.load(self.dir / "pay.shio")
        _, path = self.pay()
        _, state = self.sign(path)
        self.assertEqual(state, "PENDING")
        with self.assertRaises(ledger.SignoffRefused) as e:
            self.sign(path, "Priya", "treasury")
        self.assertIn("already signed", str(e.exception))
        _, state = self.sign(path, "arjun", "treasury")
        self.assertEqual(state, "APPROVED")

    def test_rejection(self):
        _, path = self.pay()
        _, state = self.sign(path, meaning="rejected: duplicate invoice", reject=True)
        self.assertEqual(state, "REJECTED")
        self.assertEqual(ledger.status(path)[1], "REJECTED")

    def test_pending_is_reported_by_verify(self):
        _, path = self.pay()
        ok, report, _ = self.verify()
        self.assertTrue(ok, report)
        self.assertIn(f"{path.name}: PENDING sign-off by finance_controller", "\n".join(report))

    def test_verify_catches_a_sign_off_written_around_the_checks(self):
        _, path = self.pay()
        with ledger._locked(self.traces):
            entry = ledger._record_entry(ledger._entries(self.traces), path.name)
            event = {"kind": "signoff", "file": path.name, "record_sha256": entry["sha256"], "signer": "meera",
                     "role": "finance_controller", "meaning": "approved", "decision": "approved", "time": time.time()}
            event["sha256"] = ledger.sha256({k: event[k] for k in ledger.SIGNOFF_FIELDS})
            ledger._append(self.traces, event, self.signer)
        ok, report, _ = self.verify()
        self.assertFalse(ok)
        self.assertIn("breaks maker-checker", "\n".join(report))

    def test_verify_catches_an_edited_sign_off_and_a_changed_record(self):
        _, path = self.pay()
        self.sign(path)
        lines = (self.traces / ledger.LEDGER).read_text().splitlines()
        e = json.loads(lines[1])
        e["signer"] = "someone else"
        (self.traces / ledger.LEDGER).write_text("\n".join([lines[0], json.dumps(e)]) + "\n")
        self.assertIn("sign-off entry edited", "\n".join(self.verify()[1]))
        (self.traces / ledger.LEDGER).write_text("\n".join(lines) + "\n")
        rec = json.loads(path.read_text())
        rec["signoff"]["roles"] = ["clerk"]
        path.write_text(json.dumps(rec))
        with self.assertRaises(ledger.SignoffRefused) as err:
            self.sign(path, "arjun", "clerk")
        self.assertIn("has changed", str(err.exception))

    def test_a_decided_record_cannot_be_signed(self):
        _, path = self.pay(SMALL, model="small")
        with self.assertRaises(ledger.SignoffRefused) as e:
            self.sign(path)
        self.assertIn("does not need sign-off", str(e.exception))

    def test_wrapping_in_another_agent_does_not_skip_sign_off(self):
        out, path = self.pay(agent="batch")
        self.assertEqual(out["status"], "PENDING")
        self.assertEqual(json.loads(path.read_text())["signoff"]["roles"], ["finance_controller"])

    def test_an_unevaluable_condition_needs_a_person(self):
        src = program(**{"when amount > LIMIT": "when total > LIMIT"})
        (self.dir / "pay.shio").write_text(src)
        self.mod = shio.load(self.dir / "pay.shio")
        out, _ = self.pay(SMALL, model="small")
        self.assertEqual(out["status"], "PENDING")
        self.assertIn("could not be evaluated", out["signoff"]["because"][0])

    def test_signer_own_key(self):
        _, path = self.pay()
        _, priya_pub, _ = ledger.keygen(self.dir / "priya")
        key = ledger.load_private_key(self.dir / "priya" / "shio-signing.key")
        self.sign(path, personal_key=key)
        ok, report, _ = self.verify()
        self.assertTrue(ok)
        self.assertIn("own signature not checked", "\n".join(report))
        self.assertTrue(self.verify(signer_keys=[ledger.load_public_key(priya_pub)])[0])
        _, other, _ = ledger.keygen(self.dir / "other")
        lines = (self.traces / ledger.LEDGER).read_text().splitlines()
        e = json.loads(lines[1])
        e["signer_key_id"] = ledger.key_id(ledger.load_public_key(other))
        (self.traces / ledger.LEDGER).write_text("\n".join([lines[0], json.dumps(e)]) + "\n")
        ok, report, _ = self.verify(signer_keys=[ledger.load_public_key(other)])
        self.assertFalse(ok)
        self.assertIn("BAD SIGNATURE", "\n".join(report))

    def test_replay_reproduces_a_pending_decision(self):
        _, path = self.pay()
        ok, lines = replay(path)
        self.assertTrue(ok, lines)

    def test_cli(self):
        _, path = self.pay()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli.main(["signoff", str(path)])
        self.assertIn("PENDING", buf.getvalue())
        with self.assertRaises(SystemExit) as e:
            cli.main(["signoff", str(path), "--as", "meera", "--role", "finance_controller", "--meaning", "ok"])
        self.assertIn("maker-checker", str(e.exception))
        shio.core._config["signer"] = self.signer
        with contextlib.redirect_stdout(buf):
            cli.main(["signoff", str(path), "--as", "priya", "--role", "finance_controller",
                      "--meaning", "approved for payment"])
        self.assertIn("decision is now APPROVED", buf.getvalue())

    def test_compile_errors(self):
        self.assertIn("is a model reading", compile_error(program(**{"when amount > LIMIT": "when inv.amount > 1"})))
        self.assertIn("only be used inside an agent", compile_error(program() + '\nsignoff PAY by role "x"\n'))
        self.assertIn("expected: signoff", compile_error(program(**{'by role "finance_controller"': "by finance"})))
        self.assertIn("top of an agent", compile_error(program(**{
            "    signoff PAY": "    if True:\n        signoff PAY"})))


class Explain(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.path = Path(self.tmp.name) / "pay.shio"
        self.path.write_text(textwrap.dedent(PROGRAM))

    def tearDown(self):
        self.tmp.cleanup()

    def test_decision_map(self):
        md = explain(self.path)
        for part in ("# Decision map: pay.shio", "Regulated: **yes**", "## Agent pay", "**read inv**",
                     "Personal data in this reading: contact", "`UNKNOWN_SUPPLIER`", "unknown supplier {inv.supplier}",
                     "`not checks.failures()`", "PENDING for finance_controller when `amount > LIMIT`",
                     "the outcome's submitted_by", "replay (a test double", "checked fields: amount, supplier",
                     "`inv.iban` is used by a rule but no check verifies it", "**pii**",
                     "We could not confirm the amount on the invoice."):
            self.assertIn(part, md)

    def test_html_and_cli(self):
        out = Path(self.tmp.name) / "map.html"
        with contextlib.redirect_stdout(io.StringIO()):
            cli.main(["explain", str(self.path), "-o", str(out)])
        page = out.read_text()
        self.assertTrue(page.startswith("<!doctype html>"))
        self.assertIn("<title>Decision map: pay.shio</title>", page)
        self.assertIn("<code>UNKNOWN_SUPPLIER</code>", page)

    def test_model_pins(self):
        src = textwrap.dedent(PROGRAM).replace(
            'model small =', 'model local = ollama("qwen3:8b", digest="500a1f067a9f")\nmodel small =')
        self.path.write_text(src)
        self.assertIn("pinned to Ollama digest 500a1f067a9f", explain(self.path))

    def test_every_example_explains(self):
        root = Path(__file__).resolve().parent.parent / "examples"
        for f in sorted(root.rglob("*.shio")):
            if "unsafe" in f.name:
                continue                     # written not to compile
            with self.subTest(f.name):
                self.assertIn("# Decision map", explain(f))


if __name__ == "__main__":
    unittest.main()
