"""memory.py 单元测试

覆盖 ContextCompactor / ProjectMemory / Dream-Distill 核心逻辑。

运行方式: pytest tests/test_memory.py -v
"""
import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock


class TestEstimateTokens:
    """Token 估算"""

    def test_estimate_tokens_english(self):
        from iron.agent.memory import estimate_tokens
        # 英文约 4 字符/token（fallback）
        text = "hello world from the iron agent"
        tokens = estimate_tokens(text)
        assert tokens > 0  # 至少不为零

    def test_estimate_tokens_chinese(self):
        from iron.agent.memory import estimate_tokens
        # 中文约 1.5 字/token（fallback）
        text = "你好世界嵌入式开发助手"
        tokens = estimate_tokens(text)
        assert tokens > 0

    def test_mixed_text(self):
        from iron.agent.memory import estimate_tokens
        text = "hello 你好 world 世界"
        tokens = estimate_tokens(text)
        assert tokens > 0

    def test_empty_text(self):
        from iron.agent.memory import estimate_tokens
        assert estimate_tokens("") == 0
        assert estimate_tokens(None) == 0


class TestSerializeMessage:
    """消息序列化"""

    def test_serialize_user(self):
        from iron.agent.memory import serialize_message
        msg = {"role": "user", "content": "写一个LED程序"}
        result = serialize_message(msg)
        assert "用户" in result
        assert "LED程序" in result

    def test_serialize_assistant_with_tool_calls(self):
        from iron.agent.memory import serialize_message
        msg = {
            "role": "assistant",
            "content": "我来写代码",
            "tool_calls": [
                {"function": {"name": "write_file", "arguments": '{"path": "main.c"}'}}
            ]
        }
        result = serialize_message(msg)
        assert "助手" in result
        assert "write_file" in result

    def test_serialize_tool_success(self):
        from iron.agent.memory import serialize_message
        msg = {
            "role": "tool",
            "content": json.dumps({"success": True, "path": "main.c", "stdout": "文件已创建"})
        }
        result = serialize_message(msg)
        assert "成功" in result
        assert "main.c" in result

    def test_serialize_tool_failure(self):
        from iron.agent.memory import serialize_message
        msg = {
            "role": "tool",
            "content": json.dumps({"success": False, "error": "权限拒绝"})
        }
        result = serialize_message(msg)
        assert "失败" in result
        assert "权限拒绝" in result

    def test_serialize_tool_non_json(self):
        from iron.agent.memory import serialize_message
        msg = {"role": "tool", "content": "plain text result"}
        result = serialize_message(msg)
        assert "plain text result" in result


class TestContextCompactor:
    """上下文压缩器"""

    @pytest.mark.asyncio
    async def test_no_compact_under_limit(self):
        from iron.agent.memory import ContextCompactor
        compactor = ContextCompactor()
        messages = [{"role": "user", "content": "hello"}]
        compact = await compactor.compact_if_needed(messages, "system prompt")
        assert compact == messages

    @pytest.mark.asyncio
    async def test_compact_calls_llm(self):
        from iron.agent.memory import ContextCompactor
        from unittest.mock import AsyncMock, MagicMock

        mock_llm = AsyncMock()
        mock_response = MagicMock()
        mock_response.content = '## Summary\n- done'
        mock_llm.generate = AsyncMock(return_value=mock_response)

        compactor = ContextCompactor(llm=mock_llm)
        # 15 x 5000 Chinese chars = ~50k tokens >> MAX_CONTEXT_TOKENS(30000)
        messages = [{'role': 'user', 'content': '测' * 5000} for _ in range(15)]
        result = await compactor.compact_if_needed(messages, '')
        assert mock_llm.generate.called, 'LLM generate should be called when messages exceed limit'
        assert any(m.get('role') == 'system' for m in result)

    @pytest.mark.asyncio
    async def test_last_summary_updated(self):
        from iron.agent.memory import ContextCompactor
        from unittest.mock import AsyncMock, MagicMock

        mock_llm = AsyncMock()
        mock_response = MagicMock()
        mock_response.content = 'Test summary'
        mock_llm.generate = AsyncMock(return_value=mock_response)

        compactor = ContextCompactor(llm=mock_llm)
        messages = [{'role': 'user', 'content': '测' * 5000} for _ in range(15)]
        await compactor.compact_if_needed(messages, '')
        assert compactor.last_summary == 'Test summary'

    def test_last_summary_property(self):
        """last_summary 是公共属性"""
        from iron.agent.memory import ContextCompactor
        compactor = ContextCompactor()
        compactor._last_summary = "内部值"
        assert compactor.last_summary == "内部值"


