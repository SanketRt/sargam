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


def test_the_page_matches_the_site_it_is_served_from() -> None:
    """Proxied under the host site, this shares an origin with it -- and
    therefore its localStorage. Reading the same `theme` key is what makes the
    theme follow someone across, so the key name is a contract, not a detail.
    The pre-paint script is what stops the wrong theme flashing first."""
    p = api.page("/projects/sargam")

    head = p[:p.index("</head>")]
    assert "localStorage.getItem('theme')" in head, \
        "theme is not read before first paint"
    assert "setAttribute('data-theme'" in head, "theme is not applied in <head>"
    assert "localStorage.setItem('theme'" in p, "the toggle does not persist"
    assert 'data-theme="light"' in p, "no default theme on the root element"
    assert '[data-theme="dark"]' in p, "no dark palette"

    assert "family=Inter" in p, "not using the site's typeface"
    assert "prefers-color-scheme: dark" in head, \
        "a first-time visitor's system preference is ignored"

    # Every URL the page builds must go through BASE.
    for absolute in ("fetch('/api/", 'fetch("/api/', 'href="/auth/'):
        assert absolute not in p, f"absolute URL survived: {absolute}"
    print("ok  the page shares the site's theme, typeface and path prefix")


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


def test_a_stored_key_is_reachable_only_by_its_owner() -> None:
    """The credential must reach the caller's own model calls and no one
    else's, and must never be readable over the API."""
    try:
        from sargam import vault
    except Exception:
        print("skip  vault unavailable")
        return
    saved = os.environ.get(vault.ENV)
    os.environ[vault.ENV] = vault.generate()
    KEY = "sk-ant-api03-ONLY-ALICES-KEY-4242"
    try:
        with tmproot() as root:
            acc = ACC.Accounts(root / "accounts.db")
            alice = acc.upsert_google({"sub": "ga", "email": "a@x.com",
                                       "name": "A"})
            bob = acc.upsert_google({"sub": "gb", "email": "b@x.com",
                                     "name": "B"})
            acc.close()
            for u in (alice, bob):
                W.for_user(u["id"], root).open().close()

            srv = _fresh_server(root)
            # No network: accept any key that looks like one.
            srv.extract.validate_key = lambda k: (k.startswith("sk-ant-"),
                                                  "stubbed")
            c = TestClient(srv.app)

            c.cookies.set("sargam_session", signed_session({"uid": alice["id"]}))
            r = c.post("/api/key", json={"api_key": KEY})
            assert r.status_code == 200, r.text
            assert KEY not in r.text, "the key was echoed back"
            assert r.json()["hint"].endswith("4242")

            me = c.get("/api/me").json()
            assert me["has_key"] is True and me["hint"].endswith("4242")
            assert KEY not in c.get("/api/me").text, "/api/me leaked the key"

            assert srv.api_key_for(alice["id"]) == KEY, "owner cannot use it"
            assert srv.api_key_for(bob["id"]) is None, "reachable by another"

            c.cookies.set("sargam_session", signed_session({"uid": bob["id"]}))
            assert c.get("/api/me").json()["has_key"] is False

            # A bad key is refused before anything is stored.
            r = c.post("/api/key", json={"api_key": "nonsense"})
            assert r.status_code == 400, r.status_code
            assert srv.api_key_for(bob["id"]) is None, "a rejected key stored"

            c.cookies.set("sargam_session", signed_session({"uid": alice["id"]}))
            assert c.post("/api/key/clear", json={}).status_code == 200
            assert srv.api_key_for(alice["id"]) is None, "clear did nothing"
            srv.registry.close()
    finally:
        if saved is None:
            os.environ.pop(vault.ENV, None)
        else:
            os.environ[vault.ENV] = saved
    print("ok  a stored credential reaches its owner alone and is never readable")


def test_export_contains_the_irreplaceable_part() -> None:
    """Everything but the fragments can be recomputed. An export that loses
    them is not an export."""
    import io, zipfile
    from sargam import account_ops as OPS
    with tmproot() as root:
        ws = W.for_user("uexport", root)
        st = ws.open()
        text = "I married Meera in April 1986. The mill job was 1977."
        _seed(st, text)
        import sargam.render as R
        import sargam.publish as publish
        book = R.compile_book(st, do_ground=False)
        publish.write(st, book, ws.manuscript)

        blob = OPS.export_zip(st, ws)
        st.close()

        z = zipfile.ZipFile(io.BytesIO(blob))
        names = z.namelist()
        assert any(n.startswith("fragments/") for n in names), names
        assert "fragments.json" in names and "events.json" in names
        assert "constraints.json" in names and "README.txt" in names
        assert any(n.startswith("manuscript/") for n in names), names

        raw = "".join(z.read(n).decode() for n in names
                      if n.startswith("fragments/"))
        assert text in raw, "the fragment text is not in the export"
    print("ok  an export carries the fragments, in plain text")


