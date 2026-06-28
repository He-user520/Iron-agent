"""MCP 客户端单元测试

覆盖 stdio / SSE / HTTP 三种传输的工具调用、连接失败、重连等
运行方式: pytest tests/test_mcp_client.py -v
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from iron.mcp.client import MCPClient, MCPToolWrapper


class TestMCPClientBasics:
    """基础功能测试"""

    def test_add_server_local(self):
        """add_server 存储 local 配置"""
        client = MCPClient()
        client.add_server("test", {
            "type": "local",
            "command": ["npx", "server"],
            "env": {},
            "timeout": 5000,
        })
        assert "test" in client._servers
        assert client._servers["test"]["command"] == ["npx", "server"]

    def test_add_server_sse(self):
        """add_server 存储 sse 配置"""
        client = MCPClient()
        client.add_server("srv", {"type": "sse", "url": "http://localhost/sse"})
        assert client._servers["srv"]["url"] == "http://localhost/sse"

    def test_get_tools_empty(self):
        """无连接时 get_tools 返回空列表"""
        client = MCPClient()
        assert client.get_tools() == []

    def test_call_tool_invalid_name_raises(self):
        """call_tool 名字无 __ 分隔 → ValueError"""
        client = MCPClient()
        with pytest.raises(ValueError, match="无效的 MCP 工具名"):
            asyncio.run(client.call_tool("invalid_name", {}))

    def test_call_tool_unknown_server(self):
        """call_tool 未知服务器 → RuntimeError（实际消息为"未配置"）"""
        client = MCPClient()
        with pytest.raises(RuntimeError, match="未配置"):
            asyncio.run(client.call_tool("unknown__tool", {}))


class TestMCPClientLocal:
    """stdio 传输测试"""

    @pytest.mark.asyncio
    async def test_connect_local_success(self):
        """成功连接 local 服务器（mock 子进程）— 验证 initialize/tools/list 握手与工具注册"""
        client = MCPClient()
        client.add_server("test", {
            "type": "local",
            "command": ["echo", "hello"],
            "timeout": 1000,
        })

        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdin.write = MagicMock()
        mock_proc.stdin.drain = AsyncMock()
        # readline 依次返回 initialize 响应与 tools/list 响应
        mock_proc.stdout = MagicMock()
        mock_proc.stdout.readline = AsyncMock(side_effect=[
            b'{"jsonrpc":"2.0","id":1,"result":{"capabilities":{}}}\n',
            b'{"jsonrpc":"2.0","id":2,"result":{"tools":[{"name":"echo","description":"echo tool","inputSchema":{"type":"object","properties":{}}}]}}\n',
        ])
        # stderr readline 返回 EOF，让后台 drain 任务立即退出
        mock_proc.stderr = MagicMock()
        mock_proc.stderr.readline = AsyncMock(return_value=b"")
        mock_proc.terminate = MagicMock()
        mock_proc.wait = AsyncMock(return_value=0)

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            await client.connect_local("test", ["echo", "hello"], timeout=1000)

        # 连接成功：_available=True，传输=stdio，工具已注册
        assert client._servers["test"]["_available"] is True
        assert client._servers["test"]["_transport"] == "stdio"
        tools = client.get_tools()
        assert len(tools) == 1
        assert tools[0].name == "test__echo"

        await client.disconnect_all()

    @pytest.mark.asyncio
    async def test_connect_local_command_not_found(self):
        """命令不存在 → connect_local 不抛异常，标记 _available=False"""
        client = MCPClient()
        client.add_server("test", {"type": "local", "command": ["nonexistent-bin-xyz"]})

        with patch("asyncio.create_subprocess_exec", side_effect=FileNotFoundError("no such command")):
            # 不应抛出异常（内部 try/except 吞掉）
            await client.connect_local("test", ["nonexistent-bin-xyz"], timeout=1000)

        assert client._servers["test"]["_available"] is False
        assert client.get_tools() == []

    @pytest.mark.asyncio
    async def test_disconnect_all_idempotent(self):
        """disconnect_all 可重复调用（空客户端不报错）"""
        client = MCPClient()
        await client.disconnect_all()
        await client.disconnect_all()

    @pytest.mark.asyncio
    async def test_call_stdio_server_unavailable(self):
        """_call_stdio 服务器未连接 → RuntimeError（_process 为 None）"""
        client = MCPClient()
        client.add_server("test", {"type": "local", "command": ["x"]})
        # 未连接，_process 为 None
        with pytest.raises(RuntimeError, match="不可用|未连接"):
            await client._call_stdio("test", "tool", {})


class TestMCPClientSSE:
    """SSE 传输测试"""

    @pytest.mark.asyncio
    async def test_connect_sse_success_mock_httpx(self):
        """mock httpx 成功连接 SSE — 验证 endpoint 解析与工具列表拉取"""
        client = MCPClient()
        client.add_server("srv", {"type": "sse", "url": "http://localhost/sse", "timeout": 1000})

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"capabilities": {}, "serverInfo": {"name": "test"}}
        mock_response.text = "{}"
        mock_response.headers = {"content-type": "text/event-stream"}

        # SSE 流：推送 endpoint 事件后关闭
        async def _sse_lines():
            yield 'data: {"endpoint": "/messages"}'

        mock_response.aiter_lines = _sse_lines

        # mock stream 异步上下文管理器
        mock_stream = MagicMock()
        mock_stream.__aenter__ = AsyncMock(return_value=mock_response)
        mock_stream.__aexit__ = AsyncMock(return_value=None)

        mock_client = MagicMock()
        mock_client.stream = MagicMock(return_value=mock_stream)
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.aclose = AsyncMock()

        with patch("httpx.AsyncClient", return_value=mock_client):
            await client.connect_sse("srv", "http://localhost/sse", timeout=1000)

        # 连接成功：_available=True，传输=sse，endpoint 已解析
        assert client._servers["srv"]["_available"] is True
        assert client._servers["srv"]["_transport"] == "sse"
        assert client._servers["srv"]["_endpoint_url"] == "http://localhost/messages"

        await client.disconnect_all()

    @pytest.mark.asyncio
    async def test_call_sse_server_unavailable(self):
        """_call_sse 服务器未连接 → RuntimeError"""
        client = MCPClient()
        client.add_server("srv", {"type": "sse", "url": "http://localhost/sse"})
        with pytest.raises(RuntimeError):
            await client._call_sse("srv", "tool", {})


class TestMCPClientHTTP:
    """HTTP 传输测试"""

    @pytest.mark.asyncio
    async def test_connect_http_success_mock_httpx(self):
        """mock httpx 成功连接 HTTP — 验证 initialize/tools/list 流程"""
        client = MCPClient()
        client.add_server("srv", {"type": "http", "url": "http://localhost/mcp", "timeout": 1000})

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"capabilities": {}, "serverInfo": {"name": "test"}}
        mock_response.text = "{}"
        mock_response.headers = {"content-type": "application/json"}

        mock_client = MagicMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.aclose = AsyncMock()

        with patch("httpx.AsyncClient", return_value=mock_client):
            await client.connect_http("srv", "http://localhost/mcp", timeout=1000)

        # 连接成功：_available=True，传输=http，端点已保存
        assert client._servers["srv"]["_available"] is True
        assert client._servers["srv"]["_transport"] == "http"
        assert client._servers["srv"]["_endpoint_url"] == "http://localhost/mcp"

        await client.disconnect_all()

    @pytest.mark.asyncio
    async def test_call_http_server_unavailable(self):
        """_call_http 服务器未连接 → RuntimeError"""
        client = MCPClient()
        client.add_server("srv", {"type": "http", "url": "http://localhost/mcp"})
        with pytest.raises(RuntimeError):
            await client._call_http("srv", "tool", {})


class TestMCPToolWrapper:
    """MCPToolWrapper 测试"""

    def test_wrapper_name_and_schema(self):
        """wrapper 正确返回 name 和 OpenAI function calling 格式 schema"""
        mock_client = MagicMock()
        wrapper = MCPToolWrapper(
            name="srv__tool",
            description="测试工具",
            input_schema={"type": "object", "properties": {}},
            client=mock_client,
        )
        assert wrapper.name == "srv__tool"
        schema = wrapper.schema
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "srv__tool"
        assert schema["function"]["description"] == "测试工具"
        assert schema["function"]["parameters"] == {"type": "object", "properties": {}}

    @pytest.mark.asyncio
    async def test_wrapper_execute_success(self):
        """execute 调用 client.call_tool，成功返回 success=True + result"""
        mock_client = MagicMock()
        mock_client.call_tool = AsyncMock(return_value={"result": "ok"})
        wrapper = MCPToolWrapper("srv__tool", "desc", {"type": "object"}, mock_client)

        result = await wrapper.execute({"arg": "value"}, {})
        assert result["success"] is True
        assert result["result"] == {"result": "ok"}
        mock_client.call_tool.assert_awaited_once_with("srv__tool", {"arg": "value"})

    @pytest.mark.asyncio
    async def test_wrapper_execute_failure(self):
        """execute 异常时返回 success=False + error（_sanitize_error 不修改中文）"""
        mock_client = MagicMock()
        mock_client.call_tool = AsyncMock(side_effect=RuntimeError("连接失败"))
        wrapper = MCPToolWrapper("srv__tool", "desc", {"type": "object"}, mock_client)

        result = await wrapper.execute({"arg": "value"}, {})
        assert result["success"] is False
        assert "连接失败" in result["error"]


class TestMCPClientReconnect:
    """重连测试"""

    @pytest.mark.asyncio
    async def test_reconnect_unknown_server_returns_false(self):
        """reconnect 未知服务器 → 返回 False"""
        client = MCPClient()
        result = await client.reconnect("nonexistent")
        assert result is False

    @pytest.mark.asyncio
    async def test_reconnect_never_connected_returns_false(self):
        """reconnect 已配置但从未连接的服务器 → _transport 为 None → 返回 False"""
        client = MCPClient()
        client.add_server("srv", {"type": "http", "url": "http://localhost/mcp"})
        await client.disconnect_all()
        result = await client.reconnect("srv")
        # 未连接过，transport 为 None，走 else 分支返回 False
        assert result is False

    @pytest.mark.asyncio
    async def test_disconnect_all_idempotent_after_add(self):
        """已 add_server 但未连接时 disconnect_all 不报错"""
        client = MCPClient()
        client.add_server("srv", {"type": "http", "url": "http://localhost/mcp"})
        await client.disconnect_all()  # 不应抛异常


class TestMCPClientHealthCheck:
    """健康检查测试（Phase 2 任务 2.3）"""

    @pytest.mark.asyncio
    async def test_health_check_empty_servers(self):
        """无服务器时 health_check 返回空 dict"""
        client = MCPClient()
        results = await client.health_check()
        assert results == {}

    @pytest.mark.asyncio
    async def test_ping_server_unavailable_returns_false(self):
        """_ping_server 对 _available=False 的服务器返回 False"""
        client = MCPClient()
        client.add_server("srv", {"type": "http", "url": "http://localhost/mcp"})
        client._servers["srv"]["_available"] = False
        client._servers["srv"]["_transport"] = "http"
        result = await client._ping_server("srv")
        assert result is False

    @pytest.mark.asyncio
    async def test_ping_server_unknown_returns_false(self):
        """_ping_server 未知服务器返回 False"""
        client = MCPClient()
        result = await client._ping_server("nonexistent")
        assert result is False

    @pytest.mark.asyncio
    async def test_ping_server_no_transport_returns_false(self):
        """_ping_server _transport 未设置时返回 False"""
        client = MCPClient()
        client.add_server("srv", {"type": "http"})
        client._servers["srv"]["_available"] = True
        # _transport 未设置
        result = await client._ping_server("srv")
        assert result is False

    @pytest.mark.asyncio
    async def test_health_check_no_reconnect_on_disabled(self):
        """auto_reconnect=False 时不可达服务器不被重连"""
        client = MCPClient()
        client.add_server("srv", {"type": "http", "url": "http://localhost/mcp"})
        client._servers["srv"]["_available"] = True
        client._servers["srv"]["_transport"] = "http"
        client._servers["srv"]["_http_client"] = None  # 无 client，ping 必失败

        # mock _ping_server 返回 False
        client._ping_server = AsyncMock(return_value=False)
        # mock reconnect 验证不被调用
        client.reconnect = AsyncMock(return_value=True)

        results = await client.health_check(auto_reconnect=False)
        assert "srv" in results
        assert results["srv"]["healthy"] is False
        assert results["srv"]["reconnected"] is False
        client.reconnect.assert_not_called()

    @pytest.mark.asyncio
    async def test_health_check_auto_reconnect_success(self):
        """auto_reconnect=True 且重连成功 → healthy=True, reconnected=True"""
        client = MCPClient()
        client.add_server("srv", {"type": "http", "url": "http://localhost/mcp"})
        client._servers["srv"]["_transport"] = "http"

        # ping 失败 → reconnect 成功
        client._ping_server = AsyncMock(return_value=False)
        client.reconnect = AsyncMock(return_value=True)

        results = await client.health_check(auto_reconnect=True)
        assert results["srv"]["healthy"] is True
        assert results["srv"]["reconnected"] is True
        client.reconnect.assert_called_once_with("srv")

    @pytest.mark.asyncio
    async def test_health_check_auto_reconnect_failure(self):
        """auto_reconnect=True 但重连失败 → healthy=False, reconnected=False"""
        client = MCPClient()
        client.add_server("srv", {"type": "http", "url": "http://localhost/mcp"})
        client._servers["srv"]["_transport"] = "http"

        client._ping_server = AsyncMock(return_value=False)
        client.reconnect = AsyncMock(return_value=False)

        results = await client.health_check(auto_reconnect=True)
        assert results["srv"]["healthy"] is False
        assert results["srv"]["reconnected"] is False

    @pytest.mark.asyncio
    async def test_health_check_healthy_server_no_reconnect(self):
        """健康服务器不触发重连"""
        client = MCPClient()
        client.add_server("srv", {"type": "http", "url": "http://localhost/mcp"})
        client._servers["srv"]["_transport"] = "http"

        client._ping_server = AsyncMock(return_value=True)
        client.reconnect = AsyncMock(return_value=True)

        results = await client.health_check()
        assert results["srv"]["healthy"] is True
        assert results["srv"]["reconnected"] is False
        client.reconnect.assert_not_called()

    @pytest.mark.asyncio
    async def test_health_check_multiple_servers(self):
        """多服务器：一个健康一个断开"""
        client = MCPClient()
        client.add_server("ok", {"type": "http", "url": "http://ok/mcp"})
        client.add_server("down", {"type": "http", "url": "http://down/mcp"})
        client._servers["ok"]["_transport"] = "http"
        client._servers["down"]["_transport"] = "http"

        async def fake_ping(name, timeout=5.0):
            return name == "ok"

        client._ping_server = fake_ping
        client.reconnect = AsyncMock(return_value=False)

        results = await client.health_check()
        assert results["ok"]["healthy"] is True
        assert results["down"]["healthy"] is False
        # 只对 down 触发重连
        client.reconnect.assert_called_once_with("down")

    def test_get_server_status_empty(self):
        """get_server_status 空客户端"""
        client = MCPClient()
        status = client.get_server_status()
        assert status == {}

    def test_get_server_status_with_unavailable(self):
        """get_server_status 返回 unavailable 状态"""
        client = MCPClient()
        client.add_server("srv", {"type": "http", "url": "http://localhost/mcp"})
        # 未连接，_available 默认 False
        status = client.get_server_status()
        assert "srv" in status
        assert status["srv"]["available"] is False
        assert status["srv"]["transport"] == "unknown"
        assert status["srv"]["tools_count"] == 0
        assert status["srv"]["disconnected"] is False

    def test_get_server_status_with_tools(self):
        """get_server_status 统计工具数"""
        client = MCPClient()
        client.add_server("srv", {"type": "http"})
        client._servers["srv"]["_available"] = True
        client._servers["srv"]["_transport"] = "http"
        # 手动添加 2 个工具
        client._tools["srv__tool1"] = MagicMock()
        client._tools["srv__tool2"] = MagicMock()
        client._tools["other__tool3"] = MagicMock()  # 其他服务器
        status = client.get_server_status()
        assert status["srv"]["tools_count"] == 2
        assert status["srv"]["available"] is True

    def test_get_server_status_disconnected_flag(self):
        """get_server_status 反映 _disconnected 标记"""
        client = MCPClient()
        client.add_server("srv", {"type": "http"})
        client._servers["srv"]["_disconnected"] = True
        status = client.get_server_status()
        assert status["srv"]["disconnected"] is True


class TestMCPClientReconnectWithRetries:
    """reconnect 重试次数测试（Phase 2 任务 2.3 扩展）"""

    @pytest.mark.asyncio
    async def test_reconnect_success_first_try(self):
        """reconnect 第一次就成功 → 不重试"""
        client = MCPClient()
        client.add_server("srv", {"type": "http", "url": "http://localhost/mcp"})
        client._servers["srv"]["_transport"] = "http"

        # connect_http 成功后 _available=True
        async def fake_connect_http(name, url, timeout=5000, headers=None):
            client._servers[name]["_available"] = True
            client._servers[name]["_transport"] = "http"

        client.connect_http = fake_connect_http
        result = await client.reconnect("srv", max_retries=3)
        assert result is True

    @pytest.mark.asyncio
    async def test_reconnect_marks_disconnected_after_max_retries(self):
        """重试 max_retries 次都失败 → 标记 _disconnected=True"""
        client = MCPClient()
        client.add_server("srv", {"type": "http", "url": "http://localhost/mcp"})
        client._servers["srv"]["_transport"] = "http"

        # connect_http 抛异常
        async def failing_connect(name, url, timeout=5000, headers=None):
            raise RuntimeError("connection refused")

        client.connect_http = failing_connect
        # 缩短退避时间避免测试慢（monkeypatch asyncio.sleep）
        with patch("asyncio.sleep", new=AsyncMock(return_value=None)):
            result = await client.reconnect("srv", max_retries=2)
        assert result is False
        assert client._servers["srv"]["_available"] is False
        assert client._servers["srv"]["_disconnected"] is True

    @pytest.mark.asyncio
    async def test_reconnect_unknown_transport_returns_false(self):
        """未知 transport 直接返回 False，不重试"""
        client = MCPClient()
        client.add_server("srv", {"type": "unknown"})
        client._servers["srv"]["_transport"] = "unknown-transport"

        result = await client.reconnect("srv", max_retries=3)
        assert result is False
