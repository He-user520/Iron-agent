# Track 3 · 流式中断恢复 chunk_buffer 子计划

> **For agentic workers:** 本计划基于实际阅读 `iron/llm/backend.py`（934 行）与 `iron/agent/engine.py` line 623-712 的源码生成。所有行号均对应当前工作区快照，实施前请用 `git tag pre-stream-recovery` 锁定基线。步骤使用 `- [ ]` 复选框语法跟踪。

**Goal:** 流式响应中断时保留已接收 chunk，不重发请求，避免 token 双重消耗；KeyboardInterrupt/异常时 flush 缓存内容防止数据丢失。

**Architecture:** 在 `backend.py` 流式迭代器中引入 `StreamBuffer` 累积 chunk，中断时返回 `StreamResult`（complete/partial/failed 三态）而非抛异常；`engine.py` 根据三态决策——partial 不重发、failed 才 fallback 到非流式；新增 `stream_partial` AgentEvent 通知 UI。

**Tech Stack:** Python 3.11+ dataclass、httpx async streaming、pytest asyncio。

---

## 1. 目标与约束

### 1.1 目标
- 流式中断（连接断开/超时/JSON 解析失败/KeyboardInterrupt）时，保留已接收的 chunk 内容，构造 partial 响应继续后续流程
- 用统一的 `StreamBuffer` 数据结构替代当前散落在 `engine.py:643` 的 `_accumulated_chunks = []` 临时列表
- 用 `StreamResult` 三态（complete / partial / failed）替代当前 `resp is None` 的二态判断，使决策边界显式化
- 新增 `stream_partial` AgentEvent，让 UI 显式提示"已收到 N 字符，可能不完整"

### 1.2 硬约束（来自 project_memory.md）
- **HC-1**：Streaming fallback must not resend requests if partial chunks have been received to avoid double token consumption
  - 即：只要 `_stream_chunks_received == True`，禁止调用 `self.llm.generate(...)`（非流式重发）
- **HC-2**：Streaming buffer must flush cached content on KeyboardInterrupt or exceptions to prevent data loss
  - 即：流式循环必须有 `try/finally`，`finally` 中 flush 缓存内容到 partial 响应
- **HC-3**：完全失败（0 chunk）仍允许 fallback 到非流式
  - 即：`chunks_received == 0` 时，保留原 fallback 逻辑
- **HC-4**：不完整的工具调用 JSON 不能传给 `_parse_tool_calls`
  - 即：partial 响应中的 `tool_calls` 若 JSON 不完整，必须标记为不可执行（跳过 `_parse_tool_calls` 的执行路径）

### 1.3 非目标
- 不改造 `CircuitBreaker` 与流式的交互（streaming 绕过 `_post_with_retry` 是已知 gap，留待 Track 4）
- 不改造 `prompt_cache` 缓存逻辑
- 不改造 `ContextCompactor` 压缩管道

---

## 2. 现状分析

### 2.1 backend.py 流式实现现状

#### 2.1.1 LLMBackend 抽象基类

| 元素 | 行号 | 说明 |
|------|------|------|
| `LLMResponse` dataclass | `backend.py:18-29` | 含 `content / model / usage / tool_calls`，无 partial 标记字段 |
| `CircuitBreaker` | `backend.py:32-73` | 仅在 `_post_with_retry` 中调用，streaming 路径绕过 |
| `LLMBackend.__init__` | `backend.py:83-107` | 持有 `_circuit / api_key / base_url / model / client / context_window / prompt_cache` |
| `_post_with_retry` | `backend.py:148-199` | 非流式 POST，带重试+熔断；streaming 不走此路径 |
| `stream_generate` 默认实现 | `backend.py:252-277` | fallback 到 `generate`，先 yield `("chunk", resp.content)` 再 yield `("response", resp)`；异常 yield `("error", str(e))` |

**关键签名（`backend.py:252-259`）：**
```python
async def stream_generate(
    self, system: str, messages: list[dict],
    temperature: float = 0.3, max_tokens: int = 4096,
    tools: list = None,
) -> AsyncGenerator[tuple, None]:
```

事件协议（当前二元组 `(event_type, event_data)`）：
- `("chunk", str)` — 文本增量
- `("response", LLMResponse)` — 完整响应（必选，成功时）
- `("error", str)` — 错误信息

**问题**：当前协议无法区分"0 chunk 的失败"与"已收到 N chunk 的中断"——两者都只 yield `("error", str)`，调用方 `engine.py` 必须自行维护 `_stream_chunks_received` 标志位（见 2.2）。

#### 2.1.2 OpenAIBackend.stream_generate（`backend.py:352-458`）

- line 378-382：本地累积变量 `content_parts = []`、`tool_calls_map = {}`、`model_name`、`finish_reason`
- line 384：`async with self.client.stream("POST", url, ...) as resp:` 直接发请求，**不经 `_post_with_retry`，不经熔断器**
- line 391-436：`async for line in resp.aiter_lines()` 解析 SSE
  - line 413-415：文本增量 → `content_parts.append(text_delta)` + `yield ("chunk", text_delta)`
  - line 417-436：tool_calls 增量累积到 `tool_calls_map`
- line 438-450：异常处理，**所有异常都 `yield ("error", ...) + return`，丢弃 `content_parts` 中已累积的内容**
  ```python
  except httpx.TimeoutException:
      yield ("error", f"LLM 请求超时（{self.base_url}）")
      return
  ```
- line 452-458：正常结束 yield `("response", LLMResponse(...))`

**违反点**：line 438-450 的 4 个 except 分支直接 return，已累积的 `content_parts`（可能含数百字符）被丢弃，违反 HC-2。

#### 2.1.3 AnthropicBackend.stream_generate（`backend.py:545-672`）

- line 586-591：本地累积 `content_parts = []`、`tool_calls = []`、`current_tool = None`、`model_name`、`usage = {}`
- line 594：`async with self.client.stream("POST", url, ...) as resp:` 同样绕过熔断器
- line 601-651：SSE 事件循环
  - line 630-638：`content_block_delta` → text_delta 累积 + yield chunk；input_json_delta 累积到 `current_tool["function"]["arguments"]`
  - line 640-643：`content_block_stop` → tool_calls.append(current_tool)
- line 653-664：异常处理，同样 `yield ("error", ...) + return`，丢弃 `content_parts`
- line 666-672：正常结束 yield response

**特殊点**：Anthropic 的 tool_use 是按 block 累积的，`content_block_stop` 事件未到达时 `current_tool` 不入 `tool_calls`，中断时 `current_tool` 中的 partial JSON 直接丢失。

#### 2.1.4 OllamaBackend.stream_generate（`backend.py:743-829`）

- line 756-758：本地累积 `content_parts = []`、`tool_calls = []`、`model_name`
- line 761：`async with self.client.stream("POST", url, ...) as resp:` 绕过熔断器
- line 768-809：NDJSON 流解析（非 SSE）
  - line 781-784：content 增量累积 + yield chunk
  - line 787-806：tool_calls 累积（Ollama 在 `done=true` 时返回完整 tool_calls，中断时通常无 tool_calls）
