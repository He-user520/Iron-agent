"""LLM 后端 — 支持 OpenAI / Anthropic / Ollama / Echo 四种后端"""
import asyncio
import json
import logging
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import AsyncGenerator
import httpx

from iron.constants import ECHO_COMPILE_KEYWORDS, ECHO_CHAT_KEYWORDS
from iron.llm.prompt_cache import PromptCache

logger = logging.getLogger(__name__)


@dataclass
class LLMResponse:
    content: str
    model: str = ""
    usage: dict | None = None
    tool_calls: list | None = None
    is_partial: bool = False  # Track 3: 标记流式 partial 响应

    def __post_init__(self):
        if self.usage is None:
            self.usage = {}
        if self.tool_calls is None:
            self.tool_calls = []


@dataclass
class StreamBuffer:
    """流式响应累积缓冲区

    在 stream_generate 迭代器内部累积 chunk，中断时 flush 保留已接收内容。
    满足 HC-2：KeyboardInterrupt/异常时通过 flush() 防止数据丢失。

    三态判定：
    - empty  + not complete → failed  （0 chunk，允许 fallback 重发，HC-3）
    - partial + not complete → partial（>0 chunk，禁止重发，HC-1）
    - complete              → complete（正常完成）
    """
    chunks: list = field(default_factory=list)
    accumulated_text: str = ""
    is_complete: bool = False
    failure_reason: str | None = None
    chunks_received: int = 0

    def append(self, chunk: str) -> None:
        """追加一个文本 chunk 到缓冲区

        Args:
            chunk: 文本增量（来自 ("chunk", str) 事件）
        """
        if not chunk:
            return
        self.chunks.append(chunk)
        self.accumulated_text += chunk
        self.chunks_received += 1

    def flush(self) -> str:
        """flush 缓冲区，返回已累积的完整文本

        HC-2 要求：KeyboardInterrupt/异常时必须调用此方法保留已接收内容。
        多次调用安全：返回同一份 accumulated_text，不重复拼接。
        """
        if not self.accumulated_text and self.chunks:
            # 兜底：若 accumulated_text 未同步（理论上不会），从 chunks 重新拼接
            self.accumulated_text = "".join(self.chunks)
        return self.accumulated_text

    def is_partial(self) -> bool:
        """是否已收到部分内容但不完整"""
        return self.chunks_received > 0 and not self.is_complete

    def is_empty(self) -> bool:
        """是否未收到任何 chunk（HC-3 判定：empty 才允许 fallback 重发）"""
        return self.chunks_received == 0

    def mark_complete(self) -> None:
        """标记流式正常完成"""
        self.is_complete = True

    def mark_failed(self, reason: str) -> None:
        """标记流式失败，记录失败原因（不抛异常，由调用方决策）"""
        self.failure_reason = reason

    def __len__(self) -> int:
        return self.chunks_received


@dataclass
class StreamResult:
    """stream_generate 的最终返回结果（三态）

    替代当前 (event_type, event_data) 二元组协议中的 ("response"|"error", ...) 终止事件。
    chunk 增量仍用 ("chunk", str) yield，终止事件改为 yield ("result", StreamResult)。
    """
    state: str  # "complete" | "partial" | "failed"
    content: str = ""
    tool_calls: list | None = None
    model: str = ""
    usage: dict = field(default_factory=dict)
    error: str | None = None
    chunks_received: int = 0

    @property
    def is_complete(self) -> bool:
        return self.state == "complete"

    @property
    def is_partial(self) -> bool:
        return self.state == "partial"

    @property
    def is_failed(self) -> bool:
        return self.state == "failed"

    @staticmethod
    def from_buffer(buf: StreamBuffer, *, model: str = "", usage: dict | None = None,
                    tool_calls: list | None = None) -> "StreamResult":
        """从 StreamBuffer 构造 complete 或 partial 结果

        - buf.is_complete → state="complete"
        - buf.is_partial  → state="partial"（不传 tool_calls，HC-4）
        - buf.is_empty    → state="failed"
        """
        if buf.is_complete:
            return StreamResult(
                state="complete", content=buf.flush(), tool_calls=tool_calls,
                model=model, usage=usage or {}, chunks_received=buf.chunks_received,
            )
        if buf.is_partial():
            return StreamResult(
                state="partial", content=buf.flush(), tool_calls=None,  # partial 不传 tool_calls（HC-4）
                model=model, usage=usage or {}, error=buf.failure_reason,
                chunks_received=buf.chunks_received,
            )
        return StreamResult(
            state="failed", content="", tool_calls=None, model=model,
            usage=usage or {}, error=buf.failure_reason or "未收到任何 chunk",
            chunks_received=0,
        )


