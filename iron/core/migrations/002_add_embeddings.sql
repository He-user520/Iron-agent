-- 002_add_embeddings.sql — 向量语义搜索支持
-- 为 messages 和 history 表增加 embedding 列（BLOB 存储 numpy 序列化数组）
-- 新增 embedding_meta 表记录 embedding 元数据

-- messages 表增加 embedding 列
ALTER TABLE messages ADD COLUMN embedding BLOB DEFAULT NULL;

-- history 表增加 embedding 列
ALTER TABLE history ADD COLUMN embedding BLOB DEFAULT NULL;

-- 向量元数据表（记录 embedding 模型信息）
CREATE TABLE IF NOT EXISTS embedding_meta (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_name TEXT NOT NULL,          -- 模型名称（如 text-embedding-3-small）
    dimension INTEGER NOT NULL,        -- 向量维度（如 1536）
    created_at TEXT NOT NULL           -- 创建时间
);

-- 索引：加速 embedding 查询（只索引有 embedding 的行）
CREATE INDEX IF NOT EXISTS idx_messages_embedding ON messages(embedding) WHERE embedding IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_history_embedding ON history(embedding) WHERE embedding IS NOT NULL;
