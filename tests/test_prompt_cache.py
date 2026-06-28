"""P1-3: 系统提示分块缓存单元测试 — 覆盖 iron/llm/prompt_cache.py + backend 集成

运行方式: pytest tests/test_prompt_cache.py -v

测试用例：
- test_split_prompt: 测试系统提示分块
- test_cache_hit: 测试相同内容命中
- test_cache_miss: 测试不同内容未命中
- test_cache_ttl_expiry: 测试 TTL 过期
- test_cache_invalidate: 测试缓存失效
- test_cache_stats: 测试统计信息
- test_concurrent_access: 测试并发访问安全（threading.Lock 保护）
- test_integration_with_backend: 测试与 OpenAIBackend 集成
"""
import asyncio
import threading
import time

import httpx
import pytest

from iron.llm.prompt_cache import CachedPromptBlock, PromptCache
from iron.llm.backend import OpenAIBackend


# ── split_prompt 分块测试 ─────────────────────────────────────────

class TestSplitPrompt:
    """测试系统提示分块逻辑"""

    def test_split_prompt_with_separator(self):
        """包含分隔符的系统提示应被切分为两个块（Block A / Block B）"""
        cache = PromptCache(ttl_seconds=300)
        system = (
            "你是 Iron 嵌入式助手。\n"
            "## 工具说明\n"
            "可用工具: write_file, read_file\n"
            "\n"
            "## 当前环境\n"
            "- 目标 MCU: STM32F407\n"
            "- 项目目录: ./project\n"
        )
        blocks = cache.split_prompt(system)
        # 应切分为 2 个块
        assert len(blocks) == 2
        # 每个块都有 role / content / cache_key
        for blk in blocks:
            assert blk["role"] == "system"
            assert isinstance(blk["content"], str) and blk["content"]
            assert isinstance(blk["cache_key"], str) and len(blk["cache_key"]) == 16
        # Block A 应包含分隔符之前的内容（核心指令）
        assert "工具说明" in blocks[0]["content"]
        # Block B 应包含分隔符及之后的内容（项目配置）
        assert "## 当前环境" in blocks[1]["content"]
        assert "STM32F407" in blocks[1]["content"]
        # 两个块的 cache_key 应不同（内容不同）
        assert blocks[0]["cache_key"] != blocks[1]["cache_key"]

    def test_split_prompt_no_separator_returns_single_block(self):
        """无分隔符的系统提示应整体作为一个块返回"""
        cache = PromptCache(ttl_seconds=300)
        system = "你是一个简单的助手，没有任何分隔符。"
        blocks = cache.split_prompt(system)
        assert len(blocks) == 1
        assert blocks[0]["content"] == system
        assert len(blocks[0]["cache_key"]) == 16

    def test_split_prompt_empty_input(self):
        """空字符串输入应返回空列表"""
        cache = PromptCache(ttl_seconds=300)
        assert cache.split_prompt("") == []

    def test_split_prompt_cache_key_is_sha256_prefix(self):
        """cache_key 应为内容 SHA256 的前 16 位（hex）"""
        import hashlib
        cache = PromptCache(ttl_seconds=300)
        system = "测试内容"
        blocks = cache.split_prompt(system)
        expected = hashlib.sha256(system.encode("utf-8")).hexdigest()[:16]
        assert blocks[0]["cache_key"] == expected


# ── 缓存命中 / 未命中测试 ──────────────────────────────────────────

class TestCacheHitAndMiss:
    """测试缓存命中与未命中逻辑"""

    def test_cache_hit(self):
        """相同内容第二次查询应命中（hit_count > 0）"""
        cache = PromptCache(ttl_seconds=300)
        content = "核心指令：铁律 + 工具说明"
        # 第一次：未命中
        block1 = cache.get_or_create(content)
        assert block1.hit_count == 0
        assert block1.content == content
        # 第二次：命中
        block2 = cache.get_or_create(content)
        assert block2.hit_count == 1
        assert block2.cache_key == block1.cache_key

    def test_cache_miss_different_content(self):
        """不同内容应未命中（各自独立的 cache_key，hit_count=0）"""
        cache = PromptCache(ttl_seconds=300)
        block_a = cache.get_or_create("内容 A")
        block_b = cache.get_or_create("内容 B")
        assert block_a.cache_key != block_b.cache_key
        assert block_a.hit_count == 0
        assert block_b.hit_count == 0
        # 再次查询 A，应命中
        block_a2 = cache.get_or_create("内容 A")
        assert block_a2.hit_count == 1


# ── TTL 过期测试 ──────────────────────────────────────────────────

