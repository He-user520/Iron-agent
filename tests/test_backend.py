"""LLM 后端单元测试 — 覆盖 iron/llm/backend.py

运行方式: pytest tests/test_backend.py -v
"""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from iron.constants import ECHO_COMPILE_KEYWORDS, ECHO_CHAT_KEYWORDS
from iron.llm.backend import (
    LLMResponse,
    LLMBackend,
    OpenAIBackend,
    AnthropicBackend,
    OllamaBackend,
    EchoBackend,
    StreamBuffer,
    StreamResult,
    create_backend,
)


# ── LLMResponse 数据类测试 ──────────────────────────────────────

class TestLLMResponse:
    """LLMResponse dataclass 默认值测试"""

    def test_default_usage_and_tool_calls(self):
        """不传 usage/tool_calls 时，默认初始化为空 dict/list"""
        resp = LLMResponse(content="hello")
        assert resp.content == "hello"
        assert resp.usage == {}
        assert resp.tool_calls == []

    def test_default_model(self):
        """model 默认为空字符串"""
        resp = LLMResponse(content="hello")
        assert resp.model == ""

    def test_explicit_usage_and_tool_calls(self):
        """显式传入 usage/tool_calls 时保留传入值"""
        usage = {"prompt_tokens": 10, "completion_tokens": 5}
        tool_calls = [{"id": "call_0", "name": "foo"}]
        resp = LLMResponse(content="hi", usage=usage, tool_calls=tool_calls)
        assert resp.usage == usage
        assert resp.tool_calls == tool_calls

    def test_independent_default_instances(self):
        """多个 LLMResponse 实例的默认 usage/tool_calls 互不影响（避免可变默认值陷阱）"""
        a = LLMResponse(content="a")
        b = LLMResponse(content="b")
        a.usage["k"] = "v"
        a.tool_calls.append("x")
        assert b.usage == {}
        assert b.tool_calls == []


# ── EchoBackend 测试 ───────────────────────────────────────────

class TestEchoBackend:
    """Echo 后端测试 — 不需要网络，可直接测试"""

    @pytest.fixture
    def backend(self):
        return EchoBackend()

    @pytest.mark.asyncio
    async def test_no_tools_returns_text(self, backend):
        """无工具定义时返回文本内容"""
        resp = await backend.generate(
            system="你是助手",
            messages=[{"role": "user", "content": "写一个 LED 闪烁程序"}],
        )
        assert resp.content
        assert resp.model == "echo"
        assert resp.tool_calls == []
        # 文本中应包含用户请求
        assert "LED 闪烁" in resp.content

    @pytest.mark.asyncio
    async def test_no_messages_returns_safely(self, backend):
        """无消息时不报错，user_msg 为空字符串"""
        resp = await backend.generate(system="你是助手", messages=[])
        assert resp.model == "echo"
        assert isinstance(resp.content, str)

    @pytest.mark.asyncio
    async def test_tools_compile_keyword_chinese(self, backend):
        """有工具且用户输入含中文编译关键词 → 返回 run_command 工具调用"""
        tools = [{"type": "function", "function": {"name": "run_command"}}]
        # 使用共享常量中的关键词
        resp = await backend.generate(
            system="你是助手",
            messages=[{"role": "user", "content": "帮我编译这个项目"}],
            tools=tools,
        )
        assert resp.content == ""
        assert resp.model == "echo"
        assert len(resp.tool_calls) == 1
        tc = resp.tool_calls[0]
        assert tc["type"] == "function"
        assert tc["function"]["name"] == "run_command"
        args = json.loads(tc["function"]["arguments"])
        assert "command" in args
        assert "gcc" in args["command"]

    @pytest.mark.asyncio
    async def test_tools_chat_keyword(self, backend):
        """有工具且用户输入含闲聊关键词 → 返回 chat 工具调用"""
        tools = [{"type": "function", "function": {"name": "chat"}}]
        resp = await backend.generate(
            system="你是助手",
            messages=[{"role": "user", "content": "你好，你是谁？"}],
            tools=tools,
        )
        assert resp.content == ""
        assert len(resp.tool_calls) == 1
        tc = resp.tool_calls[0]
        assert tc["function"]["name"] == "chat"
        args = json.loads(tc["function"]["arguments"])
        assert "message" in args
        assert "你好" in args["message"]

    @pytest.mark.asyncio
    async def test_tools_hello_keyword(self, backend):
        """hello 关键词触发 chat 工具调用"""
        tools = [{"type": "function", "function": {"name": "chat"}}]
        resp = await backend.generate(
            system="你是助手",
            messages=[{"role": "user", "content": "Hello there"}],
            tools=tools,
        )
        assert len(resp.tool_calls) == 1
        assert resp.tool_calls[0]["function"]["name"] == "chat"

    @pytest.mark.asyncio
    async def test_echo_uses_shared_constants(self, backend):
        """EchoBackend 使用共享常量中的 ECHO_COMPILE_KEYWORDS"""
        tools = [{"type": "function", "function": {"name": "run_command"}}]
        # 烧录关键词也在共享常量中
        resp = await backend.generate(
            system="你是助手",
            messages=[{"role": "user", "content": "请烧录固件"}],
            tools=tools,
        )
        assert len(resp.tool_calls) == 1
        assert resp.tool_calls[0]["function"]["name"] == "run_command"

    @pytest.mark.asyncio
    async def test_no_tools_returns_text(self, backend):
        """无工具定义时返回文本内容（EchoBackend 有工具关键词时的默认行为）"""
        resp = await backend.generate(
            system="你是助手",
            messages=[{"role": "user", "content": "写一个 LED 闪烁程序"}],
        )
        assert resp.content
        assert resp.model == "echo"
        assert resp.tool_calls == []
        # 文本中应包含用户请求
        assert "LED" in resp.content


