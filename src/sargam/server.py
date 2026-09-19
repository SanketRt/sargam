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
import time
from collections import OrderedDict
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (HTMLResponse, JSONResponse, RedirectResponse,
                               Response)
from pydantic import BaseModel
from starlette.middleware.sessions import SessionMiddleware

from . import account_ops as OPS
from . import accounts as ACC
from . import api
from . import extract
from . import publish
from . import vault
from . import workspace as W

BASE_PATH = os.environ.get("SARGAM_BASE_PATH", "").rstrip("/")
SINGLE_USER = os.environ.get("SARGAM_SINGLE", "").lower() in ("1", "true", "yes")

# The address the browser actually uses. Behind a proxy this is not the
# address the app is bound to, and Google will only redirect to this one.
PUBLIC_URL = os.environ.get("SARGAM_PUBLIC_URL", "http://localhost:8000").rstrip("/")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")


def redirect_uri() -> str:
    """Must match a URI registered in the Google console exactly, prefix and
    all. This is the single most common way the flow fails in production and
    the one thing that cannot be derived from the request."""
    return f"{PUBLIC_URL}{BASE_PATH}/auth/callback"


def _session_secret() -> str:
    secret = os.environ.get("SARGAM_SECRET", "")
    if secret:
        return secret
    if SINGLE_USER:
        # Nobody else can reach this process; a per-boot secret is fine and
        # means local use needs no configuration.
        return os.urandom(32).hex()
    raise RuntimeError(
        "SARGAM_SECRET is not set. Session cookies would be signed with a "
        "key that changes on restart, or worse a shared default. Set it to a "
        "long random value before serving accounts."
    )


def auth_configured() -> bool:
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)


def _parse_allowlist(raw: str) -> list[str]:
    return [e.strip().lower() for e in raw.replace("\n", ",").split(",")
            if e.strip()]


ALLOWED = _parse_allowlist(os.environ.get("SARGAM_ALLOWED_EMAILS", ""))


def may_sign_in(email: str | None) -> bool:
    """Who is allowed an account here.

    Do not rely on the identity provider for this. Google's "Testing"
    publishing status reads like an allowlist and is not one: with only
    non-sensitive scopes it does not reliably stop accounts outside the test
    user list, and project members bypass it by design. Admission is this
    application's decision, made here, where it can be reasoned about.

    An empty SARGAM_ALLOWED_EMAILS means open to anyone who signs in, which is
    a deliberate choice rather than an oversight -- but it has to be made.
    Entries may be full addresses or a bare "@domain" to admit a whole domain.
    """
    if not ALLOWED:
        return True
    if not email:
        return False
    email = email.lower()
    domain = "@" + email.partition("@")[2]
    return email in ALLOWED or domain in ALLOWED


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


MAX_OPEN = int(os.environ.get("SARGAM_MAX_OPEN", "24"))
IDLE_SECONDS = int(os.environ.get("SARGAM_IDLE_SECONDS", "900"))


class Registry:
    """Open stores, keyed by user, least-recently-used first out.

    A Timeline is a dense matrix held in memory, so how many stay loaded is
    what decides the memory bill: roughly 1 MB per hundred events, each. Most
    accounts are idle at any moment, so a small cache of the active ones is
    enough, and closing a store writes its solved closure back -- reopening it
    reads that matrix instead of replaying every constraint, which is the
    difference between milliseconds and seconds.

    Locks are per user, not global: two people compiling at once must not
    queue behind each other. Eviction only takes a store whose lock is free,
    so a request in flight is never closed underneath.
    """

    def __init__(self, max_open: int = MAX_OPEN,
                 idle_seconds: int = IDLE_SECONDS):
        self._entries: "OrderedDict[str, list]" = OrderedDict()
        self._guard = threading.Lock()
        self.max_open = max_open
        self.idle_seconds = idle_seconds
        self.evictions = 0

    def ctx(self, user_id: str) -> tuple:
        with self._guard:
            entry = self._entries.get(user_id)
            if entry is None:
                ws = _workspace(user_id)
                store = ws.open()
                publish.ensure_repo(ws.manuscript)
                entry = [api.Ctx(store=store, manuscript=ws.manuscript),
                         threading.Lock(), time.monotonic()]
                self._entries[user_id] = entry
            else:
                entry[2] = time.monotonic()
                self._entries.move_to_end(user_id)
            self._reap()
            return entry[0], entry[1]

    def _reap(self) -> None:
        """Caller holds the guard. Drops idle stores, then the oldest until
        the cache is within budget. A busy entry is skipped rather than
        waited on -- eviction is housekeeping and must not block a request."""
        now = time.monotonic()
        for uid in list(self._entries):
            entry = self._entries[uid]
            if now - entry[2] > self.idle_seconds:
                self._drop(uid)
        while len(self._entries) > self.max_open:
            for uid in list(self._entries):        # oldest first
                if self._drop(uid):
                    break
            else:
                return                             # everything is in use

    def _drop(self, user_id: str) -> bool:
        entry = self._entries.get(user_id)
        if entry is None:
            return False
        ctx, lock, _ = entry
        if not lock.acquire(blocking=False):
            return False                           # in flight; leave it
        try:
            ctx.store.close()                      # writes the closure back
        except Exception:
            pass
        finally:
            lock.release()
        del self._entries[user_id]
        self.evictions += 1
        return True

    def drop(self, user_id: str) -> bool:
        """Close and forget one user's store, waiting for any request that is
        using it. Eviction may skip a busy store; deletion may not -- unlinking
        files the process still has open is how a half-deleted account
        happens."""
        with self._guard:
            entry = self._entries.pop(user_id, None)
        if entry is None:
            return False
        ctx, lock, _seen = entry
        with lock:
            try:
                ctx.store.close()
            except Exception:
                pass
        return True

    def close(self) -> None:
        with self._guard:
            for ctx, _lock, _seen in self._entries.values():
                try:
                    ctx.store.close()
                except Exception:
                    pass
            self._entries.clear()

    def __len__(self) -> int:
        return len(self._entries)


