"""
SQLite persistence. Turns the in-memory Timeline into something that survives
the process.

The store is the authority on identity (which point index belongs to which
event) and the Timeline is the authority on order. Reloading must hand back
the *same* point indices, or every constraint in the table would point at the
wrong node, so events are replayed in ascending s_point order and the
allocation is asserted rather than assumed.

Nothing here rewrites history. Fragments are append-only, constraints are
retracted by flagging `retracted`, and a rejected constraint is written to
`conflicts` rather than dropped on the floor.
"""

from __future__ import annotations

import datetime as _dt
import json
import pathlib
import sqlite3
import uuid

from timeline import (INF, PROV_STATED, Constraint, Inconsistent, Timeline)

SCHEMA = pathlib.Path(__file__).with_name("schema.sql")

# Columns added after the first release. Tables and indexes are picked up from
# schema.sql automatically; columns cannot be, because ALTER TABLE needs an
# explicit default. Add a row here whenever you add a column to schema.sql.
_COLUMN_MIGRATIONS = [
    ("constraints", "question_id", "INTEGER"),
    ("paragraphs", "event_hash", "TEXT NOT NULL DEFAULT ''"),
]


def now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _j(x) -> str:
    """JSON with sorted keys. Render cache keys are built from these strings,
    so a nondeterministic dump would silently defeat the cache."""
    return json.dumps(x, sort_keys=True, separators=(",", ":"))


# SQLite has no infinity literal. Round-trip it through a sentinel that is far
# outside any plausible human lifespan but still a finite float.
_BIG = 1e12


def _enc(v: float) -> float:
    if v == INF:
        return _BIG
    if v == -INF:
        return -_BIG
    return float(v)


def _dec(v: float) -> float:
    if v >= _BIG:
        return INF
    if v <= -_BIG:
        return -INF
    return float(v)


