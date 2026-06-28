"""Agent Engine 单元测试

覆盖 engine.py 的核心逻辑（不含网络依赖）：
- doom_loop 检测
- 读写工具分类
- 命令/文件风险评估
- 路径守卫
- undo 操作
- 文件树缓存
- Phase/FileSpec/Plan/AgentEvent 数据类
- _flush_readonly_tasks 异常日志

运行方式: pytest tests/test_engine.py -v
"""
import asyncio
import pytest
from pathlib import Path


class TestDoomLoop:
    """doom_loop 检测 — 连续 3 次相同调用应被拒绝"""

    def _make_engine(self):
        from types import SimpleNamespace
        from iron.agent.engine import AgentEngine
        from iron.agent.prompt_builder import PromptBuilder
        from iron.llm.backend import EchoBackend
        from iron.skills.registry import SkillRegistry

        config = SimpleNamespace(
            project=SimpleNamespace(project_dir=".", mcu="stm32f407", build_system="platformio"),
            mcp={},
        )
        return AgentEngine(
            llm=EchoBackend(),
            prompt_builder=PromptBuilder(Path(".")),
            skills=SkillRegistry(),
            config=config,
        )

    def test_no_false_positive_different_args(self):
        """不同参数的相同工具名不触发 doom_loop"""
        engine = self._make_engine()
        assert engine._check_doom_loop("write_file", {"path": "a.c", "content": "x"}) is False
        assert engine._check_doom_loop("write_file", {"path": "b.c", "content": "x"}) is False
        assert engine._check_doom_loop("write_file", {"path": "c.c", "content": "x"}) is False

    def test_same_call_3_times_trigger(self):
        """连续 3 次相同调用 → 触发 doom_loop"""
        engine = self._make_engine()
        engine._recent_calls = []
        # 两次不触发
        assert engine._check_doom_loop("write_file", {"path": "a.c", "content": "x"}) is False
        assert engine._check_doom_loop("write_file", {"path": "a.c", "content": "x"}) is False
        # 第三次触发
        assert engine._check_doom_loop("write_file", {"path": "a.c", "content": "x"}) is True
        # 触发后清空
        assert engine._recent_calls == []

    def test_different_tool_not_counted(self):
        """不同工具名不计入 doom_loop"""
        engine = self._make_engine()
        engine._recent_calls = []
        engine._check_doom_loop("write_file", {"path": "a.c"})
        engine._check_doom_loop("read_file", {"path": "a.c"})
        engine._check_doom_loop("run_command", {"command": "dir"})
        # 3 个不同工具，不触发
        assert engine._check_doom_loop("write_file", {"path": "a.c"}) is False


class TestReadonlyToolClassification:
    """_is_readonly_tool 分类正确性"""

    def _make_engine(self):
        from types import SimpleNamespace
        from iron.agent.engine import AgentEngine
        from iron.agent.prompt_builder import PromptBuilder
        from iron.llm.backend import EchoBackend
        from iron.skills.registry import SkillRegistry

        config = SimpleNamespace(
            project=SimpleNamespace(project_dir=".", mcu="stm32f407", build_system="platformio"),
            mcp={},
        )
        return AgentEngine(
            llm=EchoBackend(),
            prompt_builder=PromptBuilder(Path(".")),
            skills=SkillRegistry(),
            config=config,
        )

    def test_known_readonly_tools(self):
        engine = self._make_engine()
        assert engine._is_readonly_tool("search_code", {}) is True
        assert engine._is_readonly_tool("find_files", {}) is True
        assert engine._is_readonly_tool("web_search", {}) is True
        assert engine._is_readonly_tool("embed_lint", {}) is True

    def test_known_write_tools(self):
        engine = self._make_engine()
        assert engine._is_readonly_tool("embed_flash", {}) is False
        assert engine._is_readonly_tool("embed_build", {}) is False
        assert engine._is_readonly_tool("mcp_config", {}) is False

    def test_embed_build_action_info_is_readonly(self):
        engine = self._make_engine()
        assert engine._is_readonly_tool("embed_build", {"action": "info"}) is True
        assert engine._is_readonly_tool("embed_build", {"action": "compile"}) is False

    def test_task_track_list_is_readonly(self):
        engine = self._make_engine()
        assert engine._is_readonly_tool("task_track", {"action": "list"}) is True
        assert engine._is_readonly_tool("task_track", {"action": "create"}) is False


