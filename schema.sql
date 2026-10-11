-- 与 app/db.py 等价的建表 SQL（应用启动时也会自动 create_all）
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY, name VARCHAR(80) NOT NULL, email VARCHAR(200) NOT NULL UNIQUE,
  topics JSON, keywords JSON, language VARCHAR(10) DEFAULT 'zh', send_time VARCHAR(5) DEFAULT '08:00',
  email_enabled INTEGER DEFAULT 0, created_at DATETIME
);
CREATE TABLE IF NOT EXISTS briefs (
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), date VARCHAR(10) NOT NULL,
  path VARCHAR(300) NOT NULL, status VARCHAR(20) NOT NULL, stopped_reason VARCHAR(30), steps INTEGER,
  tokens_in INTEGER, tokens_out INTEGER, trace_json TEXT, created_at DATETIME
);
CREATE INDEX IF NOT EXISTS ix_briefs_user_id ON briefs(user_id);
