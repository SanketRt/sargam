"""
Properties of the store and the compiler. Run: python test_pipeline.py

test_timeline.py guards the solver. These guard the things built on top of it,
and the list is chosen the same way -- not "does it produce a book", but the
invariants that, if they broke, would let the system quietly lie to you:

  1. a reloaded network is the same network (point indices survive)
  2. recompiling changes nothing unless an input changed
  3. a frozen paragraph is never rewritten underneath the user
  4. an unsupported sentence never reaches the manuscript
  5. a contradiction is recorded, not raised, and mutates nothing
  6. merging two entities loses no event link
  7. an event with no temporal information renders into the holding section
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

import numpy as np

from sargam import entities as E
from sargam import extract
from sargam import ground as G
from sargam import publish
from sargam import render as R
from sargam import store as S
from sargam.timeline import INF, PROV_ABSOLUTE, PROV_STATED, YEAR, days


class tmp:
    """A throwaway project directory."""

    def __enter__(self):
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix="sargam-test-"))
        self.st = S.Store(self.dir / "store.db")
        return self

    def __exit__(self, *exc):
        self.st.close()
        shutil.rmtree(self.dir, ignore_errors=True)
        return False

    def seed(self):
        """A small life with one dated anchor and two relative facts."""
        st = self.st
        f = st.add_fragment("I got married in April 1986. We moved to Pune "
                            "about two years earlier. The mill job was 1977.")
        w = st.add_event("my wedding", entities=["Meera"], from_fragment=f)
        p = st.add_event("moving to Pune", entities=["Pune"], from_fragment=f)
        m = st.add_event("the mill job", entities=["Nagpur"], from_fragment=f)
        st.assert_constraint(w.s, 0, days("1986-04-01"), days("1986-04-30"),
                             PROV_ABSOLUTE, f)
        st.assert_constraint(w.e, 0, days("1986-04-01"), days("1986-04-30"),
                             PROV_ABSOLUTE, f)
        st.assert_constraint(m.s, 0, days("1977-01-01"), days("1977-12-31"),
                             PROV_ABSOLUTE, f)
        st.assert_constraint(w.s, p.e, 1.5 * YEAR, 2.5 * YEAR, PROV_STATED, f)

        # Two more events sharing the wedding's entity and sitting right next
        # to it, so at least one paragraph groups several events together --
        # single-sentence paragraphs would not exercise sentence indexing.
        for summary, lo, hi in [("the reception", "1986-04-20", "1986-04-30"),
                                ("the honeymoon", "1986-05-01", "1986-05-20")]:
            ev = st.add_event(summary, entities=["Meera"], from_fragment=f)
            st.assert_constraint(ev.s, 0, days(lo), days(hi), PROV_ABSOLUTE, f)
            st.assert_constraint(ev.e, 0, days(lo), days(hi), PROV_ABSOLUTE, f)
        return f, w, p, m


# ---------------------------------------------------------------------- tests

def test_reload_is_the_same_network() -> None:
    """Point indices are the one thing a reload cannot get wrong: every
    constraint in the table addresses nodes by index, so drift by one would
    silently re-point the whole network at the wrong events."""
    with tmp() as t:
        t.seed()
        before_D = t.st.tl.D.copy()
        before = {e: t.st.tl.event_bounds(e) for e in t.st.tl.events}
        path = t.st.path
        t.st.close()

        st2 = S.Store(path)
        assert np.allclose(st2.tl.D, before_D, equal_nan=True), "matrix drifted"
        for eid, b in before.items():
            assert st2.tl.event_bounds(eid) == b, f"{eid} moved on reload"
        assert len(st2.tl.events) == len(before)
        st2.close()
        t.st = S.Store(path)          # so __exit__ has something to close
    print("ok  reload reconstructs an identical network")


def test_old_database_migrates() -> None:
    """Opening a store written by an earlier version must work. The design
    promise is that fragments are append-only and nothing written early is
    lost when the schema downstream changes -- a migration failure would break
    exactly the material that is hardest to recreate."""
    d = pathlib.Path(tempfile.mkdtemp(prefix="sargam-old-"))
    try:
        # Reconstruct the pre-Layer-4 schema: no render_cache / unresolved /
        # conflicts / compiles tables, no question_id, no event_hash.
        ddl = S.SCHEMA.read_text().split("-- Layer 4")[0]
        ddl = ddl.replace("  question_id INTEGER,"
                          "                        -- set when you answered for it\n", "")
        ddl = "\n".join(l for l in ddl.splitlines()
                        if not l.strip().startswith("event_hash"))
        ddl = ddl.replace("-- an edited summary or a moved date", "")
        ddl = ddl.replace("-- is detectable, not just membership", "")
        ddl = ddl.replace("-- hash of those events' *content*, so", "")
        import sqlite3
        con = sqlite3.connect(d / "old.db")
        con.executescript(ddl)
        con.execute("INSERT INTO fragments (id, body, captured_at, kind) "
                    "VALUES ('f1','an early memory','2020-01-01','chat')")
        con.commit()
        con.close()

        st = S.Store(d / "old.db")
        cols = {r["name"] for r in st.db.execute("PRAGMA table_info(paragraphs)")}
        assert "event_hash" in cols, "column migration did not run"
        cols = {r["name"] for r in st.db.execute("PRAGMA table_info(constraints)")}
        assert "question_id" in cols, "column migration did not run"
        tables = {r["name"] for r in st.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for needed in ("render_cache", "unresolved", "conflicts", "compiles"):
            assert needed in tables, f"table {needed} not created"
        assert st.fragment("f1") == "an early memory", "existing data lost"

        # And it must be a no-op the second time.
        assert st._migrate() == [], "migration is not idempotent"
        st.close()
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("ok  an older database migrates in place without losing anything")


def test_recompile_is_a_noop() -> None:
    """The cache is the determinism mechanism now that temperature is gone.
    Two compiles with nothing changed in between must render zero paragraphs
    and leave git with nothing to commit."""
    with tmp() as t:
        t.seed()
        out = t.dir / "manuscript"

        b1 = R.compile_book(t.st, do_ground=False)
        r1 = publish.write(t.st, b1, out)
        sha1 = publish.commit(r1["repo"], "first")
        first = {f: (out / f).read_text() for f in r1["files"]}
        assert sha1, "first compile should commit something"

        b2 = R.compile_book(t.st, do_ground=False)
        r2 = publish.write(t.st, b2, out)
        sha2 = publish.commit(r2["repo"], "second")

        assert b2["rendered"] == 0, f"re-rendered {b2['rendered']} paragraphs"
        assert b2["cached"] == b1["rendered"], "cache did not cover everything"
        assert sha2 is None, "a no-op compile still produced a commit"
        for f, body in first.items():
            assert (out / f).read_text() == body, f"{f} changed byte-wise"
    print("ok  recompiling with no change renders nothing and commits nothing")


def test_new_fact_reaches_the_manuscript() -> None:
    """The mirror of the previous test: the cache must not be so sticky that
    a real change is invisible."""
    with tmp() as t:
        f, w, p, m = t.seed()
        out = t.dir / "manuscript"
        R.compile_book(t.st, do_ground=False)

        # Pune is only known to within decades: its start is bounded below
        # only by the 30-year well-formedness edge. Pin it to a single year --
        # consistent with everything known, but it changes what the prose can
        # say, from a range to a date.
        before = R.coarse_when(t.st.tl, p.id)
        ok, _ = t.st.assert_constraint(p.s, 0, days("1984-01-01"),
                                       days("1984-06-30"), PROV_ABSOLUTE, f)
        assert ok, "a consistent tightening was rejected"
        assert R.coarse_when(t.st.tl, p.id) != before, "the prose did not change"

        b2 = R.compile_book(t.st, do_ground=False)
        assert b2["rendered"] > 0, "a tightened date did not invalidate the cache"
        r2 = publish.write(t.st, b2, out)
        assert publish.commit(r2["repo"], "third") is not None, \
            "a real change produced no commit"
    print("ok  a changed date does invalidate the cache")


def test_frozen_is_never_rewritten() -> None:
    with tmp() as t:
        f, w, p, m = t.seed()
        b1 = R.compile_book(t.st, do_ground=False)
        pid = b1["chapters"][0]["paragraphs"][0]["id"]
        original = t.st.paragraph(pid)["body"]
        t.st.set_frozen(pid, True)

        # Move something this paragraph is derived from.
        for eid in b1["chapters"][0]["paragraphs"][0]["derived_from"]:
            t.st.tl.events[eid].summary = "rewritten summary"
            t.st.db.execute("UPDATE events SET summary = ? WHERE id = ?",
                            ("rewritten summary", eid))
        t.st.db.commit()

        b2 = R.compile_book(t.st, do_ground=False)
        assert t.st.paragraph(pid)["body"] == original, "frozen prose was rewritten"
        assert b2["flagged"] >= 1, "changed sources under frozen prose not flagged"
        assert any(r["id"] == pid for r in t.st.flagged()), "not in review queue"
    print("ok  frozen paragraphs are flagged, never rewritten")


def test_unsupported_never_ships() -> None:
    body = ("I got married in April 1986. A brass band played for six hours.")
    verdicts = [{"ix": 0, "verdict": G.SUPPORTED, "evidence": ["f1"]},
                {"ix": 1, "verdict": G.UNSUPPORTED, "evidence": []}]
    out, dropped = G.strip_unsupported(body, verdicts)
    assert dropped == 1
    assert "brass band" not in out, "fabricated sentence reached the manuscript"
    assert "married" in out, "supported sentence was dropped too"

    # And the offline grounder must actually catch that shape.
    got = extract.offline_ground(body, {"f1": "I got married in April 1986."})
    v = {s["ix"]: s["verdict"] for s in got["sentences"]}
    assert v[0] == G.SUPPORTED, v
    assert v[1] == G.UNSUPPORTED, v
    print("ok  unsupported sentences are caught and stripped")


def test_stored_verdicts_round_trip() -> None:
    """The store spells it `sentence_ix` and JSON-encodes evidence; the rest of
    the code says `ix` and wants a list. The mismatch is invisible until a
    sentence is genuinely unsupported, so test exactly that path."""
    with tmp() as t:
        t.seed()
        R.compile_book(t.st, do_ground=False)
        rows = sorted(t.st.paragraphs(),
                      key=lambda r: -len(extract.sentences_of(r["body"])))
        pid = rows[0]["id"]
        body = rows[0]["body"]
        n = len(extract.sentences_of(body))
        assert n >= 2, f"no multi-sentence paragraph to test: {body!r}"

        t.st.set_groundings(pid, [
            {"ix": 0, "verdict": G.UNSUPPORTED, "evidence": []},
            *[{"ix": i, "verdict": G.SUPPORTED, "evidence": ["f1"]}
              for i in range(1, n)],
        ])
        vs = G.from_rows(t.st.groundings(pid))
        assert vs[0]["ix"] == 0 and vs[0]["evidence"] == []
        assert vs[1]["evidence"] == ["f1"], "evidence did not decode"

        out, dropped = G.strip_unsupported(body, vs)
        assert dropped == 1, "stored unsupported verdict was not stripped"

        book = R.compile_book(t.st, do_ground=False)
        rep = publish.write(t.st, book, t.dir / "manuscript")
        assert rep["dropped"] >= 1, "publish did not strip through the store"
        text = "\n".join((t.dir / "manuscript" / f).read_text()
                          for f in rep["files"])
        assert extract.sentences_of(body)[0] not in text, \
            "the unsupported sentence reached the manuscript"

        # And the review UI's annotation path must survive the same rows.
        ann = G.annotate(body, vs)
        assert ann[0]["verdict"] == G.UNSUPPORTED
    print("ok  stored grounding verdicts round-trip and strip correctly")


def test_conflict_is_recorded_not_raised() -> None:
    with tmp() as t:
        f, w, p, m = t.seed()
        snapshot = t.st.tl.D.copy()
        n_before = len(t.st.tl.constraints)

        # "the mill job was after the wedding" -- it was not.
        ok, res = t.st.assert_constraint(m.s, w.e, 1.0, INF, PROV_STATED, f)
        assert ok is False, "a contradiction was accepted"
        assert np.allclose(t.st.tl.D, snapshot, equal_nan=True), "state mutated"
        assert len(t.st.tl.constraints) == n_before
        rows = t.st.conflicts()
        assert len(rows) == 1, "conflict was not recorded"
        assert rows[0]["resolution"] is None, "conflict auto-resolved itself"
    print("ok  a contradiction is logged as evidence and changes nothing")


def test_merge_loses_no_links() -> None:
    with tmp() as t:
        f = t.st.add_fragment("x")
        a = t.st.add_event("the wedding", entities=["Meera"], from_fragment=f)
        b = t.st.add_event("moving house", entities=["Meera Kulkarni"],
                           from_fragment=f)
        before = {r["event_id"] for r in t.st.db.execute(
            "SELECT event_id FROM event_entities")}

        sugg = E.suggest_merges(t.st)
        assert sugg, "obvious duplicate not suggested"
        E.merge(t.st, sugg[0]["keep"], sugg[0]["drop"])

        after = {r["event_id"] for r in t.st.db.execute(
            "SELECT event_id FROM event_entities")}
        assert after == before, "merge dropped an event link"
        names = [e["name"] for e in t.st.entities()]
        assert len(names) == 1, f"merge left duplicates: {names}"
        assert "Meera Kulkarni" in E.aliases(t.st, t.st.entities()[0]["id"]), \
            "the dropped name did not survive as an alias"
        assert E.resolve(t.st, "Meera Kulkarni") is not None, \
            "the alias does not resolve"
    print("ok  merging entities preserves every event link")


def test_unplaced_goes_to_the_holding_section() -> None:
    with tmp() as t:
        t.seed()
        t.st.add_event("something with no date at all")
        book = R.compile_book(t.st, do_ground=False)
        titles = [ch["title"] for ch in book["chapters"]]
        assert "Not yet placed" in titles, titles
        holding = next(ch for ch in book["chapters"]
                       if ch["title"] == "Not yet placed")
        assert len(holding["events"]) == 1
        # And it must be last: an undated memory does not open the book.
        assert titles[-1] == "Not yet placed", titles
    print("ok  undated events render into a holding section, last")


def test_snapshot_equals_replay() -> None:
    """The solved closure is cached so an evicted store can be reopened
    without replaying every constraint. If the cached matrix ever differed
    from what a replay produces, every bound and every ordering downstream
    would be quietly wrong, so this is the property that licenses the whole
    optimisation."""
    with tmp() as t:
        f, w, p, m = t.seed()
        # Past the point threshold, or no snapshot is written at all.
        evs = []
        base = days("1990-01-01")
        for i in range(120):
            ev = t.st.add_event(f"filler {i}", from_fragment=f)
            evs.append(ev)
            t.st.assert_constraint(ev.s, 0, base + i * 40, base + i * 40 + 200,
                                   PROV_ABSOLUTE, f)
            if i:
                t.st.assert_constraint(ev.s, evs[i - 1].e, 1.0, 400.0,
                                       PROV_STATED, f)
        assert t.st.tl._n >= S.Store.SNAPSHOT_MIN_POINTS, t.st.tl._n
        path = t.st.path
        t.st.close()

        snap_store = S.Store(path)
        from_snapshot = snap_store.tl.D.copy()
        row = snap_store.db.execute(
            "SELECT n_points FROM solver_snapshot WHERE id = 1").fetchone()
        assert row is not None, "no snapshot was written"

        # Drop it and reopen without Store.close(), which would write it back.
        snap_store.db.execute("DELETE FROM solver_snapshot")
        snap_store.db.commit()
        snap_store.db.close()

        replay_store = S.Store(path)
        from_replay = replay_store.tl.D.copy()
        assert np.array_equal(from_snapshot, from_replay), \
            "the cached closure is not what a replay produces"
        assert len(replay_store.tl.constraints) == len(snap_store.tl.constraints)
        replay_store.db.close()
        t.st = S.Store(path)
    print("ok  a restored closure is bit-identical to a replay")


def test_a_stale_snapshot_is_ignored() -> None:
    """A snapshot that no longer matches the constraint rows must never be
    adopted. Rather than trusting a counter, the fingerprint is over the live
    rows themselves."""
    with tmp() as t:
        f, w, p, m = t.seed()
        base = days("1990-01-01")
        evs = []
        for i in range(120):
            ev = t.st.add_event(f"filler {i}", from_fragment=f)
            evs.append(ev)
            t.st.assert_constraint(ev.s, 0, base + i * 40, base + i * 40 + 200,
                                   PROV_ABSOLUTE, f)
        path = t.st.path
        t.st.close()

        # Tamper: keep the snapshot, change the constraint set underneath it.
        con = S.Store(path)
        fp_before = con._fingerprint()
        con.db.execute("UPDATE constraints SET retracted = 1 "
                       "WHERE id = (SELECT MAX(id) FROM constraints)")
        con.db.commit()
        assert con._fingerprint() != fp_before, "fingerprint did not move"
        assert con._snapshot_for(con.tl._n) is None, \
            "a stale snapshot was accepted"
        con.db.close()
        t.st = S.Store(path)
    print("ok  a snapshot that no longer matches its constraints is ignored")


def test_cache_key_ignores_jitter_but_not_meaning() -> None:
    """Bounds move every time a constraint lands. Only a change big enough to
    alter what the prose can say should force a re-render."""
    with tmp() as t:
        f, w, p, m = t.seed()
        ids = [w.id]
        k1 = R.event_set_hash(t.st.tl, ids)

        # Tighten the wedding by a fortnight: same year, same prose.
        t.st.assert_constraint(w.s, 0, days("1986-04-10"), days("1986-04-30"),
                               PROV_ABSOLUTE, f)
        assert R.event_set_hash(t.st.tl, ids) == k1, "sub-year jitter busted cache"

        t.st.tl.events[w.id].summary = "my wedding in the rain"
        assert R.event_set_hash(t.st.tl, ids) != k1, "a changed summary was ignored"
    print("ok  cache key tracks meaning, not bound jitter")


if __name__ == "__main__":
    test_reload_is_the_same_network()
    test_old_database_migrates()
    test_recompile_is_a_noop()
    test_new_fact_reaches_the_manuscript()
    test_frozen_is_never_rewritten()
    test_unsupported_never_ships()
    test_stored_verdicts_round_trip()
    test_conflict_is_recorded_not_raised()
    test_merge_loses_no_links()
    test_unplaced_goes_to_the_holding_section()
    test_snapshot_equals_replay()
    test_a_stale_snapshot_is_ignored()
    test_cache_key_ignores_jitter_but_not_meaning()
    print("\nall pipeline properties hold")
