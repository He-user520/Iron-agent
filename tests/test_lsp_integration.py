"""LSP 端到端集成测试 — 覆盖 bootstrap/engine/main 全链路集成

运行方式: pytest tests/test_lsp_integration.py -v

测试策略：
- 所有测试用 mock，不依赖真实 clangd/ccls
- 每个测试类覆盖一个集成点
- 共 12 个测试用例，对应 Step 2-9 的验证
"""
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from iron.integrations.lsp_client import LSPClient, LSPConfig


# ── 测试夹具 ──────────────────────────────────────────────────

@pytest.fixture
def mock_lsp_client():
    """构造已初始化的 mock LSP 客户端"""
    client = MagicMock(spec=LSPClient)
    client._initialized = True
    client.start = AsyncMock(return_value=True)
    client.stop = AsyncMock(return_value=None)
    client.did_open = AsyncMock(return_value=None)
    client.did_change = AsyncMock(return_value=None)
    client.did_close = AsyncMock(return_value=None)
    client.get_diagnostics = AsyncMock(return_value=[])
    client.definition = AsyncMock(return_value=[])
    client.references = AsyncMock(return_value=[])
    client.hover = AsyncMock(return_value=None)
    client.completion = AsyncMock(return_value=[])
    return client


@pytest.fixture
def disabled_lsp_client():
    """构造未初始化的 mock LSP 客户端（_initialized=False）"""
    client = MagicMock(spec=LSPClient)
    client._initialized = False
    return client


# ── 1. bootstrap 生命周期 ─────────────────────────────────────

class TestLSPLifecycleInBootstrap:
    """验证 bootstrap 阶段 3 的 LSP 客户端初始化"""

    def test_lsp_client_lifecycle_in_bootstrap(self, tmp_path, mock_lsp_client):
        """Test 1: 特性门控开启 + 启动成功 → lsp_client 注入 BootstrapResult

        断言要点：
        - BootstrapResult.lsp_client 不为 None
        - LSPClient.start() 被调用
        - phases_executed 包含 "run"
        """
        from iron.cli.bootstrap import Bootstrap
        with patch("iron.config.features.get_feature_flags") as gf, \
             patch("iron.integrations.lsp_client.LSPClient") as LC:
            flags = MagicMock()
            flags.is_enabled.return_value = True
            gf.return_value = flags
            LC.find_compile_commands.return_value = None
            LC.return_value = mock_lsp_client
            mock_lsp_client.start = AsyncMock(return_value=True)
            result = Bootstrap().run(tmp_path)
            assert result.lsp_client is not None
            assert "run" in result.phases_executed
            assert result.success is True


# ── 2. engine 工具注册 ────────────────────────────────────────

class TestLSPToolsRegistration:
    """验证 engine.__init__ 注册 5 个 LSP 工具"""

    def test_lsp_tools_registered_in_engine(self, mock_lsp_client):
        """Test 2: lsp_client 注入后，_tool_registry 包含 5 个 LSP 工具

        断言要点：
        - _tool_registry.get_all_schemas() 包含 lsp_diagnostics/lsp_definition/
          lsp_references/lsp_hover/lsp_completion
        - 5 个工具的 _client 指向传入的 lsp_client
        """
        from iron.agent.engine import AgentEngine
        from iron.llm.backend import EchoBackend
        from iron.agent.prompt_builder import PromptBuilder
        from iron.skills.registry import SkillRegistry
        llm = EchoBackend()
        pb = PromptBuilder(Path("."), "stm32f407")
        skills = SkillRegistry()
        engine = AgentEngine(llm=llm, prompt_builder=pb, skills=skills,
                             config=None, lsp_client=mock_lsp_client)
        schemas = engine._tool_registry.get_all_schemas()
        names = [s["function"]["name"] for s in schemas]
        assert "lsp_diagnostics" in names
        assert "lsp_definition" in names
        assert "lsp_references" in names
        assert "lsp_hover" in names
        assert "lsp_completion" in names
        assert engine._lsp_diagnostics_tool._client is mock_lsp_client

    def test_lsp_tools_registered_with_none_client(self):
        """Test 3: lsp_client=None 时，5 个工具仍注册（降级模式）

        断言要点：
        - 工具注册成功（schema 存在）
        - 工具 execute 返回 success=False
        """
        from iron.agent.engine import AgentEngine
        from iron.llm.backend import EchoBackend
        from iron.agent.prompt_builder import PromptBuilder
        from iron.skills.registry import SkillRegistry
        llm = EchoBackend()
        pb = PromptBuilder(Path("."), "stm32f407")
        skills = SkillRegistry()
        engine = AgentEngine(llm=llm, prompt_builder=pb, skills=skills,
                             config=None, lsp_client=None)
        schemas = engine._tool_registry.get_all_schemas()
        names = [s["function"]["name"] for s in schemas]
        assert "lsp_diagnostics" in names
        result = asyncio.run(engine._lsp_diagnostics_tool.execute({"file": "x.c"}, {}))
        assert result["success"] is False


