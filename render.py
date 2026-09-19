"""
Compiling a network of events into chapters of prose.

A paragraph is a compilation unit. It records what it was derived from, the
style it was written in, and whether the user has frozen it. Re-compiling is a
pure function of (events, style): if neither moved, the cached body comes back
byte-identical and the manuscript diff is empty.

That cache is not an optimisation, it is the correctness mechanism. Determinism
cannot come from `temperature=0` -- current models reject the parameter -- so
nothing here re-renders unless its inputs actually changed, which is what lets
`git diff` after a compile mean "the model changed its mind" rather than "the
model was sampled again".

Two deliberate choices:

* A paragraph is keyed on its *earliest* event, not on its whole event set.
  Keying on the set would mint a new paragraph id every time an event joined
  one, losing the freeze flag exactly when it matters -- the moment the
  sources moved under prose the user had approved.

* The event-set hash uses the *year-level* date string, not the raw float
  bounds. Every new constraint nudges the bounds a little; only a change big
  enough to alter what the prose says should force a re-render.
"""

from __future__ import annotations

import hashlib
import json

from timeline import INF, YEAR, as_date

STYLES = {
    "plain": (
        "Write in plain, unadorned first-person past tense. Short sentences. "
        "The user's own nouns. No literary flourish, no scene-setting the "
        "sources do not contain, no invented sensory detail."
    ),
    "warm": (
        "Write in first-person past tense with warmth and rhythm, the way "
        "someone tells a story to family. Keep every concrete fact exactly as "
        "given. Add no detail that is not in the sources."
    ),
    "terse": (
        "Write in terse first-person past tense. One or two sentences per "
        "event. Facts only."
    ),
}

RENDER_SYSTEM = """\
You write one paragraph of a memoir from a set of events and the user's own
words about them.

Absolute rules:
- Every name, date, place, number and quotation must appear in the sources.
- Invent nothing. No weather, no emotions, no dialogue, no sensory detail that
  is not in the sources. If the sources are thin, write a short paragraph.
- Keep the events in the order given; that order was solved, not guessed.
- Do not open with a date unless the sources state one.
- Return the paragraph only. No heading, no preamble, no commentary.
"""


