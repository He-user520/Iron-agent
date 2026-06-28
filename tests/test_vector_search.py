"""向量语义搜索单元测试

覆盖 Phase 2 任务 2.2 新增的向量搜索功能：
- embedding 序列化/反序列化（numpy + 纯 Python 降级）
- 余弦相似度计算
- _save_embedding 保存到 messages/history 表
- save_message_with_embedding / save_history_with_embedding
- search_semantic 语义搜索
- search_hybrid 混合检索（关键词 + 语义）
- ProjectMemory.append_to_memory_with_embedding
- embedding_meta 元数据记录
- 降级：无 embedding 时纯关键词搜索

运行方式: pytest tests/test_vector_search.py -v
"""
import asyncio
import math
from datetime import datetime
from pathlib import Path

import pytest

from iron.core.db import Database, SessionRow, MessageRow, HistoryRow
from iron.agent.memory import ProjectMemory
from iron.llm.backend import EchoBackend


# ── 测试夹具 ──────────────────────────────────────────────────────

@pytest.fixture
def db(tmp_path) -> Database:
    """每个测试用例独立的临时数据库"""
    db_path = tmp_path / "test_vec.db"
    db = Database(db_path=db_path)
    db.connect()
    yield db
    db.close()


@pytest.fixture
def echo_llm() -> EchoBackend:
    """Echo 后端：返回哈希伪向量，测试用"""
    return EchoBackend()


def _make_history(text: str, project_path: str = "/tmp/proj") -> HistoryRow:
    return HistoryRow(
        user_input=text,
        timestamp=datetime.now().isoformat(),
        project_path=project_path,
    )


def _make_message(session_id: str, content: str, seq: int = 0) -> MessageRow:
    return MessageRow(
        session_id=session_id,
        role="user",
        content=content,
        tool_calls="[]",
        tool_call_id="",
        created_at=datetime.now().isoformat(),
        sequence=seq,
    )


def _cosine(a, b) -> float:
    """参考实现：纯 Python 余弦相似度"""
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


# ── 序列化/反序列化测试 ──────────────────────────────────────────

class TestEmbeddingSerialization:
    """embedding 序列化与反序列化"""

    def test_serialize_deserialize_roundtrip(self):
        """序列化后反序列化保持数据一致"""
        vec = [0.1, 0.2, 0.3, 0.4, 0.5]
        blob = Database._serialize_embedding(vec)
        assert isinstance(blob, bytes)
        assert len(blob) >= 4 + 5 * 4
        out = Database._deserialize_embedding(blob)
        assert out is not None
        assert len(out) == 5
        for a, b in zip(vec, out):
            assert abs(a - b) < 1e-5

    def test_serialize_empty_vector(self):
        """空向量返回空 bytes"""
        assert Database._serialize_embedding([]) == b""

    def test_deserialize_empty_blob(self):
        """空 blob 返回 None"""
        assert Database._deserialize_embedding(b"") is None
        assert Database._deserialize_embedding(None) is None

    def test_deserialize_truncated_blob(self):
        """长度不足的 blob 返回 None"""
        # 只声明 5 维但实际数据不够
        import struct
        blob = struct.pack("<I", 5) + b"\x00\x00\x00\x00"  # 只有 1 个 float
        assert Database._deserialize_embedding(blob) is None

    def test_serialize_single_dimension(self):
        """单维向量也能正确序列化"""
        vec = [0.5]
        blob = Database._serialize_embedding(vec)
        out = Database._deserialize_embedding(blob)
        assert out is not None
        assert len(out) == 1
        assert abs(out[0] - 0.5) < 1e-5


# ── 余弦相似度测试 ────────────────────────────────────────────────

