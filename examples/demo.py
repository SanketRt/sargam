"""
End-to-end walkthrough with a hand-written extraction, so you can see the loop
before any model is wired in. Run: python demo.py
"""


from __future__ import annotations

import pathlib as _pathlib
import sys as _sys
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parents[1] / "src"))

from sargam import placement as P
from sargam.timeline import (PROV_ABSOLUTE, PROV_STATED, YEAR, Inconsistent, Timeline,
                      days, fmt)

tl = Timeline()

# --- narrated in whatever order it came out, which is the whole point --------

tl.add_event("shop", "the argument with Ravi about the shop",
             entities={"Ravi", "Nagpur"})
tl.add_event("pune", "moving to Pune", entities={"father", "Pune"})
tl.add_event("wedding", "my wedding", entities={"Meera"})
tl.add_event("firstjob", "first job at the mill", entities={"Nagpur"})
tl.add_event("meera", "meeting Meera", entities={"Meera", "Pune"})
tl.add_event("flood", "the flood year", entities={"Pune"})

# "I got married in April 1986."
tl.at("wedding", "1986-04-01", "1986-04-30", provenance=PROV_ABSOLUTE)
# "We moved to Pune about two years before the wedding."
tl.gap("pune", "wedding", 1.5, 2.5, provenance=PROV_STATED)
# "I met Meera a few months after we moved."
tl.gap("pune", "meera", 0.2, 0.8, provenance=PROV_STATED)
# "The mill job was before all that, back in Nagpur."
tl.before("firstjob", "pune", provenance=PROV_STATED)
# "The flood was the year after we married."
tl.gap("wedding", "flood", 0.8, 1.6, provenance=PROV_STATED)

print("after extraction:")
for ev in tl.order():
    lo, hi = tl.event_bounds(ev.id)
    print(f"  {fmt(lo, hi):<26}  {ev.summary}")

# --- "shop" has no time reference at all, so ask ----------------------------

print(f"\nunplaced (slack > 2y): {[e.id for e in tl.unplaced()]}")

# Simulated you. In the app this is a tap on one of the options.
TRUTH = {"shop": 1979.5, "pune": 1984.2, "wedding": 1986.3,
         "firstjob": 1977.0, "meera": 1984.7, "flood": 1987.4}


def me(q: P.Question) -> int:
    anchor = q.options[0].anchor_id
    print(f"\n  Q: {q.prompt}")
    for i, o in enumerate(q.options):
        mark = "  <- guess" if q.guess == i else ""
        print(f"       {i}. {o.label}{mark}")
    if q.rationale:
        print(f"      ({q.rationale})")
    d = TRUTH[q.event_id] - TRUTH[anchor]
    choice = 1 if abs(d) < 0.5 else (0 if d < 0 else 2)
    print(f"  A: {q.options[choice].label}")
    return choice


asked = P.place(tl, "shop", me)
print(f"\nplaced in {asked} question(s). "
      f"worst case for {len(tl.events)-1} anchors was "
      f"{P.question_budget(len(tl.events)-1)}.\n")

for ev in tl.order():
    lo, hi = tl.event_bounds(ev.id)
    print(f"  {fmt(lo, hi):<26}  {ev.summary}")

# --- a later fragment that contradicts what is already known ---------------

print("\nnew fragment: \u201cthat argument happened after the wedding\u201d")
try:
    tl.after("shop", "wedding", provenance=PROV_STATED, source="frag_88")
except Inconsistent as exc:
    print("  rejected, network unchanged. candidates to drop, weakest first:")
    for c in exc.culprits[:3]:
        print(f"    prov={c.provenance}  {c.note or 'extracted'}  "
              f"[{c.lo/YEAR:.1f}y, {c.hi/YEAR:.1f}y]")
    print("  -> surface both readings, let the user keep one, log which.")


# --- compiling it into a book ----------------------------------------------
#
# Everything above is in-memory. This section replays the same life through
# the store and compiles it, which is the loop you actually run:
# capture -> solve -> ask -> compile -> commit.

import pathlib
import shutil
import tempfile

from sargam import publish
from sargam import render as R
from sargam import store as S

work = pathlib.Path(tempfile.mkdtemp(prefix="sargam-demo-"))
st = S.Store(work / "store.db")

frag = st.add_fragment(
    "I got married in April 1986. We moved to Pune about two years before the "
    "wedding, and I met Meera a few months after we moved. The mill job was "
    "before all that, back in Nagpur. The flood was the year after we married."
)
mk = {}
for eid, summary, ents in [
    ("pune", "moving to Pune", ["Pune"]),
    ("meera", "meeting Meera", ["Meera", "Pune"]),
    ("firstjob", "the first job at the mill", ["Nagpur"]),
    ("wedding", "my wedding", ["Meera"]),
    ("flood", "the flood year", ["Pune"]),
]:
    mk[eid] = st.add_event(summary, entities=ents, from_fragment=frag)

st.assert_constraint(mk["wedding"].s, 0, days("1986-04-01"), days("1986-04-30"),
                     PROV_ABSOLUTE, frag)
st.assert_constraint(mk["wedding"].e, 0, days("1986-04-01"), days("1986-04-30"),
                     PROV_ABSOLUTE, frag)
st.assert_constraint(mk["wedding"].s, mk["pune"].e, 1.5 * YEAR, 2.5 * YEAR,
                     PROV_STATED, frag)
st.assert_constraint(mk["meera"].s, mk["pune"].e, 0.2 * YEAR, 0.8 * YEAR,
                     PROV_STATED, frag)
st.assert_constraint(mk["pune"].s, mk["firstjob"].e, 1.0, float("inf"),
                     PROV_STATED, frag)
st.assert_constraint(mk["flood"].s, mk["wedding"].e, 0.8 * YEAR, 1.6 * YEAR,
                     PROV_STATED, frag)

book = R.compile_book(st, style="plain", do_ground=True)
rep = publish.write(st, book, work / "manuscript")
sha = publish.commit(rep["repo"], "first compile")

print("\n" + "=" * 68)
print(f"compiled {len(book['chapters'])} chapter(s), "
      f"{book['rendered']} rendered, {book['cached']} cached, "
      f"commit {sha}\n")
for name in rep["files"]:
    print((work / "manuscript" / name).read_text())

# The point of the cache: compiling again changes nothing at all.
book2 = R.compile_book(st, style="plain", do_ground=False)
rep2 = publish.write(st, book2, work / "manuscript")
sha2 = publish.commit(rep2["repo"], "second compile")
print(f"recompiled: {book2['rendered']} rendered, {book2['cached']} cached, "
      f"commit {sha2}")
print("-> a recompile with nothing changed is a zero-line diff, which is how\n"
      "   you tell a real change from model drift.")

st.close()
shutil.rmtree(work, ignore_errors=True)
