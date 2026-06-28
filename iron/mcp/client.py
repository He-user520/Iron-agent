"""MCP 客户端 — 连接本地/远程 MCP 服务器，注册工具

支持三种传输（参考 MCP 协议规范 2024-11-05）：
1. stdio — 本地子进程（stdin/stdout JSON-RPC 2.0）
2. sse  — 远程 SSE 服务器（GET /sse 接收 + POST /messages 发送）
3. http — Streamable HTTP（POST 单一端点）

参考 OpenCode MCP 设计：MCP 工具自动合并到工具列表，与内置工具统一管理。
"""
import asyncio
import json
import logging
import os
from urllib.parse import urlparse, urlunparse
import httpx
from iron import __version__  # 统一版本号来源，避免硬编码
from iron.tools.base import BaseTool
from iron.llm.backend import LLMBackend  # 复用 LLMBackend._sanitize_error，避免重复定义

logger = logging.getLogger(__name__)

# 环境变量敏感关键字（出现任一即视为敏感，不继承给子进程）
_SENSITIVE_KEYS = ("KEY", "SECRET", "TOKEN", "PASSWORD", "PASSWD", "PASS", "PWD", "CREDENTIAL", "AUTH", "PRIVATE")


def _filter_env(env):
    """过滤环境变量中的敏感项，避免泄露给子进程"""
    if not env:
        return {}
    return {
        k: v for k, v in env.items()
        if not any(sens in k.upper() for sens in _SENSITIVE_KEYS)
    }


class MCPToolWrapper(BaseTool):
    """MCP 工具包装器 — 将 MCP 工具适配为 iron BaseTool"""

    def __init__(self, name: str, description: str, input_schema: dict, client: "MCPClient"):
        self._name = name
        self._description = description
        self._input_schema = input_schema
        self._client = client

    @property
    def name(self) -> str:
        return self._name

    @property
    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self._name,
                "description": self._description,
                "parameters": self._input_schema,
            },
        }

    async def execute(self, args: dict, context: dict) -> dict:
        try:
            result = await self._client.call_tool(self._name, args)
            return {"success": True, "result": result}
        except (RuntimeError, ValueError, KeyError, OSError, asyncio.TimeoutError, httpx.HTTPError) as e:
            # 脱敏异常信息，避免泄漏 API key/Bearer 等敏感字段给 AI
            return {"success": False, "error": MCPClient._sanitize_error(str(e))}


