"""P1/P2 增强功能测试 — 覆盖 doom_loop 升级、敏感文件扩展、API Key 脱敏增强、Circuit Breaker

运行方式: pytest tests/test_p1_p2_enhancements.py -v
"""
import asyncio
import time
import pytest

from iron.llm.backend import LLMBackend, CircuitBreaker
from iron.agent.engine import AgentEngine, _LOWER_SENSITIVE_NAMES, _SENSITIVE_SUFFIX_PATTERNS


# ── doom_loop 循环模式检测测试 ──────────────────────────────────

class TestDoomLoopPatternDetection:
    """P1-5: doom_loop 检测升级 — 循环模式检测"""

    def _make_engine(self):
        """构造最小可用的 AgentEngine 实例"""
        from iron.agent.prompt_builder import PromptBuilder
        from iron.llm.backend import EchoBackend
        from iron.skills.registry import SkillRegistry
        from pathlib import Path
        from types import SimpleNamespace

        config = SimpleNamespace(
            project=SimpleNamespace(project_dir=".", mcu="stm32f407", build_system="platformio"),
            mcp={},
            max_steps=50,
        )
        return AgentEngine(
            llm=EchoBackend(),
            prompt_builder=PromptBuilder(Path(".")),
            skills=SkillRegistry(),
            config=config,
        )

    def test_consecutive_same_calls_triggers(self):
        """连续 3 次相同调用 → 触发"""
        engine = self._make_engine()
        # 前两次不触发
        assert engine._check_doom_loop("write_file", {"path": "a.c"}) is False
        assert engine._check_doom_loop("write_file", {"path": "a.c"}) is False
        # 第三次触发
        assert engine._check_doom_loop("write_file", {"path": "a.c"}) is True

    def test_pattern_length_2_triggers(self):
        """A→B→A→B 循环模式 → 触发"""
        engine = self._make_engine()
        # A, B, A, B 应触发（长度 2 的循环模式）
        assert engine._check_doom_loop("write_file", {"path": "a.c"}) is False
        assert engine._check_doom_loop("write_file", {"path": "b.c"}) is False
        assert engine._check_doom_loop("write_file", {"path": "a.c"}) is False
        # 第四次 A→B→A→B 形成长度 2 的循环
        assert engine._check_doom_loop("write_file", {"path": "b.c"}) is True

    def test_pattern_length_3_triggers(self):
        """A→B→C→A→B→C 循环模式 → 触发"""
        engine = self._make_engine()
        # 6 次调用形成 A→B→C→A→B→C
        for path in ["a.c", "b.c", "c.c"]:
            assert engine._check_doom_loop("write_file", {"path": path}) is False
        # 第二轮 A→B→C 应在最后一次触发
        assert engine._check_doom_loop("write_file", {"path": "a.c"}) is False
        assert engine._check_doom_loop("write_file", {"path": "b.c"}) is False
        assert engine._check_doom_loop("write_file", {"path": "c.c"}) is True

    def test_no_pattern_does_not_trigger(self):
        """不同调用不触发"""
        engine = self._make_engine()
        for i in range(10):
            assert engine._check_doom_loop("write_file", {"path": f"file_{i}.c"}) is False

    def test_signature_extended_to_200_chars(self):
        """签名扩展到 200 字符，长参数不被误判"""
        engine = self._make_engine()
        # 两个长内容不同的调用不应被误判为相同
        long_args_1 = {"content": "A" * 200}
        long_args_2 = {"content": "B" * 200}
        assert engine._check_doom_loop("write_file", long_args_1) is False
        assert engine._check_doom_loop("write_file", long_args_2) is False
        assert engine._check_doom_loop("write_file", long_args_1) is False
        # 第四次不触发（因为前 3 次不构成循环）
        # 但如果再 A→B→A→B 会触发
        result = engine._check_doom_loop("write_file", long_args_2)
        assert result is True  # A→B→A→B 循环


# ── 敏感文件扩展测试 ──────────────────────────────────────────

class TestSensitiveFileExpansion:
    """P2-6: 敏感文件列表扩展"""

    def test_ssh_keys_blocked(self):
        """SSH 私钥被拦截"""
        assert "id_rsa" in _LOWER_SENSITIVE_NAMES
        assert "id_ed25519" in _LOWER_SENSITIVE_NAMES
        assert "id_ecdsa" in _LOWER_SENSITIVE_NAMES
        assert "id_dsa" in _LOWER_SENSITIVE_NAMES

    def test_auth_files_blocked(self):
        """认证文件被拦截"""
        assert ".htpasswd" in _LOWER_SENSITIVE_NAMES
        assert ".netrc" in _LOWER_SENSITIVE_NAMES
        assert ".gitconfig" in _LOWER_SENSITIVE_NAMES

    def test_sensitive_extensions(self):
        """敏感扩展名正则匹配"""
        import re
        test_files = [
            ("server.pem", True),
            ("private.key", True),
            ("cert.p12", True),
            ("store.pfx", True),
            ("app.keystore", True),
            ("main.c", False),
            ("README.md", False),
            ("config.h", False),
        ]
        for filename, should_match in test_files:
            matched = any(pat.search(filename.lower()) for pat in _SENSITIVE_SUFFIX_PATTERNS)
            assert matched == should_match, f"{filename}: expected {should_match}, got {matched}"


# ── API Key 脱敏增强测试 ──────────────────────────────────────

