"""MCP 客户端与配置单元测试 — 覆盖 iron/mcp/client.py 和 iron/config/settings.py 中的 MCPConfig

运行方式:
    pytest tests/test_mcp.py -v

或单独运行某个测试类:
    pytest tests/test_mcp.py::TestMCPConfig -v
    pytest tests/test_mcp.py::TestMCPToolWrapper -v

测试覆盖:
    1. MCPConfig（settings.py）— build_command()、默认值、自定义值（P1-4 重点）
    2. MCPClient（mcp/client.py）— add_server、get_tools、MCPToolWrapper
    3. IronConfig._merge_yaml 的 MCP 加载（P1-4 配置格式统一）
    4. mcp_config 工具的配置读写（P1-4 键名统一）

注意:
    - MCPClient.connect_local 会启动子进程，测试时通过 mock 跳过
    - 使用 pytest 和 asyncio（pyproject.toml 中 asyncio_mode = "auto"）
    - 对于需要 YAML 的测试，依赖 pyyaml（已在项目 dependencies 中）
"""
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import yaml

from iron.config.settings import MCPConfig, IronConfig
from iron.mcp.client import MCPClient, MCPToolWrapper
from iron.tools.mcp_config import McpConfigTool


# ── MCPConfig 测试（settings.py）────────────────────────────────

class TestMCPConfig:
    """MCPConfig dataclass 测试 — 默认值、自定义值、build_command()"""

    def test_default_values(self):
        """默认值：type/command/args/env/enabled/timeout 全部符合预期"""
        cfg = MCPConfig()
        assert cfg.type == "local"
        assert cfg.command == ""
        assert cfg.args == []
        assert cfg.env == {}
        assert cfg.enabled is True
        assert cfg.timeout == 5000

    def test_default_url_empty(self):
        """url 默认为空字符串"""
        cfg = MCPConfig()
        assert cfg.url == ""

    def test_custom_values_assigned(self):
        """自定义值正确赋值"""
        cfg = MCPConfig(
            type="remote",
            command="npx",
            args=["-y", "@modelcontextprotocol/server-filesystem", "."],
            env={"API_KEY": "xxx"},
            url="https://example.com/mcp",
            enabled=False,
            timeout=10000,
        )
        assert cfg.type == "remote"
        assert cfg.command == "npx"
        assert cfg.args == ["-y", "@modelcontextprotocol/server-filesystem", "."]
        assert cfg.env == {"API_KEY": "xxx"}
        assert cfg.url == "https://example.com/mcp"
        assert cfg.enabled is False
        assert cfg.timeout == 10000

    def test_build_command_with_command_and_args(self):
        """build_command()：command + args 拼接成完整命令列表"""
        cfg = MCPConfig(command="npx", args=["-y", "@modelcontextprotocol/server-filesystem", "."])
        cmd = cfg.build_command()
        assert cmd == ["npx", "-y", "@modelcontextprotocol/server-filesystem", "."]

    def test_build_command_empty_command_returns_args_only(self):
        """build_command() 当 command 为空时返回 args（无 command 前缀）

        实现逻辑：cmd = [command] if command else []; cmd.extend(args)
        所以 command 为空时，结果只有 args 部分。
        """
        cfg = MCPConfig(command="", args=["-y", "some-server"])
        cmd = cfg.build_command()
        # command 为空时，不包含 command 前缀，但 args 仍会拼接
        assert cmd == ["-y", "some-server"]

    def test_build_command_empty_command_and_empty_args_returns_empty(self):
        """build_command() 当 command 和 args 都为空时返回空列表"""
        cfg = MCPConfig(command="", args=[])
        cmd = cfg.build_command()
        assert cmd == []

    def test_build_command_no_args(self):
        """build_command() 无 args 时只返回 [command]"""
        cfg = MCPConfig(command="python")
        cmd = cfg.build_command()
        assert cmd == ["python"]

    def test_build_command_default_empty(self):
        """build_command() 默认配置（command 和 args 都为空）返回空列表"""
        cfg = MCPConfig()
        assert cfg.build_command() == []

    def test_build_command_non_string_args_converted_to_str(self):
        """build_command() args 中的非字符串元素被转为 str"""
        cfg = MCPConfig(command="node", args=[123, True, 3.14, "-y"])
        cmd = cfg.build_command()
        # 所有元素都应该是 str 类型
        assert all(isinstance(c, str) for c in cmd)
        assert cmd == ["node", "123", "True", "3.14", "-y"]

    def test_build_command_does_not_mutate_args(self):
        """build_command() 不应修改原始 args 列表"""
        original_args = ["-y", "server"]
        cfg = MCPConfig(command="npx", args=original_args)
        cfg.build_command()
        # 原始列表应保持不变
        assert cfg.args == ["-y", "server"]
        assert original_args == ["-y", "server"]

    def test_independent_instances_default_args(self):
        """多个 MCPConfig 实例的默认 args 互不影响（避免可变默认值陷阱）"""
        a = MCPConfig()
        b = MCPConfig()
        a.args.append("hack")
        assert b.args == []

    def test_independent_instances_default_env(self):
        """多个 MCPConfig 实例的默认 env 互不影响"""
        a = MCPConfig()
        b = MCPConfig()
        a.env["key"] = "val"
        assert b.env == {}


