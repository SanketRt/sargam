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

import api
import extract
import workspace as W


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


def _fresh_server(root: pathlib.Path):
    """A server module bound to this root, with a swappable caller."""
    import importlib
    import server as srv
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


if __name__ == "__main__":
    test_both_transports_agree()
    test_requests_reach_only_their_own_workspace()
    test_unauthenticated_gets_nothing()
    test_failures_do_not_leak_internals()
    test_page_knows_its_prefix()
    print("\nall server properties hold")
