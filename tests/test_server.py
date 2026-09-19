"""
Properties of the hosted transport. Run: python test_server.py

Skips itself when FastAPI is not installed, because the local tool runs on the
standard library and should not be made to carry a web stack to run its tests.

What matters here is not that the routes respond -- that is plumbing -- but
that the transport cannot leak across the boundary the store establishes:

  1. the two transports agree: same store, same snapshot
  2. requests are routed to the caller's own workspace, never a shared one
  3. an unauthenticated request gets nothing
  4. a handler failure does not return the server's internals
  5. the page knows the prefix it is served under
"""


from __future__ import annotations

import pathlib as _pathlib
import sys as _sys
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parents[1] / "src"))

import os
import pathlib
import shutil
import tempfile

os.environ.setdefault("SARGAM_BACKEND", "offline")

try:
    from fastapi.testclient import TestClient
except ImportError:
    print("skip  FastAPI not installed (pip install -r requirements.txt)")
    raise SystemExit(0)

import base64
import json

from sargam import accounts as ACC
from sargam import api
from sargam import extract
from sargam import workspace as W

SECRET = "test-secret-not-a-real-one"


def signed_session(payload: dict, secret: str = SECRET) -> str:
    """A cookie in the shape SessionMiddleware produces, so the session path
    can be exercised without standing up Google."""
    from itsdangerous import TimestampSigner
    data = base64.b64encode(json.dumps(payload).encode())
    return TimestampSigner(secret).sign(data).decode()


def _seed(store, text: str) -> None:
    f = store.add_fragment(text)
    extract.apply(store, extract.offline_extract(text, {}), f)


class tmproot:
    def __enter__(self):
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="sargam-srv-"))
        os.environ["SARGAM_DATA"] = str(self.root)
        return self.root

    def __exit__(self, *exc):
        shutil.rmtree(self.root, ignore_errors=True)
        os.environ.pop("SARGAM_DATA", None)
        return False


def _fresh_server(root: pathlib.Path, **env):
    """A server module bound to this root, with a swappable caller."""
    import importlib
    os.environ.setdefault("SARGAM_SECRET", SECRET)
    os.environ["SARGAM_ACCOUNTS"] = str(root / "accounts.db")
    os.environ.pop("SARGAM_SINGLE", None)
    for k, v in env.items():
        os.environ[k] = v
    from sargam import server as srv
    importlib.reload(srv)
    srv.registry.close()
    return srv


# --------------------------------------------------------------------- tests

def test_both_transports_agree() -> None:
    """web.py and server.py are two doors into one set of handlers. If they
    ever disagree, one of them has grown its own copy of the product."""
    with tmproot() as root:
        ws = W.for_user("agree", root)
        st = ws.open()
        _seed(st, "I married Meera in April 1986. The mill job was 1977.")
        direct = api.snapshot(api.Ctx(store=st, manuscript=ws.manuscript))
        st.close()

        srv = _fresh_server(root)
        srv.current_user = lambda request: "agree"
        c = TestClient(srv.app)
        over_http = c.get("/api/state").json()
        srv.registry.close()

    assert [e["id"] for e in over_http["events"]] == \
           [e["id"] for e in direct["events"]], "transports disagree on events"
    assert over_http["pending"] == direct["pending"], "transports disagree on pending"
    print("ok  the stdlib and FastAPI transports return the same state")


def test_requests_reach_only_their_own_workspace() -> None:
    with tmproot() as root:
        a = W.for_user("alice", root)
        sa = a.open()
        _seed(sa, "Alice married in April 1986.")
        sa.close()
        b = W.for_user("bob", root)
        sb = b.open()
        _seed(sb, "Bob started at the mill in 1977.")
        sb.close()

        srv = _fresh_server(root)
        c = TestClient(srv.app)

        srv.current_user = lambda request: "alice"
        seen_a = {e["summary"] for e in c.get("/api/state").json()["events"]}
        srv.current_user = lambda request: "bob"
        seen_b = {e["summary"] for e in c.get("/api/state").json()["events"]}
        srv.registry.close()

    assert seen_a and seen_b, "one of the workspaces came back empty"
    assert not (seen_a & seen_b), f"workspaces bled together: {seen_a & seen_b}"
    assert any("1986" in s for s in seen_a), seen_a
    assert any("1977" in s for s in seen_b), seen_b
    print("ok  each request sees only its own workspace")


def test_unauthenticated_gets_nothing() -> None:
    with tmproot() as root:
        srv = _fresh_server(root)
        c = TestClient(srv.app)
        r = c.get("/api/state")
        assert r.status_code == 401, f"expected 401, got {r.status_code}"
        assert "events" not in r.text, "an unauthenticated reply carried data"
        srv.registry.close()
    print("ok  an unauthenticated request is refused with no data")