class CircuitBreaker:
    """简单熔断器：连续 N 次失败后熔断，暂停 M 秒后 half-open 尝试恢复

    状态机：
    - closed: 正常，记录失败次数
    - open: 熔断，拒绝所有请求，reset_timeout 后转 half-open
    - half-open: 允许一次试探请求，成功转 closed，失败转 open
    """

    def __init__(self, failure_limit: int = 5, reset_timeout: float = 30.0):
        self.failure_limit = failure_limit
        self.reset_timeout = reset_timeout
        self.failures = 0
        self.last_failure = 0.0
        self.state = "closed"

    def record_success(self):
        """记录成功，重置失败计数"""
        self.failures = 0
        self.state = "closed"

    def record_failure(self):
        """记录失败，达到阈值后熔断"""
        self.failures += 1
        self.last_failure = time.monotonic()
        if self.failures >= self.failure_limit:
            self.state = "open"
            logger.warning("LLM 熔断器触发：连续 %d 次失败，%0.1f 秒内拒绝请求",
                           self.failures, self.reset_timeout)

    def can_execute(self) -> bool:
        """是否允许执行请求"""
        if self.state == "closed":
            return True
        if self.state == "open":
            # 检查是否过了冷却期
            if time.monotonic() - self.last_failure > self.reset_timeout:
                self.state = "half-open"
                return True
            return False
        # half-open: 只允许一次试探
        return True


