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

Put the CLI on your PATH with `ln -s "$PWD/bin/sargam" ~/.local/bin/sargam`,
or install the package with `pip install -e .`.

Start putting real material in from the first day. Fragments are append-only
and the store migrates itself, so nothing captured early is lost when the
schema downstream changes.

## What is here

```
src/sargam/
  timeline.py   STN: incremental all-pairs closure, provable ordering,
                O(1) consistency check, atomic rejection
  placement.py  anchor salience, binary-search question generation
  store.py      SQLite persistence, point-index stability, schema migration,
                solved-closure snapshot
  workspace.py  per-user paths and the user-id check
  accounts.py   identity, kept apart from anyone's material
  vault.py      sealing a user's own API credential at rest
  account_ops.py export, deletion, rate limits
  extract.py    text -> candidate events and constraints; api + offline
  entities.py   alias resolution, merges, referring expressions as questions
  render.py     chapter segmentation, paragraph compilation, the render cache
  ground.py     per-sentence anti-fabrication verdicts
  publish.py    markdown out, one git commit per compile
  ask.py        the human loop, wired to the store
  api.py        review-UI handlers, with no transport in them
  web.py        local transport, standard library only
  server.py     hosted transport, FastAPI, accounts and eviction
  cli.py        the command surface
  schema.sql    the store, including the paragraph build graph
bin/sargam      entry point, no install needed
tests/          plain scripts: timeline, pipeline, workspace, vault, server
examples/demo.py
```

```
python run_tests.py          every suite
python examples/demo.py      capture -> solve -> ask -> compile -> commit
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

Nor does it check for a negative cycle afterwards. The closure already proves
`x - y` lies in some interval, so a new assertion on that pair is satisfiable
exactly when its interval meets the proven one — two comparisons, decided
before anything is mutated. A contradiction therefore changes nothing because
nothing was changed, not because a copy was restored.

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
optimisation, it is the correctness mechanism. A paragraph is regenerated only
when its inputs actually changed, which is what makes `git diff` after a
recompile mean *the model changed its mind* rather than *the model was sampled
again*.

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
affects the `api` backend — the offline renderer has one voice.

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
26. Only the signed cookie decides whose workspace is served.
27. A forged or unsafe session cookie is refused.
28. Logout expires the session cookie.
29. The server refuses to serve accounts without a session secret.
30. Account ids are derived, so a hostile subject cannot escape.
31. Eviction caps open stores and loses nothing.
32. A store with a request in flight is skipped, not closed.
33. A restored closure is bit-identical to a replay.
34. A snapshot that no longer matches its constraints is ignored.

`test_vault.py` — credential storage (skipped without `cryptography`):

35. A sealed credential is not its plaintext and round-trips.
36. A ciphertext will not decrypt for a different account.
37. A wrong or absent master key fails closed.
38. The plaintext credential never reaches disk.
39. A credential row copied between accounts decrypts for neither.
40. Clearing a credential leaves no hint and no ciphertext.
41. An accounts database predating credentials migrates in place.
42. A stored credential reaches its owner alone and is never readable.
43. The page shares the site's theme, typeface and path prefix.
44. An export carries the fragments, in plain text.
45. Deleting an account removes the row and every file.
46. Compiles are rate limited without starving reads.

## Hosting

`server.py` serves accounts behind a reverse proxy. Identity is Google OIDC;
the session is a signed cookie carrying nothing but an opaque account id.

| variable | meaning |
|---|---|
| `SARGAM_SECRET` | session signing key. **Required** -- the server refuses to serve accounts without it rather than pick a default. |
| `SARGAM_PUBLIC_URL` | the address the browser uses, e.g. `https://example.com` |
| `SARGAM_BASE_PATH` | the prefix the proxy keeps, e.g. `/projects/sargam` |
| `SARGAM_DATA` | where per-user workspaces live |
| `SARGAM_ACCOUNTS` | the accounts database (defaults beside `SARGAM_DATA`) |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | OAuth credentials |
| `SARGAM_KEY_SECRET` | master key for sealing users' API credentials. Generate with `python -c 'from sargam import vault; print(vault.generate())'`. Without it the server runs but cannot store credentials. |
| `SARGAM_SINGLE` | run as one local user with no accounts |
| `SARGAM_MAX_OPEN` | how many stores stay loaded (default 24) |
| `SARGAM_IDLE_SECONDS` | close a store after this long unused (default 900) |

The redirect URI registered with Google must be the **public** one, prefix
included -- `SARGAM_PUBLIC_URL + SARGAM_BASE_PATH + /auth/callback`. The
server prints it via `server.redirect_uri()`.

Three things are deliberate rather than incidental:

* **The account id is derived from Google's subject, never taken from it.**
  It becomes a directory name, so it has to satisfy the id check by
  construction; and a stable identifier for a real person does not belong in
  filesystem paths or log lines.
* **The session cookie is scoped to `SARGAM_BASE_PATH`**, not `/`. A domain
  hosting several proxied projects would otherwise send this session to all
  of them.
* **Open stores are capped and evicted least-recently-used first.** A
  Timeline is a dense matrix in memory, roughly 1 MB per hundred events, so how
  many stay loaded is what decides the memory bill. Closing one writes its
  solved closure back, which is why reopening is cheap. Eviction skips a store
  with a request in flight rather than waiting on it.
* **No Google tokens are stored.** Nothing calls Google again after
  identifying the person, so keeping them would be holding a credential for
  no reason.

## Cost of the solver