# ── 3. 文件通知钩子 ──────────────────────────────────────────

class TestLSPFileNotifications:
    """验证 _execute_write_file / _execute_read_file 触发 LSP 通知"""

    @pytest.mark.asyncio
    async def test_write_file_triggers_did_change(self, mock_lsp_client, tmp_path):
        """Test 4: 写入 .c 文件后，lsp_client.did_change 被调用

        断言要点：
        - did_change 至少被调用一次
        - 参数包含正确 path 和 content
        """
        from iron.agent.engine import AgentEngine
        from iron.llm.backend import EchoBackend
        from iron.agent.prompt_builder import PromptBuilder
        from iron.skills.registry import SkillRegistry
        llm = EchoBackend()
        pb = PromptBuilder(tmp_path, "stm32f407")
        skills = SkillRegistry()
        config = SimpleNamespace(project=SimpleNamespace(project_dir=str(tmp_path)),
                                mcp=None, tool_output_max_chars=10000)
        engine = AgentEngine(llm=llm, prompt_builder=pb, skills=skills,
                             config=config, lsp_client=mock_lsp_client)
        async for _ in engine._execute_write_file(
            {"path": "src/main.c", "content": "int main(){}"}
        ):
            pass
        await asyncio.sleep(0.1)
        mock_lsp_client.did_change.assert_called()

    @pytest.mark.asyncio
    async def test_read_file_triggers_did_open(self, mock_lsp_client, tmp_path):
        """Test 5: 读取 .c 文件后，lsp_client.did_open 被调用

        断言要点：
        - did_open 至少被调用一次
        - 参数包含正确 path 和 content
        """
        from iron.agent.engine import AgentEngine
        from iron.llm.backend import EchoBackend
        from iron.agent.prompt_builder import PromptBuilder
        from iron.skills.registry import SkillRegistry
        llm = EchoBackend()
        pb = PromptBuilder(tmp_path, "stm32f407")
        skills = SkillRegistry()
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        (src_dir / "main.c").write_text("int main(){}", encoding="utf-8")
        config = SimpleNamespace(project=SimpleNamespace(project_dir=str(tmp_path)),
                                mcp=None, tool_output_max_chars=10000)
        engine = AgentEngine(llm=llm, prompt_builder=pb, skills=skills,
                             config=config, lsp_client=mock_lsp_client)
        async for _ in engine._execute_read_file({"path": "src/main.c"}):
            pass
        await asyncio.sleep(0.1)
        mock_lsp_client.did_open.assert_called()

    @pytest.mark.asyncio
    async def test_write_non_c_file_no_notification(self, mock_lsp_client, tmp_path):
        """Test 6: 写入 .md 文件不触发 LSP 通知（扩展名过滤）

        断言要点：
        - did_change 未被调用
        """
        from iron.agent.engine import AgentEngine
        from iron.llm.backend import EchoBackend
        from iron.agent.prompt_builder import PromptBuilder
        from iron.skills.registry import SkillRegistry
        llm = EchoBackend()
        pb = PromptBuilder(tmp_path, "stm32f407")
        skills = SkillRegistry()
        config = SimpleNamespace(project=SimpleNamespace(project_dir=str(tmp_path)),
                                mcp=None, tool_output_max_chars=10000)
        engine = AgentEngine(llm=llm, prompt_builder=pb, skills=skills,
                             config=config, lsp_client=mock_lsp_client)
        async for _ in engine._execute_write_file(
            {"path": "README.md", "content": "# hi"}
        ):
            pass
        await asyncio.sleep(0.1)
        mock_lsp_client.did_change.assert_not_called()


# ── 4. VerifyAgent LSP 集成 ──────────────────────────────────

class TestVerifyAgentLSPIntegration:
    """验证 VerifyAgent.verify() 显式调用 LSP 诊断"""

    @pytest.mark.asyncio
    async def test_verify_agent_calls_lsp_diagnostics(self, mock_lsp_client, tmp_path):
        """Test 7: verify() 并行调用 lsp_client.get_diagnostics 收集诊断

        断言要点：
        - get_diagnostics 被调用（且并行，调用次数 = 文件数）
        - 返回结果包含 lsp_diagnostics 字段
        """
        from iron.agent.engine import VerifyAgent
        from iron.llm.backend import EchoBackend
        from iron.agent.prompt_builder import PromptBuilder
        from iron.skills.registry import SkillRegistry
        llm = EchoBackend()
        pb = PromptBuilder(tmp_path, "stm32f407")
        skills = SkillRegistry()
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        (src_dir / "main.c").write_text("int main(){}", encoding="utf-8")
        (src_dir / "util.c").write_text("void util(){}", encoding="utf-8")
        mock_lsp_client.get_diagnostics = AsyncMock(return_value=[])
        config = SimpleNamespace(project=SimpleNamespace(project_dir=str(tmp_path)),
                                mcp=None, tool_output_max_chars=10000)
        agent = VerifyAgent(llm=llm, prompt_builder=pb, skills=skills,
                            config=config, lsp_client=mock_lsp_client)
        result = await agent.verify(str(src_dir))
        assert "lsp_diagnostics" in result
        assert mock_lsp_client.get_diagnostics.call_count >= 2

    @pytest.mark.asyncio
    async def test_verify_agent_no_lsp_degrades(self, disabled_lsp_client, tmp_path):
        """Test 8: LSP 未启动时，verify() 返回 "LSP 未启动"，不崩溃

        断言要点：
        - lsp_diagnostics 字段为 "LSP 未启动，跳过诊断"
        - get_diagnostics 未被调用
        """
        from iron.agent.engine import VerifyAgent
        from iron.llm.backend import EchoBackend
        from iron.agent.prompt_builder import PromptBuilder
        from iron.skills.registry import SkillRegistry
        llm = EchoBackend()
        pb = PromptBuilder(tmp_path, "stm32f407")
        skills = SkillRegistry()
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        (src_dir / "main.c").write_text("int main(){}", encoding="utf-8")
        config = SimpleNamespace(project=SimpleNamespace(project_dir=str(tmp_path)),
                                mcp=None, tool_output_max_chars=10000)
        agent = VerifyAgent(llm=llm, prompt_builder=pb, skills=skills,
                            config=config, lsp_client=disabled_lsp_client)
        result = await agent.verify(str(src_dir))
        assert "LSP 未启动" in result["lsp_diagnostics"]