class LLMBackend(ABC):
    """LLM 后端抽象基类"""

    # 重试配置
    _MAX_RETRIES = 3
    _RETRY_BASE_DELAY = 1.0  # 秒，指数退避基数

    def __init__(self, api_key: str = "", base_url: str = "", model: str = "",
                 timeout: int = 120, context_window: int = 30000):
        """LLM 后端基类初始化

        子类通过 super().__init__() 复用公共字段初始化，避免 4 倍签名重复。
        EchoBackend 无参数继承此默认值。

        Args:
            api_key: API 密钥（Ollama 本地后端可为空）
            base_url: API 基础 URL（不含末尾斜杠）
            model: 模型名称
            timeout: HTTP 超时秒数
            context_window: 上下文窗口大小（tokens），供 ContextCompactor 使用
        """
        # 熔断器：连续 5 次失败后熔断，30 秒后 half-open 尝试恢复
        self._circuit = CircuitBreaker(failure_limit=5, reset_timeout=30)
        self.api_key = api_key
        self.base_url = base_url.rstrip("/") if base_url else ""
        self.model = model
        self.client = httpx.AsyncClient(timeout=float(timeout)) if base_url else None
        # 上下文窗口大小（tokens），供 ContextCompactor 动态阈值使用
        self.context_window = context_window
        # P1-3: 系统提示分块缓存（默认 None，由 AgentEngine 初始化注入）
        # 后端 generate/stream_generate 在 use_cache=True 且 prompt_cache 非空时启用缓存
        self.prompt_cache: PromptCache | None = None

    @staticmethod
    def _sanitize_error(text: str) -> str:
        """脱敏错误响应中的 API key

        覆盖：
        - OpenAI/兼容平台 sk-... key（≥17 字符后缀）
        - Anthropic sk-ant-api03-... 格式（sk- 后接 ant-api03-...，含连字符）
        - Bearer / Authorization: Bearer|Basic|Token xxx
        - x-api-key: xxx（Anthropic 风格 header）
        """
        return re.sub(
            r'(sk-[a-zA-Z0-9][a-zA-Z0-9\-_]{15,}|'  # OpenAI / Anthropic
            r'Bearer\s+[^\s]+|'  # Bearer token
            r'Authorization:\s*(?:Bearer|Basic|Token)\s+[^\s]+|'  # Auth header
            r'x-api-key:\s*[^\s]+|'  # Anthropic x-api-key
            r'eyJ[a-zA-Z0-9_\-]{10,}\.[a-zA-Z0-9_\-]{10,}|'  # JWT (eyJ...)
            r'AKIA[0-9A-Z]{16}|'  # AWS Access Key
            r'AIza[0-9A-Za-z_\-]{35}|'  # Google API key
            r'gh[pousr]_[A-Za-z0-9]{36}'  # GitHub token (ghp_/gho_/ghu_/ghs_/ghr_)
            r')',
            '***REDACTED***', text, flags=re.IGNORECASE
        )

    @staticmethod
    def _is_retryable_status(status_code: int) -> bool:
        """判断 HTTP 状态码是否可重试"""
        return status_code in (429, 500, 502, 503, 504)

    @staticmethod
    def _get_retry_after(resp_headers: dict) -> float | None:
        """从 Retry-After 头解析等待时间"""
        retry_after = resp_headers.get("retry-after", "")
        if retry_after:
            try:
                return float(retry_after)
            except ValueError:
                pass
        return None

    async def _post_with_retry(self, url: str, json_payload: dict, headers: dict,
                                *, client: httpx.AsyncClient = None) -> httpx.Response:
        """带重试、429 处理和熔断的 POST 请求

        对 429/500/502/503/504 做指数退避重试，429 优先读取 Retry-After 头。
        超时和连接错误也重试。
        连续 _FAILURE_LIMIT 次失败后触发熔断，_RESET_TIMEOUT 秒内直接拒绝请求。
        """
        _client = client or getattr(self, "client", None)
        if _client is None:
            raise RuntimeError("httpx 客户端未初始化")
        # 熔断器检查
        if not self._circuit.can_execute():
            raise RuntimeError(
                f"LLM 服务熔断中（连续 {self._circuit.failure_limit} 次失败），"
                f"请稍后重试（{self._circuit.reset_timeout} 秒冷却）"
            )
        last_error = None
        for attempt in range(self._MAX_RETRIES):
            try:
                resp = await _client.post(url, json=json_payload, headers=headers)
                if not self._is_retryable_status(resp.status_code):
                    self._circuit.record_success()
                    return resp
                # 可重试的状态码
                if attempt < self._MAX_RETRIES - 1:
                    delay = self._get_retry_after(resp.headers) or (self._RETRY_BASE_DELAY * (2 ** attempt))
                    logger.warning("LLM API 返回 %d，%0.1f 秒后重试 (%d/%d)",
                                   resp.status_code, delay, attempt + 1, self._MAX_RETRIES)
                    await asyncio.sleep(delay)
                    continue
                # 重试耗尽，记录熔断失败
                self._circuit.record_failure()
                return resp  # 最后一次尝试，返回原始响应让调用方处理错误
            except httpx.TimeoutException as e:
                last_error = e
                if attempt < self._MAX_RETRIES - 1:
                    delay = self._RETRY_BASE_DELAY * (2 ** attempt)
                    logger.warning("LLM 请求超时，%0.1f 秒后重试 (%d/%d)", delay, attempt + 1, self._MAX_RETRIES)
                    await asyncio.sleep(delay)
                    continue
                self._circuit.record_failure()
                raise RuntimeError(f"LLM 请求超时（{getattr(self, 'base_url', '?')}）")
            except httpx.ConnectError:
                self._circuit.record_failure()
                raise RuntimeError(f"无法连接到 LLM 服务: {getattr(self, 'base_url', '?')}")
            except httpx.HTTPError as e:
                self._circuit.record_failure()
                raise RuntimeError(f"LLM 请求失败: {e}")
        # 重试耗尽（可重试状态码一直返回）
        self._circuit.record_failure()
        raise RuntimeError(f"LLM 请求失败，已重试 {self._MAX_RETRIES} 次: {last_error}")

    @abstractmethod
    async def generate(
        self,
        system: str,
        messages: list[dict],
        temperature: float = 0.3,
        max_tokens: int = 4096,
        tools: list = None,
    ) -> LLMResponse:
        pass

    def _resolve_prompt_cache(self, system: str, use_cache: bool) -> list[dict] | None:
        """P1-3: 解析系统提示的缓存块

        当 use_cache=True 且 self.prompt_cache 已注入时：
        1. 调用 split_prompt() 将系统提示切分为 Block A / Block B
        2. 对每个块调用 get_or_create() 记录命中次数
        3. 返回块列表（含 cache_key 与 hit_count 标记）

        未启用缓存时返回 None，调用方按原逻辑发送单条 system 消息。

        返回的块结构：
            [{"role": "system", "content": "...", "cache_key": "...",
              "hit_count": int, "hit": bool}, ...]
        """
        if not use_cache or self.prompt_cache is None or not system:
            return None
        blocks = self.prompt_cache.split_prompt(system)
        resolved = []
        for blk in blocks:
            cached = self.prompt_cache.get_or_create(blk["content"])
            resolved.append({
                "role": blk["role"],
                "content": blk["content"],
                "cache_key": blk["cache_key"],
                "hit_count": cached.hit_count,
                "hit": cached.hit_count > 0,
            })
        return resolved

    @staticmethod
    def _estimate_saved_tokens(blocks: list[dict] | None) -> int:
        """估算缓存命中节省的 token 数（粗略：字符数 / 4）

        用于遥测与 UI 显示，非精确计费。
        """
        if not blocks:
            return 0
        saved_chars = sum(len(b["content"]) for b in blocks if b.get("hit"))
        return saved_chars // 4

    async def stream_generate(
        self,
        system: str,
        messages: list[dict],
        temperature: float = 0.3,
        max_tokens: int = 4096,
        tools: list = None,
    ) -> AsyncGenerator[tuple, None]:
        """流式生成响应

        默认实现：fallback 到非流式 generate，先 yield ("chunk", str) 增量文本，
        最后 yield ("result", StreamResult) 三态终止事件（Track 3 改造）。
        子类可覆盖以实现真正的流式 SSE 输出。

        事件协议（新，Track 3）：
        - ("chunk", str): 文本增量（可选，用于 UI 流式显示）
        - ("result", StreamResult): 终止事件，三态 complete/partial/failed
        旧协议 ("response", LLMResponse) / ("error", str) 由 engine.py 兼容层识别，
        本 Track 完成后所有后端统一用新协议。
        """
        buf = StreamBuffer()
        try:
            resp = await self.generate(system, messages, temperature, max_tokens, tools)
            if resp.content:
                buf.append(resp.content)
                yield ("chunk", resp.content)
            buf.mark_complete()
            yield ("result", StreamResult(
                state="complete", content=resp.content, tool_calls=resp.tool_calls,
                model=resp.model, usage=resp.usage or {}, chunks_received=buf.chunks_received,
            ))
        except Exception as e:
            buf.mark_failed(str(e))
            yield ("result", StreamResult.from_buffer(buf))

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """生成文本向量（embedding）

        默认实现抛 NotImplementedError，子类按需覆盖：
        - OpenAIBackend: 调用 /v1/embeddings 端点
        - EchoBackend: 返回哈希伪向量（测试用）

        Args:
            texts: 待向量化的文本列表

        Returns:
            向量列表，每个向量是 float 列表（维度由模型决定）
        """
        raise NotImplementedError(f"{type(self).__name__} 不支持 embedding")

    async def aclose(self):
        """关闭后端资源（子类按需实现）"""
        pass


