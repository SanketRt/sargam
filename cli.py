"""
sargam -- personal memoir engine.

    sargam init                 create the store and the manuscript repo
    sargam add "..."            capture a fragment (also reads stdin, or -f)
    sargam ask                  answer placement and entity questions
    sargam timeline             the solved chronology
    sargam compile              recompile the manuscript and commit it
    sargam status               what is pending
    sargam review               frozen paragraphs whose sources moved
    sargam freeze <para_id>     approve a paragraph; never rewritten silently
    sargam conflicts            contradictions, newest first
    sargam entities             people and places, with merge suggestions
    sargam merge <keep> <drop>  fold one entity into another
    sargam log                  compile history
    sargam web                  the review UI on localhost

A project lives in .sargam/ (store.db + manuscript/) under the current
directory, or wherever SARGAM_HOME points.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys

import ask as A
import entities as E
import extract
import ground as G
import publish
import render as R
import store as S
from timeline import YEAR, fmt


def home() -> pathlib.Path:
    return pathlib.Path(os.environ.get("SARGAM_HOME", ".sargam")).resolve()


def db_path() -> pathlib.Path:
    return home() / "store.db"


def manuscript() -> pathlib.Path:
    return home() / "manuscript"


def open_store() -> S.Store:
    if not db_path().exists():
        sys.exit(f"no sargam project at {home()}. Run: sargam init")
    return S.Store(db_path())


def c(text: str, code: str) -> str:
    return text if not sys.stdout.isatty() else f"\033[{code}m{text}\033[0m"


dim = lambda s: c(s, "2")
bold = lambda s: c(s, "1")
green = lambda s: c(s, "32")
yellow = lambda s: c(s, "33")
red = lambda s: c(s, "31")


# ------------------------------------------------------------------ commands

def cmd_init(args) -> None:
    home().mkdir(parents=True, exist_ok=True)
    st = S.Store(db_path())
    st.close()
    publish.ensure_repo(manuscript())
    print(f"{green('initialised')} {home()}")
    print(f"  store       {db_path()}")
    print(f"  manuscript  {manuscript()} (git repo)")
    print(f"  backend     {extract.backend()}")
    print(f"\nStart dumping material in: sargam add \"...\"")


def cmd_add(args) -> None:
    if args.file:
        body = pathlib.Path(args.file).read_text()
    elif args.text:
        body = " ".join(args.text)
    elif not sys.stdin.isatty():
        body = sys.stdin.read()
    else:
        sys.exit("nothing to add: pass text, -f FILE, or pipe stdin")
    body = body.strip()
    if not body:
        sys.exit("empty fragment")

    with open_store() as st:
        fid = st.add_fragment(body)
        known = {e.id: e.summary for e in st.tl.events.values()}
        try:
            out = extract.extract(body, known)
        except Exception as exc:
            print(red(f"extraction failed: {exc}"))
            print(dim(f"fragment {fid} is saved; re-run `sargam reextract` later"))
            return
        rep = extract.apply(st, out, fid)
        E.sweep(st)
        E.harvest_referring(st)
        st.mark_dirty(set(rep["events"]))

        print(f"{green('+')} {len(rep['events'])} event(s), "
              f"{rep['landed']} constraint(s)  {dim(fid)}")
        for r in rep["rejected"]:
            print(f"  {yellow('!')} {r}")
        for u in rep["unresolved"]:
            print(f"  {dim('?')} unresolved: {u}")
        if rep["needs_placement"]:
            print(f"  {len(rep['needs_placement'])} need placement "
                  f"{dim('-> sargam ask')}")


def cmd_timeline(args) -> None:
    with open_store() as st:
        tl = st.tl
        if not tl.events:
            print(dim("no events yet"))
            return
        for ev in tl.order():
            lo, hi = tl.event_bounds(ev.id)
            slack_y = tl.slack(ev.id) / YEAR
            mark = yellow(" ~") if slack_y > 2 else "  "
            ents = dim("  " + ", ".join(sorted(ev.entities))) if ev.entities else ""
            print(f"{mark} {fmt(lo, hi):<26} {ev.summary}{ents}")
        print()
        print(dim(f"{len(tl.events)} events, {len(tl.constraints)} constraints; "
                  f"~ = loose enough to be worth a question"))


def cmd_ask(args) -> None:
    with open_store() as st:
        n = 0
        for kind, q in A.question_stream(st, limit=args.limit):
            if kind == "entity":
                n += _ask_entity(st, q)
            else:
                n += _ask_placement(st, q)
        if n == 0:
            print(green("nothing to ask -- everything is placed"))
        else:
            print(f"\n{green(str(n))} answer(s) recorded")


def _ask_placement(st, q) -> int:
    print(f"\n{bold(q.prompt)}")
    for i, o in enumerate(q.options):
        mark = dim("  <- guess") if q.guess == i else ""
        print(f"   {i}. {o.label}{mark}")
    if q.rationale:
        print(dim(f"      ({q.rationale})"))
    raw = input("   > ").strip()
    if raw in ("q", "quit", ""):
        raise SystemExit(0)
    try:
        choice = int(raw)
        opt = q.options[choice]
    except (ValueError, IndexError):
        print(yellow("   not an option, skipping"))
        return 0
    qid = st.record_question(q.event_id, q.prompt,
                             [{"kind": o.kind, "anchor": o.anchor_id,
                               "label": o.label} for o in q.options])
    st.record_answer(qid, choice)
    changed, msg = A.apply_placement(st, q, choice, question_id=qid)
    print(f"   {green(msg) if changed else yellow(msg)}")
    if changed:
        st.mark_dirty({q.event_id})
    return 1


def _ask_entity(st, q) -> int:
    print(f"\n{bold(q['prompt'])}")
    for i, o in enumerate(q["options"]):
        n = f" {dim('(' + str(o['n_events']) + ')')}" if o.get("n_events") else ""
        print(f"   {i}. {o['label']}{n}")
    raw = input("   > ").strip()
    if raw in ("q", "quit", ""):
        raise SystemExit(0)
    try:
        opt = q["options"][int(raw)]
    except (ValueError, IndexError):
        print(yellow("   not an option, skipping"))
        return 0
    new_name = None
    if opt["entity_id"] is None:
        new_name = input("   name: ").strip()
        if not new_name:
            return 0
    E.answer_entity(st, q["unresolved_id"], opt["entity_id"], new_name)
    print(f"   {green('bound')}")
    return 1


def cmd_compile(args) -> None:
    with open_store() as st:
        if not st.tl.events:
            sys.exit("nothing to compile yet")
        book = R.compile_book(st, style=args.style, do_ground=not args.no_ground)
        rep = publish.write(st, book, manuscript(), strip=not args.no_strip)
        sha = publish.commit(rep["repo"], args.message or
                             f"compile: {book['rendered']} rendered, "
                             f"{book['cached']} cached")
        st.record_compile(sha, book["rendered"], book["cached"], book["flagged"])

        print(f"{len(book['chapters'])} chapters, "
              f"{sum(len(ch['paragraphs']) for ch in book['chapters'])} paragraphs")
        print(f"  {book['cached']} cached {dim('(no-op)')}, "
              f"{book['rendered']} rendered")
        if rep["dropped"]:
            print(f"  {yellow(str(rep['dropped']))} unsupported sentence(s) dropped")
        if book["flagged"]:
            print(f"  {yellow(str(book['flagged']))} frozen paragraph(s) flagged "
                  f"{dim('-> sargam review')}")
        if sha:
            print(f"  committed {bold(sha)}")
        else:
            print(f"  {green('no change')} {dim('- the recompile was a no-op')}")
        print(dim(f"  {rep['repo']}"))


def cmd_status(args) -> None:
    with open_store() as st:
        p = A.pending(st)
        tl = st.tl
        print(f"{bold('events')}      {len(tl.events)}")
        print(f"{bold('constraints')} {len(tl.constraints)}")
        print(f"{bold('fragments')}   {len(st.fragments())}")
        print(f"{bold('paragraphs')}  {len(st.paragraphs())}")
        print()
        rows = [("unplaced events", p["placement"], "sargam ask"),
                ("entity questions", p["entity"], "sargam ask"),
                ("unresolved time refs", p["time"], "sargam ask"),
                ("open conflicts", p["conflicts"], "sargam conflicts"),
                ("flagged paragraphs", p["flagged"], "sargam review")]
        for label, n, hint in rows:
            col = yellow if n else green
            print(f"  {col(str(n).rjust(4))}  {label:<22} {dim(hint) if n else ''}")
        g = G.report(st)
        if any(g["counts"].values()):
            print(f"\n{bold('grounding')}   "
                  f"{green(str(g['counts']['supported']))} supported / "
                  f"{yellow(str(g['counts']['inferred']))} inferred / "
                  f"{red(str(g['counts']['unsupported']))} unsupported")
        print(f"\n{dim('backend     ' + extract.backend())}")


def cmd_review(args) -> None:
    with open_store() as st:
        rows = st.flagged()
        if not rows:
            print(green("nothing flagged"))
            return
        for r in rows:
            print(f"\n{bold(r['id'])}  {dim(r['chapter'])}")
            print(f"  {r['body']}")
            evs = json.loads(r["derived_from"])
            print(dim(f"  derived from: {', '.join(evs)}"))
            for v in st.groundings(r["id"]):
                if v["verdict"] != "supported":
                    print(f"  {yellow(v['verdict'])} sentence {v['sentence_ix']}")
        print(dim(f"\nunfreeze to let it recompile: sargam freeze --off <id>"))


def cmd_freeze(args) -> None:
    with open_store() as st:
        if st.paragraph(args.paragraph_id) is None:
            sys.exit(f"no paragraph {args.paragraph_id}")
        st.set_frozen(args.paragraph_id, not args.off)
        print(green("unfrozen" if args.off else "frozen"))


def cmd_conflicts(args) -> None:
    with open_store() as st:
        rows = st.conflicts(open_only=not args.all)
        if not rows:
            print(green("no conflicts"))
            return
        for r in rows:
            print(f"\n{bold('#' + str(r['id']))}  {dim(r['detected_at'])}"
                  f"  {dim(r['source'] or '')}")
            print(f"  rejected: points {r['x_point']}-{r['y_point']} "
                  f"in [{r['lo_days']:.0f}, {r['hi_days']:.0f}] days "
                  f"prov={r['provenance']}")
            for cand in json.loads(r["culprits"])[:3]:
                print(dim(f"    culprit prov={cand['prov']} {cand['note'] or ''}"))
        print(dim("\nboth readings are kept; nothing was silently overwritten"))


def cmd_entities(args) -> None:
    with open_store() as st:
        for e in st.entities():
            al = json.loads(e["aliases"])
            extra = dim("  aka " + ", ".join(al)) if al else ""
            print(f"  {str(e['n_events']).rjust(3)}  {e['name']}{extra}")
        sugg = E.suggest_merges(st)
        if sugg:
            print(f"\n{bold('possible duplicates')}")
            for s in sugg:
                print(f"  {s['keep_name']} ← {s['drop_name']}  "
                      f"{dim(s['why'])}")
                print(dim(f"    sargam merge {s['keep']} {s['drop']}"))


def cmd_merge(args) -> None:
    with open_store() as st:
        E.merge(st, args.keep, args.drop)
        print(green("merged"))


def cmd_log(args) -> None:
    with open_store() as st:
        rows = st.compiles()
        if not rows:
            print(dim("no compiles yet"))
            return
        for r in rows:
            sha = bold(r["commit_sha"]) if r["commit_sha"] else dim("no-op")
            print(f"  {r['compiled_at']}  {sha}  "
                  f"{r['n_rendered']} rendered, {r['n_cached']} cached, "
                  f"{r['n_flagged']} flagged")


def cmd_web(args) -> None:
    import web
    web.serve(db_path(), manuscript(), port=args.port, open_browser=not args.no_open)


# --------------------------------------------------------------------- parse

def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="sargam", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init").set_defaults(fn=cmd_init)

    a = sub.add_parser("add")
    a.add_argument("text", nargs="*")
    a.add_argument("-f", "--file")
    a.set_defaults(fn=cmd_add)

    sub.add_parser("timeline").set_defaults(fn=cmd_timeline)

    k = sub.add_parser("ask")
    k.add_argument("-n", "--limit", type=int, default=20)
    k.set_defaults(fn=cmd_ask)

    co = sub.add_parser("compile")
    co.add_argument("--style", choices=sorted(R.STYLES), default="plain")
    co.add_argument("-m", "--message")
    co.add_argument("--no-ground", action="store_true",
                    help="skip the anti-fabrication pass")
    co.add_argument("--no-strip", action="store_true",
                    help="keep sentences the grounder could not support")
    co.set_defaults(fn=cmd_compile)

    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("review").set_defaults(fn=cmd_review)

    f = sub.add_parser("freeze")
    f.add_argument("paragraph_id")
    f.add_argument("--off", action="store_true")
    f.set_defaults(fn=cmd_freeze)

    cf = sub.add_parser("conflicts")
    cf.add_argument("--all", action="store_true")
    cf.set_defaults(fn=cmd_conflicts)

    sub.add_parser("entities").set_defaults(fn=cmd_entities)

    m = sub.add_parser("merge")
    m.add_argument("keep")
    m.add_argument("drop")
    m.set_defaults(fn=cmd_merge)

    sub.add_parser("log").set_defaults(fn=cmd_log)

    w = sub.add_parser("web")
    w.add_argument("-p", "--port", type=int, default=7000)
    w.add_argument("--no-open", action="store_true")
    w.set_defaults(fn=cmd_web)

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        sys.exit(130)
