"""Jurisdiction-scoped policies: one program serving two regulators. An Indian bank with a UAE branch runs the same
gold-loan program; RBI's data-handling rule and loan-to-value cap govern decisions made for IN, the UAE's for AE, and
a group standard applies everywhere. Each record says which jurisdiction it was decided for, and audit, check,
impact, replay and evidence resolve versions for that jurisdiction. Without one, a regulated program refuses to run."""
import contextlib
import datetime as dt
import io
import json
import os
import textwrap
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import shio
from shio import ledger
from shio.cli import main
from shio.core import RegulatedError
from shio.evidence import collect
from shio.policy import PolicyError, Store, audit, check_programs, impact, parse
from shio.replay import replay

POLICIES = """
policy "Group privacy standard" version 1:
    effective 2026-01-01
    source "Group policy GP-7"
    FullName: vault

policy "RBI data handling" version 3:
    effective 2026-01-01
    applies_to "IN"
    source "RBI circular 2025-26/41, para 3"
    Email:       hashed
    PhoneNumber: clear

policy "RBI data handling" version 4:
    effective 2027-01-01
    applies_to "IN"
    source "RBI circular 2026-27/112, para 5"
    Email:       clear
    PhoneNumber: hashed

policy "UAE PDPL" version 1:
    effective 2026-01-01
    applies_to "AE"
    source "Federal Decree-Law 45/2021, art. 7"
    Email:       clear
    PhoneNumber: hashed

table "Gold loan limits" version 1:
    effective 2026-01-01
    applies_to "IN"
    source "RBI circular 2025-26/9"
    max_ltv: 0.75

table "Gold loan limits" version 2:
    effective 2026-01-01
    applies_to "AE"
    source "CBUAE notice 2025/31"
    max_ltv: 0.85
"""

PROGRAM = """
regulated

type Applicant:
    name: str as FullName
    email: str as Email
    phone: str as PhoneNumber
    gold_grams: float

model clerk = replay(lambda req: {"name": "Asha Rao", "email": "asha.rao@example.in", "phone": "+91 98200 12345",
                                  "gold_grams": 40.0})

agent sanction(form: str, asked: float, llm):
    limits = in_force("Gold loan limits")
    read a: Applicant by llm:
        Read the application.

        {form}
    check weight_on_form: str(int(a.gold_grams)) in form
    rule decision:
        if a is None:
            return {"status": "REVIEW"}
        cap = a.gold_grams * 6412 * limits.max_ltv
        if asked <= cap:
            return {"status": "SANCTION", "amount": asked}
        return {"status": "COUNTER-OFFER", "amount": round(cap)}
    return decision
"""
FORM = "Asha Rao, asha.rao@example.in, +91 98200 12345, 40 g of 22K gold"


def at(day):
    return mock.patch("shio.core.time.time", return_value=dt.datetime.fromisoformat(day + "T10:00").timestamp())


class Parsing(unittest.TestCase):
    def test_applies_to_and_resolution(self):
        store = Store(parse(textwrap.dedent(POLICIES)))
        self.assertEqual(store.jurisdictions, ["AE", "IN"])
        names = lambda j: sorted(p.label() for p in store.in_force(dt.date(2026, 6, 1), j))   # noqa: E731
        self.assertEqual(names("IN"), ["Group privacy standard v1", "RBI data handling v3 [IN]"])
        self.assertEqual(names("AE"), ["Group privacy standard v1", "UAE PDPL v1 [AE]"])
        self.assertEqual(names(None), ["Group privacy standard v1"])      # no jurisdiction: only what applies everywhere
        self.assertEqual(store.handling(dt.date(2026, 6, 1), "IN").of("Email"), "hashed")
        self.assertEqual(store.handling(dt.date(2026, 6, 1), "AE").of("Email"), "clear")
        self.assertEqual(store.table("Gold loan limits", dt.date(2026, 6, 1), "AE").rows, {"max_ltv": 0.85})
        self.assertIsNone(store.table("Gold loan limits", dt.date(2026, 6, 1)))

    def test_unscoped_versions_keep_their_hash(self):
        text = 'policy "X" version 1:\n    effective 2026-01-01\n    source "s"\n    Email: clear\n'
        plain = parse(text)[0]
        self.assertNotIn("applies_to", plain.stamp())
        self.assertNotIn("applies_to", plain.as_dict())     # records written before scoping still audit clean
        scoped = parse(text.replace('    source', '    applies_to "in", "AE"\n    source'))[0]
        self.assertEqual(scoped.applies_to, ("IN", "AE"))
        self.assertNotEqual(plain.sha256, scoped.sha256)

    def test_bad_applies_to_is_refused(self):
        for line in ('applies_to IN', 'applies_to ""', 'applies_to "IN" "AE" extra', 'applies_to "1N"'):
            with self.assertRaises(PolicyError) as e:
                parse(f'policy "X" version 1:\n    effective 2026-01-01\n    {line}\n    source "s"\n')
            self.assertIn("<policy>:3", str(e.exception))

    def test_versions_only_go_forward_within_shared_jurisdictions(self):
        Store(parse('table "T" version 1:\n    effective 2027-01-01\n    applies_to "IN"\n    source "s"\n    x: 1\n'
                    'table "T" version 2:\n    effective 2026-01-01\n    applies_to "AE"\n    source "s"\n    x: 2\n'))
        with self.assertRaises(PolicyError):
            Store(parse('table "T" version 1:\n    effective 2027-01-01\n    source "s"\n    x: 1\n'
                        'table "T" version 2:\n    effective 2026-01-01\n    applies_to "AE"\n    source "s"\n    x: 2\n'))