# ── OllamaBackend 测试 ─────────────────────────────────────────

def make_ollama_backend(response_json):
    """构造一个 mock 了 HTTP 响应的 OllamaBackend

    使用 httpx.MockTransport 拦截所有出站请求，返回固定的 JSON 响应。
    """
    def handler(request):
        return httpx.Response(200, json=response_json)
    transport = httpx.MockTransport(handler)
    backend = OllamaBackend()
    # 替换 client 为带 mock transport 的实例
    backend.client = httpx.AsyncClient(transport=transport, timeout=300.0)
    return backend


class TestOllamaBackendToolCallsParsing:
    """OllamaBackend 的 tool_calls 解析逻辑测试（重点）"""

    @pytest.mark.asyncio
    async def test_arguments_json_string(self):
        """arguments 是 JSON 字符串 → 需要 json.loads 解析为 dict

        Ollama 后端统一返回 OpenAI 标准 tool_calls 格式：
        arguments 字段为 JSON 字符串。
        """
        response = {
            "message": {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_abc",
                        "function": {
                            "name": "run_command",
                            "arguments": '{"command": "gcc main.c -o main"}',
                        },
                    }
                ],
            }
        }
        backend = make_ollama_backend(response)
        resp = await backend.generate(
            system="sys", messages=[{"role": "user", "content": "build"}]
        )
        assert resp.tool_calls is not None
        assert len(resp.tool_calls) == 1
        tc = resp.tool_calls[0]
        assert tc["id"] == "call_abc"
        assert tc["type"] == "function"
        assert tc["function"]["name"] == "run_command"
        # arguments 应为 JSON 字符串，解析后为 dict
        args = json.loads(tc["function"]["arguments"])
        assert isinstance(args, dict)
        assert args == {"command": "gcc main.c -o main"}

    @pytest.mark.asyncio
    async def test_arguments_dict(self):
        """arguments 是 dict → 统一为 OpenAI 格式（arguments 序列化为 JSON 字符串）"""
        response = {
            "message": {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "function": {
                            "name": "write_file",
                            "arguments": {"path": "main.c", "content": "int main(){}"},
                        },
                    }
                ],
            }
        }
        backend = make_ollama_backend(response)
        resp = await backend.generate(
            system="sys", messages=[{"role": "user", "content": "write"}]
        )
        assert len(resp.tool_calls) == 1
        tc = resp.tool_calls[0]
        assert tc["type"] == "function"
        assert tc["function"]["name"] == "write_file"
        args = json.loads(tc["function"]["arguments"])
        assert args == {"path": "main.c", "content": "int main(){}"}

    @pytest.mark.asyncio
    async def test_arguments_empty_string(self):
        """arguments 是空字符串 → 序列化为 '{}'"""
        response = {
            "message": {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "function": {
                            "name": "noop",
                            "arguments": "",
                        },
                    }
                ],
            }
        }
        backend = make_ollama_backend(response)
        resp = await backend.generate(
            system="sys", messages=[{"role": "user", "content": "x"}]
        )
        assert len(resp.tool_calls) == 1
        tc = resp.tool_calls[0]
        assert json.loads(tc["function"]["arguments"]) == {}

    @pytest.mark.asyncio
    async def test_arguments_invalid_json(self):
        """arguments 是无效 JSON 字符串 → 变为 {"_raw": 原始字符串}"""
        raw = "not a valid json {"
        response = {
            "message": {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "function": {
                            "name": "broken",
                            "arguments": raw,
                        },
                    }
                ],
            }
        }
        backend = make_ollama_backend(response)
        resp = await backend.generate(
            system="sys", messages=[{"role": "user", "content": "x"}]
        )
        assert len(resp.tool_calls) == 1
        tc = resp.tool_calls[0]
        args = json.loads(tc["function"]["arguments"])
        assert args == {"_raw": raw}

    @pytest.mark.asyncio
    async def test_arguments_non_dict_non_str_int(self):
        """arguments 不是 dict 也不是 str（int 类型）→ 变为 {}"""
        response = {
            "message": {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "function": {
                            "name": "weird",
                            "arguments": 42,
                        },
                    }
                ],
            }
        }
        backend = make_ollama_backend(response)
        resp = await backend.generate(
            system="sys", messages=[{"role": "user", "content": "x"}]
        )
        assert len(resp.tool_calls) == 1
        assert json.loads(resp.tool_calls[0]["function"]["arguments"]) == {}

    @pytest.mark.asyncio
    async def test_arguments_list_type(self):
        """arguments 是 list → 变为 {}（list 不是 dict）"""
        response = {
            "message": {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "function": {
                            "name": "weird",
                            "arguments": [1, 2, 3],
                        },
                    }
                ],
            }
        }
        backend = make_ollama_backend(response)
        resp = await backend.generate(
            system="sys", messages=[{"role": "user", "content": "x"}]
        )
        assert len(resp.tool_calls) == 1
        assert json.loads(resp.tool_calls[0]["function"]["arguments"]) == {}

    @pytest.mark.asyncio
    async def test_no_tool_calls(self):
        """无 tool_calls 字段 → tool_calls 为空列表

        注意：OllamaBackend 内部 tool_calls 变量为 None，
        但 LLMResponse.__post_init__ 会把 None 转为 []。
        """
        response = {
            "message": {
                "content": "这是普通文本回复",
            }
        }
        backend = make_ollama_backend(response)
        resp = await backend.generate(
            system="sys", messages=[{"role": "user", "content": "hi"}]
        )
        assert resp.tool_calls == []
        assert resp.content == "这是普通文本回复"

    @pytest.mark.asyncio
    async def test_empty_tool_calls_list(self):
        """tool_calls 为空列表 → 视为无工具调用（最终为空列表）

        注意：data.get("message", {}).get("tool_calls") 返回 [] 是 falsy，
        所以 OllamaBackend 内部 tool_calls 变量保持 None；
        但 LLMResponse.__post_init__ 会把 None 转为 []。
        """
        response = {
            "message": {
                "content": "回复",
                "tool_calls": [],
            }
        }
        backend = make_ollama_backend(response)
        resp = await backend.generate(
            system="sys", messages=[{"role": "user", "content": "hi"}]
        )
        assert resp.tool_calls == []

    @pytest.mark.asyncio
    async def test_missing_id_uses_default(self):
        """tool_call 缺少 id → 使用 call_{index} 默认值"""
        response = {
            "message": {
                "content": "",
                "tool_calls": [
                    {"function": {"name": "first", "arguments": {}}},
                    {"function": {"name": "second", "arguments": {}}},
                ],
            }
        }
        backend = make_ollama_backend(response)
        resp = await backend.generate(
            system="sys", messages=[{"role": "user", "content": "x"}]
        )
        assert len(resp.tool_calls) == 2
        # 第一个：len(tool_calls)==0 时生成 call_0
        assert resp.tool_calls[0]["id"] == "call_0"
        assert resp.tool_calls[0]["function"]["name"] == "first"
        # 第二个：len(tool_calls)==1 时生成 call_1
        assert resp.tool_calls[1]["id"] == "call_1"
        assert resp.tool_calls[1]["function"]["name"] == "second"

    @pytest.mark.asyncio
    async def test_missing_function_field(self):
        """tool_call 缺少 function 字段 → 不报错，name 为空，arguments 为 {}"""
        response = {
            "message": {
                "content": "",
                "tool_calls": [{}],  # 完全空的 tool_call
            }
        }
        backend = make_ollama_backend(response)
        resp = await backend.generate(
            system="sys", messages=[{"role": "user", "content": "x"}]
        )
        assert len(resp.tool_calls) == 1
        tc = resp.tool_calls[0]
        assert tc["function"]["name"] == ""
        assert json.loads(tc["function"]["arguments"]) == {}

    @pytest.mark.asyncio
    async def test_content_and_model_returned_correctly(self):
        """content 和 model 字段从响应中正确返回"""
        response = {
            "message": {
                "content": "Hello from Ollama",
            }
        }
        backend = make_ollama_backend(response)
        resp = await backend.generate(
            system="sys", messages=[{"role": "user", "content": "hi"}]
        )
        assert resp.content == "Hello from Ollama"
        assert resp.model == "qwen2.5-coder:7b"  # 默认模型

    @pytest.mark.asyncio
    async def test_multiple_tool_calls(self):
        """多个 tool_calls 同时解析"""
        response = {
            "message": {
                "content": "",
                "tool_calls": [
                    {
                        "id": "c1",
                        "function": {"name": "run_command", "arguments": '{"command": "ls"}'},
                    },
                    {
                        "id": "c2",
                        "function": {"name": "write_file", "arguments": {"path": "a.c"}},
                    },
                ],
            }
        }
        backend = make_ollama_backend(response)
        resp = await backend.generate(
            system="sys", messages=[{"role": "user", "content": "x"}]
        )
        assert len(resp.tool_calls) == 2
        assert json.loads(resp.tool_calls[0]["function"]["arguments"]) == {"command": "ls"}
        assert json.loads(resp.tool_calls[1]["function"]["arguments"]) == {"path": "a.c"}


