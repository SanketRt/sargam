# sargam

Personal memoir engine. Chat in, chronologically ordered book out.

The manuscript is a **derived artifact**. The source of truth is an append-only
fragment log plus a temporal constraint network over extracted events. Nothing
is ever rewritten in place; chapters are recompiled from events.

```
sargam init
sargam add "I got married in April 1986. We moved to Pune about two years before."
sargam ask                     # answer the questions it cannot infer
sargam compile                 # markdown into a git repo, one commit per compile
sargam web                     # review UI on localhost:7000
```

`sargam web` runs on the standard library. `sargam serve` runs the same
handlers under the server that gets deployed, and needs the hosted extras:
`python -m venv .venv && .venv/bin/pip install -r requirements.txt`.

No dependencies beyond `numpy`. The model is optional: without a credential
everything still runs on a rule-based backend (see **Backends**).

Put `sargam` on your PATH (`ln -s "$PWD/sargam" ~/.local/bin/sargam`), or call
it as `python3 cli.py <command>` from this directory.

Start putting real material in from the first day. Fragments are append-only
and the store migrates itself, so nothing captured early is lost when the
schema downstream changes.

## What is here

| file | what it does |
|---|---|
| `timeline.py` | STN: incremental all-pairs closure, bounds, provable ordering, atomic contradiction rejection |
| `placement.py` | anchor salience, binary-search question generation, answer application |
| `store.py` | SQLite persistence, point-index stability, in-place schema migration |
| `extract.py` | text -> candidate events and constraints; api and offline backends |
| `entities.py` | alias resolution, merges, referring expressions as questions |
| `render.py` | chapter segmentation, paragraph compilation, the render cache |
| `ground.py` | per-sentence anti-fabrication verdicts |
| `publish.py` | markdown out, one git commit per compile |
| `workspace.py` | per-user paths and the user-id check |
| `cli.py` | the command surface |
| `api.py` | the review UI's handlers, with no transport in them |
| `web.py` | local transport, standard library only |
| `server.py` | hosted transport, FastAPI |
| `schema.sql` | the store, including the paragraph build graph |
| `demo.py` | runnable walkthrough, capture through to a committed manuscript |
| `test_timeline.py`, `test_pipeline.py`, `test_workspace.py`, `test_server.py` | the properties worth guarding |

```
python demo.py
python test_timeline.py
python test_pipeline.py
python test_workspace.py
python test_server.py      # skips unless the hosted extras are installed
```

## Two transports, one product

The review UI's logic lives in `api.py` and has no transport in it. A handler
takes `(ctx, body)` and returns a dict; `ctx` carries the store, the
manuscript path, and the caller's own API key. Two thin adapters sit over it:
`web.py`, standard library, one store, one lock, for local use; and
`server.py`, FastAPI, a store resolved per request, for hosting.

The local tool therefore never acquires a web-framework dependency, and the
hosted app cannot drift into a second implementation of the same product.
A test asserts the two transports return identical state from one store.

Behind a reverse proxy the browser keeps a path prefix the app never sees, so
the page is served by `api.page(base)` and builds every URL from that prefix.
A page with absolute paths works in development and breaks the first time it
is proxied, which is the worst order to discover it.

## How it works

Every event owns two time points, start and end, in days from a fixed epoch.
Every temporal statement — "April 1986", "about two years before the wedding",
"after the mill job" — reduces to one primitive, `x - y in [lo, hi]`, encoded
as two edges in a distance graph. `D[a][b]` is the tightest provable upper
bound on `b - a`; the network is consistent exactly when no diagonal entry
goes negative.

Adding a fact does not re-run Floyd–Warshall. The matrix is already closed, so
any new shortest path must pass through the new edge and a single `i -> u ->
v -> j` pass suffices: O(n²) per constraint instead of O(n³). Test 1 below
exists solely to guard that equivalence.

Placement never asks about an event, it asks about the loosest **pair**,
against a pivot near the median of the unresolved anchors. Each answer
propagates, so transitivity resolves pairs the search never has to ask about.
Cost is logarithmic in the anchor count, not linear.

Rendering is a pure function of `(events, style)`. Each paragraph records what
it was derived from, a `style_hash`, a content hash of its events, and a
`frozen` flag.

## Determinism, and why the cache is load-bearing

Reproducibility cannot come from `temperature=0`: current models removed the
parameter and reject any request that sends it. So the render cache is not an
optimisation, it is the correctness mechanism: a
paragraph is regenerated only when its inputs actually changed, which is what
makes `git diff` after a recompile mean *the model changed its mind* rather
than *the model was sampled again*.