class TwoRegulators(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "policies").mkdir()
        (self.dir / "policies" / "bank.policy").write_text(textwrap.dedent(POLICIES))
        (self.dir / "gold.shio").write_text(textwrap.dedent(PROGRAM))
        self.signer = ledger.load_signer("hmac:k")
        self.records = self.dir / "records"
        shio.configure(str(self.records), signer=self.signer, policies=str(self.dir / "policies"))
        self.mod = shio.load(self.dir / "gold.shio")
        self.store = Store.load(self.dir / "policies")

    def tearDown(self):
        shio.configure("shio-traces", vault=False, signer=False, policies=False, jurisdiction="")
        self.tmp.cleanup()

    def decide(self, day, jurisdiction, asked=200000):
        shio.configure(str(self.records), jurisdiction=jurisdiction)
        with at(day):
            out = self.mod.sanction(FORM, asked, self.mod.clerk)
        path = Path(self.mod.sanction.last_trace)
        rec = json.loads(path.read_text())
        return out, path, rec, json.dumps(rec) + (self.records / rec["vault"]["file"]).read_text()

    def test_each_record_follows_and_names_its_jurisdiction(self):
        out_in, _, rec, blob = self.decide("2026-06-01", "IN")
        self.assertEqual(rec["jurisdiction"], "IN")
        self.assertEqual(rec["data_policy"]["jurisdiction"], "IN")
        self.assertEqual(sorted(s["name"] for s in rec["data_policy"]["in_force"]),
                         ["Group privacy standard", "RBI data handling"])
        self.assertEqual(rec["readings"][0]["value"]["email"]["handling"], "hashed")
        self.assertNotIn("asha.rao@example.in", blob)
        self.assertEqual((rec["tables"][0]["version"], rec["tables"][0]["applies_to"]), (1, ["IN"]))
        self.assertEqual(out_in, {"status": "COUNTER-OFFER", "amount": 192360})

        out_ae, _, rec, blob = self.decide("2026-06-01", "ae")
        self.assertEqual(rec["jurisdiction"], "AE")
        self.assertEqual(rec["readings"][0]["value"]["email"], "asha.rao@example.in")
        self.assertEqual(rec["readings"][0]["value"]["phone"]["handling"], "hashed")
        self.assertNotIn("98200 12345", blob)
        self.assertEqual(out_ae, {"status": "SANCTION", "amount": 200000})
        self.assertTrue(ledger.verify(self.records, secret=b"k")[0])

    def test_no_jurisdiction_or_an_unknown_one_is_refused(self):
        with at("2026-06-01"), self.assertRaises(RegulatedError) as e:
            self.mod.sanction(FORM, 1000, self.mod.clerk)
        self.assertIn("scoped to jurisdictions (AE, IN)", str(e.exception))
        shio.configure(str(self.records), jurisdiction="UK")
        with at("2026-06-01"), self.assertRaises(RegulatedError) as e:
            self.mod.sanction(FORM, 1000, self.mod.clerk)
        self.assertIn("no policy or table version", str(e.exception))
        self.assertFalse((self.records / "ledger.jsonl").exists())      # nothing was decided
        shio.configure(str(self.records), jurisdiction="")
        with mock.patch.dict(os.environ, {"SHIO_JURISDICTION": "IN"}), at("2026-06-01"):
            self.mod.sanction(FORM, 1000, self.mod.clerk)
        self.assertEqual(json.loads(Path(self.mod.sanction.last_trace).read_text())["jurisdiction"], "IN")

    def test_audit_resolves_each_record_for_its_jurisdiction(self):
        self.decide("2026-06-01", "IN")
        self.decide("2026-06-01", "AE")
        self.decide("2027-02-01", "IN")
        ok, report, info = audit(self.records, self.store, dt.date(2027, 3, 1))
        self.assertTrue(ok, report)
        self.assertIn("3 records checked", report[0])
        self.assertIn("(AE: 1, IN: 2)", report[0])
        ok, report, _ = audit(self.records, self.store, dt.date(2027, 3, 1), jurisdiction="AE")
        self.assertIn("1 records checked", report[0])

        # the UAE rule is edited after use: only the AE record is affected
        edited = Store(parse(textwrap.dedent(POLICIES).replace("Federal Decree-Law 45/2021, art. 7",
                                                               "Federal Decree-Law 45/2021, art. 8")))
        ok, report, info = audit(self.records, edited, dt.date(2027, 3, 1))
        self.assertFalse(ok)
        self.assertEqual(len(info["findings"]), 1)
        self.assertIn("[AE]: UAE PDPL v1 has been edited", info["findings"][0])

    def test_audit_flags_a_record_with_no_jurisdiction(self):
        global_only = self.dir / "global"
        global_only.mkdir()
        (global_only / "g.policy").write_text(textwrap.dedent(POLICIES).split('policy "RBI')[0] +
                                              'table "Gold loan limits" version 1:\n    effective 2026-01-01\n'
                                              '    source "s"\n    max_ltv: 0.75\n')
        shio.configure(str(self.records), policies=str(global_only))
        with at("2026-06-01"):
            self.mod.sanction(FORM, 1000, self.mod.clerk)
        ok, report, _ = audit(self.records, self.store, dt.date(2027, 3, 1))
        self.assertFalse(ok)
        self.assertIn("with no jurisdiction", "\n".join(report))

    def test_check_and_impact_per_jurisdiction(self):
        prog = self.dir / "gold.shio"
        ok, lines = check_programs([prog], self.store, dt.date(2026, 6, 1))
        self.assertTrue(ok, lines)
        text = "\n".join(lines)
        self.assertIn("for AE: Group privacy standard v1, UAE PDPL v1", text)
        self.assertIn("for IN: Group privacy standard v1, RBI data handling v3", text)
        self.assertIn("'Gold loan limits' v2", text)
        lines = "\n".join(impact([prog], self.store, dt.date(2027, 1, 1)))
        self.assertIn("what changes on 2027-01-01 for IN:", lines)
        self.assertIn("email (Email) hashed -> clear", lines)
        ae = lines.split("for AE:")[1].split("what changes")[0]
        self.assertIn("0 fields change", ae)                 # RBI's new version does not touch UAE decisions
        with self.assertRaises(PolicyError):
            check_programs([prog], self.store, dt.date(2026, 6, 1), "UK")

    def test_replay_under_the_records_jurisdiction_or_another(self):
        _, path, _, _ = self.decide("2026-06-01", "IN")
        shio.configure(str(self.records), jurisdiction="")
        ok, lines = replay(path, policies=self.store)
        self.assertTrue(ok, lines)
        self.assertIn("for IN", lines[0])
        ok, lines = replay(path, policies=self.store, jurisdiction="AE")
        self.assertFalse(ok)
        text = "\n".join(lines)
        self.assertIn("recorded v1, now v2", text)
        self.assertIn("SANCTION", text)

    def test_evidence_counts_and_filters_by_jurisdiction(self):
        self.decide("2026-08-01", "IN")
        self.decide("2026-08-02", "AE")
        self.decide("2026-08-03", "AE")
        data = collect(self.records, dt.date(2026, 7, 1), dt.date(2026, 9, 30), self.store, secret=b"k",
                       today=dt.date(2026, 10, 1))
        self.assertEqual(dict(data["by_jurisdiction"]), {"AE": 2, "IN": 1})
        self.assertIn("UAE PDPL v1 (AE)", data["policies"])
        self.assertTrue(data["audit"]["compliant"], data["audit"]["report"])
        data = collect(self.records, dt.date(2026, 7, 1), dt.date(2026, 9, 30), self.store, secret=b"k",
                       jurisdiction="IN")
        self.assertEqual(data["decisions"], 1)
        self.assertNotIn("UAE PDPL v1 (AE)", data["policies"])

    def test_cli(self):
        def run(*argv):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as e:
                main(list(argv))
                raise SystemExit(0)
            return e.exception.code, out.getvalue()
        code, text = run("policy", "show", "--policies", str(self.dir / "policies"), "--at", "2026-06-01")
        self.assertEqual(code, 0)
        self.assertIn("in force on 2026-06-01 for AE:", text)
        self.assertIn("UAE PDPL v1 [AE]", text)
        self.assertIn("table Gold loan limits v1 [IN]", text)
        self.decide("2026-06-01", "IN")
        code, text = run("audit", str(self.records), "--policies", str(self.dir / "policies"), "--jurisdiction", "IN")
        self.assertEqual(code, 0, text)
        self.assertIn("COMPLIANT", text)


if __name__ == "__main__":
    unittest.main()