- line 811-822：异常处理，同模式 `yield ("error", ...) + return`
- line 824-829：正常结束 yield response

#### 2.1.5 EchoBackend（`backend.py:832-905`）

- 未覆盖 `stream_generate`，使用基类默认实现（`backend.py:252-277`）——即 fallback 到 `generate`，单次 yield chunk + response。
- 测试用，本 Track 不改造。

#### 2.1.6 Circuit Breaker 与流式的交互现状

| 路径 | 是否经熔断器 | 是否经重试 |
|------|--------------|------------|
| `generate`（4 个后端） | 是（经 `_post_with_retry`） | 是（`_MAX_RETRIES=3`） |
| `stream_generate`（4 个后端） | **否**（直接 `self.client.stream`） | **否** |

**结论**：流式失败既不记录熔断失败、也不重试。本 Track 不修复此 gap（留待 Track 4），但 `StreamBuffer` 的 `failure_reason` 字段为后续修复预留接口。

---

### 2.2 engine.py fallback 逻辑现状

#### 2.2.1 流式处理代码块（`engine.py:640-698`）

```python
# engine.py:640-643
resp = None
_stream_error = None
_stream_chunks_received = False
_accumulated_chunks = []  # 累积 chunk 内容，流式中断时用于恢复
try:
    # engine.py:645-670
    if hasattr(self.llm, "stream_generate"):
        async for event_type, event_data in self.llm.stream_generate(
            system, messages, temperature=0.2, max_tokens=4096, tools=_effective_tools,
        ):
            if event_type == "chunk":
                _stream_chunks_received = True
                _accumulated_chunks.append(event_data)
                yield await self._emit_event("chat_chunk", {"text": event_data})  # line 656
            elif event_type == "response":
                resp = event_data
            elif event_type == "error":
                _stream_error = event_data
                break
        if resp is None and _stream_error is None:
            _stream_error = "流式响应未返回完整结果"
    else:
        resp = await self.llm.generate(...)  # line 665-670
except asyncio.CancelledError:
    raise
except (RuntimeError, OSError, httpx.HTTPError) as e:
    _stream_error = str(e)

# engine.py:676-698 fallback 逻辑
if resp is None:
    if _stream_chunks_received:
        # 已有部分恢复：line 679-684
        partial_content = "".join(_accumulated_chunks)
        yield await self._emit_event("thinking", {"message": "流式响应不完整，使用已接收内容继续"})
        resp = LLMResponse(content=partial_content, model="", tool_calls=None)
    elif _stream_error:
        # fallback 到非流式：line 685-698
        yield await self._emit_event("thinking", {"message": "流式响应失败，切换到非流式模式..."})
        try:
            resp = await self.llm.generate(...)  # ⚠️ 重发请求
        except asyncio.CancelledError:
            raise
        except (RuntimeError, OSError, httpx.HTTPError) as e:
            yield await self._emit_event("error", {"message": f"AI 请求失败: {e}"})
            return
```

#### 2.2.2 AgentEvent yield 顺序（当前）

正常流式成功路径：
1. `thinking`（line 633/638）— "思考中"
2. `phase` THINK（line 639）
3. `chat_chunk` × N（line 656）— 每个文本增量
4. `phase` EXECUTE（line 713）或 `phase` CHAT + `chat_response`（line 707-708）

流式中断（已收 chunk）路径（当前）：
1. `thinking` + `phase` THINK
2. `chat_chunk` × N
3. （`error` 事件或异常）
4. `thinking`（line 683）— "流式响应不完整，使用已接收内容继续"
5. 后续走 `_parse_tool_calls` → 可能 `phase` CHAT + `chat_response`

流式完全失败（0 chunk）路径（当前）：
1. `thinking` + `phase` THINK
2. （`error` 事件或异常）
3. `thinking`（line 686）— "流式响应失败，切换到非流式模式..."
4. `chat_chunk` × N（非流式 generate 的完整内容）
5. 后续正常

#### 2.2.3 重发请求的触发条件

当前 `engine.py:685-698` 的 `self.llm.generate(...)` 重发仅在以下条件全部满足时触发：
- `resp is None`（流式未返回 response 事件）
- `not _stream_chunks_received`（一个 chunk 都没收到）
- `_stream_error` 非空（有错误信息）

**表面看 HC-1 已满足**（有 chunk 就不重发）。但存在以下漏洞：

---

### 2.3 问题诊断

#### 2.3.1 何时触发 fallback（当前实际行为）

| 中断场景 | `_stream_chunks_received` | 当前走哪条分支 | 是否重发 | 是否违反 HC |
|----------|---------------------------|----------------|----------|-------------|
| 连接断开（`httpx.ConnectError`），0 chunk | False | line 685 elif | 是 | 否（HC-3 允许） |
| 连接断开，已收 10 chunk | True | line 679 if | 否 | 否 |
| 超时（`httpx.TimeoutException`），0 chunk | False | line 685 elif | 是 | 否 |
| 超时，已收 10 chunk | True | line 679 if | 否 | 否 |
| SSE JSON 解析失败，0 chunk | False | line 685 elif | 是 | 否 |
| SSE JSON 解析失败，已收 10 chunk | True | line 679 if | 否 | 否 |
| **KeyboardInterrupt**，已收 10 chunk | True | **未捕获，异常上抛** | — | **违反 HC-2** |
| **asyncio.CancelledError**，已收 10 chunk | True | line 671-672 `raise`，缓存丢弃 | — | **违反 HC-2** |
| `("error", ...)` 事件，已收 5 chunk | True | line 679 if | 否 | 否 |

#### 2.3.2 重发请求的 token 消耗估算

完全失败（0 chunk）场景下的重发是 HC-3 允许的，但需量化：
- 输入 token = `system + messages` 的 token 数（由 `_estimate_input_tokens` 估算，`engine.py:631`）
- 输出 token = 非流式 `generate` 的完整输出（`max_tokens=4096` 上限）
- 单次重发成本 = 输入 token + 输出 token（按各平台计费）
- **双重消耗场景**：若流式已消耗 N 个输出 token 后中断，当前代码在 0 chunk 时重发，浪费 = N（已消耗）+ 完整输出（重发）。但 0 chunk 意味着 N=0，无浪费。**真正的浪费只在"有 chunk 但仍重发"时发生——当前代码已避免此情况，但缺乏显式保障（见 2.3.3）。**

#### 2.3.3 现有代码违反硬约束的具体位置

