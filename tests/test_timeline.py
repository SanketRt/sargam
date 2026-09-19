"""
Stress tests. Run: python test_timeline.py

The properties worth guarding are not "does it produce a book" but:
  1. incremental closure never disagrees with a full recompute
  2. narration order does not affect reconstructed chronology
  3. contradictions are caught and change no state
  4. repeated input is a no-op
  5. placement cost stays logarithmic in the anchor count
"""


from __future__ import annotations

import pathlib as _pathlib
import sys as _sys
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parents[1] / "src"))

import random

import numpy as np

from sargam import placement as P
from sargam.timeline import (INF, PROV_ABSOLUTE, PROV_STATED, YEAR, Inconsistent,
                      Timeline, days)


# --------------------------------------------------------------- helpers

def kendall_tau(a: list, b: list) -> float:
    idx = {x: i for i, x in enumerate(b)}
    r = [idx[x] for x in a]
    n = len(r)
    if n < 2:
        return 1.0
    conc = disc = 0
    for i in range(n):
        for j in range(i + 1, n):
            if r[i] < r[j]:
                conc += 1
            else:
                disc += 1
    return (conc - disc) / (n * (n - 1) / 2)


def synthetic_life(n: int, seed: int) -> list[tuple[str, float]]:
    """n events with strictly increasing true dates, 6 months to 3 years apart."""
    rng = random.Random(seed)
    t = days("1975-03-01")
    out = []
    for i in range(n):
        t += rng.uniform(0.5, 3.0) * YEAR
        out.append((f"e{i:03d}", t))
    return out


def brute_closure(tl: Timeline) -> np.ndarray:
    n = tl.D.shape[0]
    D = np.full((n, n), INF)
    np.fill_diagonal(D, 0.0)
    for c in tl.constraints:
        D[c.y, c.x] = min(D[c.y, c.x], c.hi)
        D[c.x, c.y] = min(D[c.x, c.y], -c.lo)
    for k in range(n):
        np.minimum(D, D[:, k, None] + D[None, k, :], out=D)
    return D


# --------------------------------------------------------------- tests

def test_incremental_matches_full(trials: int = 40) -> None:
    """The O(n^2) single-edge relaxation must be exactly equivalent to a full
    O(n^3) recompute. This is the one invariant everything else rests on."""
    for seed in range(trials):
        rng = random.Random(seed)
        tl = Timeline()
        ids = [f"e{i}" for i in range(rng.randint(3, 10))]
        for i in ids:
            tl.add_event(i, i)
        for _ in range(rng.randint(5, 25)):
            a, b = rng.sample(ids, 2)
            lo = rng.uniform(-5, 5) * YEAR
            hi = lo + rng.uniform(0, 6) * YEAR
            try:
                tl.add(tl.events[a].s, tl.events[b].s, lo, hi)
            except Inconsistent:
                pass
        got, want = tl.D, brute_closure(tl)
        assert np.allclose(got, want, equal_nan=True), f"seed {seed} diverged"
    print(f"ok  incremental closure == full recompute        ({trials} seeds)")


def test_order_invariance(n: int = 60, trials: int = 8) -> None:
    """Same facts, different narration order, identical reconstruction."""
    for seed in range(trials):
        truth = synthetic_life(n, seed)
        order = [e for e, _ in truth]
        rng = random.Random(1000 + seed)

        facts = []
        for i, (eid, t) in enumerate(truth):
            if i % 5 == 0:                       # anchored by year
                facts.append(("abs", eid, t))
            if i > 0:                            # "about k years after the last"
                prev, pt = truth[i - 1]
                facts.append(("gap", prev, eid, (t - pt) / YEAR))
        rng.shuffle(facts)

        tl = Timeline()
        for eid, _ in truth:
            tl.add_event(eid, eid, max_duration_days=YEAR)
        for f in facts:
            if f[0] == "abs":
                _, eid, t = f
                tl.at(eid, str(__import__("datetime").date(1900, 1, 1)
                              + __import__("datetime").timedelta(days=t - 120)),
                      str(__import__("datetime").date(1900, 1, 1)
                          + __import__("datetime").timedelta(days=t + 120)),
                      provenance=PROV_ABSOLUTE)
            else:
                _, a, b, yrs = f
                tl.gap(a, b, yrs * 0.8, yrs * 1.2, provenance=PROV_STATED)

        got = [ev.id for ev in tl.order()]
        tau = kendall_tau(got, order)
        assert tau == 1.0, f"seed {seed}: tau={tau:.3f}"
    print(f"ok  reconstruction invariant to narration order  (tau=1.0, n={n})")


