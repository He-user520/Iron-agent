"""SQLite 持久化层 — 会话/消息/历史存储

参考 OpenCode 的 db 模块设计：
- 类型安全：用 dataclass 映射表结构
- 迁移友好：version 表 + 增量 SQL（migrations/ 目录）
- WAL 模式：支持并发读 + 单写
- 上下文管理器：自动 commit/rollback

用法:
    from iron.core.db import Database, SessionRow, MessageRow, HistoryRow

    with Database() as db:
        sid = db.save_session(SessionRow(
            session_id="abc-123",
            project_path="/path/to/project",
            created_at="2026-01-01T00:00:00",
            updated_at="2026-01-01T00:00:00",
        ))
        db.save_message(MessageRow(
            session_id="abc-123",
            role="user",
            content="hello",
            created_at="2026-01-01T00:00:01",
            sequence=0,
        ))
        messages = db.get_messages("abc-123")
"""
import sqlite3
import json
import logging
import struct
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

# 可选依赖：numpy 用于向量相似度计算，不可用时降级到纯 Python 实现
try:
    import numpy as np
    _HAS_NUMPY = True
except ImportError:
    _HAS_NUMPY = False

logger = logging.getLogger(__name__)

# 默认数据库路径：~/.iron/iron.db
DEFAULT_DB_PATH = Path.home() / ".iron" / "iron.db"

# 迁移 SQL 文件所在目录（与 db.py 同级的 migrations/ 子目录）
_MIGRATIONS_DIR = Path(__file__).parent / "migrations"


@dataclass
class SessionRow:
    """会话表行 — 对应 sessions 表

    注意：dataclass 要求有默认值的字段位于无默认值的字段之后，
    因此自增 id 放在最后（默认 None，仅在从数据库读取后填充）。
    """
    session_id: str            # UUID
    project_path: str
    created_at: str            # ISO 格式时间戳
    updated_at: str
    message_count: int = 0
    metadata: str = "{}"       # JSON 字符串
    id: Optional[int] = None   # 自增 id（仅在读取后填充）


@dataclass
class MessageRow:
    """消息表行 — 对应 messages 表"""
    session_id: str            # FK -> sessions.session_id
    role: str                  # user / assistant / system / tool
    content: str
    created_at: str
    tool_calls: str = "[]"     # JSON 数组
    tool_call_id: str = ""
    sequence: int = 0          # 消息顺序（0 开始递增）
    id: Optional[int] = None   # 自增 id（仅在读取后填充）


@dataclass
class HistoryRow:
    """历史输入表行 — 对应 history 表"""
    user_input: str
    timestamp: str
    project_path: str = ""
    id: Optional[int] = None   # 自增 id（仅在读取后填充）


