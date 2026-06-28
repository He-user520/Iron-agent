-- 初始迁移：创建 sessions / messages / history 表 + 索引
-- 表结构遵循 OpenCode db 模块设计：dataclass ↔ 表行一一映射

-- ── 会话表 ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS sessions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT    NOT NULL UNIQUE,      -- UUID 字符串
    project_path  TEXT    NOT NULL,             -- 项目绝对路径
    created_at    TEXT    NOT NULL,              -- ISO 格式时间戳
    updated_at    TEXT    NOT NULL,              -- ISO 格式时间戳
    message_count INTEGER NOT NULL DEFAULT 0,    -- 消息数（冗余字段，加速列表查询）
    metadata      TEXT    NOT NULL DEFAULT '{}'  -- JSON 元数据
);

-- ── 消息表 ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT    NOT NULL,              -- FK -> sessions.session_id
    role          TEXT    NOT NULL,              -- user / assistant / system / tool
    content       TEXT    NOT NULL DEFAULT '',   -- 消息文本
    tool_calls    TEXT    NOT NULL DEFAULT '[]',  -- JSON 工具调用数组
    tool_call_id  TEXT    NOT NULL DEFAULT '',   -- tool 消息对应的 tool_call_id
    created_at    TEXT    NOT NULL,              -- ISO 格式时间戳
    sequence      INTEGER NOT NULL DEFAULT 0,    -- 消息顺序（0 开始递增）
    FOREIGN KEY (session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
);

-- ── 历史输入表 ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_input    TEXT    NOT NULL,              -- 用户输入的文本
    timestamp     TEXT    NOT NULL,              -- ISO 格式时间戳
    project_path  TEXT    NOT NULL DEFAULT ''    -- 项目路径（可空，用于全局历史）
);

-- ── 索引 ───────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_sessions_project_path ON sessions(project_path);
CREATE INDEX IF NOT EXISTS idx_sessions_updated_at    ON sessions(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_session_id   ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_messages_sequence     ON messages(session_id, sequence);
CREATE INDEX IF NOT EXISTS idx_history_project_path  ON history(project_path);
CREATE INDEX IF NOT EXISTS idx_history_timestamp     ON history(timestamp DESC);