Two details make it hold up:

* The cache key uses the **year-level** date string, not raw bounds. Every new
  constraint nudges the bounds slightly; only a change big enough to alter what
  the prose can say should force a re-render.
* A paragraph is keyed on its **earliest event**, not its whole event set.
  Keying on the set would mint a new id every time an event joined one, losing
  the freeze flag at exactly the moment it matters — when the sources moved
  under prose you had approved.

A frozen paragraph whose sources move is flagged for review and left byte-for-
byte alone. It is never silently rewritten.

## Grounding

Every rendered paragraph is checked sentence by sentence against the fragments
it came from. `supported` means the claim is in a source; `inferred` means it
is a reasonable reading but not stated; `unsupported` means it is not
derivable at all. Names, dates, places, numbers and quoted speech are
`supported` or they are `unsupported` — there is no middle verdict for a fact
of that kind.

`unsupported` sentences are stripped before the manuscript is written. That
asymmetry is deliberate: a memoir missing a sentence is recoverable, a memoir
containing a fact you never said is not. The review UI marks `inferred`
sentences so you can see where the model reached.

## Backends

| | |
|---|---|
| `api` | `pip install anthropic` plus a credential. Model defaults to `claude-opus-5`; override with `SARGAM_MODEL`. Extraction and grounding use structured outputs, so responses cannot fail to parse. |
| `offline` | No network. Explicit dates and a few relative phrasings only; everything else goes to the unresolved queue. |

Chosen automatically; force with `SARGAM_BACKEND=offline`. `--style` only
affects the `api` backend -- the offline renderer has one voice.

Every model entry point also takes an explicit `api_key`, so a caller can
supply its own credential per call rather than relying on the process
environment. `SARGAM_BACKEND=offline` still overrides it, which is what makes
a deployment able to guarantee no outbound calls.

The offline extractor is deliberately narrow, and it refuses to guess. Given
*"the flood came one year later than the wedding"* it will not anchor that to
the previous sentence — it cannot resolve which event "the wedding" is, so the
phrase goes to the unresolved queue and the event stays unplaced. Producing a
confidently wrong date would be exactly the fabrication the rest of the design
is built to prevent.

## Invariants under test

`test_timeline.py` — the solver:

1. Incremental O(n²) edge relaxation is exactly equivalent to a full O(n³)
   recompute. Everything else rests on this.
2. Narration order does not affect reconstructed chronology (Kendall tau = 1).
3. A contradicting constraint is rejected and mutates no state.
4. Repeating an identical fact is a no-op.
5. Placing an unconstrained event into n anchors costs at most
   ceil(log2(n+1)) questions.

`test_pipeline.py` — everything built on it:

6. A reloaded network is the same network; point indices survive.
7. An older database migrates in place without losing anything.
8. Recompiling with nothing changed renders nothing and commits nothing.
9. A changed date does invalidate the cache.
10. A frozen paragraph is flagged, never rewritten.
11. An unsupported sentence never reaches the manuscript, including through a
    store round-trip.
12. A contradiction is logged as evidence and changes nothing.
13. Merging two entities loses no event link.
14. An undated event renders into a holding section, last.
15. The cache key tracks meaning, not bound jitter.

`test_workspace.py` — per-user isolation and per-request credentials:

16. A user id can never address a directory outside the data root.
17. One user's store cannot read another's.
18. The backend follows the caller's own credential; the deployment can veto.
19. The key reaches every model call site.
20. The key is never written to disk and never enters a cache key.

`test_server.py` — the hosted transport (skipped without the extras):

21. The stdlib and FastAPI transports return the same state.
22. Each request sees only its own workspace.
23. An unauthenticated request is refused with no data.
24. A handler failure returns no server internals.
25. The page builds every URL from the prefix it was served under.

## Known limits

* **The dense matrix has a ceiling.** `n` events means a `(2n+1)²` float64
  matrix plus a copy per constraint added: about 32 MB at 1,000 events, 800 MB
  at 5,000. Fine for a personal memoir, fatal past a few thousand events. The
  fix, when it is needed, is a sparse or banded representation — the public
  API would not change.
* **Chapter segmentation cuts on temporal gaps only.** It does not know that
  two stretches of life belong together thematically.
* **The offline renderer is plain by construction.** It states the events in
  order and dates them only where the network proves a date. It is a
  legitimate manuscript, not a stub, but prose is what the model is for.