class TestCacheTTLExpiry:
    """测试缓存 TTL 过期机制"""

    def test_cache_ttl_expiry(self):
        """TTL 过期后应重新创建块（hit_count 归零）"""
        # 用极短 TTL（0.05 秒）测试过期
        cache = PromptCache(ttl_seconds=0)
        # ttl_seconds=0 会导致立即过期，用 patch 时间来测试更稳定
        # 这里用 1 秒 TTL + 等待来测试
        cache = PromptCache(ttl_seconds=1)
        content = "TTL 测试内容"
        # 第一次：创建
        block1 = cache.get_or_create(content)
        assert block1.hit_count == 0
        # 第二次：命中
        block2 = cache.get_or_create(content)
        assert block2.hit_count == 1
        # 等待 TTL 过期
        time.sleep(1.2)
        # 第三次：应过期重建（hit_count 归零）
        block3 = cache.get_or_create(content)
        assert block3.hit_count == 0
        assert block3.cache_key == block1.cache_key  # 同内容同 key


# ── 缓存失效测试 ──────────────────────────────────────────────────

class TestCacheInvalidate:
    """测试缓存失效机制"""

    def test_invalidate_all(self):
        """invalidate() 无参数时清空所有缓存"""
        cache = PromptCache(ttl_seconds=300)
        cache.get_or_create("内容 A")
        cache.get_or_create("内容 B")
        assert cache.stats()["total_blocks"] == 2
        cache.invalidate()
        assert cache.stats()["total_blocks"] == 0
        # 再次查询应未命中
        block = cache.get_or_create("内容 A")
        assert block.hit_count == 0

    def test_invalidate_specific_key(self):
        """invalidate(cache_key) 仅删除指定块"""
        cache = PromptCache(ttl_seconds=300)
        block_a = cache.get_or_create("内容 A")
        cache.get_or_create("内容 B")
        assert cache.stats()["total_blocks"] == 2
        # 删除 A
        cache.invalidate(block_a.cache_key)
        assert cache.stats()["total_blocks"] == 1
        # B 仍在，应命中
        block_b2 = cache.get_or_create("内容 B")
        assert block_b2.hit_count == 1
        # A 已删除，应未命中
        block_a2 = cache.get_or_create("内容 A")
        assert block_a2.hit_count == 0

    def test_invalidate_nonexistent_key_no_error(self):
        """invalidate 不存在的 key 不报错"""
        cache = PromptCache(ttl_seconds=300)
        cache.get_or_create("内容 A")
        # 不存在的 key 不应抛异常
        cache.invalidate("nonexistent_key_12345")
        assert cache.stats()["total_blocks"] == 1


# ── 统计信息测试 ──────────────────────────────────────────────────

class TestCacheStats:
    """测试缓存统计信息"""

    def test_stats_initial(self):
        """初始状态统计全为零"""
        cache = PromptCache(ttl_seconds=300)
        stats = cache.stats()
        assert stats["total_blocks"] == 0
        assert stats["total_hits"] == 0
        assert stats["total_queries"] == 0
        assert stats["hit_rate"] == 0.0

    def test_stats_after_operations(self):
        """若干次查询后统计正确"""
        cache = PromptCache(ttl_seconds=300)
        cache.get_or_create("A")  # miss
        cache.get_or_create("A")  # hit
        cache.get_or_create("A")  # hit
        cache.get_or_create("B")  # miss
        stats = cache.stats()
        assert stats["total_blocks"] == 2
        assert stats["total_queries"] == 4
        assert stats["total_hits"] == 2
        # 命中率 = 2/4 = 0.5
        assert stats["hit_rate"] == 0.5


# ── 并发访问安全测试 ──────────────────────────────────────────────

