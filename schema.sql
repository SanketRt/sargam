-- Single-user store. SQLite is enough; the manuscript itself lives in a git
-- repo as markdown, so versioning and diffs come for free.
--
-- Adding a TABLE or INDEX here is enough: Store._migrate picks it up and
-- creates it in existing databases. Adding a COLUMN also needs a row in
-- store._COLUMN_MIGRATIONS, because ALTER TABLE needs an explicit default.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- Layer 1: raw input. Append-only. Never updated, never deleted.
CREATE TABLE fragments (
  id         TEXT PRIMARY KEY,
  body       TEXT NOT NULL,
  captured_at TEXT NOT NULL,          -- when you said it, not when it happened
  kind       TEXT NOT NULL DEFAULT 'chat'
);

-- Layer 2: structured atoms.
CREATE TABLE events (
  id         TEXT PRIMARY KEY,
  summary    TEXT NOT NULL,
  granularity TEXT NOT NULL DEFAULT 'episode',  -- moment | episode | era
  s_point    INTEGER NOT NULL,
  e_point    INTEGER NOT NULL,
  mentions   INTEGER NOT NULL DEFAULT 1,
  created_from TEXT REFERENCES fragments(id)
);

CREATE TABLE entities (
  id      TEXT PRIMARY KEY,
  name    TEXT NOT NULL,
  kind    TEXT NOT NULL,              -- person | place | org | object
  aliases TEXT NOT NULL DEFAULT '[]'  -- JSON array, grows as you resolve
);

CREATE TABLE event_entities (
  event_id  TEXT NOT NULL REFERENCES events(id),
  entity_id TEXT NOT NULL REFERENCES entities(id),
  PRIMARY KEY (event_id, entity_id)
);

-- The temporal constraint network. This is the source of truth for order.
CREATE TABLE constraints (
  id         INTEGER PRIMARY KEY,
  x_point    INTEGER NOT NULL,
  y_point    INTEGER NOT NULL,        -- asserts x - y in [lo_days, hi_days]
  lo_days    REAL NOT NULL,
  hi_days    REAL NOT NULL,
  provenance INTEGER NOT NULL,        -- 0 inferred .. 3 absolute
  source     TEXT REFERENCES fragments(id),   -- set when it came from text
  question_id INTEGER,                        -- set when you answered for it
  note       TEXT,
  retracted  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX constraints_live ON constraints(retracted, x_point, y_point);

-- Unresolved references become questions. One row per thing to ask you.
CREATE TABLE questions (
  id        INTEGER PRIMARY KEY,
  event_id  TEXT NOT NULL REFERENCES events(id),
  prompt    TEXT NOT NULL,
  options   TEXT NOT NULL,            -- JSON
  asked_at  TEXT,
  answer    INTEGER,
  answered_at TEXT
);

-- Layer 3: the build graph. A paragraph is a compilation unit.
CREATE TABLE paragraphs (
  id           TEXT PRIMARY KEY,
  chapter      TEXT NOT NULL,
  ordinal      REAL NOT NULL,
  body         TEXT NOT NULL,
  derived_from TEXT NOT NULL,         -- JSON array of event ids
  event_hash   TEXT NOT NULL DEFAULT '', -- hash of those events' *content*, so
                                         -- an edited summary or a moved date
                                         -- is detectable, not just membership
  style_hash   TEXT NOT NULL,
  frozen       INTEGER NOT NULL DEFAULT 0,
  dirty        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX paragraphs_dirty ON paragraphs(dirty, frozen);

-- Per-sentence grounding verdicts from the anti-fabrication pass.
CREATE TABLE groundings (
  paragraph_id TEXT NOT NULL REFERENCES paragraphs(id),
  sentence_ix  INTEGER NOT NULL,
  verdict      TEXT NOT NULL,         -- supported | inferred | unsupported
  evidence     TEXT,                  -- JSON array of fragment ids
  PRIMARY KEY (paragraph_id, sentence_ix)
);

-- Layer 4: compilation bookkeeping.

-- Render cache. The determinism mechanism: a paragraph is regenerated only
-- when its inputs actually changed, so a recompile that changes nothing
-- produces a zero-line diff. Current models reject `temperature`, so this
-- cache is what separates a real change from model drift -- not sampling.
CREATE TABLE render_cache (
  key        TEXT PRIMARY KEY,        -- sha256(event_set_hash || style_hash)
  body       TEXT NOT NULL,
  model      TEXT NOT NULL,
  created_at TEXT NOT NULL
);

-- Time references the extractor could not turn into a constraint. These are
-- the raw material for placement questions, surfaced rather than discarded.
CREATE TABLE unresolved (
  id       INTEGER PRIMARY KEY,
  fragment_id TEXT NOT NULL REFERENCES fragments(id),
  event_id TEXT REFERENCES events(id),
  text     TEXT NOT NULL,
  kind     TEXT NOT NULL DEFAULT 'time',   -- time | entity
  settled  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX unresolved_open ON unresolved(settled, kind);

-- Rejected constraints. A contradiction is evidence, not an error: both
-- readings are kept so the user can choose which to keep and we log which.
CREATE TABLE conflicts (
  id         INTEGER PRIMARY KEY,
  x_point    INTEGER NOT NULL,
  y_point    INTEGER NOT NULL,
  lo_days    REAL NOT NULL,
  hi_days    REAL NOT NULL,
  provenance INTEGER NOT NULL,
  source     TEXT REFERENCES fragments(id),
  note       TEXT,
  culprits   TEXT NOT NULL DEFAULT '[]',   -- JSON, constraint rowids, weakest first
  detected_at TEXT NOT NULL,
  resolution TEXT                          -- null = open | 'kept' | 'dropped'
);

-- One row per compile. Lets `sargam log` tie a manuscript commit back to the
-- state of the network that produced it.
CREATE TABLE compiles (
  id          INTEGER PRIMARY KEY,
  commit_sha  TEXT,
  compiled_at TEXT NOT NULL,
  n_rendered  INTEGER NOT NULL DEFAULT 0,
  n_cached    INTEGER NOT NULL DEFAULT 0,
  n_flagged   INTEGER NOT NULL DEFAULT 0
);
