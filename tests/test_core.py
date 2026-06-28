"""Iron 主包测试 — 覆盖核心功能

运行: pytest tests/ -v
"""
import asyncio
import json
import tempfile
from pathlib import Path

import pytest

from iron.tools import create_default_registry
from iron.tools.skill_create import SkillCreateTool
from iron.tools.mcp_config import McpConfigTool
from iron.tools.remember import RememberTool
from iron.skills.registry import SkillRegistry, BUILTIN_SKILLS
from iron.agent.agent_manager import AgentManager
from iron.agent.memory import ProjectMemory


# ── 工具注册测试 ──────────────────────────────────────────────

class TestToolRegistry:
    """工具注册中心测试"""

    def test_all_tools_registered(self):
        """13 个内置工具全部注册"""
        registry = create_default_registry()
        names = registry.tool_names()
        assert len(names) == 13
        for expected in ["edit_file", "patch", "search_code", "find_files", "ask_user",
                         "task_track", "embed_build", "embed_flash", "embed_lint",
                         "remember", "web_search", "skill_create", "mcp_config"]:
            assert expected in names, f"缺少工具: {expected}"

    def test_get_tool_by_name(self):
        """按名称获取工具"""
        registry = create_default_registry()
        tool = registry.get("skill_create")
        assert tool is not None
        assert tool.name == "skill_create"

    def test_get_nonexistent_tool(self):
        """获取不存在的工具返回 None"""
        registry = create_default_registry()
        assert registry.get("nonexistent") is None

    def test_all_schemas_valid(self):
        """所有工具 schema 格式正确"""
        registry = create_default_registry()
        schemas = registry.get_all_schemas()
        assert len(schemas) == 13
        for schema in schemas:
            assert schema["type"] == "function"
            assert "function" in schema
            fn = schema["function"]
            assert "name" in fn
            assert "description" in fn
            assert "parameters" in fn


# ── Skill 系统测试 ────────────────────────────────────────────

class TestSkillSystem:
    """技能系统测试"""

    def test_builtin_skills_count(self):
        """8 个内置技能"""
        assert len(BUILTIN_SKILLS) == 8

    def test_builtin_skill_names(self):
        """内置技能名称正确"""
        names = [s.name for s in BUILTIN_SKILLS]
        expected = ["mcu-init", "driver-gen", "peripheral-setup", "bug-hunt",
                    "rtos-setup", "misra-check", "power-optimize", "debug-helper"]
        assert names == expected

    def test_skill_match(self):
        """技能匹配"""
        registry = SkillRegistry()
        matched = registry.match("帮我初始化一个 STM32 项目")
        assert len(matched) > 0
        assert matched[0].name == "mcu-init"

    def test_skill_match_bug_hunt(self):
        """问题诊断技能匹配"""
        registry = SkillRegistry()
        matched = registry.match("程序不工作，HardFault 了")
        assert len(matched) > 0
        assert matched[0].name == "bug-hunt"

    def test_skill_no_match(self):
        """无匹配时返回空列表"""
        registry = SkillRegistry()
        matched = registry.match("今天天气怎么样")
        assert len(matched) == 0

    def test_skill_execute_returns_prompt(self):
        """技能执行返回 prompt 注入"""
        registry = SkillRegistry()
        skill = registry.get_by_name("mcu-init")
        result = asyncio.run(skill.execute({}))
        assert result.success
        assert len(result.next_steps) > 0
        assert "MCU" in result.next_steps[0]

    def test_load_custom_skill_from_dir(self, tmp_path):
        """从目录加载用户自定义 skill"""
        # 创建测试 skill 文件
        skill_file = tmp_path / "test-skill.md"
        skill_file.write_text("""---
name: test-skill
description: 测试技能
icon: 🧪
trigger_patterns:
  - 测试
  - test
---

## 测试技能 prompt
这是测试内容。""", encoding="utf-8")

        registry = SkillRegistry()
        registry.load_from_dir(tmp_path)
        skill = registry.get_by_name("test-skill")
        assert skill is not None
        assert skill.description == "测试技能"
        assert skill.icon == "🧪"
        assert "测试" in skill.trigger_patterns


# ── Agent 管理器测试 ──────────────────────────────────────────