# ── MCPClient 测试（mcp/client.py）──────────────────────────────

class TestMCPClient:
    """MCPClient 测试 — add_server、get_tools

    注意：connect_local 会启动子进程，这里只测试不涉及子进程的方法。
    """

    def test_add_server(self):
        """add_server()：添加服务器配置到内部 _servers"""
        client = MCPClient()
        config = {"type": "local", "command": ["npx", "-y", "server"]}
        client.add_server("test-server", config)
        assert "test-server" in client._servers
        assert client._servers["test-server"] == config

    def test_add_server_overwrites_same_name(self):
        """add_server()：同名服务器覆盖旧配置"""
        client = MCPClient()
        client.add_server("srv", {"command": ["old"]})
        client.add_server("srv", {"command": ["new"]})
        assert client._servers["srv"] == {"command": ["new"]}

    def test_add_multiple_servers(self):
        """add_server()：添加多个服务器"""
        client = MCPClient()
        client.add_server("server1", {"command": ["npx"]})
        client.add_server("server2", {"command": ["python"]})
        assert len(client._servers) == 2
        assert "server1" in client._servers
        assert "server2" in client._servers

    def test_get_tools_initial_empty(self):
        """get_tools()：初始为空列表"""
        client = MCPClient()
        tools = client.get_tools()
        assert tools == []
        assert isinstance(tools, list)

    def test_get_tools_returns_list_type(self):
        """get_tools() 始终返回 list 类型"""
        client = MCPClient()
        result = client.get_tools()
        assert isinstance(result, list)


# ── MCPToolWrapper 测试（mcp/client.py）─────────────────────────

