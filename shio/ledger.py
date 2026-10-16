"""Decision records an auditor can rely on.

Every top-level run is written as a record, and every record is entered in the folder's ledger (ledger.jsonl):
  - the record's sha256 over a canonical JSON form;
  - the previous entry's hash, so the ledger is a hash chain: an edited, deleted, inserted or reordered record breaks it;
  - a signature over the hash: Ed25519 (`shio keygen`, verifiable by anyone with the public key) or HMAC-SHA256 (a
    shared secret, verifiable only by its holders).

Free text (prompts, sources, raw model answers, check explanations, inputs) can go to a separate vault file; the
record keeps a sha256 of each item, so it still proves what was there. Erasing a vault (`shio erase`) is itself an
entry in the chain. `shio verify` checks all of it and prints the chain's head hash, which should be anchored
somewhere outside the folder (a ticket, a WORM bucket): a chain can only be trusted as far as its head is.

A decision that needs a person (`signoff` in Shio) is recorded as PENDING with a sign-off request. `shio signoff`
answers it with a signed entry in the same chain: who, in which role, when, what their signature means, and the hash
of the record they saw. Maker-checker holds: the signer can't be the requester, anyone the record names as a maker,
or someone who already signed it.
"""
import base64
import contextlib
import hashlib
import hmac
import json
import os
import time
from pathlib import Path

LEDGER = "ledger.jsonl"
GENESIS = "0" * 64
FORMAT = "shio-record/1"


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode()


def sha256(obj):
    data = obj if isinstance(obj, bytes) else canonical(obj)
    return hashlib.sha256(data).hexdigest()


def text_sha256(text):
    return hashlib.sha256((text or "").encode()).hexdigest()


# ---------------- signing ----------------
class Signer:
    def __init__(self, kind, key, key_id):
        self.kind, self.key, self.key_id = kind, key, key_id

    def sign(self, digest_hex):
        if self.kind == "hmac":
            return hmac.new(self.key, digest_hex.encode(), hashlib.sha256).hexdigest()
        return base64.b64encode(self.key.sign(digest_hex.encode())).decode()


def load_signer(spec=None):
    """SHIO_SIGNING_KEY: a path to an Ed25519 private key (from `shio keygen`), or `hmac:<secret>`."""
    spec = spec if spec is not None else os.getenv("SHIO_SIGNING_KEY", "")
    if not spec:
        return None
    if spec.startswith("hmac:"):
        secret = spec[5:].encode()
        return Signer("hmac", secret, "hmac:" + hashlib.sha256(secret).hexdigest()[:16])
    from cryptography.hazmat.primitives import serialization
    key = serialization.load_pem_private_key(Path(spec).expanduser().read_bytes(), password=None)
    return Signer("ed25519", key, key_id(key.public_key()))


def key_id(public_key):
    from cryptography.hazmat.primitives import serialization
    raw = public_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return "ed25519:" + hashlib.sha256(raw).hexdigest()[:16]