class TestConcurrentAccess:
    """测试并发访问安全性（threading.Lock 保护）"""

    def test_concurrent_access_safe(self):
        """多线程并发 get_or_create 不应导致数据竞争或异常"""
        cache = PromptCache(ttl_seconds=300)
        content = "并发测试内容"
        errors = []

        def worker():
            try:
                for _ in range(100):
                    cache.get_or_create(content)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # 不应有异常
        assert errors == []
        # 总查询次数 = 8 线程 × 100 次 = 800
        stats = cache.stats()
        assert stats["total_queries"] == 800
        # 命中次数 = 800 - 1（首次 miss）= 799
        assert stats["total_hits"] == 799
        # 只有一个缓存块
        assert stats["total_blocks"] == 1

    def test_concurrent_invalidate_safe(self):
        """并发 get_or_create + invalidate 不崩溃"""
        cache = PromptCache(ttl_seconds=300)
        errors = []

        def reader():
            try:
                for _ in range(50):
                    cache.get_or_create("内容 X")
            except Exception as e:
                errors.append(e)

        def invalidator():
            try:
                for _ in range(50):
                    cache.invalidate()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=reader) for _ in range(4)]
        threads += [threading.Thread(target=invalidator) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []


# ── 与 OpenAIBackend 集成测试 ────────────────────────────────────

class TestIntegrationWithBackend:
    """测试与 OpenAIBackend 集成"""

    def _make_backend_with_mock(self, response_json):
        """构造带 mock transport 的 OpenAIBackend（不发真实网络请求）

        返回 (backend, captured) — captured["data"] 为请求 payload 的 dict。
        """
        captured = {}

        def handler(request):
            # 捕获请求 payload 用于断言
            try:
                captured["data"] = __import__("json").loads(request.content)
            except Exception:
                pass
            return httpx.Response(200, json=response_json)

        transport = httpx.MockTransport(handler)
        backend = OpenAIBackend(
            api_key="sk-test", base_url="https://api.openai.com/v1", model="gpt-4o"
        )
        backend.client = httpx.AsyncClient(transport=transport, timeout=120.0)
        backend.prompt_cache = PromptCache(ttl_seconds=300)
        return backend, captured

    @pytest.mark.asyncio
    async def test_backend_uses_cache_and_emits_user_tag(self):
        """启用缓存时请求 payload 含 user 字段（cache_key 遥测标识）"""
        backend, captured = self._make_backend_with_mock({
            "choices": [{"message": {"content": "hello"}}],
            "model": "gpt-4o",
        })
        system = "## 当前环境\n- MCU: STM32"
        try:
            resp = await backend.generate(
                system=system,
                messages=[{"role": "user", "content": "hi"}],
                use_cache=True,
            )
            assert resp.content == "hello"
            # payload 应包含 user 字段（cache_key 遥测）
            assert "data" in captured
            assert "user" in captured["data"]
            assert captured["data"]["user"].startswith("iron-cache:")
            # system 内容保持原样
            assert captured["data"]["messages"][0]["content"] == system
        finally:
            await backend.aclose()

    @pytest.mark.asyncio
    async def test_backend_cache_hit_increments_count(self):
        """两次相同 system 请求后，缓存命中计数增加"""
        backend, captured = self._make_backend_with_mock({
            "choices": [{"message": {"content": "ok"}}],
            "model": "gpt-4o",
        })
        system = "## 当前环境\n- MCU: STM32"
        try:
            # 第一次：未命中
            await backend.generate(system=system, messages=[{"role": "user", "content": "hi"}])
            stats1 = backend.prompt_cache.stats()
            assert stats1["total_hits"] == 0
            # 第二次：命中（块级）
            await backend.generate(system=system, messages=[{"role": "user", "content": "hi"}])
            stats2 = backend.prompt_cache.stats()
            assert stats2["total_hits"] >= 1
        finally:
            await backend.aclose()

    @pytest.mark.asyncio
    async def test_backend_use_cache_false_skips_cache(self):
        """use_cache=False 时不启用缓存（无 user 字段）"""
        backend, captured = self._make_backend_with_mock({
            "choices": [{"message": {"content": "hello"}}],
            "model": "gpt-4o",
        })
        system = "## 当前环境\n- MCU: STM32"
        try:
            await backend.generate(
                system=system,
                messages=[{"role": "user", "content": "hi"}],
                use_cache=False,
            )
            assert "data" in captured
            # use_cache=False 时不应有 user 字段
            assert "user" not in captured["data"]
        finally:
            await backend.aclose()

    @pytest.mark.asyncio
    async def test_backend_no_prompt_cache_behaves_normal(self):
        """prompt_cache 未注入时退化为原行为（无 user 字段）"""
        def handler(request):
            return httpx.Response(200, json={
                "choices": [{"message": {"content": "hello"}}],
                "model": "gpt-4o",
            })
        transport = httpx.MockTransport(handler)
        backend = OpenAIBackend(
            api_key="sk-test", base_url="https://api.openai.com/v1", model="gpt-4o"
        )
        backend.client = httpx.AsyncClient(transport=transport, timeout=120.0)
        # 不设置 prompt_cache（保持 None）
        assert backend.prompt_cache is None
        try:
            resp = await backend.generate(
                system="sys", messages=[{"role": "user", "content": "hi"}]
            )
            assert resp.content == "hello"
        finally:
            await backend.aclose()
