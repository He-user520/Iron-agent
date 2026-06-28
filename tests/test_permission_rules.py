"""P2-1 规则评估引擎测试 — DSL 驱动的权限规则

覆盖：
- RuleCondition.matches() 四类匹配维度（tool/file_pattern/args_contains/args_regex）
- PermissionRuleEngine.add_rule / evaluate / load_default_rules / load_rules(YAML)
- 优先级：deny > ask > allow
- 嵌入式默认规则：*.ld 禁止写、startup 需确认、embed_flash 需确认、SystemInit 需确认

运行方式: pytest tests/test_permission_rules.py -v
"""
import os
import textwrap
import time

import pytest

from iron.rules.permission_rules import (
    PermissionRule,
    PermissionRuleEngine,
    RuleCondition,
    RuleDecision,
)


# ── RuleCondition.matches() 单元测试 ──────────────────────────


class TestRuleConditionToolMatch:
    """工具名 glob 匹配"""

    def test_exact_tool_match(self):
        """精确工具名匹配"""
        cond = RuleCondition(tool="embed_flash")
        assert cond.matches("embed_flash", {}) is True

    def test_wildcard_tool_match(self):
        """通配符 * 匹配任意工具"""
        cond = RuleCondition(tool="*")
        assert cond.matches("write_file", {}) is True
        assert cond.matches("embed_flash", {}) is True
        assert cond.matches("any_tool", {}) is True

    def test_prefix_glob_tool_match(self):
        """前缀 glob 匹配 embed_* 工具族"""
        cond = RuleCondition(tool="embed_*")
        assert cond.matches("embed_flash", {}) is True
        assert cond.matches("embed_build", {}) is True
        assert cond.matches("embed_lint", {}) is True

    def test_tool_no_match(self):
        """不匹配的工具名返回 False"""
        cond = RuleCondition(tool="embed_flash")
        assert cond.matches("write_file", {}) is False
        assert cond.matches("read_file", {}) is False


class TestRuleConditionFilePattern:
    """文件路径 glob 匹配"""

    def test_ld_file_pattern(self):
        """*.ld 文件路径匹配"""
        cond = RuleCondition(file_pattern="*.ld")
        assert cond.matches("write_file", {"file": "linker.ld"}) is True
        assert cond.matches("write_file", {"path": "STM32F407.ld"}) is True
        assert cond.matches("write_file", {"filename": "memmap.ld"}) is True

    def test_startup_file_pattern(self):
        """*startup*.s 文件路径匹配"""
        cond = RuleCondition(file_pattern="*startup*.s")
        assert cond.matches("write_file", {"file": "startup_stm32f407.s"}) is True
        assert cond.matches("write_file", {"file": "crt0_startup.s"}) is True

    def test_file_pattern_no_match(self):
        """不匹配的文件扩展名"""
        cond = RuleCondition(file_pattern="*.ld")
        assert cond.matches("write_file", {"file": "main.c"}) is False
        assert cond.matches("write_file", {"file": "header.h"}) is False

    def test_no_file_arg_skips_file_check(self):
        """无 file/path/filename 参数时不匹配带具体 file_pattern 的规则

        规则指定了 *.ld 文件模式，但调用方未传 file 参数 → 不匹配
        （*.ld 规则只对实际操作文件的工具调用生效）
        """
        cond = RuleCondition(file_pattern="*.ld")
        # 工具名默认 * 匹配任意；但规则要求 *.ld 文件，调用方没传 file → 不匹配
        assert cond.matches("search_code", {"pattern": "main"}) is False

    def test_default_file_pattern_matches_no_file_arg(self):
        """file_pattern=* （默认）时，无 file 参数也能匹配"""
        cond = RuleCondition(file_pattern="*")
        assert cond.matches("search_code", {"pattern": "main"}) is True
        assert cond.matches("embed_flash", {"firmware": "app.bin"}) is True