class Store:
    """Owns the database and a Timeline kept in lockstep with it."""

    def __init__(self, path: str | pathlib.Path):
        self.path = pathlib.Path(path)
        fresh = not self.path.exists()
        # check_same_thread=False so the review UI's threaded server can
        # share one connection; every writer there holds a lock.
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        if fresh:
            self.db.executescript(SCHEMA.read_text())
            self.db.commit()
        else:
            self.db.execute("PRAGMA foreign_keys = ON")
            self._migrate()
        self.tl = self._load_timeline()

    # --------------------------------------------------------------- migrate

    def _migrate(self) -> list[str]:
        """Bring an older database up to the current schema, in place.

        The whole premise is that fragments are append-only and nothing
        written early is lost when the schema downstream changes -- so opening
        an old store has to work, not print an error and ask for a rebuild.
        New tables and indexes come straight from schema.sql; new columns come
        from _COLUMN_MIGRATIONS above.
        """
        applied: list[str] = []
        have = {r["name"] for r in self.db.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','index')")}

        import re
        ddl = SCHEMA.read_text()
        for stmt in ddl.split(";"):
            m = re.search(r"CREATE\s+(TABLE|INDEX)\s+(\w+)", stmt, re.I)
            if m and m.group(2) not in have:
                self.db.execute(stmt)
                applied.append(f"+{m.group(1).lower()} {m.group(2)}")

        for table, col, decl in _COLUMN_MIGRATIONS:
            cols = {r["name"] for r in
                    self.db.execute(f"PRAGMA table_info({table})")}
            if cols and col not in cols:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
                applied.append(f"+column {table}.{col}")

        if applied:
            self.db.commit()
        return applied

    def close(self) -> None:
        self.db.commit()
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # ------------------------------------------------------------------ load

    def _load_timeline(self) -> Timeline:
        rows = self.db.execute(
            "SELECT id, summary, s_point, e_point, mentions FROM events "
            "ORDER BY s_point"
        ).fetchall()
        tl = Timeline(capacity=max(64, 2 * len(rows) + 2))

        ents = {}
        for r in self.db.execute(
            "SELECT ee.event_id, e.name FROM event_entities ee "
            "JOIN entities e ON e.id = ee.entity_id"
        ):
            ents.setdefault(r["event_id"], set()).add(r["name"])

        for r in rows:
            ev = tl.add_event(r["id"], r["summary"],
                              entities=ents.get(r["id"], set()), restore=True)
            if (ev.s, ev.e) != (r["s_point"], r["e_point"]):
                raise RuntimeError(
                    f"point drift on {r['id']}: allocated {(ev.s, ev.e)}, "
                    f"stored {(r['s_point'], r['e_point'])}. The events table "
                    f"is not contiguous -- refusing to load a network whose "
                    f"constraints would address the wrong nodes."
                )
            ev.mentions = r["mentions"]

        # Known-consistent by construction: every one of these was proved
        # against the network before it was written. Skip the re-verification.
        for c in self.db.execute(
            "SELECT * FROM constraints WHERE retracted = 0 ORDER BY id"
        ):
            tl.add(c["x_point"], c["y_point"],
                   _dec(c["lo_days"]), _dec(c["hi_days"]),
                   provenance=c["provenance"], source=c["source"] or "",
                   note=c["note"] or "", check=False)
        return tl

    def reload(self) -> Timeline:
        self.tl = self._load_timeline()
        return self.tl

    # ------------------------------------------------------------- fragments

    def add_fragment(self, body: str, kind: str = "chat") -> str:
        fid = new_id("frag")
        self.db.execute(
            "INSERT INTO fragments (id, body, captured_at, kind) VALUES (?,?,?,?)",
            (fid, body, now(), kind),
        )
        self.db.commit()
        return fid

    def fragment(self, fid: str) -> str | None:
        r = self.db.execute("SELECT body FROM fragments WHERE id = ?",
                            (fid,)).fetchone()
        return r["body"] if r else None

    def fragments(self) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM fragments ORDER BY captured_at, id").fetchall()

    # ---------------------------------------------------------------- events

    def add_event(self, summary: str, entities=(), granularity: str = "episode",
                  from_fragment: str | None = None, id: str | None = None):
        eid = id or new_id("ev")
        ev = self.tl.add_event(eid, summary, entities=entities)
        self.db.execute(
            "INSERT INTO events (id, summary, granularity, s_point, e_point, "
            "mentions, created_from) VALUES (?,?,?,?,?,?,?)",
            (eid, summary, granularity, ev.s, ev.e, 1, from_fragment),
        )
        # add_event emitted the well-formedness edge into the live network;
        # persist it so the reload replays an identical network.
        self._persist_constraint(self.tl.constraints[-1])
        for name in entities:
            self.link_entity(eid, name)
        self.db.commit()
        return ev

    def bump_mentions(self, event_id: str, by: int = 1) -> None:
        self.tl.events[event_id].mentions += by
        self.db.execute("UPDATE events SET mentions = mentions + ? WHERE id = ?",
                        (by, event_id))
        self.db.commit()

    # -------------------------------------------------------------- entities

    def link_entity(self, event_id: str, name: str, kind: str = "person") -> str:
        r = self.db.execute("SELECT id FROM entities WHERE name = ?",
                            (name,)).fetchone()
        if r:
            ent_id = r["id"]
        else:
            ent_id = new_id("ent")
            self.db.execute(
                "INSERT INTO entities (id, name, kind, aliases) VALUES (?,?,?,?)",
                (ent_id, name, kind, "[]"),
            )
        self.db.execute(
            "INSERT OR IGNORE INTO event_entities (event_id, entity_id) "
            "VALUES (?,?)", (event_id, ent_id))
        self.tl.events[event_id].entities.add(name)
        self.db.commit()
        return ent_id

    def entities(self) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT e.*, COUNT(ee.event_id) AS n_events FROM entities e "
            "LEFT JOIN event_entities ee ON ee.entity_id = e.id "
            "GROUP BY e.id ORDER BY n_events DESC, e.name").fetchall()

    # ----------------------------------------------------------- constraints

    def _persist_constraint(self, c: Constraint,
                            question_id: int | None = None) -> int:
        cur = self.db.execute(
            "INSERT INTO constraints (x_point, y_point, lo_days, hi_days, "
            "provenance, source, question_id, note) VALUES (?,?,?,?,?,?,?,?)",
            (c.x, c.y, _enc(c.lo), _enc(c.hi), c.provenance,
             c.source or None, question_id, c.note or None),
        )
        return cur.lastrowid

    def assert_constraint(self, x: int, y: int, lo: float, hi: float,
                          provenance: int = PROV_STATED, source: str = "",
                          note: str = "",
                          question_id: int | None = None) -> tuple[bool, object]:
        """Try to land one constraint. Returns (accepted, Constraint | Inconsistent).
        A rejection is recorded in `conflicts` -- it is evidence about the
        source material, not a failure to be swallowed."""
        try:
            c = self.tl.add(x, y, lo, hi, provenance=provenance,
                            source=source, note=note)
        except Inconsistent as exc:
            self.db.execute(
                "INSERT INTO conflicts (x_point, y_point, lo_days, hi_days, "
                "provenance, source, note, culprits, detected_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (x, y, _enc(lo), _enc(hi), provenance, source or None,
                 note or None,
                 _j([{"prov": k.provenance, "note": k.note,
                      "lo": _enc(k.lo), "hi": _enc(k.hi)}
                     for k in exc.culprits]),
                 now()),
            )
            self.db.commit()
            return False, exc
        self._persist_constraint(c, question_id)
        self.db.commit()
        return True, c

    def conflicts(self, open_only: bool = True) -> list[sqlite3.Row]:
        q = "SELECT * FROM conflicts"
        if open_only:
            q += " WHERE resolution IS NULL"
        return self.db.execute(q + " ORDER BY id DESC").fetchall()

    def resolve_conflict(self, cid: int, resolution: str) -> None:
        self.db.execute("UPDATE conflicts SET resolution = ? WHERE id = ?",
                        (resolution, cid))
        self.db.commit()

    # ------------------------------------------------------------- questions

    def record_question(self, event_id: str, prompt: str, options: list) -> int:
        cur = self.db.execute(
            "INSERT INTO questions (event_id, prompt, options, asked_at) "
            "VALUES (?,?,?,?)",
            (event_id, prompt, _j(options), now()),
        )
        self.db.commit()
        return cur.lastrowid

    def record_answer(self, qid: int, choice: int) -> None:
        self.db.execute(
            "UPDATE questions SET answer = ?, answered_at = ? WHERE id = ?",
            (choice, now(), qid))
        self.db.commit()

    # ------------------------------------------------------------ unresolved

    def add_unresolved(self, fragment_id: str, text: str,
                       event_id: str | None = None, kind: str = "time") -> int:
        cur = self.db.execute(
            "INSERT INTO unresolved (fragment_id, event_id, text, kind) "
            "VALUES (?,?,?,?)", (fragment_id, event_id, text, kind))
        self.db.commit()
        return cur.lastrowid

    def unresolved(self, kind: str | None = None) -> list[sqlite3.Row]:
        q = "SELECT * FROM unresolved WHERE settled = 0"
        args: tuple = ()
        if kind:
            q += " AND kind = ?"
            args = (kind,)
        return self.db.execute(q + " ORDER BY id", args).fetchall()

    def settle_unresolved(self, uid: int) -> None:
        self.db.execute("UPDATE unresolved SET settled = 1 WHERE id = ?", (uid,))
        self.db.commit()

    # ------------------------------------------------------------ paragraphs

    def upsert_paragraph(self, pid: str, chapter: str, ordinal: float,
                         body: str, derived_from: list[str], style_hash: str,
                         event_hash: str = "", frozen: bool = False,
                         dirty: bool = False) -> None:
        self.db.execute(
            "INSERT INTO paragraphs (id, chapter, ordinal, body, derived_from, "
            "event_hash, style_hash, frozen, dirty) VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET chapter=excluded.chapter, "
            "ordinal=excluded.ordinal, body=excluded.body, "
            "derived_from=excluded.derived_from, "
            "event_hash=excluded.event_hash, style_hash=excluded.style_hash, "
            "dirty=excluded.dirty",
            (pid, chapter, ordinal, body, _j(derived_from), event_hash,
             style_hash, int(frozen), int(dirty)),
        )
        self.db.commit()

    def paragraphs(self, chapter: str | None = None) -> list[sqlite3.Row]:
        q = "SELECT * FROM paragraphs"
        args: tuple = ()
        if chapter:
            q += " WHERE chapter = ?"
            args = (chapter,)
        return self.db.execute(q + " ORDER BY chapter, ordinal", args).fetchall()

    def paragraph(self, pid: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM paragraphs WHERE id = ?",
                               (pid,)).fetchone()

    def set_frozen(self, pid: str, frozen: bool) -> None:
        self.db.execute("UPDATE paragraphs SET frozen = ? WHERE id = ?",
                        (int(frozen), pid))
        self.db.commit()

    def mark_dirty(self, event_ids: set[str]) -> int:
        """Any paragraph derived from a changed event is dirty. Frozen ones
        stay frozen and get flagged at compile time; they are never rewritten
        underneath the user."""
        n = 0
        for row in self.paragraphs():
            if set(json.loads(row["derived_from"])) & event_ids:
                self.db.execute("UPDATE paragraphs SET dirty = 1 WHERE id = ?",
                                (row["id"],))
                n += 1
        self.db.commit()
        return n

    def flagged(self) -> list[sqlite3.Row]:
        """Frozen paragraphs whose sources moved. The review queue."""
        return self.db.execute(
            "SELECT * FROM paragraphs WHERE frozen = 1 AND dirty = 1 "
            "ORDER BY chapter, ordinal").fetchall()

    # ------------------------------------------------------------ groundings

    def set_groundings(self, pid: str, verdicts: list[dict]) -> None:
        self.db.execute("DELETE FROM groundings WHERE paragraph_id = ?", (pid,))
        self.db.executemany(
            "INSERT INTO groundings (paragraph_id, sentence_ix, verdict, "
            "evidence) VALUES (?,?,?,?)",
            [(pid, v["ix"], v["verdict"], _j(v.get("evidence", [])))
             for v in verdicts],
        )
        self.db.commit()

    def groundings(self, pid: str) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM groundings WHERE paragraph_id = ? ORDER BY sentence_ix",
            (pid,)).fetchall()

    # ---------------------------------------------------------- render cache

    def cached_render(self, key: str) -> str | None:
        r = self.db.execute("SELECT body FROM render_cache WHERE key = ?",
                            (key,)).fetchone()
        return r["body"] if r else None

    def put_render(self, key: str, body: str, model: str) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO render_cache (key, body, model, created_at) "
            "VALUES (?,?,?,?)", (key, body, model, now()))
        self.db.commit()

    # --------------------------------------------------------------- compiles

    def record_compile(self, sha: str | None, rendered: int, cached: int,
                       flagged: int) -> int:
        cur = self.db.execute(
            "INSERT INTO compiles (commit_sha, compiled_at, n_rendered, "
            "n_cached, n_flagged) VALUES (?,?,?,?,?)",
            (sha, now(), rendered, cached, flagged))
        self.db.commit()
        return cur.lastrowid

    def compiles(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM compiles ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