class TestMCPToolWrapper:
    """MCPToolWrapper 测试 — name/schema 属性、execute() 调用与异常处理"""

    def _make_wrapper(self, call_result=None, call_exception=None):
        """构造测试用 MCPToolWrapper，client.call_tool 用 AsyncMock 替代

        Args:
            call_result: call_tool 的返回值（默认 {}）
            call_exception: call_tool 抛出的异常（None 表示不抛异常）

        Returns:
            (wrapper, client) 元组
        """
        client = MCPClient()
        if call_exception is not None:
            client.call_tool = AsyncMock(side_effect=call_exception)
        else:
            client.call_tool = AsyncMock(return_value=call_result if call_result is not None else {})
        wrapper = MCPToolWrapper(
            name="test-server__search",
            description="搜索文件",
            input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
            client=client,
        )
        return wrapper, client

    def test_name_property(self):
        """name 属性返回构造时传入的名称"""
        wrapper, _ = self._make_wrapper()
        assert wrapper.name == "test-server__search"

    def test_schema_structure(self):
        """schema 属性返回 OpenAI function calling 格式的正确结构"""
        wrapper, _ = self._make_wrapper()
        schema = wrapper.schema
        assert schema["type"] == "function"
        assert "function" in schema
        fn = schema["function"]
        assert fn["name"] == "test-server__search"
        assert fn["description"] == "搜索文件"
        assert fn["parameters"] == {"type": "object", "properties": {"query": {"type": "string"}}}

    def test_description_via_schema(self):
        """description 通过 schema["function"]["description"] 暴露"""
        wrapper, _ = self._make_wrapper()
        assert wrapper.schema["function"]["description"] == "搜索文件"

    def test_description_internal_attribute(self):
        """_description 内部属性正确存储"""
        wrapper, _ = self._make_wrapper()
        assert wrapper._description == "搜索文件"

    def test_input_schema_passed_through(self):
        """input_schema 通过 schema 完整透传"""
        custom_schema = {"type": "object", "properties": {"x": {"type": "number"}}}
        client = MCPClient()
        wrapper = MCPToolWrapper(
            name="t__x",
            description="d",
            input_schema=custom_schema,
            client=client,
        )
        assert wrapper.schema["function"]["parameters"] == custom_schema

    @pytest.mark.asyncio
    async def test_execute_calls_call_tool_and_returns_result(self):
        """execute() 调用 client.call_tool 并返回 success=True + result"""
        expected = {"content": [{"type": "text", "text": "result"}]}
        wrapper, client = self._make_wrapper(call_result=expected)
        result = await wrapper.execute({"query": "test"}, {})
        # 验证 call_tool 被正确调用
        client.call_tool.assert_awaited_once_with("test-server__search", {"query": "test"})
        # 验证返回结构
        assert result["success"] is True
        assert result["result"] == expected

    @pytest.mark.asyncio
    async def test_execute_returns_error_on_exception(self):
        """execute() 异常时返回 success=False + error"""
        wrapper, _ = self._make_wrapper(call_exception=RuntimeError("连接失败"))
        result = await wrapper.execute({"query": "test"}, {})
        assert result["success"] is False
        assert "连接失败" in result["error"]

    @pytest.mark.asyncio
    async def test_execute_returns_error_on_value_error(self):
        """execute() ValueError 时返回错误（如无效工具名）"""
        wrapper, _ = self._make_wrapper(call_exception=ValueError("无效工具名"))
        result = await wrapper.execute({}, {})
        assert result["success"] is False
        assert "无效工具名" in result["error"]

    @pytest.mark.asyncio
    async def test_execute_passes_args_dict_to_call_tool(self):
        """execute() 将 args dict 原样传给 call_tool"""
        wrapper, client = self._make_wrapper()
        args = {"path": "/tmp", "mode": "r"}
        await wrapper.execute(args, {})
        client.call_tool.assert_awaited_once_with("test-server__search", args)

    @pytest.mark.asyncio
    async def test_execute_with_empty_args(self):
        """execute() 空参数也能正常调用"""
        wrapper, client = self._make_wrapper(call_result={"ok": True})
        result = await wrapper.execute({}, {})
        assert result["success"] is True
        client.call_tool.assert_awaited_once_with("test-server__search", {})


# ── IronConfig._merge_yaml 的 MCP 加载测试（P1-4 配置格式统一）───

