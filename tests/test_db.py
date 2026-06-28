"""SQLite 持久化层单元测试

覆盖 iron.core.db.Database 的核心功能：
- 连接和关闭
- 表创建（自动迁移）
- 会话 CRUD
- 消息保存/读取/批量/计数
- 历史 CRUD + 搜索
- 统计信息
- 事务提交/回滚
- WAL 模式启用

运行方式: pytest tests/test_db.py -v
"""
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from iron.core.db import Database, SessionRow, MessageRow, HistoryRow


# ── 测试夹具 ──────────────────────────────────────────────────────

@pytest.fixture
def db(tmp_path) -> Database:
    """每个测试用例独立的临时数据库"""
    db_path = tmp_path / "test_iron.db"
    db = Database(db_path=db_path)
    db.connect()
    yield db
    db.close()


def _make_session(session_id: str = "sess-001", project_path: str = "/tmp/proj") -> SessionRow:
    """构造一个测试会话行"""
    now = datetime.now().isoformat()
    return SessionRow(
        session_id=session_id,
        project_path=project_path,
        created_at=now,
        updated_at=now,
        message_count=0,
        metadata='{"mcu":"stm32"}',
    )


def _make_message(session_id: str, seq: int = 0, role: str = "user",
                  content: str = "hello") -> MessageRow:
    """构造一条测试消息"""
    return MessageRow(
        session_id=session_id,
        role=role,
        content=content,
        tool_calls="[]",
        tool_call_id="",
        created_at=datetime.now().isoformat(),
        sequence=seq,
    )


# ── 连接/关闭 ────────────────────────────────────────────────────

class TestConnectClose:
    """连接和关闭"""

    def test_connect_close(self, tmp_path):
        """连接后连接对象有效，关闭后置 None"""
        db = Database(db_path=tmp_path / "t.db")
        db.connect()
        assert db._conn is not None
        # 验证可执行 SQL
        cur = db._conn.execute("SELECT 1")
        assert cur.fetchone()[0] == 1
        db.close()
        assert db._conn is None

    def test_context_manager(self, tmp_path):
        """with 语法自动连接和关闭"""
        db_path = tmp_path / "ctx.db"
        with Database(db_path=db_path) as db:
            assert db._conn is not None
        # 退出 with 后连接已关闭
        assert db._conn is None

    def test_db_file_created(self, tmp_path):
        """连接后数据库文件被创建"""
        db_path = tmp_path / "created.db"
        with Database(db_path=db_path) as _:
            pass
        assert db_path.exists()
        assert db_path.stat().st_size > 0


# ── 表创建/迁移 ──────────────────────────────────────────────────

class TestSchema:
    """表结构创建与迁移"""

    def test_create_tables(self, db):
        """sessions/messages/history/schema_version 表都被创建"""
        cur = db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        names = [r[0] for r in cur.fetchall()]
        assert "sessions" in names
        assert "messages" in names
        assert "history" in names
        assert "schema_version" in names

    def test_migrate_idempotent(self, tmp_path):
        """重复调用迁移不会报错"""
        db = Database(db_path=tmp_path / "idem.db")
        db.connect()
        db._migrate()  # 再次迁移
        db._migrate()  # 第三次
        # 表仍存在
        cur = db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sessions'"
        )
        assert cur.fetchone() is not None
        db.close()

    def test_indexes_created(self, db):
        """索引被创建"""
        cur = db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' ORDER BY name"
        )
        names = [r[0] for r in cur.fetchall()]
        # 至少包含我们定义的索引
        assert "idx_sessions_project_path" in names
        assert "idx_messages_session_id" in names
        assert "idx_history_project_path" in names

    def test_schema_version_recorded(self, db):
        """schema_version 表记录了已应用的迁移版本"""
        cur = db._conn.execute("SELECT version FROM schema_version ORDER BY version")
        versions = [r[0] for r in cur.fetchall()]
        assert 1 in versions  # 001_initial.sql 已应用


