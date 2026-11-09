-- site-agent 业务台账（在 tool-proxy 的 D1 库执行，与 events 同库便于 join）
-- 表与列必须与 src/app/tools/adapters/cloudflare/sql.py 的 LEDGER_TABLES 一致：
-- 每个 UNIQUE 就是 SqlBuilder 的 ON CONFLICT 目标，缺了写入会直接报错。
-- checkpointer 表由 langgraph-checkpoint-sqlite 自动创建，不要手工改。

-- 内容页台账：一个站 + 一个 slug 唯一
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

-- 外链台账：event_key 是幂等键，lookup 策略按它先查后写
CREATE TABLE IF NOT EXISTS agent_backlinks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  channel TEXT NOT NULL,          -- devto / reddit / directory
  target_url TEXT NOT NULL,
  source_url TEXT,
  anchor_text TEXT,
  status TEXT NOT NULL DEFAULT 'draft',   -- draft/approved/published/dead
  score REAL,
  created_at INTEGER NOT NULL,
  checked_at INTEGER,
  event_key TEXT NOT NULL,
  UNIQUE(event_key)
);

CREATE INDEX IF NOT EXISTS idx_agent_backlinks_status ON agent_backlinks(status, channel);

-- 报告 / 排期：同一天同一类型唯一（plan 就是推广排期）
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

-- 事件队列：ignore 策略，重复写不会把已消费的事件改回 pending
CREATE TABLE IF NOT EXISTS agent_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_key TEXT NOT NULL,
  kind TEXT NOT NULL,                     -- hotwords-ready / pages-deployed / promo-scheduled / ...
  payload TEXT,                           -- JSON
  status TEXT NOT NULL DEFAULT 'pending', -- pending / consumed
  created_at INTEGER NOT NULL,
  consumed_at INTEGER,
  thread_id TEXT,
  UNIQUE(event_key)
);

CREATE INDEX IF NOT EXISTS idx_agent_events_pending ON agent_events(status, kind);

-- 复盘 / 反思：同一动作在 5 分钟窗口内只记一条（键在代码里带时间片）
CREATE TABLE IF NOT EXISTS agent_reflections (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_key TEXT NOT NULL,
  slot TEXT NOT NULL,                     -- 来源标识（哪个能力写入）
  action_key TEXT,
  reason TEXT,
  detail TEXT,
  created_at INTEGER NOT NULL,
  UNIQUE(event_key)
);

CREATE INDEX IF NOT EXISTS idx_agent_reflections_slot ON agent_reflections(slot, created_at);

-- 工具调用审计：write_ledger 可写，默认编排不写
CREATE TABLE IF NOT EXISTS agent_tool_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_key TEXT NOT NULL,
  tool TEXT NOT NULL,
  kind TEXT,
  thread_id TEXT,
  slot TEXT,
  args TEXT,                              -- JSON
  ok INTEGER,
  detail TEXT,
  created_at INTEGER NOT NULL,
  UNIQUE(event_key)
);

-- 幂等键表：write_ledger 可写，默认编排不写
CREATE TABLE IF NOT EXISTS agent_idempotency (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  idem_key TEXT NOT NULL,
  tool TEXT,
  result TEXT,                            -- JSON
  created_at INTEGER NOT NULL,
  UNIQUE(idem_key)
);
