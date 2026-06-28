"""规则评估引擎 — DSL 驱动的权限规则

参考 Claude Code 的规则评估器设计：
- 规则 DSL：条件 + 操作（allow/deny/ask）
- 优先级：deny > ask > allow
- 模式匹配：glob（文件路径）、regex（参数）、关键字
- 用户配置：~/.iron/rules.yml 或项目 .iron-agent/rules.yml

用法:
    engine = PermissionRuleEngine()
    engine.load_default_rules()
    decision = engine.evaluate("write_file", {"file": "src/main.c"})
    if decision.action == "deny":
        raise PermissionError(decision.reason)
"""
import fnmatch
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

# 合法的 action 取值
_VALID_ACTIONS = {"allow", "deny", "ask"}
# 合法的 severity 取值
_VALID_SEVERITY = {"info", "warning", "error"}


@dataclass
class RuleCondition:
    """规则条件 — 描述工具调用何时匹配规则

    支持四类匹配维度（全部满足才算匹配）：
    1. tool: 工具名 glob 模式（默认 "*" 匹配全部）
    2. file_pattern: 文件路径 glob 模式（默认 "*" 匹配全部）
    3. args_contains: 参数键值对精确匹配（如 {"probe": "stlink"}）
    4. args_regex: 参数值正则匹配（如 {"content": "SystemInit"}）
    """
    tool: str = "*"                      # 工具名 glob 模式
    file_pattern: str = "*"              # 文件路径 glob 模式
    args_contains: dict = field(default_factory=dict)   # 参数包含的键值对
    args_regex: dict = field(default_factory=dict)      # 参数正则匹配

    def matches(self, tool_name: str, args: dict) -> bool:
        """检查是否匹配条件（全部维度都满足才返回 True）"""
        if not isinstance(args, dict):
            args = {}
        # 1. 工具名匹配（fnmatch 大小写不敏感，与跨平台工具名一致）
        if not fnmatch.fnmatch(tool_name, self.tool):
            return False
        # 2. file 参数匹配 — 兼容 file / path / filename 三种常见参数名
        # 规则指定了具体文件模式（非 *）时，调用方必须传入 file 参数且匹配；
        # 规则 file_pattern 为 *（默认）时，无论是否有 file 参数都视为匹配。
        # 这样 *.ld 规则只对实际操作文件的工具调用生效，不会误匹配 embed_flash 等无 file 参数的工具
        file_arg = args.get("file") or args.get("path") or args.get("filename") or ""
        if self.file_pattern != "*":
            if not file_arg or not fnmatch.fnmatch(str(file_arg), self.file_pattern):
                return False
        # 3. args_contains 检查 — 键值对精确相等
        for key, value in self.args_contains.items():
            if args.get(key) != value:
                return False
        # 4. args_regex 检查 — 参数值正则搜索
        for key, pattern in self.args_regex.items():
            val = str(args.get(key, ""))
            try:
                if not re.search(pattern, val):
                    return False
            except re.error:
                # 正则编译失败视为不匹配，避免坏规则阻塞所有调用
                logger.warning("规则 args_regex 编译失败: key=%s pattern=%s", key, pattern)
                return False
        return True


@dataclass
class PermissionRule:
    """权限规则 — 条件 + 操作 + 元信息"""
    name: str                                # 规则名（唯一标识）
    description: str = ""                    # 规则描述
    condition: RuleCondition = field(default_factory=RuleCondition)
    action: str = "ask"                      # allow / deny / ask
    severity: str = "warning"                 # info / warning / error
    message: str = ""                        # 自定义提示消息（deny/ask 时展示给用户）


@dataclass
class RuleDecision:
    """规则评估结果 — 引擎返回给调用方的决策"""
    action: str                              # allow / deny / ask
    matched_rule: Optional[PermissionRule] = None   # 命中的规则（allow 默认时为 None）
    reason: str = ""                         # 决策原因（deny/ask 时展示给用户）