def keygen(folder):
    """Writes shio-signing.key (keep it secret: 0600) and shio-signing.pub (give it to auditors)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    d = Path(folder)
    d.mkdir(parents=True, exist_ok=True)
    key = Ed25519PrivateKey.generate()
    priv, pub = d / "shio-signing.key", d / "shio-signing.pub"
    priv.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))
    os.chmod(priv, 0o600)
    pub.write_bytes(key.public_key().public_bytes(serialization.Encoding.PEM,
                                                  serialization.PublicFormat.SubjectPublicKeyInfo))
    return priv, pub, key_id(key.public_key())


def _check_signature(entry, public_key=None, secret=None):
    sig, kid = entry.get("signature"), entry.get("key_id") or ""
    if not sig:
        return "unsigned"
    if kid.startswith("hmac:"):
        if secret is None:
            return "unverified (HMAC: needs the secret)"
        good = hmac.new(secret, entry["sha256"].encode(), hashlib.sha256).hexdigest()
        return "ok" if hmac.compare_digest(good, sig) else "BAD SIGNATURE"
    if public_key is None:
        return "unverified (no public key given)"
    if key_id(public_key) != kid:
        return f"BAD SIGNATURE (signed by {kid}, not this key)"
    try:
        public_key.verify(base64.b64decode(sig), entry["sha256"].encode())
        return "ok"
    except Exception:
        return "BAD SIGNATURE"


def load_public_key(path):
    from cryptography.hazmat.primitives import serialization
    return serialization.load_pem_public_key(Path(path).expanduser().read_bytes())


# ---------------- vault ----------------
VAULT_FIELDS_RUN = ("inputs",)
VAULT_FIELDS_READING = ("raw", "error")
VAULT_FIELDS_REQUEST = ("ask", "context", "system")


def _put(vault, path, value):
    if value in (None, "", {}, []):
        return value
    vault[path] = value
    return {"$vault": sha256(value)}


def _redact_value(value, pii):
    """A reading's value with its personal-data fields replaced by their hash."""
    if not pii or not isinstance(value, dict):
        return value
    return {k: ({"$pii": sha256(v)} if k in pii and v is not None else v) for k, v in value.items()}


SCRUB_MIN = 4      # shorter values (a "5", a "No") would scrub ordinary text; such fields are hashed in place only


def _decide(handling):
    """class -> clear | vault | hashed. Without a policy store, every classified field is kept in the vault."""
    return (lambda cls: handling.of(cls)) if handling is not None else (lambda cls: "vault")


def _protected(v):
    return isinstance(v, dict) and "$pii" in v


def _apply(value, classes, decide, scrub, for_vault=False, prefix=""):
    """A reading's value with each classified field handled: clear stays, vault and hashed become a hash. For the
    vault copy (for_vault), only hashed fields are replaced. Values of hashed fields are collected to be scrubbed."""
    if isinstance(value, list):
        return [_apply(v, classes, decide, scrub, for_vault, prefix) for v in value]
    if not isinstance(value, dict) or _protected(value):
        return value
    out = {}
    for k, v in value.items():
        path = f"{prefix}{k}"
        cls = classes.get(path)
        if cls is not None and v is not None and not _protected(v):
            h = decide(cls)
            if h == "hashed" and isinstance(v, (str, int, float)) and len(str(v)) >= SCRUB_MIN:
                scrub[str(v)] = sha256(v)
            if h == "hashed" or (h == "vault" and not for_vault):
                out[k] = {"$pii": sha256(v), "class": cls, "handling": h}
                continue
        if isinstance(v, list):
            out[k] = [_apply(x, classes, decide, scrub, for_vault, f"{path}[].") for x in v]
        elif isinstance(v, dict):
            out[k] = _apply(v, classes, decide, scrub, for_vault, f"{path}.")
        else:
            out[k] = v
    return out


def _scrub(node, scrub, skip=("integrity",)):
    """Replace every occurrence of a hashed value in the strings of a JSON structure."""
    if not scrub:
        return node
    if isinstance(node, str):
        for plain, digest in scrub.items():
            if plain in node:
                node = node.replace(plain, f"[hashed {digest[:12]}]")
        return node
    if isinstance(node, list):
        return [_scrub(v, scrub, ()) for v in node]
    if isinstance(node, dict):
        return {k: (v if k in skip else _scrub(v, scrub, ())) for k, v in node.items()}
    return node


def _readings(run):
    yield from run.get("readings", [])
    for c in run.get("children", []):
        yield from _readings(c)


