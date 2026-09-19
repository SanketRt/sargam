"""
Where one person's material lives.

A workspace is the pair of paths that belong to a single user -- their event
store and their manuscript repo -- plus the rule for turning a user id into a
directory. The local CLI has exactly one; a hosted server has one per account.
Both go through here so there is a single place where that mapping is defined
and a single place where it is checked.

Isolation is physical rather than a WHERE clause. Separate SQLite files mean a
bug in a query cannot return another person's memoir, which for this material
is worth more than the convenience of one shared database.
"""

from __future__ import annotations

import os
import pathlib
import re
from dataclasses import dataclass

import store as S

# A user id becomes a directory name, so it is checked rather than trusted.
# Opaque ids only: no dots, no separators, nothing that can climb out of the
# data root.
SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

LOCAL_ID = "local"


class BadUserId(ValueError):
    pass


def check_id(user_id: str) -> str:
    if not SAFE_ID.match(user_id or ""):
        raise BadUserId(f"unsafe user id {user_id!r}")
    return user_id


@dataclass(frozen=True)
class Workspace:
    user_id: str
    root: pathlib.Path

    @property
    def db_path(self) -> pathlib.Path:
        return self.root / "store.db"

    @property
    def manuscript(self) -> pathlib.Path:
        return self.root / "manuscript"

    def exists(self) -> bool:
        return self.db_path.exists()

    def create(self) -> "Workspace":
        self.root.mkdir(parents=True, exist_ok=True)
        S.Store(self.db_path).close()
        return self

    def open(self) -> S.Store:
        """Open the store, creating the directory if this is a first visit.
        Store itself creates the schema and migrates an older file."""
        self.root.mkdir(parents=True, exist_ok=True)
        return S.Store(self.db_path)


def data_root() -> pathlib.Path:
    """Where all workspaces live. One value, set by the deployment."""
    return pathlib.Path(os.environ.get("SARGAM_DATA", "/data/users")).resolve()


def accounts_path() -> pathlib.Path:
    """The control-plane database. Beside the users directory, not inside it:
    it is not anyone's workspace and must never be reachable by a user id."""
    env = os.environ.get("SARGAM_ACCOUNTS")
    if env:
        return pathlib.Path(env).resolve()
    return data_root().parent / "accounts.db"


def for_user(user_id: str, root: pathlib.Path | None = None) -> Workspace:
    """A hosted user's workspace."""
    base = (root or data_root()).resolve()
    ws = Workspace(check_id(user_id), base / user_id)
    # Belt and braces: even with SAFE_ID, assert the result cannot escape.
    if base not in ws.root.resolve().parents and ws.root.resolve() != base:
        raise BadUserId(f"{user_id!r} resolves outside {base}")
    return ws


def local() -> Workspace:
    """The single-user CLI workspace: .sargam/ under the current directory, or
    wherever SARGAM_HOME points."""
    return Workspace(LOCAL_ID,
                     pathlib.Path(os.environ.get("SARGAM_HOME", ".sargam")).resolve())