**Adding a constraint is O(n²), and used to be much worse.** Consistency is
decided in O(1) before anything moves: the closure already proves `x - y` lies
in some interval, and a new assertion holds exactly when its own interval
meets that one. The previous scheme copied the whole distance matrix per
constraint so a rejected assertion could be rolled back — at 1,500 events that
is 72 MB per constraint, roughly 250 GB to build the network.

**Reopening a store reads the solved closure rather than replaying it.**

| events | build | open from snapshot | open by replay |
|---|---|---|---|
| 150 | 1.0 s | 6 ms | 109 ms |
| 300 | 1.7 s | 6 ms | 690 ms |
| 600 | 4.6 s | 20 ms | 6.7 s |

The snapshot is fingerprinted over the live constraint rows, so a stale one is
never adopted, and a test asserts it is bit-identical to a replay.

## Look

The interface borrows the visual language of the site it is proxied from
rather than inventing its own: the same typeface, neutral palette and token
names, radii and translucent sticky header, with every colour value taken from
that site's stylesheet unchanged. Two
things differ on purpose — the content column is wider, because a two-pane
app is not a reading column, and the three grounding verdicts get the only
hues on the page, kept low-chroma so they read as annotation.

The theme lives under the same `theme` key in `localStorage`. Served from the
same origin, that means whichever theme someone chose on the portfolio is the
one this opens in, and changing it here follows them back. The key name is a
contract, and there is a test that says so.

## Bring your own key

Each account supplies its own Anthropic credential. It is sealed with AES-GCM
under a master key that lives in the deployment's environment and never in the
database, so a stolen database file alone decrypts nothing.

The account id is bound in as associated data. That is the point of it: a
ciphertext lifted from one row and pasted into another fails to decrypt rather
than quietly handing one account's credential to another. A test does exactly
that and asserts it decrypts for neither.

Only a hint — the last four characters — is ever readable back. The key is
validated against the API before it is stored, so a mistyped one fails at
paste time rather than half way through a compile, and neither the key nor the
validation error is echoed to the client, because the error text can quote the
credential.

An account with no key still works: the timeline is solved, placement
questions are asked, the manuscript compiles. It just writes plainer prose.

## It stays yours

Two things exist because of what this stores, and both were written before
anyone trusted it with real material — retrofitting deletion onto a system
that never planned for it is how half-deleted accounts happen.

**Export** hands back a zip. The fragments go in as plain text, one file each,
because they are the only irreplaceable part: events, constraints and the
whole manuscript are derived from them. The structured layers go in as JSON
beside them, and the compiled chapters as markdown.

**Deletion** removes the account row and the workspace together. They are
separate databases on purpose, so both are removed explicitly, and the open
store is closed first — unlinking files the process still holds is how an
account ends up half gone. It asks for a typed confirmation phrase in the
request body rather than inferring intent from the HTTP method.

Compiles and credential checks are rate limited per account. That is not a
security boundary — a signed-in account is already identified — but one
enthusiastic loop should not be able to spend an afternoon of someone's API
credit before anyone notices.

## Deploying

The container is a plain Python image with one non-obvious requirement: **git
is installed in it**, because the manuscript *is* a git repository and every
compile shells out to it. An image without git builds fine and fails at the
first commit.

It runs as a non-root user, on **one worker and one machine, deliberately**.
Each worker would keep its own registry of open stores, and two of them
writing the same user's SQLite file is corruption waiting to happen.
Concurrency here is per-user locks inside a single process.

Nothing that identifies a particular deployment lives in this repository.
`fly.toml` carries only the shape of the machine; the domain, the path prefix
and every credential are set as secrets. Copy `deploy/fly.env.example` to
`deploy/fly.env` (git-ignored), fill it in, then:

```
flyctl launch --no-deploy
flyctl volumes create sargam_data --size 1

set -a; . deploy/fly.env; set +a
flyctl secrets set \
  SARGAM_PUBLIC_URL="$SARGAM_PUBLIC_URL" \
  SARGAM_BASE_PATH="$SARGAM_BASE_PATH" \
  SARGAM_SECRET="$SARGAM_SECRET" \
  SARGAM_KEY_SECRET="$SARGAM_KEY_SECRET" \
  GOOGLE_CLIENT_ID="$GOOGLE_CLIENT_ID" \
  GOOGLE_CLIENT_SECRET="$GOOGLE_CLIENT_SECRET"

flyctl deploy
```

To serve it under a path on an existing site, proxy it there. With Netlify,
in `_redirects`:

```
/projects/sargam/*   https://<your-app>.fly.dev/:splat   200!
```

and register this redirect URI with Google, exactly:

```
https://<your-domain>/projects/sargam/auth/callback
```

`SARGAM_BASE_PATH` is what the browser sees, not what the app receives. It is
used to build page URLs, to scope the session cookie to this app rather than
the whole domain, and to form that redirect URI. The app answers correctly
whether or not the proxy strips the prefix, so either `_redirects` style
works.

Losing `SARGAM_KEY_SECRET` does not lose anyone's memoir -- it makes their
stored API key undecryptable, and they paste a new one. Losing the volume
loses everything, so snapshot it.

## Known limits

* **The dense matrix has a ceiling.** `n` events means a `(2n+1)²` float64
  matrix: about 32 MB at 1,000 events, 800 MB at 5,000. Relaxation is O(m·n²),
  so building a network of a few thousand events is slow even though each step
  is cheap. Fine for a personal memoir. The fix, when it is needed, is a sparse
  or banded representation — the public API would not change.
* **Chapter segmentation cuts on temporal gaps only.** It does not know that
  two stretches of life belong together thematically.
* **The offline renderer is plain by construction.** It states the events in
  order and dates them only where the network proves a date. It is a
  legitimate manuscript, not a stub, but prose is what the model is for.