class MCPClient:
    """MCP 客户端 — 管理 MCP 服务器连接和工具

    支持三种传输类型：
    - local/stdio: 本地子进程（stdin/stdout）
    - sse: 远程 SSE 服务器（GET /sse + POST /messages）
    - http: Streamable HTTP（POST 单一端点）
    """

    def __init__(self):
        self._servers: dict[str, dict] = {}  # name -> server config
        self._tools: dict[str, MCPToolWrapper] = {}  # tool_name -> wrapper
        self._rpc_id = 0  # JSON-RPC 请求 ID 计数器（实例独立）

    @staticmethod
    def _sanitize_error(text: str) -> str:
        """脱敏错误消息中的敏感信息（API key、Bearer token、Authorization header 等）

        复用 LLMBackend._sanitize_error，避免两处正则重复定义。
        """
        if not text:
            return text
        return LLMBackend._sanitize_error(text)

    @staticmethod
    async def _drain_stderr(proc, name: str):
        """后台消费子进程 stderr，避免缓冲区满后阻塞 stdout

        在 connect_local 成功保存 proc 后启动为 asyncio task，
        disconnect_all 时取消。
        """
        try:
            while True:
                line = await proc.stderr.readline()
                if not line:
                    break
                logger.debug("MCP[%s] stderr: %s", name, line.decode("utf-8", errors="replace").rstrip())
        except OSError:
            # stderr 已关闭或读取异常，安静退出
            pass

    def _next_rpc_id(self) -> int:
        """生成下一个 JSON-RPC 请求 ID"""
        self._rpc_id += 1
        return self._rpc_id

    def add_server(self, name: str, config: dict):
        """添加 MCP 服务器配置"""
        self._servers[name] = config

    # ── stdio 传输 ──────────────────────────────────────────────

    async def connect_local(self, name: str, command: list[str], timeout: int = 5000, env: dict = None):
        """连接本地 MCP 服务器（stdio 传输），获取工具列表

        Args:
            name: 服务器名称
            command: 完整启动命令列表（如 ["npx", "-y", "@modelcontextprotocol/server-filesystem"]）
            timeout: 连接超时（毫秒）
            env: 环境变量（如 {"API_KEY": "xxx"}），会合并到子进程环境
        """
        proc = None
        try:
            # 合并环境变量（继承父进程环境 + 覆盖 MCP 配置的 env）
            # 过滤敏感环境变量，避免泄露给子进程
            proc_env = _filter_env(os.environ)
            if env:
                proc_env.update({k: str(v) for k, v in env.items()})

            # 通过 stdin/stdout 与 MCP 服务器通信（JSON-RPC 2.0）
            proc = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=proc_env,
            )

            # 发送 initialize 请求
            init_msg = json.dumps({
                "jsonrpc": "2.0",
                "id": self._next_rpc_id(),
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "iron", "version": __version__},
                },
            }) + "\n"

            proc.stdin.write(init_msg.encode("utf-8"))
            await proc.stdin.drain()

            # 读取响应
            response = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout / 1000)
            if response:
                init_result = json.loads(response.decode("utf-8").strip())
                # 检查 initialize 是否返回错误
                if "error" in init_result:
                    error_msg = init_result["error"]
                    if isinstance(error_msg, dict):
                        error_msg = error_msg.get("message", str(error_msg))
                    logger.warning("  MCP 服务器 %s initialize 错误: %s", name, error_msg)
                    # 清理子进程避免成为孤儿
                    try:
                        proc.kill()
                        await proc.wait()
                    except OSError:
                        pass
                    return

                # 发送 notifications/initialized 通知（MCP 协议要求）
                init_done_msg = json.dumps({
                    "jsonrpc": "2.0",
                    "method": "notifications/initialized",
                    "params": {},
                }) + "\n"
                proc.stdin.write(init_done_msg.encode("utf-8"))
                await proc.stdin.drain()

                # 发送 tools/list 请求
                list_msg = json.dumps({
                    "jsonrpc": "2.0",
                    "id": self._next_rpc_id(),
                    "method": "tools/list",
                    "params": {},
                }) + "\n"
                proc.stdin.write(list_msg.encode("utf-8"))
                await proc.stdin.drain()

                tools_response = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout / 1000)
                if tools_response:
                    tools_result = json.loads(tools_response.decode("utf-8").strip())
                    if "error" in tools_result:
                        error_msg = tools_result["error"]
                        if isinstance(error_msg, dict):
                            error_msg = error_msg.get("message", str(error_msg))
                        logger.warning("  MCP 服务器 %s tools/list 错误: %s", name, error_msg)
                    else:
                        tools = tools_result.get("result", {}).get("tools", [])
                        for tool_def in tools:
                            tool_name = f"{name}__{tool_def['name']}"
                            wrapper = MCPToolWrapper(
                                name=tool_name,
                                description=tool_def.get("description", ""),
                                input_schema=tool_def.get("inputSchema", {"type": "object", "properties": {}}),
                                client=self,
                            )
                            self._tools[tool_name] = wrapper
            else:
                # EOF — 子进程 stdout 已关闭，清理避免成为孤儿
                logger.warning("  MCP 服务器 %s 连接关闭（EOF，无响应）", name)
                try:
                    proc.kill()
                    await proc.wait()
                except OSError:
                    pass
                return

            # 保持进程运行
            self._servers[name]["_process"] = proc
            self._servers[name]["_transport"] = "stdio"
            self._servers[name]["_available"] = True  # 连接成功后显式重置可用性
            self._servers[name]["_call_lock"] = asyncio.Lock()  # 串行化 stdio 调用
            # 启动后台任务消费 stderr，避免缓冲区满后阻塞子进程 stdout
            self._servers[name]["_stderr_task"] = asyncio.create_task(
                MCPClient._drain_stderr(proc, name)
            )
            proc = None  # 已保存到 _servers，不需要在 except 中清理

        except asyncio.TimeoutError:
            if proc is not None and proc.returncode is None:
                try:
                    proc.kill()
                    await proc.wait()
                except OSError:
                    pass
            # 连接失败时显式重置 _available，避免误用未连接服务器
            self._servers[name]["_available"] = False
            logger.warning("  MCP 服务器 %s 连接超时", name)
        except FileNotFoundError:
            if proc is not None and proc.returncode is None:
                try:
                    proc.kill()
                    await proc.wait()
                except OSError:
                    pass
            self._servers[name]["_available"] = False
            logger.warning("  MCP 服务器 %s 命令未找到: %s", name, command)
        except (BrokenPipeError, ConnectionResetError) as e:
            if proc is not None and proc.returncode is None:
                try:
                    proc.kill()
                    await proc.wait()
                except OSError:
                    pass
            self._servers[name]["_available"] = False
            logger.warning("  MCP 子进程 %s 连接断开: %s", name, e)
        except (httpx.HTTPError, OSError, RuntimeError, json.JSONDecodeError,
                asyncio.TimeoutError, BrokenPipeError, ConnectionResetError) as e:
            # 兜底：其他具体异常仍标记不可用，但收窄类型避免吞 KeyError/AttributeError 等编程 bug
            if proc is not None and proc.returncode is None:
                try:
                    proc.kill()
                    await proc.wait()
                except OSError:
                    pass
            self._servers[name]["_available"] = False
            logger.warning("  MCP 服务器 %s 连接失败: %s", name, e)

    async def _call_stdio(self, server_name: str, mcp_tool_name: str, arguments: dict) -> dict:
        """通过 stdio 调用 MCP 工具"""
        server = self._servers.get(server_name)
        if not server:
            return {"error": f"未知服务器: {server_name}"}
        if not server.get("_available", True):
            raise RuntimeError(f"MCP 服务器不可用: {server_name}")
        proc = server.get("_process")
        if not proc:
            raise RuntimeError(f"MCP 服务器 {server_name} 未连接")

        msg = json.dumps({
            "jsonrpc": "2.0",
            "id": self._next_rpc_id(),
            "method": "tools/call",
            "params": {
                "name": mcp_tool_name,
                "arguments": arguments,
            },
        }) + "\n"

        # 串行化 stdio 调用（每个服务器一把锁，避免并发请求导致响应错位）
        lock = server.get("_call_lock")
        if lock is None:
            lock = asyncio.Lock()
            server["_call_lock"] = lock
        async with lock:
            try:
                proc.stdin.write(msg.encode("utf-8"))
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as e:
                # 清理 stderr_task 避免任务悬挂
                stderr_task = server.pop("_stderr_task", None)
                if stderr_task and not stderr_task.done():
                    stderr_task.cancel()
                    try:
                        await stderr_task
                    except (asyncio.CancelledError, OSError):
                        pass
                try:
                    proc.kill()
                    await proc.wait()
                except OSError:
                    pass
                server["_process"] = None
                server["_available"] = False
                raise RuntimeError(f"MCP 子进程已断开: {server_name}") from e

            try:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=30.0)
            except asyncio.TimeoutError:
                logger.warning("MCP stdio 超时，终止子进程: %s", server_name)
                # 清理 stderr_task 避免任务悬挂
                stderr_task = server.pop("_stderr_task", None)
                if stderr_task and not stderr_task.done():
                    stderr_task.cancel()
                    try:
                        await stderr_task
                    except (asyncio.CancelledError, OSError):
                        pass
                try:
                    proc.kill()
                    await proc.wait()
                except OSError:
                    pass
                server["_available"] = False
                server["_process"] = None
                raise RuntimeError(f"MCP 服务器响应超时: {server_name}")

            if not line:
                raise RuntimeError(f"MCP 服务器无响应: {server_name}")

            try:
                result = json.loads(line.decode("utf-8").strip())
            except json.JSONDecodeError:
                raise RuntimeError(f"MCP 服务器返回非 JSON 响应: {line.decode('utf-8', errors='replace')[:200]}")
            if "error" in result:
                error_msg = result["error"]
                if isinstance(error_msg, dict):
                    error_msg = error_msg.get("message", str(error_msg))
                raise RuntimeError(f"MCP 错误: {error_msg}")
            return result.get("result", {})

    # ── SSE 传输 ────────────────────────────────────────────────

    async def connect_sse(self, name: str, url: str, timeout: int = 5000, headers: dict = None):
        """连接远程 SSE MCP 服务器，获取工具列表

        MCP SSE 传输协议：
        - 客户端 GET /sse 建立 SSE 长连接，接收服务器推送的消息
        - 客户端 POST /messages 发送 JSON-RPC 请求
        - 服务器通过 SSE event 推送响应

        注意：当前 SSE 实现为简化版，不支持服务器主动推送。
        建立 SSE 连接读取 endpoint 后即关闭流，后续通过 POST /messages 发送请求。
        如果服务器需要通过 SSE 流推送响应，会因连接已关闭而超时失败。

        Args:
            name: 服务器名称
            url: SSE 端点 URL（如 https://api.example.com/sse）
            timeout: 连接超时（毫秒）
            headers: 自定义请求头（如 Authorization）
        """
        client = None
        try:
            import httpx

            # 分层超时：连接 timeout ms，读取 30s
            client = httpx.AsyncClient(timeout=httpx.Timeout(timeout / 1000, read=30.0))
            req_headers = headers or {}

            # 1. 建立 SSE 连接，读取 endpoint 事件
            # 注意：SSE 流在读取 endpoint 后关闭，不支持服务器后续推送
            endpoint_url = None
            async with client.stream("GET", url, headers=req_headers) as response:
                if response.status_code != 200:
                    logger.warning("  MCP SSE 服务器 %s 返回 %s", name, response.status_code)
                    try:  # 失败路径显式 aclose client 避免泄漏
                        await client.aclose()
                    except (httpx.HTTPError, OSError, RuntimeError):
                        pass
                    return

                async for line in response.aiter_lines():
                    if line.startswith("data:"):
                        data = line[5:].strip()
                        # SSE endpoint 事件格式: {"endpoint": "/messages", "method": "POST"}
                        try:
                            ep_data = json.loads(data)
                            if "endpoint" in ep_data:
                                # endpoint 为绝对 URL 时直接使用，避免错误拼接
                                ep = ep_data["endpoint"]
                                if ep.startswith(("http://", "https://")):
                                    # SSRF 防护：仅允许 endpoint 与 SSE 服务器同源
                                    ep_parsed = urlparse(ep)
                                    url_parsed = urlparse(url)
                                    if ep_parsed.netloc != url_parsed.netloc:
                                        logger.warning("MCP SSE endpoint 跨域被拒: %s", ep[:100])
                                        try:
                                            await client.aclose()
                                        except (httpx.HTTPError, OSError, RuntimeError):
                                            pass
                                        return
                                    endpoint_url = ep
                                else:
                                    parsed = urlparse(url)
                                    base = urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))
                                    endpoint_url = base.rstrip("/") + "/" + ep.lstrip("/")
                                break
                        except json.JSONDecodeError:
                            # 可能是直接返回 endpoint 路径或绝对 URL
                            if data.startswith(("http://", "https://", "/")):
                                if data.startswith(("http://", "https://")):
                                    # SSRF 防护：仅允许 endpoint 与 SSE 服务器同源
                                    data_parsed = urlparse(data)
                                    url_parsed = urlparse(url)
                                    if data_parsed.netloc != url_parsed.netloc:
                                        logger.warning("MCP SSE endpoint 跨域被拒: %s", data[:100])
                                        try:
                                            await client.aclose()
                                        except (httpx.HTTPError, OSError, RuntimeError):
                                            pass
                                        return
                                    endpoint_url = data
                                else:
                                    parsed = urlparse(url)
                                    base = urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))
                                    endpoint_url = base.rstrip("/") + "/" + data.lstrip("/")
                                break

            if not endpoint_url:
                # 降级：假设 messages 端点为 /messages
                parsed = urlparse(url)
                base = urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))
                endpoint_url = base.rstrip("/") + "/messages"

            # 2. 发送 initialize 请求（POST /messages）
            init_msg = {
                "jsonrpc": "2.0",
                "id": self._next_rpc_id(),
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "iron", "version": __version__},
                },
            }
            resp = await client.post(endpoint_url, json=init_msg, headers=req_headers)
            if resp.status_code != 200:
                logger.warning("  MCP SSE 服务器 %s initialize 失败: %s", name, resp.status_code)
                try:  # 失败路径显式 aclose client 避免泄漏
                    await client.aclose()
                except (httpx.HTTPError, OSError, RuntimeError):
                    pass
                return

            # 检查 initialize 响应错误
            try:  # 捕获 JSONDecodeError，避免非 JSON 响应导致崩溃
                init_result = resp.json()
            except json.JSONDecodeError:
                logger.warning("MCP SSE 服务器 %s 返回非 JSON 响应: %s", name, resp.text[:200])
                try:
                    await client.aclose()
                except (httpx.HTTPError, OSError, RuntimeError):
                    pass
                return
            if "error" in init_result:
                error_msg = init_result["error"]
                if isinstance(error_msg, dict):
                    error_msg = error_msg.get("message", str(error_msg))
                logger.warning("  MCP SSE 服务器 %s initialize 错误: %s", name, error_msg)
                try:  # 错误路径显式 aclose client 避免泄漏
                    await client.aclose()
                except (httpx.HTTPError, OSError, RuntimeError):
                    pass
                return

            # 发送 notifications/initialized 通知（MCP 协议要求）
            init_done_msg = {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {},
            }
            await client.post(endpoint_url, json=init_done_msg, headers=req_headers)

            # 3. 发送 tools/list 请求
            list_msg = {
                "jsonrpc": "2.0",
                "id": self._next_rpc_id(),
                "method": "tools/list",
                "params": {},
            }
            resp = await client.post(endpoint_url, json=list_msg, headers=req_headers)
            if resp.status_code != 200:
                # tools/list 非 200 时不应标记 _available=True
                logger.warning("  MCP SSE 服务器 %s tools/list 失败: %s", name, resp.status_code)
                try:
                    await client.aclose()
                except (httpx.HTTPError, OSError, RuntimeError):
                    pass
                return
            try:  # 捕获 JSONDecodeError，避免非 JSON 响应导致崩溃
                tools_result = resp.json()
            except json.JSONDecodeError:
                logger.warning("MCP SSE 服务器 %s tools/list 返回非 JSON 响应: %s", name, resp.text[:200])
                tools_result = {}
            if "error" in tools_result:
                error_msg = tools_result["error"]
                if isinstance(error_msg, dict):
                    error_msg = error_msg.get("message", str(error_msg))
                logger.warning("  MCP SSE 服务器 %s tools/list 错误: %s", name, error_msg)
            else:
                tools = tools_result.get("result", {}).get("tools", [])
                for tool_def in tools:
                    tool_name = f"{name}__{tool_def['name']}"
                    wrapper = MCPToolWrapper(
                        name=tool_name,
                        description=tool_def.get("description", ""),
                        input_schema=tool_def.get("inputSchema", {"type": "object", "properties": {}}),
                        client=self,
                    )
                    self._tools[tool_name] = wrapper

            # 保存连接信息
            self._servers[name]["_http_client"] = client
            self._servers[name]["_endpoint_url"] = endpoint_url
            self._servers[name]["_headers"] = req_headers
            self._servers[name]["_transport"] = "sse"
            self._servers[name]["_available"] = True  # 连接成功后显式重置可用性
            self._servers[name]["_call_lock"] = asyncio.Lock()  # SSE 调用串行化（同 stdio）
            client = None  # 已保存到 _servers，不需要在 except 中关闭

        except (httpx.HTTPError, OSError, RuntimeError, json.JSONDecodeError,
                asyncio.TimeoutError, ValueError, AttributeError) as e:
            if client is not None:
                try:
                    await client.aclose()
                except (httpx.HTTPError, OSError, RuntimeError):
                    pass
            logger.warning("  MCP SSE 服务器 %s 连接失败: %s", name, e)

    async def _call_sse(self, server_name: str, mcp_tool_name: str, arguments: dict) -> dict:
        """通过 SSE/HTTP 调用 MCP 工具（POST /messages）"""
        server = self._servers.get(server_name)
        if not server:
            raise RuntimeError(f"MCP 服务器未配置: {server_name}")
        # 与 _call_stdio 一致，加 _available 检查
        if not server.get("_available", True):
            raise RuntimeError(f"MCP 服务器不可用: {server_name}")
        client = server.get("_http_client")
        endpoint_url = server.get("_endpoint_url")
        headers = server.get("_headers", {})
        if not client or not endpoint_url:
            raise RuntimeError(f"MCP 服务器 {server_name} 未连接")

        msg = {
            "jsonrpc": "2.0",
            "id": self._next_rpc_id(),
            "method": "tools/call",
            "params": {
                "name": mcp_tool_name,
                "arguments": arguments,
            },
        }
        # 串行化 SSE 调用（与 _call_stdio 一致，避免并发请求响应错位）
        lock = server.get("_call_lock")
        if lock is None:
            lock = asyncio.Lock()
            server["_call_lock"] = lock
        async with lock:
            try:
                resp = await client.post(endpoint_url, json=msg, headers=headers, timeout=30.0)
            except (httpx.TimeoutException, httpx.ConnectError, httpx.HTTPError) as e:
                # 网络错误后重置状态，避免后续请求继续打到失效连接
                server["_available"] = False
                try:
                    await client.aclose()
                except (httpx.HTTPError, OSError, RuntimeError):
                    pass
                server["_http_client"] = None
                raise RuntimeError(f"MCP 网络错误: {e}") from e
        if resp.status_code != 200:
            # 脱敏 resp.text，避免 Authorization header 泄漏给 AI
            raise RuntimeError(f"MCP HTTP 错误 {resp.status_code}: {self._sanitize_error(resp.text[:200])}")
        try:
            result = resp.json()
        except json.JSONDecodeError:
            raise RuntimeError(f"MCP 服务器返回非 JSON 响应: {self._sanitize_error(resp.text[:200])}")
        if "error" in result:
            error_msg = result["error"]
            if isinstance(error_msg, dict):
                error_msg = error_msg.get("message", str(error_msg))
            raise RuntimeError(f"MCP 错误: {error_msg}")
        return result.get("result", {})

    # ── Streamable HTTP 传输 ────────────────────────────────────

    async def connect_http(self, name: str, url: str, timeout: int = 5000, headers: dict = None):
        """连接 Streamable HTTP MCP 服务器，获取工具列表

        MCP Streamable HTTP 传输协议（2025-03-26）：
        - 单一端点，客户端 POST JSON-RPC 请求，服务器返回 JSON-RPC 响应
        - 比 SSE 更简单，适合无状态服务器

        Args:
            name: 服务器名称
            url: HTTP 端点 URL（如 https://api.example.com/mcp）
            timeout: 连接超时（毫秒）
            headers: 自定义请求头（如 Authorization）
        """
        client = None
        try:
            import httpx

            # 分层超时：连接 timeout ms，读取 30s
            client = httpx.AsyncClient(timeout=httpx.Timeout(timeout / 1000, read=30.0))
            req_headers = {"Content-Type": "application/json"}
            if headers:
                req_headers.update(headers)

            # 1. 发送 initialize 请求
            init_msg = {
                "jsonrpc": "2.0",
                "id": self._next_rpc_id(),
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "iron", "version": __version__},
                },
            }
            resp = await client.post(url, json=init_msg, headers=req_headers)
            if resp.status_code != 200:
                logger.warning("  MCP HTTP 服务器 %s initialize 失败: %s", name, resp.status_code)
                try:  # 失败路径显式 aclose client 避免泄漏
                    await client.aclose()
                except (httpx.HTTPError, OSError, RuntimeError):
                    pass
                return

            # 检查 initialize 响应错误
            try:  # 捕获 JSONDecodeError，避免非 JSON 响应导致崩溃
                init_result = resp.json()
            except json.JSONDecodeError:
                logger.warning("MCP HTTP 服务器 %s 返回非 JSON 响应: %s", name, resp.text[:200])
                try:
                    await client.aclose()
                except (httpx.HTTPError, OSError, RuntimeError):
                    pass
                return
            if "error" in init_result:
                error_msg = init_result["error"]
                if isinstance(error_msg, dict):
                    error_msg = error_msg.get("message", str(error_msg))
                logger.warning("  MCP HTTP 服务器 %s initialize 错误: %s", name, error_msg)
                try:  # 错误路径显式 aclose client 避免泄漏
                    await client.aclose()
                except (httpx.HTTPError, OSError, RuntimeError):
                    pass
                return

            # 发送 notifications/initialized 通知（MCP 协议要求）
            init_done_msg = {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {},
            }
            await client.post(url, json=init_done_msg, headers=req_headers)

            # 2. 发送 tools/list 请求
            list_msg = {
                "jsonrpc": "2.0",
                "id": self._next_rpc_id(),
                "method": "tools/list",
                "params": {},
            }
            resp = await client.post(url, json=list_msg, headers=req_headers)
            if resp.status_code != 200:
                # tools/list 非 200 时不应标记 _available=True
                logger.warning("  MCP HTTP 服务器 %s tools/list 失败: %s", name, resp.status_code)
                try:
                    await client.aclose()
                except (httpx.HTTPError, OSError, RuntimeError):
                    pass
                return
            try:  # 捕获 JSONDecodeError，避免非 JSON 响应导致崩溃
                tools_result = resp.json()
            except json.JSONDecodeError:
                logger.warning("MCP HTTP 服务器 %s tools/list 返回非 JSON 响应: %s", name, resp.text[:200])
                tools_result = {}
            if "error" in tools_result:
                error_msg = tools_result["error"]
                if isinstance(error_msg, dict):
                    error_msg = error_msg.get("message", str(error_msg))
                logger.warning("  MCP HTTP 服务器 %s tools/list 错误: %s", name, error_msg)
            else:
                tools = tools_result.get("result", {}).get("tools", [])
                for tool_def in tools:
                    tool_name = f"{name}__{tool_def['name']}"
                    wrapper = MCPToolWrapper(
                        name=tool_name,
                        description=tool_def.get("description", ""),
                        input_schema=tool_def.get("inputSchema", {"type": "object", "properties": {}}),
                        client=self,
                    )
                    self._tools[tool_name] = wrapper

            # 保存连接信息
            self._servers[name]["_http_client"] = client
            self._servers[name]["_endpoint_url"] = url
            self._servers[name]["_headers"] = req_headers
            self._servers[name]["_transport"] = "http"
            self._servers[name]["_available"] = True  # 连接成功后显式重置可用性
            self._servers[name]["_call_lock"] = asyncio.Lock()  # HTTP 调用串行化（同 stdio）
            client = None  # 已保存到 _servers，不需要在 except 中关闭

        except (httpx.HTTPError, OSError, RuntimeError, json.JSONDecodeError,
                asyncio.TimeoutError, ValueError, AttributeError) as e:
            if client is not None:
                try:
                    await client.aclose()
                except (httpx.HTTPError, OSError, RuntimeError):
                    pass
            logger.warning("  MCP HTTP 服务器 %s 连接失败: %s", name, e)

    async def _call_http(self, server_name: str, mcp_tool_name: str, arguments: dict) -> dict:
        """通过 Streamable HTTP 调用 MCP 工具"""
        server = self._servers.get(server_name)
        if not server:
            raise RuntimeError(f"MCP 服务器未配置: {server_name}")
        # 与 _call_stdio 一致，加 _available 检查
        if not server.get("_available", True):
            raise RuntimeError(f"MCP 服务器不可用: {server_name}")
        client = server.get("_http_client")
        url = server.get("_endpoint_url")
        headers = server.get("_headers", {})
        if not client or not url:
            raise RuntimeError(f"MCP 服务器 {server_name} 未连接")

        msg = {
            "jsonrpc": "2.0",
            "id": self._next_rpc_id(),
            "method": "tools/call",
            "params": {
                "name": mcp_tool_name,
                "arguments": arguments,
            },
        }
        # 串行化 HTTP 调用（与 _call_stdio 一致，避免并发请求响应错位）
        lock = server.get("_call_lock")
        if lock is None:
            lock = asyncio.Lock()
            server["_call_lock"] = lock
        async with lock:
            try:
                resp = await client.post(url, json=msg, headers=headers, timeout=30.0)
            except (httpx.TimeoutException, httpx.ConnectError, httpx.HTTPError) as e:
                # 网络错误后重置状态，避免后续请求继续打到失效连接
                server["_available"] = False
                try:
                    await client.aclose()
                except (httpx.HTTPError, OSError, RuntimeError):
                    pass
                server["_http_client"] = None
                raise RuntimeError(f"MCP 网络错误: {e}") from e
        if resp.status_code != 200:
            # 脱敏 resp.text，避免 Authorization header 泄漏给 AI
            raise RuntimeError(f"MCP HTTP 错误 {resp.status_code}: {self._sanitize_error(resp.text[:200])}")
        try:
            result = resp.json()
        except json.JSONDecodeError:
            raise RuntimeError(f"MCP 服务器返回非 JSON 响应: {self._sanitize_error(resp.text[:200])}")
        if "error" in result:
            error_msg = result["error"]
            if isinstance(error_msg, dict):
                error_msg = error_msg.get("message", str(error_msg))
            raise RuntimeError(f"MCP 错误: {error_msg}")
        return result.get("result", {})

    # ── 统一分发 ────────────────────────────────────────────────

    async def call_tool(self, tool_name: str, arguments: dict) -> dict:
        """调用 MCP 工具（根据传输类型自动分发）

        Args:
            tool_name: 工具全名（格式: {server_name}__{mcp_tool_name}）
            arguments: 工具参数
        """
        # 从 tool_name 中提取 server name 和 tool name
        parts = tool_name.split("__", 1)
        if len(parts) != 2:
            raise ValueError(f"无效的 MCP 工具名: {tool_name}")
        server_name, mcp_tool_name = parts

        server = self._servers.get(server_name)
        if not server:
            raise RuntimeError(f"MCP 服务器 {server_name} 未配置")

        transport = server.get("_transport", "stdio")

        # 根据传输类型分发
        if transport == "stdio":
            return await self._call_stdio(server_name, mcp_tool_name, arguments)
        elif transport == "sse":
            return await self._call_sse(server_name, mcp_tool_name, arguments)
        elif transport == "http":
            return await self._call_http(server_name, mcp_tool_name, arguments)
        else:
            raise RuntimeError(f"未知的传输类型: {transport}")

    def get_tools(self) -> list[MCPToolWrapper]:
        """获取所有 MCP 工具"""
        return list(self._tools.values())

    async def connect_all(self):
        """连接所有已配置的 MCP 服务器（根据 type 自动选择传输）"""
        for name, config in self._servers.items():
            srv_type = config.get("type", "local")
            timeout = config.get("timeout", 5000)

            if srv_type in ("local", "stdio") and config.get("command"):
                await self.connect_local(
                    name,
                    config["command"],
                    timeout,
                    config.get("env"),
                )
            elif srv_type == "sse" and config.get("url"):
                await self.connect_sse(
                    name,
                    config["url"],
                    timeout,
                    config.get("headers"),
                )
            elif srv_type == "http" and config.get("url"):
                await self.connect_http(
                    name,
                    config["url"],
                    timeout,
                    config.get("headers"),
                )
            else:
                logger.warning("  MCP 服务器 %s 配置不完整（type=%s）", name, srv_type)

    async def disconnect_all(self):
        """断开所有 MCP 服务器（支持 stdio/SSE/HTTP）"""
        for name, server in self._servers.items():
            transport = server.get("_transport", "stdio")
            # stdio: 终止子进程
            if transport == "stdio":
                # 先取消 stderr 消费任务，避免在 proc.terminate 后读 stderr 抛异常
                stderr_task = server.pop("_stderr_task", None)
                if stderr_task is not None and not stderr_task.done():
                    stderr_task.cancel()
                    try:
                        await stderr_task
                    except (asyncio.CancelledError, OSError):
                        pass
                proc = server.get("_process")
                if proc:
                    try:
                        proc.terminate()
                        await asyncio.wait_for(proc.wait(), timeout=5.0)
                    except asyncio.TimeoutError:
                        try:
                            proc.kill()
                            await proc.wait()
                        except OSError:
                            pass
                    except OSError:
                        pass
            # sse/http: 关闭 httpx 客户端
            elif transport in ("sse", "http"):
                client = server.get("_http_client")
                if client:
                    try:
                        await client.aclose()
                    except (httpx.HTTPError, OSError, RuntimeError):
                        pass

    async def reconnect(self, server_name: str, max_retries: int = 3) -> bool:
        """重新连接 MCP 服务器（网络错误后允许重连，避免必须重启 CLI）

        Args:
            server_name: 服务器名称
            max_retries: 最大重试次数（默认 3），全部失败后标记 disconnected

        Returns:
            True 表示重连成功（_available=True），False 表示服务器不存在或重连失败
        """
        server = self._servers.get(server_name)
        if not server:
            return False
        transport = server.get("_transport")
        if not transport:
            # 从未连接过，无法重连
            return False

        # 多次重试，全部失败后标记 disconnected
        for attempt in range(max_retries):
            # 重置状态，确保 connect_* 内部逻辑从干净状态开始
            server["_available"] = True
            server["_process"] = None
            server["_http_client"] = None
            server["_tools"] = []
            server["_endpoint_url"] = None
            # 根据传输类型重新连接（参数从原始 config 读取，保持与 connect_all 一致）
            try:
                if transport == "stdio":
                    await self.connect_local(
                        server_name,
                        server.get("command", []),
                        server.get("timeout", 5000),
                        server.get("env", {}),
                    )
                elif transport == "sse":
                    await self.connect_sse(
                        server_name,
                        server.get("url", ""),
                        server.get("timeout", 5000),
                        server.get("headers", {}),
                    )
                elif transport == "http":
                    await self.connect_http(
                        server_name,
                        server.get("url", ""),
                        server.get("timeout", 5000),
                        server.get("headers", {}),
                    )
                else:
                    return False
            except (httpx.HTTPError, OSError, RuntimeError, asyncio.TimeoutError) as e:
                logger.warning("MCP 服务器 %s 重连失败 (尝试 %d/%d): %s",
                               server_name, attempt + 1, max_retries, e)
                # 退避：1s, 2s, 4s
                if attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt)
                continue

            if server.get("_available", False):
                if attempt > 0:
                    logger.info("MCP 服务器 %s 重连成功（第 %d 次尝试）", server_name, attempt + 1)
                return True

        # 全部重试失败，标记 disconnected 并发射事件
        server["_available"] = False
        server["_disconnected"] = True
        self._emit_event("mcp_disconnected", server_name)
        logger.error("MCP 服务器 %s 连续 %d 次重连失败，已标记 disconnected",
                     server_name, max_retries)
        return False

    # ── 健康检查 ────────────────────────────────────────────────

    def _emit_event(self, event_name: str, server_name: str = "") -> None:
        """发射 MCP 事件（当前仅记录日志，后续可扩展为回调列表）

        Args:
            event_name: 事件名（如 "mcp_disconnected"）
            server_name: 涉及的服务器名
        """
        # 简单实现：记录到日志。后续可扩展为 _event_callbacks 列表分发。
        logger.info("MCP 事件: %s (server=%s)", event_name, server_name)

    async def _ping_server(self, server_name: str, timeout: float = 5.0) -> bool:
        """对单个服务器发轻量 ping（用 tools/list 作为健康检查）

        MCP 协议未标准化 ping 方法，但所有服务器都应支持 tools/list，
        因此用 tools/list 作为健康检查请求。

        Args:
            server_name: 服务器名称
            timeout: 超时秒数（默认 5s）

        Returns:
            True 表示服务器响应正常，False 表示不可达/超时/异常
        """
        server = self._servers.get(server_name)
        if not server:
            return False
        if not server.get("_available", False):
            return False
        transport = server.get("_transport")
        if not transport:
            return False

        # 构造 tools/list 请求
        list_msg = {
            "jsonrpc": "2.0",
            "id": self._next_rpc_id(),
            "method": "tools/list",
            "params": {},
        }

        try:
            if transport == "stdio":
                proc = server.get("_process")
                if not proc or proc.returncode is not None:
                    return False
                lock = server.get("_call_lock") or asyncio.Lock()
                async with lock:
                    proc.stdin.write((json.dumps(list_msg) + "\n").encode("utf-8"))
                    await proc.stdin.drain()
                    line = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)
                    if not line:
                        return False
                    resp = json.loads(line.decode("utf-8").strip())
                    return "result" in resp or "error" in resp  # 有响应即视为存活

            elif transport in ("sse", "http"):
                client = server.get("_http_client")
                endpoint = server.get("_endpoint_url") or server.get("url", "")
                headers = server.get("_headers", {})
                if not client or not endpoint:
                    return False
                lock = server.get("_call_lock") or asyncio.Lock()
                async with lock:
                    resp = await client.post(endpoint, json=list_msg,
                                              headers=headers, timeout=timeout)
                    if resp.status_code != 200:
                        return False
                    data = resp.json()
                    return "result" in data or "error" in data

        except (asyncio.TimeoutError, httpx.HTTPError, OSError, RuntimeError,
                json.JSONDecodeError, BrokenPipeError, ConnectionResetError) as e:
            logger.debug("MCP ping %s 失败: %s", server_name, e)
            return False
        return False

    async def health_check(self, auto_reconnect: bool = True) -> dict:
        """检查所有服务器连接健康状态

        Args:
            auto_reconnect: 对不可达的服务器自动重连（默认 True）

        Returns:
            dict: {server_name: {"healthy": bool, "reconnected": bool}}
            healthy=True 表示当前可用；reconnected=True 表示本次自动重连成功
        """
        results: dict[str, dict] = {}
        for name, server in self._servers.items():
            healthy = await self._ping_server(name)
            reconnected = False
            if not healthy and auto_reconnect:
                # 服务器不可达，尝试重连
                reconnected = await self.reconnect(name)
                healthy = reconnected
            results[name] = {
                "healthy": healthy,
                "reconnected": reconnected,
                "transport": server.get("_transport", "unknown"),
            }
        return results

    def get_server_status(self) -> dict:
        """获取所有服务器的状态快照（不发起网络请求）

        Returns:
            dict: {server_name: {"available": bool, "transport": str, "tools_count": int}}
        """
        status: dict[str, dict] = {}
        for name, server in self._servers.items():
            # 统计该服务器的工具数
            tools_count = sum(
                1 for tname in self._tools
                if tname.startswith(f"{name}__")
            )
            status[name] = {
                "available": server.get("_available", False),
                "transport": server.get("_transport", "unknown"),
                "tools_count": tools_count,
                "disconnected": server.get("_disconnected", False),
            }
        return status