class TestCommandRiskEvaluation:
    """命令风险评估"""

    def _make_engine(self):
        from types import SimpleNamespace
        from iron.agent.engine import AgentEngine
        from iron.agent.prompt_builder import PromptBuilder
        from iron.llm.backend import EchoBackend
        from iron.skills.registry import SkillRegistry

        config = SimpleNamespace(
            project=SimpleNamespace(project_dir=".", mcu="stm32f407", build_system="platformio"),
            mcp={},
        )
        return AgentEngine(
            llm=EchoBackend(),
            prompt_builder=PromptBuilder(Path(".")),
            skills=SkillRegistry(),
            config=config,
        )

    def test_safe_commands(self):
        engine = self._make_engine()
        assert engine._evaluate_command_risk("gcc main.c -o main") == "safe"
        assert engine._evaluate_command_risk("make all") == "safe"
        assert engine._evaluate_command_risk("platformio run") == "safe"
        assert engine._evaluate_command_risk("git status") == "safe"
        assert engine._evaluate_command_risk("dir") == "safe"

    def test_dangerous_commands(self):
        engine = self._make_engine()
        assert engine._evaluate_command_risk("rm -rf /") == "dangerous"
        assert engine._evaluate_command_risk("del C:\\Windows") == "dangerous"
        assert engine._evaluate_command_risk("pip install flask") == "dangerous"
        assert engine._evaluate_command_risk("sudo rm -rf /") == "dangerous"

    def test_python_minus_c_is_dangerous(self):
        engine = self._make_engine()
        assert engine._evaluate_command_risk('python -c "import os; os.system(\"rm\")"') == "dangerous"
        assert engine._evaluate_command_risk("python3 --command 'os.system()'") == "dangerous"

    def test_node_minus_e_is_dangerous(self):
        engine = self._make_engine()
        assert engine._evaluate_command_risk("node -e 'require(\"child_process\").execSync(\"rm -rf /\")'") == "dangerous"

    def test_meta_characters_blocked(self):
        engine = self._make_engine()
        assert engine._evaluate_command_risk("gcc main.c -o main; rm -rf /") == "dangerous"
        assert engine._evaluate_command_risk("dir && rm -rf /") == "dangerous"


class TestWriteRiskEvaluation:
    """文件写入风险评估"""

    def _make_engine(self, project_dir: Path):
        from types import SimpleNamespace
        from iron.agent.engine import AgentEngine
        from iron.agent.prompt_builder import PromptBuilder
        from iron.llm.backend import EchoBackend
        from iron.skills.registry import SkillRegistry

        config = SimpleNamespace(
            project=SimpleNamespace(project_dir=str(project_dir), mcu="stm32f407", build_system="platformio"),
            mcp={},
        )
        return AgentEngine(
            llm=EchoBackend(),
            prompt_builder=PromptBuilder(project_dir),
            skills=SkillRegistry(),
            config=config,
        )

    def test_in_project_safe(self, tmp_path):
        project_dir = tmp_path / "test_project"
        project_dir.mkdir()
        engine = self._make_engine(project_dir)
        assert engine._evaluate_write_risk("src/main.c") == "safe"
        assert engine._evaluate_write_risk("lib/hello.c") == "safe"

    def test_outside_project_dangerous(self, tmp_path):
        project_dir = tmp_path / "test_project"
        project_dir.mkdir()
        outside_dir = tmp_path / "other_project"
        outside_dir.mkdir()
        engine = self._make_engine(project_dir)
        outside_file = outside_dir / "evil.c"
        outside_file.write_text("")
        assert engine._evaluate_write_risk(str(outside_file)) == "dangerous"
        assert engine._evaluate_write_risk("../../etc/passwd") == "dangerous"


