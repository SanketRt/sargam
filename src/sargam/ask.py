"""
The human loop, wired to the store.

placement.py deliberately knows nothing about persistence: it takes a Timeline
and returns a Question. This module is the adapter -- it picks what to ask
next, writes the question and the answer to the store, and lands the resulting
constraint through Store.assert_constraint so a contradicting answer is
recorded as a conflict instead of raising into the UI.

Two queues feed it, and they interleave on purpose. A placement question is
worth more once the entities are resolved, because anchor salience counts
mentions per entity -- so entity questions go first when both are pending.
"""

from __future__ import annotations

from . import placement as P
from . import entities as E
from .timeline import INF, PROV_USER_PLACED, YEAR


def loosest(store, tolerance_days: float = 2 * YEAR) -> list[str]:
    """Unplaced events, loosest first -- the ones where an answer buys most."""
    evs = store.tl.unplaced(tolerance_days)
    return [ev.id for ev in sorted(evs, key=lambda e: -store.tl.slack(e.id))]


def next_placement(store, exclude: frozenset[str] = frozenset()):
    """(event_id, Question) or None."""
    for eid in loosest(store):
        q = P.next_question(store.tl, eid, exclude=exclude)
        if q is not None:
            return eid, q
    return None


def apply_placement(store, q, choice: int,
                    question_id: int | None = None) -> tuple[bool, str]:
    """Land one answer. Returns (changed, message)."""
    opt = q.options[choice]
    tl = store.tl
    if opt.kind == P.UNSURE:
        return False, "left floating"

    a = tl.events[q.event_id]
    b = tl.events[opt.anchor_id]
    note = "user placement"
    kw = dict(provenance=PROV_USER_PLACED, question_id=question_id,
              note=note)

    if opt.kind == P.BEFORE:
        ok, res = store.assert_constraint(b.s, a.e, 1.0, INF, **kw)
    elif opt.kind == P.AFTER:
        ok, res = store.assert_constraint(a.s, b.e, 1.0, INF, **kw)
    elif opt.kind == P.DURING:
        ok, res = store.assert_constraint(a.s, b.s, -YEAR / 2, YEAR / 2, **kw)
    elif opt.kind == P.COINCIDENT:
        ok, res = store.assert_constraint(a.s, b.s, -30.0, 30.0, **kw)
    else:
        return False, "unknown option"

    if not ok:
        return False, ("that contradicts what is already known; "
                       "recorded as a conflict")
    return True, "placed"


def pending(store) -> dict:
    return {
        "placement": len(loosest(store)),
        "entity": len(store.unresolved(kind="entity")),
        "time": len(store.unresolved(kind="time")),
        "conflicts": len(store.conflicts()),
        "flagged": len(store.flagged()),
    }


def next_entity(store):
    rows = store.unresolved(kind="entity")
    if not rows:
        return None
    return E.entity_question(store, rows[0])


def question_stream(store, limit: int = 20):
    """Yield questions until the budget runs out or nothing is left to ask.
    Entities first: resolving 'my wife' into Meera improves every subsequent
    placement question that uses Meera as an anchor."""
    asked = 0
    seen: set[str] = set()
    while asked < limit:
        eq = next_entity(store)
        if eq is not None:
            yield ("entity", eq)
            asked += 1
            continue
        nxt = next_placement(store, exclude=frozenset(seen))
        if nxt is None:
            return
        eid, q = nxt
        anchor = q.options[0].anchor_id
        if anchor in seen:
            return
        seen.add(anchor)
        yield ("placement", q)
        asked += 1