def test_failures_do_not_leak_internals() -> None:
    """A handler raising must not hand back a path, a key, or a stack."""
    with tmproot() as root:
        ws = W.for_user("erin", root)
        ws.open().close()
        srv = _fresh_server(root)
        srv.current_user = lambda request: "erin"
        c = TestClient(srv.app)
        r = c.post("/api/freeze", json={})          # missing paragraph_id
        assert r.status_code == 400, r.status_code
        body = r.text
        assert "ok" in body and "false" in body.lower()
        for leak in (str(root), "Traceback", "sk-ant", "store.db"):
            assert leak not in body, f"error response leaked {leak!r}: {body}"
        srv.registry.close()
    print("ok  a handler failure returns no server internals")


def test_page_knows_its_prefix() -> None:
    local = api.page()
    proxied = api.page("/projects/sargam/")
    assert 'window.__SARGAM_BASE__="";' in local
    assert 'window.__SARGAM_BASE__="/projects/sargam";' in proxied
    assert "BASE+'/api/state'" in proxied, "page still builds absolute URLs"
    assert "fetch('/api/state')" not in proxied, "an absolute fetch survived"
    print("ok  the page builds every URL from the prefix it was served under")


def test_the_session_cookie_decides_the_workspace() -> None:
    """The only thing that may name a workspace is the signed cookie. A
    header, a query parameter or a body field naming another user must do
    nothing at all."""
    with tmproot() as root:
        acc = ACC.Accounts(root / "accounts.db")
        alice = acc.upsert_google({"sub": "google-alice", "email": "a@x.com",
                                   "name": "Alice"})
        bob = acc.upsert_google({"sub": "google-bob", "email": "b@x.com",
                                 "name": "Bob"})
        acc.close()

        sa = W.for_user(alice["id"], root).open()
        _seed(sa, "Alice married in April 1986.")
        sa.close()
        sb = W.for_user(bob["id"], root).open()
        _seed(sb, "Bob started at the mill in 1977.")
        sb.close()

        srv = _fresh_server(root)
        c = TestClient(srv.app)

        c.cookies.set("sargam_session", signed_session({"uid": alice["id"]}))
        seen = {e["summary"] for e in c.get("/api/state").json()["events"]}
        assert any("1986" in s for s in seen), seen
        assert not any("1977" in s for s in seen), "saw Bob's material"

        me = c.get("/api/me").json()
        assert me["email"] == "a@x.com", me

        # Naming Bob every way a caller can. None may work.
        for kwargs in ({"headers": {"X-User": bob["id"]}},
                       {"params": {"user": bob["id"], "uid": bob["id"]}}):
            got = {e["summary"] for e in c.get("/api/state", **kwargs)
                   .json()["events"]}
            assert got == seen, f"request-supplied id changed the workspace: {kwargs}"

        r = c.post("/api/compile", json={"user": bob["id"], "uid": bob["id"]})
        assert r.json().get("ok"), r.text
        after = {e["summary"] for e in c.get("/api/state").json()["events"]}
        assert after == seen, "a body field switched workspaces"
        srv.registry.close()
    print("ok  only the signed cookie decides whose workspace is served")


def test_a_forged_cookie_is_refused() -> None:
    with tmproot() as root:
        srv = _fresh_server(root)
        c = TestClient(srv.app)
        for bad in [signed_session({"uid": "../../etc"}),
                    signed_session({"uid": "u123"}, secret="wrong-secret"),
                    "not-a-cookie-at-all"]:
            c.cookies.set("sargam_session", bad)
            r = c.get("/api/state")
            assert r.status_code in (400, 401, 403),                 f"forged cookie accepted: {bad[:24]}... -> {r.status_code}"
            assert "events" not in r.text
        srv.registry.close()
    print("ok  a forged or unsafe session cookie is refused")


def test_logout_clears_the_session() -> None:
    with tmproot() as root:
        acc = ACC.Accounts(root / "accounts.db")
        u = acc.upsert_google({"sub": "google-carol", "email": "c@x.com",
                               "name": "Carol"})
        acc.close()
        srv = _fresh_server(root)
        c = TestClient(srv.app)
        c.cookies.set("sargam_session", signed_session({"uid": u["id"]}))
        assert c.get("/api/me").status_code == 200

        r = c.get("/auth/logout", follow_redirects=False)
        # The contract is the Set-Cookie, not the test client's jar: a cookie
        # injected by hand has no domain, so httpx will not match the delete
        # against it the way a browser would.
        sc = r.headers.get("set-cookie", "")
        assert "sargam_session=" in sc, sc
        assert "01 Jan 1970" in sc or "Max-Age=0" in sc, \
            f"logout did not expire the cookie: {sc}"

        c.cookies.clear()
        assert c.get("/api/me").status_code == 401, "no cookie still signed in"
        srv.registry.close()
    print("ok  logout expires the session cookie")


