"""Ledger probes (docs/REVIEW-BRIEF.md, findings L1-L3): an attacker who can write the trace folder but does not hold
the signing key. Needs the [regulated] extra. Run from the repository root:  python docs/review/probes/ledger_probes.py"""
import json, time
from pathlib import Path
from tempfile import TemporaryDirectory
from shio import ledger as L

def fresh(tmp):
    d = Path(tmp)
    priv, pub, kid = L.keygen(d / "keys")
    signer, pubkey = L.load_signer(str(priv)), L.load_public_key(pub)
    rec = {"agent": "pay", "outcome": {"status": "PENDING"},
           "signoff": {"roles": ["controller"], "makers": ["maya"], "labels": ["PAY"], "because": ["x"], "lines": [1],
                       "requested_by": "maya", "decided": "PAY"}}
    L.write(rec, d, "r1", signer=signer)
    return d, signer, pubkey

def lines(d): return (d / L.LEDGER).read_text().splitlines()
def put(d, ls): (d / L.LEDGER).write_text("\n".join(ls) + "\n")
def show(label, d, pub):
    ok, rep, head = L.verify(d, public_key=pub)
    st = L.status(d / "r1.json")[1]
    print(f"{label}: verify ok={ok}; sign-off status={st}; " + "; ".join(r.strip() for r in rep if "PROBLEM" in r or "signatures" in r))

# L1: append an UNSIGNED sign-off entry approving the pending decision
with TemporaryDirectory() as tmp:
    d, signer, pub = fresh(tmp)
    rec_entry = json.loads(lines(d)[0])
    ev = {"kind": "signoff", "file": "r1.json", "record_sha256": rec_entry["sha256"], "signer": "Mallory",
          "role": "controller", "meaning": "approved", "decision": "approved", "time": time.time()}
    ev["sha256"] = L.sha256({k: ev[k] for k in L.SIGNOFF_FIELDS})
    ev["seq"], ev["prev"] = 1, rec_entry["sha256"]
    put(d, lines(d) + [json.dumps(ev, sort_keys=True)])
    show("L1 forged unsigned sign-off appended", d, pub)

# L2: delete a signed REJECTION followed only by other events (prev/seq of events are not in their hash)
with TemporaryDirectory() as tmp:
    d, signer, pub = fresh(tmp)
    L.signoff(d / "r1.json", "omar", "controller", "rejected: wrong IBAN", signer=signer, reject=True)
    L.label(d / "r1.json", "wrong", by="ana", signer=signer)
    show("L2 before", d, pub)
    ls = lines(d)
    rec, rej, lab = (json.loads(x) for x in ls)
    lab["seq"], lab["prev"] = 1, rec["sha256"]           # re-link the label over the removed rejection
    put(d, [ls[0], json.dumps(lab, sort_keys=True)])
    show("L2 rejection deleted, label re-linked", d, pub)

# L3: truncate trailing entries (no anchored head)
with TemporaryDirectory() as tmp:
    d, signer, pub = fresh(tmp)
    L.signoff(d / "r1.json", "omar", "controller", "rejected", signer=signer, reject=True)
    put(d, lines(d)[:1])
    show("L3 trailing rejection truncated (no --head)", d, pub)