# ── Session CRUD ─────────────────────────────────────────────────

class TestSessionCRUD:
    """会话 CRUD"""

    def test_save_get_session(self, db):
        """保存后能读取"""
        s = _make_session(session_id="abc-123", project_path="/p")
        row_id = db.save_session(s)
        assert row_id > 0
        loaded = db.get_session("abc-123")
        assert loaded is not None
        assert loaded.session_id == "abc-123"
        assert loaded.project_path == "/p"
        assert loaded.metadata == '{"mcu":"stm32"}'

    def test_get_session_nonexistent(self, db):
        """获取不存在的会话返回 None"""
        assert db.get_session("no-such-id") is None

    def test_save_session_upsert(self, db):
        """相同 session_id 再次保存是更新而非插入"""
        s = _make_session(session_id="up-1")
        db.save_session(s)
        # 修改后再次保存
        s.message_count = 10
        s.metadata = '{"k":"v"}'
        db.save_session(s)
        loaded = db.get_session("up-1")
        assert loaded.message_count == 10
        assert loaded.metadata == '{"k":"v"}'
        # 仍只有一条记录
        cur = db._conn.execute("SELECT COUNT(*) FROM sessions WHERE session_id = ?", ("up-1",))
        assert cur.fetchone()[0] == 1

    def test_list_sessions_all(self, db):
        """列出所有会话"""
        for i in range(3):
            db.save_session(_make_session(session_id=f"s{i}", project_path="/p"))
        sessions = db.list_sessions()
        assert len(sessions) == 3

    def test_list_sessions_by_project(self, db):
        """按项目路径筛选"""
        db.save_session(_make_session(session_id="a", project_path="/p1"))
        db.save_session(_make_session(session_id="b", project_path="/p2"))
        db.save_session(_make_session(session_id="c", project_path="/p1"))
        result = db.list_sessions(project_path="/p1")
        assert len(result) == 2
        for s in result:
            assert s.project_path == "/p1"

    def test_list_sessions_limit(self, db):
        """limit 参数生效"""
        for i in range(5):
            db.save_session(_make_session(session_id=f"s{i}"))
        result = db.list_sessions(limit=2)
        assert len(result) == 2

    def test_delete_session(self, db):
        """删除会话"""
        db.save_session(_make_session(session_id="del-1"))
        assert db.delete_session("del-1") is True
        assert db.get_session("del-1") is None

    def test_delete_session_nonexistent(self, db):
        """删除不存在的会话返回 False"""
        assert db.delete_session("no-such") is False

    def test_delete_session_cascades_messages(self, db):
        """删除会话级联删除其消息"""
        sid = "cascade-1"
        db.save_session(_make_session(session_id=sid))
        db.save_message(_make_message(session_id=sid, seq=0))
        db.save_message(_make_message(session_id=sid, seq=1))
        assert db.get_message_count(sid) == 2
        # 删除会话
        assert db.delete_session(sid) is True
        # 消息也应被级联删除
        assert db.get_message_count(sid) == 0


# ── Message CRUD ─────────────────────────────────────────────────