| 硬约束 | 违反位置 | 违反描述 |
|--------|----------|----------|
| HC-1（不重发） | `engine.py:685-698` | 当前逻辑依赖 `_stream_chunks_received` 布尔，但该标志在 `except` 块（line 673）中可能未正确设置——若异常发生在 `async for` 迭代器构造阶段（`stream_generate` 协程进入前），`_stream_chunks_received` 仍为 False，但实际可能已建立连接消耗了输入 token。需用 `StreamBuffer` 在 backend 层显式记录。 |
| HC-2（flush on KeyboardInterrupt） | `engine.py:671-672` | 只捕获 `asyncio.CancelledError` 和 `(RuntimeError, OSError, httpx.HTTPError)`，**未捕获 `KeyboardInterrupt`**。KeyboardInterrupt 会绕过 `try/except`，`_accumulated_chunks` 直接丢失。 |
| HC-2（flush on exception） | `backend.py:438-450 / 653-664 / 811-822` | 4 个后端的 stream 异常处理均 `yield ("error", ...) + return`，**丢弃 `content_parts`**。已累积内容未随 error 事件传出。 |
| HC-4（不完整 JSON 不传 _parse_tool_calls） | `engine.py:701` | `tool_calls = self._parse_tool_calls(resp)` 对 partial resp 无条件调用。`_parse_tool_calls`（`engine.py:1583-1608`）对 `tool_calls` 中的 `JSONDecodeError` 静默设 `args={}`（line 1599-1600），**会执行参数为空的工具调用**而非跳过。 |

---

## 3. 设计方案

### 3.1 chunk_buffer 数据结构

#### 3.1.1 StreamBuffer（在 backend.py 中累积 chunk）

```python
# 放置位置：iron/llm/backend.py 顶部（LLMResponse 之后，CircuitBreaker 之前）
from dataclasses import dataclass, field

@dataclass
class StreamBuffer:
    """流式响应累积缓冲区

    在 stream_generate 迭代器内部累积 chunk，中断时 flush 保留已接收内容。
    满足 HC-2：KeyboardInterrupt/异常时通过 flush() 防止数据丢失。
    """
    chunks: list[str] = field(default_factory=list)
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
        """是否已收到部分内容但不完整

        三态判定核心：
        - False + chunks_received==0 + not is_complete → failed（可 fallback）
        - True  + chunks_received>0  + not is_complete → partial（不重发）
        - False + is_complete → complete（正常完成）
        """
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
```

**方法数**：7 个（`append / flush / is_partial / is_empty / mark_complete / mark_failed / __len__`）+ 5 个字段（`chunks / accumulated_text / is_complete / failure_reason / chunks_received`）。

#### 3.1.2 StreamResult（流式返回值，区分三态）

```python
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
        - buf.is_partial  → state="partial"
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
```

### 3.2 backend.py 改造方案

#### 3.2.1 协议变更（向后兼容）

**新协议**（终止事件用 `StreamResult`）：
- `("chunk", str)` — 文本增量（不变）
- `("result", StreamResult)` — 终止事件，替代 `("response", LLMResponse)` 和 `("error", str)`

**向后兼容**：`engine.py` 同时识别新旧协议——收到 `("response", ...)` 或 `("error", ...)` 时按旧逻辑处理（过渡期），收到 `("result", StreamResult)` 时走三态决策。本 Track 完成后可在后续清理移除旧协议。

#### 3.2.2 OpenAIBackend.stream_generate 改造（`backend.py:352-458`）

**改造点 1**：初始化 `StreamBuffer`（line 378 附近）
```python
buf = StreamBuffer()  # 替代 content_parts = []
tool_calls_map = {}
model_name = self.model
finish_reason = None
```

**改造点 2**：chunk 累积改用 `buf.append`（line 413-415）
```python
text_delta = delta.get("content")
if text_delta:
    buf.append(text_delta)
    yield ("chunk", text_delta)
```

**改造点 3**：异常处理改为 flush + partial（line 438-450）
```python
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
except (json.JSONDecodeError, UnicodeDecodeError, KeyError) as e:
    buf.mark_failed(f"流式响应解析失败: {type(e).__name__}: {e}")
    yield ("result", StreamResult.from_buffer(buf, model=model_name))
    return
```

**改造点 4**：正常结束改为 `mark_complete` + `StreamResult`（line 452-458）
```python
buf.mark_complete()
tool_calls = [tool_calls_map[i] for i in sorted(tool_calls_map.keys())] if tool_calls_map else None
yield ("result", StreamResult(
    state="complete", content=buf.flush(), tool_calls=tool_calls,
    model=model_name, chunks_received=buf.chunks_received,
))
```

**改造点 5**：`KeyboardInterrupt` flush（用 `try/finally` 包裹整个流式块）
```python
try:
    async with self.client.stream("POST", url, json=payload, headers=headers) as resp:
        # ... 现有 SSE 解析 ...
finally:
    # HC-2：任何未捕获异常（含 KeyboardInterrupt）确保 flush
    # 若已 mark_complete 或已 yield result，此处 no-op
    # 若异常未走 except（如 KeyboardInterrupt），buf.is_partial() 为 True 且未 yield result
    # 由 engine.py 的 finally 兜底处理（见 3.3.3）
    pass
```
> 注：`KeyboardInterrupt` 在 asyncio 事件循环中通常转化为 `CancelledError`，但显式 `try/finally` 是 HC-2 的双重保险。backend 层不强制 flush 上抛（避免在 finally 中 yield），由 engine.py 的 finally 统一 flush。

#### 3.2.3 AnthropicBackend / OllamaBackend 改造

同 3.2.2 模式，分别在 `backend.py:545-672`（Anthropic）和 `backend.py:743-829`（Ollama）应用：
- 初始化 `buf = StreamBuffer()`
- `content_parts.append(text)` → `buf.append(text)`
- 4 个 except 分支改为 `buf.mark_failed(...) + yield ("result", StreamResult.from_buffer(buf, ...))`
- 正常结束 `buf.mark_complete() + yield ("result", StreamResult(state="complete", ...))`

**Anthropic 特殊处理**（line 640-643）：`content_block_stop` 未到达时 `current_tool` 含 partial JSON，中断时**不**加入 `tool_calls`（HC-4），仅丢弃 partial tool call，保留 `content_parts` 文本。

#### 3.2.4 LLMBackend.stream_generate 默认实现改造（`backend.py:252-277`）

基类默认实现（EchoBackend 使用）也改为 `StreamResult`：
```python
async def stream_generate(self, system, messages, ...):
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
```

### 3.3 engine.py fallback 策略调整

> ⚠️ **依赖 Track 1**：本节的 fallback 逻辑位于 `process` 函数 line 623-712，Track 1 将其提取为 `_handle_thinking_phase`。本 Track 的 Step 4 必须在 Track 1 合并后 rebase，否则冲突。

#### 3.3.1 三态决策表

| StreamResult.state | chunks_received | 行为 | 是否重发 | AgentEvent |
|--------------------|-----------------|------|----------|------------|
| `complete` | ≥0 | 用 `result.content` + `result.tool_calls` 构造 `LLMResponse` | 否 | 无额外事件 |
| `partial` | >0 | 用 `result.content` 构造 `LLMResponse(content=..., tool_calls=None)` | **否（HC-1）** | `stream_partial` |
| `failed` | 0 | fallback 到 `self.llm.generate(...)`（HC-3 允许） | 是 | `thinking`（现有） |

