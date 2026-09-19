"""
Export, deletion, and rate limiting.

Two of these exist because of what this stores. A memoir is not a spreadsheet:
someone who wants it back should get all of it in a form they can read without
this software, and someone who wants it gone should get a button rather than a
support thread. Both are written before anyone has trusted the thing with real
material, because retrofitting deletion onto a system that never planned for it
is how half-deleted accounts happen.

Deletion removes the account row and the workspace together. They are separate
databases by design, so it is the caller's job to do both -- and the order
matters: close the open store first, or the process keeps a handle on files it
has just unlinked.
"""

from __future__ import annotations

import io
import json
import shutil
import threading
import time
import zipfile

from . import render as R


# ------------------------------------------------------------------- export

def export_zip(store, workspace) -> bytes:
    """Everything one account holds, as a zip.

    Fragments are the only irreplaceable part -- events, constraints and the
    manuscript are all derived from them -- so they go in as plain text, one
    file each, readable with no tooling at all. The structured layers go in as
    JSON beside them for anyone who wants to reload or inspect them.
    """
    tl = store.tl
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("README.txt",
                   "Your material, exported from sargam.\n\n"
                   "fragments/   what you wrote, one file per entry. This is\n"
                   "             the irreplaceable part; everything else is\n"
                   "             derived from it.\n"
                   "manuscript/  the compiled chapters, as markdown.\n"
                   "events.json      extracted events and their solved dates\n"
                   "constraints.json the temporal facts behind those dates\n"
                   "fragments.json   the same fragments, with timestamps\n")

        frags = [dict(r) for r in store.fragments()]
        for r in frags:
            z.writestr(f"fragments/{r['id']}.txt", r["body"])
        z.writestr("fragments.json", json.dumps(frags, indent=2))

        events = []
        for ev in tl.order():
            lo, hi = tl.event_bounds(ev.id)
            events.append({
                "id": ev.id, "summary": ev.summary,
                "entities": sorted(ev.entities),
                "when": R.coarse_when(tl, ev.id),
                "earliest_days": None if lo == float("-inf") else lo,
                "latest_days": None if hi == float("inf") else hi,
            })
        z.writestr("events.json", json.dumps(events, indent=2))

        z.writestr("constraints.json", json.dumps([
            {"x": c.x, "y": c.y,
             "lo_days": None if c.lo == float("-inf") else c.lo,
             "hi_days": None if c.hi == float("inf") else c.hi,
             "provenance": c.provenance, "source": c.source, "note": c.note}
            for c in tl.constraints], indent=2))

        z.writestr("paragraphs.json", json.dumps(
            [dict(r) for r in store.paragraphs()], indent=2, default=str))

        man = workspace.manuscript
        if man.exists():
            for f in sorted(man.glob("*.md")):
                z.writestr(f"manuscript/{f.name}", f.read_text())

    return buf.getvalue()


# ----------------------------------------------------------------- deletion

def delete_everything(accounts, workspace, registry=None) -> dict:
    """Remove one account and its material. Not recoverable.

    The store is closed and dropped from the registry first: unlinking files
    the process still holds open leaves a half-deleted account and a handle to
    data that is supposed to be gone.
    """
    user_id = workspace.user_id
    if registry is not None:
        registry.drop(user_id)

    removed_files = False
    if workspace.root.exists():
        shutil.rmtree(workspace.root, ignore_errors=False)
        removed_files = True

    accounts.delete(user_id)
    return {"ok": True, "deleted_workspace": removed_files,
            "deleted_account": True}


# ------------------------------------------------------------- rate limits

class RateLimiter:
    """A token bucket per (user, action).

    Not a security boundary -- a signed-in account is already identified. It
    is there so one enthusiastic loop cannot spend an afternoon's worth of
    somebody's API credit, or fill a volume, before anyone notices.
    """

    def __init__(self):
        self._buckets: dict[tuple[str, str], tuple[float, float]] = {}
        self._guard = threading.Lock()

    def check(self, user_id: str, action: str, per_minute: float,
              burst: float | None = None) -> tuple[bool, float]:
        """(allowed, seconds_until_next) -- refuses without consuming."""
        cap = burst if burst is not None else per_minute
        rate = per_minute / 60.0
        now = time.monotonic()
        key = (user_id, action)
        with self._guard:
            tokens, last = self._buckets.get(key, (cap, now))
            tokens = min(cap, tokens + (now - last) * rate)
            if tokens < 1.0:
                self._buckets[key] = (tokens, now)
                return False, (1.0 - tokens) / rate
            self._buckets[key] = (tokens - 1.0, now)
            return True, 0.0

    def forget(self, user_id: str) -> None:
        with self._guard:
            for key in [k for k in self._buckets if k[0] == user_id]:
                del self._buckets[key]


# Compiles and credential checks both reach outward -- one spends the user's
# API credit, the other hits Anthropic to validate a key. Reads are cheap and
# only bounded to stop a runaway client.
LIMITS = {
    "compile": (6, 3),        # per minute, burst
    "key": (5, 5),
    "export": (4, 2),
    "write": (60, 30),
    "read": (240, 120),
}
