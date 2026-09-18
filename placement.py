"""
Turning an under-constrained event into the smallest number of questions.

Principle: never ask about an event, ask about the loosest *pair*. After each
answer the network propagates, so one answer can resolve many pairs at once.
Asking against a pivot near the median of the unknown set makes the number of
questions logarithmic in the number of anchors rather than linear.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from timeline import PROV_USER_PLACED, YEAR, Event, Timeline, as_date

# Answer kinds an option can carry.
BEFORE, AFTER, DURING, COINCIDENT, UNSURE = (
    "before", "after", "during", "coincident", "unsure")


@dataclass
class Option:
    kind: str
    anchor_id: str | None
    label: str


@dataclass
class Question:
    event_id: str
    prompt: str
    options: list[Option]
    guess: int | None = None     # index into options
    rationale: str = ""


def salience(tl: Timeline, ev: Event) -> float:
    """How good an anchor is: tightly dated and often referenced.
    A vague event makes a terrible pivot even if it sits at the median."""
    slack_years = tl.slack(ev.id) / YEAR
    return ev.mentions / (1.0 + slack_years)


def anchors(tl: Timeline, target: str, max_slack_years: float = 1.5,
            exclude: frozenset[str] = frozenset()) -> list[Event]:
    """Placed events whose order relative to the target is not yet provable.
    `exclude` holds anchors already asked about: an 'around the same time'
    answer leaves the pair overlapping, so without this the search would ask
    about the same anchor forever."""
    out = [ev for ev in tl.events.values()
           if ev.id != target
           and ev.id not in exclude
           and tl.slack(ev.id) <= max_slack_years * YEAR
           and tl.relation(target, ev.id) == "unknown"]
    out.sort(key=lambda ev: tl.bounds(ev.s)[0])
    return out


def _pivot(tl: Timeline, cands: list[Event]) -> Event:
    """Most memorable candidate in the middle third. Median position keeps the
    search logarithmic; salience keeps the question answerable."""
    n = len(cands)
    if n <= 2:
        return max(cands, key=lambda ev: salience(tl, ev))
    mid = n // 2
    lo, hi = n // 3, max(n // 3 + 1, (2 * n) // 3)
    window = list(range(lo, hi)) or [mid]
    # Salience first, then closeness to the median. Equal-salience anchors give
    # a true 1/2 split; a standout anchor is worth an off-median split, but only
    # inside the middle third, which bounds the damage to the search.
    best = max(window, key=lambda i: (salience(tl, cands[i]), -abs(i - mid)))
    return cands[best]


def next_question(tl: Timeline, event_id: str,
                  tolerance_days: float = 2 * YEAR,
                  exclude: frozenset[str] = frozenset()) -> Question | None:
    """None when the event is placed tightly enough, or when no anchor is left
    whose order we cannot already prove."""
    if tl.slack(event_id) <= tolerance_days:
        return None
    cands = anchors(tl, event_id, exclude=exclude)
    if not cands:
        return None

    piv = _pivot(tl, cands)
    lo, hi = tl.bounds(piv.s)
    when = as_date(lo).year
    label = piv.summary or piv.id
    opts = [
        Option(BEFORE, piv.id, f"Before {label}"),
        Option(DURING, piv.id, f"Around the same time as {label}"),
        Option(AFTER, piv.id, f"After {label}"),
        Option(UNSURE, None, "Not sure"),
    ]
    ev = tl.events[event_id]
    guess, why = _guess(tl, ev, piv)
    return Question(
        event_id=event_id,
        prompt=f"Where does \u201c{ev.summary or ev.id}\u201d sit relative to "
               f"{label} ({when})?",
        options=opts,
        guess=guess,
        rationale=why,
    )


def _guess(tl: Timeline, ev: Event, piv: Event) -> tuple[int | None, str]:
    """Weak prior from entity overlap and from whatever bounds already exist.
    Shown to the user so answering is confirmation rather than recall."""
    shared = ev.entities & piv.entities
    e_lo, _ = tl.bounds(ev.s)
    p_lo, _ = tl.bounds(piv.s)
    if shared:
        return 1, f"both mention {', '.join(sorted(shared))}"
    if e_lo < p_lo - YEAR:
        return 0, "current lower bound sits earlier"
    if e_lo > p_lo + YEAR:
        return 2, "current lower bound sits later"
    return None, ""


def apply_answer(tl: Timeline, q: Question, choice: int, source: str = "") -> bool:
    """Returns False for 'not sure', which leaves the event floating rather
    than guessing. A floating event renders into a holding section."""
    opt = q.options[choice]
    kw = dict(provenance=PROV_USER_PLACED, source=source, note="user placement")
    if opt.kind == BEFORE:
        tl.before(q.event_id, opt.anchor_id, **kw)
    elif opt.kind == AFTER:
        tl.after(q.event_id, opt.anchor_id, **kw)
    elif opt.kind == DURING:
        a, b = tl.events[q.event_id], tl.events[opt.anchor_id]
        tl.add(a.s, b.s, -YEAR / 2, YEAR / 2, **kw)
    elif opt.kind == COINCIDENT:
        a, b = tl.events[q.event_id], tl.events[opt.anchor_id]
        tl.add(a.s, b.s, -30.0, 30.0, **kw)
    else:
        return False
    return True


def question_budget(n_anchors: int) -> int:
    """Worst case for a fully unconstrained event."""
    return max(1, math.ceil(math.log2(max(n_anchors, 1) + 1)))


def place(tl: Timeline, event_id: str, oracle, max_questions: int = 12) -> int:
    """Drive the loop to completion. `oracle(question) -> int` is the user in
    production and a simulated answerer in tests. Returns questions asked."""
    asked, seen = 0, set()
    while asked < max_questions:
        q = next_question(tl, event_id, exclude=frozenset(seen))
        if q is None:
            break
        choice = oracle(q)
        asked += 1
        seen.add(q.options[0].anchor_id)
        if not apply_answer(tl, q, choice):
            break
    return asked