def split(record, handling=None):
    """(record without free text or personal data, vault holding them by JSON path), under a data-handling policy
    (shio.policy.Handling) if one is given."""
    vault = {}
    decide = _decide(handling)
    scrub = {}
    for r in _readings(record):           # first: which values a policy says must not survive anywhere
        classes = dict(r.get("classes") or {})
        for f in r.get("pii") or ():
            classes.setdefault(f, "PII")
        r["classes"] = classes
        if classes and r.get("value") is not None:
            _apply(r["value"], classes, decide, scrub)
    if handling is not None:
        record["data_policy"] = handling.as_dict()

    def walk(run, base):
        for f in VAULT_FIELDS_RUN:
            run[f] = _put(vault, f"{base}/{f}", run.get(f))
        for name, src in run.get("sources", {}).items():
            if src.get("text"):
                src["text"] = _put(vault, f"{base}/sources/{name}/text", src["text"])
        for i, r in enumerate(run.get("readings", [])):
            p = f"{base}/readings/{i}"
            for f in VAULT_FIELDS_READING:
                r[f] = _put(vault, f"{p}/{f}", r.get(f))
            for f in VAULT_FIELDS_REQUEST:
                r["request"][f] = _put(vault, f"{p}/request/{f}", r["request"].get(f))
            classes = r.get("classes") or {}
            if classes and r.get("value") is not None:
                full = _apply(r["value"], classes, decide, {}, for_vault=True)
                handled = _apply(r["value"], classes, decide, {})
                if handled != full:
                    vault[f"{p}/value"] = full
                r["value"] = handled
                r["handled"] = {path: decide(cls) for path, cls in sorted(classes.items())}
        for i, c in enumerate(run.get("checks", [])):
            p = f"{base}/checks/{i}"
            c["detail"] = _put(vault, f"{p}/detail", c.get("detail"))
            c["args"] = [_put(vault, f"{p}/args/{j}", a) if "const" in a else a for j, a in enumerate(c.get("args", []))]
            c["kwargs"] = {k: (_put(vault, f"{p}/kwargs/{k}", a) if "const" in a else a)
                           for k, a in c.get("kwargs", {}).items()}
        for i, ch in enumerate(run.get("children", [])):
            walk(ch, f"{base}/children/{i}")
    walk(record, "")
    if scrub:                              # hashed values: gone from the vault's free text and the record's
        vault = {k: _scrub(v, scrub, ()) for k, v in vault.items()}
        record = _scrub(record, scrub)
    return record, vault


def merge(record, vault):
    """Put vault items back where split() took them from."""
    for path, value in vault.items():
        parts = path.strip("/").split("/")
        node = record
        for part in parts[:-1]:
            node = node[int(part)] if isinstance(node, list) else node[part]
        last = parts[-1]
        if isinstance(node, list):
            node[int(last)] = value
        else:
            node[last] = value
    return record


# ---------------- writing ----------------
@contextlib.contextmanager
def _locked(folder):
    with open(folder / ".ledger.lock", "a") as lock:
        try:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX)
        except ImportError:      # Windows: no advisory lock; one writer per folder
            pass
        yield


def _entries(folder):
    p = Path(folder) / LEDGER
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def _append(folder, entry, signer):
    """Chain and sign one ledger entry (a record or an event) and append it. Caller holds the lock."""
    entries = _entries(folder)
    entry["seq"] = len(entries)
    entry["prev"] = entries[-1]["sha256"] if entries else GENESIS
    if signer:
        entry["key_id"] = signer.key_id
        entry["signature"] = signer.sign(entry["sha256"])
    with open(Path(folder) / LEDGER, "a") as f:
        f.write(json.dumps(entry, sort_keys=True) + "\n")
    return entry


def record_hash(record):
    body = {k: v for k, v in record.items() if k != "integrity"}
    body["integrity"] = {k: record["integrity"][k] for k in ("seq", "prev")}
    return sha256(body)