class TestRuleConditionArgsContains:
    """参数键值对精确匹配"""

    def test_args_contains_match(self):
        """args_contains 键值对匹配"""
        cond = RuleCondition(args_contains={"probe": "stlink"})
        assert cond.matches("embed_flash", {"probe": "stlink"}) is True

    def test_args_contains_no_match(self):
        """args_contains 值不匹配"""
        cond = RuleCondition(args_contains={"probe": "stlink"})
        assert cond.matches("embed_flash", {"probe": "jlink"}) is False

    def test_args_contains_missing_key(self):
        """args_contains 缺少键"""
        cond = RuleCondition(args_contains={"probe": "stlink"})
        assert cond.matches("embed_flash", {}) is False

    def test_multiple_args_contains(self):
        """多个键值对必须全部匹配"""
        cond = RuleCondition(args_contains={"probe": "stlink", "action": "write"})
        assert cond.matches("embed_flash", {"probe": "stlink", "action": "write"}) is True
        assert cond.matches("embed_flash", {"probe": "stlink", "action": "read"}) is False


class TestRuleConditionArgsRegex:
    """参数正则匹配"""

    def test_args_regex_match(self):
        """args_regex 匹配 content 中的 SystemInit"""
        cond = RuleCondition(args_regex={"content": "SystemInit"})
        assert cond.matches("write_file", {"content": "void SystemInit(void) {"}) is True
        assert cond.matches("write_file", {"content": "  HAL_SystemInit();"}) is True

    def test_args_regex_no_match(self):
        """args_regex 不匹配"""
        cond = RuleCondition(args_regex={"content": "SystemInit"})
        assert cond.matches("write_file", {"content": "int main() {"}) is False

    def test_args_regex_missing_key(self):
        """args_regex 缺少键时视为不匹配（空字符串不匹配）"""
        cond = RuleCondition(args_regex={"content": "SystemInit"})
        assert cond.matches("write_file", {"path": "main.c"}) is False

    def test_args_regex_partial_match(self):
        """正则部分匹配即可（re.search 语义）"""
        cond = RuleCondition(args_regex={"content": "volatile.*reg"})
        assert cond.matches("write_file", {"content": "  volatile uint32_t *reg;"}) is True


class TestRuleConditionNoMatch:
    """组合条件不匹配"""

    def test_tool_and_file_both_required(self):
        """tool 和 file_pattern 都需满足"""
        cond = RuleCondition(tool="write_file", file_pattern="*.ld")
        # 工具名不匹配
        assert cond.matches("read_file", {"file": "linker.ld"}) is False
        # 文件不匹配
        assert cond.matches("write_file", {"file": "main.c"}) is False
        # 都匹配
        assert cond.matches("write_file", {"file": "linker.ld"}) is True

    def test_all_dimensions_required(self):
        """四类维度全部满足才匹配"""
        cond = RuleCondition(
            tool="embed_*",
            file_pattern="*.bin",
            args_contains={"action": "write"},
            args_regex={"firmware": "app_v"},
        )
        # 全部满足
        assert cond.matches("embed_flash", {
            "file": "firmware.bin", "action": "write", "firmware": "app_v1.0.bin"
        }) is True
        # 任一不满足
        assert cond.matches("embed_flash", {
            "file": "firmware.hex", "action": "write", "firmware": "app_v1.0.bin"
        }) is False  # file_pattern 不匹配


# ── PermissionRuleEngine 单元测试 ──────────────────────────────


class TestEngineAddRule:
    """添加规则"""

    def test_add_rule(self):
        """添加一条规则后 list_rules 包含它"""
        engine = PermissionRuleEngine()
        rule = PermissionRule(
            name="test_rule",
            condition=RuleCondition(tool="write_file"),
            action="deny",
        )
        engine.add_rule(rule)
        rules = engine.list_rules()
        assert len(rules) == 1
        assert rules[0].name == "test_rule"

    def test_add_rule_invalid_action_rejected(self):
        """非法 action 抛 ValueError"""
        engine = PermissionRuleEngine()
        with pytest.raises(ValueError):
            engine.add_rule(PermissionRule(name="bad", action="invalid"))

    def test_clear_rules(self):
        """clear_rules 清空规则列表"""
        engine = PermissionRuleEngine()
        engine.add_rule(PermissionRule(name="r1", action="deny"))
        engine.add_rule(PermissionRule(name="r2", action="ask"))
        assert len(engine.list_rules()) == 2
        engine.clear_rules()
        assert len(engine.list_rules()) == 0