class TestMessageCRUD:
    """消息 CRUD"""

    def test_save_get_messages(self, db):
        """保存和获取消息（按 sequence 排序）"""
        sid = "msg-1"
        db.save_session(_make_session(session_id=sid))
        # 故意乱序保存
        db.save_message(_make_message(session_id=sid, seq=2, content="third"))
        db.save_message(_make_message(session_id=sid, seq=0, content="first"))
        db.save_message(_make_message(session_id=sid, seq=1, content="second"))
        msgs = db.get_messages(sid)
        assert len(msgs) == 3
        assert msgs[0].sequence == 0
        assert msgs[0].content == "first"
        assert msgs[1].sequence == 1
        assert msgs[2].sequence == 2

    def test_get_messages_empty(self, db):
        """空会话返回空列表"""
        db.save_session(_make_session(session_id="empty"))
        assert db.get_messages("empty") == []

    def test_get_messages_nonexistent_session(self, db):
        """不存在的会话返回空列表"""
        assert db.get_messages("no-such") == []

    def test_save_messages_batch(self, db):
        """批量保存消息（替换现有）"""
        sid = "batch-1"
        db.save_session(_make_session(session_id=sid))
        # 先插一条
        db.save_message(_make_message(session_id=sid, seq=0, content="old"))
        assert db.get_message_count(sid) == 1
        # 批量替换
        messages = [
            {"role": "user", "content": "hello", "timestamp": "2026-01-01T00:00:00"},
            {"role": "assistant", "content": "hi", "timestamp": "2026-01-01T00:00:01"},
            {"role": "user", "content": "bye", "timestamp": "2026-01-01T00:00:02",
             "tool_calls": [{"id": "tc1", "function": {"name": "f"}}]},
        ]
        count = db.save_messages(sid, messages)
        assert count == 3
        assert db.get_message_count(sid) == 3
        msgs = db.get_messages(sid)
        assert msgs[0].role == "user"
        assert msgs[0].content == "hello"
        # tool_calls 被序列化为 JSON
        assert '"tc1"' in msgs[2].tool_calls

    def test_save_messages_batch_replaces(self, db):
        """批量保存会替换原有消息"""
        sid = "repl-1"
        db.save_session(_make_session(session_id=sid))
        db.save_messages(sid, [{"role": "user", "content": "v1"}])
        db.save_messages(sid, [{"role": "user", "content": "v2"}])
        msgs = db.get_messages(sid)
        assert len(msgs) == 1
        assert msgs[0].content == "v2"

    def test_save_messages_batch_empty(self, db):
        """批量保存空列表会清空现有消息"""
        sid = "empty-batch"
        db.save_session(_make_session(session_id=sid))
        db.save_messages(sid, [{"role": "user", "content": "v1"}])
        assert db.get_message_count(sid) == 1
        count = db.save_messages(sid, [])
        assert count == 0
        assert db.get_message_count(sid) == 0

    def test_get_message_count(self, db):
        """消息计数"""
        sid = "cnt-1"
        db.save_session(_make_session(session_id=sid))
        assert db.get_message_count(sid) == 0
        for i in range(5):
            db.save_message(_make_message(session_id=sid, seq=i))
        assert db.get_message_count(sid) == 5


# ── History CRUD ─────────────────────────────────────────────────

