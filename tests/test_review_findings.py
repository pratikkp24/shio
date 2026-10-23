"""Regression tests for the findings in docs/REVIEW-BRIEF.md §4 (found preparing the 1.0 security review). Each test is
the probe in docs/review/probes/ turned into an assertion that the attack now fails."""
import importlib.util
import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from shio import ledger as L

NEEDS_CRYPTO = unittest.skipUnless(importlib.util.find_spec("cryptography"), "needs the [regulated] extra (cryptography)")


def _fresh(tmp):
    d = Path(tmp)
    priv, pub, _ = L.keygen(d / "keys")
    signer, pubkey = L.load_signer(str(priv)), L.load_public_key(pub)
    rec = {"agent": "pay", "outcome": {"status": "PENDING"},
           "signoff": {"roles": ["controller"], "makers": ["maya"], "labels": ["PAY"], "because": ["x"], "lines": [1],
                       "requested_by": "maya", "decided": "PAY"}}
    L.write(rec, d, "r1", signer=signer)
    return d, signer, pubkey


def _lines(d):
    return (d / L.LEDGER).read_text().splitlines()


def _put(d, ls):
    (d / L.LEDGER).write_text("\n".join(ls) + "\n")


@NEEDS_CRYPTO
class LedgerFindings(unittest.TestCase):
    def test_L1_forged_unsigned_signoff_fails_verify(self):
        with TemporaryDirectory() as tmp:
            d, _, pub = _fresh(tmp)
            first = json.loads(_lines(d)[0])
            ev = {"kind": "signoff", "file": "r1.json", "record_sha256": first["sha256"], "signer": "Mallory",
                  "role": "controller", "meaning": "approved", "decision": "approved", "time": time.time()}
            L._chain([first], ev, L.SIGNOFF_FIELDS)          # well formed in every way except the signature
            _put(d, _lines(d) + [json.dumps(ev, sort_keys=True)])
            ok, report, _ = L.verify(d, public_key=pub)
            self.assertFalse(ok)
            self.assertTrue(any("UNSIGNED" in r for r in report), report)
            ok, _, _ = L.verify(d, secret=b"k")              # an HMAC secret is a key too
            self.assertFalse(ok)
            ok, _, _ = L.verify(d)                           # no key: nothing to check signatures against
            self.assertTrue(ok)

    def test_L2_deleting_a_signed_rejection_followed_by_events_is_caught(self):
        with TemporaryDirectory() as tmp:
            d, signer, pub = _fresh(tmp)
            L.signoff(d / "r1.json", "omar", "controller", "rejected: wrong IBAN", signer=signer, reject=True)
            L.label(d / "r1.json", "wrong", by="ana", signer=signer)
            self.assertTrue(L.verify(d, public_key=pub)[0])
            ls = _lines(d)
            rec, _, lab = (json.loads(x) for x in ls)
            lab["seq"], lab["prev"] = 1, rec["sha256"]
            _put(d, [ls[0], json.dumps(lab, sort_keys=True)])
            ok, report, _ = L.verify(d, public_key=pub)
            self.assertFalse(ok, report)
            self.assertEqual(L.status(d / "r1.json")[1], "PENDING")   # what the attacker wanted; verify says no

    def test_L2_events_bind_their_place_and_old_events_still_verify(self):
        with TemporaryDirectory() as tmp:
            d, signer, pub = _fresh(tmp)
            ev, _ = L.signoff(d / "r1.json", "omar", "controller", "ok", signer=signer)
            self.assertEqual(ev["v"], L.EVENT_VERSION)
            self.assertEqual(ev["sha256"], L.sha256({**{k: ev[k] for k in L.SIGNOFF_FIELDS},
                                                     "seq": 1, "prev": ev["prev"], "v": L.EVENT_VERSION}))
            # a ledger written before 1.0: events hash their own fields only
            ls = _lines(d)
            old = json.loads(ls[1])
            del old["v"]
            old["sha256"] = L.sha256({k: old[k] for k in L.SIGNOFF_FIELDS})
            old["signature"] = signer.sign(old["sha256"])
            _put(d, [ls[0], json.dumps(old, sort_keys=True)])
            ok, report, _ = L.verify(d, public_key=pub)
            self.assertTrue(ok, report)
            self.assertTrue(any("pre-1.0 form" in r for r in report), report)
            # ...but an old-form event after a current one is a downgrade, even when chained and signed correctly
            _put(d, ls)
            lab = L.label(d / "r1.json", "right", by="ana", signer=signer)
            del lab["v"]
            lab["sha256"] = L.sha256({k: lab[k] for k in L.LABEL_FIELDS})
            lab["signature"] = signer.sign(lab["sha256"])
            _put(d, ls + [json.dumps(lab, sort_keys=True)])
            ok, report, _ = L.verify(d, public_key=pub)
            self.assertFalse(ok)
            self.assertTrue(any("pre-1.0 form after" in r for r in report), report)


if __name__ == "__main__":
    unittest.main()