def write(record, folder, stem, use_vault=False, signer=None, handling=None):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    record["format"] = FORMAT
    vault = None
    if use_vault or handling is not None:
        record, vault = split(record, handling)
    with _locked(folder):
        entries = _entries(folder)
        if vault:
            vdir = folder / "vault"
            vdir.mkdir(exist_ok=True)
            vpath = vdir / f"{stem}.vault.json"
            vpath.write_text(json.dumps(vault, indent=1, ensure_ascii=False, default=str))
            record["vault"] = {"file": f"vault/{vpath.name}", "sha256": sha256(vault)}
        record["integrity"] = {"seq": len(entries), "prev": entries[-1]["sha256"] if entries else GENESIS}
        digest = record_hash(record)
        record["integrity"].update(sha256=digest)
        if signer:
            record["integrity"].update(key_id=signer.key_id, signature=signer.sign(digest))
        path = folder / f"{stem}.json"
        path.write_text(json.dumps(record, indent=1, ensure_ascii=False, default=str))
        _append(folder, {"kind": "record", "file": path.name, "sha256": digest, "time": time.time()}, signer)
    return path


def erase(record_path, reason, signer=None):
    """Delete a record's vault (personal data, free text) and enter the erasure in the chain. The record stays."""
    record_path = Path(record_path)
    folder = record_path.parent
    rec = json.loads(record_path.read_text())
    v = rec.get("vault")
    if not v:
        raise ValueError(f"{record_path.name} has no vault to erase")
    with _locked(folder):
        (folder / v["file"]).unlink(missing_ok=True)
        event = {"kind": "erasure", "file": record_path.name, "vault": v["file"], "reason": reason, "time": time.time()}
        event["sha256"] = sha256({k: event[k] for k in ("kind", "file", "vault", "reason", "time")})
        return _append(folder, event, signer)


MIGRATION_FIELDS = ("kind", "file", "from_sha256", "to_sha256", "reason", "policies", "time")


def _tighter(record, handling):
    """Fields this record keeps less protected than the handling now requires: [(reading, path, was, now)]."""
    from .policy import STRICTNESS
    out = []
    for r in _readings(record):
        handled = r.get("handled") or {}
        for path, cls in (r.get("classes") or {}).items():
            was = handled.get(path, "clear" if not r.get("pii") else "vault")
            now = handling.of(cls)
            if STRICTNESS[now] > STRICTNESS[was]:
                out.append((r.get("name"), path, was, now))
    return out


def migrate(folder, handling, reason, signer=None, dry_run=False):
    """Re-seal records written under a less protective rule than `handling` (never a more permissive one). Each
    becomes a signed migration entry chaining the record's old hash to its new one; verify follows the chain.
    A handling resolved for a jurisdiction (Store.handling(on, "IN")) re-seals only that jurisdiction's records."""
    import copy
    folder = Path(folder)
    done = []
    with _locked(folder):
        for e in _entries(folder):
            if e.get("kind") != "record":
                continue
            path = folder / e["file"]
            if not path.exists():
                continue
            rec = json.loads(path.read_text())
            scope = getattr(handling, "jurisdiction", None)
            if scope is not None and rec.get("jurisdiction") != scope:    # another regulator's records
                continue
            changes = _tighter(rec, handling)
            if not changes:
                continue
            done.append((e["file"], changes))
            if dry_run:
                continue
            old = rec["integrity"]["sha256"]
            vault = load_vault(path, rec)
            full = merge(copy.deepcopy(rec), vault) if vault is not None else copy.deepcopy(rec)
            for r in _readings(full):           # re-apply from the value as it was handled at write time
                r["handled"] = r.get("handled") or {}
            new, new_vault = split(full, handling)
            if vault is None:                    # erased vault: only what the record itself holds can be tightened
                new_vault = None
                new["vault"] = rec.get("vault")
            elif new_vault:
                (folder / rec["vault"]["file"]).write_text(json.dumps(new_vault, indent=1, ensure_ascii=False,
                                                                      default=str))
                new["vault"] = {"file": rec["vault"]["file"], "sha256": sha256(new_vault)}
            # the policy the decision was written under stays its policy; the migration says what was applied later
            new["data_policy"] = rec.get("data_policy")
            new["migrations"] = rec.get("migrations", []) + [
                {"from_sha256": old, "reason": reason, "time": time.time(), "policies": handling.stamps,
                 "tightened": [{"reading": r, "field": f, "was": w, "now": n} for r, f, w, n in changes]}]
            new["integrity"] = {"seq": rec["integrity"]["seq"], "prev": rec["integrity"]["prev"]}
            digest = record_hash(new)
            new["integrity"]["sha256"] = digest
            if signer:
                new["integrity"].update(key_id=signer.key_id, signature=signer.sign(digest))
            path.write_text(json.dumps(new, indent=1, ensure_ascii=False, default=str))
            event = {"kind": "migration", "file": e["file"], "from_sha256": old, "to_sha256": digest,
                     "reason": reason, "policies": handling.stamps, "time": time.time()}
            event["sha256"] = sha256({k: event[k] for k in MIGRATION_FIELDS})
            _append(folder, event, signer)
    return done


