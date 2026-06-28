"""LSP 客户端 — Language Server Protocol 客户端

支持 clangd 和 ccls，提供诊断/跳转定义/引用查找/悬停/补全能力。
嵌入式定制：自动检测 compile_commands.json，自动查找 clangd/ccls。
LSP 不可用时优雅降级（返回空列表），所有 async 方法处理 CancelledError。
"""
import asyncio
import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import unquote, urlparse

logger = logging.getLogger(__name__)


@dataclass
class LSPDiagnostic:
    """LSP 诊断信息"""
    file: str
    line: int  # 0-based
    col: int
    end_line: int = 0
    end_col: int = 0
    severity: int = 1  # 1=Error, 2=Warning, 3=Info, 4=Hint
    source: str = ""  # "clangd" / "ccls"
    message: str = ""
    code: str = ""


@dataclass
class LSPPosition:
    """LSP 位置"""
    file: str
    line: int
    col: int


@dataclass
class LSPHover:
    """LSP 悬停信息"""
    content: str
    range_start: Optional[LSPPosition] = None
    range_end: Optional[LSPPosition] = None


@dataclass
class LSPCompletion:
    """LSP 补全项"""
    label: str
    kind: int  # LSP CompletionItemKind
    detail: str = ""
    documentation: str = ""
    insert_text: str = ""


@dataclass
class LSPConfig:
    """LSP 配置"""
    server_command: str = "clangd"  # clangd / ccls 路径
    server_args: list = field(default_factory=list)
    enabled: bool = True
    compile_commands_dir: str = ""  # build/ 目录
    init_options: dict = field(default_factory=dict)