def _h(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def style_hash(style: str) -> str:
    return _h(STYLES[style] + "\x00" + RENDER_SYSTEM)[:16]


def coarse_when(tl, event_id: str) -> str:
    """Year-level position. This is what the prose can honestly say, and what
    the cache key is allowed to depend on."""
    lo, hi = tl.event_bounds(event_id)
    if lo == -INF and hi == INF:
        return "unplaced"
    if lo == -INF:
        return f"by {as_date(hi).year}"
    if hi == INF:
        return f"from {as_date(lo).year}"
    a, b = as_date(lo).year, as_date(hi).year
    return str(a) if a == b else f"{a}-{b}"


def event_set_hash(tl, event_ids: list[str]) -> str:
    """Everything that could legitimately change the prose."""
    payload = [
        {
            "id": eid,
            "summary": tl.events[eid].summary,
            "entities": sorted(tl.events[eid].entities),
            "when": coarse_when(tl, eid),
        }
        for eid in event_ids
    ]
    return _h(json.dumps(payload, sort_keys=True, separators=(",", ":")))[:16]


# ----------------------------------------------------------------- chapters

def chapters(tl, target: int = 8, min_size: int = 3) -> list[list[str]]:
    """Cut the solved order into chapters at the largest temporal gaps.

    Gap size, not event count, is the signal: a memoir's chapters are the
    stretches of life separated by the moves, the deaths, the long quiet
    years. Cutting purely on count would split a dense decade in half and
    glue two unrelated eras together.
    """
    placed, floating = [], []
    for ev in tl.order():
        lo, hi = tl.event_bounds(ev.id)
        (floating if lo == -INF and hi == INF else placed).append(ev.id)

    def _finish(groups):
        return [g for g in groups if g] + ([floating] if floating else [])

    order = placed
    if len(order) <= target:
        return _finish([order])

    lows = []
    for eid in order:
        lo, hi = tl.event_bounds(eid)
        lows.append(lo if lo != -INF else (hi if hi != INF else 0.0))

    gaps = sorted(
        ((lows[i + 1] - lows[i], i + 1) for i in range(len(order) - 1)),
        reverse=True,
    )
    n_cuts = max(1, round(len(order) / target) - 1)

    cuts: list[int] = []
    for _, idx in gaps:
        if len(cuts) >= n_cuts:
            break
        bounds = sorted(cuts + [0, len(order)])
        # Never make a chapter shorter than min_size.
        prev = max(b for b in bounds if b <= idx)
        nxt = min(b for b in bounds if b > idx)
        if idx - prev < min_size or nxt - idx < min_size:
            continue
        cuts.append(idx)

    cuts = sorted(cuts)
    out, start = [], 0
    for c in cuts + [len(order)]:
        out.append(order[start:c])
        start = c
    return _finish(out)


def chapter_title(tl, event_ids: list[str]) -> str:
    spans = [coarse_when(tl, e) for e in event_ids]
    if all(s == "unplaced" for s in spans):
        return "Not yet placed"
    years = [int(y) for s in spans for y in s.replace("-", " ").split()
             if y.isdigit()]
    if not years:
        return "Unplaced"
    a, b = min(years), max(years)
    return str(a) if a == b else f"{a}–{b}"


# --------------------------------------------------------------- paragraphs

def group(tl, event_ids: list[str], max_events: int = 4) -> list[list[str]]:
    """Split a chapter into paragraph-sized runs. A run breaks when the next
    event shares no entity with the run and sits a long way after it."""
    runs: list[list[str]] = []
    cur: list[str] = []
    for eid in event_ids:
        if not cur:
            cur = [eid]
            continue
        prev = tl.events[cur[-1]]
        ev = tl.events[eid]
        shared = bool(prev.entities & ev.entities)
        lo_prev = tl.event_bounds(prev.id)[0]
        lo_now = tl.event_bounds(eid)[0]
        far = (lo_prev != -INF and lo_now != -INF
               and (lo_now - lo_prev) > 3 * YEAR)
        if len(cur) >= max_events or (far and not shared):
            runs.append(cur)
            cur = [eid]
        else:
            cur.append(eid)
    if cur:
        runs.append(cur)
    return runs


def paragraph_id(event_ids: list[str]) -> str:
    return f"para_{_h(event_ids[0])[:12]}"


def sources_for(store, event_ids: list[str]) -> dict[str, str]:
    """Fragment id -> body, for every fragment that produced these events.
    This is what the paragraph is allowed to contain, and what grounding
    checks it against."""
    out: dict[str, str] = {}
    for eid in event_ids:
        r = store.db.execute("SELECT created_from FROM events WHERE id = ?",
                             (eid,)).fetchone()
        fid = r["created_from"] if r else None
        if fid and fid not in out:
            body = store.fragment(fid)
            if body:
                out[fid] = body
    return out


# ------------------------------------------------------------------- writing

def _decap(s: str, entities: set[str]) -> str:
    """Lowercase the opening word when it is capitalised by orthography rather
    than because it names something.

    The event's own entity set is the evidence: those are the words the
    extractor identified as people and places. Anything else opening a summary
    is a common word or the pronoun "I", and "In 1984, Moving to Pune" reads
    like a bug while "In 1984, moving to Pune" reads like a sentence.
    """
    first = s.split(" ", 1)[0]
    if first == "I" or first.rstrip(",.") in entities:
        return s
    return s[:1].lower() + s[1:]


def offline_paragraph(tl, event_ids: list[str], sources: dict[str, str]) -> str:
    """Deterministic, no network. Says exactly what the events say and dates
    them only where the network actually proves a date. Plain, but it never
    fabricates, which makes it a legitimate manuscript rather than a stub."""
    parts = []
    for i, eid in enumerate(event_ids):
        ev = tl.events[eid]
        when = coarse_when(tl, eid)
        s = ev.summary.strip().rstrip(".")
        s = s[0].upper() + s[1:] if s else s
        if when == "unplaced":
            parts.append(f"{s}.")
        elif when.startswith(("by ", "from ")):
            parts.append(f"{s} ({when}).")
        elif i:
            parts.append(f"{s} ({when}).")
        else:
            parts.append(f"In {when}, {_decap(s, ev.entities)}.")
    return " ".join(parts)


def api_paragraph(tl, event_ids: list[str], sources: dict[str, str],
                  style: str, api_key: str | None = None) -> str:
    import extract
    lines = []
    for eid in event_ids:
        ev = tl.events[eid]
        ents = ", ".join(sorted(ev.entities)) or "-"
        lines.append(f"- {ev.summary} | when: {coarse_when(tl, eid)} | "
                     f"people/places: {ents}")
    src = "\n\n".join(f"[{k}] {v}" for k, v in sources.items()) or "(none)"
    user = (f"Style: {STYLES[style]}\n\n"
            f"Events, in solved order:\n" + "\n".join(lines) +
            f"\n\nThe user's own words (the only permitted source of fact):\n{src}")
    r = extract._client(api_key).messages.create(
        model=extract.MODEL,
        max_tokens=2000,
        system=RENDER_SYSTEM,
        messages=[{"role": "user", "content": user}],
    )
    if r.stop_reason == "refusal":
        raise RuntimeError("model declined to render this paragraph")
    return "".join(b.text for b in r.content if b.type == "text").strip()


def write_paragraph(store, event_ids: list[str], style: str,
                    api_key: str | None = None) -> tuple[str, bool]:
    """Returns (body, from_cache).

    The cache key deliberately does not include the api key: two users with
    the same events and the same style should get the same paragraph, and
    keying on the credential would both leak it into the key space and make
    the cache useless in a multi-tenant store.
    """
    import extract
    tl = store.tl
    key = _h(event_set_hash(tl, event_ids) + style_hash(style))
    hit = store.cached_render(key)
    if hit is not None:
        return hit, True
    sources = sources_for(store, event_ids)
    if extract.backend(api_key) == "offline":
        body = offline_paragraph(tl, event_ids, sources)
        model = "offline"
    else:
        body = api_paragraph(tl, event_ids, sources, style, api_key=api_key)
        model = extract.MODEL
    store.put_render(key, body, model)
    return body, False


# -------------------------------------------------------------------- compile

def compile_book(store, style: str = "plain", do_ground: bool = True,
                 api_key: str | None = None) -> dict:
    """Recompile every chapter. Frozen paragraphs whose sources moved are
    flagged and left exactly as they are."""
    import ground as G

    tl = store.tl
    sh = style_hash(style)
    rendered = cached = flagged = 0
    book: list[dict] = []
    live_ids: set[str] = set()

    for chap in chapters(tl):
        title = chapter_title(tl, chap)
        paras = []
        for ordinal, run in enumerate(group(tl, chap)):
            pid = paragraph_id(run)
            live_ids.add(pid)
            existing = store.paragraph(pid)
            eh = event_set_hash(tl, run)

            if existing and existing["frozen"]:
                # Membership alone is not enough: the usual way a frozen
                # paragraph goes stale is an event it already contained being
                # re-dated or re-summarised, which leaves the id list intact.
                stale = (existing["style_hash"] != sh
                         or set(json.loads(existing["derived_from"])) != set(run)
                         or (existing["event_hash"] or "") != eh)
                if stale:
                    flagged += 1
                    store.db.execute(
                        "UPDATE paragraphs SET dirty = 1 WHERE id = ?", (pid,))
                    store.db.commit()
                paras.append({"id": pid, "body": existing["body"],
                              "derived_from": json.loads(existing["derived_from"]),
                              "frozen": True, "flagged": bool(stale)})
                continue

            body, from_cache = write_paragraph(store, run, style,
                                               api_key=api_key)
            cached += from_cache
            rendered += not from_cache
            store.upsert_paragraph(pid, title, float(ordinal), body, run, sh,
                                   event_hash=eh, frozen=False, dirty=False)
            if do_ground and not from_cache:
                G.check(store, pid, body, sources_for(store, run),
                        api_key=api_key)
            paras.append({"id": pid, "body": body, "derived_from": run,
                          "frozen": False, "flagged": False})
        book.append({"title": title, "paragraphs": paras, "events": chap})

    # Paragraphs whose anchor event no longer leads any run are orphans.
    for row in store.paragraphs():
        if row["id"] not in live_ids and not row["frozen"]:
            store.db.execute("DELETE FROM paragraphs WHERE id = ?", (row["id"],))
    store.db.commit()

    return {"chapters": book, "rendered": rendered, "cached": cached,
            "flagged": flagged, "style": style}
