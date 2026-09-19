"""
Properties of credential storage. Run: python tests/test_vault.py

Skips itself without `cryptography` (see requirements.txt), which the local
tool does not need.

A stored Anthropic key is spendable and belongs to someone else's account, so
the bar is higher than for the memoir it sits beside:

  1. the plaintext never reaches the database file
  2. a ciphertext is bound to its owner and will not decrypt for another
  3. a wrong or missing master key fails closed, never open
  4. only a hint is ever readable back
  5. a database written before credentials existed migrates in place
"""

from __future__ import annotations

import pathlib as _pathlib
import sys as _sys
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parents[1] / "src"))

import os
import shutil
import sqlite3
import tempfile

try:
    import cryptography  # noqa: F401
except ImportError:
    print("skip  cryptography not installed (pip install -r requirements.txt)")
    raise SystemExit(0)

from sargam import accounts as ACC
from sargam import vault

REAL_KEY = "sk-ant-api03-PLAINTEXT-MUST-NEVER-LAND-9876"


class tmp:
    def __enter__(self):
        self.dir = _pathlib.Path(tempfile.mkdtemp(prefix="sargam-vault-"))
        self.saved = os.environ.get(vault.ENV)
        os.environ[vault.ENV] = vault.generate()
        return self.dir

    def __exit__(self, *exc):
        shutil.rmtree(self.dir, ignore_errors=True)
        if self.saved is None:
            os.environ.pop(vault.ENV, None)
        else:
            os.environ[vault.ENV] = self.saved
        return False


# --------------------------------------------------------------------- tests

def test_sealed_text_is_not_the_plaintext() -> None:
    with tmp():
        ct, nonce = vault.seal(REAL_KEY, owner="u_alice")
        assert REAL_KEY.encode() not in ct
        assert vault.open_(ct, nonce, "u_alice") == REAL_KEY
    print("ok  a sealed credential is not its plaintext and round-trips")


def test_a_ciphertext_is_bound_to_its_owner() -> None:
    """Binding the account id in as associated data is what stops a row being
    lifted from one account into another."""
    with tmp():
        ct, nonce = vault.seal(REAL_KEY, owner="u_alice")
        try:
            vault.open_(ct, nonce, "u_bob")
        except vault.VaultError:
            pass
        else:
            raise AssertionError("decrypted under the wrong owner")
    print("ok  a ciphertext will not decrypt for a different account")


def test_a_wrong_or_missing_master_key_fails_closed() -> None:
    with tmp():
        ct, nonce = vault.seal(REAL_KEY, owner="u_alice")
        os.environ[vault.ENV] = vault.generate()          # rotated / wrong
        try:
            vault.open_(ct, nonce, "u_alice")
        except vault.VaultError:
            pass
        else:
            raise AssertionError("decrypted with a different master key")

        os.environ.pop(vault.ENV)
        assert vault.available() is False
        try:
            vault.seal(REAL_KEY, owner="u_alice")
        except vault.VaultError as exc:
            assert vault.ENV in str(exc)
        else:
            raise AssertionError("sealed without a master key")
    print("ok  a wrong or absent master key fails closed")


def test_the_store_never_holds_the_plaintext() -> None:
    with tmp() as d:
        acc = ACC.Accounts(d / "accounts.db")
        u = acc.upsert_google({"sub": "s1", "email": "a@x.com", "name": "A"})
        assert acc.key_status(u["id"])["has_key"] is False

        hint = acc.set_api_key(u["id"], REAL_KEY)
        assert hint.endswith("9876") and REAL_KEY not in hint
        st = acc.key_status(u["id"])
        assert st["has_key"] and st["hint"] == hint
        assert acc.get_api_key(u["id"]) == REAL_KEY
        acc.close()

        blob = (d / "accounts.db").read_bytes()
        assert REAL_KEY.encode() not in blob, "plaintext reached the database"
        for p in d.rglob("*"):
            if p.is_file():
                assert REAL_KEY.encode() not in p.read_bytes(), p.name
    print("ok  the plaintext credential never reaches disk")


def test_a_row_moved_between_accounts_is_useless() -> None:
    with tmp() as d:
        acc = ACC.Accounts(d / "accounts.db")
        a = acc.upsert_google({"sub": "sa", "email": "a@x.com", "name": "A"})
        b = acc.upsert_google({"sub": "sb", "email": "b@x.com", "name": "B"})
        acc.set_api_key(a["id"], REAL_KEY)

        row = acc.db.execute(
            "SELECT api_key_ct, api_key_nonce FROM users WHERE id = ?",
            (a["id"],)).fetchone()
        acc.db.execute(
            "UPDATE users SET api_key_ct = ?, api_key_nonce = ? WHERE id = ?",
            (row["api_key_ct"], row["api_key_nonce"], b["id"]))
        acc.db.commit()

        assert acc.get_api_key(b["id"]) is None, \
            "a stolen row decrypted for another account"
        assert acc.get_api_key(a["id"]) == REAL_KEY, "the owner lost their key"
        acc.close()
    print("ok  a credential row copied between accounts decrypts for neither")


def test_clearing_removes_everything() -> None:
    with tmp() as d:
        acc = ACC.Accounts(d / "accounts.db")
        u = acc.upsert_google({"sub": "s1", "email": "a@x.com", "name": "A"})
        acc.set_api_key(u["id"], REAL_KEY)
        acc.clear_api_key(u["id"])
        assert acc.get_api_key(u["id"]) is None
        assert acc.key_status(u["id"]) == {"has_key": False, "hint": None,
                                           "set_at": None}
        acc.close()
    print("ok  clearing a credential leaves no hint and no ciphertext")


def test_an_older_accounts_database_migrates() -> None:
    with tmp() as d:
        con = sqlite3.connect(d / "old.db")
        con.executescript(
            "CREATE TABLE users (id TEXT PRIMARY KEY, google_sub TEXT NOT NULL "
            "UNIQUE, email TEXT, name TEXT, created_at TEXT NOT NULL, "
            "last_seen_at TEXT NOT NULL);")
        con.execute("INSERT INTO users VALUES "
                    "('u1','sub1','a@x.com','A','2020','2020')")
        con.commit()
        con.close()

        acc = ACC.Accounts(d / "old.db")
        cols = {r["name"] for r in acc.db.execute("PRAGMA table_info(users)")}
        for needed in ("api_key_ct", "api_key_nonce", "api_key_hint",
                       "api_key_set_at"):
            assert needed in cols, f"{needed} not migrated in"
        assert acc.get("u1")["email"] == "a@x.com", "existing account lost"
        acc.set_api_key("u1", REAL_KEY)
        assert acc.get_api_key("u1") == REAL_KEY
        assert acc._migrate() == [], "migration is not idempotent"
        acc.close()
    print("ok  an accounts database predating credentials migrates in place")


if __name__ == "__main__":
    test_sealed_text_is_not_the_plaintext()
    test_a_ciphertext_is_bound_to_its_owner()
    test_a_wrong_or_missing_master_key_fails_closed()
    test_the_store_never_holds_the_plaintext()
    test_a_row_moved_between_accounts_is_useless()
    test_clearing_removes_everything()
    test_an_older_accounts_database_migrates()
    print("\nall vault properties hold")