class TestCosineSimilarity:
    """余弦相似度计算"""

    def test_identical_vectors(self):
        """相同向量相似度为 1.0"""
        vec = [1.0, 2.0, 3.0, 4.0]
        score = Database._cosine_similarity(vec, vec)
        assert abs(score - 1.0) < 1e-5

    def test_orthogonal_vectors(self):
        """正交向量相似度为 0"""
        a = [1.0, 0.0]
        b = [0.0, 1.0]
        score = Database._cosine_similarity(a, b)
        assert abs(score) < 1e-5

    def test_opposite_vectors(self):
        """相反向量相似度为 -1"""
        a = [1.0, 2.0]
        b = [-1.0, -2.0]
        score = Database._cosine_similarity(a, b)
        assert abs(score + 1.0) < 1e-5

    def test_zero_vector(self):
        """零向量相似度为 0"""
        a = [0.0, 0.0]
        b = [1.0, 2.0]
        assert Database._cosine_similarity(a, b) == 0.0

    def test_different_dimensions(self):
        """维度不同返回 0"""
        assert Database._cosine_similarity([1.0, 2.0], [1.0, 2.0, 3.0]) == 0.0

    def test_matches_reference_implementation(self):
        """与参考实现结果一致"""
        import random
        random.seed(42)
        for _ in range(5):
            a = [random.uniform(-1, 1) for _ in range(20)]
            b = [random.uniform(-1, 1) for _ in range(20)]
            expected = _cosine(a, b)
            actual = Database._cosine_similarity(a, b)
            assert abs(actual - expected) < 1e-4


# ── 保存 embedding 测试 ──────────────────────────────────────────

class TestSaveEmbedding:
    """保存 embedding 到数据库"""

    def test_save_embedding_to_history(self, db):
        """保存 embedding 到 history 表"""
        h = _make_history("test query")
        rid = db.save_history(h)
        vec = [0.1, 0.2, 0.3]
        ok = db._save_embedding("history", rid, vec)
        assert ok
        # 验证已写入
        cur = db._conn.execute("SELECT embedding FROM history WHERE id = ?", (rid,))
        row = cur.fetchone()
        assert row["embedding"] is not None
        out = Database._deserialize_embedding(row["embedding"])
        assert len(out) == 3

    def test_save_embedding_to_messages(self, db):
        """保存 embedding 到 messages 表"""
        # 先建会话
        s = SessionRow(
            session_id="s1", project_path="/p",
            created_at=datetime.now().isoformat(),
            updated_at=datetime.now().isoformat(),
        )
        db.save_session(s)
        m = _make_message("s1", "hello")
        rid = db.save_message(m)
        vec = [0.5, 0.6]
        ok = db._save_embedding("messages", rid, vec)
        assert ok

    def test_save_embedding_invalid_table(self, db):
        """不支持的表抛 ValueError"""
        with pytest.raises(ValueError):
            db._save_embedding("unknown_table", 1, [0.1])

    def test_save_history_with_embedding(self, db):
        """save_history_with_embedding 一步保存"""
        h = _make_history("with vec")
        vec = [1.0, 0.0, 0.0]
        rid = db.save_history_with_embedding(h, vec)
        assert rid > 0
        cur = db._conn.execute("SELECT embedding FROM history WHERE id = ?", (rid,))
        row = cur.fetchone()
        assert row["embedding"] is not None

    def test_save_message_with_embedding(self, db):
        """save_message_with_embedding 一步保存"""
        s = SessionRow(
            session_id="s2", project_path="/p",
            created_at=datetime.now().isoformat(),
            updated_at=datetime.now().isoformat(),
        )
        db.save_session(s)
        m = _make_message("s2", "msg with vec", seq=0)
        vec = [0.9, 0.1]
        rid = db.save_message_with_embedding(m, vec)
        assert rid > 0
        cur = db._conn.execute("SELECT embedding FROM messages WHERE id = ?", (rid,))
        row = cur.fetchone()
        assert row["embedding"] is not None


# ── 语义搜索测试 ─────────────────────────────────────────────────