class TestHistoryCRUD:
    """历史输入 CRUD"""

    def test_save_get_history(self, db):
        """保存后能获取"""
        db.save_history(HistoryRow(
            user_input="hello world",
            timestamp="2026-01-01T00:00:00",
            project_path="/p",
        ))
        history = db.get_history(project_path="/p", limit=10)
        assert len(history) == 1
        assert history[0].user_input == "hello world"
        assert history[0].project_path == "/p"

    def test_get_history_order_desc(self, db):
        """历史按 timestamp 倒序"""
        db.save_history(HistoryRow(user_input="old", timestamp="2026-01-01T00:00:00"))
        db.save_history(HistoryRow(user_input="new", timestamp="2026-06-01T00:00:00"))
        db.save_history(HistoryRow(user_input="mid", timestamp="2026-03-01T00:00:00"))
        history = db.get_history(limit=10)
        assert history[0].user_input == "new"
        assert history[1].user_input == "mid"
        assert history[2].user_input == "old"

    def test_get_history_all_projects(self, db):
        """不指定 project_path 返回全部"""
        db.save_history(HistoryRow(user_input="a", timestamp="2026-01-01", project_path="/p1"))
        db.save_history(HistoryRow(user_input="b", timestamp="2026-01-02", project_path="/p2"))
        history = db.get_history(limit=10)
        assert len(history) == 2

    def test_search_history(self, db):
        """LIKE 搜索"""
        db.save_history(HistoryRow(user_input="help me flash", timestamp="2026-01-01"))
        db.save_history(HistoryRow(user_input="build project", timestamp="2026-01-02"))
        db.save_history(HistoryRow(user_input="flash firmware", timestamp="2026-01-03"))
        results = db.search_history("flash", limit=10)
        assert len(results) == 2
        for r in results:
            assert "flash" in r.user_input

    def test_search_history_no_match(self, db):
        """无匹配返回空"""
        db.save_history(HistoryRow(user_input="hello", timestamp="2026-01-01"))
        assert db.search_history("nonexistent") == []

    def test_clear_history_by_project(self, db):
        """按项目清空历史"""
        db.save_history(HistoryRow(user_input="a", timestamp="2026-01-01", project_path="/p1"))
        db.save_history(HistoryRow(user_input="b", timestamp="2026-01-02", project_path="/p2"))
        deleted = db.clear_history(project_path="/p1")
        assert deleted == 1
        assert len(db.get_history()) == 1
        assert db.get_history()[0].project_path == "/p2"

    def test_clear_history_all(self, db):
        """清空全部历史"""
        db.save_history(HistoryRow(user_input="a", timestamp="2026-01-01", project_path="/p1"))
        db.save_history(HistoryRow(user_input="b", timestamp="2026-01-02", project_path="/p2"))
        deleted = db.clear_history()
        assert deleted == 2
        assert db.get_history() == []

    def test_clear_history_empty(self, db):
        """清空空表返回 0"""
        assert db.clear_history() == 0


# ── 统计 ──────────────────────────────────────────────────────────

class TestStats:
    """数据库统计"""

    def test_get_stats_empty(self, db):
        """空数据库统计"""
        stats = db.get_stats()
        assert stats["sessions"] == 0
        assert stats["messages"] == 0
        assert stats["history"] == 0
        assert stats["db_size"] > 0  # 表结构已创建，文件非空
        assert "db_path" in stats

    def test_get_stats_with_data(self, db):
        """有数据后统计正确"""
        sid = "stat-1"
        db.save_session(_make_session(session_id=sid))
        db.save_message(_make_message(session_id=sid, seq=0))
        db.save_message(_make_message(session_id=sid, seq=1))
        db.save_history(HistoryRow(user_input="hi", timestamp="2026-01-01"))
        stats = db.get_stats()
        assert stats["sessions"] == 1
        assert stats["messages"] == 2
        assert stats["history"] == 1


# ── 事务 ──────────────────────────────────────────────────────────

class TestTransaction:
    """事务上下文管理器"""

    def test_transaction_commit(self, db):
        """正常退出事务自动提交"""
        with db.transaction() as conn:
            conn.execute(
                "INSERT INTO history (user_input, timestamp, project_path) VALUES (?, ?, ?)",
                ("committed", "2026-01-01", "/p"),
            )
        # 在事务外查询应能读到
        history = db.get_history()
        assert len(history) == 1
        assert history[0].user_input == "committed"

    def test_transaction_rollback(self, db):
        """事务中抛异常自动回滚"""
        try:
            with db.transaction() as conn:
                conn.execute(
                    "INSERT INTO history (user_input, timestamp, project_path) VALUES (?, ?, ?)",
                    ("will-rollback", "2026-01-01", "/p"),
                )
                raise RuntimeError("故意失败")
        except RuntimeError:
            pass
        # 回滚后应查不到数据
        history = db.get_history()
        assert len(history) == 0

    def test_transaction_returns_connection(self, db):
        """事务 yield 的是有效的 sqlite3.Connection"""
        with db.transaction() as conn:
            assert isinstance(conn, sqlite3.Connection)
            cur = conn.execute("SELECT 1")
            assert cur.fetchone()[0] == 1

    def test_transaction_without_connect_raises(self, tmp_path):
        """未连接时调用 transaction 应抛 RuntimeError"""
        db = Database(db_path=tmp_path / "x.db")
        with pytest.raises(RuntimeError):
            with db.transaction() as _:
                pass


