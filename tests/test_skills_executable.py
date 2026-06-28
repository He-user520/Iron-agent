"""可执行 Skill 系统测试 — v2.8 新增

测试 ExecutableSkill 的核心能力：
- 工具注册（get_tools）
- 预处理/后处理（pre_execute/post_execute）
- prompt 构建（build_prompt）
- 向后兼容（PromptSkill 仍工作）
"""
import asyncio
import pytest
from iron.skills.base import ExecutableSkill, SkillContext, SkillResult, BaseSkill
from iron.skills.executable import (
    McuInitSkill, DriverGenSkill, BugHuntSkill, MisraCheckSkill,
    EXECUTABLE_SKILLS,
)
from iron.skills.registry import BUILTIN_SKILLS, SkillRegistry
from iron.tools.skill_tools import MCUInitTool, DriverGenTool, BugHuntTool, MisraCheckTool


class TestExecutableSkillBase:
    """ExecutableSkill 抽象基类测试"""

    def test_executable_skill_is_subclass_of_base(self):
        """ExecutableSkill 继承 BaseSkill"""
        assert issubclass(ExecutableSkill, BaseSkill)

    def test_executable_skill_default_methods(self):
        """默认方法返回空/成功"""
        # 用一个最小实现测试基类默认行为
        class MinimalSkill(ExecutableSkill):
            name = "minimal"
            description = "test"
            trigger_patterns = ["test"]
            icon = "T"

            def can_handle(self, user_input, intent=None):
                return "test" in user_input.lower()

        skill = MinimalSkill()
        ctx = SkillContext(user_input="test")

        # get_tools 默认返回空
        assert skill.get_tools() == []

        # pre_execute 默认成功
        result = asyncio.run(skill.pre_execute(ctx))
        assert result.success is True

        # post_execute 默认成功
        result = asyncio.run(skill.post_execute(ctx, None))
        assert result.success is True

        # build_prompt 默认空
        assert skill.build_prompt(ctx) == ""

    def test_execute_backward_compatible(self):
        """execute() 兼容旧接口（返回 next_steps）"""
        skill = McuInitSkill()
        result = asyncio.run(skill.execute({}))
        assert result.success is True
        assert len(result.next_steps) > 0  # mcu-init 有 prompt


class TestFourExecutableSkills:
    """4 个可执行 Skill 子类测试"""

    def test_four_executable_skills_exist(self):
        """4 个可执行 Skill 实例"""
        assert len(EXECUTABLE_SKILLS) == 4
        names = {s.name for s in EXECUTABLE_SKILLS}
        assert names == {"mcu-init", "driver-gen", "bug-hunt", "misra-check"}

    def test_mcu_init_skill_registers_tool(self):
        """mcu-init 注册 MCUInitTool"""
        skill = McuInitSkill()
        tools = skill.get_tools()
        assert len(tools) == 1
        assert isinstance(tools[0], MCUInitTool)
        assert tools[0].name == "skill_mcu_init"

    def test_driver_gen_skill_registers_tool(self):
        """driver-gen 注册 DriverGenTool"""
        skill = DriverGenSkill()
        tools = skill.get_tools()
        assert len(tools) == 1
        assert isinstance(tools[0], DriverGenTool)
        assert tools[0].name == "skill_driver_gen"

    def test_bug_hunt_skill_registers_tool(self):
        """bug-hunt 注册 BugHuntTool"""
        skill = BugHuntSkill()
        tools = skill.get_tools()
        assert len(tools) == 1
        assert isinstance(tools[0], BugHuntTool)
        assert tools[0].name == "skill_bug_hunt"

    def test_misra_check_skill_registers_tool(self):
        """misra-check 注册 MisraCheckTool"""
        skill = MisraCheckSkill()
        tools = skill.get_tools()
        assert len(tools) == 1
        assert isinstance(tools[0], MisraCheckTool)
        assert tools[0].name == "skill_misra_check"

    def test_all_skills_build_prompt(self):
        """所有 4 个 Skill 都能构建 prompt"""
        ctx = SkillContext()
        for skill in EXECUTABLE_SKILLS:
            prompt = skill.build_prompt(ctx)
            assert isinstance(prompt, str)
            assert len(prompt) > 0, f"{skill.name} 的 prompt 为空"


class TestPreExecute:
    """pre_execute 行为测试"""

    def test_mcu_init_pre_execute_sets_session_data(self):
        """mcu-init pre_execute 设置 session_data"""
        skill = McuInitSkill()
        ctx = SkillContext()
        result = asyncio.run(skill.pre_execute(ctx))
        assert result.success is True
        assert ctx.session_data.get("mcu_init_active") is True

    def test_driver_gen_pre_execute_no_mcu_file(self):
        """driver-gen pre_execute 无 target-mcu.md 时正常返回"""
        skill = DriverGenSkill()
        ctx = SkillContext(project_root="/nonexistent/path")
        result = asyncio.run(skill.pre_execute(ctx))
        assert result.success is True

    def test_bug_hunt_pre_execute_no_lsp(self):
        """bug-hunt pre_execute 无 LSP 客户端时跳过"""
        skill = BugHuntSkill()
        ctx = SkillContext(lsp_client=None)
        result = asyncio.run(skill.pre_execute(ctx))
        assert result.success is True
        assert "LSP" in result.message or "跳过" in result.message

    def test_misra_check_pre_execute_sets_flag(self):
        """misra-check pre_execute 设置激活标志"""
        skill = MisraCheckSkill()
        ctx = SkillContext()
        result = asyncio.run(skill.pre_execute(ctx))
        assert result.success is True
        assert ctx.session_data.get("misra_check_active") is True


