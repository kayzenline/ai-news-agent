-- 与 app/db.py 对应的建表 SQL（默认值/空值约束与 ORM 并非严格等价）。
-- 应用启动时 create_all 建缺失表，并为旧库补缺失的可空列；这不是完整迁移。
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY, name VARCHAR(80) NOT NULL, email VARCHAR(200) NOT NULL UNIQUE,
  topics JSON, keywords JSON, language VARCHAR(10) DEFAULT 'zh', send_time VARCHAR(5) DEFAULT '08:00', timezone VARCHAR(64),
  email_enabled INTEGER DEFAULT 0, created_at DATETIME
);
CREATE TABLE IF NOT EXISTS briefs (
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), date VARCHAR(10) NOT NULL,
  path VARCHAR(300) NOT NULL, status VARCHAR(20) NOT NULL, stopped_reason VARCHAR(30), steps INTEGER,
  tokens_in INTEGER, tokens_out INTEGER, trace_json TEXT, run_id VARCHAR(40), created_at DATETIME
);
CREATE INDEX IF NOT EXISTS ix_briefs_user_id ON briefs(user_id);
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY, run_id VARCHAR(40) NOT NULL UNIQUE, user_id INTEGER NOT NULL REFERENCES users(id),
  trigger VARCHAR(20) NOT NULL, idempotency_key VARCHAR(120) UNIQUE, status VARCHAR(20) NOT NULL DEFAULT 'queued',
  error TEXT DEFAULT '', brief_id INTEGER, created_at DATETIME, started_at DATETIME, finished_at DATETIME
);
CREATE INDEX IF NOT EXISTS ix_runs_user_id ON runs(user_id);
CREATE TABLE IF NOT EXISTS deliveries (
  id INTEGER PRIMARY KEY, brief_id INTEGER NOT NULL REFERENCES briefs(id), channel VARCHAR(20) NOT NULL,
  recipient VARCHAR(200) NOT NULL, status VARCHAR(20) NOT NULL, attempts INTEGER DEFAULT 0, last_error TEXT,
  message_id VARCHAR(200), updated_at DATETIME, UNIQUE (brief_id, channel, recipient)
);
CREATE INDEX IF NOT EXISTS ix_deliveries_brief_id ON deliveries(brief_id);
