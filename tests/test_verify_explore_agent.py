"""P3-4 专门化子代理扩展测试 — VerifyAgent + ExploreAgent

测试用例：
1. test_verify_agent_tools: VerifyAgent 工具集正确
2. test_verify_agent_prompt: 系统提示包含"验证代理"
3. test_verify_agent_inheritance: VerifyAgent 是 TaskAgentEngine 子类
4. test_verify_agent_blocks_write: 写工具被阻止
5. test_verify_agent_allows_readonly: 只读工具允许
6. test_explore_agent_tools: ExploreAgent 工具集正确
7. test_explore_agent_prompt: 系统提示包含"探索代理"
8. test_explore_agent_inheritance: ExploreAgent 是 TaskAgentEngine 子类
9. test_explore_agent_blocks_command: 不允许 run_command
10. test_explore_agent_allows_lsp: 允许 LSP 工具
11. test_verify_method: verify() 方法存在且可调用
12. test_explore_method: explore() 方法存在且可调用

运行方式: pytest tests/test_verify_explore_agent.py -v
"""
import inspect
from pathlib import Path
from types import SimpleNamespace


def _make_config():
    """构建测试用 config（SimpleNamespace，与 test_task_agent.py 风格一致）"""
    return SimpleNamespace(
        project=SimpleNamespace(project_dir=".", mcu="stm32f407", build_system="platformio"),
        mcp={},
    )


def _make_engine(engine_class):
    """构建指定类型的 engine 实例（共用工厂，减少重复）"""
    from iron.agent.prompt_builder import PromptBuilder
    from iron.llm.backend import EchoBackend
    from iron.skills.registry import SkillRegistry
    return engine_class(
        llm=EchoBackend(),
        prompt_builder=PromptBuilder(Path(".")),
        skills=SkillRegistry(),
        config=_make_config(),
    )


def _make_schema(name: str) -> dict:
    """构造工具 schema（仅含 name，用于过滤测试）"""
    return {"function": {"name": name}}


# ── 1. VerifyAgent 工具集正确 ────────────────────────────────


class TestVerifyAgentTools:
    """VerifyAgent 工具集测试"""

    def test_verify_agent_tools(self):
        """VerifyAgent 工具集包含只读工具 + run_command_readonly"""
        from iron.agent.engine import VerifyAgent, TaskAgentEngine
        engine = _make_engine(VerifyAgent)
        allowed = engine._get_allowed_tools()

        # 只读工具应该在允许集合内（继承自 TaskAgentEngine.READONLY_TOOLS）
        for name in TaskAgentEngine.READONLY_TOOLS:
            assert name in allowed, f"只读工具 {name} 应在 VerifyAgent 工具集中"

        # run_command_readonly 是 VerifyAgent 的标志工具
        assert "run_command_readonly" in allowed, "run_command_readonly 应在 VerifyAgent 工具集中"


# ── 2. VerifyAgent 系统提示包含"验证代理" ────────────────────


class TestVerifyAgentPrompt:
    """VerifyAgent 系统提示前缀测试"""

    def test_verify_agent_prompt(self):
        """VerifyAgent 系统提示包含"验证代理"标记"""
        from iron.agent.engine import VerifyAgent
        engine = _make_engine(VerifyAgent)
        # _get_system_prompt_prefix 应返回包含"验证代理"的字符串
        prefix = engine._get_system_prompt_prefix()
        assert "验证代理" in prefix, "VerifyAgent 前缀应包含'验证代理'"

        # 完整系统提示也应包含"验证代理"
        system_prompt = engine._build_system_prompt()
        assert "验证代理" in system_prompt, "VerifyAgent 系统提示应包含'验证代理'"


# ── 3. VerifyAgent 是 TaskAgentEngine 子类 ────────────────────


class TestVerifyAgentInheritance:
    """VerifyAgent 继承关系测试"""

    def test_verify_agent_inheritance(self):
        """VerifyAgent 是 TaskAgentEngine 子类"""
        from iron.agent.engine import VerifyAgent, TaskAgentEngine, BaseAgentEngine
        assert issubclass(VerifyAgent, TaskAgentEngine), \
            "VerifyAgent 应继承自 TaskAgentEngine"
        assert issubclass(VerifyAgent, BaseAgentEngine), \
            "VerifyAgent 应继承自 BaseAgentEngine"