def load_vault(record_path, record=None):
    record_path = Path(record_path)
    record = record if record is not None else json.loads(record_path.read_text())
    v = record.get("vault")
    if not v:
        return None
    p = record_path.parent / v["file"]
    return json.loads(p.read_text()) if p.exists() else None


# ---------------- sign-off ----------------
SIGNOFF_FIELDS = ("kind", "file", "record_sha256", "signer", "role", "meaning", "decision", "time")


class SignoffRefused(ValueError):
    pass


def _same_person(a, b):
    return " ".join(str(a).split()).casefold() == " ".join(str(b).split()).casefold()


def signoff_state(request, entries):
    """-> (status, problems) for a sign-off request and the sign-off entries that answer it, in ledger order.
    status: PENDING, APPROVED or REJECTED. problems: sign-offs that break maker-checker or the request."""
    problems, signed, covered, status = [], [], {}, "PENDING"
    for e in entries:
        who = f"sign-off by {e.get('signer')} ({e.get('role')})"
        if status != "PENDING":
            problems.append(f"{who} after the decision was already {status}")
            continue
        if e.get("role") not in request["roles"]:
            problems.append(f"{who}: the request asks for {', '.join(request['roles'])}, not {e.get('role')}")
            continue
        if any(_same_person(e.get("signer"), m) for m in request.get("makers", [])):
            problems.append(f"{who} breaks maker-checker: {e.get('signer')} requested or made this decision")
            continue
        if any(_same_person(e.get("signer"), s) for s in signed):
            problems.append(f"{who} breaks maker-checker: {e.get('signer')} has already signed this decision")
            continue
        if e.get("role") in covered:
            problems.append(f"{who}: the {e.get('role')} sign-off was already given by {covered[e['role']]}")
            continue
        signed.append(e.get("signer"))
        covered[e["role"]] = e.get("signer")
        if e.get("decision") == "rejected":
            status = "REJECTED"
        elif all(r in covered for r in request["roles"]):
            status = "APPROVED"
    return status, problems


def _record_entry(entries, name):
    for e in entries:
        if e.get("kind", "record") == "record" and e.get("file") == name:
            return e
    return None


def status(record_path):
    """-> (request or None, status, the sign-off entries) for one record."""
    record_path = Path(record_path)
    rec = json.loads(record_path.read_text())
    req = rec.get("signoff")
    entries = [e for e in _entries(record_path.parent) if e.get("kind") == "signoff" and e.get("file") == record_path.name]
    if not req:
        return None, "DECIDED", entries
    return req, signoff_state(req, entries)[0], entries