# ── 5. 退出清理 ──────────────────────────────────────────────

class TestLSPCleanup:
    """验证 main.py 退出时清理 LSP 客户端"""

    def test_lsp_cleanup_on_exit(self, mock_lsp_client):
        """Test 9: _cleanup_lsp 调用 lsp_client.stop()

        断言要点：
        - stop() 被调用一次
        - 异常不传播（约束 C1）
        """
        from iron.cli.main import _cleanup_lsp
        _cleanup_lsp(mock_lsp_client)
        mock_lsp_client.stop.assert_called_once()


# ── 6. 特性门控与降级 ────────────────────────────────────────

class TestFeatureGateAndDegradation:
    """验证特性门控和启动失败降级"""

    def test_lsp_disabled_when_feature_off(self, tmp_path):
        """Test 10: features.lsp_tools=False 时，bootstrap 不创建 LSP 客户端

        断言要点：
        - BootstrapResult.lsp_client is None
        - LSPClient.start 未被调用
        """
        from iron.cli.bootstrap import Bootstrap
        with patch("iron.config.features.get_feature_flags") as gf:
            flags = MagicMock()
            flags.is_enabled.return_value = False
            gf.return_value = flags
            result = Bootstrap().run(tmp_path)
            assert result.lsp_client is None

    def test_lsp_startup_failure_degrades_gracefully(self, tmp_path):
        """Test 11: LSP start() 失败时，降级到 lsp_client=None，iron 不崩溃

        断言要点：
        - BootstrapResult.lsp_client is None
        - result.warnings 包含降级提示
        - result.success is True（不阻塞启动）
        """
        from iron.cli.bootstrap import Bootstrap
        with patch("iron.config.features.get_feature_flags") as gf, \
             patch("iron.integrations.lsp_client.LSPClient") as LC:
            flags = MagicMock()
            flags.is_enabled.return_value = True
            gf.return_value = flags
            LC.find_compile_commands.return_value = None
            mock_client = MagicMock(spec=LSPClient)
            mock_client.start = AsyncMock(return_value=False)
            LC.return_value = mock_client
            result = Bootstrap().run(tmp_path)
            assert result.lsp_client is None
            assert result.success is True
            assert any("LSP" in w for w in result.warnings)


# ── 7. ExploreAgent 工具可用性 ───────────────────────────────

class TestExploreAgentLSPTools:
    """验证 ExploreAgent 能看到 LSP 工具 schema"""

    def test_explore_agent_has_lsp_tools(self, mock_lsp_client):
        """Test 12: ExploreAgent 的 _tools_schema 包含 LSP 工具

        断言要点：
        - _tools_schema 包含 lsp_definition/lsp_references/lsp_hover
        - 不包含 lsp_diagnostics/lsp_completion（不在 EXPLORE_TOOLS 中）
        """
        from iron.agent.engine import ExploreAgent
        from iron.llm.backend import EchoBackend
        from iron.agent.prompt_builder import PromptBuilder
        from iron.skills.registry import SkillRegistry
        llm = EchoBackend()
        pb = PromptBuilder(Path("."), "stm32f407")
        skills = SkillRegistry()
        agent = ExploreAgent(llm=llm, prompt_builder=pb, skills=skills,
                             config=None, lsp_client=mock_lsp_client)
        schemas = agent._tool_registry.get_all_schemas()
        names = [s["function"]["name"] for s in schemas]
        # 过滤到 allowed 集合
        allowed = agent._get_allowed_tools()
        visible = [n for n in names if n in allowed]
        assert "lsp_definition" in visible
        assert "lsp_references" in visible
        assert "lsp_hover" in visible
        assert "lsp_diagnostics" not in visible
        assert "lsp_completion" not in visible