class PermissionRuleEngine:
    """规则评估引擎

    用法:
        engine = PermissionRuleEngine()
        engine.load_default_rules()
        decision = engine.evaluate("write_file", {"file": "src/main.c"})
        if decision.action == "deny":
            raise PermissionError(decision.reason)

    评估优先级：deny > ask > allow
    - 任一 deny 规则匹配 → 返回 deny
    - 否则任一 ask 规则匹配 → 返回 ask
    - 否则返回默认动作（默认 allow）
    """

    def __init__(self):
        self._rules: list[PermissionRule] = []
        self._default_action: str = "allow"   # 无规则匹配时的默认动作

    def add_rule(self, rule: PermissionRule) -> None:
        """添加一条规则"""
        if not isinstance(rule, PermissionRule):
            raise TypeError("rule 必须是 PermissionRule 实例")
        if rule.action not in _VALID_ACTIONS:
            raise ValueError(f"非法 action: {rule.action}，允许: {_VALID_ACTIONS}")
        self._rules.append(rule)

    def load_rules(self, path: str | Path) -> int:
        """从 YAML 文件加载规则，返回加载的规则数

        YAML 格式:
            default_action: ask  # 可选，默认 allow
            rules:
              - name: protect_linker_scripts
                description: 保护链接脚本
                condition:
                  tool: "*"
                  file_pattern: "*.ld"
                  args_contains: {}
                  args_regex: {}
                action: deny
                severity: error
                message: 链接脚本禁止自动修改
        """
        p = Path(path).expanduser()
        if not p.exists():
            return 0
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except (OSError, yaml.YAMLError) as e:
            logger.warning("加载规则文件失败 %s: %s", p, e)
            return 0

        if not isinstance(data, dict):
            return 0

        # 可选：default_action
        default_action = data.get("default_action")
        if isinstance(default_action, str) and default_action in _VALID_ACTIONS:
            self._default_action = default_action

        rules_data = data.get("rules", [])
        if not isinstance(rules_data, list):
            return 0

        count = 0
        for item in rules_data:
            if not isinstance(item, dict):
                continue
            rule = self._parse_rule(item)
            if rule is not None:
                self.add_rule(rule)
                count += 1
        return count

    def _parse_rule(self, data: dict) -> Optional[PermissionRule]:
        """从 dict 解析一条规则，失败返回 None"""
        name = data.get("name", "")
        if not name:
            return None
        action = data.get("action", "ask")
        if action not in _VALID_ACTIONS:
            logger.warning("规则 %s 非法 action: %s，跳过", name, action)
            return None
        severity = data.get("severity", "warning")
        if severity not in _VALID_SEVERITY:
            severity = "warning"
        # 解析 condition
        cond_data = data.get("condition", {}) or {}
        if not isinstance(cond_data, dict):
            cond_data = {}
        condition = RuleCondition(
            tool=cond_data.get("tool", "*") or "*",
            file_pattern=cond_data.get("file_pattern", "*") or "*",
            args_contains=dict(cond_data.get("args_contains", {}) or {}),
            args_regex=dict(cond_data.get("args_regex", {}) or {}),
        )
        return PermissionRule(
            name=name,
            description=data.get("description", ""),
            condition=condition,
            action=action,
            severity=severity,
            message=data.get("message", ""),
        )

    def load_default_rules(self) -> None:
        """加载嵌入式专用默认规则

        默认规则覆盖典型嵌入式风险场景：
        - 链接脚本（*.ld）禁止自动修改（deny）
        - 启动文件（*startup*.s）修改需确认（ask）
        - 烧录工具（embed_flash）需确认（ask）
        - 修改 SystemInit 相关代码需确认（ask）
        """
        default_rules = [
            # 链接脚本禁止修改 — 改动可能导致程序无法启动
            PermissionRule(
                name="protect_linker_scripts",
                description="保护链接脚本",
                condition=RuleCondition(file_pattern="*.ld"),
                action="deny",
                severity="error",
                message="链接脚本 (.ld) 禁止自动修改",
            ),
            # 启动文件需确认 — 中断向量表/堆栈配置错误会导致硬故障
            PermissionRule(
                name="confirm_startup_files",
                description="启动文件需确认",
                condition=RuleCondition(file_pattern="*startup*.s"),
                action="ask",
                severity="warning",
                message="启动文件修改需要确认",
            ),
            # flash 工具必须确认 — 烧录是不可逆物理操作
            PermissionRule(
                name="confirm_flash",
                description="烧录操作需确认",
                condition=RuleCondition(tool="embed_flash"),
                action="ask",
                severity="warning",
                message="烧录操作需要确认",
            ),
            # SystemInit 区域需确认 — 改动会影响系统时钟和外设
            PermissionRule(
                name="protect_system_init",
                description="保护 SystemInit",
                condition=RuleCondition(args_regex={"content": "SystemInit"}),
                action="ask",
                severity="warning",
                message="修改 SystemInit 相关代码需要确认",
            ),
        ]
        for rule in default_rules:
            # 默认规则直接 append，避免重复校验开销
            self._rules.append(rule)

    def evaluate(self, tool_name: str, args: dict) -> RuleDecision:
        """评估规则，返回决策

        优先级：deny > ask > allow
        - 找到第一个匹配的 deny 规则就返回（deny 优先）
        - 否则找到第一个匹配的 ask 规则返回
        - 否则返回默认动作

        性能要求：<1ms 每次。规则数量通常 <100，线性扫描足够。
        """
        if not isinstance(args, dict):
            args = {}
        matched_ask: Optional[PermissionRule] = None
        for rule in self._rules:
            try:
                if not rule.condition.matches(tool_name, args):
                    continue
            except (TypeError, ValueError):
                # 匹配过程异常视为不匹配，避免坏规则阻塞调用
                continue
            if rule.action == "deny":
                return RuleDecision(
                    action="deny",
                    matched_rule=rule,
                    reason=rule.message or f"规则 {rule.name} 拒绝操作",
                )
            if rule.action == "ask" and matched_ask is None:
                matched_ask = rule
                # ask 不立即返回，继续扫描是否有 deny 规则（deny 优先级更高）
        if matched_ask is not None:
            return RuleDecision(
                action="ask",
                matched_rule=matched_ask,
                reason=matched_ask.message or f"规则 {matched_ask.name} 要求确认",
            )
        return RuleDecision(action=self._default_action, matched_rule=None, reason="无规则匹配，使用默认动作")

    def list_rules(self) -> list[PermissionRule]:
        """列出所有规则（返回副本，避免外部修改内部列表）"""
        return list(self._rules)

    def clear_rules(self) -> None:
        """清空规则"""
        self._rules.clear()

    def set_default_action(self, action: str) -> None:
        """设置默认动作（当无规则匹配时）

        Args:
            action: "allow" / "deny" / "ask"
        """
        if action not in _VALID_ACTIONS:
            raise ValueError(f"非法 action: {action}，允许: {_VALID_ACTIONS}")
        self._default_action = action