def signoff(record_path, signer_name, role, meaning, signer=None, reject=False, personal_key=None):
    """A person signs a pending decision. Refused (SignoffRefused) if the record is not pending, has been changed
    since it was entered in the ledger, asks for another role, or the person is its maker or has signed it already.
    signer: the ledger's key, which signs the entry like any other. personal_key: the person's own Ed25519 key
    (optional), which signs it too, so the sign-off can be tied to them and not only to the system."""
    record_path = Path(record_path)
    folder = record_path.parent
    if not signer_name.strip() or not role.strip() or not meaning.strip():
        raise SignoffRefused("a sign-off needs the signer's name, their role and what the signature means")
    with _locked(folder):
        rec = json.loads(record_path.read_text())
        req = rec.get("signoff")
        if not req:
            raise SignoffRefused(f"{record_path.name} does not need sign-off: its outcome was decided without a person")
        entries = _entries(folder)
        entry = _record_entry(entries, record_path.name)
        if entry is None:
            raise SignoffRefused(f"{record_path.name} is not in the ledger")
        if "integrity" not in rec or record_hash(rec) != entry["sha256"]:
            raise SignoffRefused(f"{record_path.name} has changed since it was entered in the ledger: not signing it")
        if rec.get("integrity", {}).get("signature") and signer is None:
            raise SignoffRefused("the record is signed, so its sign-off must be too: set SHIO_SIGNING_KEY")
        prior = [e for e in entries if e.get("kind") == "signoff" and e.get("file") == record_path.name]
        event = {"kind": "signoff", "file": record_path.name, "record_sha256": entry["sha256"], "signer": signer_name,
                 "role": role, "meaning": meaning, "decision": "rejected" if reject else "approved", "time": time.time()}
        state, problems = signoff_state(req, prior + [event])
        new = [p for p in problems if p not in signoff_state(req, prior)[1]]
        if new:
            raise SignoffRefused(new[0])
        event["sha256"] = sha256({k: event[k] for k in SIGNOFF_FIELDS})
        if personal_key is not None:
            event["signer_key_id"] = key_id(personal_key.public_key())
            event["signer_signature"] = base64.b64encode(personal_key.sign(event["sha256"].encode())).decode()
        _append(folder, event, signer)
        return event, state


def load_private_key(path):
    from cryptography.hazmat.primitives import serialization
    return serialization.load_pem_private_key(Path(path).expanduser().read_bytes(), password=None)