def test_contradiction_is_caught_and_atomic() -> None:
    tl = Timeline()
    for i in "abc":
        tl.add_event(i, i, max_duration_days=YEAR)
    tl.before("a", "b")
    tl.before("b", "c")
    snapshot = tl.D.copy()
    ncons = len(tl.constraints)
    try:
        tl.before("c", "a")          # closes the cycle
    except Inconsistent as exc:
        assert exc.culprits, "should name candidate constraints to drop"
    else:
        raise AssertionError("cycle not detected")
    assert np.allclose(tl.D, snapshot), "rejected constraint mutated state"
    assert len(tl.constraints) == ncons
    print("ok  contradiction detected, state unchanged")


def test_idempotence() -> None:
    tl = Timeline()
    tl.add_event("a", "a")
    tl.add_event("b", "b")
    tl.gap("a", "b", 1, 2)
    first = tl.D.copy()
    for _ in range(5):
        tl.gap("a", "b", 1, 2)
    assert np.allclose(tl.D, first), "repeated identical fact moved the bounds"
    print("ok  repeated identical constraints are a no-op")


def test_placement_budget(n: int = 64, trials: int = 20) -> None:
    """A fully unconstrained event must cost O(log n) questions, and the mean
    should sit well under the worst case because transitivity resolves pairs
    the search never has to ask about."""
    budget = P.question_budget(n)
    costs = []
    for seed in range(trials):
        truth = dict(synthetic_life(n, seed))
        tl = Timeline()
        for eid, t in truth.items():
            tl.add_event(eid, eid, max_duration_days=30.0)
            tl.at(eid,
                  str(__import__("datetime").date(1900, 1, 1)
                      + __import__("datetime").timedelta(days=t - 30)),
                  str(__import__("datetime").date(1900, 1, 1)
                      + __import__("datetime").timedelta(days=t + 30)),
                  provenance=PROV_ABSOLUTE)

        vals = list(truth.values())
        # Drop it into the widest gap, so a strict ordering is recoverable at
        # all. Land it inside a narrow gap and the honest answer is "around the
        # same time", which leaves the pair genuinely unordered by design.
        k = max(range(len(vals) - 1), key=lambda i: vals[i + 1] - vals[i])
        target_t = (vals[k] + vals[k + 1]) / 2
        tl.add_event("new", "the unplaced memory", max_duration_days=30.0)
        truth["new"] = target_t

        def oracle(q: P.Question) -> int:
            piv = q.options[0].anchor_id
            d = (truth["new"] - truth[piv]) / YEAR
            if abs(d) < 0.5:
                return 1
            return 0 if d < 0 else 2

        asked = P.place(tl, "new", oracle, max_questions=budget + 2)
        assert asked <= budget, f"seed {seed}: {asked} questions > budget {budget}"
        costs.append(asked)

        placed = [ev.id for ev in tl.order()]
        want = [k for k, _ in sorted(truth.items(), key=lambda kv: kv[1])]
        assert kendall_tau(placed, want) == 1.0, f"seed {seed}: misplaced"
    print(f"ok  placement cost mean={sum(costs)/len(costs):.1f} "
          f"max={max(costs)} budget={budget} (n={n} anchors)")


def test_user_placement_outranks_inference() -> None:
    """Provenance ordering must let the blame list surface the weak constraint
    first, so the UI offers to drop the guess rather than the user's answer."""
    tl = Timeline()
    for i in "ab":
        tl.add_event(i, i, max_duration_days=YEAR)
    weak = tl.add(tl.events["b"].s, tl.events["a"].s, 2 * YEAR, 4 * YEAR,
                  provenance=0, note="model guess")
    try:
        tl.add(tl.events["a"].s, tl.events["b"].s, 2 * YEAR, 4 * YEAR,
               provenance=2, note="user said the opposite")
    except Inconsistent as exc:
        assert exc.culprits[0] is weak or exc.culprits[0].provenance == 0
        print("ok  blame list surfaces weakest-provenance constraint first")
        return
    raise AssertionError("opposing constraints were not detected")


if __name__ == "__main__":
    test_incremental_matches_full()
    test_order_invariance()
    test_contradiction_is_caught_and_atomic()
    test_idempotence()
    test_placement_budget()
    test_user_placement_outranks_inference()
    print("\nall properties hold")
