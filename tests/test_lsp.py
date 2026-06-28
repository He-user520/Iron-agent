"""LSP 客户端与工具单元测试 — 覆盖 iron/integrations/lsp_client.py 和 iron/tools/lsp_tools.py

所有测试用 mock，不依赖真实 clangd/ccls 安装。

运行方式: pytest tests/test_lsp.py -v
"""
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from iron.integrations.lsp_client import (
    LSPClient, LSPCompletion, LSPConfig, LSPDiagnostic, LSPHover, LSPPosition,
)
from iron.tools.lsp_tools import (
    LSPCompletionTool, LSPDefinitionTool, LSPDiagnosticsTool,
    LSPHoverTool, LSPReferencesTool,
)


# ── 服务器检测与 compile_commands 查找 ─────────────────────────

class TestLSPDetectAndFind:
    """服务器检测与 compile_commands.json 查找"""

    def test_detect_server(self):
        """检测 LSP 服务器（mock shutil.which 返回 clangd 路径）"""
        def fake_which(cmd):
            return f"/usr/bin/{cmd}" if cmd == "clangd" else None
        with patch("iron.integrations.lsp_client.shutil.which", side_effect=fake_which):
            path = LSPClient.detect_server()
        assert path == "/usr/bin/clangd"

    def test_detect_server_fallback_ccls(self):
        """clangd 不可用时回退到 ccls"""
        def fake_which(cmd):
            return f"/usr/bin/{cmd}" if cmd == "ccls" else None
        with patch("iron.integrations.lsp_client.shutil.which", side_effect=fake_which):
            path = LSPClient.detect_server()
        assert path == "/usr/bin/ccls"

    def test_detect_server_not_found(self):
        """都不可用时返回 None"""
        with patch("iron.integrations.lsp_client.shutil.which", return_value=None):
            path = LSPClient.detect_server()
        assert path is None

    def test_find_compile_commands_cmake(self, tmp_path):
        """查找 CMake build/compile_commands.json"""
        build_dir = tmp_path / "build"
        build_dir.mkdir()
        cc = build_dir / "compile_commands.json"
        cc.write_text("[]", encoding="utf-8")

        result = LSPClient.find_compile_commands(tmp_path)
        assert result == cc

    def test_find_compile_commands_pio(self, tmp_path):
        """查找 PlatformIO .pio/build/<env>/compile_commands.json"""
        pio_env = tmp_path / ".pio" / "build" / "stm32f407"
        pio_env.mkdir(parents=True)
        cc = pio_env / "compile_commands.json"
        cc.write_text("[]", encoding="utf-8")

        result = LSPClient.find_compile_commands(tmp_path)
        assert result == cc

    def test_find_compile_commands_cmake_build_glob(self, tmp_path):
        """查找 cmake-build-*/compile_commands.json"""
        cmake_build = tmp_path / "cmake-build-debug"
        cmake_build.mkdir()
        cc = cmake_build / "compile_commands.json"
        cc.write_text("[]", encoding="utf-8")

        result = LSPClient.find_compile_commands(tmp_path)
        assert result == cc

    def test_find_compile_commands_not_found(self, tmp_path):
        """未找到返回 None"""
        result = LSPClient.find_compile_commands(tmp_path)
        assert result is None


# ── 数据类测试 ──────────────────────────────────────────────────