class TestIronConfigMergeYamlMCP:
    """IronConfig._merge_yaml 的 MCP 加载测试

    验证从 YAML 加载 mcp 配置（command: str + args: list + env: dict 格式），
    以及对旧格式的兼容性。
    """

    def test_load_mcp_from_yaml(self, tmp_path):
        """从 YAML 加载 mcp 配置（command: str + args: list + env: dict 格式）"""
        yaml_content = """
mcp:
  filesystem:
    type: local
    command: npx
    args:
      - "-y"
      - "@modelcontextprotocol/server-filesystem"
      - "."
    env:
      API_KEY: secret123
    enabled: true
    timeout: 8000
"""
        config_path = tmp_path / "iron.yml"
        config_path.write_text(yaml_content, encoding="utf-8")

        config = IronConfig()
        config._merge_yaml(config_path)

        # 应加载到 mcp 字典
        assert "filesystem" in config.mcp
        mcp_cfg = config.mcp["filesystem"]
        # 应为 MCPConfig 实例
        assert isinstance(mcp_cfg, MCPConfig)
        # 字段正确
        assert mcp_cfg.command == "npx"
        assert mcp_cfg.args == ["-y", "@modelcontextprotocol/server-filesystem", "."]
        assert mcp_cfg.env == {"API_KEY": "secret123"}
        assert mcp_cfg.type == "local"
        assert mcp_cfg.enabled is True
        assert mcp_cfg.timeout == 8000

    def test_load_mcp_build_command_correct(self, tmp_path):
        """加载后 build_command() 返回正确的命令列表"""
        yaml_content = """
mcp:
  fs:
    command: npx
    args:
      - "-y"
      - "@modelcontextprotocol/server-filesystem"
      - "/tmp"
"""
        config_path = tmp_path / "iron.yml"
        config_path.write_text(yaml_content, encoding="utf-8")

        config = IronConfig()
        config._merge_yaml(config_path)

        mcp_cfg = config.mcp["fs"]
        cmd = mcp_cfg.build_command()
        assert cmd == ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/tmp"]

    def test_load_mcp_minimal_config(self, tmp_path):
        """加载最小配置（只有 command），其他字段使用默认值"""
        yaml_content = """
mcp:
  simple:
    command: python
"""
        config_path = tmp_path / "iron.yml"
        config_path.write_text(yaml_content, encoding="utf-8")

        config = IronConfig()
        config._merge_yaml(config_path)

        mcp_cfg = config.mcp["simple"]
        assert mcp_cfg.command == "python"
        assert mcp_cfg.args == []
        assert mcp_cfg.env == {}
        # 未指定的字段使用 MCPConfig 默认值
        assert mcp_cfg.type == "local"
        assert mcp_cfg.enabled is True
        assert mcp_cfg.timeout == 5000

    def test_load_mcp_empty_when_no_mcp_section(self, tmp_path):
        """YAML 中无 mcp 配置时 mcp 字典为空"""
        yaml_content = """
llm:
  model: gpt-4o
"""
        config_path = tmp_path / "iron.yml"
        config_path.write_text(yaml_content, encoding="utf-8")

        config = IronConfig()
        config._merge_yaml(config_path)
        assert config.mcp == {}

    def test_load_mcp_multiple_servers(self, tmp_path):
        """加载多个 MCP 服务器配置"""
        yaml_content = """
mcp:
  fs:
    command: npx
    args: ["-y", "server-fs"]
  git:
    command: npx
    args: ["-y", "server-git"]
    env:
      GIT_AUTHOR: test
"""
        config_path = tmp_path / "iron.yml"
        config_path.write_text(yaml_content, encoding="utf-8")

        config = IronConfig()
        config._merge_yaml(config_path)

        assert len(config.mcp) == 2
        assert "fs" in config.mcp
        assert "git" in config.mcp
        assert config.mcp["git"].env == {"GIT_AUTHOR": "test"}
        assert config.mcp["fs"].build_command() == ["npx", "-y", "server-fs"]

    def test_load_mcp_ignores_unknown_fields(self, tmp_path):
        """加载时忽略 MCPConfig 中不存在的字段（不崩溃）"""
        yaml_content = """
mcp:
  weird:
    command: npx
    unknown_field: should_be_ignored
    args: ["-y"]
"""
        config_path = tmp_path / "iron.yml"
        config_path.write_text(yaml_content, encoding="utf-8")

        config = IronConfig()
        config._merge_yaml(config_path)

        # 不崩溃，且已知字段正确加载
        mcp_cfg = config.mcp["weird"]
        assert mcp_cfg.command == "npx"
        assert mcp_cfg.args == ["-y"]

    def test_load_mcp_compatible_with_old_list_command_format(self, tmp_path):
        """兼容性：旧格式（command 直接是 list）不崩溃

        旧格式中 command 可能是 list 类型，新格式期望 str。
        此测试验证加载逻辑不会因类型不符而崩溃。
        """
        yaml_content = """
mcp:
  old-style:
    command: ["npx", "-y", "old-server"]
    args: []
"""
        config_path = tmp_path / "iron.yml"
        config_path.write_text(yaml_content, encoding="utf-8")

        config = IronConfig()
        # 不应抛出异常
        config._merge_yaml(config_path)
        # 加载成功
        assert "old-style" in config.mcp

    def test_load_mcp_does_not_affect_other_sections(self, tmp_path):
        """加载 mcp 配置不影响 llm / project 等其他配置段"""
        yaml_content = """
llm:
  model: gpt-4o
  backend: openai
mcp:
  srv:
    command: npx
"""
        config_path = tmp_path / "iron.yml"
        config_path.write_text(yaml_content, encoding="utf-8")

        config = IronConfig()
        config._merge_yaml(config_path)

        # mcp 加载正确
        assert "srv" in config.mcp
        # llm 不受影响
        assert config.llm.model == "gpt-4o"
        assert config.llm.backend == "openai"