#### 3.3.2 _handle_thinking_phase 改造（Track 1 提取后）

```python
async def _handle_thinking_phase(self, system, messages, _effective_tools, step, _input_tokens):
    """思考阶段：流式生成 + 三态恢复（Track 1 提取，Track 3 改造）"""
    yield await self._emit_event("thinking", {...})
    yield await self._emit_event("phase", {"phase": Phase.THINK.value})

    buf = StreamBuffer()  # engine 层也持有一份，用于 finally 兜底
    stream_result: StreamResult | None = None
    resp: LLMResponse | None = None

    try:
        async for event_type, event_data in self.llm.stream_generate(
            system, messages, temperature=0.2, max_tokens=4096, tools=_effective_tools,
        ):
            if event_type == "chunk":
                buf.append(event_data)
                yield await self._emit_event("chat_chunk", {"text": event_data})
            elif event_type == "result":
                stream_result = event_data  # 新协议
            elif event_type == "response":  # 旧协议兼容
                resp = event_data
                stream_result = StreamResult(
                    state="complete", content=resp.content, tool_calls=resp.tool_calls,
                    model=resp.model, usage=resp.usage or {}, chunks_received=buf.chunks_received,
                )
            elif event_type == "error":  # 旧协议兼容
                stream_result = StreamResult.from_buffer(buf)
                stream_result.error = event_data
    except asyncio.CancelledError:
        # HC-2：CancelledError 时 flush
        if buf.is_partial():
            yield await self._emit_event("stream_partial", {
                "message": "流式响应被取消，已保留已接收内容",
                "chunks_received": buf.chunks_received,
                "content_len": len(buf.flush()),
            })
            resp = LLMResponse(content=buf.flush(), model="", tool_calls=None)
        raise  # CancelledError 仍需上抛
    except KeyboardInterrupt:
        # HC-2：KeyboardInterrupt 时 flush（不重发）
        if buf.is_partial():
            yield await self._emit_event("stream_partial", {
                "message": "用户中断流式响应，已保留已接收内容",
                "chunks_received": buf.chunks_received,
                "content_len": len(buf.flush()),
            })
            resp = LLMResponse(content=buf.flush(), model="", tool_calls=None)
        raise  # KeyboardInterrupt 上抛给 main.py 处理
    except (RuntimeError, OSError, httpx.HTTPError) as e:
        # 异常时 flush（HC-2）
        if buf.is_partial():
            yield await self._emit_event("stream_partial", {
                "message": f"流式响应异常，已保留已接收内容: {e}",
                "chunks_received": buf.chunks_received,
                "content_len": len(buf.flush()),
            })
            resp = LLMResponse(content=buf.flush(), model="", tool_calls=None)
        else:
            stream_result = StreamResult(state="failed", error=str(e))

    # 三态决策
    if resp is None and stream_result is not None:
        if stream_result.is_complete:
            resp = LLMResponse(
                content=stream_result.content, model=stream_result.model,
                usage=stream_result.usage, tool_calls=stream_result.tool_calls,
            )
        elif stream_result.is_partial:
            # HC-1：partial 不重发
            yield await self._emit_event("stream_partial", {
                "message": f"流式响应不完整，使用已接收的 {stream_result.chunks_received} 个 chunk 继续",
                "chunks_received": stream_result.chunks_received,
                "content_len": len(stream_result.content),
            })
            resp = LLMResponse(content=stream_result.content, model=stream_result.model, tool_calls=None)
        elif stream_result.is_failed:
            # HC-3：failed（0 chunk）才 fallback
            yield await self._emit_event("thinking", {"message": "流式响应失败，切换到非流式模式..."})
            try:
                resp = await self.llm.generate(
                    system, messages, temperature=0.2, max_tokens=4096, tools=_effective_tools,
                )
            except asyncio.CancelledError:
                raise
            except (RuntimeError, OSError, httpx.HTTPError) as e:
                yield await self._emit_event("error", {"message": f"AI 请求失败: {e}"})
                return None

    if resp is None:
        # 兜底：理论上不应到达
        yield await self._emit_event("error", {"message": "流式响应未返回任何结果"})
        return None

    return resp  # 返回给 process 主循环，后续走 _parse_tool_calls
```

#### 3.3.3 KeyboardInterrupt / 异常 flush 策略

`_handle_thinking_phase` 的 `try` 块覆盖整个流式循环，3 个 except 分支（CancelledError / KeyboardInterrupt / RuntimeError+OSError+HTTPError）均检查 `buf.is_partial()`：
- partial → yield `stream_partial` + 构造 partial `LLMResponse`（**不重发**，HC-1）
- empty → 走 `stream_result = StreamResult(state="failed", ...)` 让三态决策 fallback（HC-3）

CancelledError 和 KeyboardInterrupt 在 flush 后仍 `raise` 上抛，由 `main.py` 决定是否终止会话。

#### 3.3.4 新增 stream_partial / stream_interrupted AgentEvent

在 `engine_events.py:36` 的 `AgentEvent` 文档注释中增加：
- `stream_partial`: 流式响应不完整，已保留已接收内容（含 `chunks_received / content_len / message`）

`data` 字段约定：
```python
{
    "message": str,           # 人类可读说明
    "chunks_received": int,   # 已接收 chunk 数
    "content_len": int,       # flush 后内容字符数
}
```

`main.py:1166` 附近的 UI 事件分发增加：
```python
elif etype == "stream_partial":
    # 显示黄色警告：[流式不完整] 已接收 {chunks_received} 个 chunk（{content_len} 字符）
    console.print(f"[yellow][流式不完整] {data.get('message')}[/yellow]")
```

### 3.4 partial 响应解析

#### 3.4.1 工具调用 JSON 不完整的处理（HC-4）

`_parse_tool_calls`（`engine.py:1583-1608`）当前对 `resp.tool_calls` 中的 `JSONDecodeError` 静默设 `args={}`（line 1599-1600），会执行参数为空的工具调用。

**改造方案**：partial 响应在 `_handle_thinking_phase` 中已设 `tool_calls=None`（见 3.3.2），不会进入 `_parse_tool_calls` 的标准 tool_calls 分支。但若 partial 文本中含 markdown JSON 代码块（line 1610-1627 的兼容模式），仍可能解析出"看似有效但语义不完整"的工具调用。

**保护措施**：在 `_handle_thinking_phase` 返回 partial resp 时，标记 `resp._is_partial = True`（LLMResponse 增加字段），`_parse_tool_calls` 检测到该标记时跳过文本兼容解析：
```python
# LLMResponse 增加字段（backend.py:18-29）
@dataclass
class LLMResponse:
    content: str
    model: str = ""
    usage: dict | None = None
    tool_calls: list | None = None
    is_partial: bool = False  # Track 3 新增

# _parse_tool_calls 改造（engine.py:1583+）
def _parse_tool_calls(self, resp: LLMResponse) -> list[dict]:
    if resp.tool_calls:
        # ... 现有标准 tool_calls 解析 ...
        return calls
    # HC-4：partial 响应跳过文本兼容解析（避免不完整 JSON 误触发工具调用）
    if getattr(resp, "is_partial", False):
        return []
    # ... 现有文本兼容解析 ...
```

