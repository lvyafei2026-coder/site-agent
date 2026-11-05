-- site-agent 业务台账（在 tool-proxy 的 D1 库执行，与 events 同库便于 join）
-- checkpointer 表由 langgraph-checkpoint-sqlite 自动创建，不要手工改

CREATE TABLE IF NOT EXISTS agent_pages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  site TEXT NOT NULL,
  keyword TEXT NOT NULL,
  slug TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'generated',  -- generated/deployed/indexed/verified/failed
  gsc_clicks INTEGER DEFAULT 0,
  gsc_impressions INTEGER DEFAULT 0,
  avg_position REAL,
  created_at INTEGER NOT NULL,
  verified_at INTEGER,
  UNIQUE(site, slug)
);

CREATE INDEX IF NOT EXISTS idx_agent_pages_site ON agent_pages(site, status);

CREATE TABLE IF NOT EXISTS agent_backlinks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  channel TEXT NOT NULL,          -- devto / reddit / directory
  target_url TEXT NOT NULL,
  source_url TEXT,
  anchor_text TEXT,
  status TEXT NOT NULL DEFAULT 'draft',   -- draft/approved/published/dead
  score REAL,
  created_at INTEGER NOT NULL,
  checked_at INTEGER
);

CREATE INDEX IF NOT EXISTS idx_agent_backlinks_status ON agent_backlinks(status, channel);

CREATE TABLE IF NOT EXISTS agent_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  report_date TEXT NOT NULL,              -- YYYY-MM-DD
  kind TEXT NOT NULL DEFAULT 'daily',     -- daily / weekly / plan
  summary TEXT,
  anomalies TEXT,                         -- JSON
  actions TEXT,                           -- JSON
  created_at INTEGER NOT NULL,
  UNIQUE(report_date, kind)
);