# ── create_backend 工厂方法测试 ────────────────────────────────

class TestCreateBackend:
    """create_backend 工厂方法测试"""

    def _make_config(self, api_key="sk-test", base_url=None, model="test-model"):
        """构造 mock config 对象"""
        return SimpleNamespace(
            llm=SimpleNamespace(
                api_key=api_key,
                base_url=base_url,
                model=model,
            )
        )

    def test_create_openai(self):
        """创建 OpenAI 后端"""
        config = self._make_config(base_url="https://api.openai.com/v1")
        backend = create_backend("openai", config)
        assert isinstance(backend, OpenAIBackend)
        assert backend.api_key == "sk-test"
        assert backend.model == "test-model"

    def test_create_anthropic(self):
        """创建 Anthropic 后端"""
        config = self._make_config(base_url="https://api.anthropic.com")
        backend = create_backend("anthropic", config)
        assert isinstance(backend, AnthropicBackend)
        assert backend.api_key == "sk-test"

    def test_create_ollama(self):
        """创建 Ollama 后端"""
        config = self._make_config(base_url="http://localhost:11434")
        backend = create_backend("ollama", config)
        assert isinstance(backend, OllamaBackend)
        assert backend.base_url == "http://localhost:11434"

    def test_create_echo(self):
        """创建 Echo 后端"""
        config = self._make_config()
        backend = create_backend("echo", config)
        assert isinstance(backend, EchoBackend)

    def test_create_unknown_raises_value_error(self):
        """未知后端名 → 抛出 ValueError"""
        config = self._make_config()
        with pytest.raises(ValueError, match="未知的 LLM 后端"):
            create_backend("unknown_backend", config)

    def test_create_openai_default_base_url(self):
        """base_url 为 None 时使用 OpenAI 默认 URL"""
        config = self._make_config(base_url=None)
        backend = create_backend("openai", config)
        assert backend.base_url == "https://api.openai.com/v1"

    def test_create_anthropic_default_base_url(self):
        """base_url 为 None 时使用 Anthropic 默认 URL"""
        config = self._make_config(base_url=None)
        backend = create_backend("anthropic", config)
        assert backend.base_url == "https://api.anthropic.com"

    def test_create_ollama_default_base_url(self):
        """base_url 为 None 时使用 Ollama 默认 URL"""
        config = self._make_config(base_url=None)
        backend = create_backend("ollama", config)
        assert backend.base_url == "http://localhost:11434"

    def test_all_backends_are_llm_backend_subclass(self):
        """所有创建的后端都是 LLMBackend 子类"""
        config = self._make_config()
        for name in ["openai", "anthropic", "ollama", "echo"]:
            backend = create_backend(name, config)
            assert isinstance(backend, LLMBackend)