#### 3.4.2 partial 响应的对话历史处理

partial 响应写入 `self.conversation` 时（`engine.py:709`），标记 content 来源：
```python
self.conversation.append({
    "role": "assistant",
    "content": resp.content or "",
    "_partial": True,  # 标记，供后续 compactor 参考（不阻塞主流程）
})
```
> 注：`_partial` 字段不强制要求下游处理，仅作遥测/调试用，符合"不过度工程化"原则。

---

## 4. 实施步骤（按顺序执行）

### Step 1: 创建 git tag pre-stream-recovery

- [ ] **Step 1.1: 确认工作区干净**

Run: `git status`
Expected: nothing to commit, working tree clean

- [ ] **Step 1.2: 创建基线 tag**

Run: `git tag pre-stream-recovery`
Expected: 无输出（tag 创建成功）

- [ ] **Step 1.3: 验证 tag**

Run: `git tag --list "pre-stream-recovery"`
Expected: `pre-stream-recovery`

### Step 2: 定义 StreamBuffer 和 StreamResult 数据类

**Files:**
- Modify: `iron/llm/backend.py:18-29`（LLMResponse 增加 `is_partial` 字段）
- Modify: `iron/llm/backend.py:30-31`（在 LLMResponse 后、CircuitBreaker 前插入 StreamBuffer + StreamResult）

- [ ] **Step 2.1: LLMResponse 增加 is_partial 字段**

```python
# backend.py:18-29
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
```

- [ ] **Step 2.2: 插入 StreamBuffer 数据类**

在 `backend.py:30`（LLMResponse 类之后、`class CircuitBreaker` 之前）插入 3.1.1 节的 `StreamBuffer` 完整定义（7 个方法 + 5 个字段）。

- [ ] **Step 2.3: 插入 StreamResult 数据类**

紧接 StreamBuffer 之后插入 3.1.2 节的 `StreamResult` 完整定义（含 `from_buffer` 静态方法）。

- [ ] **Step 2.4: 写失败测试**

Create: `tests/test_stream_buffer.py`
```python
import pytest
from iron.llm.backend import StreamBuffer, StreamResult

def test_stream_buffer_append_accumulates():
    buf = StreamBuffer()
    buf.append("hello ")
    buf.append("world")
    assert buf.flush() == "hello world"
    assert buf.chunks_received == 2
    assert buf.is_partial() is True
    assert buf.is_empty() is False

def test_stream_buffer_empty_is_failed_state():
    buf = StreamBuffer()
    assert buf.is_empty() is True
    assert buf.is_partial() is False
    result = StreamResult.from_buffer(buf)
    assert result.is_failed

def test_stream_buffer_mark_complete():
    buf = StreamBuffer()
    buf.append("done")
    buf.mark_complete()
    assert buf.is_partial() is False
    result = StreamResult.from_buffer(buf)
    assert result.is_complete

def test_stream_buffer_flush_idempotent():
    buf = StreamBuffer()
    buf.append("a")
    first = buf.flush()
    second = buf.flush()
    assert first == second == "a"

def test_stream_buffer_mark_failed_records_reason():
    buf = StreamBuffer()
    buf.append("partial")
    buf.mark_failed("timeout")
    assert buf.failure_reason == "timeout"
    result = StreamResult.from_buffer(buf)
    assert result.is_partial
    assert result.error == "timeout"

def test_stream_result_from_buffer_partial_no_tool_calls():
    buf = StreamBuffer()
    buf.append("partial text")
    result = StreamResult.from_buffer(buf, tool_calls=[{"id": "x"}])
    assert result.is_partial
    assert result.tool_calls is None  # HC-4: partial 不传 tool_calls
    assert result.content == "partial text"

def test_stream_buffer_empty_chunk_ignored():
    buf = StreamBuffer()
    buf.append("")
    assert buf.chunks_received == 0
    assert buf.is_empty()
```

- [ ] **Step 2.5: 运行测试验证通过**

Run: `pytest tests/test_stream_buffer.py -v`
Expected: 7 passed

- [ ] **Step 2.6: Commit**

```bash
git add iron/llm/backend.py tests/test_stream_buffer.py
git commit -m "feat(llm): add StreamBuffer and StreamResult for stream recovery"
```

### Step 3: backend.py 流式迭代器改造

**Files:**
- Modify: `iron/llm/backend.py:252-277`（LLMBackend.stream_generate 默认实现）
- Modify: `iron/llm/backend.py:352-458`（OpenAIBackend.stream_generate）
- Modify: `iron/llm/backend.py:545-672`（AnthropicBackend.stream_generate）
- Modify: `iron/llm/backend.py:743-829`（OllamaBackend.stream_generate）
- Test: `tests/test_backend.py`

- [ ] **Step 3.1: 改造 LLMBackend.stream_generate 默认实现**

按 3.2.4 节改造 `backend.py:252-277`，引入 `buf = StreamBuffer()`，终止事件 yield `("result", StreamResult)`。

- [ ] **Step 3.2: 改造 OpenAIBackend.stream_generate**

按 3.2.2 节改造 `backend.py:352-458`：
- line 378：`content_parts = []` → `buf = StreamBuffer()`
- line 413-415：`buf.append(text_delta)` 替代 `content_parts.append`
- line 438-450：4 个 except 分支改为 `buf.mark_failed(...) + yield ("result", StreamResult.from_buffer(buf, model=model_name))`
- line 452-458：`buf.mark_complete() + yield ("result", StreamResult(state="complete", content=buf.flush(), tool_calls=tool_calls, model=model_name))`

- [ ] **Step 3.3: 改造 AnthropicBackend.stream_generate**

按 3.2.3 节改造 `backend.py:545-672`，同 OpenAI 模式。注意 `current_tool` 在 `content_block_stop` 未到达时丢弃（HC-4）。

- [ ] **Step 3.4: 改造 OllamaBackend.stream_generate**

按 3.2.3 节改造 `backend.py:743-829`，同 OpenAI 模式。

- [ ] **Step 3.5: 写 backend 流式测试**