# ── 4. VerifyAgent 阻止写工具 ────────────────────────────────


class TestVerifyAgentBlocksWrite:
    """VerifyAgent 写工具阻止测试"""

    def test_verify_agent_blocks_write(self):
        """write_file/edit_file/embed_flash 被 VerifyAgent 阻止"""
        from iron.agent.engine import VerifyAgent
        engine = _make_engine(VerifyAgent)
        schemas = [
            _make_schema("write_file"),
            _make_schema("edit_file"),
            _make_schema("embed_flash"),
            _make_schema("embed_build"),
            _make_schema("read_file"),
        ]
        filtered = engine._filter_tools_schema(schemas)
        names = [s["function"]["name"] for s in filtered]
        # 写工具被阻止
        assert "write_file" not in names, "write_file 应被 VerifyAgent 阻止"
        assert "edit_file" not in names, "edit_file 应被 VerifyAgent 阻止"
        assert "embed_flash" not in names, "embed_flash 应被 VerifyAgent 阻止"
        assert "embed_build" not in names, "embed_build 应被 VerifyAgent 阻止"
        # 只读工具仍可用
        assert "read_file" in names, "read_file 应被 VerifyAgent 允许"


# ── 5. VerifyAgent 允许只读工具 ──────────────────────────────


class TestVerifyAgentAllowsReadonly:
    """VerifyAgent 只读工具允许测试"""

    def test_verify_agent_allows_readonly(self):
        """read_file/search_code/find_files/embed_lint 被 VerifyAgent 允许"""
        from iron.agent.engine import VerifyAgent
        engine = _make_engine(VerifyAgent)
        schemas = [
            _make_schema("read_file"),
            _make_schema("search_code"),
            _make_schema("find_files"),
            _make_schema("web_search"),
            _make_schema("embed_lint"),
            _make_schema("chat"),
        ]
        filtered = engine._filter_tools_schema(schemas)
        names = [s["function"]["name"] for s in filtered]
        for name in ["read_file", "search_code", "find_files", "web_search", "embed_lint", "chat"]:
            assert name in names, f"{name} 应被 VerifyAgent 允许"


# ── 6. ExploreAgent 工具集正确 ────────────────────────────────


class TestExploreAgentTools:
    """ExploreAgent 工具集测试"""

    def test_explore_agent_tools(self):
        """ExploreAgent 工具集包含纯只读工具 + LSP 跳转工具"""
        from iron.agent.engine import ExploreAgent
        engine = _make_engine(ExploreAgent)
        allowed = engine._get_allowed_tools()

        # 纯只读工具
        for name in ["read_file", "list_files", "search_code", "grep", "glob"]:
            assert name in allowed, f"只读工具 {name} 应在 ExploreAgent 工具集中"

        # LSP 跳转工具
        for name in ["lsp_definition", "lsp_references", "lsp_hover"]:
            assert name in allowed, f"LSP 工具 {name} 应在 ExploreAgent 工具集中"


# ── 7. ExploreAgent 系统提示包含"探索代理" ────────────────────


class TestExploreAgentPrompt:
    """ExploreAgent 系统提示前缀测试"""

    def test_explore_agent_prompt(self):
        """ExploreAgent 系统提示包含"探索代理"标记"""
        from iron.agent.engine import ExploreAgent
        engine = _make_engine(ExploreAgent)
        # _get_system_prompt_prefix 应返回包含"探索代理"的字符串
        prefix = engine._get_system_prompt_prefix()
        assert "探索代理" in prefix, "ExploreAgent 前缀应包含'探索代理'"

        # 完整系统提示也应包含"探索代理"
        system_prompt = engine._build_system_prompt()
        assert "探索代理" in system_prompt, "ExploreAgent 系统提示应包含'探索代理'"


# ── 8. ExploreAgent 是 TaskAgentEngine 子类 ────────────────────


class TestExploreAgentInheritance:
    """ExploreAgent 继承关系测试"""

    def test_explore_agent_inheritance(self):
        """ExploreAgent 是 TaskAgentEngine 子类"""
        from iron.agent.engine import ExploreAgent, TaskAgentEngine, BaseAgentEngine
        assert issubclass(ExploreAgent, TaskAgentEngine), \
            "ExploreAgent 应继承自 TaskAgentEngine"
        assert issubclass(ExploreAgent, BaseAgentEngine), \
            "ExploreAgent 应继承自 BaseAgentEngine"