registry = Registry()
limiter = OPS.RateLimiter()


def _limit(user_id: str, action: str) -> None:
    per_minute, burst = OPS.LIMITS[action]
    ok, wait = limiter.check(user_id, action, per_minute, burst)
    if not ok:
        raise HTTPException(
            status_code=429, detail=f"too many requests; retry in {wait:.0f}s",
            headers={"Retry-After": str(max(1, int(wait)))})


_accounts: ACC.Accounts | None = None


def accounts() -> ACC.Accounts:
    global _accounts
    if _accounts is None:
        _accounts = ACC.Accounts(W.accounts_path())
    return _accounts


def current_user(request: Request) -> str:
    """Who is asking.

    Single-user mode short-circuits to the local workspace. Otherwise the id
    comes from the signed session cookie and nowhere else -- never a header,
    never a query parameter, never the request body -- so a caller cannot name
    a workspace it does not own.
    """
    if SINGLE_USER:
        return W.LOCAL_ID
    # request.scope, not hasattr(request, "session"): the attribute always
    # exists and raises when SessionMiddleware is absent, so probing it that
    # way turns "not signed in" into a 500.
    user = (request.scope.get("session") or {}).get("uid")
    if not user:
        raise HTTPException(status_code=401, detail="sign in required")
    try:
        uid = W.check_id(user)
    except W.BadUserId:
        # A validly-signed cookie carrying an id that is not one we would
        # ever have issued. Letting BadUserId escape here would answer with a
        # 500 and a traceback; the honest answer is that this is not a
        # session. Drop it so the browser stops presenting it.
        request.session.clear()
        raise HTTPException(status_code=401, detail="sign in required")

    # Admission is checked on every request, not only at sign-in. A session
    # cookie lasts thirty days; if it were the only check, removing someone
    # from the allowlist would take a month to mean anything, and an account
    # admitted before the list existed would keep its access indefinitely.
    # Revocation has to take effect on the next request, not the next login.
    row = accounts().get(uid)
    if row is None or not may_sign_in(row["email"]):
        request.session.clear()
        raise HTTPException(status_code=401, detail="sign in required")
    return uid


def api_key_for(user_id: str) -> str | None:
    """The caller's own Anthropic credential, decrypted for this request only.

    None means the caller has not supplied one. Locally that falls back to the
    process environment, which is what a single-user install wants. On a
    server with accounts it means the calls stay on the offline backend rather
    than quietly spending somebody else's credit.
    """
    if SINGLE_USER:
        return None
    if not vault.available():
        return None
    return accounts().get_api_key(user_id)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    registry.close()
    if _accounts is not None:
        _accounts.close()


app = FastAPI(title="sargam", docs_url=None, redoc_url=None,
              root_path=BASE_PATH, lifespan=lifespan)

app.add_middleware(
    SessionMiddleware,
    secret_key=_session_secret(),
    session_cookie="sargam_session",
    same_site="lax",          # the OAuth redirect is a cross-site GET back
    https_only=PUBLIC_URL.startswith("https://"),
    max_age=30 * 24 * 3600,
    # Scoped to this app's own prefix. A domain that hosts several proxied
    # projects would otherwise send this cookie to all of them, which is a
    # session handed to code that has no business seeing it.
    path=BASE_PATH or "/",
)

oauth = None
if auth_configured():
    from authlib.integrations.starlette_client import OAuth

    oauth = OAuth()
    oauth.register(
        name="google",
        server_metadata_url=(
            "https://accounts.google.com/.well-known/openid-configuration"),
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        client_kwargs={"scope": "openid email profile"},
    )


# -------------------------------------------------------------------- routes

@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "open_stores": len(registry),
            "max_open": registry.max_open, "evictions": registry.evictions,
            "base_path": BASE_PATH, "single_user": SINGLE_USER,
            "auth": auth_configured(), "vault": vault.available(),
            "allowlist": len(ALLOWED) or None}


# ----------------------------------------------------------------------- auth