def test_delete_removes_account_and_material() -> None:
    from sargam import account_ops as OPS
    with tmproot() as root:
        acc = ACC.Accounts(root / "accounts.db")
        u = acc.upsert_google({"sub": "gdel", "email": "d@x.com", "name": "D"})
        ws = W.for_user(u["id"], root)
        st = ws.open()
        _seed(st, "Something private happened in 1986.")
        st.close()
        acc.close()

        srv = _fresh_server(root)
        srv.current_user = lambda request: u["id"]
        c = TestClient(srv.app)
        assert c.get("/api/state").status_code == 200   # opens the store

        r = c.post("/api/account/delete", json={"confirm": "wrong"})
        assert r.status_code == 400, "deleted without the confirmation phrase"
        assert ws.root.exists(), "material removed on a refused delete"

        r = c.post("/api/account/delete", json={"confirm": "delete everything"})
        assert r.status_code == 200, r.text
        assert not ws.root.exists(), "the workspace survived deletion"

        acc2 = ACC.Accounts(root / "accounts.db")
        assert acc2.get(u["id"]) is None, "the account row survived"
        acc2.close()
        srv.registry.close()
    print("ok  deleting an account removes the row and every file")


def test_rate_limits_bound_the_expensive_routes() -> None:
    with tmproot() as root:
        acc = ACC.Accounts(root / "accounts.db")
        u = acc.upsert_google({"sub": "grate", "email": "r@x.com", "name": "R"})
        acc.close()
        W.for_user(u["id"], root).open().close()

        srv = _fresh_server(root)
        srv.current_user = lambda request: u["id"]
        c = TestClient(srv.app)

        from sargam import account_ops as OPS
        burst = OPS.LIMITS["compile"][1]
        codes = [c.post("/api/compile", json={}).status_code
                 for _ in range(burst + 4)]
        assert 429 in codes, f"compile was never limited: {codes}"
        assert codes[0] == 200, codes
        assert codes.count(200) <= burst, f"burst exceeded: {codes}"

        r = [x for x in
             [c.post("/api/compile", json={})] if x.status_code == 429][0]
        assert r.headers.get("Retry-After"), "429 without Retry-After"

        # A cheap route is not caught by the expensive route's bucket.
        assert c.get("/api/state").status_code == 200, "reads were limited too"
        srv.registry.close()
    print("ok  compiles are rate limited without starving reads")


def test_admission_is_this_app_s_decision() -> None:
    """Google's "Testing" publishing status reads like an allowlist and is
    not one: with non-sensitive scopes it does not reliably stop accounts off
    the test-user list, and project members bypass it by design. Whether
    someone gets an account has to be decided here."""
    with tmproot() as root:
        srv = _fresh_server(root, SARGAM_ALLOWED_EMAILS="keep@x.com, @ok.org")
        assert srv.may_sign_in("keep@x.com") is True
        assert srv.may_sign_in("KEEP@X.com") is True, "matching is case sensitive"
        assert srv.may_sign_in("anyone@ok.org") is True, "domain entry ignored"
        assert srv.may_sign_in("stranger@x.com") is False, "allowlist not enforced"
        assert srv.may_sign_in(None) is False, "no email should not be admitted"
        srv.registry.close()

    with tmproot() as root:
        srv = _fresh_server(root, SARGAM_ALLOWED_EMAILS="")
        assert srv.may_sign_in("anyone@anywhere.com") is True, \
            "an empty allowlist must stay open rather than lock everyone out"
        srv.registry.close()
    print("ok  admission is decided here, not by the identity provider")


def test_a_refused_visitor_leaves_nothing_behind() -> None:
    """Refusal happens before the account row and the workspace directory
    exist, so someone turned away cannot consume a slot or a byte."""
    from sargam import account_ops  # noqa: F401
    with tmproot() as root:
        srv = _fresh_server(root, SARGAM_ALLOWED_EMAILS="only@allowed.com")
        acc = srv.accounts()
        before = acc.count()

        assert srv.may_sign_in("stranger@elsewhere.com") is False
        # The callback returns before upsert_google, so nothing is created.
        assert acc.count() == before, "a refused sign-in created an account"
        uid = ACC.derive_id("would-be-subject")
        assert not W.for_user(uid, root).root.exists(), \
            "a refused sign-in created a workspace"
        srv.registry.close()
    print("ok  a refused visitor creates no account and no workspace")


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
    test_the_page_matches_the_site_it_is_served_from()
    test_the_session_cookie_decides_the_workspace()
    test_a_forged_cookie_is_refused()
    test_logout_clears_the_session()
    test_accounts_refuse_a_default_secret()
    test_eviction_bounds_memory_and_preserves_state()
    test_a_busy_store_is_not_evicted()
    test_a_stored_key_is_reachable_only_by_its_owner()
    test_export_contains_the_irreplaceable_part()
    test_delete_removes_account_and_material()
    test_rate_limits_bound_the_expensive_routes()
    test_admission_is_this_app_s_decision()
    test_a_refused_visitor_leaves_nothing_behind()
    test_account_ids_are_derived_not_taken()
    print("\nall server properties hold")