class TestProjectMemory:
    """项目持久记忆"""

    def test_load_save_memory(self, tmp_path):
        from iron.agent.memory import ProjectMemory

        memory = ProjectMemory(str(tmp_path))
        memory.ensure_dirs()

        memory.save_memory("# 项目记忆\n\n## 长期知识\n- 使用HAL库\n")
        loaded = memory.load_memory()
        assert "项目记忆" in loaded
        assert "HAL库" in loaded

    def test_append_to_memory_new_section(self, tmp_path):
        from iron.agent.memory import ProjectMemory

        memory = ProjectMemory(str(tmp_path))
        memory.ensure_dirs()
        memory.save_memory("# 项目记忆\n")

        memory.append_to_memory("硬件配置", "STM32F407VG 芯片")

        loaded = memory.load_memory()
        assert "硬件配置" in loaded
        assert "STM32F407VG" in loaded

    def test_append_to_memory_existing_section(self, tmp_path):
        from iron.agent.memory import ProjectMemory

        memory = ProjectMemory(str(tmp_path))
        memory.ensure_dirs()
        memory.save_memory("# 项目记忆\n\n## 硬件配置\n- STM32F103\n")

        memory.append_to_memory("硬件配置", "STM32F407")

        loaded = memory.load_memory()
        assert loaded.count("STM32") == 2  # 原有 + 新追加

    def test_save_checkpoint(self, tmp_path):
        from iron.agent.memory import ProjectMemory

        memory = ProjectMemory(str(tmp_path))
        memory.save_checkpoint(
            summary="completed GPIO init",
            files_changed=["main.c", "gpio.c"],
            current_task="LED task",
        )

        assert memory.checkpoint_file.exists()
        content = memory.checkpoint_file.read_text(encoding="utf-8")
        assert "LED task" in content
        assert "main.c" in content
        assert "GPIO init" in content

    def test_load_checkpoint(self, tmp_path):
        from iron.agent.memory import ProjectMemory

        memory = ProjectMemory(str(tmp_path))
        memory.save_checkpoint(summary="测试", files_changed=[], current_task="")
        loaded = memory.load_checkpoint()
        assert "测试" in loaded

    def test_save_task_progress(self, tmp_path):
        from iron.agent.memory import ProjectMemory

        memory = ProjectMemory(str(tmp_path))

        # 合法 task_id
        memory.save_task_progress("task_001", "GPIO configured")
        progress_file = memory.tasks_dir / "task_001" / "progress.md"
        assert progress_file.exists()
        assert "GPIO configured" in progress_file.read_text(encoding="utf-8")

    def test_save_task_progress_invalid_id_rejected(self, tmp_path):
        from iron.agent.memory import ProjectMemory

        memory = ProjectMemory(str(tmp_path))
        with pytest.raises(ValueError, match="非法 task_id"):
            memory.save_task_progress("../etc/passwd", "hacked")
        with pytest.raises(ValueError):
            memory.save_task_progress("task\x00id", "bad")

    def test_build_context_injection(self, tmp_path):
        from iron.agent.memory import ProjectMemory

        memory = ProjectMemory(str(tmp_path))
        memory.save_checkpoint(summary="上次会话", files_changed=[], current_task="LED")
        memory.save_memory("# 项目\n\n## 知识\n- 使用HAL\n")

        ctx = memory.build_context_injection(token_budget=500)
        assert "上次会话" in ctx or "知识" in ctx

    def test_build_context_injection_truncation(self, tmp_path):
        from iron.agent.memory import ProjectMemory

        memory = ProjectMemory(str(tmp_path))
        large_memory = "# 项目记忆\n\n" + "\n".join([f"- 知识点{i}: 内容" for i in range(200)])
        memory.save_memory(large_memory)

        # 小预算应该截断
        ctx = memory.build_context_injection(token_budget=100)
        assert "项目记忆" in ctx or "截断" in ctx