# ---------------- verifying ----------------
def verify(folder, public_key=None, secret=None, head=None, signer_keys=()):
    """-> (ok, lines of report, head hash). signer_keys: public keys of people who sign off with their own key."""
    folder = Path(folder)
    entries = _entries(folder)
    problems, notes = [], []
    if not entries:
        return False, [f"{folder}: no ledger"], None
    erased = {e["file"]: e for e in entries if e.get("kind") == "erasure"}
    migrations = {}
    for e in entries:
        if e.get("kind") == "migration":
            migrations.setdefault(e["file"], []).append(e)
    people = {key_id(k): k for k in signer_keys}
    signoffs, requests, record_hashes = {}, {}, {}
    seen = set()
    prev = GENESIS
    sig_states = {}
    for i, e in enumerate(entries):
        where = f"entry {i}"
        if e.get("seq") != i:
            problems.append(f"{where}: sequence number {e.get('seq')} out of order")
        if e.get("prev") != prev:
            problems.append(f"{where}: chain broken (an entry before it was changed, removed or reordered)")
        prev = e.get("sha256")
        state = _check_signature(e, public_key, secret)
        sig_states[state] = sig_states.get(state, 0) + 1
        if state.startswith("BAD"):
            problems.append(f"{where} ({e.get('file')}): {state}")
        if e.get("kind") == "erasure":
            body = {k: e[k] for k in ("kind", "file", "vault", "reason", "time")}
            if sha256(body) != e["sha256"]:
                problems.append(f"{where}: erasure entry edited")
            continue
        if e.get("kind") == "migration":
            if sha256({k: e.get(k) for k in MIGRATION_FIELDS}) != e.get("sha256"):
                problems.append(f"{where}: migration entry edited")
            continue
        if e.get("kind") == "signoff":
            if sha256({k: e.get(k) for k in SIGNOFF_FIELDS}) != e.get("sha256"):
                problems.append(f"{where}: sign-off entry edited")
                continue
            if e.get("file") not in record_hashes:
                problems.append(f"{where}: sign-off for {e.get('file')}, which is not an earlier record in the ledger")
                continue
            if e.get("record_sha256") != record_hashes[e["file"]]:
                problems.append(f"{where}: sign-off by {e.get('signer')} is for a different version of {e['file']}")
            if e.get("signer_key_id"):
                k = people.get(e["signer_key_id"])
                if k is None:
                    notes.append(f"{e['file']}: {e.get('signer')}'s own signature not checked (no --signer-pubkey "
                                 f"for {e['signer_key_id']})")
                else:
                    try:
                        k.verify(base64.b64decode(e["signer_signature"]), e["sha256"].encode())
                    except Exception:
                        problems.append(f"{where}: BAD SIGNATURE from {e.get('signer')}'s own key")
            signoffs.setdefault(e["file"], []).append(e)
            continue
        path = folder / e["file"]
        seen.add(e["file"])
        record_hashes[e["file"]] = e.get("sha256")
        if not path.exists():
            problems.append(f"{e['file']}: MISSING (in the ledger, not on disk)")
            continue
        rec = json.loads(path.read_text())
        integ = rec.get("integrity", {})
        if integ.get("seq") != i or integ.get("prev") != e.get("prev"):
            problems.append(f"{e['file']}: record's own chain position does not match the ledger")
        actual = record_hash(rec) if "integrity" in rec else None
        expected = e["sha256"]
        for m in migrations.get(e["file"], []):      # re-sealed under a stricter policy: follow the signed chain
            if m.get("from_sha256") == expected:
                expected = m["to_sha256"]
        if expected != e["sha256"] and actual == expected:
            n = len(migrations[e["file"]])
            notes.append(f"{e['file']}: re-sealed {n} time{'s' if n > 1 else ''} under a stricter data policy")
        if actual != expected or integ.get("sha256") != expected:
            problems.append(f"{e['file']}: TAMPERED (its content no longer matches the hash in the ledger)")
            continue
        if rec.get("signoff"):
            requests[e["file"]] = rec["signoff"]
        v = rec.get("vault")
        if v:
            vp = folder / v["file"]
            if vp.exists():
                vault = json.loads(vp.read_text())
                if sha256(vault) != v["sha256"]:
                    problems.append(f"{v['file']}: TAMPERED vault")
            elif e["file"] in erased:
                er = erased[e["file"]]
                notes.append(f"{e['file']}: vault erased {time.strftime('%Y-%m-%d', time.localtime(er['time']))} "
                             f"({er['reason']})")
            else:
                problems.append(f"{v['file']}: vault MISSING with no erasure entry")
    pending = 0
    for name, req in requests.items():
        state, bad = signoff_state(req, signoffs.get(name, []))
        problems += [f"{name}: {b}" for b in bad]
        if state == "PENDING":
            pending += 1
            given = {s["role"] for s in signoffs.get(name, [])}
            notes.append(f"{name}: PENDING sign-off by " + ", ".join(r for r in req["roles"] if r not in given))
    for name in signoffs:
        if name not in requests and name in record_hashes and (folder / name).exists():
            problems.append(f"{name}: signed off, but its record asks for no sign-off")
    on_disk = {p.name for p in folder.glob("*.json")}
    for extra in sorted(on_disk - seen):
        problems.append(f"{extra}: not in the ledger (written outside it, or a ledger entry was removed)")
    head_hash = entries[-1]["sha256"]
    if head and not head_hash.startswith(head):
        problems.append(f"head is {head_hash[:16]}…, not the anchored {head[:16]}… (entries were added or replaced)")
    report = [f"{len(entries)} ledger entries, {len(seen)} records, {len(erased)} erasures, "
              f"{sum(len(v) for v in signoffs.values())} sign-offs, {pending} pending",
              "signatures: " + ", ".join(f"{n} {s}" for s, n in sig_states.items())]
    report += [f"  note: {n}" for n in notes]
    report += [f"  PROBLEM: {p}" for p in problems]
    report.append(f"head: {head_hash}")
    return not problems, report, head_hash