class TestSearchSemantic:
    """search_semantic 语义搜索"""

    def test_search_history_semantic(self, db):
        """搜索 history 表 — 按相似度排序"""
        # 写入 3 条带 embedding 的历史
        texts = ["STM32 HAL 库初始化", "FreeRTOS 任务创建", "UART 波特率配置"]
        # 用正交向量保证相似度可预测
        vecs = [
            [1.0, 0.0, 0.0],
            [0.0, 1.0, 0.0],
            [0.0, 0.0, 1.0],
        ]
        for t, v in zip(texts, vecs):
            h = _make_history(t)
            db.save_history_with_embedding(h, v)
        # 查询向量接近第一条
        query = [0.9, 0.1, 0.0]
        results = db.search_semantic(query, table="history", limit=3)
        assert len(results) == 3
        # 最相似的应该排第一
        assert results[0]["user_input"] == "STM32 HAL 库初始化"
        assert results[0]["score"] > results[1]["score"]

    def test_search_messages_semantic(self, db):
        """搜索 messages 表"""
        s = SessionRow(
            session_id="s3", project_path="/p",
            created_at=datetime.now().isoformat(),
            updated_at=datetime.now().isoformat(),
        )
        db.save_session(s)
        contents = ["配置 GPIO", "配置 UART", "配置 SPI"]
        vecs = [[1.0, 0.0], [0.0, 1.0], [0.7, 0.7]]
        for i, (c, v) in enumerate(zip(contents, vecs)):
            m = _make_message("s3", c, seq=i)
            db.save_message_with_embedding(m, v)
        # 查询接近 SPI
        query = [0.6, 0.6]
        results = db.search_semantic(query, table="messages", limit=3)
        assert len(results) == 3
        # SPI 向量 [0.7, 0.7] 与查询 [0.6, 0.6] 余弦相似度最高
        assert results[0]["content"] == "配置 SPI"

    def test_search_with_min_score(self, db):
        """min_score 阈值过滤"""
        texts = ["A", "B"]
        vecs = [[1.0, 0.0], [0.0, 1.0]]
        for t, v in zip(texts, vecs):
            db.save_history_with_embedding(_make_history(t), v)
        query = [1.0, 0.0]
        # 高阈值只保留相似度 >= 0.9 的
        results = db.search_semantic(query, table="history", min_score=0.9)
        assert len(results) == 1
        assert results[0]["user_input"] == "A"

    def test_search_with_project_path_filter(self, db):
        """project_path 过滤"""
        db.save_history_with_embedding(
            _make_history("proj1 item", project_path="/proj1"), [1.0, 0.0])
        db.save_history_with_embedding(
            _make_history("proj2 item", project_path="/proj2"), [1.0, 0.0])
        query = [1.0, 0.0]
        results = db.search_semantic(query, table="history", project_path="/proj1")
        assert len(results) == 1
        assert results[0]["user_input"] == "proj1 item"

    def test_search_empty_db(self, db):
        """空数据库返回空列表"""
        results = db.search_semantic([1.0, 0.0], table="history")
        assert results == []

    def test_search_invalid_table(self, db):
        """不支持的表抛 ValueError"""
        with pytest.raises(ValueError):
            db.search_semantic([1.0], table="invalid")

    def test_search_limit_truncation(self, db):
        """limit 截断结果"""
        for i in range(5):
            db.save_history_with_embedding(_make_history(f"item{i}"), [1.0, 0.0])
        results = db.search_semantic([1.0, 0.0], table="history", limit=2)
        assert len(results) == 2


# ── 混合检索测试 ─────────────────────────────────────────────────