class TestPostExecute:
    """post_execute 行为测试"""

    def test_misra_check_post_execute_with_violations(self):
        """misra-check post_execute 记录违规数"""
        skill = MisraCheckSkill()
        ctx = SkillContext()
        result = asyncio.run(skill.post_execute(ctx, {"violations": ["v1", "v2"]}))
        assert result.success is True
        assert ctx.session_data.get("misra_violation_count") == 2

    def test_misra_check_post_execute_no_violations(self):
        """misra-check post_execute 无违规时正常返回"""
        skill = MisraCheckSkill()
        ctx = SkillContext()
        result = asyncio.run(skill.post_execute(ctx, {"violations": []}))
        assert result.success is True

    def test_default_post_execute(self):
        """默认 post_execute 返回成功"""
        skill = McuInitSkill()
        ctx = SkillContext()
        result = asyncio.run(skill.post_execute(ctx, None))
        assert result.success is True


class TestSkillRegistryIntegration:
    """SkillRegistry 集成测试"""

    def test_registry_contains_executable_skills(self):
        """SkillRegistry 包含可执行 Skill"""
        registry = SkillRegistry()
        for name in ["mcu-init", "driver-gen", "bug-hunt", "misra-check"]:
            skill = registry.get_by_name(name)
            assert skill is not None, f"{name} 不在 registry 中"
            assert isinstance(skill, ExecutableSkill), f"{name} 不是 ExecutableSkill"

    def test_registry_contains_prompt_skills(self):
        """SkillRegistry 仍包含 PromptSkill（向后兼容）"""
        registry = SkillRegistry()
        for name in ["peripheral-setup", "rtos-setup", "power-optimize", "debug-helper"]:
            skill = registry.get_by_name(name)
            assert skill is not None, f"{name} 不在 registry 中"
            assert not isinstance(skill, ExecutableSkill), f"{name} 不应是 ExecutableSkill"

    def test_builtin_skills_count_is_8(self):
        """内置 Skill 总数仍为 8"""
        assert len(BUILTIN_SKILLS) == 8

    def test_match_mcu_init(self):
        """匹配 mcu-init"""
        registry = SkillRegistry()
        matched = registry.match("帮我初始化一个 STM32 项目")
        assert len(matched) > 0
        assert matched[0].name == "mcu-init"
        assert isinstance(matched[0], ExecutableSkill)

    def test_match_bug_hunt(self):
        """匹配 bug-hunt"""
        registry = SkillRegistry()
        matched = registry.match("程序不工作，HardFault 了")
        assert len(matched) > 0
        assert matched[0].name == "bug-hunt"
        assert isinstance(matched[0], ExecutableSkill)


class TestSkillTools:
    """Skill 专属工具测试"""

    def test_mcu_init_tool_execute(self):
        """MCUInitTool 生成 platformio.ini"""
        tool = MCUInitTool()
        result = asyncio.run(tool.execute({"mcu": "STM32F407"}, {}))
        assert result["success"] is True
        assert "platformio_ini" in result
        assert "ststm32" in result["platformio_ini"]

    def test_mcu_init_tool_missing_param(self):
        """MCUInitTool 缺少参数返回错误"""
        tool = MCUInitTool()
        result = asyncio.run(tool.execute({}, {}))
        assert result["success"] is False

    def test_driver_gen_tool_execute(self):
        """DriverGenTool 生成驱动模板"""
        tool = DriverGenTool()
        result = asyncio.run(tool.execute({"peripheral": "uart"}, {}))
        assert result["success"] is True
        assert "header_content" in result
        assert "source_content" in result
        assert "UART" in result["header_content"]

    def test_driver_gen_tool_invalid_peripheral(self):
        """DriverGenTool 不支持的外设返回错误"""
        tool = DriverGenTool()
        result = asyncio.run(tool.execute({"peripheral": "invalid"}, {}))
        assert result["success"] is False

    def test_bug_hunt_tool_execute(self):
        """BugHuntTool 生成诊断清单"""
        tool = BugHuntTool()
        result = asyncio.run(tool.execute({"symptom": "hardfault"}, {}))
        assert result["success"] is True
        assert "checklist" in result
        assert len(result["checklist"]) > 0

    def test_misra_check_tool_execute(self):
        """MisraCheckTool 执行检查"""
        tool = MisraCheckTool()
        result = asyncio.run(tool.execute({"files": ["src/main.c"]}, {}))
        assert result["success"] is True
        assert "rules_applied" in result
        assert len(result["rules_applied"]) == 15

    def test_misra_check_tool_missing_files(self):
        """MisraCheckTool 缺少 files 参数返回错误"""
        tool = MisraCheckTool()
        result = asyncio.run(tool.execute({}, {}))
        assert result["success"] is False

    def test_all_tools_have_valid_schema(self):
        """所有 Skill 工具有有效 schema"""
        for tool_cls in [MCUInitTool, DriverGenTool, BugHuntTool, MisraCheckTool]:
            tool = tool_cls()
            schema = tool.schema
            assert "type" in schema
            assert schema["type"] == "function"
            assert "function" in schema
            assert schema["function"]["name"] == tool.name