class TestLSPDataclasses:
    """dataclass 默认值与字段"""

    def test_lsp_config_defaults(self):
        """默认配置"""
        cfg = LSPConfig()
        assert cfg.server_command == "clangd"
        assert cfg.server_args == []
        assert cfg.enabled is True
        assert cfg.compile_commands_dir == ""
        assert cfg.init_options == {}

    def test_lsp_config_independent_instances(self):
        """多个实例默认 list/dict 互不影响（避免可变默认值陷阱）"""
        a = LSPConfig()
        b = LSPConfig()
        a.server_args.append("x")
        a.init_options["k"] = "v"
        assert b.server_args == []
        assert b.init_options == {}

    def test_lsp_diagnostics_dataclass(self):
        """LSPDiagnostic 字段"""
        d = LSPDiagnostic(
            file="src/main.c", line=10, col=5,
            end_line=10, end_col=10,
            severity=1, source="clangd",
            message="未声明标识符", code="undeclared",
        )
        assert d.file == "src/main.c"
        assert d.line == 10
        assert d.col == 5
        assert d.end_line == 10
        assert d.end_col == 10
        assert d.severity == 1
        assert d.source == "clangd"
        assert d.message == "未声明标识符"
        assert d.code == "undeclared"

    def test_lsp_diagnostics_defaults(self):
        """LSPDiagnostic 默认值"""
        d = LSPDiagnostic(file="a.c", line=0, col=0, severity=1)
        assert d.end_line == 0
        assert d.end_col == 0
        assert d.source == ""
        assert d.message == ""
        assert d.code == ""

    def test_lsp_position_dataclass(self):
        """LSPPosition 字段"""
        p = LSPPosition(file="b.c", line=20, col=3)
        assert p.file == "b.c"
        assert p.line == 20
        assert p.col == 3

    def test_lsp_hover_dataclass(self):
        """LSPHover 默认值"""
        h = LSPHover(content="hello")
        assert h.content == "hello"
        assert h.range_start is None
        assert h.range_end is None

    def test_lsp_completion_dataclass(self):
        """LSPCompletion 默认值"""
        c = LSPCompletion(label="foo", kind=3)
        assert c.label == "foo"
        assert c.kind == 3
        assert c.detail == ""
        assert c.documentation == ""
        assert c.insert_text == ""


# ── LSPClient 测试 ──────────────────────────────────────────────