class TestAgentManager:
    """Agent 管理器测试"""

    def test_load_builtin_agents(self, tmp_path):
        """加载内置 Agent"""
        manager = AgentManager(str(tmp_path))
        agents = manager.list_agents()
        assert len(agents) > 0

    def test_get_current_agent(self, tmp_path):
        """获取当前 Agent"""
        manager = AgentManager(str(tmp_path))
        current = manager.get_current()
        assert current.name is not None

    def test_switch_agent(self, tmp_path):
        """切换 Agent"""
        manager = AgentManager(str(tmp_path))
        agents = manager.list_agents()
        if len(agents) > 1:
            first_name = agents[0]["name"]
            assert manager.switch(first_name)
            assert manager.get_current_name() == first_name

    def test_switch_nonexistent_agent(self, tmp_path):
        """切换不存在的 Agent 返回 False"""
        manager = AgentManager(str(tmp_path))
        assert not manager.switch("nonexistent")

    def test_get_permission_default(self, tmp_path):
        """获取默认权限"""
        manager = AgentManager(str(tmp_path))
        perm = manager.get_permission("edit")
        assert perm in ["allow", "ask", "deny"]

    def test_load_project_agent(self, tmp_path):
        """加载项目级 Agent"""
        agents_dir = tmp_path / ".iron" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "custom.md").write_text("""---
description: 自定义 Agent
mode: primary
permissions:
  read: allow
  edit: ask
  bash: deny
---
# Custom Agent
你是自定义 Agent。""", encoding="utf-8")

        manager = AgentManager(str(tmp_path))
        assert manager.switch("custom")
        agent = manager.get_current()
        assert agent.description == "自定义 Agent"
        assert agent.permissions["edit"] == "ask"
        assert agent.permissions["bash"] == "deny"


# ── 记忆系统测试 ──────────────────────────────────────────────

class TestMemorySystem:
    """记忆系统测试"""

    def test_project_memory_init(self, tmp_path):
        """项目记忆初始化"""
        memory = ProjectMemory(str(tmp_path))
        assert memory is not None

    def test_save_and_load_memory(self, tmp_path):
        """保存和加载记忆"""
        memory = ProjectMemory(str(tmp_path))
        memory.save_memory("# 测试记忆\n这是测试内容。")
        loaded = memory.load_memory()
        assert "测试记忆" in loaded
        assert "测试内容" in loaded

    def test_append_to_memory(self, tmp_path):
        """追加记忆到章节"""
        memory = ProjectMemory(str(tmp_path))
        memory.save_memory("# 项目记忆\n\n## 用户偏好\n- 喜欢中文\n")
        memory.append_to_memory("用户偏好", "要求代码注释用中文")
        loaded = memory.load_memory()
        assert "要求代码注释用中文" in loaded

    def test_build_context_injection(self, tmp_path):
        """构建上下文注入"""
        memory = ProjectMemory(str(tmp_path))
        memory.save_memory("# 项目记忆\n\n## 项目约定\n- 使用 HAL 库\n")
        injection = memory.build_context_injection()
        assert "项目约定" in injection or "使用 HAL 库" in injection


# ── skill_create 工具测试 ─────────────────────────────────────

class TestSkillCreateTool:
    """skill_create 工具测试"""

    def test_create_skill_success(self, tmp_path):
        """成功创建 skill"""
        tool = SkillCreateTool()
        result = asyncio.run(tool.execute({
            "name": "test-skill",
            "description": "测试技能",
            "prompt": "## 测试\n这是测试 prompt",
            "trigger_patterns": ["测试", "test"],
            "icon": "🧪",
        }, {"project_dir": str(tmp_path)}))

        assert result["success"]
        skill_file = tmp_path / ".iron" / "skills" / "test-skill.md"
        assert skill_file.exists()
        content = skill_file.read_text(encoding="utf-8")
        assert "test-skill" in content
        assert "测试技能" in content
        assert "测试 prompt" in content

    def test_create_skill_missing_fields(self, tmp_path):
        """缺少必填字段"""
        tool = SkillCreateTool()
        result = asyncio.run(tool.execute({
            "name": "",
            "description": "",
            "prompt": "",
        }, {"project_dir": str(tmp_path)}))
        assert not result["success"]
        assert "不能为空" in result["error"]

    def test_create_skill_invalid_name(self, tmp_path):
        """无效名称"""
        tool = SkillCreateTool()
        result = asyncio.run(tool.execute({
            "name": "invalid name!",
            "description": "测试",
            "prompt": "测试",
        }, {"project_dir": str(tmp_path)}))
        assert not result["success"]
        assert "name" in result["error"] and "字母数字" in result["error"]

    def test_create_duplicate_skill(self, tmp_path):
        """重复创建"""
        tool = SkillCreateTool()
        args = {
            "name": "dup-skill",
            "description": "测试",
            "prompt": "测试",
        }
        asyncio.run(tool.execute(args, {"project_dir": str(tmp_path)}))
        result = asyncio.run(tool.execute(args, {"project_dir": str(tmp_path)}))
        assert not result["success"]
        assert "已存在" in result["error"]


