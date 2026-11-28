-- All Agent tables live in the shared radar-db database.
-- Analysis is one current row per term; full content and source evidence stay in D1.

DROP TRIGGER IF EXISTS trg_radar_term_analyses_updated_at;
DROP INDEX IF EXISTS idx_radar_term_analyses_term_created;
DROP INDEX IF EXISTS idx_radar_term_analyses_status_created;
DROP TABLE radar_term_analyses;

CREATE TABLE radar_term_analyses (
  term_id INTEGER PRIMARY KEY REFERENCES radar_terms(term_id) ON DELETE CASCADE,
  status TEXT NOT NULL DEFAULT 'queued'
    CHECK (status IN ('queued', 'running', 'completed', 'failed')),
  verdict TEXT,
  summary TEXT,
  result_json TEXT,
  serp_json TEXT,
  trend_json TEXT,
  model TEXT,
  prompt_version TEXT,
  result_version TEXT,
  error_message TEXT,
  started_at TEXT,
  completed_at TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX idx_radar_term_analyses_status_updated
  ON radar_term_analyses(status, updated_at DESC);
CREATE INDEX idx_radar_term_analyses_completed
  ON radar_term_analyses(completed_at DESC);

CREATE TRIGGER trg_radar_term_analyses_updated_at
AFTER UPDATE ON radar_term_analyses
FOR EACH ROW WHEN NEW.updated_at = OLD.updated_at
BEGIN
  UPDATE radar_term_analyses
  SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
  WHERE term_id = NEW.term_id;
END;

CREATE TABLE agent_runtime_state (
  key TEXT PRIMARY KEY,
  value_json TEXT NOT NULL,
  expires_at INTEGER,
  updated_at INTEGER NOT NULL
);