class TestLSPClient:
    """LSP 客户端测试（mock 子进程）"""

    def _make_client(self, tmp_path, **kwargs):
        """构造带 mock 配置的 LSP 客户端"""
        config = LSPConfig(server_command="clangd", enabled=True, **kwargs)
        return LSPClient(config=config, project_root=str(tmp_path))

    def _make_mock_proc(self):
        """构造 mock 子进程"""
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stdin.write = MagicMock()
        mock_proc.stdin.drain = AsyncMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stdout.readline = AsyncMock(return_value=b"")  # EOF
        mock_proc.stderr = MagicMock()
        mock_proc.stderr.readline = AsyncMock(return_value=b"")
        mock_proc.terminate = MagicMock()
        mock_proc.wait = AsyncMock(return_value=0)
        mock_proc.returncode = 0
        return mock_proc

    @pytest.mark.asyncio
    async def test_lsp_client_start_stop(self, tmp_path):
        """启动停止（mock subprocess）"""
        client = self._make_client(tmp_path)
        mock_proc = self._make_mock_proc()

        async def mock_send_request(method, params):
            if method == "initialize":
                return {"capabilities": {}}
            return {}

        with patch("iron.integrations.lsp_client.shutil.which", return_value="/usr/bin/clangd"), \
             patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            client._send_request = AsyncMock(side_effect=mock_send_request)
            client._send_notification = AsyncMock()
            ok = await client.start()

        assert ok is True
        assert client._initialized is True

        await client.stop()
        assert client._proc is None
        assert client._initialized is False

    @pytest.mark.asyncio
    async def test_lsp_client_start_disabled(self, tmp_path):
        """enabled=False 时 start 返回 False"""
        config = LSPConfig(enabled=False)
        client = LSPClient(config=config, project_root=str(tmp_path))
        ok = await client.start()
        assert ok is False

    @pytest.mark.asyncio
    async def test_lsp_client_start_no_server(self, tmp_path):
        """没有可用的 LSP 服务器"""
        config = LSPConfig(server_command="nonexistent-clangd-xyz")
        client = LSPClient(config=config, project_root=str(tmp_path))
        with patch("iron.integrations.lsp_client.shutil.which", return_value=None), \
             patch("iron.integrations.lsp_client.LSPClient.detect_server", return_value=None):
            ok = await client.start()
        assert ok is False

    @pytest.mark.asyncio
    async def test_lsp_client_initialize(self, tmp_path):
        """initialize 握手（mock）"""
        client = self._make_client(tmp_path)
        # 不调用 start()，直接设置 _stdin/_proc 以便 _send_request 不报错
        client._stdin = MagicMock()
        client._stdin.write = MagicMock()
        client._stdin.drain = AsyncMock()
        client._proc = MagicMock()
        client._proc.returncode = None

        client._send_request = AsyncMock(return_value={"capabilities": {}})
        client._send_notification = AsyncMock()

        result = await client.initialize()
        assert result is True
        # 验证 _send_request 被调用 initialize 方法
        client._send_request.assert_called_once()
        called_args = client._send_request.call_args
        assert called_args[0][0] == "initialize"
        # 验证 _send_notification 被调用 initialized
        client._send_notification.assert_called_once_with("initialized", {})

    @pytest.mark.asyncio
    async def test_lsp_client_initialize_failure(self, tmp_path):
        """initialize 失败时返回 False"""
        client = self._make_client(tmp_path)
        client._stdin = MagicMock()
        client._stdin.write = MagicMock()
        client._stdin.drain = AsyncMock()
        client._proc = MagicMock()
        client._proc.returncode = None

        client._send_request = AsyncMock(side_effect=RuntimeError("LSP 错误"))
        result = await client.initialize()
        assert result is False

    @pytest.mark.asyncio
    async def test_lsp_client_did_open(self, tmp_path):
        """did_open 通知（mock）"""
        client = self._make_client(tmp_path)
        client._initialized = True
        client._send_notification = AsyncMock()

        await client.did_open("src/main.c", "int main() { return 0; }")

        client._send_notification.assert_called_once()
        call_args = client._send_notification.call_args
        assert call_args[0][0] == "textDocument/didOpen"
        params = call_args[0][1]
        assert "textDocument" in params
        assert params["textDocument"]["languageId"] == "c"
        assert params["textDocument"]["text"] == "int main() { return 0; }"

    @pytest.mark.asyncio
    async def test_lsp_client_did_change(self, tmp_path):
        """did_change 通知（mock）"""
        client = self._make_client(tmp_path)
        client._initialized = True
        client._send_notification = AsyncMock()

        await client.did_change("src/main.c", "new content")

        client._send_notification.assert_called_once()
        call_args = client._send_notification.call_args
        assert call_args[0][0] == "textDocument/didChange"

    @pytest.mark.asyncio
    async def test_lsp_client_did_close(self, tmp_path):
        """did_close 通知（mock）"""
        client = self._make_client(tmp_path)
        client._initialized = True
        client._send_notification = AsyncMock()

        await client.did_close("src/main.c")

        client._send_notification.assert_called_once()
        call_args = client._send_notification.call_args
        assert call_args[0][0] == "textDocument/didClose"

    @pytest.mark.asyncio
    async def test_lsp_client_did_open_not_initialized(self, tmp_path):
        """未初始化时 did_open 静默返回"""
        client = self._make_client(tmp_path)
        client._initialized = False
        client._send_notification = AsyncMock()

        await client.did_open("src/main.c", "content")
        client._send_notification.assert_not_called()

    @pytest.mark.asyncio
    async def test_lsp_client_get_diagnostics(self, tmp_path):
        """获取诊断（mock 缓存）"""
        client = self._make_client(tmp_path)
        client._initialized = True
        # 构造已缓存的诊断（与 get_diagnostics 内部 normalize 一致）
        abs_path = str((tmp_path / "src" / "main.c").resolve())
        client._diagnostics[abs_path] = [
            LSPDiagnostic(file=abs_path, line=5, col=0, severity=1, message="err"),
        ]

        result = await client.get_diagnostics("src/main.c")
        assert len(result) == 1
        assert result[0].message == "err"
        assert result[0].severity == 1

    @pytest.mark.asyncio
    async def test_lsp_client_get_diagnostics_not_initialized(self, tmp_path):
        """未初始化返回空列表（优雅降级）"""
        client = self._make_client(tmp_path)
        client._initialized = False
        result = await client.get_diagnostics("src/main.c")
        assert result == []

    @pytest.mark.asyncio
    async def test_lsp_client_definition(self, tmp_path):
        """跳转定义（mock）"""
        client = self._make_client(tmp_path)
        client._initialized = True
        target = tmp_path / "lib.c"
        target.write_text("", encoding="utf-8")
        client._send_request = AsyncMock(return_value=[
            {"uri": target.as_uri(), "range": {"start": {"line": 3, "character": 5}}}
        ])

        positions = await client.definition("src/main.c", 1, 2)
        assert len(positions) == 1
        assert positions[0].line == 3
        assert positions[0].col == 5

    @pytest.mark.asyncio
    async def test_lsp_client_definition_not_initialized(self, tmp_path):
        """未初始化时跳转定义返回空列表"""
        client = self._make_client(tmp_path)
        client._initialized = False
        positions = await client.definition("src/main.c", 1, 2)
        assert positions == []

    @pytest.mark.asyncio
    async def test_lsp_client_definition_error_returns_empty(self, tmp_path):
        """请求异常时返回空列表（优雅降级）"""
        client = self._make_client(tmp_path)
        client._initialized = True
        client._send_request = AsyncMock(side_effect=RuntimeError("LSP 错误"))

        positions = await client.definition("src/main.c", 1, 2)
        assert positions == []

    @pytest.mark.asyncio
    async def test_lsp_client_references(self, tmp_path):
        """查找引用（mock）"""
        client = self._make_client(tmp_path)
        client._initialized = True
        ref_file = tmp_path / "ref.c"
        ref_file.write_text("", encoding="utf-8")
        client._send_request = AsyncMock(return_value=[
            {"uri": ref_file.as_uri(), "range": {"start": {"line": 10, "character": 0}}},
            {"uri": ref_file.as_uri(), "range": {"start": {"line": 20, "character": 4}}},
        ])

        positions = await client.references("src/main.c", 1, 2)
        assert len(positions) == 2
        assert positions[0].line == 10
        assert positions[1].line == 20

    @pytest.mark.asyncio
    async def test_lsp_client_references_not_initialized(self, tmp_path):
        """未初始化时引用查找返回空列表"""
        client = self._make_client(tmp_path)
        client._initialized = False
        positions = await client.references("src/main.c", 1, 2)
        assert positions == []

    @pytest.mark.asyncio
    async def test_lsp_client_hover(self, tmp_path):
        """悬停文档（mock MarkupContent）"""
        client = self._make_client(tmp_path)
        client._initialized = True
        client._send_request = AsyncMock(return_value={
            "contents": {"kind": "markdown", "value": "**int** main()"},
            "range": {"start": {"line": 1, "character": 0}, "end": {"line": 1, "character": 3}},
        })

        hover = await client.hover("src/main.c", 1, 0)
        assert hover is not None
        assert "int" in hover.content
        assert hover.range_start is not None
        assert hover.range_start.line == 1
        assert hover.range_end is not None
        assert hover.range_end.col == 3

    @pytest.mark.asyncio
    async def test_lsp_client_hover_string_contents(self, tmp_path):
        """悬停文档字符串 contents（MarkedString）"""
        client = self._make_client(tmp_path)
        client._initialized = True
        client._send_request = AsyncMock(return_value={"contents": "plain text"})

        hover = await client.hover("src/main.c", 0, 0)
        assert hover is not None
        assert hover.content == "plain text"
        assert hover.range_start is None

    @pytest.mark.asyncio
    async def test_lsp_client_hover_none(self, tmp_path):
        """悬停无结果返回 None"""
        client = self._make_client(tmp_path)
        client._initialized = True
        client._send_request = AsyncMock(return_value=None)

        hover = await client.hover("src/main.c", 0, 0)
        assert hover is None

    @pytest.mark.asyncio
    async def test_lsp_client_hover_not_initialized(self, tmp_path):
        """未初始化时悬停返回 None"""
        client = self._make_client(tmp_path)
        client._initialized = False
        hover = await client.hover("src/main.c", 0, 0)
        assert hover is None

    @pytest.mark.asyncio
    async def test_lsp_client_completion(self, tmp_path):
        """代码补全（CompletionList 格式）"""
        client = self._make_client(tmp_path)
        client._initialized = True
        client._send_request = AsyncMock(return_value={
            "isIncomplete": False,
            "items": [
                {"label": "main", "kind": 3, "detail": "int main()", "insertText": "main()"},
                {"label": "printf", "kind": 2, "documentation": "打印函数"},
            ],
        })

        completions = await client.completion("src/main.c", 5, 5)
        assert len(completions) == 2
        assert completions[0].label == "main"
        assert completions[0].detail == "int main()"
        assert completions[1].documentation == "打印函数"

    @pytest.mark.asyncio
    async def test_lsp_client_completion_list_response(self, tmp_path):
        """补全返回直接 list（CompletionItem[] 格式）"""
        client = self._make_client(tmp_path)
        client._initialized = True
        client._send_request = AsyncMock(return_value=[
            {"label": "foo", "kind": 1},
        ])

        completions = await client.completion("src/main.c", 5, 5)
        assert len(completions) == 1
        assert completions[0].label == "foo"

    @pytest.mark.asyncio
    async def test_lsp_client_completion_not_initialized(self, tmp_path):
        """未初始化时补全返回空列表"""
        client = self._make_client(tmp_path)
        client._initialized = False
        result = await client.completion("src/main.c", 5, 5)
        assert result == []

    @pytest.mark.asyncio
    async def test_lsp_client_handle_diagnostics(self, tmp_path):
        """_handle_diagnostics 解析 publishDiagnostics 通知"""
        client = self._make_client(tmp_path)
        target_file = tmp_path / "src" / "main.c"
        target_file.parent.mkdir(parents=True)
        target_file.write_text("", encoding="utf-8")

        await client._handle_diagnostics({
            "uri": target_file.as_uri(),
            "diagnostics": [
                {
                    "range": {
                        "start": {"line": 1, "character": 0},
                        "end": {"line": 1, "character": 5},
                    },
                    "severity": 1,
                    "source": "clangd",
                    "message": "未声明",
                    "code": "undecl",
                },
            ],
        })

        # 诊断按文件路径缓存
        key = str(target_file)
        assert key in client._diagnostics
        diags = client._diagnostics[key]
        assert len(diags) == 1
        assert diags[0].severity == 1
        assert diags[0].source == "clangd"
        assert diags[0].message == "未声明"
        assert diags[0].code == "undecl"
        assert diags[0].end_line == 1

    @pytest.mark.asyncio
    async def test_lsp_client_handle_diagnostics_empty(self, tmp_path):
        """_handle_diagnostics 空诊断列表（清空已有诊断）"""
        client = self._make_client(tmp_path)
        target_file = tmp_path / "empty.c"
        target_file.write_text("", encoding="utf-8")

        await client._handle_diagnostics({
            "uri": target_file.as_uri(),
            "diagnostics": [],
        })
        key = str(target_file)
        assert key in client._diagnostics
        assert client._diagnostics[key] == []

    def test_uri_to_path_windows(self):
        """_uri_to_path 处理 Windows 风格 file:///C:/... URI"""
        # file:///C:/Users/foo/main.c → C:\Users\foo\main.c (on Windows)
        result = LSPClient._uri_to_path("file:///C:/Users/foo/main.c")
        # 结果应包含 main.c，且不包含 file:// 前缀
        assert "main.c" in result
        assert not result.startswith("file://")

    def test_uri_to_path_linux(self):
        """_uri_to_path 处理 Linux 风格 file:///home/user/... URI"""
        result = LSPClient._uri_to_path("file:///home/user/src/main.c")
        assert "main.c" in result
        assert not result.startswith("file://")

    def test_uri_to_path_non_file_uri(self):
        """_uri_to_path 非 file:// URI 原样返回"""
        result = LSPClient._uri_to_path("untitled:Untitled-1")
        assert result == "untitled:Untitled-1"