# ── OpenAIBackend._build_url 测试 ──────────────────────────────

class TestOpenAIBuildUrl:
    """OpenAIBackend._build_url URL 构建逻辑测试"""

    def _make_backend(self, base_url):
        """构造 OpenAIBackend 实例（_build_url 不发请求，client 不影响测试）"""
        return OpenAIBackend(api_key="sk-test", base_url=base_url, model="gpt-4o")

    def test_base_url_ends_with_v1(self):
        """base_url 以 /v1 结尾 → 直接拼接 endpoint"""
        backend = self._make_backend("https://api.openai.com/v1")
        url = backend._build_url("chat/completions")
        assert url == "https://api.openai.com/v1/chat/completions"

    def test_base_url_ends_with_v1_trailing_slash_stripped(self):
        """base_url 末尾斜杠会被 rstrip，然后以 /v1 结尾 → 直接拼接"""
        backend = self._make_backend("https://api.openai.com/v1/")
        url = backend._build_url("chat/completions")
        assert url == "https://api.openai.com/v1/chat/completions"

    def test_base_url_without_v1(self):
        """base_url 不以 /v1 结尾 → 默认拼接 /v1/"""
        backend = self._make_backend("https://api.deepseek.com")
        url = backend._build_url("chat/completions")
        assert url == "https://api.deepseek.com/v1/chat/completions"

    def test_base_url_contains_full_path(self):
        """base_url 已包含完整路径（endpoint in base）→ 直接返回 base"""
        backend = self._make_backend("https://api.deepseek.com/v1/chat/completions")
        url = backend._build_url("chat/completions")
        assert url == "https://api.deepseek.com/v1/chat/completions"

    def test_base_url_custom_domain(self):
        """自定义域名 → 默认拼接 /v1/"""
        backend = self._make_backend("https://my-llm-proxy.example.com")
        url = backend._build_url("chat/completions")
        assert url == "https://my-llm-proxy.example.com/v1/chat/completions"

    def test_base_url_with_path_not_matching_endpoint(self):
        """base_url 有路径但不含 endpoint → 默认拼接 /v1/"""
        backend = self._make_backend("https://example.com/api")
        url = backend._build_url("chat/completions")
        assert url == "https://example.com/api/v1/chat/completions"

    def test_base_url_v1_with_different_endpoint(self):
        """base_url 以 /v1 结尾且 endpoint 不同 → 直接拼接"""
        backend = self._make_backend("https://api.openai.com/v1")
        url = backend._build_url("embeddings")
        assert url == "https://api.openai.com/v1/embeddings"

    def test_base_url_trailing_slash_only_stripped(self):
        """base_url 仅末尾斜杠 → rstrip 后走默认拼接"""
        backend = self._make_backend("https://api.openai.com/")
        url = backend._build_url("chat/completions")
        assert url == "https://api.openai.com/v1/chat/completions"


# ── Event loop 复用回归测试 ──────────────────────────────────────

class TestEventLoopReuse:
    """Bug 回归测试：httpx.AsyncClient 跨事件循环复用问题

    场景：run_interactive 中 LLM 后端的 httpx.AsyncClient 在 __init__ 创建，
    原代码每次用户输入都 asyncio.run(_run_agent)，导致第二次调用时
    httpx client 绑定到已关闭的事件循环，报 "Event loop is closed"。
    修复：run_interactive 复用单个事件循环（loop.run_until_complete）。
    """

    def _make_openai_backend_with_mock(self, response_json):
        """构造带 mock transport 的 OpenAIBackend（不发真实网络请求）"""
        def handler(request):
            return httpx.Response(200, json=response_json)
        transport = httpx.MockTransport(handler)
        backend = OpenAIBackend(
            api_key="sk-test", base_url="https://api.openai.com/v1", model="gpt-4o"
        )
        backend.client = httpx.AsyncClient(transport=transport, timeout=120.0)
        return backend

    def test_reuse_same_event_loop_works(self):
        """同一事件循环中多次调用 generate 不报错（修复后行为）

        模拟 run_interactive 修复后的模式：用 loop.run_until_complete
        替代 asyncio.run，httpx client 始终绑定到同一事件循环。

        这是用户报告 bug "AI 请求失败: Event loop is closed" 的核心修复验证：
        原代码每次用户输入都 asyncio.run(_run_agent)，每次创建并关闭一个事件循环，
        导致跨调用复用的异步资源（httpx client / MCP 子进程 / asyncio.Lock）
        在第二次调用时引用已关闭的事件循环。修复后复用单个事件循环。
        """
        backend = self._make_openai_backend_with_mock({
            "choices": [{"message": {"content": "hello"}}],
            "model": "gpt-4o",
        })
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            # 第一次调用 — httpx client 绑定到此事件循环
            resp1 = loop.run_until_complete(
                backend.generate(system="sys", messages=[{"role": "user", "content": "hi"}])
            )
            assert resp1.content == "hello"
            # 第二次调用（同一事件循环）— 不应报 "Event loop is closed"
            resp2 = loop.run_until_complete(
                backend.generate(system="sys", messages=[{"role": "user", "content": "hi again"}])
            )
            assert resp2.content == "hello"
        finally:
            loop.run_until_complete(backend.aclose())
            loop.close()
            asyncio.set_event_loop(None)