class Database:
    """SQLite 数据库 — WAL 模式，自动迁移

    用法:
        with Database() as db:
            db.save_session(session)
            messages = db.get_messages(session_id)
    """

    def __init__(self, db_path: Path = None):
        self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None

    def __enter__(self) -> "Database":
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def connect(self) -> None:
        """连接数据库，启用 WAL，执行迁移"""
        # check_same_thread=False 让连接可跨线程使用
        # （WAL 模式下读写分离，单写多读安全）
        self._conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
        )
        # 启用外键约束（默认关闭），支持 ON DELETE CASCADE
        self._conn.execute("PRAGMA foreign_keys = ON")
        # WAL 模式：并发读 + 单写
        self._conn.execute("PRAGMA journal_mode = WAL")
        # 普通 DML 提交策略：手动提交
        self._conn.row_factory = sqlite3.Row
        # 执行迁移
        self._migrate()

    def close(self) -> None:
        """关闭连接"""
        if self._conn is not None:
            try:
                self._conn.commit()
            except sqlite3.Error:
                pass
            self._conn.close()
            self._conn = None

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """事务上下文管理器 — 自动 commit / rollback

        用法:
            with db.transaction() as conn:
                conn.execute("INSERT INTO ...", ...)
        """
        if self._conn is None:
            raise RuntimeError("数据库未连接，请先调用 connect() 或使用 with 语法")
        conn = self._conn
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def _migrate(self) -> None:
        """执行数据库迁移

        - 维护 schema_version 表记录当前迁移版本
        - 按文件名排序依次执行 migrations/*.sql 中尚未执行的迁移
        """
        conn = self._conn
        # 创建版本表（如果不存在）
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version ("
            "  version INTEGER PRIMARY KEY,"
            "  applied_at TEXT NOT NULL"
            ")"
        )
        # 查询当前已应用的版本号
        cur = conn.execute("SELECT MAX(version) FROM schema_version")
        row = cur.fetchone()
        current_version = row[0] if row and row[0] is not None else 0

        # 扫描 migrations 目录，按文件名排序
        if not _MIGRATIONS_DIR.exists():
            logger.warning("迁移目录不存在: %s", _MIGRATIONS_DIR)
            return
        migrations = sorted(_MIGRATIONS_DIR.glob("*.sql"))
        applied = 0
        for mf in migrations:
            # 从文件名提取版本号（如 001_initial.sql -> 1）
            try:
                version = int(mf.stem.split("_", 1)[0])
            except (ValueError, IndexError):
                logger.warning("迁移文件名格式错误，跳过: %s", mf.name)
                continue
            if version <= current_version:
                continue
            sql_text = mf.read_text(encoding="utf-8")
            # executescript 支持多条 SQL 语句
            conn.executescript(sql_text)
            conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (version, datetime.now().isoformat()),
            )
            conn.commit()
            current_version = version
            applied += 1
            logger.info("已应用迁移 %s (version=%d)", mf.name, version)
        if applied == 0:
            logger.debug("无新迁移需要应用，当前版本=%d", current_version)

    # ── Session 操作 ──────────────────────────────────────────────

    def save_session(self, session: SessionRow) -> int:
        """插入或更新会话（根据 session_id upsert）

        返回新插入行的自增 id
        """
        with self.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO sessions (session_id, project_path, created_at, updated_at, "
                "message_count, metadata) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(session_id) DO UPDATE SET "
                "  project_path=excluded.project_path, "
                "  updated_at=excluded.updated_at, "
                "  message_count=excluded.message_count, "
                "  metadata=excluded.metadata "
                "RETURNING id",
                (
                    session.session_id,
                    session.project_path,
                    session.created_at,
                    session.updated_at,
                    session.message_count,
                    session.metadata,
                ),
            )
            row = cur.fetchone()
            return int(row["id"])

    def get_session(self, session_id: str) -> Optional[SessionRow]:
        """获取会话"""
        cur = self._conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?",
            (session_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return SessionRow(
            id=row["id"],
            session_id=row["session_id"],
            project_path=row["project_path"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            message_count=row["message_count"],
            metadata=row["metadata"],
        )

    def list_sessions(
        self, project_path: str = None, limit: int = 50
    ) -> list[SessionRow]:
        """列出会话 — 按 updated_at 倒序"""
        if project_path is None:
            cur = self._conn.execute(
                "SELECT * FROM sessions ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            )
        else:
            cur = self._conn.execute(
                "SELECT * FROM sessions WHERE project_path = ? "
                "ORDER BY updated_at DESC LIMIT ?",
                (project_path, limit),
            )
        rows = cur.fetchall()
        return [
            SessionRow(
                id=r["id"],
                session_id=r["session_id"],
                project_path=r["project_path"],
                created_at=r["created_at"],
                updated_at=r["updated_at"],
                message_count=r["message_count"],
                metadata=r["metadata"],
            )
            for r in rows
        ]

    def delete_session(self, session_id: str) -> bool:
        """删除会话（级联删除其消息）

        返回是否删除了行
        """
        with self.transaction() as conn:
            cur = conn.execute(
                "DELETE FROM sessions WHERE session_id = ?",
                (session_id,),
            )
            return cur.rowcount > 0

    # ── Message 操作 ──────────────────────────────────────────────

    def save_message(self, message: MessageRow) -> int:
        """插入消息，返回新插入行的自增 id"""
        with self.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO messages (session_id, role, content, tool_calls, "
                "tool_call_id, created_at, sequence) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id",
                (
                    message.session_id,
                    message.role,
                    message.content,
                    message.tool_calls,
                    message.tool_call_id,
                    message.created_at,
                    message.sequence,
                ),
            )
            row = cur.fetchone()
            return int(row["id"])

    def save_messages(self, session_id: str, messages: list[dict]) -> int:
        """批量保存消息（替换现有）

        - 先删除该 session 的所有消息
        - 再批量插入新消息
        - 返回插入的消息数
        """
        with self.transaction() as conn:
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            if not messages:
                return 0
            rows = []
            now = datetime.now().isoformat()
            for i, m in enumerate(messages):
                role = m.get("role", "user")
                content = m.get("content", "")
                tool_calls = m.get("tool_calls", [])
                if not isinstance(tool_calls, str):
                    tool_calls = json.dumps(tool_calls, ensure_ascii=False)
                tool_call_id = m.get("tool_call_id", "") or m.get("tool_call_id", "")
                seq = m.get("sequence", i)
                ts = m.get("timestamp") or m.get("created_at") or now
                rows.append((session_id, role, content, tool_calls, tool_call_id, ts, seq))
            conn.executemany(
                "INSERT INTO messages (session_id, role, content, tool_calls, "
                "tool_call_id, created_at, sequence) VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            return len(rows)

    def get_messages(self, session_id: str) -> list[MessageRow]:
        """获取会话所有消息（按 sequence 升序）"""
        cur = self._conn.execute(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY sequence ASC",
            (session_id,),
        )
        rows = cur.fetchall()
        return [
            MessageRow(
                id=r["id"],
                session_id=r["session_id"],
                role=r["role"],
                content=r["content"],
                tool_calls=r["tool_calls"],
                tool_call_id=r["tool_call_id"],
                created_at=r["created_at"],
                sequence=r["sequence"],
            )
            for r in rows
        ]

    def get_message_count(self, session_id: str) -> int:
        """获取消息数"""
        cur = self._conn.execute(
            "SELECT COUNT(*) AS cnt FROM messages WHERE session_id = ?",
            (session_id,),
        )
        row = cur.fetchone()
        return int(row["cnt"]) if row else 0

    # ── History 操作 ──────────────────────────────────────────────

    def save_history(self, history: HistoryRow) -> int:
        """保存用户输入历史，返回新插入行的自增 id"""
        with self.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO history (user_input, timestamp, project_path) "
                "VALUES (?, ?, ?) RETURNING id",
                (history.user_input, history.timestamp, history.project_path),
            )
            row = cur.fetchone()
            return int(row["id"])

    def get_history(
        self, project_path: str = None, limit: int = 100
    ) -> list[HistoryRow]:
        """获取历史输入 — 按 timestamp 倒序"""
        if project_path is None:
            cur = self._conn.execute(
                "SELECT * FROM history ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            )
        else:
            cur = self._conn.execute(
                "SELECT * FROM history WHERE project_path = ? "
                "ORDER BY timestamp DESC LIMIT ?",
                (project_path, limit),
            )
        rows = cur.fetchall()
        return [
            HistoryRow(
                id=r["id"],
                user_input=r["user_input"],
                timestamp=r["timestamp"],
                project_path=r["project_path"],
            )
            for r in rows
        ]

    def search_history(self, query: str, limit: int = 20) -> list[HistoryRow]:
        """搜索历史（LIKE 查询）"""
        cur = self._conn.execute(
            "SELECT * FROM history WHERE user_input LIKE ? "
            "ORDER BY timestamp DESC LIMIT ?",
            (f"%{query}%", limit),
        )
        rows = cur.fetchall()
        return [
            HistoryRow(
                id=r["id"],
                user_input=r["user_input"],
                timestamp=r["timestamp"],
                project_path=r["project_path"],
            )
            for r in rows
        ]

    def clear_history(self, project_path: str = None) -> int:
        """清空历史

        - project_path=None：清空全部
        - 否则只清空指定项目的记录
        返回删除的行数
        """
        with self.transaction() as conn:
            if project_path is None:
                cur = conn.execute("DELETE FROM history")
            else:
                cur = conn.execute(
                    "DELETE FROM history WHERE project_path = ?",
                    (project_path,),
                )
            return cur.rowcount

    # ── 统计 ──────────────────────────────────────────────────────

    def get_stats(self) -> dict:
        """获取数据库统计：会话数、消息数、历史数、db 大小（字节）"""
        cur = self._conn.execute("SELECT COUNT(*) FROM sessions")
        session_count = cur.fetchone()[0]
        cur = self._conn.execute("SELECT COUNT(*) FROM messages")
        message_count = cur.fetchone()[0]
        cur = self._conn.execute("SELECT COUNT(*) FROM history")
        history_count = cur.fetchone()[0]
        db_size = self.db_path.stat().st_size if self.db_path.exists() else 0
        return {
            "sessions": int(session_count),
            "messages": int(message_count),
            "history": int(history_count),
            "db_size": db_size,
            "db_path": str(self.db_path),
        }

    # ── 向量语义搜索 ────────────────────────────────────────────
    #
    # embedding 以 BLOB 存储，序列化格式：
    #   [4 字节: 维度 N (uint32, little-endian)]
    #   [N * 4 字节: float32 (little-endian)]
    # numpy 可用时用 numpy 序列化/反序列化；不可用时用 struct。
    # 相似度计算用余弦相似度（numpy）或纯 Python 降级实现。

    @staticmethod
    def _serialize_embedding(vec: list[float]) -> bytes:
        """将 float 向量序列化为 BLOB（兼容 numpy / 纯 Python）

        格式：4 字节维度 + N * 4 字节 float32
        """
        if not vec:
            return b""
        dim = len(vec)
        if _HAS_NUMPY:
            arr = np.asarray(vec, dtype=np.float32)
            return struct.pack("<I", dim) + arr.tobytes()
        # 纯 Python 降级
        return struct.pack("<I", dim) + b"".join(
            struct.pack("<f", float(v)) for v in vec
        )

    @staticmethod
    def _deserialize_embedding(blob: bytes) -> Optional[list[float]]:
        """从 BLOB 反序列化 float 向量"""
        if not blob or len(blob) < 4:
            return None
        dim = struct.unpack("<I", blob[:4])[0]
        if dim == 0 or len(blob) < 4 + dim * 4:
            return None
        if _HAS_NUMPY:
            arr = np.frombuffer(blob[4:4 + dim * 4], dtype=np.float32)
            return arr.tolist()
        # 纯 Python 降级
        return [struct.unpack("<f", blob[4 + i * 4:8 + i * 4])[0]
                for i in range(dim)]

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        """计算余弦相似度（numpy 优先，不可用时纯 Python）"""
        if not a or not b or len(a) != len(b):
            return 0.0
        if _HAS_NUMPY:
            arr_a = np.asarray(a, dtype=np.float32)
            arr_b = np.asarray(b, dtype=np.float32)
            norm_a = np.linalg.norm(arr_a)
            norm_b = np.linalg.norm(arr_b)
            if norm_a == 0 or norm_b == 0:
                return 0.0
            return float(np.dot(arr_a, arr_b) / (norm_a * norm_b))
        # 纯 Python 降级
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = sum(x * x for x in a) ** 0.5
        norm_b = sum(x * x for x in b) ** 0.5
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    def _save_embedding(self, table: str, row_id: int,
                       embedding: list[float]) -> bool:
        """保存 embedding 到 messages 或 history 表的指定行

        Args:
            table: "messages" 或 "history"
            row_id: 行的自增 id
            embedding: float 向量

        Returns:
            是否保存成功
        """
        if table not in ("messages", "history"):
            raise ValueError(f"不支持的表: {table}（仅支持 messages/history）")
        blob = self._serialize_embedding(embedding)
        with self.transaction() as conn:
            cur = conn.execute(
                f"UPDATE {table} SET embedding = ? WHERE id = ?",
                (blob, row_id),
            )
            return cur.rowcount > 0

    def save_message_with_embedding(self, message: MessageRow,
                                     embedding: list[float]) -> int:
        """保存消息并写入 embedding（一步到位）

        等价于 save_message + _save_embedding，但只开一次事务。
        返回新插入行的自增 id。
        """
        blob = self._serialize_embedding(embedding) if embedding else None
        with self.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO messages (session_id, role, content, tool_calls, "
                "tool_call_id, created_at, sequence, embedding) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
                (
                    message.session_id,
                    message.role,
                    message.content,
                    message.tool_calls,
                    message.tool_call_id,
                    message.created_at,
                    message.sequence,
                    blob,
                ),
            )
            row = cur.fetchone()
            return int(row["id"])

    def save_history_with_embedding(self, history: HistoryRow,
                                     embedding: list[float]) -> int:
        """保存历史并写入 embedding（一步到位）"""
        blob = self._serialize_embedding(embedding) if embedding else None
        with self.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO history (user_input, timestamp, project_path, embedding) "
                "VALUES (?, ?, ?, ?) RETURNING id",
                (history.user_input, history.timestamp, history.project_path, blob),
            )
            row = cur.fetchone()
            return int(row["id"])

    def search_semantic(self, query_embedding: list[float],
                        table: str = "history",
                        limit: int = 20,
                        min_score: float = 0.0,
                        project_path: str = None) -> list[dict]:
        """向量语义搜索

        Args:
            query_embedding: 查询向量
            table: "messages" 或 "history"（默认 history）
            limit: 返回结果数上限
            min_score: 最小相似度阈值（0.0~1.0）
            project_path: 仅搜索此项目（None = 全局）

        Returns:
            list[dict]：按相似度降序排列，每条含 id/content/score/...字段
        """
        if table not in ("messages", "history"):
            raise ValueError(f"不支持的表: {table}")
        # 读取所有有 embedding 的行
        if project_path:
            if table == "messages":
                cur = self._conn.execute(
                    "SELECT m.id, m.session_id, m.role, m.content, m.created_at, "
                    "m.sequence, m.embedding, s.project_path "
                    "FROM messages m JOIN sessions s ON m.session_id = s.session_id "
                    "WHERE m.embedding IS NOT NULL AND s.project_path = ?",
                    (project_path,),
                )
            else:
                cur = self._conn.execute(
                    "SELECT id, user_input, timestamp, project_path, embedding "
                    "FROM history WHERE embedding IS NOT NULL AND project_path = ?",
                    (project_path,),
                )
        else:
            if table == "messages":
                cur = self._conn.execute(
                    "SELECT id, session_id, role, content, created_at, sequence, embedding "
                    "FROM messages WHERE embedding IS NOT NULL",
                )
            else:
                cur = self._conn.execute(
                    "SELECT id, user_input, timestamp, project_path, embedding "
                    "FROM history WHERE embedding IS NOT NULL",
                )
        rows = cur.fetchall()
        # 计算相似度并排序
        scored = []
        for r in rows:
            vec = self._deserialize_embedding(r["embedding"])
            if vec is None:
                continue
            score = self._cosine_similarity(query_embedding, vec)
            if score < min_score:
                continue
            item = {"id": r["id"], "score": score}
            if table == "messages":
                item.update({
                    "session_id": r["session_id"],
                    "role": r["role"],
                    "content": r["content"],
                    "created_at": r["created_at"],
                    "sequence": r["sequence"],
                })
            else:
                item.update({
                    "user_input": r["user_input"],
                    "timestamp": r["timestamp"],
                    "project_path": r["project_path"],
                })
            scored.append(item)
        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:limit]

    def search_hybrid(self, query: str, query_embedding: list[float] = None,
                      table: str = "history",
                      limit: int = 20,
                      keyword_weight: float = 0.3,
                      semantic_weight: float = 0.7,
                      project_path: str = None) -> list[dict]:
        """混合检索：关键词（LIKE）+ 语义（向量）

        无 query_embedding 时降级为纯关键词搜索。

        Args:
            query: 文本查询
            query_embedding: 查询向量（None 时降级为纯关键词）
            table: "messages" 或 "history"
            limit: 返回结果数上限
            keyword_weight: 关键词权重（0~1）
            semantic_weight: 语义权重（0~1）
            project_path: 仅搜索此项目

        Returns:
            list[dict]：按融合分数降序排列
        """
        # 关键词搜索（基础候选集，扩大 limit 避免漏掉）
        kw_limit = limit * 3 if query else 0
        if table == "history":
            if query:
                kw_rows = self.search_history(query, limit=kw_limit)
            else:
                kw_rows = self.get_history(project_path=project_path, limit=kw_limit)
            kw_map = {r.id: r for r in kw_rows}
        else:
            # messages 表无关键词搜索接口，跳过关键词部分
            kw_map = {}

        # 语义搜索
        semantic_results = []
        if query_embedding:
            semantic_results = self.search_semantic(
                query_embedding, table=table, limit=limit * 3,
                project_path=project_path,
            )
        sem_map = {r["id"]: r for r in semantic_results}

        # 候选集 = 关键词 ∪ 语义
        candidate_ids = set(kw_map.keys()) | set(sem_map.keys())
        scored = []
        for cid in candidate_ids:
            kw_score = 1.0 if cid in kw_map else 0.0  # 简单二值（命中=1）
            sem_score = sem_map.get(cid, {}).get("score", 0.0)
            fused = keyword_weight * kw_score + semantic_weight * sem_score
            # 构造结果项
            if cid in sem_map:
                item = dict(sem_map[cid])
            else:
                row = kw_map[cid]
                item = {"id": row.id, "score": 0.0}
                if table == "history":
                    item.update({
                        "user_input": row.user_input,
                        "timestamp": row.timestamp,
                        "project_path": row.project_path,
                    })
            item["fused_score"] = fused
            item["keyword_score"] = kw_score
            item["semantic_score"] = sem_score
            scored.append(item)
        scored.sort(key=lambda x: x["fused_score"], reverse=True)
        return scored[:limit]

    def record_embedding_meta(self, model_name: str, dimension: int) -> int:
        """记录 embedding 模型元数据，返回自增 id"""
        with self.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO embedding_meta (model_name, dimension, created_at) "
                "VALUES (?, ?, ?) RETURNING id",
                (model_name, dimension, datetime.now().isoformat()),
            )
            row = cur.fetchone()
            return int(row["id"])

    def get_embedding_meta(self) -> list[dict]:
        """获取所有 embedding 模型元数据"""
        cur = self._conn.execute(
            "SELECT id, model_name, dimension, created_at FROM embedding_meta "
            "ORDER BY id DESC"
        )
        rows = cur.fetchall()
        return [
            {
                "id": r["id"],
                "model_name": r["model_name"],
                "dimension": r["dimension"],
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    # ── 代码索引操作（v3.0: symbols + callgraph） ──────────────────

    def save_symbol(self, name: str, kind: str, file_path: str,
                    line_start: int, line_end: int,
                    col_start: int, col_end: int,
                    project_path: str) -> int:
        """保存符号定义（UPSERT），返回自增 id

        Args:
            name: 符号名（如 HAL_Delay）
            kind: 类型（function | variable | type | macro）
            file_path: 相对项目根的路径
            line_start/line_end: 行范围
            col_start/col_end: 列范围
            project_path: 项目根（多项目隔离）
        """
        with self.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO symbols (name, kind, file_path, line_start, line_end, "
                "col_start, col_end, project_path, indexed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(name, file_path, line_start) DO UPDATE SET "
                "  kind=excluded.kind, line_end=excluded.line_end, "
                "  col_start=excluded.col_start, col_end=excluded.col_end, "
                "  indexed_at=excluded.indexed_at "
                "RETURNING id",
                (name, kind, file_path, line_start, line_end,
                 col_start, col_end, project_path, datetime.now().isoformat()),
            )
            row = cur.fetchone()
            return int(row["id"])

    def save_symbols_batch(self, symbols: list[dict], project_path: str) -> int:
        """批量保存符号，返回插入数"""
        if not symbols:
            return 0
        now = datetime.now().isoformat()
        rows = [
            (s["name"], s["kind"], s["file_path"], s["line_start"], s["line_end"],
             s["col_start"], s["col_end"], project_path, now)
            for s in symbols
        ]
        with self.transaction() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO symbols (name, kind, file_path, line_start, "
                "line_end, col_start, col_end, project_path, indexed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            return len(rows)

    def delete_symbols_by_file(self, file_path: str, project_path: str) -> int:
        """删除指定文件的所有符号（增量索引前清理）"""
        with self.transaction() as conn:
            cur = conn.execute(
                "DELETE FROM symbols WHERE file_path = ? AND project_path = ?",
                (file_path, project_path),
            )
            return cur.rowcount

    def get_symbol_definition(self, name: str,
                              project_path: Optional[str] = None) -> list[dict]:
        """查找符号定义（可能多处）"""
        if project_path:
            cur = self._conn.execute(
                "SELECT * FROM symbols WHERE name = ? AND project_path = ? "
                "ORDER BY file_path, line_start",
                (name, project_path),
            )
        else:
            cur = self._conn.execute(
                "SELECT * FROM symbols WHERE name = ? "
                "ORDER BY file_path, line_start",
                (name,),
            )
        return [dict(r) for r in cur.fetchall()]

    def search_symbols(self, query: str, project_path: Optional[str] = None,
                       limit: int = 20) -> list[dict]:
        """按名称搜索符号（LIKE 查询）"""
        pattern = f"%{query}%"
        if project_path:
            cur = self._conn.execute(
                "SELECT * FROM symbols WHERE name LIKE ? AND project_path = ? "
                "ORDER BY name LIMIT ?",
                (pattern, project_path, limit),
            )
        else:
            cur = self._conn.execute(
                "SELECT * FROM symbols WHERE name LIKE ? ORDER BY name LIMIT ?",
                (pattern, limit),
            )
        return [dict(r) for r in cur.fetchall()]

    def save_call_edge(self, caller_name: str, callee_name: str,
                       caller_file: str, caller_line: int,
                       project_path: str) -> int:
        """保存调用关系（UPSERT），返回自增 id"""
        with self.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO callgraph (caller_name, callee_name, caller_file, "
                "caller_line, project_path, indexed_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(caller_name, callee_name, caller_file, caller_line) DO UPDATE SET "
                "  indexed_at=excluded.indexed_at "
                "RETURNING id",
                (caller_name, callee_name, caller_file, caller_line,
                 project_path, datetime.now().isoformat()),
            )
            row = cur.fetchone()
            return int(row["id"])

    def save_call_edges_batch(self, edges: list[dict], project_path: str) -> int:
        """批量保存调用关系"""
        if not edges:
            return 0
        now = datetime.now().isoformat()
        rows = [
            (e["caller_name"], e["callee_name"], e["caller_file"], e["caller_line"],
             project_path, now)
            for e in edges
        ]
        with self.transaction() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO callgraph (caller_name, callee_name, "
                "caller_file, caller_line, project_path, indexed_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )
            return len(rows)

    def delete_calls_by_file(self, file_path: str, project_path: str) -> int:
        """删除指定文件的所有调用关系"""
        with self.transaction() as conn:
            cur = conn.execute(
                "DELETE FROM callgraph WHERE caller_file = ? AND project_path = ?",
                (file_path, project_path),
            )
            return cur.rowcount

    def get_callers(self, callee_name: str,
                    project_path: Optional[str] = None) -> list[dict]:
        """查找调用某函数的所有位置"""
        if project_path:
            cur = self._conn.execute(
                "SELECT * FROM callgraph WHERE callee_name = ? AND project_path = ? "
                "ORDER BY caller_file, caller_line",
                (callee_name, project_path),
            )
        else:
            cur = self._conn.execute(
                "SELECT * FROM callgraph WHERE callee_name = ? "
                "ORDER BY caller_file, caller_line",
                (callee_name,),
            )
        return [dict(r) for r in cur.fetchall()]

    def get_callees(self, caller_name: str,
                    project_path: Optional[str] = None) -> list[dict]:
        """查找某函数调用的所有函数"""
        if project_path:
            cur = self._conn.execute(
                "SELECT * FROM callgraph WHERE caller_name = ? AND project_path = ? "
                "ORDER BY caller_file, caller_line",
                (caller_name, project_path),
            )
        else:
            cur = self._conn.execute(
                "SELECT * FROM callgraph WHERE caller_name = ? "
                "ORDER BY caller_file, caller_line",
                (caller_name,),
            )
        return [dict(r) for r in cur.fetchall()]

    def find_dead_code(self, project_path: str) -> list[dict]:
        """查找未被任何函数调用的函数（死代码）

        策略：所有定义的 function 符号 - 所有被调用的 callee_name
        """
        cur = self._conn.execute(
            "SELECT s.* FROM symbols s "
            "WHERE s.kind = 'function' AND s.project_path = ? "
            "AND s.name NOT IN ("
            "  SELECT DISTINCT callee_name FROM callgraph WHERE project_path = ?"
            ") "
            "ORDER BY s.file_path, s.line_start",
            (project_path, project_path),
        )
        return [dict(r) for r in cur.fetchall()]

    def get_index_stats(self, project_path: str) -> dict:
        """获取索引统计信息"""
        sym_count = self._conn.execute(
            "SELECT COUNT(*) AS c FROM symbols WHERE project_path = ?",
            (project_path,),
        ).fetchone()["c"]
        call_count = self._conn.execute(
            "SELECT COUNT(*) AS c FROM callgraph WHERE project_path = ?",
            (project_path,),
        ).fetchone()["c"]
        file_count = self._conn.execute(
            "SELECT COUNT(DISTINCT file_path) AS c FROM symbols WHERE project_path = ?",
            (project_path,),
        ).fetchone()["c"]
        return {
            "symbols": sym_count,
            "calls": call_count,
            "files_indexed": file_count,
        }