class OpenAIBackend(LLMBackend):
    """OpenAI 兼容后端（支持 DeepSeek/Together/国产平台）"""

    def __init__(self, api_key: str, base_url: str = "https://api.openai.com/v1", model: str = "gpt-4o", timeout: int = 120):
        super().__init__(api_key=api_key, base_url=base_url, model=model,
                         timeout=timeout, context_window=128000)

    async def aclose(self):
        """关闭 httpx 客户端"""
        await self.client.aclose()

    async def embed(self, texts: list[str], model: str = None) -> list[list[float]]:
        """生成文本向量 — 调用 /v1/embeddings 端点

        Args:
            texts: 待向量化的文本列表
            model: embedding 模型名（默认 text-embedding-3-small，1536 维）

        Returns:
            向量列表，每个向量是 float 列表
        """
        if not texts:
            return []
        embed_model = model or "text-embedding-3-small"
        payload = {
            "model": embed_model,
            "input": texts,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        url = self._build_url("embeddings")
        resp = await self._post_with_retry(url, payload, headers)
        if not (200 <= resp.status_code < 300):
            raise RuntimeError(f"Embedding API 返回 {resp.status_code}: {resp.text[:200]}")
        try:
            data = resp.json()
        except json.JSONDecodeError:
            raise RuntimeError(f"Embedding 响应非 JSON: {resp.text[:200]}")
        # OpenAI 返回格式：{"data": [{"embedding": [...], "index": 0}, ...]}
        embeddings_data = data.get("data", [])
        # 按 index 排序确保顺序与输入一致
        embeddings_data.sort(key=lambda x: x.get("index", 0))
        return [item["embedding"] for item in embeddings_data]

    def _build_url(self, endpoint: str) -> str:
        """构建 API URL，处理各种 URL 格式"""
        base = self.base_url
        # 如果 base_url 已经以 /v1 结尾，直接拼接
        if base.endswith("/v1"):
            return f"{base}/{endpoint}"
        # 如果已经包含完整路径（如 /v1/chat/completions），直接返回
        if base.endswith("/" + endpoint) or base.endswith(endpoint):
            return base
        # 默认拼接
        return f"{base}/v1/{endpoint}"

    async def generate(self, system, messages, temperature=0.3, max_tokens=4096, tools=None,
                       use_cache: bool = True):
        # P1-3: 系统提示分块缓存（OpenAI 无原生块级缓存，仅用 cache_key 标注 user 字段
        # 做遥测，并记录命中次数用于成本估算。system 内容保持原样不修改。）
        _blocks = self._resolve_prompt_cache(system, use_cache)
        _cache_key_tag = _blocks[0]["cache_key"] if _blocks else ""

        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}] + messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = tools
        # OpenAI 无原生 Prompt Caching，用 user 字段标注 cache_key 用于遥测
        if _cache_key_tag:
            payload["user"] = f"iron-cache:{_cache_key_tag}"

        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        url = self._build_url("chat/completions")
        resp = await self._post_with_retry(url, payload, headers)
        if not (200 <= resp.status_code < 300):
            error_body = self._sanitize_error(resp.text[:500])
            raise RuntimeError(f"OpenAI API 返回 {resp.status_code}: {error_body}")
        try:
            data = resp.json()
        except json.JSONDecodeError:
            error_body = self._sanitize_error(resp.text[:200])
            raise RuntimeError(f"响应非 JSON: {error_body}")
        if "error" in data:
            err = data["error"]
            err_msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            raise RuntimeError(f"API 返回错误: {self._sanitize_error(err_msg)}")
        choices = data.get("choices", [])
        if not choices:
            raise RuntimeError(f"API 返回空 choices: {data}")
        msg = choices[0].get("message", {})
        return LLMResponse(
            content=msg.get("content", "") or "",
            model=data.get("model", self.model),
            usage=data.get("usage", {}),
            tool_calls=msg.get("tool_calls"),
        )

    async def stream_generate(self, system, messages, temperature=0.3, max_tokens=4096, tools=None,
                              use_cache: bool = True):
        """OpenAI 流式响应，实时 yield 文本增量

        通过 SSE（Server-Sent Events）解析流式响应，累积 content 和 tool_calls，
        每收到一个文本块就 yield ("chunk", text) 供 UI 增量显示。
        """
        # P1-3: 系统提示分块缓存（与 generate 一致，仅遥测不改 system 内容）
        _blocks = self._resolve_prompt_cache(system, use_cache)
        _cache_key_tag = _blocks[0]["cache_key"] if _blocks else ""

        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}] + messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,  # 启用流式
        }
        if tools:
            payload["tools"] = tools
        if _cache_key_tag:
            payload["user"] = f"iron-cache:{_cache_key_tag}"

        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        url = self._build_url("chat/completions")

        buf = StreamBuffer()
        tool_calls_map = {}  # index -> {id, name, arguments_str}
        model_name = self.model
        finish_reason = None

        try:
            async with self.client.stream("POST", url, json=payload, headers=headers) as resp:
                if not (200 <= resp.status_code < 300):
                    body = await resp.aread()
                    error_body = self._sanitize_error(body.decode("utf-8", errors="replace")[:500])
                    buf.mark_failed(f"OpenAI API 返回 {resp.status_code}: {error_body}")
                    yield ("result", StreamResult.from_buffer(buf, model=model_name))
                    return

                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data: "):
                        continue
                    data_str = line[6:]
                    if data_str.strip() == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    if chunk.get("model"):
                        model_name = chunk["model"]
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    choice = choices[0] if isinstance(choices, list) else {}
                    delta = choice.get("delta") or {}
                    if choice.get("finish_reason"):
                        finish_reason = choice["finish_reason"]

                    # 文本增量
                    text_delta = delta.get("content")
                    if text_delta:
                        buf.append(text_delta)
                        yield ("chunk", text_delta)

                    # tool_calls 增量累积（delta.get 可能返回 None，用 or 兜底）
                    _tool_calls = delta.get("tool_calls") or []
                    for tc_delta in _tool_calls:
                        if not isinstance(tc_delta, dict):
                            continue
                        idx = tc_delta.get("index", 0)
                        if idx not in tool_calls_map:
                            tool_calls_map[idx] = {
                                "id": tc_delta.get("id", ""),
                                "type": "function",
                                "function": {"name": "", "arguments": ""},
                            }
                        tc = tool_calls_map[idx]
                        if tc_delta.get("id"):
                            tc["id"] = tc_delta["id"]
                        fn = tc_delta.get("function") or {}
                        if fn.get("name"):
                            tc["function"]["name"] += fn["name"]
                        if fn.get("arguments"):
                            tc["function"]["arguments"] += fn["arguments"]

        except httpx.TimeoutException:
            buf.mark_failed(f"LLM 请求超时（{self.base_url}）")
            yield ("result", StreamResult.from_buffer(buf, model=model_name))
            return
        except httpx.ConnectError:
            buf.mark_failed(f"无法连接到 LLM 服务: {self.base_url}")
            yield ("result", StreamResult.from_buffer(buf, model=model_name))
            return
        except httpx.HTTPError as e:
            buf.mark_failed(f"LLM 请求失败: {e}")
            yield ("result", StreamResult.from_buffer(buf, model=model_name))
            return
        except (json.JSONDecodeError, UnicodeDecodeError, KeyError, httpx.HTTPError) as e:
            import traceback as _tb
            buf.mark_failed(f"流式响应解析失败: {type(e).__name__}: {e}\n{_tb.format_exc()[-300:]}")
            yield ("result", StreamResult.from_buffer(buf, model=model_name))
            return

        # 正常完成
        buf.mark_complete()
        tool_calls = [tool_calls_map[i] for i in sorted(tool_calls_map.keys())] if tool_calls_map else None
        yield ("result", StreamResult(
            state="complete", content=buf.flush(), tool_calls=tool_calls,
            model=model_name, chunks_received=buf.chunks_received,
        ))


