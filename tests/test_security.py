"""安全修复测试 — 覆盖 path_guard / SSRF / API Key / 命令注入"""
import os
import tempfile
from pathlib import Path

import pytest

from iron.tools.path_guard import validate_path_in_project


class TestPathGuard:
    """路径穿越防护测试"""

    def setup_method(self):
        # 使用解析后的绝对路径，避免 Windows 长路径前缀差异导致的断言失败
        self.project_dir = str(Path(tempfile.mkdtemp()).resolve())

    def test_normal_relative_path(self):
        """正常相对路径通过"""
        # allow_create=True 时父目录必须存在，预先创建 src/ 子目录
        (Path(self.project_dir) / "src").mkdir(exist_ok=True)
        result = validate_path_in_project("src/main.c", self.project_dir, allow_create=True)
        assert str(result).startswith(self.project_dir)

    def test_dotdot_traversal_blocked(self):
        """../ 穿越被拦截"""
        # 在项目目录同级创建真实文件，使穿越路径的父目录存在，从而命中"路径越界"边界校验
        outside_dir = Path(self.project_dir).parent / "embedguard_test_outside"
        outside_dir.mkdir(exist_ok=True)
        outside_file = outside_dir / "secret.txt"
        outside_file.write_text("secret")
        try:
            with pytest.raises(ValueError, match="路径越界"):
                validate_path_in_project(
                    f"../{outside_dir.name}/secret.txt",
                    self.project_dir,
                    allow_create=True,
                )
        finally:
            outside_file.unlink(missing_ok=True)
            try:
                outside_dir.rmdir()
            except OSError:
                pass

    def test_absolute_path_outside_blocked(self):
        """绝对路径越界被拦截"""
        with pytest.raises(ValueError):
            validate_path_in_project("/etc/passwd", self.project_dir, allow_create=True)

    def test_empty_path_blocked(self):
        """空路径被拦截"""
        with pytest.raises(ValueError, match="不能为空"):
            validate_path_in_project("", self.project_dir, allow_create=True)

    def test_allow_create_nonexistent(self):
        """allow_create=True 允许不存在的路径"""
        result = validate_path_in_project("new_file.txt", self.project_dir, allow_create=True)
        assert not result.exists()

    def test_no_allow_create_requires_existence(self):
        """allow_create=False 要求路径存在"""
        with pytest.raises((ValueError, FileNotFoundError)):
            validate_path_in_project("nonexistent.txt", self.project_dir, allow_create=False)

    def test_symlink_outside_blocked(self):
        """符号链接指向项目外被拦截"""
        # 创建符号链接（跳过 Windows 无权限的情况）
        outside = tempfile.mkdtemp()
        outside_file = Path(outside) / "secret.txt"
        outside_file.write_text("secret")

        link_path = Path(self.project_dir) / "link.txt"
        try:
            link_path.symlink_to(outside_file)
            with pytest.raises((ValueError, FileNotFoundError)):
                validate_path_in_project("link.txt", self.project_dir, allow_create=False)
        except (OSError, NotImplementedError):
            pytest.skip("无法创建符号链接")


class TestSSRFProtection:
    """SSRF 防护测试"""

    def test_is_safe_url_private_ip(self):
        """私有 IP 被拦截"""
        from iron.tools.web_search import WebSearchTool
        tool = WebSearchTool()
        assert not tool._is_safe_url("http://127.0.0.1/")
        assert not tool._is_safe_url("http://10.0.0.1/")
        assert not tool._is_safe_url("http://172.16.0.1/")
        assert not tool._is_safe_url("http://192.168.1.1/")

    def test_is_safe_url_loopback(self):
        """环回地址被拦截"""
        from iron.tools.web_search import WebSearchTool
        tool = WebSearchTool()
        assert not tool._is_safe_url("http://localhost/")

    def test_is_safe_url_unspecified(self):
        """未指定地址被拦截"""
        from iron.tools.web_search import WebSearchTool
        tool = WebSearchTool()
        assert not tool._is_safe_url("http://0.0.0.0/")

    def test_is_safe_url_link_local(self):
        """链路本地地址被拦截"""
        from iron.tools.web_search import WebSearchTool
        tool = WebSearchTool()
        assert not tool._is_safe_url("http://169.254.1.1/")

    def test_is_safe_url_public(self):
        """公网地址通过"""
        from iron.tools.web_search import WebSearchTool
        tool = WebSearchTool()
        assert tool._is_safe_url("http://93.184.216.34/")
        assert tool._is_safe_url("https://example.com/")

    def test_is_safe_url_ipv4_mapped_ipv6_blocked(self):
        """IPv4-mapped IPv6 形式（::ffff:127.0.0.1）被拦截"""
        from iron.tools.web_search import WebSearchTool
        tool = WebSearchTool()
        # 标准方括号格式 — urlparse 才能解析出 hostname，触发 IPv4-mapped 检测分支
        # ::ffff:127.0.0.1 应被归一化为 127.0.0.1 并按 loopback 拦截
        assert not tool._is_safe_url("http://[::ffff:127.0.0.1]/")
        assert not tool._is_safe_url("http://[::ffff:10.0.0.1]/")
        assert not tool._is_safe_url("http://[::ffff:192.168.1.1]/")

    def test_is_safe_url_decimal_ipv4_blocked(self):
        """十进制 IPv4 形式（2130706433 = 127.0.0.1）被拦截"""
        from iron.tools.web_search import WebSearchTool
        tool = WebSearchTool()
        assert not tool._is_safe_url("http://2130706433/")
        # 注意：这个测试只有在修复后才会通过，所以如果修复未完成会失败

    def test_is_safe_url_hex_ipv4_blocked(self):
        """十六进制 IPv4 形式（0x7f000001 = 127.0.0.1）被拦截"""
        from iron.tools.web_search import WebSearchTool
        tool = WebSearchTool()
        assert not tool._is_safe_url("http://0x7f000001/")

    def test_is_safe_url_localhost_dot_blocked(self):
        """trailing-dot localhost 被拦截"""
        from iron.tools.web_search import WebSearchTool
        tool = WebSearchTool()
        assert not tool._is_safe_url("http://localhost./")
        assert not tool._is_safe_url("http://localhost.")