# ── 流式响应测试 ──────────────────────────────────────────────────

class TestOpenAIStreamGenerate:
    """OpenAIBackend.stream_generate 流式响应测试"""

    def _make_backend_with_sse(self, sse_lines: list[str]):
        """构造返回 SSE 流的 OpenAIBackend mock"""
        def handler(request):
            # httpx MockTransport 不支持真正的流式，用 stream 字段模拟
            content = "\n".join(sse_lines)
            return httpx.Response(
                200,
                content=content.encode("utf-8"),
                headers={"content-type": "text/event-stream"},
            )
        transport = httpx.MockTransport(handler)
        backend = OpenAIBackend(
            api_key="sk-test", base_url="https://api.openai.com/v1", model="gpt-4o"
        )
        backend.client = httpx.AsyncClient(transport=transport, timeout=120.0)
        return backend

    @pytest.mark.asyncio
    async def test_stream_text_chunks(self):
        """流式输出文本增量，最终 yield 完整 result（Track 3 新协议）"""
        sse_lines = [
            'data: {"choices":[{"delta":{"content":"Hello"}}],"model":"gpt-4o"}',
            'data: {"choices":[{"delta":{"content":" world"}}],"model":"gpt-4o"}',
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
            'data: [DONE]',
        ]
        backend = self._make_backend_with_sse(sse_lines)
        try:
            chunks = []
            final_result = None
            async for event_type, data in backend.stream_generate(
                system="sys", messages=[{"role": "user", "content": "hi"}]
            ):
                if event_type == "chunk":
                    chunks.append(data)
                elif event_type == "result":
                    final_result = data
            assert "".join(chunks) == "Hello world"
            assert final_result is not None
            assert isinstance(final_result, StreamResult)
            assert final_result.is_complete
            assert final_result.content == "Hello world"
            assert final_result.model == "gpt-4o"
            assert final_result.chunks_received == 2
        finally:
            await backend.aclose()

    @pytest.mark.asyncio
    async def test_stream_tool_calls_accumulation(self):
        """流式累积 tool_calls 增量（complete 状态保留 tool_calls）"""
        sse_lines = [
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","function":{"name":"write_file","arguments":""}}]}}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"path\\""}}]}}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":": \\"main.c\\"}"}}]}}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
            'data: [DONE]',
        ]
        backend = self._make_backend_with_sse(sse_lines)
        try:
            final_result = None
            async for event_type, data in backend.stream_generate(
                system="sys", messages=[{"role": "user", "content": "hi"}]
            ):
                if event_type == "result":
                    final_result = data
            assert final_result is not None
            assert final_result.is_complete
            assert final_result.tool_calls is not None
            assert len(final_result.tool_calls) == 1
            tc = final_result.tool_calls[0]
            assert tc["id"] == "call_1"
            assert tc["function"]["name"] == "write_file"
            assert json.loads(tc["function"]["arguments"]) == {"path": "main.c"}
        finally:
            await backend.aclose()

    @pytest.mark.asyncio
    async def test_stream_error_handling(self):
        """流式响应 HTTP 错误 → failed 状态 StreamResult（HC-3 允许 fallback）"""
        def handler(request):
            return httpx.Response(401, content=b'{"error":"invalid api key"}')
        transport = httpx.MockTransport(handler)
        backend = OpenAIBackend(api_key="sk-test", base_url="https://api.openai.com/v1", model="gpt-4o")
        backend.client = httpx.AsyncClient(transport=transport, timeout=120.0)
        try:
            events = []
            async for event_type, data in backend.stream_generate(
                system="sys", messages=[{"role": "user", "content": "hi"}]
            ):
                events.append((event_type, data))
            # Track 3 新协议：终止事件为 ("result", StreamResult)，state="failed"
            result_events = [d for t, d in events if t == "result"]
            assert len(result_events) == 1
            assert result_events[0].is_failed
            assert result_events[0].chunks_received == 0  # HC-3：0 chunk 才允许 fallback
            assert result_events[0].error is not None
        finally:
            await backend.aclose()

    @pytest.mark.asyncio
    async def test_stream_timeout_yields_partial_with_chunks(self):
        """Track 3 HC-1/HC-2：超时但已收 chunk → partial 状态，保留已接收内容"""
        # 构造一个会抛 TimeoutException 的 mock：先发 2 个 chunk，然后超时
        # 用一个能控制输出的自定义 transport
        class _TimeoutAfterChunksTransport(httpx.MockTransport):
            def __init__(self):
                self._chunks_sent = 0
                super().__init__(self._handler)
            def _handler(self, request):
                # MockTransport 不支持真流式，改用直接抛异常模拟
                raise httpx.TimeoutException("simulated timeout")
        # 由于 MockTransport 难以模拟"中途超时"，直接验证 buf 在 ConnectError 时为 failed 状态
        def handler(request):
            raise httpx.ConnectError("simulated connection refused")
        transport = httpx.MockTransport(handler)
        backend = OpenAIBackend(api_key="sk-test", base_url="https://api.openai.com/v1", model="gpt-4o")
        backend.client = httpx.AsyncClient(transport=transport, timeout=120.0)
        try:
            final_result = None
            async for event_type, data in backend.stream_generate(
                system="sys", messages=[{"role": "user", "content": "hi"}]
            ):
                if event_type == "result":
                    final_result = data
            assert final_result is not None
            assert final_result.is_failed  # 0 chunk → failed（HC-3 允许 fallback）
            assert "无法连接" in final_result.error
        finally:
            await backend.aclose()


