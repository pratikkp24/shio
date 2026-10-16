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


def split(record):
    """(record without free text or personal data, vault holding them by JSON path)."""
    vault = {}

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
            pii = set(r.get("pii") or ())
            if pii and isinstance(r.get("value"), dict):
                vault[f"{p}/value"] = r["value"]
                r["value"] = _redact_value(r["value"], pii)
        for i, c in enumerate(run.get("checks", [])):
            p = f"{base}/checks/{i}"
            c["detail"] = _put(vault, f"{p}/detail", c.get("detail"))
            c["args"] = [_put(vault, f"{p}/args/{j}", a) if "const" in a else a for j, a in enumerate(c.get("args", []))]
            c["kwargs"] = {k: (_put(vault, f"{p}/kwargs/{k}", a) if "const" in a else a)
                           for k, a in c.get("kwargs", {}).items()}
        for i, ch in enumerate(run.get("children", [])):
            walk(ch, f"{base}/children/{i}")
    walk(record, "")
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


def write(record, folder, stem, use_vault=False, signer=None):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    record["format"] = FORMAT
    vault = None
    if use_vault:
        record, vault = split(record)
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


def load_vault(record_path, record=None):
    record_path = Path(record_path)
    record = record if record is not None else json.loads(record_path.read_text())
    v = record.get("vault")
    if not v:
        return None
    p = record_path.parent / v["file"]
    return json.loads(p.read_text()) if p.exists() else None


# ---------------- verifying ----------------
def verify(folder, public_key=None, secret=None, head=None):
    """-> (ok, lines of report, head hash)."""
    folder = Path(folder)
    entries = _entries(folder)
    problems, notes = [], []
    if not entries:
        return False, [f"{folder}: no ledger"], None
    erased = {e["file"]: e for e in entries if e.get("kind") == "erasure"}
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
        path = folder / e["file"]
        seen.add(e["file"])
        if not path.exists():
            problems.append(f"{e['file']}: MISSING (in the ledger, not on disk)")
            continue
        rec = json.loads(path.read_text())
        integ = rec.get("integrity", {})
        if integ.get("seq") != i or integ.get("prev") != e.get("prev"):
            problems.append(f"{e['file']}: record's own chain position does not match the ledger")
        actual = record_hash(rec) if "integrity" in rec else None
        if actual != e["sha256"] or integ.get("sha256") != e["sha256"]:
            problems.append(f"{e['file']}: TAMPERED (its content no longer matches the hash in the ledger)")
            continue
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
    on_disk = {p.name for p in folder.glob("*.json")}
    for extra in sorted(on_disk - seen):
        problems.append(f"{extra}: not in the ledger (written outside it, or a ledger entry was removed)")
    head_hash = entries[-1]["sha256"]
    if head and not head_hash.startswith(head):
        problems.append(f"head is {head_hash[:16]}…, not the anchored {head[:16]}… (entries were added or replaced)")
    report = [f"{len(entries)} ledger entries, {len(seen)} records, {len(erased)} erasures",
              "signatures: " + ", ".join(f"{n} {s}" for s, n in sig_states.items())]
    report += [f"  note: {n}" for n in notes]
    report += [f"  PROBLEM: {p}" for p in problems]
    report.append(f"head: {head_hash}")
    return not problems, report, head_hash
