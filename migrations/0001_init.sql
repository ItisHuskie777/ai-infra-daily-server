-- Sources are configuration (FR1.4): editable at runtime, seeded from app/sources/seeds.py.
CREATE TABLE sources (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  kind TEXT NOT NULL,
  category TEXT NOT NULL,
  language TEXT NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1,
  weight REAL NOT NULL DEFAULT 1.0,
  config_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- One row per fetch attempt per source, for the source-health footer (FR1.5) and QA2.
CREATE TABLE source_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id TEXT NOT NULL REFERENCES sources(id),
  started_at TEXT NOT NULL,
  finished_at TEXT NOT NULL,
  ok INTEGER NOT NULL,
  error TEXT,
  detail TEXT,
  item_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX source_runs_source_started ON source_runs(source_id, started_at);

-- A thread links events about the same topic across days (FR4).
CREATE TABLE threads (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  day_count INTEGER NOT NULL DEFAULT 1,
  pinned INTEGER NOT NULL DEFAULT 0,
  pinned_query TEXT
);
CREATE INDEX threads_last_seen ON threads(last_seen_at);

-- An event groups items about the same story within a rolling window (FR3).
CREATE TABLE events (
  id TEXT PRIMARY KEY,
  thread_id TEXT REFERENCES threads(id),
  title TEXT NOT NULL,
  summary TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  centroid_item_id TEXT
);
CREATE INDEX events_updated ON events(updated_at);

-- Items are identified by the hash of their canonical URL (FR2.1), so re-fetching is idempotent.
-- The embedding is stored inline as little-endian float32 bytes; there is no separate vector store.
CREATE TABLE items (
  id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES sources(id),
  kind TEXT NOT NULL,
  url TEXT NOT NULL,
  canonical_url TEXT NOT NULL,
  title TEXT NOT NULL,
  author TEXT,
  language TEXT NOT NULL,
  published_at TEXT NOT NULL,
  first_seen_at TEXT NOT NULL,
  text TEXT NOT NULL,
  engagement_json TEXT,
  comments_json TEXT NOT NULL DEFAULT '[]',
  tags_json TEXT NOT NULL DEFAULT '[]',
  embedding BLOB,
  event_id TEXT REFERENCES events(id)
);
CREATE INDEX items_published ON items(published_at);
CREATE INDEX items_event ON items(event_id);
CREATE INDEX items_source ON items(source_id, published_at);

CREATE TABLE digests (
  id TEXT PRIMARY KEY,
  date TEXT NOT NULL,
  style TEXT NOT NULL,
  subject TEXT NOT NULL,
  html TEXT NOT NULL,
  health_json TEXT NOT NULL DEFAULT '[]',
  delivery_status TEXT NOT NULL,
  delivery_error TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX digests_date ON digests(date);

-- Which events (and which items within them) appeared in which digest; drives FR2.2 and FR4.1.
CREATE TABLE digest_events (
  digest_id TEXT NOT NULL REFERENCES digests(id),
  event_id TEXT NOT NULL REFERENCES events(id),
  section TEXT NOT NULL,
  position INTEGER NOT NULL,
  relevance_score REAL NOT NULL DEFAULT 0,
  rationale TEXT NOT NULL DEFAULT '',
  item_ids_json TEXT NOT NULL,
  PRIMARY KEY (digest_id, event_id)
);

CREATE TABLE feedback (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  digest_id TEXT REFERENCES digests(id),
  event_id TEXT REFERENCES events(id),
  item_id TEXT REFERENCES items(id),
  source_id TEXT REFERENCES sources(id),
  signal TEXT NOT NULL
);

-- Owner preferences: one JSON document per key ("profile", "interests").
CREATE TABLE preferences (
  key TEXT PRIMARY KEY,
  value_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- One row per pipeline run, for QA1 timing and operator visibility.
CREATE TABLE runs (
  id TEXT PRIMARY KEY,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  stage TEXT NOT NULL,
  outcome TEXT,
  detail TEXT,
  digest_id TEXT REFERENCES digests(id)
);
