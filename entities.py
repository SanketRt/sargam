"""
Entity resolution.

An extracted entity is a bare string, so "Meera", "my wife" and "Meera
Kulkarni" arrive as three different people -- which splits the mention count
that anchor salience in placement.py depends on, and makes the same person
read as a stranger twice. This module folds them into one.

Resolution is never silent. A merge the code is confident about is applied; a
merge it is unsure about becomes a question, the same shape as a placement
question, so it is answered in the same loop. Nothing is guessed into the
graph, for the same reason extract.py refuses to invent a constraint: a wrong
merge is much harder to notice later than an unmerged duplicate.
"""

from __future__ import annotations

import difflib
import json
import re

# Definite descriptions that stand in for a person already named somewhere.
# These are references, not entities, and belong on the unresolved queue.
_REFERRING = re.compile(
    r"^(?:my|his|her|their|our)\s+"
    r"(wife|husband|mother|father|brother|sister|son|daughter|uncle|aunt|"
    r"cousin|friend|boss|neighbour|neighbor|teacher)$", re.I)

_TITLES = re.compile(r"^(?:mr|mrs|ms|dr|shri|smt|sir)\.?\s+", re.I)


def is_referring(name: str) -> bool:
    return bool(_REFERRING.match(name.strip()))


def normalise(name: str) -> str:
    return _TITLES.sub("", name.strip()).strip()


def aliases(store, entity_id: str) -> list[str]:
    r = store.db.execute("SELECT aliases FROM entities WHERE id = ?",
                         (entity_id,)).fetchone()
    return json.loads(r["aliases"]) if r else []


def add_alias(store, entity_id: str, alias: str) -> None:
    cur = set(aliases(store, entity_id))
    cur.add(alias)
    store.db.execute("UPDATE entities SET aliases = ? WHERE id = ?",
                     (json.dumps(sorted(cur)), entity_id))
    store.db.commit()


def resolve(store, name: str) -> str | None:
    """Existing entity id for `name`, matching canonical names and aliases.
    Returns None when the name is new or is a referring expression that needs
    a human to bind it."""
    name = normalise(name)
    if is_referring(name):
        return None
    r = store.db.execute("SELECT id FROM entities WHERE name = ?",
                         (name,)).fetchone()
    if r:
        return r["id"]
    for row in store.db.execute("SELECT id, aliases FROM entities"):
        if name in json.loads(row["aliases"]):
            return row["id"]
    return None


def merge(store, keep_id: str, drop_id: str) -> None:
    """Fold drop into keep. Event links are repointed, the dropped name
    survives as an alias so the same text resolves correctly next time."""
    if keep_id == drop_id:
        return
    drop = store.db.execute("SELECT * FROM entities WHERE id = ?",
                            (drop_id,)).fetchone()
    if drop is None:
        return
    rows = store.db.execute(
        "SELECT event_id FROM event_entities WHERE entity_id = ?",
        (drop_id,)).fetchall()
    for r in rows:
        store.db.execute(
            "INSERT OR IGNORE INTO event_entities (event_id, entity_id) "
            "VALUES (?,?)", (r["event_id"], keep_id))
    store.db.execute("DELETE FROM event_entities WHERE entity_id = ?", (drop_id,))

    keep_name = store.db.execute("SELECT name FROM entities WHERE id = ?",
                                 (keep_id,)).fetchone()["name"]
    merged = set(aliases(store, keep_id)) | set(json.loads(drop["aliases"]))
    merged.add(drop["name"])
    merged.discard(keep_name)
    store.db.execute("UPDATE entities SET aliases = ? WHERE id = ?",
                     (json.dumps(sorted(merged)), keep_id))
    store.db.execute("DELETE FROM entities WHERE id = ?", (drop_id,))
    store.db.commit()

    # Keep the live Timeline's entity sets in step with the store, so anchor
    # salience sees the merged mention count immediately.
    for r in rows:
        ev = store.tl.events.get(r["event_id"])
        if ev:
            ev.entities.discard(drop["name"])
            ev.entities.add(keep_name)


def _cooccurrence(store) -> dict[frozenset, int]:
    """How often two entities appear on the same event. Two names that never
    co-occur are more likely to be the same person than two that always do:
    a person is rarely introduced alongside themselves."""
    by_event: dict[str, set[str]] = {}
    for r in store.db.execute(
            "SELECT event_id, entity_id FROM event_entities"):
        by_event.setdefault(r["event_id"], set()).add(r["entity_id"])
    out: dict[frozenset, int] = {}
    for ents in by_event.values():
        for a in ents:
            for b in ents:
                if a < b:
                    k = frozenset((a, b))
                    out[k] = out.get(k, 0) + 1
    return out