class AnthropicBackend(LLMBackend):
    """Anthropic Claude 后端"""

    def __init__(self, api_key: str, base_url: str = "https://api.anthropic.com", model: str = "claude-sonnet-4-20250514", timeout: int = 120):
        super().__init__(api_key=api_key, base_url=base_url, model=model,
                         timeout=timeout, context_window=200000)

    async def aclose(self):
        """关闭 httpx 客户端"""
        await self.client.aclose()

    async def generate(self, system, messages, temperature=0.3, max_tokens=4096, tools=None,
                       use_cache: bool = True):
        # P1-3: Anthropic 原生 Prompt Caching — 启用缓存时将 system 转为带
        # cache_control 的内容块列表，让服务端缓存 Block A/B，命中时跳过重复计算
        _blocks = self._resolve_prompt_cache(system, use_cache)
        if _blocks is not None:
            system_field = [
                {"type": "text", "text": b["content"],
                 "cache_control": {"type": "ephemeral"}}
                for b in _blocks
            ]
        else:
            system_field = system

        payload = {
            "model": self.model,
            "system": system_field,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = tools

        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        url = f"{self.base_url}/v1/messages"
        resp = await self._post_with_retry(url, payload, headers)
        if resp.status_code != 200:
            error_body = self._sanitize_error(resp.text[:500])
            raise RuntimeError(f"Anthropic API 错误 {resp.status_code}: {error_body}")
        try:
            data = resp.json()
        except json.JSONDecodeError:
            error_body = self._sanitize_error(resp.text[:200])
            raise RuntimeError(f"响应非 JSON: {error_body}")
        if "error" in data:
            err = data["error"]
            err_msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            raise RuntimeError(f"API 返回错误: {self._sanitize_error(err_msg)}")
        content = ""
        tool_calls = []
        for block in data.get("content", []):
            if block.get("type") == "text":
                content += block["text"]
            elif block.get("type") == "tool_use":
                # 解析 Claude 的 tool_use block → OpenAI tool_calls 格式
                args = block.get("input", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                if not isinstance(args, dict):
                    args = {}
                tool_calls.append({
                    "id": block.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": json.dumps(args, ensure_ascii=False),
                    },
                })
        return LLMResponse(
            content=content,
            model=data.get("model", self.model),
            usage=data.get("usage", {}),
            tool_calls=tool_calls if tool_calls else None,
        )

    async def stream_generate(self, system, messages, temperature=0.3, max_tokens=4096, tools=None,
                              use_cache: bool = True):
        """Anthropic 流式响应

        Anthropic SSE 事件类型：
        - message_start: 消息开始
        - content_block_start: 内容块开始（text/tool_use）
        - content_block_delta: 内容块增量（text_delta/input_json_delta）
        - content_block_stop: 内容块结束
        - message_delta: 消息级增量（含 usage）
        - message_stop: 消息结束
        """
        # P1-3: Anthropic 原生 Prompt Caching（与 generate 一致）
        _blocks = self._resolve_prompt_cache(system, use_cache)
        if _blocks is not None:
            system_field = [
                {"type": "text", "text": b["content"],
                 "cache_control": {"type": "ephemeral"}}
                for b in _blocks
            ]
        else:
            system_field = system

        payload = {
            "model": self.model,
            "system": system_field,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        if tools:
            payload["tools"] = tools

        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        url = f"{self.base_url}/v1/messages"

        buf = StreamBuffer()  # 注：原为 content_parts = []
        tool_calls = []
        # 当前正在累积的 tool_use block
        current_tool = None
        model_name = self.model
        usage = {}

        try:
            async with self.client.stream("POST", url, json=payload, headers=headers) as resp:
                if resp.status_code != 200:
                    body = await resp.aread()
                    error_body = self._sanitize_error(body.decode("utf-8", errors="replace")[:500])
                    buf.mark_failed(f"Anthropic API 错误 {resp.status_code}: {error_body}")
                    yield ("result", StreamResult.from_buffer(buf, model=model_name, usage=usage))
                    return

                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data: "):
                        continue
                    data_str = line[6:]
                    try:
                        event = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    event_type = event.get("type", "")

                    if event_type == "message_start":
                        msg = event.get("message") or {}
                        if msg.get("model"):
                            model_name = msg["model"]
                        usage = msg.get("usage") or {}

                    elif event_type == "content_block_start":
                        block = event.get("content_block") or {}
                        if block.get("type") == "tool_use":
                            current_tool = {
                                "id": block.get("id", ""),
                                "type": "function",
                                "function": {
                                    "name": block.get("name", ""),
                                    "arguments": "",
                                },
                            }

                    elif event_type == "content_block_delta":
                        delta = event.get("delta") or {}
                        if delta.get("type") == "text_delta":
                            text = delta.get("text", "")
                            if text:
                                buf.append(text)
                                yield ("chunk", text)
                        elif delta.get("type") == "input_json_delta" and current_tool:
                            current_tool["function"]["arguments"] += delta.get("partial_json", "")

                    elif event_type == "content_block_stop":
                        if current_tool:
                            tool_calls.append(current_tool)
                            current_tool = None

                    elif event_type == "message_delta":
                        delta = event.get("delta", {})
                        if "usage" in event:
                            usage.update(event["usage"])

                    elif event_type == "message_stop":
                        break

        except httpx.TimeoutException:
            buf.mark_failed(f"LLM 请求超时（{self.base_url}）")
            yield ("result", StreamResult.from_buffer(buf, model=model_name, usage=usage))
            return
        except httpx.ConnectError:
            buf.mark_failed(f"无法连接到 LLM 服务: {self.base_url}")
            yield ("result", StreamResult.from_buffer(buf, model=model_name, usage=usage))
            return
        except httpx.HTTPError as e:
            buf.mark_failed(f"LLM 请求失败: {e}")
            yield ("result", StreamResult.from_buffer(buf, model=model_name, usage=usage))
            return
        except (json.JSONDecodeError, UnicodeDecodeError, KeyError, httpx.HTTPError) as e:
            buf.mark_failed(f"流式响应解析失败: {e}")
            yield ("result", StreamResult.from_buffer(buf, model=model_name, usage=usage))
            return

        # 正常完成（HC-4：current_tool 在 content_block_stop 未到达时不入 tool_calls，已自动丢弃）
        buf.mark_complete()
        yield ("result", StreamResult(
            state="complete", content=buf.flush(),
            tool_calls=tool_calls if tool_calls else None,
            model=model_name, usage=usage, chunks_received=buf.chunks_received,
        ))


class OllamaBackend(LLMBackend):
    """Ollama 本地后端"""

    def __init__(self, base_url: str = "http://localhost:11434", model: str = "qwen2.5-coder:7b", timeout: int = 300):
        super().__init__(api_key="", base_url=base_url, model=model,
                         timeout=timeout, context_window=8000)

    async def aclose(self):
        """关闭 httpx 客户端"""
        await self.client.aclose()

    async def generate(self, system, messages, temperature=0.3, max_tokens=4096, tools=None):
        full_messages = [{"role": "system", "content": system}] + messages
        payload = {"model": self.model, "messages": full_messages, "stream": False,
                   "options": {"temperature": temperature, "num_predict": max_tokens}}
        # Ollama 支持 tools 参数（0.3.0+），传入工具定义
        if tools:
            payload["tools"] = tools
        url = f"{self.base_url}/api/chat"
        resp = await self._post_with_retry(url, payload, {})
        if resp.status_code != 200:
            error_body = self._sanitize_error(resp.text[:500])
            raise RuntimeError(f"Ollama API 错误 {resp.status_code}: {error_body}")
        try:
            data = resp.json()
        except json.JSONDecodeError:
            error_body = self._sanitize_error(resp.text[:200])
            raise RuntimeError(f"响应非 JSON: {error_body}")

        if "error" in data:
            raise RuntimeError(f"Ollama 错误: {self._sanitize_error(str(data['error']))}")

        # 解析工具调用（统一为 OpenAI 标准 tool_calls 格式）
        # Ollama 格式: {"message": {"tool_calls": [{"function": {"name": "...", "arguments": "..."}}]}}
        # arguments 可能是 JSON 字符串或 dict
        tool_calls = None
        if data.get("message", {}).get("tool_calls"):
            tool_calls = []
            for tc in data["message"]["tool_calls"]:
                fn = tc.get("function", {})
                name = fn.get("name", "")
                args = fn.get("arguments", {})
                # arguments 可能是 JSON 字符串，需要解析
                if isinstance(args, str):
                    try:
                        args = json.loads(args) if args else {}
                    except json.JSONDecodeError:
                        args = {"_raw": args}
                if not isinstance(args, dict):
                    logger.warning("Ollama tool_calls arguments 非 dict，已丢弃: %s", args)
                    args = {}
                tool_calls.append({
                    "id": tc.get("id", f"call_{len(tool_calls)}"),
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(args, ensure_ascii=False),
                    },
                })

        msg = data.get("message", {})
        content = msg.get("content", "")
        return LLMResponse(
            content=content,
            model=self.model,
            tool_calls=tool_calls,
        )

    async def stream_generate(self, system, messages, temperature=0.3, max_tokens=4096, tools=None):
        """Ollama 流式响应

        Ollama 用 NDJSON 流（每行一个 JSON 对象），非 SSE。
        每行包含 {"message": {"content": "..."}, "done": false/true}。
        """
        full_messages = [{"role": "system", "content": system}] + messages
        payload = {"model": self.model, "messages": full_messages, "stream": True,
                   "options": {"temperature": temperature, "num_predict": max_tokens}}
        if tools:
            payload["tools"] = tools
        url = f"{self.base_url}/api/chat"

        buf = StreamBuffer()
        tool_calls = []
        model_name = self.model

        try:
            async with self.client.stream("POST", url, json=payload) as resp:
                if resp.status_code != 200:
                    body = await resp.aread()
                    error_body = self._sanitize_error(body.decode("utf-8", errors="replace")[:500])
                    buf.mark_failed(f"Ollama API 错误 {resp.status_code}: {error_body}")
                    yield ("result", StreamResult.from_buffer(buf, model=model_name))
                    return

                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    if "error" in chunk:
                        buf.mark_failed(f"Ollama 错误: {self._sanitize_error(str(chunk['error']))}")
                        yield ("result", StreamResult.from_buffer(buf, model=model_name))
                        return

                    msg = chunk.get("message") or {}
                    text_delta = msg.get("content", "")
                    if text_delta:
                        buf.append(text_delta)
                        yield ("chunk", text_delta)

                    # 累积 tool_calls（Ollama 在 done=true 时返回完整 tool_calls）
                    if msg.get("tool_calls"):
                        for tc in msg["tool_calls"]:
                            fn = tc.get("function") or {}
                            name = fn.get("name", "")
                            args = fn.get("arguments", {})
                            if isinstance(args, str):
                                try:
                                    args = json.loads(args) if args else {}
                                except json.JSONDecodeError:
                                    args = {}
                            if not isinstance(args, dict):
                                args = {}
                            tool_calls.append({
                                "id": tc.get("id", f"call_{len(tool_calls)}"),
                                "type": "function",
                                "function": {
                                    "name": name,
                                    "arguments": json.dumps(args, ensure_ascii=False),
                                },
                            })

                    if chunk.get("done", False):
                        break

        except httpx.TimeoutException:
            buf.mark_failed(f"LLM 请求超时（{self.base_url}）")
            yield ("result", StreamResult.from_buffer(buf, model=model_name))
            return
        except httpx.ConnectError:
            buf.mark_failed(f"无法连接到 LLM 服务: {self.base_url}")
            yield ("result", StreamResult.from_buffer(buf, model=model_name))
            return
        except httpx.HTTPError as e:
            buf.mark_failed(f"LLM 请求失败: {e}")
            yield ("result", StreamResult.from_buffer(buf, model=model_name))
            return
        except (json.JSONDecodeError, UnicodeDecodeError, KeyError, httpx.HTTPError) as e:
            buf.mark_failed(f"流式响应解析失败: {e}")
            yield ("result", StreamResult.from_buffer(buf, model=model_name))
            return

        # 正常完成
        buf.mark_complete()
        yield ("result", StreamResult(
            state="complete", content=buf.flush(),
            tool_calls=tool_calls if tool_calls else None,
            model=model_name, chunks_received=buf.chunks_received,
        ))


class EchoBackend(LLMBackend):
    """Echo 后端 — 用于测试，返回模板代码或工具调用"""

    async def generate(self, system, messages, temperature=0.3, max_tokens=4096, tools=None):
        user_msg = messages[-1].get("content", "") if messages else ""
        user_lower = user_msg.lower()

        # 如果提供了工具定义，返回工具调用
        if tools:
            if any(kw in user_lower for kw in ECHO_COMPILE_KEYWORDS):
                # 编译类：扫描文件后编译
                return LLMResponse(
                    content="",
                    model="echo",
                    tool_calls=[{
                        "id": "echo_1",
                        "type": "function",
                        "function": {
                            "name": "run_command",
                            "arguments": json.dumps({"command": "gcc main.c -o main.exe"}),
                        },
                    }],
                )
            elif any(kw in user_lower for kw in ECHO_CHAT_KEYWORDS):
                # 闲聊
                return LLMResponse(
                    content="",
                    model="echo",
                    tool_calls=[{
                        "id": "echo_1",
                        "type": "function",
                        "function": {
                            "name": "chat",
                            "arguments": json.dumps({"message": f"你好！我是 Iron 嵌入式开发助手。你说了: {user_msg[:50]}"}),
                        },
                    }],
                )
            else:
                # 默认：写代码
                code = (
                    f"#include <stdio.h>\n\n"
                    f"int main() {{\n"
                    f'    printf("Hello, World!\\n");\n'
                    f"    return 0;\n"
                    f"}}\n"
                )
                return LLMResponse(
                    content="",
                    model="echo",
                    tool_calls=[{
                        "id": "echo_1",
                        "type": "function",
                        "function": {
                            "name": "write_file",
                            "arguments": json.dumps({"path": "main.c", "content": code}),
                        },
                    }],
                )

        # 没有工具定义，返回文本
        nl = "\n"
        return LLMResponse(
            content=(
                f"// Echo 模式 — AI 生成的占位代码{nl}"
                f"// 用户请求: {user_msg[:100]}{nl}{nl}"
                f"#include <stdint.h>{nl}{nl}"
                f"int main(void) {chr(123)}{nl}"
                f"    // TODO: 实现功能{nl}"
                f"    while(1) {chr(123)}{chr(125)}{nl}"
                f"    return 0;{nl}"
                f"{chr(125)}{nl}"
            ),
            model="echo",
        )

    async def embed(self, texts: list[str], model: str = None) -> list[list[float]]:
        """Echo 模式 embedding — 用哈希伪向量（测试用）

        生成固定维度的伪向量，基于文本哈希，让测试可验证语义搜索流程。
        维度：64（远小于真实 embedding，但够测试用）
        """
        import hashlib
        dim = 64
        results = []
        for text in texts:
            # 用 SHA256 哈希生成伪向量
            h = hashlib.sha256(text.encode("utf-8")).digest()
            # 扩展到 dim 维度
            vec = []
            for i in range(dim):
                byte_val = h[i % len(h)]
                # 归一化到 [-1, 1]
                vec.append((byte_val / 127.5) - 1.0)
            results.append(vec)
        return results


def create_backend(backend_name: str, config) -> LLMBackend:
    """工厂方法：根据配置创建 LLM 后端"""
    _timeout = getattr(config.llm, "request_timeout", 120)
    if backend_name == "openai":
        return OpenAIBackend(
            api_key=config.llm.api_key,
            base_url=config.llm.base_url or "https://api.openai.com/v1",
            model=config.llm.model,
            timeout=_timeout,
        )
    elif backend_name == "anthropic":
        return AnthropicBackend(
            api_key=config.llm.api_key,
            base_url=config.llm.base_url or "https://api.anthropic.com",
            model=config.llm.model,
            timeout=_timeout,
        )
    elif backend_name == "ollama":
        return OllamaBackend(
            base_url=config.llm.base_url or "http://localhost:11434",
            model=config.llm.model,
            timeout=max(_timeout, 300),
        )
    elif backend_name == "echo":
        return EchoBackend()
    else:
        raise ValueError(f"未知的 LLM 后端: {backend_name}")
