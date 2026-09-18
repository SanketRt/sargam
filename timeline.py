"""
Simple Temporal Network over memoir events.

Model
-----
Every event owns two time points, s (start) and e (end), measured in days
relative to a single reference point Z (index 0, value 0 by definition).

Every temporal statement reduces to one primitive:

    x - y in [lo, hi]

encoded in a distance graph as two edges:

    y -> x  weight  hi      (x - y <= hi)
    x -> y  weight -lo      (y - x <= -lo, i.e. x - y >= lo)

D[a][b] = shortest path a -> b = tightest provable upper bound on (b - a).
The network is consistent iff the distance graph has no negative cycle,
detected as D[i][i] < 0 for some i.

Consequences used elsewhere:
  bounds(p)          = (-D[p][Z], D[Z][p])
  A strictly before B  iff  D[s_B][e_A] <= 0
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Iterable

import numpy as np

INF = np.inf
EPOCH = _dt.date(1900, 1, 1)
YEAR = 365.2425

# Provenance tiers. Higher wins when a contradiction must be resolved.
PROV_INFERRED = 0      # model guess from lexical cues
PROV_STATED = 1        # extracted from something the user said
PROV_USER_PLACED = 2   # user answered a placement question
PROV_ABSOLUTE = 3      # explicit calendar date


def days(d: _dt.date | str) -> float:
    """Calendar date -> reference-relative days."""
    if isinstance(d, str):
        d = _dt.date.fromisoformat(d)
    return float((d - EPOCH).days)


def as_date(x: float) -> _dt.date:
    return EPOCH + _dt.timedelta(days=float(x))


def fmt(lo: float, hi: float) -> str:
    """Readable bound. Open on one side means the event is only ordered, not
    dated, which is a perfectly valid state to render from."""
    if lo == -INF and hi == INF:
        return "unplaced"
    if lo == -INF:
        return f"before {as_date(hi)}"
    if hi == INF:
        return f"after {as_date(lo)}"
    return f"{as_date(lo)} .. {as_date(hi)}"


@dataclass(frozen=True)
class Constraint:
    x: int
    y: int
    lo: float
    hi: float
    provenance: int = PROV_STATED
    source: str = ""          # fragment id
    note: str = ""


@dataclass
class Event:
    id: str
    summary: str
    s: int                     # point index of start
    e: int                     # point index of end
    entities: set[str] = field(default_factory=set)
    mentions: int = 1          # how often referenced, feeds anchor salience


class Inconsistent(Exception):
    def __init__(self, constraint: Constraint, culprits: list[Constraint]):
        self.constraint = constraint
        self.culprits = culprits
        super().__init__(
            f"constraint {constraint.x}-{constraint.y} in "
            f"[{constraint.lo}, {constraint.hi}] contradicts "
            f"{len(culprits)} existing constraint(s)"
        )


class Timeline:
    """Incrementally maintained all-pairs shortest path closure."""

    def __init__(self, capacity: int = 64):
        self._n = 1                                   # point 0 is Z
        self._cap = max(capacity, 1)
        self._D = np.full((self._cap, self._cap), INF)
        np.fill_diagonal(self._D, 0.0)
        self.constraints: list[Constraint] = []
        self.events: dict[str, Event] = {}
        self._point_owner: dict[int, tuple[str, str]] = {}

    # ---------------------------------------------------------------- points

    @property
    def D(self) -> np.ndarray:
        return self._D[: self._n, : self._n]

    def _new_point(self) -> int:
        if self._n == self._cap:
            self._grow()
        p = self._n
        self._n += 1
        return p

    def _grow(self) -> None:
        cap = self._cap * 2
        D = np.full((cap, cap), INF)
        D[: self._cap, : self._cap] = self._D
        np.fill_diagonal(D, 0.0)
        self._D, self._cap = D, cap

    # ---------------------------------------------------------------- events

    def add_event(self, id: str, summary: str = "", entities: Iterable[str] = (),
                  max_duration_days: float = 30 * YEAR,
                  restore: bool = False) -> Event:
        """`restore=True` allocates the points but skips the well-formedness
        edge. Used when reloading from the store, where that constraint is
        already in the constraint table and gets replayed with the rest."""
        if id in self.events:
            raise KeyError(f"duplicate event id {id!r}")
        s, e = self._new_point(), self._new_point()
        ev = Event(id=id, summary=summary, s=s, e=e, entities=set(entities))
        self.events[id] = ev
        self._point_owner[s] = (id, "s")
        self._point_owner[e] = (id, "e")
        if not restore:
            # e - s in [0, max_duration]: an event does not end before it starts.
            self.add(e, s, 0.0, max_duration_days,
                     provenance=PROV_ABSOLUTE, note="well-formedness")
        return ev

    # ----------------------------------------------------------- constraints

    def add(self, x: int, y: int, lo: float, hi: float,
            provenance: int = PROV_STATED, source: str = "",
            note: str = "", check: bool = True) -> Constraint:
        """Assert x - y in [lo, hi]. Raises Inconsistent and changes nothing
        if the assertion contradicts the existing network.

        `check=False` skips the snapshot-and-verify, which is the dominant
        per-constraint cost. Only for replaying a stored network that was
        already proved consistent when each constraint was first accepted."""
        if lo > hi:
            raise ValueError("lo > hi")
        c = Constraint(x, y, lo, hi, provenance, source, note)
        if not check:
            self._relax(y, x, hi)
            self._relax(x, y, -lo)
            self.constraints.append(c)
            return c
        snapshot = self._D[: self._n, : self._n].copy()
        self._relax(y, x, hi)
        self._relax(x, y, -lo)
        bad = np.diagonal(self.D) < -1e-9
        if bad.any():
            self._D[: self._n, : self._n] = snapshot
            raise Inconsistent(c, self._blame(c))
        self.constraints.append(c)
        return c

    def _relax(self, u: int, v: int, w: float) -> None:
        """Insert edge u->v of weight w into an already closed matrix.
        Any new shortest path must pass through this edge, so one O(n^2)
        pass over i -> u -> v -> j suffices."""
        if w >= self._D[u, v]:
            return
        D = self._D[: self._n, : self._n]
        D[u, v] = w
        cand = D[:, u, None] + w + D[None, v, :]
        np.minimum(D, cand, out=D)

    def rebuild(self) -> None:
        """Full Floyd-Warshall from the constraint list. Use after removing or
        relaxing a constraint, where incremental tightening does not apply."""
        n = self._n
        D = np.full((n, n), INF)
        np.fill_diagonal(D, 0.0)
        for c in self.constraints:
            D[c.y, c.x] = min(D[c.y, c.x], c.hi)
            D[c.x, c.y] = min(D[c.x, c.y], -c.lo)
        for k in range(n):
            np.minimum(D, D[:, k, None] + D[None, k, :], out=D)
        self._D[:n, :n] = D

    def retract(self, c: Constraint) -> None:
        self.constraints.remove(c)
        self.rebuild()

    def _blame(self, c: Constraint) -> list[Constraint]:
        """Constraints on the conflicting path, lowest provenance first, so the
        caller can offer the user a sensible thing to drop."""
        touching = [k for k in self.constraints
                    if {k.x, k.y} & {c.x, c.y}
                    or self.D[c.x, k.x] < INF or self.D[c.y, k.y] < INF]
        return sorted(touching, key=lambda k: (k.provenance, -abs(k.hi - k.lo)))[:5]

    # ----------------------------------------------------------- queries

    def bounds(self, p: int) -> tuple[float, float]:
        """Tightest provable [lo, hi] for point p in reference-relative days."""
        D = self.D
        return (-D[p, 0], D[0, p])

    def event_bounds(self, id: str) -> tuple[float, float]:
        """Bounds on the event's start point."""
        return self.bounds(self.events[id].s)

    def slack(self, id: str) -> float:
        lo, hi = self.event_bounds(id)
        return hi - lo

    def relation(self, a: str, b: str) -> str:
        """'before' | 'after' | 'unknown', provable orderings only."""
        A, B = self.events[a], self.events[b]
        D = self.D
        if D[B.s, A.e] <= 1e-9:
            return "before"
        if D[A.s, B.e] <= 1e-9:
            return "after"
        return "unknown"

    def order(self) -> list[Event]:
        """Events sorted by position. Ties broken on upper bound, then id, so
        the sort is deterministic under re-render."""
        return sorted(
            self.events.values(),
            key=lambda ev: (self.bounds(ev.s)[0], self.bounds(ev.s)[1], ev.id),
        )

    def unplaced(self, tolerance_days: float = 2 * YEAR) -> list[Event]:
        """Events whose position is loose enough to be worth a question."""
        return [ev for ev in self.events.values()
                if self.slack(ev.id) > tolerance_days]

    # ----------------------------------------------------- sugar for extraction

    def at(self, id: str, lo: _dt.date | str, hi: _dt.date | str, **kw):
        """Event happened somewhere inside [lo, hi]."""
        ev = self.events[id]
        self.add(ev.s, 0, days(lo), days(hi), **kw)
        self.add(ev.e, 0, days(lo), days(hi), **kw)

    def before(self, a: str, b: str, min_gap_days: float = 1.0, **kw):
        """a ends strictly before b starts. Strict, not weak: a user saying
        'before' means before, and weak inequalities would let a ring of
        orderings collapse to a single instant and escape cycle detection."""
        kw.setdefault("provenance", PROV_USER_PLACED)
        self.add(self.events[b].s, self.events[a].e, min_gap_days, INF, **kw)

    def after(self, a: str, b: str, **kw):
        self.before(b, a, **kw)

    def during(self, a: str, b: str, **kw):
        """a happens inside b."""
        kw.setdefault("provenance", PROV_USER_PLACED)
        A, B = self.events[a], self.events[b]
        self.add(A.s, B.s, 0.0, INF, **kw)
        self.add(B.e, A.e, 0.0, INF, **kw)

    def gap(self, a: str, b: str, lo_years: float, hi_years: float, **kw):
        """b starts between lo and hi years after a ends."""
        self.add(self.events[b].s, self.events[a].e,
                 lo_years * YEAR, hi_years * YEAR, **kw)