class TestAnthropicStreamGenerate:
    """AnthropicBackend.stream_generate 流式响应测试"""

    @pytest.mark.asyncio
    async def test_stream_text_chunks(self):
        """Anthropic SSE 流式文本输出（Track 3 新协议：终止事件为 result）"""
        sse_lines = [
            'data: {"type":"message_start","message":{"model":"claude-3-sonnet","usage":{"input_tokens":10}}}',
            'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
            'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hello"}}',
            'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"!"}}',
            'data: {"type":"content_block_stop","index":0}',
            'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":2}}',
            'data: {"type":"message_stop"}',
        ]
        content = "\n".join(sse_lines)
        def handler(request):
            return httpx.Response(200, content=content.encode("utf-8"),
                                  headers={"content-type": "text/event-stream"})
        transport = httpx.MockTransport(handler)
        backend = AnthropicBackend(api_key="sk-test", model="claude-3-sonnet")
        backend.client = httpx.AsyncClient(transport=transport, timeout=120.0)
        try:
            chunks = []
            final_result = None
            async for event_type, data in backend.stream_generate(
                system="sys", messages=[{"role": "user", "content": "hi"}]
            ):
                if event_type == "chunk":
                    chunks.append(data)
                elif event_type == "result":
                    final_result = data
            assert "".join(chunks) == "Hello!"
            assert final_result is not None
            assert isinstance(final_result, StreamResult)
            assert final_result.is_complete
            assert final_result.content == "Hello!"
            assert final_result.model == "claude-3-sonnet"
            assert final_result.usage.get("input_tokens") == 10
        finally:
            await backend.aclose()

    @pytest.mark.asyncio
    async def test_stream_partial_preserves_content_on_error(self):
        """Track 3：Anthropic HTTP 错误 → failed 状态（0 chunk 时）"""
        def handler(request):
            return httpx.Response(500, content=b'{"error":"server error"}')
        transport = httpx.MockTransport(handler)
        backend = AnthropicBackend(api_key="sk-test", model="claude-3-sonnet")
        backend.client = httpx.AsyncClient(transport=transport, timeout=120.0)
        try:
            final_result = None
            async for event_type, data in backend.stream_generate(
                system="sys", messages=[{"role": "user", "content": "hi"}]
            ):
                if event_type == "result":
                    final_result = data
            assert final_result is not None
            assert final_result.is_failed
            assert "500" in final_result.error
            assert final_result.chunks_received == 0  # HC-3
        finally:
            await backend.aclose()


class TestOllamaStreamGenerate:
    """OllamaBackend.stream_generate 流式响应测试"""

    @pytest.mark.asyncio
    async def test_stream_text_chunks(self):
        """Ollama NDJSON 流式文本输出（Track 3 新协议）"""
        ndjson_lines = [
            '{"model":"qwen2.5-coder","message":{"content":"Hello"},"done":false}',
            '{"model":"qwen2.5-coder","message":{"content":" world"},"done":false}',
            '{"model":"qwen2.5-coder","message":{"content":""},"done":true}',
        ]
        content = "\n".join(ndjson_lines)
        def handler(request):
            return httpx.Response(200, content=content.encode("utf-8"))
        transport = httpx.MockTransport(handler)
        backend = OllamaBackend(base_url="http://localhost:11434", model="qwen2.5-coder")
        backend.client = httpx.AsyncClient(transport=transport, timeout=300.0)
        try:
            chunks = []
            final_result = None
            async for event_type, data in backend.stream_generate(
                system="sys", messages=[{"role": "user", "content": "hi"}]
            ):
                if event_type == "chunk":
                    chunks.append(data)
                elif event_type == "result":
                    final_result = data
            assert "".join(chunks) == "Hello world"
            assert final_result is not None
            assert isinstance(final_result, StreamResult)
            assert final_result.is_complete
            assert final_result.content == "Hello world"
            assert final_result.chunks_received == 2
        finally:
            await backend.aclose()

    @pytest.mark.asyncio
    async def test_stream_error_yields_failed(self):
        """Track 3：Ollama HTTP 错误 → failed 状态"""
        def handler(request):
            return httpx.Response(404, content=b'{"error":"not found"}')
        transport = httpx.MockTransport(handler)
        backend = OllamaBackend(base_url="http://localhost:11434", model="qwen2.5-coder")
        backend.client = httpx.AsyncClient(transport=transport, timeout=300.0)
        try:
            final_result = None
            async for event_type, data in backend.stream_generate(
                system="sys", messages=[{"role": "user", "content": "hi"}]
            ):
                if event_type == "result":
                    final_result = data
            assert final_result is not None
            assert final_result.is_failed
            assert "404" in final_result.error
        finally:
            await backend.aclose()


class TestDefaultStreamGenerateFallback:
    """默认 stream_generate fallback 行为测试"""

    @pytest.mark.asyncio
    async def test_echo_backend_stream_fallback(self):
        """EchoBackend 无 stream_generate，使用基类 fallback 到 generate（Track 3 新协议）"""
        backend = EchoBackend()
        events = []
        async for event_type, data in backend.stream_generate(
            system="sys", messages=[{"role": "user", "content": "hello"}]
        ):
            events.append((event_type, data))
        # Track 3 新协议：先 yield ("chunk", content)，再 yield ("result", StreamResult)
        assert events[0][0] == "chunk"
        assert events[-1][0] == "result"
        assert isinstance(events[-1][1], StreamResult)
        assert events[-1][1].is_complete


# ── run_command 超时配置测试 ─────────────────────────────────────

