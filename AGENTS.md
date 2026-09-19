# AGENTS.md

Context for working on this repo.

## What this is

A personal memoir engine. Chat in, chronologically ordered book out.

The one idea everything follows from: **the manuscript is a derived artifact.**
The source of truth is an append-only fragment log plus a temporal constraint
network over extracted events. Chapters are recompiled from events; nothing is
edited in place. Narration order does not matter, because chronology is
*solved*, not transcribed.

## Layout

```
src/sargam/
  timeline.py   Simple Temporal Network. The core. Everything rests on it.
  placement.py  turning an under-constrained event into the fewest questions
  store.py      SQLite persistence, point-index stability, schema migration
  workspace.py  per-user paths and the user-id check
  accounts.py   identity, deliberately a different database from any memoir
  vault.py      sealing a user's own API credential at rest
  account_ops.py export, deletion, rate limits
  extract.py    text -> candidate events and constraints; api + offline backends
  entities.py   alias resolution, merges, referring expressions as questions
  render.py     chapter segmentation, paragraph compilation, the render cache
  ground.py     per-sentence anti-fabrication verdicts
  publish.py    markdown out, one git commit per compile
  ask.py        the human loop, wired to the store
  api.py        review-UI handlers with no transport in them
  web.py        local transport, standard library only
  server.py     hosted transport, FastAPI, accounts and eviction
  cli.py        the command surface
  schema.sql    the store, including the paragraph build graph
bin/sargam      entry point (no install needed)
tests/          plain scripts, no framework
examples/demo.py
Dockerfile      git is installed on purpose -- publish.py shells out to it
deploy/entrypoint.sh   chowns the volume, drops to a non-root user, one worker
fly.toml        one machine, one volume, scale-to-zero
```

## Running things

```
python run_tests.py             every suite
python examples/demo.py         capture -> solve -> ask -> compile -> commit
./bin/sargam --help             the CLI
SARGAM_BACKEND=offline ...      force the no-network path
```

The local tool needs **only numpy**. The hosted server needs the extras in
`requirements.txt`; install them into `.venv/`. `tests/test_server.py` skips
itself when they are absent, so a bare checkout still runs green.

## Things that will bite you

**The solver is the load-bearing part.** `timeline.py` maintains an all-pairs
closure incrementally: adding a constraint relaxes through the new edge in
O(n²) rather than re-running Floyd-Warshall. `test_incremental_matches_full`
guards that equivalence across 40 random seeds. If it ever fails, stop —
every bound and every ordering downstream is derived from that matrix.

**Consistency is checked before anything moves**, not by rolling back after.
`Timeline._admits` decides in O(1) whether an assertion's interval meets the
one the closure already proves. This replaced a full matrix copy per
constraint, which at 1500 events moved ~250 GB through memory just to build
the network. Do not reintroduce a copy-and-rollback scheme.

**Determinism comes from the render cache, not from sampling.** Current models
removed `temperature`, so a paragraph is regenerated only when its inputs
change. That is what makes `git diff` after a recompile mean *the model
changed its mind* rather than *the model was sampled again*. Two details hold
it up: the cache key uses year-level dates (not raw bounds, which jitter on
every new constraint), and a paragraph is keyed on its **earliest event** (not
its event set, which would mint a new id — and lose the freeze flag — exactly
when sources move under approved prose).

**Point indices must survive a reload.** Constraints address nodes by index,
so `store._load_timeline` replays events in `s_point` order and *asserts* the
allocation rather than assuming it.

**The snapshot must equal a replay.** `solver_snapshot` caches the solved
matrix, fingerprinted over the live constraint rows. It is 17-330× faster to
reopen a store. `test_snapshot_equals_replay` is what licenses trusting it.

**A user's API credential is sealed, and bound to its owner.** `vault.py`
uses AES-GCM with the account id as associated data, under a master key from
`SARGAM_KEY_SECRET` that is never in the database. The binding is what stops a
ciphertext being lifted from one row into another. Only a four-character hint
is ever readable back, and a key is validated before it is stored. Never log
it, never return it, never put it in a cache key.

**The UI is the host site's design system, not its own.** Tokens, typeface
and radii are copied from that site's stylesheet; if you change a colour here,
change it there or the two drift apart. The theme is stored under the same
`theme` localStorage key, on the same origin, so it carries across — renaming
the key silently breaks that. The palette is monochrome, which leaves the only
hues on the page for the three grounding verdicts.

**One worker, one machine.** Each worker keeps its own registry of open
stores; two writing the same user's SQLite file corrupts it. `--workers 1` in
the entrypoint and a single machine in `fly.toml` are load-bearing, not
defaults. Scaling out means moving off SQLite first.

**Never treat an identity provider's test mode as access control.** Google's
"Testing" status was assumed to limit sign-ins to listed test users; on this
app, with only `openid`/`email`/`profile`, a second account got in anyway and
Google's own counter still read "1 test, 0 other". Admission lives in
`server.may_sign_in`, gated by `SARGAM_ALLOWED_EMAILS`, and refusal happens
before any row or directory is created.

**Multi-tenancy is physical.** One SQLite file per user, ids derived
(never taken) from Google's subject so they satisfy `SAFE_ID` by construction.
The **only** thing that may name a workspace is the signed session cookie —
never a header, query parameter or body field. There is a test that tries all
three.

**Deletion closes before it unlinks.** `registry.drop` waits for a request
in flight rather than skipping it, unlike eviction. Removing files the process
still has open leaves a half-deleted account and a live handle to data that is
meant to be gone. The account row and the workspace are separate databases, so
both are removed explicitly.

**A green test suite does not mean the image is deployable.** Tests run in a
virtualenv containing whatever was ever installed there; the container has
only `requirements.txt`. Worse, optional imports hide behind configuration --
authlib is imported only when OAuth credentials are present, so a missing
dependency can survive every local check and every deploy until the moment
credentials are set. `deploy/smoke.sh` boots the image with OAuth configured
for exactly this reason. Run it before any deploy.

## Conventions

- **No development-history language anywhere** — no "weekend 3", no "not
  started yet", no "the part that was missing". Comments say why the code is
  the way it is, not when it was written.
- Comments earn their place by explaining a decision or a trap. Don't narrate
  what the line already says.
- Tests assert *properties*, not line coverage. Each one prints `ok <claim>`.
  The claim is the point: if it cannot be stated in a sentence, it is probably
  not the right test.
- Git: brief subject line, no co-author trailer. Commit to `main`.
- Update `README.md` in the same commit as the change it describes.

## State

Local tool is complete and usable. Hosting is partway:

- done: per-request API keys, per-user workspaces, FastAPI transport,
  Google sign-in, LRU + idle eviction, solver snapshot, sealed bring-your-own-
  key storage
- done: the UI in the host site's design system; Dockerfile, entrypoint and
  fly.toml, all verified by building and running the image locally
- done: export, account deletion and per-account rate limits
- next: `fly deploy` (blocked on Fly billing), Google OAuth credentials, and
  the reverse-proxy line on the host site
- deferred: subscriptions on the owner's key (needs per-user spend caps and a
  read of Anthropic's commercial terms before any money changes hands)

Known ceilings, documented in the README: dense matrix (~32 MB at 1000
events), chapter segmentation cuts on time gaps only, single machine.