class TestAPIKeySecurity:
    """API Key 安全测试"""

    def test_api_key_not_persisted(self, tmp_path):
        """API Key 不落盘"""
        from iron.config.settings import IronConfig
        config = IronConfig()
        config.llm.api_key = "sk-test-secret-key-12345"
        config.llm.backend = "openai"

        # 保存（save 接受 Path 对象）
        config.save(tmp_path / "test_config.yml")

        # 读取文件内容，确认 api_key 为空
        content = (tmp_path / "test_config.yml").read_text()
        assert "sk-test-secret-key-12345" not in content

    def test_env_var_priority(self, monkeypatch):
        """环境变量优先加载"""
        monkeypatch.setenv("IRON_API_KEY", "sk-from-env")
        from iron.config.settings import IronConfig
        # load 接受 Path，传入一个空临时目录避免读取项目配置
        config = IronConfig.load(Path(tempfile.mkdtemp()))
        # 环境变量应该覆盖文件值
        assert config.llm.api_key == "sk-from-env"


class TestCommandInjection:
    """命令注入防护测试 — 真正调用 _evaluate_command_risk"""

    def _make_engine(self, tmp_path):
        """创建测试用 AgentEngine 实例（无网络/MCP）

        _evaluate_command_risk 是实例方法，且 _CMD_METACHARS 是方法内局部变量
        （非模块级常量），因此必须构造 engine 实例后调用。
        使用与 tests/test_engine.py 一致的 EchoBackend + SimpleNamespace 配置模式。
        """
        from types import SimpleNamespace

        from iron.agent.engine import AgentEngine
        from iron.agent.prompt_builder import PromptBuilder
        from iron.llm.backend import EchoBackend
        from iron.skills.registry import SkillRegistry

        config = SimpleNamespace(
            project=SimpleNamespace(
                project_dir=str(tmp_path),
                mcu="stm32f407",
                build_system="platformio",
            ),
            mcp={},
        )
        llm = EchoBackend()
        prompt_builder = PromptBuilder(Path(tmp_path))
        skills = SkillRegistry()
        engine = AgentEngine(
            llm=llm,
            prompt_builder=prompt_builder,
            skills=skills,
            config=config,
        )
        return engine

    def test_metachar_newline_blocked(self, tmp_path):
        """换行符命令注入被识别为 dangerous"""
        engine = self._make_engine(tmp_path)
        result = engine._evaluate_command_risk("echo safe\ntype secret.txt")
        assert result == "dangerous", f"换行符未被检测为危险: {result}"

    def test_metachar_backtick_blocked(self, tmp_path):
        """反引号命令注入被识别为 dangerous"""
        engine = self._make_engine(tmp_path)
        result = engine._evaluate_command_risk("echo `whoami`")
        assert result == "dangerous"

    def test_metachar_dollar_paren_blocked(self, tmp_path):
        """$() 子shell 注入被拦截"""
        engine = self._make_engine(tmp_path)
        result = engine._evaluate_command_risk("echo $(whoami)")
        assert result == "dangerous"

    def test_metachar_redirect_blocked(self, tmp_path):
        """重定向 > 被拦截"""
        engine = self._make_engine(tmp_path)
        result = engine._evaluate_command_risk("echo hi > /etc/passwd")
        assert result == "dangerous"

    def test_metachar_null_byte_blocked(self, tmp_path):
        """NULL 字节被拦截"""
        engine = self._make_engine(tmp_path)
        result = engine._evaluate_command_risk("echo\x00rm")
        assert result == "dangerous"

    def test_safe_command_echo(self, tmp_path):
        """安全命令 echo 被识别为 safe"""
        engine = self._make_engine(tmp_path)
        result = engine._evaluate_command_risk("echo hello")
        assert result == "safe"

    def test_python_c_blocked(self, tmp_path):
        """python -c 任意代码被拦截"""
        engine = self._make_engine(tmp_path)
        assert engine._evaluate_command_risk('python -c "import os"') == "dangerous"
        assert engine._evaluate_command_risk('python3 -c "import os"') == "dangerous"
        assert engine._evaluate_command_risk('py -c "import os"') == "dangerous"

    def test_python_c_bypass_variants_blocked(self, tmp_path):
        r"""第六轮 P0 回归：python -c 绕过形式全部被拦截

        第五轮正则 `(^|\s)(-c|--command)(\s|$)` 要求 -c 后跟空格/行尾，
        无法匹配 python -cprint(...)、python -c'code'、python -c"code" 等合法形式。
        第六轮改为 `(^|\s)-c` 子串匹配，覆盖所有绕过。
        """
        engine = self._make_engine(tmp_path)
        # -c 后直接跟代码（无空格）
        assert engine._evaluate_command_risk("python -cprint(1)") == "dangerous"
        assert engine._evaluate_command_risk("python3 -cprint(1)") == "dangerous"
        # -c 后跟引号
        assert engine._evaluate_command_risk("python -c'print(1)'") == "dangerous"
        assert engine._evaluate_command_risk('python -c"print(1)"') == "dangerous"
        # 敏感操作绕过尝试
        assert engine._evaluate_command_risk("python -cprint(open('.env').read())") == "dangerous"
        assert engine._evaluate_command_risk("python -cprint(__import__('os').system('whoami'))") == "dangerous"
        # --command 变体
        assert engine._evaluate_command_risk("python --command code") == "dangerous"
        # 误报检查：--config 不应被误判（-c 前导是 -，非空格）
        assert engine._evaluate_command_risk("python --config config.json") != "dangerous" or \
               engine._evaluate_command_risk("python --config config.json") == "safe"

    def test_node_e_blocked(self, tmp_path):
        """node -e 任意代码被拦截"""
        engine = self._make_engine(tmp_path)
        assert engine._evaluate_command_risk('node -e "console.log(1)"') == "dangerous"

    def test_node_e_bypass_variants_blocked(self, tmp_path):
        """第六轮 P0 回归：node -e 绕过形式全部被拦截"""
        engine = self._make_engine(tmp_path)
        assert engine._evaluate_command_risk("node -eprint(1)") == "dangerous"
        assert engine._evaluate_command_risk("node -e'code'") == "dangerous"
        assert engine._evaluate_command_risk('node -e"code"') == "dangerous"
        assert engine._evaluate_command_risk("node --eval code") == "dangerous"

    def test_echo_percent_blocked(self, tmp_path):
        """echo %VAR% 环境变量泄漏被拦截"""
        engine = self._make_engine(tmp_path)
        assert engine._evaluate_command_risk("echo %PATH%") == "dangerous"
        assert engine._evaluate_command_risk("echo %API_KEY%") == "dangerous"