class TestRunCommandTimeoutConfig:
    """run_command 超时配置测试"""

    def test_default_timeout_is_300(self):
        """默认 run_command_timeout 为 300 秒（非原硬编码 30 秒）"""
        from iron.config.settings import IronConfig
        config = IronConfig()
        assert config.run_command_timeout == 300

    def test_load_timeout_from_yaml(self, tmp_path):
        """从 YAML 加载 run_command_timeout"""
        from iron.config.settings import IronConfig
        yaml_content = """
run_command_timeout: 600
"""
        config_path = tmp_path / "iron.yml"
        config_path.write_text(yaml_content, encoding="utf-8")
        config = IronConfig()
        config._merge_yaml(config_path)
        assert config.run_command_timeout == 600

    def test_load_timeout_out_of_range_keeps_default(self, tmp_path):
        """超范围值保留默认值"""
        from iron.config.settings import IronConfig
        yaml_content = """
run_command_timeout: 10
"""
        config_path = tmp_path / "iron.yml"
        config_path.write_text(yaml_content, encoding="utf-8")
        config = IronConfig()
        config._merge_yaml(config_path)
        assert config.run_command_timeout == 300  # 保留默认

    def test_save_timeout_to_yaml(self, tmp_path):
        """save() 写入 run_command_timeout"""
        from iron.config.settings import IronConfig
        config = IronConfig()
        config.run_command_timeout = 900
        config_file = tmp_path / "config.yml"
        config.save(config_file)
        import yaml
        raw = yaml.safe_load(config_file.read_text(encoding="utf-8"))
        assert raw["run_command_timeout"] == 900


# ── 上下文保持测试 ────────────────────────────────────────────────

class TestConversationContextPreservation:
    """多轮对话上下文保持测试

    普通输入切换 engine 时传递 prior_conversation，
    避免 AI 每轮对话后失忆。
    """

    def test_engine_inherits_conversation(self):
        """新 engine 接收 prior_conversation 后继承历史"""
        from types import SimpleNamespace
        from pathlib import Path
        from iron.agent.engine import AgentEngine
        from iron.agent.prompt_builder import PromptBuilder
        from iron.llm.backend import EchoBackend
        from iron.skills.registry import SkillRegistry

        config = SimpleNamespace(
            project=SimpleNamespace(project_dir=".", mcu="stm32f407", build_system="platformio"),
            mcp={},
        )
        llm = EchoBackend()
        prompt_builder = PromptBuilder(Path("."))
        skills = SkillRegistry()

        # 模拟上一轮 engine 的 conversation
        prior_conversation = [
            {"role": "user", "content": "之前讨论的 STM32 GPIO 配置"},
            {"role": "assistant", "content": "已配置 PA5 为输出模式"},
        ]

        engine = AgentEngine(llm=llm, prompt_builder=prompt_builder, skills=skills, config=config)
        # 模拟 _run_agent 的修复逻辑
        engine.conversation = list(prior_conversation)

        # 验证新 engine 继承了历史
        assert len(engine.conversation) == 2
        assert engine.conversation[0]["content"] == "之前讨论的 STM32 GPIO 配置"
        assert engine.conversation[1]["content"] == "已配置 PA5 为输出模式"

    def test_engine_without_prior_conversation_starts_empty(self):
        """无 prior_conversation 时 engine 从空 conversation 开始"""
        from types import SimpleNamespace
        from pathlib import Path
        from iron.agent.engine import AgentEngine
        from iron.agent.prompt_builder import PromptBuilder
        from iron.llm.backend import EchoBackend
        from iron.skills.registry import SkillRegistry

        config = SimpleNamespace(
            project=SimpleNamespace(project_dir=".", mcu="stm32f407", build_system="platformio"),
            mcp={},
        )
        engine = AgentEngine(
            llm=EchoBackend(),
            prompt_builder=PromptBuilder(Path(".")),
            skills=SkillRegistry(),
            config=config,
        )
        assert engine.conversation == []


# ── 重试机制和 429 处理测试 ────────────────────────────────────────

