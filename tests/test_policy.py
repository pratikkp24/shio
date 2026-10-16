"""Data-handling policies: programs say what data is (`as Email`), dated policy versions say how it is stored, each
decision follows the version in force on its date and says so, and audit, migrate, retention, check and impact work
over time. The scenario: RBI requires emails hashed (v3); a later circular allows emails in clear but requires phone
numbers hashed (v4)."""
import datetime as dt
import json
import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import shio
from shio import ledger
from shio.compiler import ShioError, compile_source
from shio.policy import PolicyError, Store, audit, check_programs, impact, parse, retention

POLICIES = """
policy "RBI data handling" version 3:
    effective 2026-01-01
    source "RBI circular 2025-26/41, para 3"
    approved_by "CCO"
    Email:       hashed
    PhoneNumber: clear
    PAN:         vault, retain 1 years
    FullName:    vault

policy "RBI data handling" version 4:
    effective 2027-01-01
    source "RBI circular 2026-27/112, para 5"
    approved_by "CCO"
    Email:       clear
    PhoneNumber: hashed
    PAN:         vault, retain 8 years
    FullName:    vault
"""

PROGRAM = """
regulated

type Contact:
    label: str
    phone: str as PhoneNumber

type Customer:
    name: str as FullName
    email: str as Email
    phone: str as PhoneNumber
    pan: str as PAN
    income: float
    others: list[Contact]

model clerk = replay(lambda req: {"name": "Asha Rao", "email": "asha.rao@example.in", "phone": "+91 98200 12345",
                                  "pan": "ABCPS1234K", "income": 58000,
                                  "others": [{"label": "spouse", "phone": "+91 98200 77777"}]})

agent onboard(form: str, llm):
    read c: Customer by llm:
        Read the customer's details from this form.

        {form}
    check email_on_form: c.email in form, "email {c.email} is not on the form"
    rule decision:
        if c is None:
            return {"status": "REVIEW"}
        if checks.failures():
            return {"status": "REVIEW", "why": [x["detail"] for x in checks.failures()]}
        return {"status": "OPEN", "contact": c.email, "sms": c.phone}
    return decision
"""
FORM = "Asha Rao, asha.rao@example.in, +91 98200 12345, spouse +91 98200 77777, PAN ABCPS1234K"


def at(day):
    return mock.patch("shio.core.time.time", return_value=dt.datetime.fromisoformat(day + "T10:00").timestamp())


class Parsing(unittest.TestCase):
    def test_versions_and_in_force(self):
        store = Store(parse(textwrap.dedent(POLICIES)))
        self.assertEqual([p.version for p in store.in_force(dt.date(2026, 6, 1))], [3])
        self.assertEqual([p.version for p in store.in_force(dt.date(2027, 1, 1))], [4])
        self.assertEqual(store.in_force(dt.date(2025, 6, 1)), [])
        h = store.handling(dt.date(2026, 6, 1))
        self.assertEqual((h.of("Email"), h.of("PhoneNumber"), h.of("PAN")), ("hashed", "clear", "vault"))
        self.assertEqual(h.rule("PAN")[1], 1)
        self.assertEqual(h.of("Unknown"), "vault")          # no rule: protected by default

    def test_overlapping_policies_take_the_most_protective_rule(self):
        store = Store(parse(textwrap.dedent(POLICIES) + textwrap.dedent('''
            policy "Group privacy standard" version 1:
                effective 2026-01-01
                source "Group policy GP-7"
                PhoneNumber: vault
                *: hashed
            ''')))
        h = store.handling(dt.date(2026, 6, 1))
        self.assertEqual(h.of("PhoneNumber"), "vault")      # stricter than RBI v3's clear
        self.assertEqual(h.of("Email"), "hashed")
        self.assertEqual(h.of("Anything"), "hashed")        # the group default

    def test_bad_policies_are_refused(self):
        for text, word in (('policy "X" version 1:\n    source "s"\n    Email: clear\n', "effective"),
                           ('policy "X" version 1:\n    effective 2026-01-01\n    Email: clear\n', "source"),
                           ('policy "X" version 1:\n    effective 2026-01-01\n    source "s"\n    Email: shredded\n',
                            "clear|vault|hashed"),
                           ('policy "X" version 1:\n    effective 2026-01-01\n    source "s"\n    Email: clear\n'
                            '    Email: vault\n', "twice")):
            with self.assertRaises(PolicyError) as e:
                parse(text)
            self.assertIn(word, str(e.exception))
        with self.assertRaises(PolicyError):
            Store(parse('policy "X" version 1:\n    effective 2027-01-01\n    source "s"\n'
                        'policy "X" version 2:\n    effective 2026-01-01\n    source "s"\n'))