class TestEvaluateWriteRisk:
    """_evaluate_write_risk 测试 — 敏感文件检测

    v2.3.1 changelog 强调"写文件读取移到授权后 + dangerous 路径硬阻断"，
    本测试覆盖 .env / credentials / secret / password 等敏感文件名识别。
    """

    def _make_engine(self, tmp_path):
        from types import SimpleNamespace

        from iron.agent.engine import AgentEngine
        from iron.agent.prompt_builder import PromptBuilder
        from iron.llm.backend import EchoBackend
        from iron.skills.registry import SkillRegistry

        config = SimpleNamespace(
            project=SimpleNamespace(
                project_dir=str(tmp_path),
                mcu="stm32f407",
                build_system="platformio",
            ),
            mcp={},
        )
        llm = EchoBackend()
        prompt_builder = PromptBuilder(Path(tmp_path))
        skills = SkillRegistry()
        engine = AgentEngine(
            llm=llm,
            prompt_builder=prompt_builder,
            skills=skills,
            config=config,
        )
        return engine

    def test_dangerous_env_file(self, tmp_path):
        engine = self._make_engine(tmp_path)
        assert engine._evaluate_write_risk(".env") == "dangerous"
        assert engine._evaluate_write_risk(".env.local") == "dangerous"

    def test_dangerous_credentials_file(self, tmp_path):
        engine = self._make_engine(tmp_path)
        assert engine._evaluate_write_risk("credentials") == "dangerous"
        assert engine._evaluate_write_risk("secret") == "dangerous"
        assert engine._evaluate_write_risk("password") == "dangerous"

    def test_safe_normal_file(self, tmp_path):
        engine = self._make_engine(tmp_path)
        assert engine._evaluate_write_risk("src/main.c") == "safe"
        assert engine._evaluate_write_risk("README.md") == "safe"
