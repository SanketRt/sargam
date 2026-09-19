"""
The hosted server.

A FastAPI transport over the same handlers the local tool uses. It adds the
three things hosting needs and local use does not: a notion of who is asking,
a store resolved per request rather than per process, and awareness of the
path prefix a reverse proxy keeps in the browser's URL but never forwards.

Right now `current_user` always returns the local id, so this behaves exactly
like `sargam web` with a different server underneath. That function is the
seam: authentication replaces it, and nothing else in this file changes.

    uvicorn server:app --port 8000
    sargam serve

Environment:
    SARGAM_DATA      where per-user workspaces live   (default /data/users)
    SARGAM_BASE_PATH public prefix, e.g. /projects/sargam   (default "")
    SARGAM_SINGLE    run as one local user, no accounts     (default off)
"""

from __future__ import annotations

import os
import pathlib
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

import api
import publish
import workspace as W

BASE_PATH = os.environ.get("SARGAM_BASE_PATH", "").rstrip("/")
SINGLE_USER = os.environ.get("SARGAM_SINGLE", "").lower() in ("1", "true", "yes")


# ------------------------------------------------------------------ sessions

def _workspace(user_id: str) -> W.Workspace:
    """Where this caller's material lives.

    Single-user mode points at the same `.sargam/` the CLI uses, so `sargam
    serve` and `sargam web` show identical data. It does not go through
    `for_user`: that function's job is to turn an untrusted account id into a
    safe directory, and a local path chosen by the operator is neither
    untrusted nor a valid id.
    """
    if SINGLE_USER and user_id == W.LOCAL_ID:
        return W.local()
    return W.for_user(user_id, W.data_root())


class Registry:
    """Open stores, keyed by user.

    A Timeline is a dense matrix held in memory, so keeping one loaded per
    user is the thing that decides what this costs to run. For now every store
    that is opened stays open; eviction is a separate piece of work and wants
    a real policy rather than a guess. The lock is per user, not global:
    two people compiling at once must not queue behind each other.
    """

    def __init__(self):
        self._entries: dict[str, tuple] = {}
        self._guard = threading.Lock()

    def ctx(self, user_id: str) -> tuple:
        with self._guard:
            hit = self._entries.get(user_id)
            if hit is None:
                ws = _workspace(user_id)
                store = ws.open()
                publish.ensure_repo(ws.manuscript)
                hit = (api.Ctx(store=store, manuscript=ws.manuscript),
                       threading.Lock())
                self._entries[user_id] = hit
            return hit

    def close(self) -> None:
        with self._guard:
            for ctx, _ in self._entries.values():
                ctx.store.close()
            self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)


registry = Registry()


def current_user(request: Request) -> str:
    """Who is asking.

    Authentication replaces this and nothing else. Until it does, every
    request is the single local user, which is what makes this file a
    like-for-like swap for the standard-library server.
    """
    if SINGLE_USER:
        return W.LOCAL_ID
    # request.scope, not hasattr(request, "session"): the attribute always
    # exists and raises when SessionMiddleware is absent, so probing it that
    # way turns "not signed in" into a 500.
    user = (request.scope.get("session") or {}).get("uid")
    if not user:
        raise HTTPException(status_code=401, detail="sign in required")
    return W.check_id(user)


def api_key_for(user_id: str) -> str | None:
    """The caller's own Anthropic credential.

    Stage 4 decrypts it from the user's row. Returning None here means calls
    fall back to the server's environment, which is correct for local use and
    must never be the answer once there are real accounts.
    """
    return None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    registry.close()


app = FastAPI(title="sargam", docs_url=None, redoc_url=None,
              root_path=BASE_PATH, lifespan=lifespan)


# -------------------------------------------------------------------- routes

@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "open_stores": len(registry),
            "base_path": BASE_PATH, "single_user": SINGLE_USER}


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse(api.page(BASE_PATH))


@app.get("/api/state")
def state(request: Request) -> Response:
    uid = current_user(request)
    ctx, lock = registry.ctx(uid)
    ctx.api_key = api_key_for(uid)
    with lock:
        return Response(api.dumps(api.snapshot(ctx)),
                        media_type="application/json")


@app.post("/api/{action}")
async def action(action: str, request: Request) -> Response:
    fn = api.ROUTES.get(f"/api/{action}")
    if fn is None:
        raise HTTPException(status_code=404, detail="no such action")
    uid = current_user(request)
    ctx, lock = registry.ctx(uid)
    ctx.api_key = api_key_for(uid)
    raw = await request.body()
    body = __import__("json").loads(raw or b"{}")
    try:
        with lock:
            out = fn(ctx, body)
    except Exception as exc:
        # Never surface the exception text raw: it can carry a credential or
        # a path from the server's filesystem.
        return JSONResponse({"ok": False, "message": type(exc).__name__},
                            status_code=400)
    return Response(api.dumps(out), media_type="application/json")