class TestPathGuard:
    """路径越界守卫"""

    def _make_engine(self, project_dir: Path):
        from types import SimpleNamespace
        from iron.agent.engine import AgentEngine
        from iron.agent.prompt_builder import PromptBuilder
        from iron.llm.backend import EchoBackend
        from iron.skills.registry import SkillRegistry

        config = SimpleNamespace(
            project=SimpleNamespace(project_dir=str(project_dir), mcu="stm32f407", build_system="platformio"),
            mcp={},
        )
        return AgentEngine(
            llm=EchoBackend(),
            prompt_builder=PromptBuilder(project_dir),
            skills=SkillRegistry(),
            config=config,
        )

    def test_relative_path_resolved(self, tmp_path):
        project_dir = tmp_path / "safe_project"
        project_dir.mkdir()
        engine = self._make_engine(project_dir)
        resolved = engine._resolve_project_path("src/app.c")
        assert resolved.is_absolute()
        assert "safe_project" in str(resolved)

    def test_path_traversal_blocked(self, tmp_path):
        project_dir = tmp_path / "safe_project"
        project_dir.mkdir()
        outside_dir = tmp_path / "other_project"
        outside_dir.mkdir()
        engine = self._make_engine(project_dir)
        outside_file = outside_dir / "evil.c"
        outside_file.write_text("")
        with pytest.raises(ValueError, match="路径越界"):
            engine._resolve_project_path(str(outside_file))
        with pytest.raises(ValueError, match="路径越界"):
            engine._resolve_project_path("../../etc/passwd")

    def test_windows_reserved_names_blocked(self, tmp_path):
        project_dir = tmp_path / "safe_project"
        project_dir.mkdir()
        engine = self._make_engine(project_dir)
        with pytest.raises(ValueError, match="Windows 保留设备名"):
            engine._resolve_project_path("CON.txt")
        with pytest.raises(ValueError, match="Windows 保留设备名"):
            engine._resolve_project_path("NUL.c")


class TestDetectLanguage:
    """文件语言检测"""

    def _make_engine(self):
        from types import SimpleNamespace
        from iron.agent.engine import AgentEngine
        from iron.agent.prompt_builder import PromptBuilder
        from iron.llm.backend import EchoBackend
        from iron.skills.registry import SkillRegistry

        config = SimpleNamespace(
            project=SimpleNamespace(project_dir=".", mcu="stm32f407", build_system="platformio"),
            mcp={},
        )
        return AgentEngine(
            llm=EchoBackend(),
            prompt_builder=PromptBuilder(Path(".")),
            skills=SkillRegistry(),
            config=config,
        )

    def test_detect_languages(self):
        engine = self._make_engine()
        assert engine._detect_language("main.c") == "c"
        assert engine._detect_language("driver.h") == "c"
        assert engine._detect_language("server.py") == "python"
        assert engine._detect_language("main.rs") == "rust"
        assert engine._detect_language("app.js") == "javascript"
        assert engine._detect_language("utils.ts") == "typescript"
        assert engine._detect_language("server.go") == "go"
        assert engine._detect_language("Main.java") == "java"
        assert engine._detect_language("boot.s") == "asm"
        assert engine._detect_language("linker.ld") == "linker"
        assert engine._detect_language("README.md") == "markdown"
        assert engine._detect_language("config.json") == "json"
        assert engine._detect_language("settings.yaml") == "yaml"
        assert engine._detect_language("setup.cfg") == "text"


class TestFileTreeCache:
    """文件树缓存 — 同一 process 只扫描一次"""

    def _make_engine(self):
        from types import SimpleNamespace
        from iron.agent.engine import AgentEngine
        from iron.agent.prompt_builder import PromptBuilder
        from iron.llm.backend import EchoBackend
        from iron.skills.registry import SkillRegistry

        config = SimpleNamespace(
            project=SimpleNamespace(project_dir=".", mcu="stm32f407", build_system="platformio"),
            mcp={},
        )
        return AgentEngine(
            llm=EchoBackend(),
            prompt_builder=PromptBuilder(Path(".")),
            skills=SkillRegistry(),
            config=config,
        )

    def test_cache_populated_on_first_call(self):
        engine = self._make_engine()
        # 初始未加载
        assert engine._file_tree_loaded is False

        tree1 = engine._build_file_tree()
        # 调用后缓存已填充
        assert engine._file_tree_loaded is True
        assert engine._cached_file_tree == tree1

    def test_cache_avoids_second_scan(self):
        engine = self._make_engine()
        tree1 = engine._build_file_tree()
        engine._cached_file_tree = ["FAKE_FILE"]  # 模拟缓存
        tree2 = engine._build_file_tree()
        # 应返回缓存，不重新扫描
        assert tree2 == ["FAKE_FILE"]