class TestEngineEvaluateDeny:
    """deny 优先级测试"""

    def test_deny_overrides_ask(self):
        """deny 规则优先于 ask 规则"""
        engine = PermissionRuleEngine()
        engine.add_rule(PermissionRule(
            name="ask_rule",
            condition=RuleCondition(tool="write_file"),
            action="ask",
        ))
        engine.add_rule(PermissionRule(
            name="deny_rule",
            condition=RuleCondition(tool="write_file"),
            action="deny",
            message="禁止写入",
        ))
        decision = engine.evaluate("write_file", {})
        assert decision.action == "deny"
        assert decision.matched_rule.name == "deny_rule"
        assert "禁止写入" in decision.reason

    def test_deny_returned_immediately(self):
        """匹配 deny 规则直接返回 deny 决策"""
        engine = PermissionRuleEngine()
        engine.add_rule(PermissionRule(
            name="deny_ld",
            condition=RuleCondition(file_pattern="*.ld"),
            action="deny",
            message=".ld 文件禁止修改",
        ))
        decision = engine.evaluate("write_file", {"file": "linker.ld"})
        assert decision.action == "deny"
        assert ".ld" in decision.reason


class TestEngineEvaluateAsk:
    """ask 优先级测试"""

    def test_ask_when_no_deny(self):
        """无 deny 规则匹配时，ask 规则生效"""
        engine = PermissionRuleEngine()
        engine.add_rule(PermissionRule(
            name="ask_startup",
            condition=RuleCondition(file_pattern="*startup*.s"),
            action="ask",
            message="startup 文件需确认",
        ))
        decision = engine.evaluate("write_file", {"file": "startup_stm32.s"})
        assert decision.action == "ask"
        assert decision.matched_rule.name == "ask_startup"

    def test_ask_uses_default_message_when_empty(self):
        """ask 规则无 message 时使用默认原因"""
        engine = PermissionRuleEngine()
        engine.add_rule(PermissionRule(
            name="ask_rule",
            condition=RuleCondition(tool="embed_flash"),
            action="ask",
        ))
        decision = engine.evaluate("embed_flash", {})
        assert decision.action == "ask"
        assert "ask_rule" in decision.reason


class TestEngineEvaluateAllow:
    """allow 默认动作测试"""

    def test_no_match_default_allow(self):
        """无规则匹配时返回默认 allow"""
        engine = PermissionRuleEngine()
        decision = engine.evaluate("write_file", {"file": "main.c"})
        assert decision.action == "allow"
        assert decision.matched_rule is None

    def test_set_default_action_deny(self):
        """set_default_action 改变默认动作"""
        engine = PermissionRuleEngine()
        engine.set_default_action("deny")
        decision = engine.evaluate("any_tool", {})
        assert decision.action == "deny"

    def test_set_default_action_invalid_rejected(self):
        """set_default_action 非法值抛 ValueError"""
        engine = PermissionRuleEngine()
        with pytest.raises(ValueError):
            engine.set_default_action("invalid")


# ── 默认规则测试 ─────────────────────────────────────────────


class TestEngineLoadDefaultRules:
    """加载嵌入式专用默认规则"""

    def test_load_default_rules_count(self):
        """load_default_rules 加载 4 条默认规则"""
        engine = PermissionRuleEngine()
        engine.load_default_rules()
        rules = engine.list_rules()
        assert len(rules) == 4
        names = {r.name for r in rules}
        assert "protect_linker_scripts" in names
        assert "confirm_startup_files" in names
        assert "confirm_flash" in names
        assert "protect_system_init" in names

    def test_default_rules_deny_action_for_ld(self):
        """默认规则中 *.ld 是 deny"""
        engine = PermissionRuleEngine()
        engine.load_default_rules()
        rules = engine.list_rules()
        ld_rule = next(r for r in rules if r.name == "protect_linker_scripts")
        assert ld_rule.action == "deny"
        assert ld_rule.severity == "error"

    def test_default_rules_ask_action_for_flash(self):
        """默认规则中 embed_flash 是 ask"""
        engine = PermissionRuleEngine()
        engine.load_default_rules()
        rules = engine.list_rules()
        flash_rule = next(r for r in rules if r.name == "confirm_flash")
        assert flash_rule.action == "ask"
        assert flash_rule.condition.tool == "embed_flash"