class OverTime(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "policies").mkdir()
        (self.dir / "policies" / "rbi.policy").write_text(textwrap.dedent(POLICIES))
        (self.dir / "onboard.shio").write_text(textwrap.dedent(PROGRAM))
        self.signer = ledger.load_signer("hmac:k")
        self.records = self.dir / "records"
        shio.configure(str(self.records), signer=self.signer, policies=str(self.dir / "policies"))
        self.mod = shio.load(self.dir / "onboard.shio")
        self.store = Store.load(self.dir / "policies")

    def tearDown(self):
        shio.configure("shio-traces", vault=False, signer=False, policies=False)
        self.tmp.cleanup()

    def decide(self, day):
        with at(day):
            self.mod.onboard(FORM, self.mod.clerk)
        path = Path(self.mod.onboard.last_trace)
        rec = json.loads(path.read_text())
        everything = json.dumps(rec) + (self.records / rec["vault"]["file"]).read_text()
        return path, rec, everything

    def test_each_decision_follows_the_rule_of_its_date(self):
        _, rec, blob = self.decide("2026-06-01")
        v = rec["readings"][0]["value"]
        self.assertEqual(rec["data_policy"]["in_force"][0]["version"], 3)
        self.assertEqual(v["email"]["handling"], "hashed")
        self.assertEqual(v["phone"], "+91 98200 12345")
        self.assertNotIn("asha.rao@example.in", blob)         # hashed: in no prompt, answer or outcome either
        self.assertIn("+91 98200 12345", blob)

        _, rec, blob = self.decide("2027-02-01")
        v = rec["readings"][0]["value"]
        self.assertEqual(rec["data_policy"]["in_force"][0]["version"], 4)
        self.assertEqual(v["email"], "asha.rao@example.in")
        self.assertEqual(v["phone"]["handling"], "hashed")
        self.assertEqual(v["others"][0]["phone"]["handling"], "hashed")   # nested in a list of another type
        self.assertNotIn("98200 12345", blob)
        self.assertNotIn("98200 77777", blob)
        self.assertTrue(ledger.verify(self.records, secret=b"k")[0])

    def test_audit_migrate_and_verify(self):
        self.decide("2026-06-01")
        self.decide("2027-02-01")
        ok, report, _ = audit(self.records, self.store, dt.date(2027, 3, 1))
        self.assertTrue(ok, report)
        done = ledger.migrate(self.records, self.store.handling(dt.date(2027, 3, 1)), "circular 112", self.signer)
        self.assertEqual([(c[1], c[2], c[3]) for c in done[0][1]], [("phone", "clear", "hashed"),
                                                                    ("others[].phone", "clear", "hashed")])
        path = self.records / done[0][0]
        rec = json.loads(path.read_text())
        blob = json.dumps(rec) + (self.records / rec["vault"]["file"]).read_text()
        self.assertNotIn("98200 12345", blob)
        self.assertEqual(rec["data_policy"]["in_force"][0]["version"], 3)   # still the policy it was decided under
        ok, report, _ = ledger.verify(self.records, secret=b"k")
        self.assertTrue(ok, report)
        self.assertIn("re-sealed", "\n".join(report))
        self.assertTrue(audit(self.records, self.store, dt.date(2027, 3, 1))[0])
        self.assertEqual(ledger.migrate(self.records, self.store.handling(dt.date(2027, 3, 1)), "again",
                                        self.signer), [])                    # nothing left to tighten

    def test_migration_never_loosens(self):
        self.decide("2026-06-01")                  # email hashed under v3; v4 allows clear
        done = ledger.migrate(self.records, self.store.handling(dt.date(2027, 3, 1)), "x", self.signer, dry_run=True)
        self.assertNotIn("email", [c[1] for _, cs in done for c in cs])

    def test_audit_findings(self):
        path, _, _ = self.decide("2026-06-01")
        text = (self.dir / "policies" / "rbi.policy").read_text()
        (self.dir / "policies" / "rbi.policy").write_text(text.replace("PhoneNumber: clear", "PhoneNumber: vault"))
        ok, report, _ = audit(self.records, Store.load(self.dir / "policies"), dt.date(2027, 3, 1))
        self.assertFalse(ok)
        self.assertIn("has been edited since this record used it", "\n".join(report))
        (self.dir / "policies" / "rbi.policy").write_text(text)
        backdated = Store(parse(textwrap.dedent(POLICIES).replace("effective 2027-01-01", "effective 2026-03-01")))
        ok, report, _ = audit(self.records, backdated, dt.date(2027, 3, 1))
        self.assertFalse(ok)
        self.assertIn("but v4 was in force", "\n".join(report))

    def test_retention(self):
        self.decide("2026-06-01")
        self.assertEqual(retention(self.records, self.store, today=dt.date(2027, 5, 1)), [])
        due = retention(self.records, self.store, self.signer, apply=True, today=dt.date(2027, 7, 1))
        self.assertEqual(len(due), 1)
        ok, report, _ = ledger.verify(self.records, secret=b"k")
        self.assertTrue(ok, report)
        self.assertIn("retention ended", "\n".join(report))

    def test_check_and_impact(self):
        prog = self.dir / "onboard.shio"
        ok, lines = check_programs([prog], self.store, dt.date(2027, 2, 1))
        self.assertTrue(ok, lines)
        no_name = Store(parse(textwrap.dedent(POLICIES).replace("    FullName:    vault\n", "")))
        ok, lines = check_programs([prog], no_name, dt.date(2027, 2, 1))
        self.assertFalse(ok)
        self.assertIn("NO RULE", "\n".join(lines))
        lines = "\n".join(impact([prog], self.store, dt.date(2027, 1, 1)))
        self.assertIn("email (Email) hashed -> clear", lines)
        self.assertIn("phone (PhoneNumber) clear -> hashed", lines)
        self.assertIn("shio migrate", lines)


class Compile(unittest.TestCase):
    def test_regulated_programs_must_classify_personal_fields(self):
        src = textwrap.dedent(PROGRAM).replace("    phone: str as PhoneNumber\n    pan", "    phone: str\n    pan")
        with self.assertRaises(ShioError) as e:
            compile_source(src, "t.shio")
        self.assertIn("'phone' looks like personal data", str(e.exception))
        compile_source(src.replace("regulated\n", ""), "t.shio")      # outside regulated mode it is the author's call

    def test_no_policy_store_keeps_classified_fields_in_the_vault(self):
        with TemporaryDirectory() as tmp:
            (Path(tmp) / "p.shio").write_text(textwrap.dedent(PROGRAM).replace("regulated\n", ""))
            shio.configure(tmp, vault=True, signer=False, policies=False)
            try:
                mod = shio.load(Path(tmp) / "p.shio")
                mod.onboard(FORM, mod.clerk)
                rec = json.loads(Path(mod.onboard.last_trace).read_text())
                self.assertEqual(rec["readings"][0]["value"]["email"]["handling"], "vault")
                self.assertNotIn("data_policy", rec)
            finally:
                shio.configure("shio-traces", vault=False, signer=False, policies=False)


if __name__ == "__main__":
    unittest.main()
