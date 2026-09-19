"""
Properties of per-user isolation and per-request credentials.
Run: python test_workspace.py

These are the invariants hosting depends on. Locally they are nearly
tautological -- there is one user and the key comes from the environment. In a
multi-tenant server they are the whole security boundary:

  1. a user id can never address a directory outside the data root
  2. one user's store cannot read another's
  3. a caller's own key decides the backend, not the server's environment
  4. the key reaches every model call site
  5. the key is never written to disk, and never becomes part of a cache key
"""

from __future__ import annotations

import os
import pathlib
import shutil
import tempfile

os.environ.setdefault("SARGAM_BACKEND", "offline")

import extract
import render as R
import workspace as W
from timeline import PROV_ABSOLUTE, days

FAKE_KEY = "sk-ant-thiskeymustneverbewrittentodisk"


class tmproot:
    def __enter__(self):
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="sargam-ws-"))
        return self.root

    def __exit__(self, *exc):
        shutil.rmtree(self.root, ignore_errors=True)
        return False


# --------------------------------------------------------------------- tests

def test_user_ids_are_checked() -> None:
    with tmproot() as root:
        for bad in ["../etc", "a/b", "..", "", "a.b", "/abs", "x" * 65,
                    "a\\b", "user id", "."]:
            try:
                W.for_user(bad, root)
            except W.BadUserId:
                continue
            raise AssertionError(f"unsafe user id accepted: {bad!r}")
        good = W.for_user("user_abc-123", root)
        assert good.root.parent == root.resolve()
    print("ok  unsafe user ids are rejected before touching the filesystem")


def test_workspaces_are_isolated() -> None:
    with tmproot() as root:
        a, b = W.for_user("alice", root), W.for_user("bob", root)
        sa, sb = a.open(), b.open()
        fid = sa.add_fragment("something Alice would not want shared")
        ev = sa.add_event("a private thing", from_fragment=fid)
        assert sb.fragment(fid) is None, "cross-user fragment read"
        assert ev.id not in sb.tl.events, "cross-user event read"
        assert sa.path != sb.path
        sa.close()
        sb.close()
    print("ok  one user's store cannot see another's")


def test_backend_follows_the_caller_not_the_server() -> None:
    saved = os.environ.pop("SARGAM_BACKEND", None)
    try:
        os.environ.pop("ANTHROPIC_API_KEY", None)
        assert extract.backend() == "offline", "no credential should be offline"
        assert extract.backend(FAKE_KEY) == "api", \
            "a caller's own key must select the api path"
        os.environ["SARGAM_BACKEND"] = "offline"
        assert extract.backend(FAKE_KEY) == "offline", \
            "an explicitly offline deployment must override a caller's key"
    finally:
        os.environ.pop("SARGAM_BACKEND", None)
        if saved is not None:
            os.environ["SARGAM_BACKEND"] = saved
    print("ok  backend follows the caller's credential, deployment can veto")


class _Block:
    type = "text"
    text = "In 1986 I married Meera. We had moved to Pune two years before."


class _Reply:
    stop_reason = "end_turn"
    content = [_Block()]


def test_key_reaches_the_model_and_never_lands_on_disk() -> None:
    """Substitute the client so nothing leaves the machine, then check both
    halves: the key arrives at the call site, and it is nowhere in the store
    afterwards."""
    seen: list[str | None] = []
    real_client, real_backend = extract._client, extract.backend

    class _Fake:
        class messages:
            @staticmethod
            def create(**kw):
                return _Reply()

    extract._client = lambda api_key=None: (seen.append(api_key) or _Fake())
    extract.backend = lambda api_key=None: "api" if api_key else "offline"
    try:
        with tmproot() as root:
            w = W.for_user("carol", root)
            st = w.open()
            f = st.add_fragment("I married Meera in April 1986.")
            ev = st.add_event("my wedding", entities=["Meera"], from_fragment=f)
            st.assert_constraint(ev.s, 0, days("1986-04-01"), days("1986-04-30"),
                                 PROV_ABSOLUTE, f)
            st.assert_constraint(ev.e, 0, days("1986-04-01"), days("1986-04-30"),
                                 PROV_ABSOLUTE, f)

            book = R.compile_book(st, do_ground=False, api_key=FAKE_KEY)
            assert book["rendered"] >= 1, "nothing was rendered"
            assert seen and seen[0] == FAKE_KEY, \
                f"key did not reach the client: {seen!r}"

            # The cache key must not depend on the credential: same events and
            # style, a different user's key, still a cache hit.
            seen.clear()
            book2 = R.compile_book(st, do_ground=False,
                                   api_key="sk-ant-a-completely-different-key")
            assert book2["rendered"] == 0, "cache key leaked the credential"
            assert not seen, "a cached paragraph still built a client"

            st.close()
            blob = w.db_path.read_bytes()
            assert FAKE_KEY.encode() not in blob, "the key was written to the store"
            for p in w.root.rglob("*"):
                if p.is_file():
                    assert FAKE_KEY.encode() not in p.read_bytes(), \
                        f"the key leaked into {p.name}"
    finally:
        extract._client, extract.backend = real_client, real_backend
    print("ok  the key reaches the model, and never the disk or the cache key")


if __name__ == "__main__":
    test_user_ids_are_checked()
    test_workspaces_are_isolated()
    test_backend_follows_the_caller_not_the_server()
    test_key_reaches_the_model_and_never_lands_on_disk()
    print("\nall workspace properties hold")