# ── mcp_config 工具测试 ───────────────────────────────────────

class TestMcpConfigTool:
    """mcp_config 工具测试"""

    def test_add_server(self, tmp_path):
        """添加 MCP 服务器"""
        tool = McpConfigTool()
        result = asyncio.run(tool.execute({
            "action": "add",
            "name": "test-mcp",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem"],
        }, {"project_dir": str(tmp_path)}))

        assert result["success"]
        config_file = tmp_path / "iron.yml"
        assert config_file.exists()

    def test_list_servers_empty(self, tmp_path):
        """列出空 MCP 配置"""
        tool = McpConfigTool()
        result = asyncio.run(tool.execute({
            "action": "list",
        }, {"project_dir": str(tmp_path)}))
        assert result["success"]
        assert result["count"] == 0

    def test_list_servers_after_add(self, tmp_path):
        """添加后列出 MCP"""
        tool = McpConfigTool()
        asyncio.run(tool.execute({
            "action": "add",
            "name": "test-mcp",
            "command": "npx",
            "args": ["-y", "test-server"],
        }, {"project_dir": str(tmp_path)}))

        result = asyncio.run(tool.execute({
            "action": "list",
        }, {"project_dir": str(tmp_path)}))
        assert result["success"]
        assert result["count"] == 1
        assert result["servers"][0]["name"] == "test-mcp"

    def test_remove_server(self, tmp_path):
        """移除 MCP 服务器"""
        tool = McpConfigTool()
        asyncio.run(tool.execute({
            "action": "add",
            "name": "to-remove",
            "command": "npx",
        }, {"project_dir": str(tmp_path)}))

        result = asyncio.run(tool.execute({
            "action": "remove",
            "name": "to-remove",
        }, {"project_dir": str(tmp_path)}))
        assert result["success"]

        # 验证已移除
        list_result = asyncio.run(tool.execute({
            "action": "list",
        }, {"project_dir": str(tmp_path)}))
        assert list_result["count"] == 0

    def test_remove_nonexistent_server(self, tmp_path):
        """移除不存在的 MCP"""
        tool = McpConfigTool()
        result = asyncio.run(tool.execute({
            "action": "remove",
            "name": "nonexistent",
        }, {"project_dir": str(tmp_path)}))
        assert not result["success"]

    def test_unknown_action(self, tmp_path):
        """未知 action"""
        tool = McpConfigTool()
        result = asyncio.run(tool.execute({
            "action": "unknown",
        }, {"project_dir": str(tmp_path)}))
        assert not result["success"]


# ── remember 工具测试 ────────────────────────────────────────

class TestRememberTool:
    """remember 工具测试"""

    def test_remember_success(self, tmp_path):
        """成功保存记忆"""
        tool = RememberTool()
        # remember 工具需要 engine 实例（通过 context 传入）
        memory = ProjectMemory(str(tmp_path))
        mock_engine = type("MockEngine", (), {"_memory": memory})()
        result = asyncio.run(tool.execute({
            "section": "用户偏好",
            "content": "要求代码注释用中文",
        }, {"engine": mock_engine}))
        assert result["success"]

        # 验证已写入
        loaded = memory.load_memory()
        assert "要求代码注释用中文" in loaded

    def test_remember_missing_fields(self, tmp_path):
        """缺少必填字段"""
        tool = RememberTool()
        result = asyncio.run(tool.execute({
            "section": "",
            "content": "",
        }, {"project_dir": str(tmp_path)}))
        assert not result["success"]