class TestUndoWithOldContent:
    """undo 操作：优先用 old_content 全文件快照，fallback 到 old_string 替换"""

    @pytest.mark.asyncio
    async def test_undo_new_file_deletes(self, tmp_path):
        """新建文件的撤销应删除文件"""
        from unittest.mock import patch, AsyncMock, MagicMock
        from iron.agent.engine import AgentEngine
        from iron.agent.prompt_builder import PromptBuilder
        from iron.llm.backend import EchoBackend
        from iron.skills.registry import SkillRegistry

        config = MagicMock()
        config.project.project_dir = str(tmp_path)
        config.project.mcu = "stm32f407"
        config.project.build_system = "platformio"
        config.mcp = {}

        with patch.object(AgentEngine, '_get_project_dir', return_value=str(tmp_path)):
            engine = AgentEngine(
                llm=EchoBackend(),
                prompt_builder=PromptBuilder(Path(".")),
                skills=SkillRegistry(),
                config=config,
            )

        test_file = tmp_path / "new.c"
        test_file.write_text("new file content\n", encoding="utf-8")

        engine._change_history.append({
            "action": "新建",
            "path": "new.c",
        })

        result = await engine.undo_last()
        assert result is not None
        assert result["action"] == "新建"
        assert not test_file.exists()

    @pytest.mark.asyncio
    async def test_undo_edit_fallback_string_replace(self, tmp_path):
        """edit 撤销（无 old_content）：使用 old_string/new_string 字符串替换"""
        from unittest.mock import patch, MagicMock
        from iron.agent.engine import AgentEngine
        from iron.agent.prompt_builder import PromptBuilder
        from iron.llm.backend import EchoBackend
        from iron.skills.registry import SkillRegistry

        config = MagicMock()
        config.project.project_dir = str(tmp_path)
        config.project.mcu = "stm32f407"
        config.project.build_system = "platformio"
        config.mcp = {}

        with patch.object(AgentEngine, '_get_project_dir', return_value=str(tmp_path)):
            engine = AgentEngine(
                llm=EchoBackend(),
                prompt_builder=PromptBuilder(Path(".")),
                skills=SkillRegistry(),
                config=config,
            )

        test_file = tmp_path / "edit.c"
        # 文件当前内容 = edit 的 new_string（"changed text"）
        # undo 应把 "changed text" 替换回 "original text"
        test_file.write_text("line1\nchanged text\nline3\n", encoding="utf-8")

        engine._change_history.append({
            "action": "edit",
            "path": "edit.c",
            "old_content": None,
            # 模拟 edit_file 已将 "original text" 改为 "changed text"
            "old_string": "original text",
            "new_string": "changed text",
            "timestamp": 0.0,
        })

        result = await engine.undo_last()
        assert result is not None

        restored = test_file.read_text(encoding="utf-8")
        assert "original text" in restored
        assert "changed text" not in restored

    @pytest.mark.asyncio
    async def test_undo_returns_record(self):
        """undo 返回正确记录"""
        from unittest.mock import patch, MagicMock
        from iron.agent.engine import AgentEngine
        from iron.agent.prompt_builder import PromptBuilder
        from iron.llm.backend import EchoBackend
        from iron.skills.registry import SkillRegistry

        config = MagicMock()
        config.project.project_dir = "."
        config.project.mcu = "stm32f407"
        config.project.build_system = "platformio"
        config.mcp = {}

        engine = AgentEngine(
            llm=EchoBackend(),
            prompt_builder=PromptBuilder(Path(".")),
            skills=SkillRegistry(),
            config=config,
        )
        engine._change_history.append({"action": "新建", "path": "dummy.c"})
        result = await engine.undo_last()
        assert result is not None
        assert result["path"] == "dummy.c"

    @pytest.mark.asyncio
    async def test_undo_empty_history_returns_none(self):
        """空历史时 undo 返回 None"""
        from unittest.mock import MagicMock
        from iron.agent.engine import AgentEngine
        from iron.agent.prompt_builder import PromptBuilder
        from iron.llm.backend import EchoBackend
        from iron.skills.registry import SkillRegistry

        config = MagicMock()
        config.project.project_dir = "."
        config.project.mcu = "stm32f407"
        config.project.build_system = "platformio"
        config.mcp = {}

        engine = AgentEngine(
            llm=EchoBackend(),
            prompt_builder=PromptBuilder(Path(".")),
            skills=SkillRegistry(),
            config=config,
        )
        result = await engine.undo_last()
        assert result is None


class TestLastSummaryProperty:
    """ContextCompactor.last_summary 公共属性"""

    def test_last_summary_property(self):
        from iron.agent.memory import ContextCompactor
        compactor = ContextCompactor()
        assert compactor.last_summary == ""
        compactor._last_summary = "测试摘要内容"
        assert compactor.last_summary == "测试摘要内容"