Create/Modify: `tests/test_backend.py`（若存在则追加，否则新建）
```python
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from iron.llm.backend import StreamResult, StreamBuffer, OpenAIBackend, AnthropicBackend

@pytest.mark.asyncio
async def test_openai_stream_success_yields_result_complete():
    # mock httpx stream 返回正常 SSE
    backend = OpenAIBackend(api_key="sk-test", base_url="https://api.test.com/v1", model="gpt-4o")
    # ... 构造 mock response，断言最终 yield ("result", StreamResult(state="complete", ...))
    pass  # 实现细节见 test_backend.py 现有 mock 模式

@pytest.mark.asyncio
async def test_openai_stream_timeout_yields_result_partial():
    # mock httpx stream 抛 TimeoutException，已 yield 2 个 chunk
    # 断言最终 yield ("result", StreamResult(state="partial", chunks_received=2))
    pass

@pytest.mark.asyncio
async def test_openai_stream_connect_error_yields_result_failed():
    # mock httpx stream 抛 ConnectError，0 chunk
    # 断言最终 yield ("result", StreamResult(state="failed", chunks_received=0))
    pass

@pytest.mark.asyncio
async def test_anthropic_stream_partial_preserves_content():
    # mock Anthropic SSE，中途 TimeoutException，已收 text_delta
    # 断言 StreamResult.state == "partial"，content 含已收文本
    pass
```

- [ ] **Step 3.6: 运行 backend 测试验证不回归**

Run: `pytest tests/test_backend.py -v`
Expected: 全绿（原有用例 + 新增 4 用例）

- [ ] **Step 3.7: Commit**

```bash
git add iron/llm/backend.py tests/test_backend.py
git commit -m "feat(llm): integrate StreamBuffer into 4 backends, yield StreamResult"
```

### Step 4: engine.py fallback 策略调整

> ⚠️ **依赖 Track 1**：本 Step 改造 `_handle_thinking_phase`（Track 1 从 `process` line 623-712 提取）。若 Track 1 未合并，需先 rebase Track 1 分支。

**Files:**
- Modify: `iron/agent/engine.py:640-698`（或 Track 1 提取后的 `_handle_thinking_phase`）
- Modify: `iron/agent/engine.py:1583-1608`（_parse_tool_calls 增加 partial 跳过）
- Modify: `iron/agent/engine_events.py:36-56`（AgentEvent 文档增加 stream_partial）
- Test: `tests/test_engine.py`

- [ ] **Step 4.1: 等待 Track 1 合并并 rebase**

Run: `git fetch origin && git rebase origin/main`（假设 Track 1 已合入 main）
Expected: 无冲突或冲突已解决

- [ ] **Step 4.2: AgentEvent 文档增加 stream_partial**

修改 `engine_events.py:39-53` 的 AgentEvent 文档注释，在 `chat_response` 后增加：
```
- stream_partial: 流式响应不完整，已保留已接收内容
```

- [ ] **Step 4.3: 改造 _handle_thinking_phase 走三态决策**

按 3.3.2 节改造 `_handle_thinking_phase`：
- 初始化 `buf = StreamBuffer()` + `stream_result = None`
- 流式循环识别 `("result", StreamResult)` 新协议 + `("response"|"error")` 旧协议兼容
- 3 个 except 分支（CancelledError / KeyboardInterrupt / RuntimeError+OSError+HTTPError）检查 `buf.is_partial()` 并 flush
- 三态决策：complete 用 resp / partial 不重发 yield stream_partial / failed 才 fallback

- [ ] **Step 4.4: _parse_tool_calls 增加 partial 跳过**

修改 `engine.py:1583+`：
```python
def _parse_tool_calls(self, resp: LLMResponse) -> list[dict]:
    if resp.tool_calls:
        # ... 现有标准 tool_calls 解析（不变）...
        return calls
    # HC-4: partial 响应跳过文本兼容解析
    if getattr(resp, "is_partial", False):
        return []
    # ... 现有文本兼容解析（不变）...
```

- [ ] **Step 4.5: partial resp 标记 is_partial=True**

在 `_handle_thinking_phase` 构造 partial LLMResponse 时（3.3.2 节 3 处）：
```python
resp = LLMResponse(content=buf.flush(), model="", tool_calls=None, is_partial=True)
```

- [ ] **Step 4.6: 写 engine fallback 测试**

