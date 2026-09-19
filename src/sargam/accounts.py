"""
Who has an account.

Deliberately a different database from anyone's memoir. This one holds
identity and, later, an encrypted credential; the per-user stores hold the
material. Keeping them apart means a query bug here cannot return someone's
life, and the account row can be read on every request without opening a
32 MB constraint network to do it.

The account id is derived from Google's subject claim, not taken from it:

  * it becomes a directory name, so it must satisfy workspace.SAFE_ID by
    construction rather than by validation
  * Google's `sub` is a stable identifier for a real person and does not
    belong in filesystem paths or log lines

A one-way derivation gives both. The same Google account always lands on the
same id; the id says nothing about the account.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import pathlib
import sqlite3

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE users (
  id           TEXT PRIMARY KEY,   -- derived, opaque, safe as a directory name
  google_sub   TEXT NOT NULL UNIQUE,
  email        TEXT,
  name         TEXT,
  created_at   TEXT NOT NULL,
  last_seen_at TEXT NOT NULL
);
CREATE INDEX users_sub ON users(google_sub);
"""

# Columns added after first release. Same contract as store._COLUMN_MIGRATIONS:
# tables come from SCHEMA, columns need an explicit default.
_COLUMN_MIGRATIONS: list[tuple[str, str, str]] = []


def now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def derive_id(google_sub: str) -> str:
    """Google subject -> account id. One-way, stable, always path-safe."""
    if not google_sub:
        raise ValueError("empty google subject")
    return "u" + hashlib.sha256(f"sargam:{google_sub}".encode()).hexdigest()[:20]


class Accounts:
    def __init__(self, path: str | pathlib.Path):
        self.path = pathlib.Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fresh = not self.path.exists()
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        if fresh:
            self.db.executescript(SCHEMA)
            self.db.commit()
        else:
            self._migrate()

    def close(self) -> None:
        self.db.commit()
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def _migrate(self) -> list[str]:
        import re
        applied: list[str] = []
        have = {r["name"] for r in self.db.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','index')")}
        for stmt in SCHEMA.split(";"):
            m = re.search(r"CREATE\s+(TABLE|INDEX)\s+(\w+)", stmt, re.I)
            if m and m.group(2) not in have:
                self.db.execute(stmt)
                applied.append(f"+{m.group(1).lower()} {m.group(2)}")
        for table, col, decl in _COLUMN_MIGRATIONS:
            cols = {r["name"] for r in self.db.execute(f"PRAGMA table_info({table})")}
            if cols and col not in cols:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
                applied.append(f"+column {table}.{col}")
        if applied:
            self.db.commit()
        return applied

    # ------------------------------------------------------------------ users

    def upsert_google(self, claims: dict) -> sqlite3.Row:
        """Sign-in. Creates the account on first visit, refreshes the profile
        on every later one. Only the fields actually used are stored: no
        tokens, because nothing here calls Google again after identifying the
        person."""
        sub = claims.get("sub")
        if not sub:
            raise ValueError("id token carried no subject")
        uid = derive_id(sub)
        t = now()
        self.db.execute(
            "INSERT INTO users (id, google_sub, email, name, created_at, "
            "last_seen_at) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(google_sub) DO UPDATE SET "
            "email=excluded.email, name=excluded.name, last_seen_at=excluded.last_seen_at",
            (uid, sub, claims.get("email"), claims.get("name"), t, t),
        )
        self.db.commit()
        return self.get(uid)

    def get(self, user_id: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM users WHERE id = ?",
                               (user_id,)).fetchone()

    def touch(self, user_id: str) -> None:
        self.db.execute("UPDATE users SET last_seen_at = ? WHERE id = ?",
                        (now(), user_id))
        self.db.commit()

    def count(self) -> int:
        return self.db.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]

    def delete(self, user_id: str) -> None:
        """Remove the account row. The caller is responsible for the
        workspace: identity and material are separate on purpose, and a
        deletion that drops one without the other is a bug in the caller."""
        self.db.execute("DELETE FROM users WHERE id = ?", (user_id,))
        self.db.commit()