class TestEngineProtectLd:
    """*.ld 文件禁止写"""

    def test_ld_file_denied(self):
        """写入 *.ld 文件被 deny"""
        engine = PermissionRuleEngine()
        engine.load_default_rules()
        decision = engine.evaluate("write_file", {"file": "STM32F407.ld"})
        assert decision.action == "deny"
        assert "链接脚本" in decision.reason or ".ld" in decision.reason

    def test_ld_file_with_path_arg(self):
        """path 参数也能匹配 *.ld"""
        engine = PermissionRuleEngine()
        engine.load_default_rules()
        decision = engine.evaluate("write_file", {"path": "linker/memory.ld"})
        assert decision.action == "deny"

    def test_non_ld_file_allowed(self):
        """非 .ld 文件不被 deny"""
        engine = PermissionRuleEngine()
        engine.load_default_rules()
        decision = engine.evaluate("write_file", {"file": "main.c"})
        assert decision.action == "allow"


class TestEngineProtectStartup:
    """startup 文件需确认"""

    def test_startup_file_ask(self):
        """写入 *startup*.s 文件触发 ask"""
        engine = PermissionRuleEngine()
        engine.load_default_rules()
        decision = engine.evaluate("write_file", {"file": "startup_stm32f407.s"})
        assert decision.action == "ask"
        assert "启动文件" in decision.reason or "startup" in decision.reason.lower()

    def test_non_startup_file_allowed(self):
        """非 startup 文件不触发 ask"""
        engine = PermissionRuleEngine()
        engine.load_default_rules()
        decision = engine.evaluate("write_file", {"file": "main.s"})
        assert decision.action == "allow"


class TestEngineFlashConfirm:
    """flash 工具需确认"""

    def test_embed_flash_ask(self):
        """embed_flash 工具触发 ask"""
        engine = PermissionRuleEngine()
        engine.load_default_rules()
        decision = engine.evaluate("embed_flash", {"firmware": "app.bin"})
        assert decision.action == "ask"
        assert "烧录" in decision.reason

    def test_other_tool_allowed(self):
        """非 embed_flash 工具不触发 ask"""
        engine = PermissionRuleEngine()
        engine.load_default_rules()
        decision = engine.evaluate("read_file", {"file": "main.c"})
        assert decision.action == "allow"


class TestEngineNoMatchDefault:
    """无匹配时默认 allow"""

    def test_unmatched_tool_returns_allow(self):
        """未匹配任何规则的工具返回默认 allow"""
        engine = PermissionRuleEngine()
        engine.load_default_rules()
        decision = engine.evaluate("search_code", {"pattern": "main"})
        assert decision.action == "allow"
        assert decision.matched_rule is None

    def test_write_regular_file_allowed(self):
        """写入普通 .c 文件不被默认规则拦截"""
        engine = PermissionRuleEngine()
        engine.load_default_rules()
        decision = engine.evaluate("write_file", {"file": "src/main.c", "content": "int main(){}"})
        # *.c 不匹配 *.ld 也不匹配 *startup*.s，content 不含 SystemInit
        assert decision.action == "allow"


# ── YAML 加载测试 ─────────────────────────────────────────────


class TestEngineLoadYaml:
    """从 YAML 文件加载规则"""

    def test_load_yaml_rules(self, tmp_path):
        """从 YAML 文件加载自定义规则"""
        yaml_content = textwrap.dedent("""
            default_action: allow
            rules:
              - name: custom_deny_py
                description: 禁止修改 Python 文件
                condition:
                  tool: write_file
                  file_pattern: "*.py"
                action: deny
                severity: error
                message: 禁止自动修改 Python 文件
              - name: custom_ask_test
                condition:
                  tool: run_command
                  args_regex:
                    command: "rm\\\\s+-rf"
                action: ask
                message: 危险命令需确认
        """)
        yaml_file = tmp_path / "rules.yml"
        yaml_file.write_text(yaml_content, encoding="utf-8")

        engine = PermissionRuleEngine()
        count = engine.load_rules(yaml_file)
        assert count == 2

        # 验证 deny 规则生效
        decision = engine.evaluate("write_file", {"file": "test.py"})
        assert decision.action == "deny"
        assert "Python" in decision.reason

        # 验证 ask 规则生效
        decision = engine.evaluate("run_command", {"command": "rm -rf /tmp"})
        assert decision.action == "ask"
        assert "危险命令" in decision.reason

    def test_load_yaml_nonexistent_file(self):
        """加载不存在的文件返回 0（不报错）"""
        engine = PermissionRuleEngine()
        count = engine.load_rules("/nonexistent/path/rules.yml")
        assert count == 0

    def test_load_yaml_invalid_action_skipped(self, tmp_path):
        """YAML 中非法 action 的规则被跳过"""
        yaml_content = textwrap.dedent("""
            rules:
              - name: bad_action
                condition: {tool: write_file}
                action: invalid
              - name: good_rule
                condition: {tool: write_file}
                action: deny
        """)
        yaml_file = tmp_path / "rules.yml"
        yaml_file.write_text(yaml_content, encoding="utf-8")

        engine = PermissionRuleEngine()
        count = engine.load_rules(yaml_file)
        assert count == 1  # 只有 good_rule 被加载

    def test_load_yaml_default_action(self, tmp_path):
        """YAML 中 default_action 字段生效"""
        yaml_content = textwrap.dedent("""
            default_action: deny
            rules: []
        """)
        yaml_file = tmp_path / "rules.yml"
        yaml_file.write_text(yaml_content, encoding="utf-8")

        engine = PermissionRuleEngine()
        engine.load_rules(yaml_file)
        decision = engine.evaluate("any_tool", {})
        assert decision.action == "deny"