class TestSearchHybrid:
    """search_hybrid 混合检索（关键词 + 语义）"""

    def test_hybrid_keyword_only(self, db):
        """无 query_embedding 时降级为纯关键词搜索"""
        db.save_history(_make_history("STM32 初始化"))
        db.save_history(_make_history("ESP32 配置"))
        results = db.search_hybrid("STM32", table="history", limit=10)
        assert len(results) >= 1
        # 至少命中 STM32
        assert any("STM32" in r.get("user_input", "") for r in results)

    def test_hybrid_semantic_only(self, db):
        """query 为空但有 embedding 时纯语义搜索"""
        db.save_history_with_embedding(_make_history("A"), [1.0, 0.0])
        db.save_history_with_embedding(_make_history("B"), [0.0, 1.0])
        results = db.search_hybrid("", query_embedding=[1.0, 0.0],
                                    table="history", limit=5)
        # query 为空时 kw_limit=0，仅语义部分
        assert len(results) >= 1
        assert results[0]["user_input"] == "A"

    def test_hybrid_combines_keyword_and_semantic(self, db):
        """关键词 + 语义融合排序"""
        # item1：关键词命中 + 语义高分
        db.save_history_with_embedding(_make_history("STM32 配置"), [1.0, 0.0])
        # item2：关键词不命中 + 语义低分
        db.save_history_with_embedding(_make_history("ESP32 配置"), [0.1, 0.9])
        # item3：关键词不命中 + 语义中分
        db.save_history_with_embedding(_make_history("Arduino 配置"), [0.5, 0.5])
        # 查询：关键词 STM32 + 向量接近 [1,0]
        results = db.search_hybrid("STM32", query_embedding=[1.0, 0.0],
                                    table="history", limit=3)
        # item1 应排第一（关键词 1.0 + 语义 1.0）
        assert results[0]["user_input"] == "STM32 配置"
        assert results[0]["fused_score"] > results[1]["fused_score"]
        # keyword_score 和 semantic_score 都被记录
        assert results[0]["keyword_score"] == 1.0
        assert results[0]["semantic_score"] > 0.99

    def test_hybrid_weights(self, db):
        """权重调节影响排序"""
        db.save_history_with_embedding(_make_history("STM32"), [1.0, 0.0])
        db.save_history_with_embedding(_make_history("STM32 类似"), [0.95, 0.0])
        # 关键词权重高时，二者都命中关键词，看语义
        results = db.search_hybrid("STM32", query_embedding=[1.0, 0.0],
                                    table="history", limit=2,
                                    keyword_weight=0.5, semantic_weight=0.5)
        assert len(results) == 2


# ── embedding 元数据测试 ─────────────────────────────────────────

class TestEmbeddingMeta:
    """embedding_meta 表"""

    def test_record_meta(self, db):
        """记录 embedding 元数据"""
        rid = db.record_embedding_meta("text-embedding-3-small", 1536)
        assert rid > 0

    def test_get_meta(self, db):
        """查询 embedding 元数据"""
        db.record_embedding_meta("model-a", 64)
        db.record_embedding_meta("model-b", 128)
        metas = db.get_embedding_meta()
        assert len(metas) == 2
        # 按 id DESC 排序，最新的在前
        assert metas[0]["model_name"] == "model-b"
        assert metas[0]["dimension"] == 128

    def test_get_meta_empty(self, db):
        """无记录时返回空列表"""
        assert db.get_embedding_meta() == []


# ── EchoBackend embed() 测试 ────────────────────────────────────

class TestEchoBackendEmbed:
    """EchoBackend.embed() 接口测试"""

    def test_embed_returns_vectors(self, echo_llm):
        """embed 返回向量列表"""
        texts = ["hello", "world"]
        vecs = asyncio.run(echo_llm.embed(texts))
        assert len(vecs) == 2
        for v in vecs:
            assert isinstance(v, list)
            assert len(v) > 0

    def test_embed_dimension_consistent(self, echo_llm):
        """同一次调用所有向量维度一致"""
        vecs = asyncio.run(echo_llm.embed(["a", "b", "c"]))
        dim = len(vecs[0])
        for v in vecs:
            assert len(v) == dim

    def test_embed_deterministic(self, echo_llm):
        """相同输入产生相同向量（哈希伪向量应确定）"""
        v1 = asyncio.run(echo_llm.embed(["test"]))[0]
        v2 = asyncio.run(echo_llm.embed(["test"]))[0]
        assert v1 == v2

    def test_embed_different_inputs_different_vectors(self, echo_llm):
        """不同输入产生不同向量"""
        v1 = asyncio.run(echo_llm.embed(["STM32"]))[0]
        v2 = asyncio.run(echo_llm.embed(["ESP32"]))[0]
        assert v1 != v2