def test_accounts_refuse_a_default_secret() -> None:
    """A session key that changes on restart, or is shared, is not a key. The
    server must refuse to serve accounts rather than pick something."""
    saved = os.environ.pop("SARGAM_SECRET", None)
    try:
        with tmproot() as root:
            os.environ["SARGAM_ACCOUNTS"] = str(root / "accounts.db")
            os.environ.pop("SARGAM_SINGLE", None)
            import importlib
            from sargam import server as srv
            try:
                importlib.reload(srv)
            except RuntimeError as exc:
                assert "SARGAM_SECRET" in str(exc), exc
            else:
                raise AssertionError("served accounts with no session secret")
    finally:
        if saved is not None:
            os.environ["SARGAM_SECRET"] = saved
    print("ok  the server refuses to serve accounts without a session secret")


def test_eviction_bounds_memory_and_preserves_state() -> None:
    """Open stores are capped, and an evicted one comes back identical. If
    eviction lost anything, the cap would be trading correctness for memory."""
    with tmproot() as root:
        srv = _fresh_server(root, SARGAM_MAX_OPEN="3")
        srv.registry.max_open = 3
        uids = [f"u{i:020d}" for i in range(8)]
        for uid in uids:
            ws = W.for_user(uid, root)
            st = ws.open()
            _seed(st, f"Something happened to {uid} in 19{70 + len(uid) % 20}.")
            st.close()

        srv.current_user = lambda request: "placeholder"
        c = TestClient(srv.app)
        seen = {}
        for uid in uids:
            srv.current_user = (lambda u: (lambda request: u))(uid)
            seen[uid] = {e["summary"] for e in c.get("/api/state").json()["events"]}
            assert len(srv.registry) <= 3, \
                f"{len(srv.registry)} stores open, cap is 3"
        assert srv.registry.evictions >= len(uids) - 3, srv.registry.evictions

        # Re-visit the ones evicted first; state must be unchanged.
        for uid in uids[:3]:
            srv.current_user = (lambda u: (lambda request: u))(uid)
            again = {e["summary"] for e in c.get("/api/state").json()["events"]}
            assert again == seen[uid], f"{uid} lost state across eviction"
        srv.registry.close()
    print("ok  eviction caps open stores and loses nothing")


def test_a_busy_store_is_not_evicted() -> None:
    """Eviction closes a sqlite connection. Doing that to a request in flight
    would fail it, so a held lock means skip, never wait."""
    with tmproot() as root:
        srv = _fresh_server(root)
        srv.registry.max_open = 1
        for uid in ("ubusy", "uother"):
            W.for_user(uid, root).open().close()

        ctx_busy, lock_busy = srv.registry.ctx("ubusy")
        lock_busy.acquire()                    # pretend a request is running
        try:
            srv.registry.ctx("uother")         # triggers a reap
            assert "ubusy" in srv.registry._entries, \
                "an in-flight store was evicted"
            # The connection must still be usable.
            assert ctx_busy.store.tl is not None
            ctx_busy.store.fragments()
        finally:
            lock_busy.release()
        srv.registry.close()
    print("ok  a store with a request in flight is skipped, not closed")


def test_account_ids_are_derived_not_taken() -> None:
    hostile = "../../../etc/passwd"
    uid = ACC.derive_id(hostile)
    assert W.SAFE_ID.match(uid), uid
    assert hostile not in uid and "/" not in uid
    assert ACC.derive_id("abc") == ACC.derive_id("abc"), "not stable"
    assert ACC.derive_id("abc") != ACC.derive_id("abd"), "collides"
    print("ok  account ids are derived, so a hostile subject cannot escape")


if __name__ == "__main__":
    test_both_transports_agree()
    test_requests_reach_only_their_own_workspace()
    test_unauthenticated_gets_nothing()
    test_failures_do_not_leak_internals()
    test_page_knows_its_prefix()
    test_the_session_cookie_decides_the_workspace()
    test_a_forged_cookie_is_refused()
    test_logout_clears_the_session()
    test_accounts_refuse_a_default_secret()
    test_eviction_bounds_memory_and_preserves_state()
    test_a_busy_store_is_not_evicted()
    test_account_ids_are_derived_not_taken()
    print("\nall server properties hold")