# ── WAL 模式 ──────────────────────────────────────────────────────

class TestWALMode:
    """WAL 模式启用"""

    def test_wal_mode_enabled(self, tmp_path):
        """连接后 journal_mode 应为 wal"""
        db = Database(db_path=tmp_path / "wal.db")
        db.connect()
        try:
            cur = db._conn.execute("PRAGMA journal_mode")
            mode = cur.fetchone()[0]
            assert mode.lower() == "wal"
        finally:
            db.close()

    def test_foreign_keys_enabled(self, db):
        """外键约束已启用"""
        cur = db._conn.execute("PRAGMA foreign_keys")
        assert cur.fetchone()[0] == 1


# ── ConversationSession 集成测试 ──────────────────────────────────

class TestConversationIntegration:
    """ConversationSession 与 Database 集成"""

    def test_save_with_db_then_load_from_db(self, tmp_path):
        """通过 db 保存后能从 db 加载"""
        from iron.agent.conversation import ConversationSession
        db_path = tmp_path / "conv.db"
        sessions_dir = tmp_path / "sessions"
        with Database(db_path=db_path) as db:
            session = ConversationSession(
                mcu="stm32f407",
                project_dir="/test/proj",
            )
            session.id = "conv-int-1"
            session.created_at = "2026-01-01T00:00:00"
            session.add_message("user", "hello")
            session.add_message("assistant", "hi there")
            # 保存到 JSON + SQLite
            session.save(sessions_dir, db=db)
            # 验证 SQLite 中能查到
            assert db.get_session("conv-int-1") is not None
            assert db.get_message_count("conv-int-1") == 2

            # 删除 JSON 文件，仅从 SQLite 加载
            json_file = sessions_dir / "conv-int-1.json"
            assert json_file.exists()
            json_file.unlink()
            # 加载应回退到 SQLite
            loaded = ConversationSession.load(path=json_file, db=db)
            assert loaded.id == "conv-int-1"
            assert loaded.mcu == "stm32f407"
            assert loaded.project_dir == "/test/proj"
            assert len(loaded.messages) == 2
            assert loaded.messages[0]["role"] == "user"
            assert loaded.messages[0]["content"] == "hello"
            assert loaded.messages[1]["role"] == "assistant"

    def test_save_without_db_keeps_json_only(self, tmp_path):
        """不传 db 时仅保存 JSON（向后兼容）"""
        from iron.agent.conversation import ConversationSession
        sessions_dir = tmp_path / "sessions"
        session = ConversationSession(mcu="stm32", project_dir="/p")
        session.id = "json-only-1"
        session.add_message("user", "hi")
        path = session.save(sessions_dir)
        assert path.exists()
        # JSON 文件可正常加载
        loaded = ConversationSession.load(path=path)
        assert loaded.id == "json-only-1"
        assert loaded.mcu == "stm32"
        assert len(loaded.messages) == 1

    def test_load_fallback_to_db_when_json_missing(self, tmp_path):
        """JSON 文件不存在时回退到 db 加载"""
        from iron.agent.conversation import ConversationSession
        db_path = tmp_path / "fallback.db"
        sessions_dir = tmp_path / "sessions"
        with Database(db_path=db_path) as db:
            session = ConversationSession(mcu="stm32", project_dir="/p")
            session.id = "fallback-1"
            session.created_at = "2026-01-01T00:00:00"
            session.add_message("user", "test")
            session.save(sessions_dir, db=db)
            json_file = sessions_dir / "fallback-1.json"
            json_file.unlink()
            # 通过文件名提取 session_id 加载
            loaded = ConversationSession.load(path=json_file, db=db)
            assert loaded.id == "fallback-1"
            assert loaded.messages[0]["content"] == "test"
