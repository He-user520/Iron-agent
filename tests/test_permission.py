"""P2-3 三级审批持久化测试 — PermissionManager

覆盖：
- _hash_args: 参数 hash 计算（含空参数）
- check: 默认 ask / 会话级允许 / 黑名单拒绝 / 黑名单通配符
- record_decision: once（不持久化）/ session（内存）/ never（持久化）
- revoke: 撤销黑名单
- clear_session: 清空会话允许
- _load_denied / _save_denied: 加载保存黑名单
- 跨实例持久化
- 与 engine 集成（黑名单阻断工具执行）

运行方式: pytest tests/test_permission.py -v
"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from iron.agent.permission import (
    PermissionManager,
    PermissionDecision,
    DeniedEntry,
)


# ── _hash_args 单元测试 ────────────────────────────────────────


class TestHashArgs:
    """参数 hash 计算"""

    def test_hash_args(self):
        """含关键字段的参数返回稳定的 8 位 hash"""
        mgr = PermissionManager(persist_path=Path("/tmp/_test_perm_nonexist.yml"))
        h = mgr._hash_args({"firmware": "fw.bin", "probe": "stlink"})
        # firmware 是关键字段，应产生非 "*" 的 hash
        assert h != "*"
        assert len(h) == 8
        # 相同参数应产生相同 hash
        h2 = mgr._hash_args({"firmware": "fw.bin", "probe": "jlink"})
        assert h == h2  # probe 不是关键字段，不影响 hash

    def test_hash_args_empty(self):
        """空参数返回 "*" """
        mgr = PermissionManager(persist_path=Path("/tmp/_test_perm_nonexist.yml"))
        assert mgr._hash_args({}) == "*"
        assert mgr._hash_args(None) == "*"
        # 无关键字段的参数也返回 "*"
        assert mgr._hash_args({"unknown_key": "value"}) == "*"


# ── check 权限检查 ─────────────────────────────────────────────


class TestCheck:
    """check() 权限决策"""

    def test_check_default_ask(self):
        """默认（无黑名单/无会话允许）返回 ask"""
        mgr = PermissionManager(persist_path=Path("/tmp/_test_perm_nonexist.yml"))
        decision = mgr.check("embed_flash", {"firmware": "fw.bin"})
        assert decision.action == "ask"
        assert decision.scope == "once"

    def test_check_session_allow(self):
        """会话级允许后不再询问"""
        mgr = PermissionManager(persist_path=Path("/tmp/_test_perm_nonexist.yml"))
        # 记录会话允许
        mgr.record_decision("embed_flash", {"firmware": "fw.bin"}, "session")
        # 相同参数应命中会话允许
        decision = mgr.check("embed_flash", {"firmware": "fw.bin"})
        assert decision.action == "allow"
        assert decision.scope == "session"
        # 不同 firmware 参数（但 firmware 是关键字段，hash 相同）也命中
        decision2 = mgr.check("embed_flash", {"firmware": "fw.bin", "probe": "stlink"})
        assert decision2.action == "allow"

    def test_check_never_deny(self, tmp_path):
        """黑名单拒绝"""
        mgr = PermissionManager(persist_path=tmp_path / "permissions.yml")
        mgr.record_decision("embed_flash", {"firmware": "fw.bin"}, "never", reason="测试拒绝")
        # 精确匹配的黑名单应拒绝
        decision = mgr.check("embed_flash", {"firmware": "fw.bin"})
        assert decision.action == "deny"
        assert decision.scope == "never"
        assert "测试拒绝" in decision.reason

    def test_check_never_wildcard(self, tmp_path):
        """黑名单通配符 — args_hash=* 拒绝该工具所有参数"""
        mgr = PermissionManager(persist_path=tmp_path / "permissions.yml")
        # 用空参数记录 never → args_hash = "*"
        mgr.record_decision("embed_flash", {}, "never")
        # 任何参数都应被拒绝
        decision = mgr.check("embed_flash", {"firmware": "other.bin"})
        assert decision.action == "deny"
        assert decision.scope == "never"


# ── record_decision 决策记录 ──────────────────────────────────


class TestRecordDecision:
    """record_decision() 三级决策记录"""

    def test_record_decision_once(self):
        """单次允许 — 不持久化，下次仍询问"""
        mgr = PermissionManager(persist_path=Path("/tmp/_test_perm_nonexist.yml"))
        mgr.record_decision("embed_flash", {"firmware": "fw.bin"}, "once")
        # once 不持久化，check 仍返回 ask
        decision = mgr.check("embed_flash", {"firmware": "fw.bin"})
        assert decision.action == "ask"
        # 不应写入黑名单文件
        assert len(mgr.list_denied()) == 0

    def test_record_decision_session(self):
        """会话允许 — 内存保存，同会话内不再问"""
        mgr = PermissionManager(persist_path=Path("/tmp/_test_perm_nonexist.yml"))
        mgr.record_decision("embed_flash", {"firmware": "fw.bin"}, "session")
        decision = mgr.check("embed_flash", {"firmware": "fw.bin"})
        assert decision.action == "allow"
        assert decision.scope == "session"

    def test_record_decision_never(self, tmp_path):
        """永不 — 持久化到黑名单文件"""
        perm_file = tmp_path / "permissions.yml"
        mgr = PermissionManager(persist_path=perm_file)
        mgr.record_decision("embed_flash", {"firmware": "fw.bin"}, "never", reason="用户拒绝")
        # 黑名单应有 1 条
        denied = mgr.list_denied()
        assert len(denied) == 1
        assert denied[0].tool == "embed_flash"
        assert "用户拒绝" in denied[0].reason
        # 文件应已写入
        assert perm_file.exists()


# ── revoke 撤销黑名单 ──────────────────────────────────────────


class TestRevoke:
    """revoke() 撤销黑名单"""

    def test_revoke(self, tmp_path):
        """撤销黑名单条目"""
        perm_file = tmp_path / "permissions.yml"
        mgr = PermissionManager(persist_path=perm_file)
        # 添加两条黑名单
        mgr.record_decision("embed_flash", {}, "never")
        mgr.record_decision("embed_build", {}, "never")
        assert len(mgr.list_denied()) == 2
        # 撤销 embed_flash
        removed = mgr.revoke("embed_flash")
        assert removed is True
        denied = mgr.list_denied()
        assert len(denied) == 1
        assert denied[0].tool == "embed_build"
        # 撤销不存在的工具返回 False
        removed2 = mgr.revoke("nonexistent_tool")
        assert removed2 is False
        # 文件应已更新（只剩 embed_build）
        import yaml
        with open(perm_file, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        tools = [e["tool"] for e in data["denied"]]
        assert "embed_build" in tools
        assert "embed_flash" not in tools


# ── clear_session 清空会话允许 ────────────────────────────────


class TestClearSession:
    """clear_session() 清空会话级允许"""

    def test_clear_session(self):
        """清空会话允许后恢复询问"""
        mgr = PermissionManager(persist_path=Path("/tmp/_test_perm_nonexist.yml"))
        mgr.record_decision("embed_flash", {"firmware": "fw.bin"}, "session")
        # 会话允许生效
        assert mgr.check("embed_flash", {"firmware": "fw.bin"}).action == "allow"
        # 清空后恢复 ask
        mgr.clear_session()
        assert mgr.check("embed_flash", {"firmware": "fw.bin"}).action == "ask"


# ── 加载/保存黑名单 ──────────────────────────────────────────


class TestLoadSaveDenied:
    """_load_denied() / _save_denied() 持久化"""

    def test_load_save_denied(self, tmp_path):
        """保存后重新加载，黑名单应一致"""
        perm_file = tmp_path / "permissions.yml"
        mgr1 = PermissionManager(persist_path=perm_file)
        mgr1.record_decision("embed_flash", {}, "never", reason="原因A")
        mgr1.record_decision("embed_build", {}, "never", reason="原因B")
        # 新实例加载同一文件
        mgr2 = PermissionManager(persist_path=perm_file)
        denied = mgr2.list_denied()
        assert len(denied) == 2
        tools = {d.tool for d in denied}
        assert tools == {"embed_flash", "embed_build"}
        # 检查加载后 check 能正确拒绝
        assert mgr2.check("embed_flash", {}).action == "deny"
        assert mgr2.check("embed_build", {}).action == "deny"


class TestPersistAcrossInstances:
    """跨实例持久化"""

    def test_persist_across_instances(self, tmp_path):
        """黑名单跨实例持久化 — 新实例应加载旧实例的黑名单"""
        perm_file = tmp_path / "permissions.yml"
        # 第一个实例记录 never
        mgr1 = PermissionManager(persist_path=perm_file)
        mgr1.record_decision("mcp_config", {"action": "add"}, "never")
        # 第二个实例（模拟进程重启）应加载到黑名单
        mgr2 = PermissionManager(persist_path=perm_file)
        decision = mgr2.check("mcp_config", {"action": "add"})
        assert decision.action == "deny"
        # 会话级允许不应跨实例（只在内存）
        mgr2.record_decision("embed_flash", {}, "session")
        mgr3 = PermissionManager(persist_path=perm_file)
        # 新实例无会话允许
        assert mgr3.check("embed_flash", {}).action == "ask"


# ── 与 engine 集成 ────────────────────────────────────────────


class TestIntegrationWithEngine:
    """与 AgentEngine 集成 — 黑名单阻断工具执行"""

    def test_integration_with_engine(self, tmp_path):
        """黑名单工具在 process() 中被阻断"""
        from iron.agent.engine import AgentEngine
        from iron.agent.prompt_builder import PromptBuilder
        from iron.llm.backend import LLMBackend, LLMResponse
        from iron.skills.registry import SkillRegistry

        config = SimpleNamespace(
            project=SimpleNamespace(
                project_dir=str(tmp_path), mcu="stm32f407", build_system="platformio"
            ),
            mcp={},
            # P2-3: 黑名单持久化路径指向临时目录，避免污染用户 ~/.iron/permissions.yml
            permission_persist_path=str(tmp_path / "permissions.yml"),
        )

        class _ScriptedLLM(LLMBackend):
            """按脚本返回响应的 mock LLM"""
            def __init__(self, responses):
                self._responses = list(responses)
                self._call = 0

            async def generate(self, system, messages, temperature=0.3, max_tokens=4096, tools=None):
                self._call += 1
                if self._call <= len(self._responses):
                    return self._responses[self._call - 1]
                return LLMResponse(content="完成", model="mock")

            async def stream_generate(self, system, messages, temperature=0.3, max_tokens=4096, tools=None):
                resp = await self.generate(system, messages, temperature, max_tokens, tools)
                if resp.content:
                    yield ("chunk", resp.content)
                yield ("response", resp)

        # LLM 第一步调用 write_file（被黑名单阻断），第二步 chat 收尾
        llm = _ScriptedLLM([
            LLMResponse(
                content="", model="mock",
                tool_calls=[{
                    "id": "call_1", "type": "function",
                    "function": {
                        "name": "write_file",
                        "arguments": json.dumps({"path": "test.c", "content": "int main(){}"}),
                    },
                }],
            ),
            LLMResponse(content="被黑名单阻断", model="mock"),
        ])

        engine = AgentEngine(
            llm=llm,
            prompt_builder=PromptBuilder(Path(".")),
            skills=SkillRegistry(),
            config=config,
        )
        # 将 write_file 加入黑名单（通配符，拒绝所有参数）
        engine._permission_mgr.record_decision("write_file", {}, "never")

        # 消费所有事件
        events = []
        import asyncio
        async def _run():
            async for event in engine.process("写一个文件"):
                events.append(event)
        asyncio.run(_run())

        # 应有 tool_blocked 事件
        blocked_events = [e for e in events if e.type == "tool_blocked"]
        assert len(blocked_events) >= 1
        # 阻断原因应包含"黑名单"
        assert "黑名单" in blocked_events[0].data.get("reason", "")

        # 工具结果应包含失败信息（通过 conversation 检查）
        tool_msgs = [m for m in engine.conversation if m.get("role") == "tool"]
        assert len(tool_msgs) >= 1
        parsed = json.loads(tool_msgs[0]["content"])
        assert parsed["success"] is False
        assert "黑名单" in parsed["error"]