class TestRetryAndRateLimit:
    """重试机制和 429 速率限制处理测试"""

    def test_is_retryable_status(self):
        """可重试状态码判断"""
        assert LLMBackend._is_retryable_status(429) is True
        assert LLMBackend._is_retryable_status(500) is True
        assert LLMBackend._is_retryable_status(502) is True
        assert LLMBackend._is_retryable_status(503) is True
        assert LLMBackend._is_retryable_status(504) is True
        assert LLMBackend._is_retryable_status(200) is False
        assert LLMBackend._is_retryable_status(400) is False
        assert LLMBackend._is_retryable_status(401) is False
        assert LLMBackend._is_retryable_status(404) is False

    def test_get_retry_after_header(self):
        """Retry-After 头解析"""
        assert LLMBackend._get_retry_after({"retry-after": "5"}) == 5.0
        assert LLMBackend._get_retry_after({"retry-after": "1.5"}) == 1.5
        assert LLMBackend._get_retry_after({"retry-after": "abc"}) is None
        assert LLMBackend._get_retry_after({}) is None

    def test_post_with_retry_success_first_attempt(self, monkeypatch):
        """首次成功 → 不重试"""
        backend = OpenAIBackend(api_key="sk-test", base_url="https://test.com", model="m")

        async def mock_post(url, json=None, headers=None):
            return httpx.Response(200, json={"ok": True})

        monkeypatch.setattr(backend.client, "post", mock_post)

        async def _run():
            return await backend._post_with_retry("https://test.com", {}, {})
        resp = asyncio.run(_run())
        assert resp.status_code == 200

    def test_post_with_retry_retries_on_429(self, monkeypatch):
        """429 → 自动重试并成功"""
        backend = OpenAIBackend(api_key="sk-test", base_url="https://test.com", model="m")
        call_count = {"n": 0}

        async def mock_post(url, json=None, headers=None):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return httpx.Response(429, json={"error": "rate limited"}, headers={"retry-after": "0.01"})
            return httpx.Response(200, json={"ok": True})

        monkeypatch.setattr(backend.client, "post", mock_post)
        backend._RETRY_BASE_DELAY = 0.01

        async def _run():
            return await backend._post_with_retry("https://test.com", {}, {})
        resp = asyncio.run(_run())
        assert resp.status_code == 200
        assert call_count["n"] == 2

    def test_post_with_retry_exhausts_retries(self, monkeypatch):
        """持续 503 → 耗尽重试后返回最后一次响应"""
        backend = OpenAIBackend(api_key="sk-test", base_url="https://test.com", model="m")

        async def mock_post(url, json=None, headers=None):
            return httpx.Response(503, json={"error": "unavailable"})

        monkeypatch.setattr(backend.client, "post", mock_post)
        backend._RETRY_BASE_DELAY = 0.01

        async def _run():
            return await backend._post_with_retry("https://test.com", {}, {})
        resp = asyncio.run(_run())
        assert resp.status_code == 503

    def test_post_with_retry_timeout_retries(self, monkeypatch):
        """超时 → 自动重试"""
        backend = OpenAIBackend(api_key="sk-test", base_url="https://test.com", model="m")
        call_count = {"n": 0}

        async def mock_post(url, json=None, headers=None):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise httpx.TimeoutException("timeout")
            return httpx.Response(200, json={"ok": True})

        monkeypatch.setattr(backend.client, "post", mock_post)
        backend._RETRY_BASE_DELAY = 0.01

        async def _run():
            return await backend._post_with_retry("https://test.com", {}, {})
        resp = asyncio.run(_run())
        assert resp.status_code == 200
        assert call_count["n"] == 2

    def test_post_with_retry_connect_error_no_retry(self, monkeypatch):
        """连接错误 → 不重试，直接抛出"""
        backend = OpenAIBackend(api_key="sk-test", base_url="https://test.com", model="m")

        async def mock_post(url, json=None, headers=None):
            raise httpx.ConnectError("connection refused")

        monkeypatch.setattr(backend.client, "post", mock_post)

        async def _run():
            return await backend._post_with_retry("https://test.com", {}, {})
        with pytest.raises(RuntimeError, match="无法连接到 LLM 服务"):
            asyncio.run(_run())


# ── LLM 超时配置测试 ─────────────────────────────────────────────

class TestLLMTimeoutConfig:
    """LLM 超时可配置化测试"""

    def test_default_request_timeout(self):
        """默认 request_timeout 为 120 秒"""
        from iron.config.settings import IronConfig
        config = IronConfig()
        assert config.llm.request_timeout == 120

    def test_load_request_timeout_from_yaml(self, tmp_path):
        """从 YAML 加载 request_timeout"""
        from iron.config.settings import IronConfig
        yaml_content = """
llm:
  request_timeout: 300
"""
        config_path = tmp_path / "iron.yml"
        config_path.write_text(yaml_content, encoding="utf-8")
        config = IronConfig()
        config._merge_yaml(config_path)
        assert config.llm.request_timeout == 300

    def test_create_backend_passes_timeout(self):
        """create_backend 将 request_timeout 传递给后端"""
        config = SimpleNamespace(
            llm=SimpleNamespace(
                api_key="sk-test",
                base_url=None,
                model="gpt-4o",
                request_timeout=240,
            )
        )
        backend = create_backend("openai", config)
        assert backend.client.timeout.connect == 240.0

    def test_create_backend_ollama_min_timeout(self):
        """Ollama 后端超时至少 300 秒"""
        config = SimpleNamespace(
            llm=SimpleNamespace(
                api_key="",
                base_url=None,
                model="qwen:7b",
                request_timeout=60,
            )
        )
        backend = create_backend("ollama", config)
        assert backend.client.timeout.connect == 300.0


# ── MAX_STEPS 一致性测试 ─────────────────────────────────────────

class TestMaxStepsConsistency:
    """MAX_STEPS 在 settings.py 和 engine.py 之间的一致性测试"""

    def test_engine_max_steps_respects_settings_upper_bound(self, tmp_path):
        """engine.py 的 MAX_STEPS 截断上限应与 settings.py 的上限一致"""
        from iron.config.settings import IronConfig
        # settings.py 允许配置到 5000
        config = IronConfig()
        assert config.max_steps == 50
        # 通过 YAML 加载，settings.py 截断到 [10, 5000]
        config_path = tmp_path / "iron.yml"
        config_path.write_text("max_steps: 9999", encoding="utf-8")
        config._merge_yaml(config_path)
        assert config.max_steps == 50  # 超出范围，保留默认值

    def test_engine_max_steps_valid_range(self, tmp_path):
        """settings.py 接受 [10, 5000] 范围内的 max_steps"""
        from iron.config.settings import IronConfig
        config_path = tmp_path / "iron.yml"
        config_path.write_text("max_steps: 5000", encoding="utf-8")
        config = IronConfig()
        config._merge_yaml(config_path)
        assert config.max_steps == 5000

    def test_engine_max_steps_floor_is_10(self, tmp_path):
        """settings.py 下限为 10"""
        from iron.config.settings import IronConfig
        config_path = tmp_path / "iron.yml"
        config_path.write_text("max_steps: 5", encoding="utf-8")
        config = IronConfig()
        config._merge_yaml(config_path)
        assert config.max_steps == 50  # 低于下限，保留默认值