class LSPClient:
    """LSP 客户端 — stdio 传输

    用法:
        client = LSPClient(config)
        await client.start()
        diags = await client.get_diagnostics("src/main.c")
        await client.stop()

    LSP 不可用时所有查询方法返回空列表 / None（优雅降级）。
    """

    _CONTENT_LENGTH_PREFIX = b"Content-Length: "  # LSP 消息头前缀
    _REQUEST_TIMEOUT = 30.0  # 默认请求超时（秒）

    def __init__(self, config: LSPConfig = None, project_root: str = "."):
        self.config = config or LSPConfig()
        self.project_root = Path(project_root).resolve()
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._stdin: Optional[asyncio.StreamWriter] = None
        self._stdout: Optional[asyncio.StreamReader] = None
        self._initialized = False
        self._diagnostics: dict[str, list[LSPDiagnostic]] = {}
        self._request_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._reader_task: Optional[asyncio.Task] = None

    # ── 服务器检测与 compile_commands 查找 ─────────────────────

    @staticmethod
    def detect_server() -> Optional[str]:
        """检测可用的 LSP 服务器（clangd 优先，ccls 次之）"""
        for cmd in ("clangd", "ccls"):
            path = shutil.which(cmd)
            if path:
                return path
        return None

    @staticmethod
    def find_compile_commands(project_root: Path) -> Optional[Path]:
        """查找 compile_commands.json

        检查路径：
        - project_root/build/compile_commands.json
        - project_root/.pio/build/*/compile_commands.json (PlatformIO)
        - project_root/cmake-build-*/compile_commands.json
        """
        root = Path(project_root)

        # 标准位置：build/
        candidate = root / "build" / "compile_commands.json"
        if candidate.exists():
            return candidate

        # PlatformIO: .pio/build/<env>/
        pio_build = root / ".pio" / "build"
        if pio_build.is_dir():
            for env_dir in sorted(pio_build.iterdir()):
                cc = env_dir / "compile_commands.json"
                if cc.exists():
                    return cc

        # CMake: cmake-build-*/
        for d in sorted(root.glob("cmake-build-*")):
            if d.is_dir():
                cc = d / "compile_commands.json"
                if cc.exists():
                    return cc

        return None

    # ── 生命周期 ───────────────────────────────────────────────

    async def start(self) -> bool:
        """启动 LSP 服务器进程"""
        if not self.config.enabled:
            logger.info("LSP 已禁用")
            return False

        # 检测服务器命令
        cmd = self.config.server_command
        if not shutil.which(cmd):
            detected = self.detect_server()
            if not detected:
                logger.warning("未找到 clangd/ccls，LSP 客户端不可用")
                return False
            cmd = detected

        # 构造完整启动命令
        full_cmd = [cmd] + list(self.config.server_args)
        if self.config.compile_commands_dir:
            full_cmd.extend(["--compile-commands-dir", self.config.compile_commands_dir])

        try:
            self._proc = await asyncio.create_subprocess_exec(
                *full_cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self._stdin = self._proc.stdin
            self._stdout = self._proc.stdout
        except (FileNotFoundError, OSError) as e:
            logger.warning("LSP 服务器启动失败: %s", e)
            self._proc = None
            return False

        # 启动后台消息读取任务
        self._reader_task = asyncio.create_task(self._read_messages())

        # 执行 initialize 握手
        if not await self.initialize():
            await self.stop()
            return False

        self._initialized = True
        return True

    async def stop(self) -> None:
        """停止 LSP 服务器"""
        # 取消后台读取任务
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, RuntimeError, OSError, asyncio.IncompleteReadError):
                pass
            self._reader_task = None

        if self._proc:
            try:
                try:  # 发送 exit 通知（不等待响应）
                    await self._send_notification("exit", {})
                except (RuntimeError, OSError, ValueError):
                    pass
                self._proc.terminate()
                try:
                    await asyncio.wait_for(self._proc.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    self._proc.kill()
                    await self._proc.wait()
                except (RuntimeError, OSError):
                    pass
            except asyncio.CancelledError:
                raise
            except (RuntimeError, OSError):
                pass
            self._proc = None
            self._stdin = None
            self._stdout = None

        self._initialized = False
        # 取消所有挂起的 future
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()

    # ── 消息收发 ───────────────────────────────────────────────

    async def _send_request(self, method: str, params: dict) -> dict:
        """发送 LSP 请求（等待响应）"""
        if not self._stdin or not self._proc:
            raise RuntimeError("LSP 客户端未启动")
        self._request_id += 1
        req_id = self._request_id
        message = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}

        loop = asyncio.get_event_loop()
        fut = loop.create_future()
        self._pending[req_id] = fut

        try:
            await self._write_message(message)
        except (RuntimeError, OSError, ValueError) as e:
            self._pending.pop(req_id, None)
            raise RuntimeError(f"发送 LSP 请求失败: {e}")
        try:
            return await asyncio.wait_for(fut, timeout=self._REQUEST_TIMEOUT)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            raise RuntimeError(f"LSP 请求超时: {method}")
        except asyncio.CancelledError:
            self._pending.pop(req_id, None)
            raise

    async def _send_notification(self, method: str, params: dict) -> None:
        """发送 LSP 通知（不等待响应）"""
        if not self._stdin or not self._proc:
            raise RuntimeError("LSP 客户端未启动")
        await self._write_message({"jsonrpc": "2.0", "method": method, "params": params})

    async def _write_message(self, message: dict) -> None:
        """写入 LSP 消息（带 Content-Length 头）"""
        data = json.dumps(message).encode("utf-8")
        header = f"Content-Length: {len(data)}\r\n\r\n".encode("utf-8")
        try:
            self._stdin.write(header + data)
            await self._stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            raise RuntimeError(f"LSP 写入失败: {e}")

    async def _read_messages(self) -> None:
        """读取 LSP 消息循环（后台任务）

        解析 Content-Length 头 + JSON body，分发响应到 _pending future，
        处理 textDocument/publishDiagnostics 通知。
        """
        try:
            while True:
                headers: dict = {}
                while True:  # 读取 header
                    line = await self._stdout.readline()
                    if not line:
                        return  # EOF
                    line = line.rstrip(b"\r\n")
                    if not line:
                        break  # 头部结束
                    if line.startswith(self._CONTENT_LENGTH_PREFIX):
                        try:
                            headers["Content-Length"] = int(line[len(self._CONTENT_LENGTH_PREFIX):])
                        except ValueError:
                            continue
                length = headers.get("Content-Length")
                if not length:
                    continue
                body = await self._stdout.readexactly(length)  # 读取 body
                try:
                    message = json.loads(body.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError) as e:
                    logger.warning("LSP 消息解析失败: %s", e)
                    continue
                # 响应消息（有 id，无 method）
                if "id" in message and "method" not in message:
                    req_id = message["id"]
                    fut = self._pending.pop(req_id, None)
                    if fut and not fut.done():
                        if "error" in message:
                            fut.set_exception(RuntimeError(f"LSP 错误: {message['error']}"))
                        else:
                            fut.set_result(message.get("result", {}))
                elif "method" in message:  # 通知或请求
                    if message.get("method") == "textDocument/publishDiagnostics":
                        await self._handle_diagnostics(message.get("params", {}))
        except asyncio.CancelledError:
            raise
        except (RuntimeError, OSError, asyncio.IncompleteReadError) as e:
            logger.debug("LSP 消息读取结束: %s", e)

    async def _handle_diagnostics(self, params: dict) -> None:
        """处理 publishDiagnostics 通知"""
        uri = params.get("uri", "")
        if not uri:
            return
        file_path = self._uri_to_path(uri)
        diags: list[LSPDiagnostic] = []
        for d in params.get("diagnostics", []):
            start = d.get("range", {}).get("start", {})
            end = d.get("range", {}).get("end", {})
            diags.append(LSPDiagnostic(
                file=file_path, line=start.get("line", 0), col=start.get("character", 0),
                end_line=end.get("line", 0), end_col=end.get("character", 0),
                severity=d.get("severity", 1), source=d.get("source", ""),
                message=d.get("message", ""), code=str(d.get("code", "")),
            ))
        self._diagnostics[file_path] = diags

    # ── LSP 协议方法 ────────────────────────────────────────────

    async def initialize(self) -> bool:
        """LSP initialize 握手"""
        capabilities = {
            "textDocument": {
                "sync": {"didOpen": True, "didChange": True, "didClose": True},
                "publishDiagnostics": {"relatedInformation": True},
                "definition": {"linkSupport": False},
                "references": {},
                "hover": {"contentFormat": ["markdown", "plaintext"]},
                "completion": {"triggerCharacters": [".", ">", ":"], "resolveProvider": False},
            },
        }
        try:
            await self._send_request("initialize", {
                "processId": os.getpid(),
                "rootUri": self.project_root.as_uri(),
                "capabilities": capabilities,
                "initializationOptions": dict(self.config.init_options),
            })
        except (RuntimeError, asyncio.TimeoutError) as e:
            logger.warning("LSP initialize 失败: %s", e)
            return False

        # 发送 initialized 通知
        try:
            await self._send_notification("initialized", {})
        except RuntimeError as e:
            logger.warning("LSP initialized 通知失败: %s", e)
            return False
        return True

    def _file_uri(self, file_path: str) -> str:
        """将文件路径转换为 file:// URI"""
        p = Path(file_path)
        if not p.is_absolute():
            p = self.project_root / file_path
        return p.resolve().as_uri()

    @staticmethod
    def _uri_to_path(uri: str) -> str:
        """file:// URI 转换为文件路径

        Windows: file:///C:/Users/... → C:\\Users\\...
        Linux:   file:///home/user/... → /home/user/...
        """
        if uri.startswith("file://"):
            parsed = urlparse(uri)
            path = unquote(parsed.path)
            # Windows: /C:/... → C:/...
            if path.startswith("/") and len(path) > 2 and path[2] == ":":
                path = path[1:]
            return str(Path(path))
        return uri

    async def _notify_text_document(self, method: str, params: dict, label: str) -> None:
        """发送 textDocument/* 通知的通用包装（处理 CancelledError 与 RuntimeError）"""
        if not self._initialized:
            return
        try:
            await self._send_notification(method, params)
        except asyncio.CancelledError:
            raise
        except RuntimeError as e:
            logger.warning("LSP %s 失败: %s", label, e)

    async def did_open(self, file_path: str, content: str) -> None:
        """通知 LSP 文件打开"""
        uri = self._file_uri(file_path)
        ext = Path(file_path).suffix.lower()
        lang_id = {
            ".c": "c", ".h": "c",
            ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp",
            ".hpp": "cpp", ".hh": "cpp",
        }.get(ext, "c")
        await self._notify_text_document("textDocument/didOpen", {
            "textDocument": {"uri": uri, "languageId": lang_id, "version": 1, "text": content},
        }, "didOpen")

    async def did_change(self, file_path: str, content: str) -> None:
        """通知 LSP 文件修改"""
        uri = self._file_uri(file_path)
        await self._notify_text_document("textDocument/didChange", {
            "textDocument": {"uri": uri, "version": 2},
            "contentChanges": [{"text": content}],
        }, "didChange")

    async def did_close(self, file_path: str) -> None:
        """通知 LSP 文件关闭"""
        uri = self._file_uri(file_path)
        await self._notify_text_document("textDocument/didClose", {
            "textDocument": {"uri": uri},
        }, "didClose")

    async def get_diagnostics(self, file_path: str) -> list[LSPDiagnostic]:
        """获取文件诊断（推送模式，从本地缓存读取；未启动返回空列表）"""
        if not self._initialized:
            return []
        p = Path(file_path)
        if not p.is_absolute():
            p = self.project_root / file_path
        key = str(p.resolve())
        return self._diagnostics.get(key, [])

    async def _request_text_document(self, method: str, file_path: str,
                                      line: int, col: int, label: str,
                                      extra_params: dict = None) -> Optional[dict]:
        """发送 textDocument/* 请求的通用包装（处理 CancelledError 与超时）"""
        if not self._initialized:
            return None
        uri = self._file_uri(file_path)
        params = {"textDocument": {"uri": uri}, "position": {"line": line, "character": col}}
        if extra_params:
            params.update(extra_params)
        try:
            return await self._send_request(method, params)
        except asyncio.CancelledError:
            raise
        except (RuntimeError, asyncio.TimeoutError) as e:
            logger.warning("LSP %s 失败: %s", label, e)
            return None

    async def definition(self, file_path: str, line: int, col: int) -> list[LSPPosition]:
        """跳转定义"""
        result = await self._request_text_document(
            "textDocument/definition", file_path, line, col, "definition")
        return self._parse_locations(result)

    async def references(self, file_path: str, line: int, col: int) -> list[LSPPosition]:
        """查找引用"""
        result = await self._request_text_document(
            "textDocument/references", file_path, line, col, "references",
            extra_params={"context": {"includeDeclaration": True}},
        )
        return self._parse_locations(result)

    def _parse_locations(self, result) -> list[LSPPosition]:
        """解析 LSP location / location[] 响应为 LSPPosition 列表"""
        if not result:
            return []
        locations = [result] if isinstance(result, dict) else result if isinstance(result, list) else []
        positions: list[LSPPosition] = []
        for loc in locations:
            uri = loc.get("uri", "")
            if not uri:
                continue
            file_path = self._uri_to_path(uri)
            start = loc.get("range", {}).get("start", {})
            positions.append(LSPPosition(
                file=file_path,
                line=start.get("line", 0),
                col=start.get("character", 0),
            ))
        return positions

    async def hover(self, file_path: str, line: int, col: int) -> Optional[LSPHover]:
        """悬停文档"""
        result = await self._request_text_document(
            "textDocument/hover", file_path, line, col, "hover")
        if not result:
            return None

        content = self._parse_hover_content(result.get("contents", ""))

        range_start = range_end = None
        range_obj = result.get("range")
        if range_obj:
            s = range_obj.get("start", {})
            e = range_obj.get("end", {})
            hover_file = self._uri_to_path(self._file_uri(file_path))
            range_start = LSPPosition(hover_file, s.get("line", 0), s.get("character", 0))
            range_end = LSPPosition(hover_file, e.get("line", 0), e.get("character", 0))

        return LSPHover(content=content, range_start=range_start, range_end=range_end)

    @staticmethod
    def _parse_hover_content(contents) -> str:
        """解析 hover contents（支持字符串 / MarkupContent / MarkedString[]）"""
        if isinstance(contents, str):
            return contents
        if isinstance(contents, dict):
            return contents.get("value", "")
        if isinstance(contents, list):
            parts = [item if isinstance(item, str) else item.get("value", "")
                     for item in contents if isinstance(item, (str, dict))]
            return "\n\n".join(parts)
        return ""

    async def completion(self, file_path: str, line: int, col: int) -> list[LSPCompletion]:
        """代码补全"""
        result = await self._request_text_document(
            "textDocument/completion", file_path, line, col, "completion")
        if not result:
            return []
        # 结果可能是 CompletionList 或 CompletionItem[]
        if isinstance(result, dict):
            items = result.get("items", [])
        elif isinstance(result, list):
            items = result
        else:
            return []

        completions: list[LSPCompletion] = []
        for item in items:
            completions.append(LSPCompletion(
                label=item.get("label", ""),
                kind=item.get("kind", 1),
                detail=item.get("detail", ""),
                documentation=self._parse_doc(item.get("documentation", "")),
                insert_text=item.get("insertText", item.get("label", "")),
            ))
        return completions

    @staticmethod
    def _parse_doc(doc) -> str:
        """解析 documentation 字段"""
        if isinstance(doc, str):
            return doc
        if isinstance(doc, dict):
            return doc.get("value", "")
        return ""