# ── 9. ExploreAgent 不允许 run_command ───────────────────────


class TestExploreAgentBlocksCommand:
    """ExploreAgent 命令阻止测试"""

    def test_explore_agent_blocks_command(self):
        """run_command 被 ExploreAgent 阻止"""
        from iron.agent.engine import ExploreAgent
        engine = _make_engine(ExploreAgent)
        allowed = engine._get_allowed_tools()
        # run_command 不在允许集合内
        assert "run_command" not in allowed, "run_command 应被 ExploreAgent 阻止"
        assert "run_command_readonly" not in allowed, \
            "run_command_readonly 也应被 ExploreAgent 阻止（纯只读）"

        # schema 过滤也阻止 run_command
        schemas = [_make_schema("run_command"), _make_schema("read_file")]
        filtered = engine._filter_tools_schema(schemas)
        names = [s["function"]["name"] for s in filtered]
        assert "run_command" not in names, "run_command 应被 ExploreAgent 阻止"
        assert "read_file" in names


# ── 10. ExploreAgent 允许 LSP 工具 ────────────────────────────


class TestExploreAgentAllowsLsp:
    """ExploreAgent LSP 工具允许测试"""

    def test_explore_agent_allows_lsp(self):
        """lsp_definition/lsp_references/lsp_hover 被 ExploreAgent 允许"""
        from iron.agent.engine import ExploreAgent
        engine = _make_engine(ExploreAgent)
        schemas = [
            _make_schema("lsp_definition"),
            _make_schema("lsp_references"),
            _make_schema("lsp_hover"),
            _make_schema("read_file"),
        ]
        filtered = engine._filter_tools_schema(schemas)
        names = [s["function"]["name"] for s in filtered]
        for name in ["lsp_definition", "lsp_references", "lsp_hover", "read_file"]:
            assert name in names, f"{name} 应被 ExploreAgent 允许"


# ── 11. verify() 方法存在且可调用 ──────────────────────────────


class TestVerifyMethod:
    """verify() 方法存在性测试"""

    def test_verify_method(self):
        """verify() 方法存在且可调用"""
        from iron.agent.engine import VerifyAgent
        # 类级别：verify 方法存在
        assert hasattr(VerifyAgent, "verify"), "VerifyAgent 类应有 verify 方法"
        assert callable(getattr(VerifyAgent, "verify")), "verify 应可调用"

        # 实例级别：verify 是协程方法
        engine = _make_engine(VerifyAgent)
        assert hasattr(engine, "verify"), "VerifyAgent 实例应有 verify 方法"
        assert callable(engine.verify), "verify 应可调用"

        # 验证方法签名：target 参数默认为 "src/"
        sig = inspect.signature(engine.verify)
        assert "target" in sig.parameters, "verify 应有 target 参数"
        default = sig.parameters["target"].default
        assert default == "src/", f"verify 的 target 默认值应为 'src/'，实际为 {default!r}"

        # verify 是协程方法（返回 async generator 或 coroutine）
        assert inspect.iscoroutinefunction(engine.verify), "verify 应为 async 方法"


# ── 12. explore() 方法存在且可调用 ─────────────────────────────


class TestExploreMethod:
    """explore() 方法存在性测试"""

    def test_explore_method(self):
        """explore() 方法存在且可调用"""
        from iron.agent.engine import ExploreAgent
        # 类级别：explore 方法存在
        assert hasattr(ExploreAgent, "explore"), "ExploreAgent 类应有 explore 方法"
        assert callable(getattr(ExploreAgent, "explore")), "explore 应可调用"

        # 实例级别：explore 是协程方法
        engine = _make_engine(ExploreAgent)
        assert hasattr(engine, "explore"), "ExploreAgent 实例应有 explore 方法"
        assert callable(engine.explore), "explore 应可调用"

        # 验证方法签名：query 参数（无默认值，必填）
        sig = inspect.signature(engine.explore)
        assert "query" in sig.parameters, "explore 应有 query 参数"

        # explore 是协程方法
        assert inspect.iscoroutinefunction(engine.explore), "explore 应为 async 方法"