# ── 性能测试 ─────────────────────────────────────────────────


class TestRuleEnginePerformance:
    """规则评估性能 — 必须快速（<1ms 每次）"""

    def test_evaluate_under_1ms(self):
        """100 条规则下单次评估 < 1ms"""
        engine = PermissionRuleEngine()
        # 加载 100 条规则
        for i in range(100):
            engine.add_rule(PermissionRule(
                name=f"rule_{i}",
                condition=RuleCondition(tool=f"tool_{i}"),
                action="ask",
            ))
        # 预热
        engine.evaluate("warmup", {})
        # 计时
        start = time.perf_counter()
        for _ in range(100):
            engine.evaluate("write_file", {"file": "main.c"})
        elapsed = (time.perf_counter() - start) / 100 * 1000  # ms
        assert elapsed < 1.0, f"单次评估 {elapsed:.3f}ms 超过 1ms 限制"


# ── Agent 引擎集成测试 ────────────────────────────────────────


class TestAgentEngineIntegration:
    """验证 BaseAgentEngine 集成了规则引擎"""

    def _make_engine(self):
        """构造最小可用的 AgentEngine 实例"""
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
        return AgentEngine(
            llm=EchoBackend(),
            prompt_builder=PromptBuilder(Path(".")),
            skills=SkillRegistry(),
            config=config,
        )

    def test_engine_has_rule_engine(self):
        """BaseAgentEngine 实例包含 _rule_engine 属性"""
        engine = self._make_engine()
        assert hasattr(engine, "_rule_engine")
        assert hasattr(engine, "_permission_rules_enabled")
        assert engine._permission_rules_enabled is True

    def test_engine_default_rules_loaded(self):
        """默认规则已加载（protect_linker_scripts 等）"""
        engine = self._make_engine()
        rule_names = {r.name for r in engine._rule_engine.list_rules()}
        assert "protect_linker_scripts" in rule_names
        assert "confirm_flash" in rule_names

    def test_engine_rule_evaluate_ld_deny(self):
        """通过 AgentEngine 的规则引擎评估 *.ld 文件 → deny"""
        engine = self._make_engine()
        decision = engine._rule_engine.evaluate("write_file", {"file": "test.ld"})
        assert decision.action == "deny"

    def test_engine_permission_rules_disabled(self):
        """permission_rules_enabled=False 时不加载默认规则"""
        from types import SimpleNamespace
        from pathlib import Path
        from iron.agent.engine import AgentEngine
        from iron.agent.prompt_builder import PromptBuilder
        from iron.llm.backend import EchoBackend
        from iron.skills.registry import SkillRegistry

        config = SimpleNamespace(
            project=SimpleNamespace(project_dir=".", mcu="stm32f407", build_system="platformio"),
            mcp={},
            permission_rules_enabled=False,
        )
        engine = AgentEngine(
            llm=EchoBackend(),
            prompt_builder=PromptBuilder(Path(".")),
            skills=SkillRegistry(),
            config=config,
        )
        assert engine._permission_rules_enabled is False
        # 规则列表为空（未加载默认规则）
        assert len(engine._rule_engine.list_rules()) == 0