# ── LSP 工具测试 ────────────────────────────────────────────────

class TestLSPTools:
    """LSP 工具测试（mock LSPClient）"""

    def test_lsp_diagnostics_tool_schema(self):
        """LSPDiagnosticsTool schema 正确"""
        tool = LSPDiagnosticsTool()
        assert tool.name == "lsp_diagnostics"
        schema = tool.schema
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "lsp_diagnostics"
        assert "file" in schema["function"]["parameters"]["properties"]
        assert schema["function"]["parameters"]["required"] == ["file"]

    @pytest.mark.asyncio
    async def test_lsp_tools_diagnostics(self, tmp_path):
        """LSP 诊断工具（mock）"""
        client = MagicMock()
        client._initialized = True
        abs_path = str((tmp_path / "src" / "main.c").resolve())
        client.get_diagnostics = AsyncMock(return_value=[
            LSPDiagnostic(
                file=abs_path, line=5, col=0,
                end_line=5, end_col=10,
                severity=1, source="clangd",
                message="未声明标识符", code="undecl",
            ),
        ])

        tool = LSPDiagnosticsTool(client=client)
        result = await tool.execute({"file": "src/main.c"}, {"project_dir": str(tmp_path)})

        assert result["success"] is True
        assert result["count"] == 1
        assert result["diagnostics"][0]["severity_name"] == "error"
        assert result["diagnostics"][0]["message"] == "未声明标识符"
        assert result["diagnostics"][0]["source"] == "clangd"

    @pytest.mark.asyncio
    async def test_lsp_tools_diagnostics_not_started(self):
        """LSP 未启动时返回错误"""
        client = MagicMock()
        client._initialized = False
        tool = LSPDiagnosticsTool(client=client)
        result = await tool.execute({"file": "a.c"}, {})
        assert result["success"] is False
        assert "未启动" in result["error"]
        assert result["diagnostics"] == []

    @pytest.mark.asyncio
    async def test_lsp_tools_diagnostics_missing_file(self):
        """缺少 file 参数返回错误"""
        client = MagicMock()
        client._initialized = True
        tool = LSPDiagnosticsTool(client=client)
        result = await tool.execute({}, {})
        assert result["success"] is False
        assert "缺少 file 参数" in result["error"]

    @pytest.mark.asyncio
    async def test_lsp_tools_definition(self, tmp_path):
        """LSP 跳转定义工具（mock）"""
        client = MagicMock()
        client._initialized = True
        client.definition = AsyncMock(return_value=[
            LSPPosition(file="lib.c", line=10, col=0),
        ])

        tool = LSPDefinitionTool(client=client)
        result = await tool.execute(
            {"file": "src/main.c", "line": 1, "col": 0},
            {"project_dir": str(tmp_path)},
        )

        assert result["success"] is True
        assert result["count"] == 1
        assert result["definitions"][0]["file"] == "lib.c"
        assert result["definitions"][0]["line"] == 10

    @pytest.mark.asyncio
    async def test_lsp_tools_definition_not_started(self):
        """LSP 跳转定义工具未启动时返回错误"""
        client = MagicMock()
        client._initialized = False
        tool = LSPDefinitionTool(client=client)
        result = await tool.execute({"file": "a.c", "line": 0, "col": 0}, {})
        assert result["success"] is False
        assert "未启动" in result["error"]

    @pytest.mark.asyncio
    async def test_lsp_tools_references(self, tmp_path):
        """LSP 引用工具（mock）"""
        client = MagicMock()
        client._initialized = True
        client.references = AsyncMock(return_value=[
            LSPPosition(file="a.c", line=1, col=0),
            LSPPosition(file="b.c", line=2, col=0),
        ])

        tool = LSPReferencesTool(client=client)
        result = await tool.execute(
            {"file": "src/main.c", "line": 1, "col": 0},
            {"project_dir": str(tmp_path)},
        )

        assert result["success"] is True
        assert result["count"] == 2
        assert result["references"][0]["file"] == "a.c"
        assert result["references"][1]["file"] == "b.c"

    @pytest.mark.asyncio
    async def test_lsp_tools_hover(self, tmp_path):
        """LSP 悬停工具（mock）"""
        client = MagicMock()
        client._initialized = True
        client.hover = AsyncMock(return_value=LSPHover(content="int x"))

        tool = LSPHoverTool(client=client)
        result = await tool.execute(
            {"file": "src/main.c", "line": 1, "col": 0},
            {"project_dir": str(tmp_path)},
        )

        assert result["success"] is True
        assert result["hover"]["content"] == "int x"

    @pytest.mark.asyncio
    async def test_lsp_tools_hover_none(self, tmp_path):
        """LSP 悬停无结果时返回 hover=None"""
        client = MagicMock()
        client._initialized = True
        client.hover = AsyncMock(return_value=None)

        tool = LSPHoverTool(client=client)
        result = await tool.execute(
            {"file": "src/main.c", "line": 1, "col": 0},
            {"project_dir": str(tmp_path)},
        )

        assert result["success"] is True
        assert result["hover"] is None

    @pytest.mark.asyncio
    async def test_lsp_tools_completion(self, tmp_path):
        """LSP 补全工具（mock）"""
        client = MagicMock()
        client._initialized = True
        client.completion = AsyncMock(return_value=[
            LSPCompletion(label="foo", kind=3, detail="int foo", insert_text="foo()"),
        ])

        tool = LSPCompletionTool(client=client)
        result = await tool.execute(
            {"file": "src/main.c", "line": 1, "col": 0},
            {"project_dir": str(tmp_path)},
        )

        assert result["success"] is True
        assert result["count"] == 1
        assert result["completions"][0]["label"] == "foo"
        assert result["completions"][0]["detail"] == "int foo"
        assert result["completions"][0]["insert_text"] == "foo()"

    @pytest.mark.asyncio
    async def test_lsp_tools_completion_not_started(self):
        """LSP 补全工具未启动时返回错误"""
        client = MagicMock()
        client._initialized = False
        tool = LSPCompletionTool(client=client)
        result = await tool.execute({"file": "a.c", "line": 0, "col": 0}, {})
        assert result["success"] is False
        assert "未启动" in result["error"]

    @pytest.mark.asyncio
    async def test_lsp_tools_set_client(self):
        """set_client 注入客户端"""
        tool = LSPDiagnosticsTool()
        assert tool._client is None
        client = MagicMock()
        tool.set_client(client)
        assert tool._client is client

    @pytest.mark.asyncio
    async def test_lsp_tools_diagnostics_exception(self):
        """LSP 诊断工具异常时优雅降级"""
        client = MagicMock()
        client._initialized = True
        client.get_diagnostics = AsyncMock(side_effect=RuntimeError("连接断开"))

        tool = LSPDiagnosticsTool(client=client)
        result = await tool.execute({"file": "a.c"}, {})
        assert result["success"] is False
        assert "连接断开" in result["error"]
        assert result["diagnostics"] == []