Modify: `tests/test_engine.py`
```python
@pytest.mark.asyncio
async def test_handle_thinking_phase_complete_no_fallback(fake_llm):
    fake_llm.stream_generate = mock_stream([("chunk", "hi"), ("result", StreamResult(state="complete", content="hi"))])
    # 断言不调用 llm.generate，resp.content == "hi"

@pytest.mark.asyncio
async def test_handle_thinking_phase_partial_uses_buffer_no_resend(fake_llm):
    fake_llm.stream_generate = mock_stream([("chunk", "partial"), ("result", StreamResult(state="partial", content="partial", chunks_received=1))])
    # 断言不调用 llm.generate（HC-1），yield stream_partial 事件，resp.is_partial=True

@pytest.mark.asyncio
async def test_handle_thinking_phase_failed_falls_back(fake_llm):
    fake_llm.stream_generate = mock_stream([("result", StreamResult(state="failed", error="conn"))])
    fake_llm.generate = AsyncMock(return_value=LLMResponse(content="full"))
    # 断言调用 llm.generate（HC-3 允许），resp.content == "full"

@pytest.mark.asyncio
async def test_handle_thinking_phase_keyboard_interrupt_flushes(fake_llm):
    fake_llm.stream_generate = mock_stream_interrupt_after_chunks([("chunk", "a"), ("chunk", "b")], interrupt=KeyboardInterrupt)
    # 断言 yield stream_partial，resp.content == "ab"，is_partial=True

@pytest.mark.asyncio
async def test_parse_tool_calls_skips_partial_text_compat():
    resp = LLMResponse(content='```json\n{"name": "too', is_partial=True)
    # 断言返回 []（HC-4）
```

- [ ] **Step 4.7: 运行 engine 测试验证**

Run: `pytest tests/test_engine.py -v`
Expected: 全绿（原有用例不回归 + 新增 5 用例）

- [ ] **Step 4.8: Commit**

```bash
git add iron/agent/engine.py iron/agent/engine_events.py tests/test_engine.py
git commit -m "feat(agent): three-state stream recovery, no resend on partial"
```

### Step 5: 新增 stream_partial AgentEvent UI 处理

**Files:**
- Modify: `iron/cli/main.py:1166`（事件分发增加 stream_partial 分支）

- [ ] **Step 5.1: main.py 事件分发增加 stream_partial**

在 `main.py:1166`（`elif etype == "chat_chunk":` 附近）增加：
```python
elif etype == "stream_partial":
    msg = data.get("message", "流式响应不完整")
    chunks = data.get("chunks_received", 0)
    clen = data.get("content_len", 0)
    console.print(f"[yellow][流式不完整] {msg}（{chunks} chunk / {clen} 字符）[/yellow]")
```

- [ ] **Step 5.2: 手动验证**

Run: `python -m iron.cli main`（触发流式中断场景，如断网）
Expected: UI 显示黄色"[流式不完整]"提示，且不重发请求

- [ ] **Step 5.3: Commit**

```bash
git add iron/cli/main.py
git commit -m "feat(cli): display stream_partial event as yellow warning"
```

### Step 6: 新增测试 tests/test_stream_recovery.py

**Files:**
- Create: `tests/test_stream_recovery.py`

- [ ] **Step 6.1: 写 6 个端到端恢复测试**

```python
"""Track 3 端到端流式恢复测试"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from iron.llm.backend import StreamResult, StreamBuffer, LLMResponse
from iron.agent.engine import AgentEngine

@pytest.mark.asyncio
async def test_stream_success_no_fallback(fake_engine_with_stream):
    """流式成功不触发 fallback"""
    fake_engine_with_stream.llm.stream_generate = mock_stream([
        ("chunk", "hello"),
        ("result", StreamResult(state="complete", content="hello", chunks_received=1)),
    ])
    fake_engine_with_stream.llm.generate = AsyncMock()
    async for _ in fake_engine_with_stream.process("test"):
        pass
    fake_engine_with_stream.llm.generate.assert_not_called()  # 不重发

@pytest.mark.asyncio
async def test_stream_partial_recovery_uses_buffer(fake_engine_with_stream):
    """partial 状态用 buffer，不重发（HC-1）"""
    fake_engine_with_stream.llm.stream_generate = mock_stream([
        ("chunk", "partial "),
        ("chunk", "content"),
        ("result", StreamResult(state="partial", content="partial content", chunks_received=2)),
    ])
    fake_engine_with_stream.llm.generate = AsyncMock()
    events = []
    async for ev in fake_engine_with_stream.process("test"):
        events.append(ev)
    fake_engine_with_stream.llm.generate.assert_not_called()  # HC-1
    assert any(e.type == "stream_partial" for e in events)

@pytest.mark.asyncio
async def test_stream_total_failure_falls_back(fake_engine_with_stream):
    """0 chunk 失败允许 fallback（HC-3）"""
    fake_engine_with_stream.llm.stream_generate = mock_stream([
        ("result", StreamResult(state="failed", error="conn refused")),
    ])
    fake_engine_with_stream.llm.generate = AsyncMock(return_value=LLMResponse(content="full"))
    async for _ in fake_engine_with_stream.process("test"):
        pass
    fake_engine_with_stream.llm.generate.assert_called_once()  # HC-3 允许重发

@pytest.mark.asyncio
async def test_stream_no_double_token_consumption(fake_engine_with_stream):
    """有 chunk 时绝不重发（HC-1 量化验证）"""
    fake_engine_with_stream.llm.stream_generate = mock_stream([
        ("chunk", "a"), ("chunk", "b"), ("chunk", "c"),
        ("result", StreamResult(state="partial", content="abc", chunks_received=3)),
    ])
    fake_engine_with_stream.llm.generate = AsyncMock()
    async for _ in fake_engine_with_stream.process("test"):
        pass
    assert fake_engine_with_stream.llm.generate.call_count == 0  # HC-1 核心

@pytest.mark.asyncio
async def test_stream_flush_on_keyboard_interrupt(fake_engine_with_stream):
    """KeyboardInterrupt 时 flush 缓存（HC-2）"""
    fake_engine_with_stream.llm.stream_generate = mock_stream_interrupt(
        [("chunk", "flushed "), ("chunk", "content")],
        interrupt=KeyboardInterrupt,
    )
    events = []
    with pytest.raises(KeyboardInterrupt):
        async for ev in fake_engine_with_stream.process("test"):
            events.append(ev)
    # HC-2: flush 事件已发出
    assert any(e.type == "stream_partial" for e in events)
    partial_ev = next(e for e in events if e.type == "stream_partial")
    assert partial_ev.data["content_len"] > 0

@pytest.mark.asyncio
async def test_partial_tool_call_json_handled(fake_engine_with_stream):
    """partial 响应的不完整 JSON 不传给 _parse_tool_calls（HC-4）"""
    fake_engine_with_stream.llm.stream_generate = mock_stream([
        ("chunk", '```json\n{"name": "write_file", "arguments": {"path": "main.c", "content": "incomp'),
        ("result", StreamResult(state="partial", content='```json\n{"name":...', chunks_received=1)),
    ])
    # 断言不执行 write_file 工具
    events = []
    async for ev in fake_engine_with_stream.process("test"):
        events.append(ev)
    assert not any(e.type == "file_start" for e in events)  # HC-4: 工具未执行
```

- [ ] **Step 6.2: 运行端到端测试**

Run: `pytest tests/test_stream_recovery.py -v`
Expected: 6 passed

- [ ] **Step 6.3: Commit**

```bash
git add tests/test_stream_recovery.py
git commit -m "test: add 6 end-to-end stream recovery tests for HC-1 to HC-4"
```

---

## 5. 验证清单

- [ ] `grep -n "StreamBuffer\|StreamResult\|chunk_buffer" iron/llm/backend.py` 命中 ≥ 5 行
- [ ] `pytest tests/test_stream_recovery.py -v` 全绿（6 用例）
- [ ] `pytest tests/test_backend.py -v` 全绿（原有用例不回归 + 新增 4 用例）
- [ ] `pytest tests/test_engine.py -v` 全绿（原有用例不回归 + 新增 5 用例）
- [ ] `pytest tests/test_stream_buffer.py -v` 全绿（7 用例）
- [ ] `grep -n "stream_partial" iron/agent/engine_events.py iron/cli/main.py iron/agent/engine.py` 命中
- [ ] `grep -n "is_partial" iron/llm/backend.py iron/agent/engine.py` 命中（LLMResponse 字段 + _parse_tool_calls 跳过）
- [ ] HC-1 验证：`grep -A5 "is_partial" iron/agent/engine.py` 确认 partial 分支无 `self.llm.generate(` 调用
- [ ] HC-2 验证：`grep -B2 -A5 "KeyboardInterrupt" iron/agent/engine.py` 确认有 flush 逻辑
- [ ] HC-3 验证：`grep -A10 "is_failed" iron/agent/engine.py` 确认 failed 分支有 fallback
- [ ] HC-4 验证：`grep -B2 -A3 "is_partial" iron/agent/engine.py` 的 `_parse_tool_calls` 确认 `return []`
- [ ] grep 确认流式中断（partial）时不调用 `llm.generate(stream=False)`（除非 0 chunk）

---

## 6. 回滚策略

### 6.1 完整回滚

```bash
git reset --hard pre-stream-recovery
git tag -d stream-recovery-merged  # 若已打合并 tag
```

### 6.2 分步回滚

| Step | 回滚命令 | 影响 |
|------|----------|------|
| Step 6 | `git revert <commit-6>` | 仅丢测试，功能不受影响 |
| Step 5 | `git revert <commit-5>` | UI 不显示 stream_partial，功能仍工作 |
| Step 4 | `git revert <commit-4>` | engine 回到旧 fallback（partial 用 _accumulated_chunks，无三态） |
| Step 3 | `git revert <commit-3>` | backend 回到旧协议（yield response/error），需同时 revert Step 4 |
| Step 2 | `git revert <commit-2>` | 删除 StreamBuffer/StreamResult，需先 revert Step 3 |

### 6.3 Step 4 独立回滚（依赖 Track 1）

Step 4（engine.py 改造）依赖 Track 1 的 `_handle_thinking_phase` 提取。若 Track 1 出现问题需回滚，本 Track 的 Step 1-3（backend.py 改造）可独立保留——backend 层的新协议（`("result", StreamResult)`）与 engine 旧协议通过 3.2.1 的向后兼容共存。

---

## 7. 与其他 Track 的接口契约

### 7.1 与 Track 1（engine.py 拆分）的依赖

- **依赖点**：Step 4 改造 `_handle_thinking_phase`，该方法是 Track 1 从 `process` line 623-712 提取。
- **依赖方向**：Track 3 Step 4 ← Track 1（Track 1 必须先合并）
- **冲突文件**：`iron/agent/engine.py`（line 623-712 区域）
- **协调机制**：Track 1 合并后，Track 3 在 `_handle_thinking_phase` 内部改造，不改方法签名，冲突最小。

### 7.2 与 Track 1-3 的并行性

| Step | 是否可与 Track 1 并行 | 说明 |
|------|----------------------|------|
| Step 1（git tag） | ✅ | 仅打 tag |
| Step 2（StreamBuffer 定义） | ✅ | 仅改 backend.py，与 engine.py 拆分无冲突 |
| Step 3（backend 流式改造） | ✅ | 仅改 backend.py，与 engine.py 拆分无冲突 |
| Step 4（engine fallback 改造） | ❌ | 必须在 Track 1 合并后 rebase |
| Step 5（main.py UI） | ✅ | 仅改 main.py 事件分发，与 Track 2（main.py 拆分）可能冲突，需协调 |
| Step 6（端到端测试） | ⚠️ | 依赖 Step 4 完成，但测试文件本身不冲突 |

### 7.3 与 Track 2（main.py 拆分）的关系

- **无直接冲突**：Track 3 仅在 `main.py:1166` 附近增加一个 `elif etype == "stream_partial"` 分支，Track 2 拆分 main.py 时保留该分支即可。
- **协调机制**：Track 2 拆分时若移动事件分发逻辑，需保留 `stream_partial` 分支。建议 Track 2 在 Track 3 Step 5 之后 rebase。

### 7.4 接口契约总结

| 接口 | 提供方 | 消费方 | 契约 |
|------|--------|--------|------|
| `StreamBuffer` | Track 3 Step 2 | Track 3 Step 3/4 | 7 方法 + 5 字段（见 3.1.1） |
| `StreamResult` | Track 3 Step 2 | Track 3 Step 3/4 | 三态 state + from_buffer（见 3.1.2） |
| `("result", StreamResult)` 事件 | Track 3 Step 3 | Track 3 Step 4 | 替代旧 `("response"|"error")`，向后兼容 |
| `LLMResponse.is_partial` 字段 | Track 3 Step 2 | Track 3 Step 4 | bool，默认 False |
| `stream_partial` AgentEvent | Track 3 Step 4 | Track 3 Step 5 / UI | data: {message, chunks_received, content_len} |
| `_parse_tool_calls` partial 跳过 | Track 3 Step 4 | — | `if resp.is_partial: return []` |

### 7.5 不在本 Track 范围

- CircuitBreaker 与流式集成（streaming 经 `_post_with_retry`）→ Track 4
- 流式重试机制（partial 后是否重试剩余部分）→ Track 5（未来）
- partial 响应的 token 计费修正（usage 字段在中断时不准确）→ Track 6（未来）

---

## 附录 A: 现有代码行号速查表

| 文件 | 行号 | 内容 |
|------|------|------|
| `iron/llm/backend.py` | 18-29 | LLMResponse dataclass |
| `iron/llm/backend.py` | 32-73 | CircuitBreaker |
| `iron/llm/backend.py` | 76-281 | LLMBackend 抽象基类 |
| `iron/llm/backend.py` | 148-199 | _post_with_retry（非流式，含熔断） |
| `iron/llm/backend.py` | 252-277 | stream_generate 默认实现 |
| `iron/llm/backend.py` | 352-458 | OpenAIBackend.stream_generate |
| `iron/llm/backend.py` | 438-450 | OpenAI 流式异常处理（违反 HC-2） |
| `iron/llm/backend.py` | 545-672 | AnthropicBackend.stream_generate |
| `iron/llm/backend.py` | 653-664 | Anthropic 流式异常处理（违反 HC-2） |
| `iron/llm/backend.py` | 743-829 | OllamaBackend.stream_generate |
| `iron/llm/backend.py` | 811-822 | Ollama 流式异常处理（违反 HC-2） |
| `iron/llm/backend.py` | 832-905 | EchoBackend（无 stream 覆盖） |
| `iron/agent/engine.py` | 516 | _emit_event 定义 |
| `iron/agent/engine.py` | 623-712 | process 流式处理 + fallback（Track 1 提取目标） |
| `iron/agent/engine.py` | 640-643 | resp/_stream_error/_stream_chunks_received/_accumulated_chunks 初始化 |
| `iron/agent/engine.py` | 644-670 | 流式循环（chunk/response/error 分支） |
| `iron/agent/engine.py` | 671-674 | 异常捕获（缺 KeyboardInterrupt → 违反 HC-2） |
| `iron/agent/engine.py` | 676-698 | fallback 逻辑（partial 用 buffer / failed 重发） |
| `iron/agent/engine.py` | 701 | _parse_tool_calls 调用（partial 未跳过 → 违反 HC-4） |
| `iron/agent/engine.py` | 1583-1627+ | _parse_tool_calls 定义（JSONDecodeError 静默 args={}） |
| `iron/agent/engine_events.py` | 36-56 | AgentEvent dataclass（无 stream_partial） |
| `iron/cli/main.py` | 1166 | chat_chunk 事件分发（stream_partial 待增加） |

## 附录 B: 硬约束违反点修复映射

| 硬约束 | 违反位置 | 修复 Step | 修复方式 |
|--------|----------|-----------|----------|
| HC-1 | engine.py:685-698（依赖 _stream_chunks_received 布尔，不够显式） | Step 4 | StreamBuffer.is_empty() 显式判定 + 三态决策表 |
| HC-2 | engine.py:671-672（未捕获 KeyboardInterrupt） | Step 4 | 增加 `except KeyboardInterrupt` 分支 + flush |
| HC-2 | backend.py:438-450/653-664/811-822（丢弃 content_parts） | Step 3 | StreamBuffer.append + StreamResult.from_buffer 保留内容 |
| HC-3 | engine.py:685-698（0 chunk 重发，当前已允许） | Step 4 | 三态决策表显式记录 allowed |
| HC-4 | engine.py:701 + 1583-1608（partial JSON 静默 args={}） | Step 4 | LLMResponse.is_partial + _parse_tool_calls 早返回 [] |

---

**文档版本**: v1.0
**基于源码快照**: `iron/llm/backend.py`（934 行）/ `iron/agent/engine.py`（line 623-712）
**生成日期**: 2026-06-28
**Track**: Track 3 · 流式中断恢复 chunk_buffer
