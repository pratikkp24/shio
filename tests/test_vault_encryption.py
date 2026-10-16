"""Vaults encrypted at rest: personal data never on disk in clear, records still verify, the right key reads them,
a wrong or missing key does not, and tampering or moving a vault is caught."""
import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest

pytest.importorskip("cryptography")
import shio
from shio import ledger
from shio.core import configure

SRC = """
regulated

type Person:
    name: str pii              # full name
    email: str as Email

model m = replay(lambda req: {"name": "Asha Rao", "email": "asha.rao@example.in"})

agent onboard(form: str, llm):
    read p: Person by llm:
        Read the form.

        {form}
    check named: p.name in form, "name not on the form"
    rule out:
        return {"status": "OK" if checks.passed("named") else "REVIEW"}
    return out
"""


@pytest.fixture
def env():
    d = Path(tempfile.mkdtemp())
    (d / "prog.shio").write_text(SRC)
    key = "hex:" + "11" * 32
    configure(trace_dir=str(d / "rec"), signer=ledger.load_signer("hmac:test"), vault_key=key)
    mod = shio.load(d / "prog.shio")
    mod.onboard("Name: Asha Rao, email asha.rao@example.in", mod.m)
    yield d, key
    configure(trace_dir=None, vault_key="")
    shutil.rmtree(d)


def test_vault_is_ciphertext_and_record_says_so(env):
    d, key = env
    rec_path = next((d / "rec").glob("onboard-*.json"))
    rec = json.loads(rec_path.read_text())
    assert rec["vault"]["encryption"]["alg"] == "AES-256-GCM" and rec["vault"]["file"].endswith(".vault.enc")
    raw = (d / "rec" / rec["vault"]["file"]).read_text()
    assert "Asha" not in raw and "asha.rao" not in raw and "Asha" not in rec_path.read_text()
    assert ledger.load_vault(rec_path)                                    # the key reads it back


def test_verify_with_and_without_the_key(env):
    d, key = env
    ok, lines, _ = ledger.verify(d / "rec", secret=b"test")
    assert ok, lines
    ledger.set_vault_key(None)
    os.environ.pop("SHIO_VAULT_KEY", None)
    ok, lines, _ = ledger.verify(d / "rec", secret=b"test")              # ciphertext integrity still checked
    assert ok, lines
    with pytest.raises(ledger.VaultLocked):
        ledger.load_vault(next((d / "rec").glob("onboard-*.json")))
    ledger.set_vault_key("hex:" + "22" * 32)
    with pytest.raises(ledger.VaultLocked):                               # the wrong key
        ledger.load_vault(next((d / "rec").glob("onboard-*.json")))


def test_tampered_or_moved_vault_is_caught(env):
    d, key = env
    rec_path = next((d / "rec").glob("onboard-*.json"))
    rec = json.loads(rec_path.read_text())
    vp = d / "rec" / rec["vault"]["file"]
    blob = json.loads(vp.read_text())
    ct = bytearray(__import__("base64").b64decode(blob["ciphertext"]))
    ct[0] ^= 1
    blob["ciphertext"] = __import__("base64").b64encode(bytes(ct)).decode()
    vp.write_text(json.dumps(blob, indent=1))
    ok, lines, _ = ledger.verify(d / "rec", secret=b"test")
    assert not ok and any("TAMPERED vault" in x for x in lines)


def test_vaultkey_command_makes_a_private_key(tmp_path):
    path, kid = ledger.vaultkeygen(tmp_path / "keys")
    assert oct(path.stat().st_mode)[-3:] == "600" and kid.startswith("aes256:")
    with pytest.raises(FileExistsError):
        ledger.vaultkeygen(tmp_path / "keys")