@app.get("/auth/login")
async def login(request: Request):
    if SINGLE_USER:
        return RedirectResponse(f"{BASE_PATH}/")
    if oauth is None:
        raise HTTPException(status_code=503,
                            detail="sign-in is not configured on this server")
    return await oauth.google.authorize_redirect(request, redirect_uri())


@app.get("/auth/callback")
async def callback(request: Request):
    if oauth is None:
        raise HTTPException(status_code=503, detail="sign-in is not configured")
    try:
        # Validates state, exchanges the code, and verifies the id token
        # against Google's keys. `userinfo` is the verified claim set.
        token = await oauth.google.authorize_access_token(request)
    except Exception:
        # The message can carry the client secret or the raw token, so it is
        # never surfaced. A failed sign-in is a failed sign-in.
        return RedirectResponse(f"{BASE_PATH}/?error=signin")

    claims = token.get("userinfo") or {}
    if not claims.get("sub"):
        return RedirectResponse(f"{BASE_PATH}/?error=signin")
    if claims.get("email") and claims.get("email_verified") is False:
        return RedirectResponse(f"{BASE_PATH}/?error=unverified")
    if not may_sign_in(claims.get("email")):
        # Refused before any account or workspace is created, so a turned-away
        # visitor leaves nothing behind on the volume.
        return RedirectResponse(f"{BASE_PATH}/?error=closed")

    user = accounts().upsert_google(claims)
    # Only the opaque id goes in the cookie. No tokens: nothing here calls
    # Google again, so keeping them would be holding a credential for no
    # reason. Rotate the session id to blunt fixation.
    request.session.clear()
    request.session["uid"] = user["id"]
    return RedirectResponse(f"{BASE_PATH}/")


@app.post("/auth/logout")
@app.get("/auth/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(f"{BASE_PATH}/", status_code=303)


@app.get("/api/me")
def me(request: Request) -> dict:
    if SINGLE_USER:
        return {"signed_in": True, "single_user": True, "name": "local"}
    uid = current_user(request)      # also verifies the account still stands
    row = accounts().get(uid)
    if row is None:
        request.session.clear()
        raise HTTPException(status_code=401, detail="sign in required")
    accounts().touch(uid)
    return {"signed_in": True, "single_user": False,
            "name": row["name"], "email": row["email"],
            "can_store_keys": vault.available(),
            **accounts().key_status(uid)}


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse(api.page(BASE_PATH))


@app.get("/api/state")
def state(request: Request) -> Response:
    uid = current_user(request)
    _limit(uid, "read")
    ctx, lock = registry.ctx(uid)
    ctx.api_key = api_key_for(uid)
    with lock:
        return Response(api.dumps(api.snapshot(ctx)),
                        media_type="application/json")


class KeyIn(BaseModel):
    api_key: str


@app.post("/api/key")
def set_key(request: Request, body: KeyIn) -> dict:
    """Store the caller's own credential.

    Validated before it is stored, so a mistyped key fails here rather than
    half way through a compile. Neither the key nor the validation error text
    is echoed back: the error can quote the credential.
    """
    uid = current_user(request)
    _limit(uid, "key")
    if not vault.available():
        raise HTTPException(status_code=503,
                            detail="this server cannot store credentials")
    key = (body.api_key or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="no key supplied")
    ok, _detail = extract.validate_key(key)
    if not ok:
        raise HTTPException(status_code=400,
                            detail="that key was not accepted by the API")
    hint = accounts().set_api_key(uid, key)
    return {"ok": True, "has_key": True, "hint": hint}


@app.post("/api/key/clear")
def clear_key(request: Request) -> dict:
    accounts().clear_api_key(current_user(request))
    return {"ok": True, "has_key": False, "hint": None}


@app.get("/api/export")
def export(request: Request) -> Response:
    """Everything this account holds, as a zip. Fragments go in as plain text
    because they are the part that cannot be recomputed."""
    uid = current_user(request)
    _limit(uid, "export")
    ws = _workspace(uid)
    ctx, lock = registry.ctx(uid)
    with lock:
        blob = OPS.export_zip(ctx.store, ws)
    return Response(
        blob, media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="sargam-export.zip"'})


class DeleteIn(BaseModel):
    confirm: str = ""


@app.post("/api/account/delete")
def delete_account(request: Request, body: DeleteIn) -> dict:
    """Remove the account and everything in it. Not recoverable.

    The confirmation is required in the body rather than inferred from the
    method, so a mis-routed or replayed request cannot destroy a memoir."""
    uid = current_user(request)
    if body.confirm != "delete everything":
        raise HTTPException(status_code=400, detail="confirmation phrase required")
    ws = _workspace(uid)
    out = OPS.delete_everything(accounts(), ws, registry)
    limiter.forget(uid)
    request.session.clear()
    return out


@app.post("/api/{action}")
async def action(action: str, request: Request) -> Response:
    fn = api.ROUTES.get(f"/api/{action}")
    if fn is None:
        raise HTTPException(status_code=404, detail="no such action")
    uid = current_user(request)
    _limit(uid, "compile" if action == "compile" else "write")
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