class TestAPISanitizationEnhanced:
    """P2-6: API Key 脱敏增强 — JWT/AWS/Google/GitHub"""

    def test_openai_key_redacted(self):
        """OpenAI sk- key 被脱敏"""
        text = "Error: sk-abcdefghijklmnopqrstuvwxyz123456 invalid"
        result = LLMBackend._sanitize_error(text)
        assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in result
        assert "***REDACTED***" in result

    def test_bearer_token_redacted(self):
        """Bearer token 被脱敏"""
        text = "Authorization: Bearer abc123token"
        result = LLMBackend._sanitize_error(text)
        assert "abc123token" not in result
        assert "***REDACTED***" in result

    def test_jwt_redacted(self):
        """JWT token 被脱敏"""
        # 简化的 JWT 格式：eyJ.payload.signature
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature123456"
        text = f"Token: {jwt}"
        result = LLMBackend._sanitize_error(text)
        assert jwt not in result
        assert "***REDACTED***" in result

    def test_aws_key_redacted(self):
        """AWS Access Key 被脱敏"""
        aws_key = "AKIAIOSFODNN7EXAMPLE"
        text = f"AWS key: {aws_key}"
        result = LLMBackend._sanitize_error(text)
        assert aws_key not in result
        assert "***REDACTED***" in result

    def test_google_api_key_redacted(self):
        """Google API key 被脱敏"""
        google_key = "AIzaSyD-9tSrke7PXdLxdYWvA-9tSrke7PXdLxdYWvA-9tSrke7"
        # 上面这个不满足 35 字符后缀，构造一个满足的
        google_key = "AIza" + "A" * 35
        text = f"Google API: {google_key}"
        result = LLMBackend._sanitize_error(text)
        assert google_key not in result
        assert "***REDACTED***" in result

    def test_github_token_redacted(self):
        """GitHub token 被脱敏"""
        github_token = "ghp_" + "A" * 36
        text = f"GitHub token: {github_token}"
        result = LLMBackend._sanitize_error(text)
        assert github_token not in result
        assert "***REDACTED***" in result

    def test_chinese_text_not_corrupted(self):
        """中文字符串不被误脱敏（回归测试）"""
        text = "连接失败"
        result = LLMBackend._sanitize_error(text)
        assert result == "连接失败", f"中文字符被误脱敏: {result}"

    def test_normal_error_preserved(self):
        """普通错误信息保留"""
        text = "FileNotFoundError: main.c not found"
        result = LLMBackend._sanitize_error(text)
        assert result == text


# ── Circuit Breaker 测试 ──────────────────────────────────────

class TestCircuitBreaker:
    """P2-4: Circuit Breaker 熔断器"""

    def test_initial_state_closed(self):
        """初始状态为 closed"""
        cb = CircuitBreaker(failure_limit=5, reset_timeout=30)
        assert cb.state == "closed"
        assert cb.can_execute() is True

    def test_record_success_resets_failures(self):
        """成功重置失败计数"""
        cb = CircuitBreaker(failure_limit=3, reset_timeout=30)
        cb.record_failure()
        cb.record_failure()
        assert cb.failures == 2
        cb.record_success()
        assert cb.failures == 0
        assert cb.state == "closed"

    def test_opens_after_failure_limit(self):
        """达到失败上限后熔断"""
        cb = CircuitBreaker(failure_limit=3, reset_timeout=30)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == "closed"
        cb.record_failure()  # 第 3 次失败
        assert cb.state == "open"
        assert cb.can_execute() is False

    def test_half_open_after_timeout(self):
        """超时后转 half-open，允许一次试探"""
        cb = CircuitBreaker(failure_limit=1, reset_timeout=0.1)  # 100ms 超时
        cb.record_failure()  # 立即熔断
        assert cb.state == "open"
        assert cb.can_execute() is False
        # 等待超时
        time.sleep(0.15)
        assert cb.can_execute() is True
        assert cb.state == "half-open"

    def test_half_open_success_closes(self):
        """half-open 成功后恢复 closed"""
        cb = CircuitBreaker(failure_limit=1, reset_timeout=0.1)
        cb.record_failure()
        time.sleep(0.15)
        cb.can_execute()  # 转 half-open
        cb.record_success()
        assert cb.state == "closed"
        assert cb.failures == 0

    def test_half_open_failure_reopens(self):
        """half-open 失败后重新 open"""
        cb = CircuitBreaker(failure_limit=1, reset_timeout=0.1)
        cb.record_failure()
        time.sleep(0.15)
        cb.can_execute()  # 转 half-open
        cb.record_failure()  # 试探失败
        assert cb.state == "open"

    def test_backend_has_circuit_breaker(self):
        """LLMBackend 实例有熔断器"""
        from iron.llm.backend import EchoBackend
        backend = EchoBackend()
        assert hasattr(backend, "_circuit")
        assert isinstance(backend._circuit, CircuitBreaker)
        assert backend._circuit.can_execute() is True

    def test_openai_backend_has_circuit_breaker(self):
        """OpenAIBackend 实例有熔断器（super().__init__ 被调用）"""
        from iron.llm.backend import OpenAIBackend
        backend = OpenAIBackend(api_key="test-key")
        assert hasattr(backend, "_circuit")
        assert isinstance(backend._circuit, CircuitBreaker)

    def test_circuit_breaker_blocks_request_when_open(self):
        """熔断状态下 _post_with_retry 直接拒绝"""
        from iron.llm.backend import OpenAIBackend
        import httpx

        backend = OpenAIBackend(api_key="test-key")
        # 强制熔断
        backend._circuit.state = "open"
        backend._circuit.failures = 5
        backend._circuit.last_failure = time.monotonic()  # 刚失败

        async def _run():
            return await backend._post_with_retry("https://test.com", {}, {})

        with pytest.raises(RuntimeError, match="熔断"):
            asyncio.run(_run())