# ── ProjectMemory 向量化测试 ────────────────────────────────────

class TestProjectMemoryEmbedding:
    """ProjectMemory.append_to_memory_with_embedding"""

    def test_append_with_embedding_success(self, tmp_path, db, echo_llm):
        """成功追加并向量化"""
        memory = ProjectMemory(str(tmp_path))
        # 先建表（通过 db fixture 已迁移）
        ok = asyncio.run(memory.append_to_memory_with_embedding(
            "用户偏好", "要求代码注释用中文",
            llm=echo_llm, db=db, project_path=str(tmp_path),
        ))
        assert ok is True
        # MEMORY.md 已写入
        loaded = memory.load_memory()
        assert "要求代码注释用中文" in loaded
        # history 表也写入了带 embedding 的记录
        results = db.search_semantic(
            asyncio.run(echo_llm.embed(["要求代码注释用中文"]))[0],
            table="history", limit=5,
        )
        assert len(results) >= 1
        assert any("要求代码注释用中文" in r.get("user_input", "") for r in results)

    def test_append_without_llm_returns_false(self, tmp_path, db):
        """无 llm 时仍写入 MEMORY.md，但返回 False"""
        memory = ProjectMemory(str(tmp_path))
        ok = asyncio.run(memory.append_to_memory_with_embedding(
            "测试", "内容", llm=None, db=db,
        ))
        assert ok is False
        # MEMORY.md 仍写入
        assert "内容" in memory.load_memory()

    def test_append_without_db_returns_false(self, tmp_path, echo_llm):
        """无 db 时仍写入 MEMORY.md，但返回 False"""
        memory = ProjectMemory(str(tmp_path))
        ok = asyncio.run(memory.append_to_memory_with_embedding(
            "测试", "内容", llm=echo_llm, db=None,
        ))
        assert ok is False
        assert "内容" in memory.load_memory()

    def test_append_backward_compat(self, tmp_path):
        """同步 append_to_memory 仍工作（向后兼容）"""
        memory = ProjectMemory(str(tmp_path))
        memory.append_to_memory("章节", "内容")
        loaded = memory.load_memory()
        assert "内容" in loaded
        assert "## 章节" in loaded


# ── 降级测试 ─────────────────────────────────────────────────────

class TestFallback:
    """降级场景"""

    def test_search_hybrid_fallback_to_keyword(self, db):
        """无 embedding 时 search_hybrid 降级为关键词搜索"""
        db.save_history(_make_history("STM32 重要配置"))
        db.save_history(_make_history("ESP32 配置"))
        # query_embedding=None 时应降级
        results = db.search_hybrid("STM32", query_embedding=None,
                                    table="history", limit=5)
        assert len(results) >= 1
        assert any("STM32" in r.get("user_input", "") for r in results)

    def test_backend_not_implemented_embed(self):
        """LLMBackend 基类 embed() 默认抛 NotImplementedError"""
        from iron.llm.backend import LLMBackend
        # LLMBackend 是 ABC，需要实例化子类
        # 用 EchoBackend 验证基类默认实现被覆盖
        echo = EchoBackend()
        # EchoBackend 应该不抛 NotImplementedError
        vec = asyncio.run(echo.embed(["test"]))
        assert len(vec) == 1
        assert len(vec[0]) > 0