class TestDataclasses:
    """Phase / FileSpec / Plan / AgentEvent 数据类"""

    def test_phase_enum(self):
        from iron.agent.engine import Phase
        assert Phase.THINK.value == "think"
        assert Phase.EXECUTE.value == "execute"
        assert Phase.DONE.value == "done"
        assert Phase.CHAT.value == "chat"

    def test_filespec_dataclass(self):
        from iron.agent.engine import FileSpec
        spec = FileSpec(path="main.c", action="新建", description="主程序", language="c")
        assert spec.path == "main.c"
        assert spec.action == "新建"

    def test_plan_dataclass(self):
        from iron.agent.engine import Plan
        plan = Plan(intent="点亮LED", modules=["gpio", "delay"])
        assert plan.intent == "点亮LED"
        assert "gpio" in plan.modules

    def test_agent_event_dataclass(self):
        from iron.agent.engine import AgentEvent
        ev = AgentEvent(type="step_done", data={"message": "完成"})
        assert ev.type == "step_done"
        assert ev.data["message"] == "完成"


class TestFlushReadonlyLogging:
    """_flush_readonly_tasks 异常时记录日志"""

    def _make_engine(self):
        from types import SimpleNamespace
        from iron.agent.engine import AgentEngine
        from iron.agent.prompt_builder import PromptBuilder
        from iron.llm.backend import EchoBackend
        from iron.skills.registry import SkillRegistry

        config = SimpleNamespace(
            project=SimpleNamespace(project_dir=".", mcu="stm32f407", build_system="platformio"),
            mcp={},
        )
        return AgentEngine(
            llm=EchoBackend(),
            prompt_builder=PromptBuilder(Path(".")),
            skills=SkillRegistry(),
            config=config,
        )

    @pytest.mark.asyncio
    async def test_single_task_exception_logged(self, caplog):
        """单个只读任务异常时记录 warning 日志"""
        import logging
        engine = self._make_engine()

        async def bad_task():
            raise RuntimeError("工具执行失败")

        pending = [("call_1", "search_code", {}, asyncio.ensure_future(bad_task()))]
        tool_results = []

        with caplog.at_level(logging.WARNING):
            await engine._flush_readonly_tasks(pending, tool_results)

        assert len(tool_results) == 1
        assert tool_results[0]["role"] == "tool"
        parsed = __import__("json").loads(tool_results[0]["content"])
        assert parsed["success"] is False
        assert "RuntimeError" in parsed["error"]
        assert any("search_code" in record.message for record in caplog.records)


class TestConstantsShared:
    """共享常量正确导出"""

    def test_echo_keywords_importable(self):
        from iron.constants import ECHO_COMPILE_KEYWORDS, ECHO_CHAT_KEYWORDS
        assert "编译" in ECHO_COMPILE_KEYWORDS
        assert "build" in ECHO_COMPILE_KEYWORDS
        assert "你好" in ECHO_CHAT_KEYWORDS
        assert "hello" in ECHO_CHAT_KEYWORDS

    def test_engine_constants_importable(self):
        from iron.constants import SOURCE_EXTENSIONS, CHAT_INDICATORS
        assert ".c" in SOURCE_EXTENSIONS
        assert ".py" in SOURCE_EXTENSIONS
        assert "是的，我会" in CHAT_INDICATORS


class TestEngineToolRegistry:
    """工具注册表集成"""

    def _make_engine(self):
        from types import SimpleNamespace
        from iron.agent.engine import AgentEngine
        from iron.agent.prompt_builder import PromptBuilder
        from iron.llm.backend import EchoBackend
        from iron.skills.registry import SkillRegistry

        config = SimpleNamespace(
            project=SimpleNamespace(project_dir=".", mcu="stm32f407", build_system="platformio"),
            mcp={},
        )
        return AgentEngine(
            llm=EchoBackend(),
            prompt_builder=PromptBuilder(Path(".")),
            skills=SkillRegistry(),
            config=config,
        )

    def test_builtin_schemas_imported(self):
        """BUILTIN_SCHEMAS 从 engine_builtins 正确导入"""
        from iron.agent.engine import _BUILTIN_SCHEMAS
        names = {s["function"]["name"] for s in _BUILTIN_SCHEMAS}
        assert names == {"write_file", "run_command", "read_file", "chat"}