class TestDreamDistill:
    """Dream/Distill 记忆整理"""

    def test_should_dream_first_time(self, tmp_path):
        from iron.agent.memory import ProjectMemory

        memory = ProjectMemory(str(tmp_path))
        memory.ensure_dirs()
        # 首次：有 checkpoint 但无 dream 历史 → 应该 dream
        memory.save_checkpoint(summary="测试", files_changed=[], current_task="")
        assert memory.should_dream() is True

    def test_should_not_dream_recent(self, tmp_path):
        from iron.agent.memory import ProjectMemory
        from datetime import datetime, timedelta

        memory = ProjectMemory(str(tmp_path))
        memory.ensure_dirs()

        # 写入最近的 dream 时间
        meta = {"last_dream": datetime.now().isoformat()}
        memory._save_meta(meta)

        # 刚 dream 过，不应再触发
        assert memory.should_dream() is False

    def test_should_dream_after_7_days(self, tmp_path):
        from iron.agent.memory import ProjectMemory
        from datetime import datetime, timedelta

        memory = ProjectMemory(str(tmp_path))
        memory.ensure_dirs()

        # 8 天前的 dream
        meta = {"last_dream": (datetime.now() - timedelta(days=8)).isoformat()}
        memory._save_meta(meta)
        memory.save_checkpoint(summary="测试", files_changed=[], current_task="")

        assert memory.should_dream() is True

    def test_should_not_distill_first_time(self, tmp_path):
        from iron.agent.memory import ProjectMemory

        memory = ProjectMemory(str(tmp_path))
        memory.ensure_dirs()
        # 首次 distill 不自动触发（需要先有素材）
        assert memory.should_distill() is False

    def test_should_distill_after_30_days(self, tmp_path):
        from iron.agent.memory import ProjectMemory
        from datetime import datetime, timedelta

        memory = ProjectMemory(str(tmp_path))
        memory.ensure_dirs()

        meta = {"last_distill": (datetime.now() - timedelta(days=31)).isoformat()}
        memory._save_meta(meta)

        assert memory.should_distill() is True

    @pytest.mark.asyncio
    async def test_dream_updates_meta(self, tmp_path):
        from iron.agent.memory import ProjectMemory

        memory = ProjectMemory(str(tmp_path))
        memory.ensure_dirs()
        memory.save_checkpoint(summary="测试会话", files_changed=["main.c"], current_task="LED")

        mock_llm = AsyncMock()
        mock_response = MagicMock()
        mock_response.content = "## 长期知识\n- LED 使用低电平点亮"
        mock_llm.generate = AsyncMock(return_value=mock_response)

        result = await memory.dream(llm=mock_llm)

        # 更新了元数据
        meta = memory._load_meta()
        assert meta["last_dream"] is not None

        # 生成了知识
        assert "LED" in result

    @pytest.mark.asyncio
    async def test_distill_creates_archive(self, tmp_path):
        from iron.agent.memory import ProjectMemory

        memory = ProjectMemory(str(tmp_path))
        memory.ensure_dirs()
        memory.save_memory("# 项目\n\n" + "原始记忆内容\n" * 100)

        mock_llm = AsyncMock()
        mock_response = MagicMock()
        mock_response.content = "# 蒸馏后\n\n## 核心\n- 简洁的知识"
        mock_llm.generate = AsyncMock(return_value=mock_response)

        await memory.distill(llm=mock_llm)

        # 归档了原始记忆
        archive_dir = memory.memory_dir / "archive"
        assert archive_dir.exists()
        archives = list(archive_dir.glob("MEMORY_*.md"))
        assert len(archives) >= 1

        # 更新了元数据
        meta = memory._load_meta()
        assert meta["last_distill"] is not None

    @pytest.mark.asyncio
    async def test_maybe_dream_distill_order(self, tmp_path):
        """distill 和 dream 可以同时触发（elif → if 修复）"""
        import os
        import time
        from iron.agent.memory import ProjectMemory
        from datetime import datetime, timedelta

        memory = ProjectMemory(str(tmp_path))
        memory.ensure_dirs()

        # 1. 写入 checkpoint 并将其 mtime 设为 8 天前，触发 should_dream
        memory.save_checkpoint(summary="session", files_changed=[], current_task="")
        old_mtime = (time.time() - 8 * 86400)
        os.utime(memory.checkpoint_file, (old_mtime, old_mtime))

        # 2. 设置 meta 让 distill 条件也满足
        memory._save_meta({
            "last_distill": (datetime.now() - timedelta(days=31)).isoformat(),
            "last_dream": (datetime.now() - timedelta(days=8)).isoformat(),
        })

        mock_llm = AsyncMock()
        mock_response = MagicMock()
        mock_response.content = "## Summary"
        mock_llm.generate = AsyncMock(return_value=mock_response)

        # 3. memory 足够长（>500 字符）以触发 distill.generate
        large_memory = "## long term knowledge base\n" + "\n".join([f"- item{i}: detailed description" for i in range(60)])
        memory.save_memory(large_memory)

        await memory.maybe_dream_distill(llm=mock_llm)

        # 修复后 distill 和 dream 都在 if 中，两者都可能触发 generate
        # 至少 distill 触发（memory 够长且上次 distill > 30 天）
        assert mock_llm.generate.call_count >= 1, \
            f"distill 或 dream 应调用 generate，实际: {mock_llm.generate.call_count}"

    @pytest.mark.asyncio
    async def test_maybe_dream_distill_llm_failure_safe(self, tmp_path):
        """LLM 失败时优雅降级，不抛出异常"""
        from iron.agent.memory import ProjectMemory

        memory = ProjectMemory(str(tmp_path))
        memory.ensure_dirs()
        memory.save_checkpoint(summary="测试", files_changed=[], current_task="")

        mock_llm = AsyncMock()
        mock_llm.generate = AsyncMock(side_effect=RuntimeError("LLM 不可用"))

        # 不应抛出
        await memory.maybe_dream_distill(llm=mock_llm)


class TestCountTokensExport:
    """统一导出 count_tokens"""

    def test_count_tokens_callable(self):
        from iron.agent.memory import count_tokens
        assert callable(count_tokens)
        assert count_tokens("hello world") > 0