def suggest_merges(store, threshold: float = 0.84) -> list[dict]:
    """Candidate duplicate pairs, best first. Surface diffs are the signal;
    co-occurrence is the veto."""
    rows = store.entities()
    co = _cooccurrence(store)
    out = []
    for i, a in enumerate(rows):
        for b in rows[i + 1:]:
            an, bn = normalise(a["name"]), normalise(b["name"])
            if an == bn:
                score, why = 1.0, "identical after normalisation"
            elif an.lower() in bn.lower().split() or bn.lower() in an.lower().split():
                score, why = 0.9, f"{an!r} is a name part of {bn!r}"
            else:
                score = difflib.SequenceMatcher(None, an.lower(), bn.lower()).ratio()
                why = "similar spelling"
            if score < threshold:
                continue
            if co.get(frozenset((a["id"], b["id"])), 0) > 0:
                continue          # they appear together, so they are not one
            out.append({
                "keep": a["id"] if a["n_events"] >= b["n_events"] else b["id"],
                "drop": b["id"] if a["n_events"] >= b["n_events"] else a["id"],
                "keep_name": a["name"] if a["n_events"] >= b["n_events"] else b["name"],
                "drop_name": b["name"] if a["n_events"] >= b["n_events"] else a["name"],
                "score": round(score, 3),
                "why": why,
            })
    out.sort(key=lambda d: -d["score"])
    return out


def sweep(store) -> int:
    """Apply only the merges that are not judgement calls -- exact matches
    after normalisation. Everything else stays a question."""
    n = 0
    for s in suggest_merges(store, threshold=0.999):
        merge(store, s["keep"], s["drop"])
        n += 1
    return n


def harvest_referring(store) -> int:
    """Move referring expressions off events and onto the unresolved queue as
    entity questions. 'my wife' is a pointer, not a person."""
    n = 0
    for e in store.entities():
        if not is_referring(e["name"]):
            continue
        rows = store.db.execute(
            "SELECT event_id FROM event_entities WHERE entity_id = ?",
            (e["id"],)).fetchall()
        for r in rows:
            already = store.db.execute(
                "SELECT 1 FROM unresolved WHERE event_id = ? AND text = ? "
                "AND kind = 'entity'", (r["event_id"], e["name"])).fetchone()
            if already:
                continue
            ev_row = store.db.execute(
                "SELECT created_from FROM events WHERE id = ?",
                (r["event_id"],)).fetchone()
            store.add_unresolved(ev_row["created_from"] or "", e["name"],
                                 event_id=r["event_id"], kind="entity")
            n += 1
    return n


def entity_question(store, row) -> dict:
    """An open entity row rendered as a question with concrete options: the
    people already in the book, most-mentioned first."""
    cands = [e for e in store.entities() if not is_referring(e["name"])][:8]
    ev = store.db.execute("SELECT summary FROM events WHERE id = ?",
                          (row["event_id"],)).fetchone()
    return {
        "unresolved_id": row["id"],
        "event_id": row["event_id"],
        "prompt": f"In “{ev['summary'] if ev else row['text']}”, "
                  f"who is “{row['text']}”?",
        "options": [{"entity_id": c["id"], "label": c["name"],
                     "n_events": c["n_events"]} for c in cands]
                   + [{"entity_id": None, "label": "Someone not listed yet"},
                      {"entity_id": "__skip__", "label": "Skip"}],
    }


def answer_entity(store, unresolved_id: int, entity_id: str | None,
                  new_name: str | None = None) -> None:
    """Bind a referring expression to a person. The expression becomes an
    alias, so the next fragment that says 'my wife' resolves without asking."""
    row = store.db.execute("SELECT * FROM unresolved WHERE id = ?",
                           (unresolved_id,)).fetchone()
    if row is None:
        return
    if entity_id == "__skip__":
        store.settle_unresolved(unresolved_id)
        return
    if entity_id is None:
        if not new_name:
            return
        entity_id = store.link_entity(row["event_id"], new_name)
    else:
        name = store.db.execute("SELECT name FROM entities WHERE id = ?",
                                (entity_id,)).fetchone()["name"]
        store.link_entity(row["event_id"], name)
    add_alias(store, entity_id, row["text"])

    old = store.db.execute("SELECT id FROM entities WHERE name = ?",
                           (row["text"],)).fetchone()
    if old and old["id"] != entity_id:
        merge(store, entity_id, old["id"])
    store.settle_unresolved(unresolved_id)