# ── mcp_config 工具配置读写测试（P1-4 键名统一）─────────────────

class TestMcpConfigToolKeyNames:
    """mcp_config 工具的配置读写测试

    验证：
    - _add_server 写入 "mcp" 键（不是 "mcp_servers"）
    - _list_servers 读取 "mcp" 键
    - _remove_server 从 "mcp" 键删除
    - 兼容旧格式 "mcp_servers" 键
    """

    def test_add_server_writes_mcp_key_not_mcp_servers(self, tmp_path):
        """_add_server 写入 "mcp" 键（不是 "mcp_servers"）"""
        tool = McpConfigTool()
        asyncio.run(tool.execute({
            "action": "add",
            "name": "test-mcp",
            "command": "npx",
            "args": ["-y", "server"],
        }, {"project_dir": str(tmp_path)}))

        # 读取 iron.yml 验证键名
        config_path = tmp_path / "iron.yml"
        with open(config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        # 必须写入 "mcp" 键，不能是 "mcp_servers"
        assert "mcp" in data
        assert "mcp_servers" not in data
        assert "test-mcp" in data["mcp"]
        assert data["mcp"]["test-mcp"]["command"] == "npx"
        assert data["mcp"]["test-mcp"]["args"] == ["-y", "server"]

    def test_add_server_includes_type_and_enabled(self, tmp_path):
        """_add_server 写入的配置包含 type 和 enabled 字段"""
        tool = McpConfigTool()
        asyncio.run(tool.execute({
            "action": "add",
            "name": "srv",
            "command": "npx",
        }, {"project_dir": str(tmp_path)}))

        config_path = tmp_path / "iron.yml"
        with open(config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        cfg = data["mcp"]["srv"]
        assert cfg["type"] == "local"
        assert cfg["enabled"] is True

    def test_list_servers_reads_mcp_key(self, tmp_path):
        """_list_servers 读取 "mcp" 键"""
        # 先写入 mcp 键的配置
        config_path = tmp_path / "iron.yml"
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.safe_dump({
                "mcp": {
                    "fs": {
                        "command": "npx",
                        "args": ["-y", "fs-server"],
                        "env": {"KEY": "val"},
                    }
                }
            }, f)

        tool = McpConfigTool()
        result = asyncio.run(tool.execute({
            "action": "list",
        }, {"project_dir": str(tmp_path)}))

        assert result["success"]
        assert result["count"] == 1
        assert result["servers"][0]["name"] == "fs"
        assert result["servers"][0]["command"] == "npx"

    def test_remove_server_from_mcp_key(self, tmp_path):
        """_remove_server 从 "mcp" 键删除"""
        tool = McpConfigTool()
        # 先添加
        asyncio.run(tool.execute({
            "action": "add",
            "name": "to-remove",
            "command": "npx",
        }, {"project_dir": str(tmp_path)}))

        # 再删除
        result = asyncio.run(tool.execute({
            "action": "remove",
            "name": "to-remove",
        }, {"project_dir": str(tmp_path)}))
        assert result["success"]

        # 验证已从 mcp 键删除
        config_path = tmp_path / "iron.yml"
        with open(config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        assert "mcp" in data
        assert "to-remove" not in data.get("mcp", {})

    def test_list_servers_compatible_with_old_mcp_servers_key(self, tmp_path):
        """兼容旧格式 "mcp_servers" 键：_list_servers 能读取"""
        config_path = tmp_path / "iron.yml"
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.safe_dump({
                "mcp_servers": {
                    "legacy": {
                        "command": "python",
                        "args": ["server.py"],
                    }
                }
            }, f)

        tool = McpConfigTool()
        result = asyncio.run(tool.execute({
            "action": "list",
        }, {"project_dir": str(tmp_path)}))

        # 应能读取旧键名
        assert result["success"]
        assert result["count"] == 1
        assert result["servers"][0]["name"] == "legacy"
        assert result["servers"][0]["command"] == "python"

    def test_remove_server_compatible_with_old_mcp_servers_key(self, tmp_path):
        """兼容旧格式 "mcp_servers" 键：_remove_server 能删除"""
        config_path = tmp_path / "iron.yml"
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.safe_dump({
                "mcp_servers": {
                    "legacy": {
                        "command": "python",
                    }
                }
            }, f)

        tool = McpConfigTool()
        result = asyncio.run(tool.execute({
            "action": "remove",
            "name": "legacy",
        }, {"project_dir": str(tmp_path)}))

        assert result["success"]

        # 验证已删除
        with open(config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        assert "legacy" not in data.get("mcp_servers", {})

    def test_add_server_with_env(self, tmp_path):
        """_add_server 写入 env 配置"""
        tool = McpConfigTool()
        result = asyncio.run(tool.execute({
            "action": "add",
            "name": "with-env",
            "command": "npx",
            "args": ["-y", "server"],
            "env": {"API_KEY": "secret", "DEBUG": "true"},
        }, {"project_dir": str(tmp_path)}))

        assert result["success"]

        config_path = tmp_path / "iron.yml"
        with open(config_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        # env 值现在存为 ${KEY} 占位符（启动时从 os.environ 解析）
        assert data["mcp"]["with-env"]["env"] == {"API_KEY": "${API_KEY}", "DEBUG": "${DEBUG}"}

    def test_add_server_missing_name_returns_error(self, tmp_path):
        """_add_server 缺少 name 返回错误"""
        tool = McpConfigTool()
        result = asyncio.run(tool.execute({
            "action": "add",
            "name": "",
            "command": "npx",
        }, {"project_dir": str(tmp_path)}))

        assert not result["success"]
        assert "不能为空" in result["error"]

    def test_add_server_missing_command_returns_error(self, tmp_path):
        """_add_server 缺少 command 返回错误"""
        tool = McpConfigTool()
        result = asyncio.run(tool.execute({
            "action": "add",
            "name": "test",
            "command": "",
        }, {"project_dir": str(tmp_path)}))

        assert not result["success"]
        assert "不能为空" in result["error"]

    def test_list_servers_shows_full_command(self, tmp_path):
        """_list_servers 返回的 full_command 正确拼接 command + args"""
        config_path = tmp_path / "iron.yml"
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.safe_dump({
                "mcp": {
                    "fs": {
                        "command": "npx",
                        "args": ["-y", "server-fs", "/tmp"],
                    }
                }
            }, f)

        tool = McpConfigTool()
        result = asyncio.run(tool.execute({
            "action": "list",
        }, {"project_dir": str(tmp_path)}))

        assert result["success"]
        full_cmd = result["servers"][0]["full_command"]
        assert "npx" in full_cmd
        assert "server-fs" in full_cmd

    def test_list_servers_env_keys_only(self, tmp_path):
        """_list_servers 只返回 env 的 key，不泄露 value"""
        config_path = tmp_path / "iron.yml"
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.safe_dump({
                "mcp": {
                    "srv": {
                        "command": "npx",
                        "args": [],
                        "env": {"API_KEY": "super-secret-value"},
                    }
                }
            }, f)

        tool = McpConfigTool()
        result = asyncio.run(tool.execute({
            "action": "list",
        }, {"project_dir": str(tmp_path)}))

        assert result["success"]
        env_keys = result["servers"][0]["env_keys"]
        assert "API_KEY" in env_keys
        # 不应泄露 value
        result_str = str(result)
        assert "super-secret-value" not in result_str


# ── env 占位符展开测试（P0 回归修复：save→load round-trip）────────

def test_mcp_env_placeholder_expansion(monkeypatch):
    """${VAR} 占位符在加载时被展开为环境变量值"""
    from iron.config.settings import _expand_env

    # 设置环境变量
    monkeypatch.setenv("TEST_MCP_KEY", "real-secret-value")

    # 测试 _expand_env 函数
    assert _expand_env("${TEST_MCP_KEY}") == "real-secret-value"
    assert _expand_env("${MISSING_VAR}") == ""  # 不存在的变量返回空字符串
    assert _expand_env("prefix-${TEST_MCP_KEY}-suffix") == "prefix-real-secret-value-suffix"
    assert _expand_env({"KEY": "${TEST_MCP_KEY}", "OTHER": "literal"}) == {
        "KEY": "real-secret-value",
        "OTHER": "literal",
    }
    assert _expand_env(["${TEST_MCP_KEY}", "literal"]) == ["real-secret-value", "literal"]
    assert _expand_env(123) == 123  # 非字符串原样返回


def test_mcp_config_env_expansion_in_post_init(monkeypatch):
    """MCPConfig.__post_init__ 展开 env 占位符"""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-real-key")

    config = MCPConfig(
        command="npx",
        args=["-y", "server"],
        env={"OPENAI_API_KEY": "${OPENAI_API_KEY}", "DEBUG": "true"},
    )

    assert config.env["OPENAI_API_KEY"] == "sk-real-key"
    assert config.env["DEBUG"] == "true"  # 字面值不变


def test_mcp_config_url_command_expansion(monkeypatch):
    """MCPConfig.__post_init__ 展开 url / command 中的 ${VAR} 占位符"""
    monkeypatch.setenv("MCP_HOST", "example.com")
    monkeypatch.setenv("MCP_BIN", "/usr/local/bin/mcp-server")

    config = MCPConfig(
        command="${MCP_BIN}",
        url="https://${MCP_HOST}/mcp",
    )

    assert config.command == "/usr/local/bin/mcp-server"
    assert config.url == "https://example.com/mcp"


def test_mcp_config_env_no_placeholder_unchanged():
    """env 无占位符时原样保留（不破坏现有字面值）"""
    config = MCPConfig(
        command="npx",
        args=["-y", "server"],
        env={"API_KEY": "literal-secret", "DEBUG": "true"},
    )

    assert config.env == {"API_KEY": "literal-secret", "DEBUG": "true"}


def test_mcp_config_save_load_roundtrip_env(monkeypatch, tmp_path):
    """配置 save→load round-trip 后 env 占位符被正确展开（P0 回归核心场景）"""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-roundtrip-key")

    # 1. 构造配置并 save（env 落盘为 ${KEY} 占位符）
    config = IronConfig()
    config.mcp["openai-server"] = MCPConfig(
        command="npx",
        args=["-y", "server"],
        env={"OPENAI_API_KEY": "sk-roundtrip-key"},  # save 前是明文
    )
    config_file = tmp_path / "config.yml"
    config.save(config_file)

    # 2. 验证落盘内容是占位符
    raw = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    assert raw["mcp"]["openai-server"]["env"] == {"OPENAI_API_KEY": "${OPENAI_API_KEY}"}

    # 3. load 回来，env 应被展开为真实值
    loaded = IronConfig()
    loaded._merge_yaml(config_file)
    assert loaded.mcp["openai-server"].env["OPENAI_API_KEY"] == "sk-roundtrip-key"


def test_mcp_config_save_masks_headers_token(monkeypatch, tmp_path):
    """第六轮 P0 回归：save() 时 headers 中的 Authorization token 被占位符化，不明文落盘

    漏洞场景：第五轮 save() 用 getattr 直接写入 headers 展开后的真实值，
    导致 Authorization: Bearer sk-xxx 明文落盘到 iron.yml。
    修复后 save() 用 _mask_env_values 反向映射，将 token 值替换为 ${VAR_NAME}。
    """
    monkeypatch.setenv("MCP_AUTH_TOKEN", "Bearer sk-secret-token-xyz")

    config = IronConfig()
    config.mcp["remote-server"] = MCPConfig(
        type="http",
        url="https://mcp.example.com/sse",
        headers={"Authorization": "Bearer sk-secret-token-xyz"},  # 与环境变量值相同
    )
    config_file = tmp_path / "config.yml"
    config.save(config_file)

    raw = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    # headers 值应为占位符，不明文落盘
    assert raw["mcp"]["remote-server"]["headers"]["Authorization"] == "${MCP_AUTH_TOKEN}"
    # 明文 token 不应出现在文件中
    file_content = config_file.read_text(encoding="utf-8")
    assert "sk-secret-token-xyz" not in file_content


def test_mcp_config_save_masks_url_token(monkeypatch, tmp_path):
    """第六轮 P0 回归：save() 时 url 中的 token 部分被占位符化

    部分远程 MCP 服务通过 URL query 传递 token，save() 时 url 若等于环境变量值
    也应被占位符化。
    """
    monkeypatch.setenv("MCP_SERVICE_URL", "https://mcp.example.com/sse?token=abc123")

    config = IronConfig()
    config.mcp["sse-server"] = MCPConfig(
        type="sse",
        url="https://mcp.example.com/sse?token=abc123",  # 与环境变量值相同
    )
    config_file = tmp_path / "config.yml"
    config.save(config_file)

    raw = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    assert raw["mcp"]["sse-server"]["url"] == "${MCP_SERVICE_URL}"
    file_content = config_file.read_text(encoding="utf-8")
    assert "token=abc123" not in file_content


def test_mcp_config_save_preserves_literal_headers(tmp_path):
    """第六轮 P0 回归：headers 中非环境变量值的明文保留原值

    _mask_env_values 仅在 value 等于某个环境变量值时替换为占位符，
    用户手动写的明文 headers（不匹配任何环境变量）应原样落盘。
    """
    config = IronConfig()
    config.mcp["literal-server"] = MCPConfig(
        type="http",
        url="https://mcp.example.com/sse",
        headers={"X-Custom-Header": "literal-value-not-in-env"},
    )
    config_file = tmp_path / "config.yml"
    config.save(config_file)

    raw = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    # 不匹配环境变量的明文应原样保留
    assert raw["mcp"]["literal-server"]["headers"]["X-Custom-Header"] == "literal-value-not-in-env"


def test_mcp_config_args_expand_env(monkeypatch):
    """第六轮 P1 回归：MCPConfig.__post_init__ 中 args 字段调用 _expand_env

    漏洞场景：第五轮 __post_init__ 展开 env/headers/url/command 但遗漏了 args，
    导致 args 中的 ${VAR} 占位符不会被展开，子进程收到字面量 ${VAR} 而非真实值。
    """
    monkeypatch.setenv("MCP_SERVER_NAME", "filesystem-server")

    cfg = MCPConfig(
        command="npx",
        args=["-y", "@modelcontextprotocol/server-${MCP_SERVER_NAME}", "/tmp"],
    )
    # __post_init__ 应展开 args 中的占位符
    assert cfg.args == ["-y", "@modelcontextprotocol/server-filesystem-server", "/tmp"]


def test_mcp_config_args_expand_multiple_env(monkeypatch):
    """第六轮 P1 回归：args 中多个 ${VAR} 占位符同时展开"""
    monkeypatch.setenv("MCP_HOST", "localhost")
    monkeypatch.setenv("MCP_PORT", "8080")

    cfg = MCPConfig(
        command="node",
        args=["server.js", "--host", "${MCP_HOST}", "--port", "${MCP_PORT}"],
    )
    assert cfg.args == ["server.js", "--host", "localhost", "--port", "8080"]


def test_mcp_config_args_expand_unset_env(monkeypatch):
    """第六轮 P1 回归：args 中未设置的环境变量展开为空字符串（并触发 warning）"""
    monkeypatch.delenv("MCP_UNSET_VAR", raising=False)

    cfg = MCPConfig(
        command="npx",
        args=["-y", "server-${MCP_UNSET_VAR}", "arg2"],
    )
    # 未设置的环境变量展开为空字符串
    assert cfg.args == ["-y", "server-", "arg2"]

