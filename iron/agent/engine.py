"""Agent 引擎 — 工具调用模式（参考 OpenCode 架构）

核心思路：不做意图分类，直接给 AI 提供工具，让 AI 自己决定调什么。
AI 看到用户输入 + 项目上下文后，返回工具调用列表，引擎负责执行。

v2: 模块化工具系统 — 内置工具 + 外部注册工具

P1-4: 双 Agent 类型（参考 OpenCode Coder/Task 双 Agent 设计）
- BaseAgentEngine: 抽象基类，包含共享逻辑
- CoderAgentEngine: 完整工具集（默认编码 Agent）
- TaskAgentEngine: 只读工具集（探索/规划/审查）
- AgentEngine = CoderAgentEngine（向后兼容别名）
- CoderAgent / TaskAgent: 简短别名（满足 P1-4 命名规范）
"""
import asyncio
import json
import logging
import os
import re
import subprocess
import time
from abc import ABC, abstractmethod
from pathlib import Path

import httpx
from iron.agent.prompt_builder import PromptBuilder
from iron.agent.memory import ContextCompactor, ProjectMemory
from iron.agent.agent_manager import AgentManager
from iron.core.pubsub import EventBus, Event, get_default_bus
from iron.llm.backend import LLMBackend, LLMResponse
from iron.llm.prompt_cache import PromptCache
from iron.skills.registry import SkillRegistry
from iron.tools.registry import ToolRegistry
from iron.tools import create_default_registry
from iron.constants import SOURCE_EXTENSIONS, CHAT_INDICATORS
from iron.agent.engine_builtins import BUILTIN_SCHEMAS
from iron.agent.engine_events import Phase, FileSpec, Plan, AgentEvent
from iron.agent.risk_evaluator import (
    evaluate_command_risk,
    SAFE_COMMANDS as _MODULE_SAFE_COMMANDS,
    DANGEROUS_KEYWORDS as _MODULE_DANGEROUS_KEYWORDS,
)
from iron.agent.stop_hooks import (
    StopHookManager,
    MaxConsecutiveFailures,
    DoomLoopDetector,
    MaxToolRepetition,
    NoProgressDetector,
)
from iron.agent.hooks import (
    HookManager,
    SafetyCheckHook,
    AuditLogHook,
)
from iron.agent.permission import PermissionManager
from iron.rules.permission_rules import PermissionRuleEngine, RuleDecision
from iron.tools.path_guard import _WIN_RESERVED_NAMES

# v4.0 Track 9: 观测性指标采集（可选模块，缺失时静默降级）
try:
    from iron.utils.metrics import counter as _metrics_counter
    from iron.utils.metrics import timing as _metrics_timing
    from iron.utils.metrics import gauge as _metrics_gauge
    _HAS_METRICS = True
except ImportError:
    _HAS_METRICS = False

    def _metrics_counter(*_a, **_kw):
        pass

    def _metrics_timing(*_a, **_kw):
        pass

    def _metrics_gauge(*_a, **_kw):
        pass

# 破坏性/写类外部工具集合 — 这些工具调用时需要走 bash 权限检查
_EXTERNAL_WRITE_TOOLS = {"embed_flash", "embed_build", "mcp_config", "skill_create"}

# 敏感文件名集合 — read_file 拒绝读取，避免泄漏密钥给 LLM
_LOWER_SENSITIVE_NAMES = {
    ".env", ".env.local", "credentials", "secret", "password", ".npmrc", ".pypirc",
    # SSH / 身份凭证
    "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa",
    ".htpasswd", ".netrc", ".gitconfig",
}

# 敏感文件扩展名正则 — 补充精确匹配无法覆盖的模式
_SENSITIVE_SUFFIX_PATTERNS = [
    re.compile(r"\.pem$", re.IGNORECASE),
    re.compile(r"\.key$", re.IGNORECASE),
    re.compile(r"\.p12$", re.IGNORECASE),
    re.compile(r"\.pfx$", re.IGNORECASE),
    re.compile(r"\.keystore$", re.IGNORECASE),
]


# 内置工具 schema 从 engine_builtins.py 导入（保持单源）
_BUILTIN_SCHEMAS = BUILTIN_SCHEMAS


def _build_tools_schema(external_schemas: list[dict]) -> list[dict]:
    """构建完整的工具 schema 列表 = 内置 + 外部注册"""
    return _BUILTIN_SCHEMAS + external_schemas


class BaseAgentEngine(ABC):
    """Agent 引擎基类 — Agentic Loop 核心

    抽象基类（参考 OpenCode 架构），子类必须实现：
    - _get_allowed_tools(): 返回允许的工具名集合（None = 全部允许）
    - _get_system_prompt_prefix(): 返回角色描述前缀（注入系统提示）

    子类通过覆盖 _get_allowed_tools() 控制可用工具集：
    - 返回 None 表示全部允许（Coder 行为）
    - 返回集合表示只允许集合内的工具（Task 只读行为）

    注意：BaseAgentEngine 是抽象类，不能直接实例化。
    使用 CoderAgentEngine（编码）或 TaskAgentEngine（只读）。
    向后兼容：AgentEngine = CoderAgentEngine 别名。
    """

    def __init__(self, llm: LLMBackend, prompt_builder: PromptBuilder, skills: SkillRegistry,
                 config=None, tools: dict = None, event_bus: EventBus = None,
                 lsp_client=None, code_indexer=None):
        self.llm = llm
        self.prompt_builder = prompt_builder
        self.skills = skills
        self.config = config
        self.tools = tools or {}
        self.conversation: list[dict] = []
        self.phase = Phase.THINK
        self._change_history: list[dict] = []
        self._permission_callback = None
        self._question_callback = None  # ask_user 工具的提问回调
        # P3-1: PubSub 事件总线 — 解耦事件生产者与消费者
        # 默认使用全局单例总线，允许调用方注入独立实例（如测试隔离）
        self._event_bus: EventBus = event_bus if event_bus is not None else get_default_bus()
        # v4.0 Track 6: console 懒加载（仅供 diff 预览等工具渲染用）
        # 延迟 import 避免顶层 rich 依赖影响 headless 测试
        self._console = None  # type: ignore[assignment]

        # 项目目录
        self._project_dir = "."
        if config is not None and hasattr(config, "project"):
            self._project_dir = getattr(config.project, "project_dir", ".") or "."

        # 记忆系统
        self._memory = ProjectMemory(self._project_dir)
        self._compactor = ContextCompactor(llm)

        # v2: 模块化工具注册
        self._tool_registry = create_default_registry()
        # P4-3: 工具输出截断阈值 — 从 config 读取并应用到所有注册工具
        # 默认 10000 字符，超阈值自动截断避免上下文 token 浪费
        _tool_max_chars = 10000
        if config is not None:
            _tool_max_chars = getattr(config, "tool_output_max_chars", 10000) or 10000
        self._tool_max_chars = _tool_max_chars
        self._tool_registry.set_max_output_chars(_tool_max_chars)

        # LSP 客户端 + 5 个 LSP 工具注册（lsp_client=None 时工具降级返回 success=False）
        self._lsp_client = lsp_client
        from iron.tools.lsp_tools import (
            LSPDiagnosticsTool, LSPDefinitionTool, LSPReferencesTool,
            LSPHoverTool, LSPCompletionTool,
        )
        self._lsp_diagnostics_tool = LSPDiagnosticsTool(client=lsp_client)
        self._lsp_definition_tool = LSPDefinitionTool(client=lsp_client)
        self._lsp_references_tool = LSPReferencesTool(client=lsp_client)
        self._lsp_hover_tool = LSPHoverTool(client=lsp_client)
        self._lsp_completion_tool = LSPCompletionTool(client=lsp_client)
        self._tool_registry.register(self._lsp_diagnostics_tool)
        self._tool_registry.register(self._lsp_definition_tool)
        self._tool_registry.register(self._lsp_references_tool)
        self._tool_registry.register(self._lsp_hover_tool)
        self._tool_registry.register(self._lsp_completion_tool)
        # P4-3: 对 LSP 工具应用截断阈值（LSP 工具在 set_max_output_chars 之后注册，需重新应用）
        self._tool_registry.set_max_output_chars(_tool_max_chars)

        # v3.0: 代码索引器 + 4 个语义工具（code_indexer=None 时工具降级返回 success=False）
        # 反模式防护 #1：engine.py 不直接调用 CodeIndexer 业务方法，仅通过 context 注入
        self._code_indexer = code_indexer
        from iron.tools.semantic_tools import (
            SemanticSearchTool, GetCallersTool, GetCalleesTool, FindDeadCodeTool,
        )
        self._tool_registry.register(SemanticSearchTool())
        self._tool_registry.register(GetCallersTool())
        self._tool_registry.register(GetCalleesTool())
        self._tool_registry.register(FindDeadCodeTool())
        self._tool_registry.set_max_output_chars(_tool_max_chars)

        # v4.0 Track 5: Git 工具集（默认启用，通用编码能力）
        # 特性门控：features.git_tools 控制是否注册
        try:
            from iron.config.features import is_feature_enabled
            if is_feature_enabled("git_tools"):
                from iron.tools.git_tools import register_git_tools
                register_git_tools(self._tool_registry)
                self._tool_registry.set_max_output_chars(_tool_max_chars)
        except (ImportError, RuntimeError) as _e:
            logging.warning("git_tools 加载失败，跳过: %s", _e)

        # v4.0 Track 7: MultiEdit 多文件原子编辑（默认启用）
        # 特性门控：features.multi_edit 控制是否注册
        try:
            from iron.config.features import is_feature_enabled
            if is_feature_enabled("multi_edit"):
                from iron.tools.multi_edit import register_multi_edit_tool
                register_multi_edit_tool(self._tool_registry)
                self._tool_registry.set_max_output_chars(_tool_max_chars)
        except (ImportError, RuntimeError) as _e:
            logging.warning("multi_edit 加载失败，跳过: %s", _e)

        # v4.0 Track 8: 子 Agent 并行编排工具（默认启用）
        # 特性门控：features.sub_agents 控制是否注册
        try:
            from iron.config.features import is_feature_enabled
            if is_feature_enabled("sub_agents"):
                from iron.agent.sub_agent import register_task_tool
                register_task_tool(self._tool_registry)
                self._tool_registry.set_max_output_chars(_tool_max_chars)
        except (ImportError, RuntimeError) as _e:
            logging.warning("sub_agent 加载失败，跳过: %s", _e)

        # v2: MCP 客户端（从配置加载外部 MCP 服务器）
        self._mcp_client = None
        self._mcp_connected = False
        if config is not None and hasattr(config, "mcp") and config.mcp:
            from iron.mcp.client import MCPClient
            self._mcp_client = MCPClient()
            for name, mcp_cfg in config.mcp.items():
                if hasattr(mcp_cfg, "enabled") and not mcp_cfg.enabled:
                    continue
                srv_type = getattr(mcp_cfg, "type", "local")
                # stdio 传输：command + args + env
                if srv_type in ("local", "stdio"):
                    if hasattr(mcp_cfg, "build_command"):
                        cmd = mcp_cfg.build_command()
                        env = getattr(mcp_cfg, "env", {}) or {}
                    else:
                        # 统一用 getattr + isinstance dict 检查，避免非 dict 对象调用 .get() 崩溃
                        cmd = getattr(mcp_cfg, "command", None)
                        if cmd is None and isinstance(mcp_cfg, dict):
                            cmd = mcp_cfg.get("command", [])
                        if cmd is None:
                            cmd = []
                        env = getattr(mcp_cfg, "env", None)
                        if env is None and isinstance(mcp_cfg, dict):
                            env = mcp_cfg.get("env", {})
                        if env is None:
                            env = {}
                    self._mcp_client.add_server(name, {
                        "type": "local",
                        "command": cmd,
                        "env": env,
                        "timeout": getattr(mcp_cfg, "timeout", 5000),
                    })
                # SSE/HTTP 传输：url + headers
                elif srv_type in ("sse", "http"):
                    url = getattr(mcp_cfg, "url", "") or (mcp_cfg.get("url", "") if hasattr(mcp_cfg, "get") else "")
                    headers = getattr(mcp_cfg, "headers", None) or (mcp_cfg.get("headers") if hasattr(mcp_cfg, "get") else None)
                    self._mcp_client.add_server(name, {
                        "type": srv_type,
                        "url": url,
                        "headers": headers,
                        "timeout": getattr(mcp_cfg, "timeout", 5000),
                    })
        # 注入内置工具的 schema（MCP 工具在 connect 后动态合并）
        # P1-4: 根据 _get_allowed_tools() 过滤工具 schema（只读 Agent 看不到写工具）
        self._tools_schema = self._filter_tools_schema(
            _build_tools_schema(self._tool_registry.get_all_schemas())
        )

        # v2: Agent 管理器
        self._agent_manager = AgentManager(self._project_dir)

        # doom_loop 检测
        self._recent_calls: list[str] = []  # 最近的工具调用签名

        # P1-2: Stop Hooks — 收敛检测器（避免无效循环）
        # 配置读取用 getattr + 默认值，兼容 SimpleNamespace 测试 config
        # P6-2: 特性门控 — is_feature_enabled("stop_hooks") 也控制此功能
        _stop_enabled = True
        _max_failures = 5
        _max_repetition = 10
        _no_progress = 8
        if config is not None:
            _stop_enabled = getattr(config, "stop_hooks_enabled", True)
            _max_failures = getattr(config, "max_consecutive_failures", 5)
            _max_repetition = getattr(config, "max_tool_repetition", 10)
            _no_progress = getattr(config, "no_progress_steps", 8)
        # 特性门控：stop_hooks 特性关闭时强制禁用（默认 True，不影响现有行为）
        try:
            from iron.config.features import is_feature_enabled
            if not is_feature_enabled("stop_hooks"):
                _stop_enabled = False
        except ImportError:
            pass  # 特性门控模块不可用时保持现有行为
        self._stop_hooks = StopHookManager(enabled=_stop_enabled)
        self._stop_hooks.register(MaxConsecutiveFailures(_max_failures))
        self._stop_hooks.register(DoomLoopDetector())
        self._stop_hooks.register(MaxToolRepetition(_max_repetition))
        self._stop_hooks.register(NoProgressDetector(_no_progress))

        # P2-1: 规则评估引擎 — DSL 驱动的权限规则（deny > ask > allow）
        # 默认加载嵌入式专用规则（*.ld 禁止写、startup 需确认、embed_flash 需确认等）
        # 即使无配置文件也加载默认规则，保证基本安全
        self._rule_engine: PermissionRuleEngine = PermissionRuleEngine()
        _rules_enabled = True
        if config is not None:
            _rules_enabled = getattr(config, "permission_rules_enabled", True)
        if _rules_enabled:
            self._rule_engine.load_default_rules()
            # 尝试加载用户级规则 ~/.iron/rules.yml（全局）
            try:
                from iron.config.settings import DEFAULT_CONFIG_DIR
                _user_rules = DEFAULT_CONFIG_DIR / "rules.yml"
                if _user_rules.exists():
                    self._rule_engine.load_rules(_user_rules)
            except (OSError, ImportError) as e:
                logging.warning(f"加载用户规则失败，不影响主流程: {e}", exc_info=True)
            # 尝试加载项目级规则 .iron-agent/rules.yml（项目覆盖）
            try:
                _proj_rules = Path(self._project_dir) / ".iron-agent" / "rules.yml"
                if _proj_rules.exists():
                    self._rule_engine.load_rules(_proj_rules)
            except (OSError, ValueError) as e:
                logging.warning(f"加载项目规则失败，不影响主流程: {e}", exc_info=True)
            # 自定义规则文件路径（最高优先级）
            if config is not None:
                _custom_rules = getattr(config, "permission_rules_file", "") or ""
                if _custom_rules:
                    try:
                        self._rule_engine.load_rules(_custom_rules)
                    except (OSError, ValueError) as e:
                        logging.warning(f"加载自定义规则失败: {e}", exc_info=True)
        self._permission_rules_enabled: bool = _rules_enabled

        # P2-2: Tool Hooks — 工具执行前后介入（PreToolUse / PostToolUse）
        # 参考 Claude Code 的 Hook 系统：用户在 ~/.iron/hooks/ 放 Python 脚本
        # 加载失败不影响主流程（try/except）；内置 SafetyCheckHook + AuditLogHook
        self._hook_manager = HookManager()
        self._hook_manager.add_pre_hook(SafetyCheckHook())
        self._audit_log_hook = AuditLogHook()
        self._hook_manager.add_post_hook(self._audit_log_hook)
        # 尝试从 ~/.iron/hooks/ 加载用户 hooks（用户脚本可加载自 PreToolUseHook/PostToolUseHook）
        try:
            from iron.config.settings import DEFAULT_CONFIG_DIR
            _user_hooks_dir = DEFAULT_CONFIG_DIR / "hooks"
            if _user_hooks_dir.exists():
                self._hook_manager.load_hooks_from_dir(_user_hooks_dir)
        except (OSError, ImportError) as e:
            logging.warning(f"加载用户 hooks 失败，不影响主流程: {e}", exc_info=True)

        # P2-3: 三级审批持久化 — once/session/never
        # 黑名单持久化到 ~/.iron/permissions.yml；会话级允许只在内存
        # 支持通过 config.permission_persist_path 覆盖路径（测试隔离用）
        _perm_path = None
        if config is not None:
            _perm_path = getattr(config, "permission_persist_path", None)
        self._permission_mgr = PermissionManager(persist_path=_perm_path)

        # P1-3: 系统提示分块缓存（参考 Claude Code 两块缓存策略）
        # 默认启用，可通过 config.prompt_caching_enabled=False 关闭
        # P6-2: 特性门控 — is_feature_enabled("prompt_caching") 也控制此功能
        _cache_enabled = True
        _cache_ttl = 300
        if config is not None:
            _cache_enabled = getattr(config, "prompt_caching_enabled", True)
            _cache_ttl = getattr(config, "prompt_cache_ttl", 300)
        # 特性门控：特性关闭时强制禁用（默认 True，不影响现有行为）
        try:
            from iron.config.features import is_feature_enabled
            if not is_feature_enabled("prompt_caching"):
                _cache_enabled = False
        except ImportError:
            pass  # 特性门控模块不可用时保持现有行为
        # 仅当启用且后端未自带 prompt_cache 时注入（避免覆盖外部已注入的实例）
        if _cache_enabled and getattr(self.llm, "prompt_cache", None) is None:
            self.llm.prompt_cache = PromptCache(ttl_seconds=_cache_ttl)
        self._prompt_caching_enabled = _cache_enabled

        # 文件树缓存（避免同一轮对话扫描两次目录）
        self._cached_file_tree: list[str] = []
        self._file_tree_loaded: bool = False

    @abstractmethod
    def _get_allowed_tools(self) -> set[str] | None:
        """返回允许的工具名集合，None 表示全部允许

        子类必须实现此方法以控制可用工具集：
        - CoderAgentEngine: 返回 None（全部允许）
        - TaskAgentEngine: 返回只读工具集合

        被 _filter_tools_schema() 和 process() 中的工具过滤逻辑使用。
        """

    @abstractmethod
    def _get_system_prompt_prefix(self) -> str:
        """返回角色描述前缀，注入到系统提示开头

        子类必须实现此方法：
        - CoderAgentEngine: 返回空字符串（不修改默认提示）
        - TaskAgentEngine: 返回"你是只读探索 Agent..."等角色描述

        被 _build_system_prompt() 使用，前缀会拼接到系统提示最前面。
        """

    def _filter_tools_schema(self, schemas: list[dict]) -> list[dict]:
        """根据 _get_allowed_tools() 过滤工具 schema

        只读 Agent 看不到写工具的 schema，避免 AI 尝试调用被阻止的工具。
        """
        allowed = self._get_allowed_tools()
        if allowed is None:
            return schemas  # 全部允许，不过滤
        return [s for s in schemas if s.get("function", {}).get("name", "") in allowed]

    def _maybe_enable_search_mode(self, system: str) -> tuple[str, list[dict]]:
        """P4-1: 检查是否需要启用工具搜索模式

        当 系统提示 token + 工具 schema token 超过阈值时，切换到搜索模式：
        - 只暴露 tool_search + chat 的 schema（大幅减少提示词长度）
        - 系统提示追加搜索模式说明
        - 注册 ToolSearchTool 到 registry（使引擎能分发执行）

        默认阈值 SEARCH_MODE_THRESHOLD=20000，可通过 config.search_mode_threshold 覆盖。
        config.search_mode_enabled=False 可强制关闭搜索模式。

        Args:
            system: 已构建的系统提示（含 skill 上下文）

        Returns:
            (effective_system, effective_tools_schema)
            - 未触发搜索模式时返回原值（保持现有行为）
            - 触发时返回追加了说明的 system 和精简后的 tools_schema
        """
        from iron.tools.tool_search import (
            ToolSearchTool,
            should_use_search_mode,
            build_search_mode_tools,
        )
        from iron.utils.token_counter import count_tokens

        # config.search_mode_enabled=False 可强制关闭
        _enabled = True
        _threshold = 20000
        if self.config is not None:
            _enabled = getattr(self.config, "search_mode_enabled", True)
            _threshold = getattr(self.config, "search_mode_threshold", 20000)
        if not _enabled:
            return system, self._tools_schema

        # 估算 token 数
        system_tokens = count_tokens(system)
        tool_tokens = count_tokens(json.dumps(self._tools_schema, ensure_ascii=False))

        if not should_use_search_mode(system_tokens, tool_tokens, _threshold):
            return system, self._tools_schema

        # 启用搜索模式：注册 ToolSearchTool + 精简 schema
        search_tools = build_search_mode_tools(self._tool_registry)
        for tool in search_tools:
            if not self._tool_registry.has(tool.name):
                self._tool_registry.register(tool)

        # 构建搜索模式 schema = [tool_search_schema] + [chat_schema]
        chat_schema = next(
            (s for s in BUILTIN_SCHEMAS if s.get("function", {}).get("name") == "chat"),
            None,
        )
        effective_schemas = [search_tools[0].schema]
        if chat_schema:
            effective_schemas.append(chat_schema)

        # 系统提示追加搜索模式说明
        search_note = (
            "\n\n## 工具搜索模式已启用\n"
            "由于工具数量较多，已切换到搜索模式。当前只暴露 tool_search 和 chat 两个工具。\n"
            "请先调用 tool_search(query=...) 查找需要的工具及其参数说明，再调用实际工具。\n"
            "示例：tool_search(query='读取文件') → 返回 read_file 的 schema → 调用 read_file"
        )
        return system + search_note, effective_schemas

    def _build_system_prompt(self) -> str:
        """构建系统提示，包含项目上下文和工具说明

        P1-4: 在系统提示最前面注入 _get_system_prompt_prefix() 前缀，
        让 TaskAgent 等子类能标注自己的角色（如"只读模式"）。
        """
        # P1-4: 角色描述前缀（CoderAgent 返回空字符串，不改变默认行为）
        prefix = self._get_system_prompt_prefix()
        base = self.prompt_builder.build()

        # 扫描项目文件，提供上下文
        file_tree = self._build_file_tree()
        files_context = ""
        if file_tree:
            files_context = f"\n\n当前项目已有文件:\n" + "\n".join(f"  {f}" for f in file_tree[:50])

        # 构建系统配置
        build_system = ""
        if self.config is not None and hasattr(self.config, "project"):
            build_system = getattr(self.config.project, "build_system", "") or ""
        mcu = ""
        if self.config is not None and hasattr(self.config, "project"):
            mcu = getattr(self.config.project, "mcu", "") or ""

        config_context = ""
        if build_system:
            config_context += f"\n构建系统: {build_system}"
        if mcu:
            config_context += f"\n目标 MCU: {mcu}"

        tool_usage = """
你是一个编程助手，可以通过工具调用来帮助用户。

可用工具:
- write_file(path, content): 创建或覆盖文件（完整写入）
- edit_file(path, old_string, new_string): 精确编辑文件中的指定文本（只改需要改的部分，比 write_file 更安全）
- patch(diff, dry_run?): 应用 unified diff 补丁修改文件。支持多文件补丁，比 edit_file 更适合批量修改
- run_command(command): 执行 shell 命令（需要用户授权）
- read_file(path, offset?, limit?): 读取文件内容或列出目录（支持分页，无需授权）。内置支持二进制文档格式：docx/pdf/xlsx/xls/pptx，自动提取文本，无需用 run_command 调 python
- search_code(pattern, glob?): 在项目中搜索代码内容（正则表达式，无需授权）
- find_files(pattern): 按 glob 模式查找文件（如 **/*.c，无需授权）
- ask_user(question, options?): 向用户提问确认
- task_track(action, task_id?, title?, status?): 管理任务列表（创建/更新/完成/列出任务）
- embed_build(action?, target?): 编译嵌入式项目（调用 EmbedForge，支持 PlatformIO/CMake/Make/ESP-IDF/Keil/GCC）
- embed_flash(firmware?, probe?): 烧录固件到目标芯片（调用 EmbedForge，支持 stlink/jlink/openocd）
- embed_lint(files?, rules?): 嵌入式代码静态分析（调用 EmbedGuard，检查 volatile/中断安全/内存安全）
- remember(section, content): 保存知识到项目持久记忆（跨会话保留）。用户说"记住..."时调用
- web_search(action, query?, url?): 搜索网页或获取网页内容。查找芯片手册/库用法/错误解决方案时调用
- skill_create(name, description, prompt, trigger_patterns?, icon?): 创建自定义技能。用户说"创建一个xxx技能"时调用
- mcp_config(action, query?/name?/command?): 配置 MCP 服务器。action: search(搜索GitHub)/add(添加)/list(列出)/test(测试)/remove(移除)。用户说"添加xxx MCP"时调用
- chat(message): 直接回复用户

重要规则:
0. 【最高优先级】回复用户问题、解释代码、给出建议时，必须使用 chat(message=...) 工具。绝对不要用 write_file 写入解释性文字！write_file 只用于创建/修改源代码文件（.c/.h/.py/.json 等），绝不能用来回复用户。
   【重要】chat() 是终止性工具：调用 chat() 后 Agent 循环会立即结束，不会再执行后续工具。因此：
   - 如果需要先执行操作（编译/写入/读取）再回复用户，必须把 chat() 放在工具调用列表的最后
   - 不要在 chat() 之后再调用其他工具，它们不会被执行
   - 一次回复只调用一次 chat()，不要重复调用
1. 修改已有文件时优先用 edit_file，只在创建新文件或需要完全重写时用 write_file
2. 搜索代码用 search_code，查找文件用 find_files，不要用 shell 命令
3. 读取文件用 read_file，不要用 cat/type/ls
4. 复杂任务开始时用 task_track 创建任务列表，完成后更新状态
5. 需要用户确认时用 ask_user，不要直接猜测用户意图
6. 可以一次返回多个工具调用（按顺序执行）
7. 这是 Windows 系统，shell 命令用 dir 而不是 ls，用 del 而不是 rm
8. 项目文件列表已经在上面提供了，不需要再列目录
9. 如果工具执行失败，分析错误根因后再尝试。同一类方案最多试 2 次（如换路径/换写法），仍失败就用 chat() 向用户说明情况并请求指引，不要陷入试错循环
10. 不要因为一次失败就放弃，但要识别"环境问题"（如库未安装、权限不足）——这类问题向用户报告一次即可，等用户处理后再继续，不要反复尝试相同操作

## 任务完成驱动（参考 Claude Code）

你的运行没有固定步数限制，主要靠任务完成来驱动终止：
- 任务全部完成时，立即用 chat() 向用户总结结果并结束
- 用 task_track 创建任务列表，每完成一个子任务就更新状态为 completed
- 系统检测到所有任务完成时会提示你收尾
- 剩余步数不足时系统会预警，收到预警后立即用 chat() 总结当前进度
- 不要在任务完成后继续做多余的操作（如重复编译、重复读取）
- 如果任务无法完成（遇到阻塞），用 chat() 说明情况并停止，不要无限重试

## 编译/烧录/分析 — 必须使用内置工具（禁止用 run_command 替代）

【强制】编译嵌入式项目时，必须使用 embed_build(action="compile")，不要用 run_command 调用 pio/gcc/make 等。
【强制】烧录固件时，必须使用 embed_flash()，不要用 run_command 调用 st-flash/openocd 等。
【强制】代码静态分析时，必须使用 embed_lint()，不要用 run_command 调用外部分析工具。
【强制】嵌入式项目的编译、烧录、分析全部由 EmbedForge 工具处理，你不需要关心底层工具链路径。
【强制】不要尝试用 run_command 安装或检测编译工具（如 pio、gcc、arm-none-eabi-gcc），embed_build 会自动处理。
【例外】run_command 只用于：git 操作、目录管理、运行编译后的程序、执行脚本等非编译任务。"""

        # 注入持久记忆（跨会话）
        memory_context = self._memory.build_context_injection()

        # v2: 注入 Agent 专属 prompt
        # P1-4: prefix 在最前面（TaskAgent 标注"只读模式"，CoderAgent 为空不影响）
        combined = prefix + base + files_context + config_context + memory_context + tool_usage
        combined = self._agent_manager.build_agent_prompt(combined)

        # v2: 注入匹配的 Skill prompt（参考 Claude Code skill 自动触发）
        # 注意：这里不匹配，因为 system prompt 在用户输入前构建
        # Skill 匹配在 process() 方法中进行，匹配后追加到 conversation

        return combined

    # 步数上限仅作安全网（参考 Claude Code max_turns），主要靠任务完成驱动
    # 从 config.max_steps 读取，默认 50；用户可在 iron.yml 中配置
    @property
    def MAX_STEPS(self) -> int:
        if self.config is not None and hasattr(self.config, "max_steps"):
            try:
                steps = int(self.config.max_steps)
            except (TypeError, ValueError):
                return 50
            if steps < 10:
                return 10
            if steps > 5000:
                return 5000  # 与 settings.py 上限一致
            return steps
        return 50

    async def _emit_event(self, event_type: str, data: dict | None = None) -> AgentEvent:
        """P3-1: 统一事件发射器 — 发布到事件总线并返回 AgentEvent

        把"发布到事件总线"和"yield 给 UI"两件事合一，避免每个 yield await self._emit_event(...)
        前都写一遍 await self._event_bus.publish(...)。

        错误隔离：事件总线 publish 内部已捕获订阅者异常，这里再兜一层防止
        总线本身故障阻塞主流程。

        Args:
            event_type: 事件类型字符串（如 "thinking"、"tool.executed"）
            data: 事件负载数据（None 视为空 dict）

        Returns:
            AgentEvent 实例（供 process() 中 `yield await self._emit_event(...)` 使用）
        """
        payload = data if data is not None else {}
        try:
            await self._event_bus.publish(Event(type=event_type, data=payload))
        except asyncio.CancelledError:
            raise
        except (RuntimeError, OSError, AttributeError, KeyError, TypeError, ValueError) as e:
            # 兜底：事件总线异常不应阻塞 Agent 主流程
            logging.warning(f"事件总线发布失败 ({event_type}): {e}", exc_info=True)
        return AgentEvent(event_type, payload)

    async def _init_session(self, user_input: str):
        """会话前置初始化：记忆整理 / 状态重置 / skill 匹配 / 系统提示 / 缓存命中 / MCP 连接

        7 个职责：
        1. Dream/Distill 记忆整理（maybe_dream_distill）
        2. 重置 doom_loop / stop_hooks / 文件树缓存
        3. Skill 自动匹配 + conversation append user 消息
        4. 构建系统提示（含 skill prompt 追加）
        5. P4-1 工具搜索模式切换（提示词过长时只暴露 tool_search + chat）
        6. P1-3 系统提示缓存命中检测 → yield cache_hit 事件
        7. v2 MCP 首次连接 + schema 重建

        通过实例属性传值（async generator 不能用 return 值）：
        - self._init_system: 系统提示
        - self._init_effective_tools: 生效的工具 schema 列表
        - self._init_files_created: 本会话创建的文件列表（初始空）
        - self._init_files_modified: 本会话修改的文件列表（初始空）

        yield 事件：cache_hit（若缓存命中）
        """
        # v2: Dream/Distill 记忆整理（参考 MiMo Code 7天/30天）
        # 在会话开始时检查是否需要整理记忆
        try:
            await self._memory.maybe_dream_distill(self.llm)
        except (RuntimeError, OSError, asyncio.TimeoutError, AttributeError) as e:
            logging.warning(f"记忆整理失败，不影响主流程: {e}", exc_info=True)

        # 每次新会话重置 doom_loop 检测签名，避免上一轮残留签名导致误判
        self._recent_calls = []

        # P1-2: 重置 stop_hooks 内部状态（避免上一轮计数残留）
        self._stop_hooks.reset()

        # 刷新文件树缓存（跨 process 调用时重新扫描）
        self._file_tree_loaded = False
        self._cached_file_tree = []

        # v2: Skill 自动匹配 — 匹配到的 skill prompt 追加到用户消息
        # v2.8: 支持可执行 Skill（pre_execute + 工具注册）
        self._skill_session_data = {}  # 会话级 Skill 数据共享
        skill_context = await self._match_skills(user_input)

        self.conversation.append({"role": "user", "content": user_input})
        system = self._build_system_prompt()
        # 匹配到的 skill prompt 追加到 system prompt
        if skill_context:
            system += f"\n\n## 当前激活的 Skill\n{skill_context}"

        # P4-1: 工具搜索模式 — 提示词过长时只暴露 tool_search + chat
        # 未超阈值时返回原值，保持现有行为（默认不启用搜索模式）
        system, _effective_tools = self._maybe_enable_search_mode(system)

        # P1-3: 检查系统提示缓存命中（用于 UI 显示节省的 token 估算）
        # 对完整 system prompt 做一次 get_or_create，命中说明本次会话内系统提示未变
        if self._prompt_caching_enabled and getattr(self.llm, "prompt_cache", None) is not None:
            _cached_block = self.llm.prompt_cache.get_or_create(system)
            if _cached_block.hit_count > 0:
                # 粗略估算节省的 token 数（字符数 / 4）
                _saved_tokens = len(system) // 4
                yield await self._emit_event("cache_hit", {
                    "hit_count": _cached_block.hit_count,
                    "saved_tokens": _saved_tokens,
                    "cache_key": _cached_block.cache_key,
                })

        # v2: 首次调用时连接 MCP 服务器，合并外部工具
        if self._mcp_client is not None and not self._mcp_connected:
            try:
                await self._mcp_client.connect_all()
                mcp_tools = self._mcp_client.get_tools()
                for tool in mcp_tools:
                    if not self._tool_registry.has(tool.name):
                        self._tool_registry.register(tool)
                # P4-3: 对新加入的 MCP 工具应用截断阈值
                self._tool_registry.set_max_output_chars(self._tool_max_chars)
                # 重建 schema（包含 MCP 工具），并按权限过滤
                # P1-4: 只读 Agent 的 schema 不含写工具
                self._tools_schema = self._filter_tools_schema(
                    _build_tools_schema(self._tool_registry.get_all_schemas())
                )
            except (RuntimeError, OSError, httpx.HTTPError, json.JSONDecodeError, KeyError, AttributeError) as e:
                # MCP 连接失败不阻塞主流程
                logging.warning(f"MCP 连接失败，不阻塞主流程: {e}", exc_info=True)
            finally:
                # 连接失败也置位，避免每次 process 重试
                self._mcp_connected = True

        # 通过实例属性返回值
        self._init_system = system
        self._init_effective_tools = _effective_tools
        self._init_files_created = []
        self._init_files_modified = []

    async def process(self, user_input: str):
        """主处理流程 — Agentic Loop（参考 OpenCode 架构）

        核心模式：
        1. 用户输入 → AI 返回工具调用
        2. 执行工具 → 收集结果（含错误）
        3. 把结果送回 AI → AI 决定下一步
        4. 循环直到 AI 不再调工具，或达到最大步数

        这样 AI 在工具失败时能自动重试或换方案。
        """
        # 会话前置初始化：记忆整理 / 状态重置 / skill 匹配 / 系统提示 / 缓存命中 / MCP 连接
        # 返回值通过实例属性传值：_init_system / _init_effective_tools /
        # _init_files_created / _init_files_modified
        async for ev in self._init_session(user_input):
            yield ev
        system = self._init_system
        _effective_tools = self._init_effective_tools
        all_files_created = self._init_files_created
        all_files_modified = self._init_files_modified
        _should_terminate = False  # chat() 调用后终止标志，防止 AI 重复输出
        _chat_message = None       # chat() 的最终回复内容

        for step in range(self.MAX_STEPS):
            # 思考阶段
            # P1-1: 5 层压缩管道（Level 1→2→3→4→5，每层超阈值才触发）
            messages = await self._compactor.compact_pipeline(self.conversation, system)
            self._thinking_resp = None
            async for ev in self._handle_thinking_phase(system, messages, step, _effective_tools):
                yield ev
            resp = self._thinking_resp
            if resp is None:
                return  # 流式+非流式均失败，error 已 yield

            # 解析工具调用
            tool_calls = self._parse_tool_calls(resp)

            # 没有工具调用 → AI 给出了最终回复，循环结束
            if not tool_calls:
                if step == 0:
                    # 第一步就是纯聊天
                    yield await self._emit_event("phase", {"phase": Phase.CHAT.value})
                yield await self._emit_event("chat_response", {"message": resp.content or ""})
                self.conversation.append({"role": "assistant", "content": resp.content or ""})
                break

            # 执行工具调用，收集结果
            yield await self._emit_event("phase", {"phase": Phase.EXECUTE.value})
            tool_results = []  # 送回 AI 的工具执行结果

            # P1-4: 工具过滤 — 只读 Agent 只执行 _get_allowed_tools() 允许的工具
            # 被阻止的工具：yield tool_blocked 事件 + 加入 tool_results 告知 AI
            _block_events, tool_calls = await self._filter_tool_calls_by_permission(
                tool_calls, tool_results
            )
            for _ev in _block_events:
                yield _ev

            # v2: 只读工具并行执行（参考 Claude Code 并行工具调用）
            _pending_readonly: list = []  # [(call_id, name, args, task), ...]

            # try/finally 确保异常退出时取消未完成的只读并行任务（防止孤儿）
            try:
                for idx, call in enumerate(tool_calls):
                    name = call.get("name", "")
                    args = call.get("arguments", {})
                    # 用 or 处理空字符串，避免 id="" 被传给 API
                    call_id = call.get("id") or f"call_{idx}"

                    # v2: 写工具执行前先 flush 只读并行任务（保证结果顺序正确）
                    if not self._is_readonly_tool(name, args) and _pending_readonly:
                        await self._flush_readonly_tasks(_pending_readonly, tool_results)

                    # P2-1/2-2/2-3: 三段式前置门控（黑名单 + 规则引擎 + PreToolUse hooks）
                    # chat 工具豁免（终止性工具，不应被门控拦截）
                    if name != "chat":
                        self._gate_skip = False
                        self._gate_modified_args = None
                        async for ev in self._check_pre_tool_gates(name, args, call_id, tool_results):
                            yield ev
                        if self._gate_skip:
                            continue
                        if self._gate_modified_args is not None:
                            args = self._gate_modified_args

                    # 工具分发路由器：按 name 分派到 _handle_* 子方法
                    # chat 工具触发终止（_should_terminate + _chat_message 通过实例属性传值）
                    self._dispatch_terminate = False
                    self._dispatch_chat_message = None
                    _tool_t0 = time.monotonic()  # v4.0 Track 9: 工具调用耗时采集
                    async for ev in self._dispatch_tool_call(
                        call, idx, call_id, args, tool_results,
                        _pending_readonly, all_files_created, all_files_modified
                    ):
                        yield ev
                    # v4.0 Track 9: 工具调用指标采集（counter + timing）
                    _tool_elapsed = time.monotonic() - _tool_t0
                    _metrics_counter("tool_calls", tags={"tool": name})
                    _metrics_timing("tool_duration", _tool_elapsed, tags={"tool": name})
                    if self._dispatch_terminate:
                        _should_terminate = True
                        _chat_message = self._dispatch_chat_message
                        continue  # chat 跳过 tool_results.append

                # v2: for 循环结束后 flush 剩余的只读并行任务
                if _pending_readonly:
                    await self._flush_readonly_tasks(_pending_readonly, tool_results)
            finally:
                # 异常退出时取消未完成的只读并行任务（防止孤儿任务泄漏）
                for _, _, _, _t in _pending_readonly:
                    if not _t.done():
                        _t.cancel()

            # chat() 终止检查：AI 已给出最终回复，不再继续循环
            if _should_terminate:
                # 把 chat 的回复作为 assistant 消息加入历史（跨会话保留）
                if _chat_message:
                    self.conversation.append({"role": "assistant", "content": _chat_message})
                break

            # 循环后处理：任务完成检测 / 步数预警 / 对话历史 / 失败检测 / Stop Hooks
            # stop_hook 触发时通过实例属性传 break 标志
            self._post_step_break = False
            async for ev in self._handle_post_step(step, resp, tool_results, tool_calls):
                yield ev
            if self._post_step_break:
                break

            # 循环继续 → AI 会看到失败结果，自行决定重试或换方案
        else:
            # 达到最大步数（安全网触发）
            async for ev in self._handle_max_steps_exceeded():
                yield ev

        # 总结与持久化
        async for ev in self._handle_summary_and_persist(
            user_input, step, all_files_created, all_files_modified
        ):
            yield ev

    async def _handle_summary_and_persist(self, user_input, step, all_files_created, all_files_modified):
        """总结阶段：yield DONE phase + summary + file_tree，并持久化检查点和任务进度

        - phase=DONE 事件必须先发
        - 文件清单非空时 yield summary + file_tree
        - 保存会话检查点（持久记忆），OSError 容错
        - 持久化任务进度到磁盘（跨会话保留），OSError 容错
        """
        yield await self._emit_event("phase", {"phase": Phase.DONE.value})
        if all_files_created or all_files_modified:
            yield await self._emit_event("summary", {
                "files_created": all_files_created,
                "files_modified": all_files_modified,
            })
            yield await self._emit_event("file_tree", {"files": self._build_file_tree()})

        # 保存会话检查点（持久记忆）
        try:
            # ContextCompactor._last_summary 是跨类私有访问，依赖 memory.py 的实现
            summary_text = self._compactor.last_summary or f"执行了 {step + 1} 个步骤"
            self._memory.save_checkpoint(
                summary=summary_text,
                files_changed=all_files_created + all_files_modified,
                current_task=user_input[:200],
            )
        except OSError as e:
            logging.warning(f"检查点保存失败，不影响主流程: {e}")

        # v2: 持久化任务进度到磁盘（跨会话保留，参考 Claude Code tasks/）
        try:
            task_tool = self._tool_registry.get("task_track")
            if task_tool is not None:
                summary_for_tasks = self._compactor.last_summary or user_input[:200]
                task_tool.save_to_file(self._project_dir, summary_for_tasks)
        except OSError as e:
            logging.warning(f"任务持久化失败，不影响主流程: {e}")

    async def _check_pre_tool_gates(self, name, args, call_id, tool_results):
        """三段式前置门控：黑名单 + 规则引擎 + PreToolUse hooks

        chat 工具豁免（由调用方判定，本方法只处理非 chat 工具）。

        三段式检查（任一拦截即跳过该工具）：
        1. P2-3 黑名单（_permission_mgr.check）→ deny 直接拒绝
        2. P2-1 规则引擎（_rule_engine.evaluate）→ deny 拒绝 / ask 触发权限回调
        3. P2-2 PreToolUse hooks（_hook_manager.run_pre_hooks）→ deny 拒绝 / modify 改 args

        返回值通过实例属性传递（async generator 不能用 return 值）：
        - self._gate_skip: True 表示该工具被门控拦截，调用方应 continue
        - self._gate_modified_args: 非 None 表示 hook 修改了参数，调用方应使用新 args

        yield 事件：tool_blocked / step_warn / 权限回调事件
        """
        # P2-3: 三级审批持久化 — 黑名单检查（优先级最高，在规则引擎之前）
        # 黑名单（never）直接拒绝；会话级允许（session）由 _check_permission_with_callback 处理
        _perm_mgr = getattr(self, "_permission_mgr", None)
        if _perm_mgr is not None:
            _perm_decision = _perm_mgr.check(name, args)
            if _perm_decision.action == "deny":
                tool_results.append({
                    "tool_call_id": call_id,
                    "role": "tool",
                    "content": json.dumps({
                        "success": False,
                        "error": f"已加入黑名单: {_perm_decision.reason}",
                    }, ensure_ascii=False),
                })
                yield await self._emit_event("tool_blocked", {
                    "tool": name,
                    "reason": f"黑名单: {_perm_decision.reason}",
                })
                self._gate_skip = True
                return

        # P2-1: 规则评估引擎 — DSL 驱动的权限规则（deny > ask > allow）
        # deny 立即拒绝；ask 触发权限回调（无回调则视为允许，与现有 ask 逻辑一致）
        if self._permission_rules_enabled:
            _rule_decision = self._rule_engine.evaluate(name, args)
            if _rule_decision.action == "deny":
                tool_results.append({
                    "tool_call_id": call_id,
                    "role": "tool",
                    "content": json.dumps({"success": False, "error": _rule_decision.reason}, ensure_ascii=False),
                })
                yield await self._emit_event("step_warn", {"message": f"规则拒绝: {_rule_decision.reason}"})
                self._gate_skip = True
                return
            elif _rule_decision.action == "ask" and self._permission_callback:
                _rule_msg = _rule_decision.reason or f"规则要求确认: {name}"
                _rule_allowed, _rule_ev = await self._check_permission_with_callback(
                    _rule_msg, name, args
                )
                if _rule_ev:
                    yield _rule_ev
                if not _rule_allowed:
                    tool_results.append({
                        "tool_call_id": call_id,
                        "role": "tool",
                        "content": json.dumps({"success": False, "error": f"用户拒绝执行 {name}"}, ensure_ascii=False),
                    })
                    self._gate_skip = True
                    return

        # P2-2: PreToolUse hooks — 工具执行前介入
        # deny 阻止执行；modify 修改参数；allow 放行
        # PreHook 异常按 allow 处理（容错，避免 hook bug 阻塞主流程）
        try:
            pre_result = await self._hook_manager.run_pre_hooks(name, args)
        except asyncio.CancelledError:
            raise
        except (TypeError, ValueError, AttributeError, KeyError,
                RuntimeError, OSError) as e:
            logging.warning(f"PreHook 执行异常，按 allow 处理: {e}", exc_info=True)
            pre_result = None
        if pre_result is not None:
            if pre_result.action == "deny":
                yield await self._emit_event("tool_blocked", {
                    "tool": name, "reason": pre_result.reason,
                })
                tool_results.append({
                    "tool_call_id": call_id,
                    "role": "tool",
                    "content": json.dumps({
                        "success": False,
                        "error": pre_result.reason,
                    }, ensure_ascii=False),
                })
                self._gate_skip = True
                return
            if pre_result.action == "modify" and pre_result.modified_args:
                self._gate_modified_args = pre_result.modified_args

    async def _dispatch_tool_call(self, call, idx, call_id, args, tool_results,
                                   _pending_readonly, all_files_created, all_files_modified):
        """工具分发路由器：按 name 分派到 _handle_* 子方法

        6 个分支：
        - chat → _handle_chat_tool（终止性工具，不 append tool_results）
        - write_file → _handle_write_file_tool
        - edit_file → _handle_edit_file_tool
        - run_command → _handle_run_command_tool
        - read_file → _handle_read_file_tool
        - else → _handle_external_tool（注册工具 + 只读并行 + 破坏性授权）

        通过实例属性传值（async generator 不能用 return 值）：
        - self._dispatch_terminate: True 表示 chat 工具触发终止
        - self._dispatch_chat_message: chat 工具的最终回复内容

        yield 事件：转发各子方法的 yield
        """
        name = call.get("name", "")

        if name == "chat":
            _chat_events, _chat_message = await self._handle_chat_tool(args)
            for _ev in _chat_events:
                yield _ev
            # chat 是最终回复：设置终止标志，不作为 tool_result 送回 AI
            # 防止 AI 看到 "chat 成功" 后继续生成重复回复
            self._dispatch_terminate = True
            self._dispatch_chat_message = _chat_message
            return  # 等同于原 continue（跳出本方法，跳过 tool_results.append）

        elif name == "write_file":
            async for ev in self._handle_write_file_tool(call_id, args, tool_results, all_files_created, all_files_modified):
                yield ev

        elif name == "edit_file":
            async for ev in self._handle_edit_file_tool(call_id, args, tool_results, all_files_modified):
                yield ev

        elif name == "run_command":
            async for ev in self._handle_run_command_tool(call_id, args, tool_results):
                yield ev

        elif name == "read_file":
            async for ev in self._handle_read_file_tool(call_id, args, tool_results):
                yield ev

        else:
            async for ev in self._handle_external_tool(call, call_id, name, args, tool_results, _pending_readonly):
                yield ev

    async def _handle_post_step(self, step, resp, tool_results, tool_calls):
        """循环后处理：任务完成检测 / 步数预警 / 对话历史 / 失败检测 / Stop Hooks

        5 个职责：
        1. 任务完成检测：task_track 所有任务完成 → 提示 AI 用 chat 收尾
        2. 步数预警：剩余 5 步 / 1 步时提示 AI 收尾
        3. 对话历史 append：assistant 消息（含 tool_calls）+ tool 结果
        4. 失败工具检测：JSON 解析 success=False → yield step_done（带失败计数）
        5. Stop Hooks 收敛检测：任一 hook 触发 → yield stop_hook + chat_response + break

        通过实例属性传值：
        - self._post_step_break: True 表示 stop_hook 触发，调用方应 break 主循环

        yield 事件：step_done / stop_hook / chat_response
        """
        # 任务完成检测：检查 task_track 是否所有任务都已完成
        try:
            _task_hint = self._check_task_completion()
        except (ValueError, TypeError, AttributeError) as e:
            logging.warning(f"任务完成检测失败: {e}")
            _task_hint = False
        if _task_hint:
            # 所有任务已完成 → 提示 AI 用 chat 收尾
            tool_results.append({
                "tool_call_id": "system_task_complete",
                "role": "tool",
                "content": json.dumps({
                    "success": True,
                    "system_message": "所有任务已完成。请用 chat() 向用户总结结果并结束。",
                }, ensure_ascii=False),
            })

        # 步数预警：剩余步数不足时提示 AI 收尾
        remaining = self.MAX_STEPS - step - 1
        if remaining == 5:
            tool_results.append({
                "tool_call_id": "system_step_warning",
                "role": "tool",
                "content": json.dumps({
                    "success": True,
                    "system_message": f"⚠ 剩余 {remaining} 步，请尽快收尾。如果任务接近完成，用 chat() 总结结果；如果遇到阻塞，用 chat() 说明情况并停止。",
                }, ensure_ascii=False),
            })
        elif remaining == 1:
            tool_results.append({
                "tool_call_id": "system_step_final",
                "role": "tool",
                "content": json.dumps({
                    "success": True,
                    "system_message": "⚠ 仅剩 1 步，必须立即用 chat() 总结当前进度并结束。",
                }, ensure_ascii=False),
            })

        # 把 AI 的回复和工具结果都加入对话历史
        # assistant 消息需要包含 tool_calls（OpenAI API 要求）
        assistant_msg = {"role": "assistant", "content": resp.content or ""}
        if hasattr(resp, 'tool_calls') and resp.tool_calls:
            assistant_msg["tool_calls"] = resp.tool_calls
        self.conversation.append(assistant_msg)
        # 工具执行结果
        for tr in tool_results:
            self.conversation.append(tr)

        # 用 JSON 解析检测失败的工具调用，替代脆弱的字符串匹配（tr["content"] 可能为 None）
        failed = []
        for tr in tool_results:
            content = tr.get("content")
            if not content or not isinstance(content, str):
                continue
            try:
                parsed = json.loads(content)
                if isinstance(parsed, dict) and parsed.get("success") is False:
                    failed.append(tr)
            except (json.JSONDecodeError, TypeError):
                pass
        if failed:
            yield await self._emit_event("step_done", {"message": f"步骤 {step + 1} 完成，{len(failed)} 个操作需要调整"})
        else:
            yield await self._emit_event("step_done", {"message": f"步骤 {step + 1} 完成"})

        # P1-2: Stop Hooks 收敛检测 — 工具执行后检查是否应该停止循环
        # 任一 hook 触发即停止，避免无效循环浪费 token / 时间
        _stop_decision = self._stop_hooks.check_all(
            tool_calls, tool_results, step, self._recent_calls
        )
        if _stop_decision is not None:
            yield await self._emit_event("stop_hook", {
                "reason": _stop_decision.reason,
                "severity": _stop_decision.severity,
            })
            # 强制生成收尾 chat，避免用户看到硬截断
            yield await self._emit_event("chat_response", {
                "message": f"⚠ 检测到收敛条件，已停止循环：\n{_stop_decision.reason}",
            })
            self._post_step_break = True
            return

    async def _handle_external_tool(self, call, call_id, name, args, tool_results, _pending_readonly):
        """外部工具分发：注册工具的统一处理器

        - 工具未注册 → yield step_warn + append 错误 tool_result
        - doom_loop 检测（在权限检查前，避免 denied 调用不计数导致 AI 无限重试）
        - 只读/写权限判定（read / bash）
        - 只读工具并行路径（asyncio.ensure_future + append 到 _pending_readonly）
        - 破坏性外部工具授权（_EXTERNAL_WRITE_TOOLS，ask 弹窗 / auto 记 warning）
        - 串行执行 + PostHook + task_track 特殊 step_done 事件
        """
        # 工具未注册
        if not self._tool_registry.has(name):
            yield await self._emit_event("step_warn", {"message": f"未知工具: {name}"})
            tool_results.append({
                "tool_call_id": call_id,
                "role": "tool",
                "content": json.dumps({"success": False, "error": f"未知工具: {name}"}, ensure_ascii=False),
            })
            return
        # doom_loop 检测
        if self._check_doom_loop(name, args):
            yield await self._emit_event("step_warn", {"message": f"⚠ 检测到重复调用 {name}，请尝试其他方案"})
            tool_results.append({
                "tool_call_id": call_id,
                "role": "tool",
                "content": json.dumps({"success": False, "error": "同一工具连续3次相同调用，请尝试其他方案"}, ensure_ascii=False),
            })
            return
        # 只读工具检查 read 权限，写工具检查 bash 权限
        if self._is_readonly_tool(name, args):
            _perm = self._agent_manager.get_permission("read")
            _perm_type = "read"
        else:
            _perm = self._agent_manager.get_permission("bash")
            _perm_type = "bash"
        if _perm == "deny":
            tool_results.append({
                "tool_call_id": call_id,
                "role": "tool",
                "content": json.dumps({"success": False, "error": f"Agent 权限拒绝: {_perm_type}"}, ensure_ascii=False),
            })
            yield await self._emit_event("step_warn", {"message": f"Agent 权限拒绝: {_perm_type}"})
            return
        # 只读工具：创建并行任务（不立即 await）
        if self._is_readonly_tool(name, args):
            context = {
                "project_dir": self._project_dir,
                "engine": self,
                "question_callback": self._question_callback,
                "code_indexer": self._code_indexer,
                # v4.0 Track 6: 注入 console 供 diff 预览使用
                "console": self._get_console(),
            }
            tool = self._tool_registry.get(name)
            if tool is not None:
                task = asyncio.ensure_future(tool.safe_execute(args, context))
                _pending_readonly.append((call_id, name, args, task))
            else:
                tool_results.append({
                    "tool_call_id": call_id,
                    "role": "tool",
                    "content": json.dumps({"success": False, "error": f"工具 {name} 未注册"}, ensure_ascii=False),
                })
            return  # 跳过串行执行

        # 破坏性外部工具（embed_flash 等）需要授权
        # 无论权限模式（auto/ask）都检查：ask 模式弹窗，auto 模式记录 warning
        if name in _EXTERNAL_WRITE_TOOLS:
            if _perm == "ask" and self._permission_callback:
                _desc_target = args.get("firmware", "") or args.get("action", "")
                allowed, ev = await self._check_permission_with_callback(
                    f"执行 {name} {_desc_target}".strip(), name, args
                )
                if ev:
                    yield ev
                if not allowed:
                    tool_results.append({
                        "tool_call_id": call_id,
                        "role": "tool",
                        "content": json.dumps({"success": False, "error": f"用户拒绝执行 {name}"}, ensure_ascii=False),
                    })
                    return
            elif _perm == "auto":
                # auto 模式下破坏性工具仍记录 warning（不阻塞执行）
                logging.warning("破坏性工具 %s 在 auto 权限下自动执行: %s",
                                name, args.get("firmware", "") or args.get("action", ""))
        # 串行执行
        context = {
            "project_dir": self._project_dir,
            "engine": self,
            "question_callback": self._question_callback,
            "code_indexer": self._code_indexer,
            # v4.0 Track 6: 注入 console 供 diff 预览使用
            "console": self._get_console(),
        }
        tool = self._tool_registry.get(name)
        # P4-3: 包裹 tool.safe_execute 防止工具异常崩溃整个会话
        try:
            result = await tool.safe_execute(args, context)
        except asyncio.CancelledError:
            raise
        except (OSError, RuntimeError, ValueError, AttributeError, TypeError, KeyError, httpx.HTTPError, subprocess.SubprocessError, json.JSONDecodeError) as e:
            logging.warning(f"工具 {name} 执行异常: {e}", exc_info=True)
            result = {"success": False, "error": f"工具 {name} 内部异常: {type(e).__name__}: {e}"}
        # 工具可能返回 None，做防御性检查
        if result is None:
            result = {"success": False, "error": f"工具 {name} 返回空结果"}
        # P2-2: PostToolUse hooks — 修改工具返回结果
        result = await self._hook_manager.run_post_hooks(name, args, result)
        tool_results.append({
            "tool_call_id": call_id,
            "role": "tool",
            "content": json.dumps(result, ensure_ascii=False),
        })
        # 任务跟踪事件
        if name == "task_track" and result.get("success"):
            yield await self._emit_event("step_done", {"message": f"任务: {args.get('action', '')} {args.get('title', '') or args.get('task_id', '')}"})

    async def _handle_run_command_tool(self, call_id, args, tool_results):
        """run_command 工具：doom_loop + 权限 + 编译重定向 + 执行

        - doom_loop 检测
        - Agent 权限三态（allow/ask/deny，bash 权限）
        - 编译命令重定向到 embed_build（PostHook 用原 name/args，保留用户原始请求语义）
        - stdout/stderr 截断到 2000 字符，None 降级为 ""
        - cmd_result 的 returncode 默认 -1
        """
        # doom_loop 检测
        if self._check_doom_loop("run_command", args):
            yield await self._emit_event("step_warn", {"message": f"⚠ 检测到重复调用 run_command，请尝试其他方案"})
            tool_results.append({
                "tool_call_id": call_id,
                "role": "tool",
                "content": json.dumps({"success": False, "error": "同一工具连续3次相同调用，请尝试其他方案"}, ensure_ascii=False),
            })
            return
        # Agent 权限检查
        # 此处为 Agent 权限（allow/ask/deny）授权；_execute_run_command 内部的
        # _request_permission 是命令风险等级授权（safe/dangerous），两个维度独立，
        # 设计意图为双重检查，不算 bug。保留现状。
        _perm = self._agent_manager.get_permission("bash")
        if _perm == "deny":
            tool_results.append({
                "tool_call_id": call_id,
                "role": "tool",
                "content": json.dumps({"success": False, "error": "Agent 权限拒绝: bash"}, ensure_ascii=False),
            })
            yield await self._emit_event("step_warn", {"message": "Agent 权限拒绝: bash"})
            return
        elif _perm == "ask" and self._permission_callback:
            allowed, perm_event = await self._check_permission_with_callback(
                f"执行命令 {args.get('command', '')[:60]}", "run_command", args
            )
            if perm_event:
                yield perm_event
            if not allowed:
                tool_results.append({
                    "tool_call_id": call_id,
                    "role": "tool",
                    "content": json.dumps({"success": False, "error": "用户拒绝执行"}, ensure_ascii=False),
                })
                return
        # 拦截：如果 AI 用 run_command 执行编译命令，自动重定向到 embed_build
        cmd_text = args.get("command", "").lower()
        _build_kw = ["pio run", "pio build", "platformio run", "platformio build",
                     "arm-none-eabi-gcc", "make all", "make -j",
                     "cmake --build", "idf.py build", "cargo build"]
        if any(kw in cmd_text for kw in _build_kw):
            yield await self._emit_event("step_warn", {
                "message": "⚠ 编译命令已重定向到 embed_build"
            })
            build_tool = self._tool_registry.get("embed_build")
            if build_tool:
                result = await build_tool.safe_execute({"action": "compile"}, {
                    "project_dir": self._project_dir,
                    "engine": self,
                })
                # embed_build 可能返回 None，做防御性检查避免 json.dumps 崩溃
                if result is None:
                    result = {"success": False, "error": "embed_build 返回空结果"}
                # P2-2: PostToolUse hooks — 修改工具返回结果
                # 注意：此处 name 仍是 run_command，args 仍是原命令，
                # 这是有意的（hook 看到的是用户原始请求，而非重定向后的 embed_build）
                result = await self._hook_manager.run_post_hooks("run_command", args, result)
                tool_results.append({
                    "tool_call_id": call_id,
                    "role": "tool",
                    "content": json.dumps(result, ensure_ascii=False),
                })
            return
        cmd_result = {"success": False, "command": "", "stdout": "", "stderr": "", "returncode": -1}
        async for event in self._execute_run_command(args):
            yield event
            if event.type == "command":
                d = event.data
                cmd_result = {
                    "success": d.get("returncode") == 0,
                    "command": d.get("command", ""),
                    "stdout": (d.get("stdout") or "")[-2000:],  # 截断避免上下文过长，None 安全
                    "stderr": (d.get("stderr") or "")[-2000:],  # None 时降级为空字符串
                    "returncode": d.get("returncode", -1),
                }
            elif event.type == "step_warn" and "跳过" in event.data.get("message", ""):
                cmd_result = {"success": False, "command": args.get("command"), "error": "用户拒绝执行"}
        # P2-2: PostToolUse hooks — 修改工具返回结果
        cmd_result = await self._hook_manager.run_post_hooks("run_command", args, cmd_result)
        tool_results.append({
            "tool_call_id": call_id,
            "role": "tool",
            "content": json.dumps(cmd_result, ensure_ascii=False),
        })

    async def _handle_edit_file_tool(self, call_id, args, tool_results, all_files_modified):
        """edit_file 工具：doom_loop + 权限 + 执行 + 撤销历史

        - doom_loop 检测
        - Agent 权限三态（allow/ask/deny）
        - edit 前读取整个文件内容作为 old_content（安全撤销）
        - safe_execute + None 防御
        - 成功时 append 到 _change_history（限制最大 20 条）
        - PostToolUse hooks 修改返回结果
        """
        # doom_loop 检测
        if self._check_doom_loop("edit_file", args):
            yield await self._emit_event("step_warn", {"message": f"⚠ 检测到重复调用 edit_file，请尝试其他方案"})
            tool_results.append({
                "tool_call_id": call_id,
                "role": "tool",
                "content": json.dumps({"success": False, "error": "同一工具连续3次相同调用，请尝试其他方案"}, ensure_ascii=False),
            })
            return
        # Agent 权限检查
        _perm = self._agent_manager.get_permission("edit")
        if _perm == "deny":
            tool_results.append({
                "tool_call_id": call_id,
                "role": "tool",
                "content": json.dumps({"success": False, "error": "Agent 权限拒绝: edit"}, ensure_ascii=False),
            })
            yield await self._emit_event("step_warn", {"message": "Agent 权限拒绝: edit"})
            return
        elif _perm == "ask" and self._permission_callback:
            allowed, perm_event = await self._check_permission_with_callback(
                f"编辑文件 {args.get('path', '')}", "edit_file", args
            )
            if perm_event:
                yield perm_event
            if not allowed:
                tool_results.append({
                    "tool_call_id": call_id,
                    "role": "tool",
                    "content": json.dumps({"success": False, "error": "用户拒绝编辑"}, ensure_ascii=False),
                })
                return
        # 执行 edit_file 并记录到撤销历史
        edit_result = {"success": False, "path": "", "error": ""}
        context = {
            "project_dir": self._project_dir,
            "engine": self,
            "question_callback": self._question_callback,
            "code_indexer": self._code_indexer,
            # v4.0 Track 6: 注入 console 供 diff 预览使用
            "console": self._get_console(),
        }
        tool = self._tool_registry.get("edit_file")
        if tool:
            # 撤销前先读取文件的当前内容（edit 前快照），存入 change_history
            old_file_content = ""
            edit_path = args.get("path", "")
            try:
                resolved = self._resolve_project_path(edit_path)
                if resolved.exists():
                    old_file_content = resolved.read_text(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass  # 文件不存在或路径越界，old_content 保持为空
            result = await tool.safe_execute(args, context)
            # safe_execute 已处理异常和 None，但保留防御性检查
            if result is None:
                result = {"success": False, "error": "工具返回空结果"}
            edit_result = result
            # 记录到撤销历史（与 write_file 一致）
            if result.get("success"):
                all_files_modified.append(args.get("path"))
                self._change_history.append({
                    "action": "edit",
                    "path": args.get("path"),
                    "old_content": old_file_content,  # 整个文件快照（安全撤销）
                    "old_string": args.get("old_string", ""),   # 保留参考信息
                    "new_string": args.get("new_string", ""),
                    "timestamp": __import__("time").time(),
                })
                # 限制变更历史最大长度，与 write_file 一致
                if len(self._change_history) > 20:
                    self._change_history = self._change_history[-20:]
                # LSP 文件变更通知（fire-and-forget，约束 C2）
                # edit 已写盘，读取修改后的完整内容通知 LSP
                _edited_path = args.get("path", "")
                try:
                    _resolved = self._resolve_project_path(_edited_path)
                    if _resolved.exists():
                        _new_content = _resolved.read_text(encoding="utf-8", errors="replace")
                        await self._notify_lsp_file_change(_edited_path, _new_content)
                except (OSError, ValueError) as e:
                    logging.warning(f"LSP did_change 通知失败 (edit_file): {e}")
                # v3.0: 代码索引增量更新（仅对 C/C++ 源文件，特性启用时触发）
                # 反模式防护 #1：此处是 edit_file 的后置 hook，不是业务查询
                self._trigger_index_update(_edited_path)
        else:
            edit_result = {"success": False, "error": "edit_file 工具未注册"}
        # P2-2: PostToolUse hooks — 修改工具返回结果
        edit_result = await self._hook_manager.run_post_hooks("edit_file", args, edit_result)
        tool_results.append({
            "tool_call_id": call_id,
            "role": "tool",
            "content": json.dumps(edit_result, ensure_ascii=False),
        })

    async def _handle_write_file_tool(self, call_id, args, tool_results, all_files_created, all_files_modified):
        """write_file 工具：doom_loop + 权限 + 聊天内容拦截 + 执行

        - doom_loop 检测（三大写工具都在此检查）
        - Agent 权限三态（allow/ask/deny）
        - 聊天内容写入源码拦截（SOURCE_EXTENSIONS + CHAT_INDICATORS）
        - 执行 _execute_write_file 并消费 file_done/error/step_warn 事件
        - PostToolUse hooks 修改返回结果
        - 被拦截时 append tool_result 并 return（等同原 continue）
        """
        # doom_loop 检测
        if self._check_doom_loop("write_file", args):
            yield await self._emit_event("step_warn", {"message": f"⚠ 检测到重复调用 write_file，请尝试其他方案"})
            tool_results.append({
                "tool_call_id": call_id,
                "role": "tool",
                "content": json.dumps({"success": False, "error": "同一工具连续3次相同调用，请尝试其他方案"}, ensure_ascii=False),
            })
            return
        # Agent 权限检查：allow/ask/deny 三态
        _perm = self._agent_manager.get_permission("edit")
        if _perm == "deny":
            tool_results.append({
                "tool_call_id": call_id,
                "role": "tool",
                "content": json.dumps({"success": False, "error": "Agent 权限拒绝: edit"}, ensure_ascii=False),
            })
            yield await self._emit_event("step_warn", {"message": "Agent 权限拒绝: edit"})
            return
        elif _perm == "ask" and self._permission_callback:
            allowed, perm_event = await self._check_permission_with_callback(
                f"写入文件 {args.get('path', '')}", "write_file", args
            )
            if perm_event:
                yield perm_event
            if not allowed:
                tool_results.append({
                    "tool_call_id": call_id,
                    "role": "tool",
                    "content": json.dumps({"success": False, "error": "用户拒绝写入"}, ensure_ascii=False),
                })
                return
        # 防护：检测 AI 是否试图把聊天回复写入源码文件
        path = args.get("path", "")
        content = args.get("content", "")
        if Path(path).suffix.lower() in SOURCE_EXTENSIONS:
            if any(indicator in content for indicator in CHAT_INDICATORS):
                yield await self._emit_event("step_warn", {
                    "message": f"⚠ 检测到聊天内容写入源码文件 {path}，已重定向到 chat"
                })
                tool_results.append({
                    "tool_call_id": call_id,
                    "role": "tool",
                    "content": json.dumps({"success": False, "error": "禁止将聊天内容写入源码文件，请使用 chat() 工具回复用户"}, ensure_ascii=False),
                })
                return
        file_result = {"success": False, "path": "", "error": ""}
        async for event in self._execute_write_file(args):
            yield event
            if event.type == "file_done":
                file_result = {"success": True, "path": args.get("path")}
                # 根据 file_done 携带的 action 分流到 created/modified
                _file_action = event.data.get("action", "新建")
                if _file_action == "新建":
                    all_files_created.append(args.get("path"))
                else:
                    all_files_modified.append(args.get("path"))
            elif event.type == "error":
                file_result = {"success": False, "path": args.get("path"), "error": event.data.get("message", "")}
            elif event.type == "step_warn" and "跳过" in event.data.get("message", ""):
                file_result = {"success": False, "path": args.get("path"), "error": "用户拒绝写入"}
        # P2-2: PostToolUse hooks — 修改工具返回结果
        file_result = await self._hook_manager.run_post_hooks("write_file", args, file_result)
        tool_results.append({
            "tool_call_id": call_id,
            "role": "tool",
            "content": json.dumps(file_result, ensure_ascii=False),
        })

    async def _handle_chat_tool(self, args):
        """chat 工具：终止性工具，yield chat_response 事件

        - chat 不 append 到 tool_results（防止 AI 看到 "chat 成功" 后重复回复）
        - 返回 (events_list, chat_message)，调用方据 chat_message 设 _should_terminate
        """
        _chat_message = args.get("message", "")
        events = [await self._emit_event("chat_response", {"message": _chat_message})]
        return events, _chat_message

    async def _handle_read_file_tool(self, call_id, args, tool_results):
        """read_file 工具：执行读文件，yield file_read/error 事件，append 结果到 tool_results

        - content 截断到 20000 字符，None 降级为 ""
        - PostToolUse hooks 修改返回结果
        """
        read_result = {"success": False, "path": "", "error": ""}
        async for event in self._execute_read_file(args):
            yield event
            if event.type == "file_read":
                # content 可能为 None，用 or "" 降级避免 None 切片崩溃
                read_result = {"success": True, "path": args.get("path"), "content": (event.data.get("content") or "")[:20000]}
            elif event.type == "error":
                read_result = {"success": False, "path": args.get("path"), "error": event.data.get("message", "")}
        # P2-2: PostToolUse hooks — 修改工具返回结果
        read_result = await self._hook_manager.run_post_hooks("read_file", args, read_result)
        tool_results.append({
            "tool_call_id": call_id,
            "role": "tool",
            "content": json.dumps(read_result, ensure_ascii=False),
        })

    async def _handle_thinking_phase(self, system, messages, step, effective_tools):
        """思考阶段：流式生成 + fallback 三态恢复

        - yield thinking / phase / chat_chunk / error 事件
        - 流式 chat_chunk 实时 yield（不缓冲）
        - 流式失败时三态处理：已收 chunk → 用累积内容构造 resp；流式出错 → 切非流式；均失败 → yield error + 置 None
        - 返回值通过 self._thinking_resp 传递（None 表示应终止 process）
        """
        _input_tokens = self._estimate_input_tokens(system, messages)
        if step == 0:
            yield await self._emit_event("thinking", {
                "message": "思考中",
                "input_tokens": _input_tokens,
            })
        else:
            yield await self._emit_event("thinking", {"message": "继续思考"})
        yield await self._emit_event("phase", {"phase": Phase.THINK.value})
        resp = None
        _stream_error = None
        _stream_chunks_received = False
        _accumulated_chunks = []  # 累积 chunk 内容，流式中断时用于恢复
        _llm_t0 = time.monotonic()  # v4.0 Track 9: LLM 调用耗时采集
        try:
            if hasattr(self.llm, "stream_generate"):
                # 流式模式：消费 chunk 事件，UI 实时显示
                # 支持两种协议：新协议 ("result", StreamResult) / 旧协议 ("response", LLMResponse) / ("error", str)
                async for event_type, event_data in self.llm.stream_generate(
                    system, messages,
                    temperature=0.2,
                    max_tokens=4096,
                    tools=effective_tools,
                ):
                    if event_type == "chunk":
                        _stream_chunks_received = True
                        _accumulated_chunks.append(event_data)
                        yield await self._emit_event("chat_chunk", {"text": event_data})
                    elif event_type == "result":
                        # 新协议：StreamResult 三态（complete/partial/failed）
                        sr = event_data
                        if sr.is_complete:
                            resp = LLMResponse(
                                content=sr.content, model=sr.model,
                                usage=sr.usage, tool_calls=sr.tool_calls,
                            )
                        elif sr.is_partial:
                            # 流式中断但已收到内容 → 用累积内容恢复（不重发请求，避免双倍 token）
                            _stream_chunks_received = True
                            resp = LLMResponse(
                                content=sr.content or "".join(_accumulated_chunks),
                                model=sr.model, usage=sr.usage, tool_calls=None,
                            )
                        else:  # failed
                            _stream_error = sr.error or "流式响应失败"
                    elif event_type == "response":
                        # 旧协议兼容：直接是 LLMResponse
                        resp = event_data
                    elif event_type == "error":
                        # 旧协议兼容：错误字符串
                        _stream_error = event_data
                        break
                if resp is None and _stream_error is None:
                    _stream_error = "流式响应未返回完整结果"
            else:
                resp = await self.llm.generate(
                    system, messages,
                    temperature=0.2,
                    max_tokens=4096,
                    tools=effective_tools,
                )
        except asyncio.CancelledError:
            raise
        except (RuntimeError, OSError, httpx.HTTPError) as e:
            _stream_error = str(e)

        # 流式失败 → fallback 到非流式 generate
        # 如果已收到部分 chunk，说明流式已部分成功，不应重发（避免双倍 token 消耗）
        if resp is None:
            if _stream_chunks_received:
                # 流式已收到内容但缺少 response 事件 → 用累积的 chunk 构造响应
                partial_content = "".join(_accumulated_chunks)
                yield await self._emit_event("thinking", {"message": "流式响应不完整，使用已接收内容继续"})
                resp = LLMResponse(content=partial_content, model="", tool_calls=None)
            elif _stream_error:
                yield await self._emit_event("thinking", {"message": f"流式响应失败，切换到非流式模式..."})
                try:
                    resp = await self.llm.generate(
                        system, messages,
                        temperature=0.2,
                        max_tokens=4096,
                        tools=effective_tools,
                    )
                except asyncio.CancelledError:
                    raise
                except (RuntimeError, OSError, httpx.HTTPError) as e:
                    yield await self._emit_event("error", {"message": f"AI 请求失败: {e}"})
                    self._thinking_resp = None
                    return
        self._thinking_resp = resp
        # v4.0 Track 9: LLM 调用指标采集（counter + timing + gauge）
        if resp is not None:
            _llm_elapsed = time.monotonic() - _llm_t0
            _metrics_counter("llm_calls", tags={"status": "success"})
            _metrics_timing("llm_response", _llm_elapsed)
            _metrics_gauge("context_tokens", _input_tokens)

    async def _filter_tool_calls_by_permission(self, tool_calls, tool_results):
        """只读 Agent 工具过滤：阻止不在 _get_allowed_tools() 白名单内的工具

        - _get_allowed_tools() 返回 None → 不过滤（CoderAgent 等全权限 Agent）
        - 返回集合 → 不在白名单的工具被阻止，yield tool_blocked 事件，
          并把阻止结果 append 到 tool_results 告知 AI 只读模式限制
        - 返回 (events_list, filtered_tool_calls)
        """
        _allowed_tools = self._get_allowed_tools()
        if _allowed_tools is None:
            return [], tool_calls
        filtered_calls = []
        blocked_calls = []
        for tc in tool_calls:
            if tc.get("name", "") in _allowed_tools:
                filtered_calls.append(tc)
            else:
                blocked_calls.append(tc)
        # 被阻止的工具结果加入对话，告知 AI 只读模式限制
        events = []
        for _bi, tc in enumerate(blocked_calls):
            _tc_name = tc.get("name", "")
            _call_id = tc.get("id") or f"blocked_{_bi}"
            events.append(await self._emit_event("tool_blocked", {"name": _tc_name, "reason": "只读模式"}))
            tool_results.append({
                "tool_call_id": _call_id,
                "role": "tool",
                "content": json.dumps({
                    "success": False,
                    "error": f"工具 {_tc_name} 在只读模式下被阻止",
                }, ensure_ascii=False),
            })
        return events, filtered_calls

    async def _handle_max_steps_exceeded(self):
        """达到最大步数时的兜底处理（for...else 的 else 分支）

        安全网触发时，yield step_warn 提示用户，并强制生成收尾 chat，
        避免用户看到硬截断。
        """
        yield await self._emit_event("step_warn", {
            "message": f"已达步数上限 ({self.MAX_STEPS})，强制停止。如需继续，可调高 iron.yml 中的 max_steps"
        })
        # 强制生成一个收尾 chat，避免用户看到硬截断
        yield await self._emit_event("chat_response", {
            "message": f"⚠ 已达步数上限 ({self.MAX_STEPS})，任务未完全完成。当前进度：\n"
                       + self._build_progress_summary(),
        })

    def _check_task_completion(self) -> bool:
        """任务完成检测：检查 task_track 工具中所有任务是否都已完成

        参考 Claude Code 的 task:complete 机制：
        当 AI 通过 task_track 创建了任务列表，且所有任务状态都是 completed/failed 时，
        视为任务整体完成，提示 AI 用 chat() 收尾。
        """
        task_tool = self._tool_registry.get("task_track")
        if task_tool is None:
            return False
        tasks = task_tool.get_tasks_for_display()
        if not tasks:
            return False  # 没有创建任务，不触发
        # 所有任务都是终态（completed 或 failed）才算完成
        terminal_states = {"completed", "failed"}
        all_done = all(t.get("status") in terminal_states for t in tasks)
        return all_done

    async def _match_skills(self, user_input: str) -> str:
        """匹配用户输入到 Skill，返回激活的 skill prompt

        v2.8 升级：支持 ExecutableSkill
        1. 用 SkillRegistry.match() 计算匹配分数
        2. 取分数 > 0.5 的 skill
        3. 对 ExecutableSkill 调用 pre_execute（预处理）+ build_prompt（构建 prompt）
        4. 对 PromptSkill 保持原逻辑（_build_prompt）
        5. 注册 ExecutableSkill 的工具到 tool_registry

        返回拼接后的 skill prompt（可能为空字符串）
        """
        if not self.skills:
            return ""

        try:
            matched = self.skills.match(user_input)
            if not matched:
                return ""

            from iron.skills.base import ExecutableSkill, SkillContext

            # 构建 SkillContext（受控访问 engine 状态）
            context = SkillContext(
                user_input=user_input,
                project_root=self._project_dir,
                tool_registry=self._tool_registry,
                llm=self.llm,
                lsp_client=getattr(self, "_lsp_client", None),
                session_data=getattr(self, "_skill_session_data", {}),
            )

            prompts = []
            for skill in matched[:3]:  # 最多激活 3 个 skill
                try:
                    if isinstance(skill, ExecutableSkill):
                        # 可执行 Skill：先 pre_execute，再 build_prompt
                        try:
                            pre_result = await asyncio.wait_for(
                                skill.pre_execute(context),
                                timeout=5.0,
                            )
                            if not pre_result.success:
                                logging.warning(f"Skill {skill.name} pre_execute 失败: {pre_result.message}")
                                continue
                        except asyncio.TimeoutError:
                            logging.warning(f"Skill {skill.name} pre_execute 超时（5秒），跳过")
                            continue
                        except (RuntimeError, OSError, AttributeError) as e:
                            logging.warning(f"Skill {skill.name} pre_execute 异常: {e}")
                            continue

                        # 注册 Skill 工具到 tool_registry（去重，避免重复注册）
                        for tool in skill.get_tools():
                            if not self._tool_registry.has(tool.name):
                                self._tool_registry.register(tool)
                                # 重建 schema（包含新工具）
                                self._tools_schema = self._filter_tools_schema(
                                    _build_tools_schema(self._tool_registry.get_all_schemas())
                                )

                        prompt = skill.build_prompt(context)
                        if prompt:
                            prompts.append(f"### Skill: {skill.name}\n{prompt}")
                    else:
                        # PromptSkill：保持原逻辑
                        if hasattr(skill, "_build_prompt"):
                            prompt = skill._build_prompt()
                            if prompt:
                                prompts.append(f"### Skill: {skill.name}\n{prompt}")
                except (AttributeError, TypeError, ValueError) as e:
                    logging.warning(f"skill prompt 构建异常: {e}", exc_info=True)

            return "\n\n".join(prompts)
        except (AttributeError, TypeError, KeyError, RuntimeError) as e:
            logging.warning(f"skill 匹配异常: {e}", exc_info=True)
            return ""

    def _build_progress_summary(self) -> str:
        """构建当前进度摘要（步数耗尽时用）"""
        parts = []
        # 任务进度
        task_tool = self._tool_registry.get("task_track")
        if task_tool:
            tasks = task_tool.get_tasks_for_display()
            if tasks:
                done = sum(1 for t in tasks if t.get("status") == "completed")
                parts.append(f"任务进度: {done}/{len(tasks)} 完成")
                for t in tasks:
                    icon = {"pending": "○", "in_progress": "◎", "completed": "✓", "failed": "✗"}.get(t["status"], "?")
                    parts.append(f"  {icon} {t['title']}")
        # 文件变更
        if self._change_history:
            parts.append(f"已修改文件: {len(self._change_history)} 个")
            for ch in self._change_history[-5:]:
                parts.append(f"  - {ch['path']} ({ch['action']})")
        return "\n".join(parts) if parts else "无进度信息"

    async def _check_permission_with_callback(self, description: str, tool_name: str, args: dict):
        """通过权限回调请求用户授权（Agent 权限模型）

        参考 Claude Code 的权限系统：
        - allow: 直接执行
        - ask: 弹出授权请求
        - deny: 拒绝

        P2-3: 集成三级审批持久化
        - 黑名单（never）→ 直接拒绝，不弹窗
        - 会话级允许（session）→ 跳过询问，直接允许
        - 用户选择记录到 PermissionManager（once/session/never）

        返回 (allowed, event)：
        - allowed: bool 用户是否允许
        - event: AgentEvent 或 None（需要 yield 给 UI 的事件）
        """
        # P2-3: 先检查权限管理器（黑名单/会话允许）
        _perm_mgr = getattr(self, "_permission_mgr", None)
        if _perm_mgr is not None:
            _perm_decision = _perm_mgr.check(tool_name, args)
            if _perm_decision.action == "deny":
                # 黑名单直接拒绝，不弹窗
                return False, AgentEvent("permission_denied", {
                    "description": description,
                    "tool": tool_name,
                    "reason": _perm_decision.reason,
                    "args": args,
                })
            if _perm_decision.action == "allow":
                # 会话级允许，跳过询问
                return True, None

        if self._permission_callback is None:
            return True, None  # 无回调默认允许

        event = AgentEvent("permission_request", {
            "description": description,
            "tool": tool_name,
            "args": args,
        })
        # 权限回调由 CLI 设置（可能是同步阻塞操作），用 to_thread 避免阻塞事件循环
        # 统一为传单个 dict（与 _request_permission 一致），并加 fail-safe 拒绝
        info = {"action": tool_name, "target": description, "details": description, "args": args}
        try:
            allowed = await asyncio.to_thread(self._permission_callback, info)
        except asyncio.CancelledError:
            raise
        except (RuntimeError, OSError, TypeError, ValueError, AttributeError) as e:
            logging.warning(f"权限回调异常，fail-safe 拒绝: {e}", exc_info=True)
            allowed = False

        # P2-3: 记录用户决策（支持 str 返回三级选择 + bool 向后兼容）
        # 回调返回 "once"/"session"/"never" 时记录到权限管理器
        if _perm_mgr is not None and isinstance(allowed, str) and allowed in ("once", "session", "never"):
            _perm_mgr.record_decision(tool_name, args, allowed)
            return allowed != "never", event

        return bool(allowed), event

    async def _flush_readonly_tasks(self, pending: list, tool_results: list):
        """等待并收集所有挂起的只读工具并行任务结果

        参考 Claude Code 的并行工具执行：
        - 多个只读工具（search_code/find_files/web_search/read_file）可以并行执行
        - 写工具执行前必须先 flush，保证结果顺序正确
        - 单个任务直接 await（避免 gather 开销）
        - P2-2: flush 时统一运行 PostToolUse hooks 修改结果
        """
        if not pending:
            return
        if len(pending) == 1:
            call_id, name, args, task = pending[0]
            try:
                result = await task
            except asyncio.CancelledError:
                raise
            except (OSError, httpx.HTTPError, subprocess.SubprocessError, json.JSONDecodeError,
                    KeyError, AttributeError, ValueError, TypeError, RuntimeError) as e:
                logging.warning(f"只读工具 {name} 执行异常: {type(e).__name__}: {e}")
                result = {"success": False, "error": f"{type(e).__name__}: {e}"}
            # P2-2: PostToolUse hooks — 修改工具返回结果
            result = await self._hook_manager.run_post_hooks(name, args, result)
            tool_results.append({
                "tool_call_id": call_id,
                "role": "tool",
                "content": json.dumps(result, ensure_ascii=False),
            })
        else:
            # 多个只读任务并行 await
            results = await asyncio.gather(
                *[t for _, _, _, t in pending],
                return_exceptions=True,
            )
            for (call_id, name, args, _), result in zip(pending, results):
                if isinstance(result, Exception):
                    # 异常记录日志，CLI 端通过 step_warn 事件向用户展示
                    logging.warning(f"只读工具 {name} 执行异常: {type(result).__name__}: {result}")
                    result = {"success": False, "error": f"{type(result).__name__}: {result}"}
                # P2-2: PostToolUse hooks — 修改工具返回结果
                result = await self._hook_manager.run_post_hooks(name, args, result)
                tool_results.append({
                    "tool_call_id": call_id,
                    "role": "tool",
                    "content": json.dumps(result, ensure_ascii=False),
                })
        pending.clear()

    def _is_readonly_tool(self, name: str, args: dict) -> bool:
        """判断工具调用是否是只读的（可并行执行）

        判断规则：
        1. 工具名在 _READONLY_EXTERNAL_TOOLS 集合中 → 只读
        2. 工具名在 _READONLY_ACTIONS 中且 action 参数在对应集合中 → 只读
        3. 其他 → 非只读（写工具）
        """
        # 完全只读的工具
        if name in self._READONLY_EXTERNAL_TOOLS:
            return True
        # 特定 action 才只读的工具
        if name in self._READONLY_ACTIONS:
            action = args.get("action", "")
            if action in self._READONLY_ACTIONS[name]:
                return True
        return False

    def _check_doom_loop(self, name: str, args: dict) -> bool:
        """检测 doom_loop（连续相同调用 + 循环模式检测）

        返回 True 表示触发（应拒绝执行），False 表示正常。
        两级检测：
        1. 连续 3 次完全相同调用 → 触发
        2. 长度 2/3/4 的循环模式（如 A→B→A→B 或 A→B→C→A→B→C）→ 触发

        签名扩展到 200 字符，减少长参数误判。
        维护 self._recent_calls（最近 12 次调用签名，足够检测长度 4 的循环 × 2）。
        """
        try:
            call_sig = f"{name}:{json.dumps(args, sort_keys=True, ensure_ascii=False)[:200]}"
        except (TypeError, ValueError):
            call_sig = f"{name}:{str(args)[:200]}"
        self._recent_calls.append(call_sig)
        # 保留最近 12 次调用（4 种模式长度 × 2 重复 + 余量）
        if len(self._recent_calls) > 12:
            self._recent_calls = self._recent_calls[-12:]

        # 检测 1：连续 3 次完全相同
        if len(self._recent_calls) >= 3:
            last3 = self._recent_calls[-3:]
            if len(set(last3)) == 1:
                self._recent_calls.clear()
                return True

        # 检测 2：循环模式（长度 2/3/4 的重复子序列）
        # 例如 A→B→A→B（长度 2）、A→B→C→A→B→C（长度 3）
        for pattern_len in (2, 3, 4):
            if len(self._recent_calls) >= pattern_len * 2:
                recent = self._recent_calls[-pattern_len * 2:]
                first_half = recent[:pattern_len]
                second_half = recent[pattern_len:]
                if first_half == second_half:
                    self._recent_calls.clear()
                    return True

        return False

    def _parse_tool_calls(self, resp: LLMResponse) -> list[dict]:
        """解析 AI 返回的工具调用

        支持两种格式:
        1. OpenAI 标准 tool_calls 格式
        2. 从文本中提取 JSON 工具调用（兼容不支持 tools 参数的后端）
        """
        # 标准 tool_calls
        if resp.tool_calls:
            calls = []
            for tc in resp.tool_calls:
                func = tc.get("function", {})
                name = func.get("name", "")
                args_str = func.get("arguments", "{}")
                try:
                    args = json.loads(args_str) if isinstance(args_str, str) else args_str
                except json.JSONDecodeError:
                    args = {}
                if name:
                    calls.append({
                        # 用 or 处理空字符串，避免 id="" 被传给 API
                        "id": tc.get("id") or f"call_{len(calls)}",
                        "name": name,
                        "arguments": args,
                    })
            return calls

        # 从文本中提取（兼容模式）
        text = resp.content
        if not text:
            return []

        # 尝试解析整个回复为 JSON 数组
        try:
            data = json.loads(text.strip())
            if isinstance(data, list):
                return [d for d in data if isinstance(d, dict) and "name" in d]
            if isinstance(data, dict) and "name" in data:
                return [data]
        except json.JSONDecodeError:
            pass

        # 从 markdown 代码块中提取 JSON
        json_match = re.search(r'```(?:json)?\s*\n?(.*?)```', text, re.DOTALL)
        if json_match:
            try:
                data = json.loads(json_match.group(1).strip())
                if isinstance(data, list):
                    return [d for d in data if isinstance(d, dict) and "name" in d]
                if isinstance(data, dict) and "name" in data:
                    return [data]
            except json.JSONDecodeError:
                pass

        # 移除过于激进的 _looks_like_code 自动包装为 write_file 的逻辑
        # 该启发式会在技术解释（含 #include/return/if( 等关键词）时误触发写文件
        # 改为返回空列表，让 process() 把文本作为 chat 回复处理（AI 自行决定下一步）
        return []

    async def _notify_lsp_file_change(self, path: str, content: str) -> None:
        """通知 LSP 文件修改（fire-and-forget，不阻塞主循环）

        约束 C2：失败仅 warning，不上抛。
        仅对 C/C++ 源文件通知。
        """
        if not self._lsp_client or not getattr(self._lsp_client, "_initialized", False):
            return
        ext = Path(path).suffix.lower()
        if ext not in {".c", ".h", ".cpp", ".cc", ".cxx", ".hpp", ".hh"}:
            return
        try:
            # fire-and-forget：不 await，避免阻塞 generator
            asyncio.create_task(self._lsp_client.did_change(path, content))
        except (RuntimeError, OSError) as e:
            logger.warning("LSP did_change 通知失败 (%s): %s", path, e)

    async def _notify_lsp_file_open(self, path: str, content: str) -> None:
        """通知 LSP 文件打开（fire-and-forget，不阻塞主循环）"""
        if not self._lsp_client or not getattr(self._lsp_client, "_initialized", False):
            return
        ext = Path(path).suffix.lower()
        if ext not in {".c", ".h", ".cpp", ".cc", ".cxx", ".hpp", ".hh"}:
            return
        try:
            asyncio.create_task(self._lsp_client.did_open(path, content))
        except (RuntimeError, OSError) as e:
            logger.warning("LSP did_open 通知失败 (%s): %s", path, e)

    def _trigger_index_update(self, path: str) -> None:
        """v3.0: 文件变更后触发代码索引增量更新（hook 副作用，非业务查询）

        反模式防护 #1：本方法是 edit_file/write_file 的后置 hook，
        仅触发 index_file 保持索引新鲜，不调用 search/get_callers 等查询方法
        （查询通过 semantic_tools 工具注册暴露给 AI）。
        """
        if not self._code_indexer:
            return
        # 仅对 C/C++ 源文件触发
        if not path.lower().endswith((".c", ".h", ".cpp", ".hpp")):
            return
        try:
            from iron.config.features import is_feature_enabled
            if not is_feature_enabled("code_indexer"):
                return
            self._code_indexer.index_file(path)
        except (ImportError, OSError, RuntimeError, ValueError) as e:
            logging.debug(f"代码索引增量更新失败 ({path}): {e}")

    async def _execute_write_file(self, args: dict):
        """执行 write_file 工具"""
        path = args.get("path", "")
        content = args.get("content", "")
        action = args.get("action", "新建")

        if not path or not content:
            yield await self._emit_event("error", {"message": "write_file 缺少 path 或 content"})
            return

        yield await self._emit_event("file_start", {"path": path, "action": action})

        # 先解析路径并做边界校验；路径越界直接拒绝，不进入授权流程
        try:
            full_path = self._resolve_project_path(path)
        except ValueError as e:
            yield await self._emit_event("error", {"message": str(e)})
            return

        # dangerous 路径硬阻断（项目目录外或敏感文件），直接拒绝不进入授权流程
        risk = self._evaluate_write_risk(path)
        if risk == "dangerous":
            yield await self._emit_event("error", {"message": f"拒绝访问项目目录外的文件: {path}"})
            return

        # 授权由外层 write_file 分支的 _check_permission_with_callback 统一处理，避免双重授权

        # 读取旧内容（用于 diff 和撤销）
        # 旧文件可能非 UTF-8 编码，捕获 UnicodeDecodeError 避免中断整个 process 循环
        old_content = None
        if full_path.exists():
            try:
                old_content = full_path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                old_content = None
                yield await self._emit_event("step_warn", {"message": "旧文件编码非 UTF-8，不生成 diff"})
            action = "修改"

        # 产出代码事件
        lang = self._detect_language(path)
        yield await self._emit_event("file_code", {"path": path, "code": content, "language": lang})

        # 修改场景：显示 diff
        if old_content is not None:
            yield await self._emit_event("file_diff", {
                "path": path, "old_code": old_content, "new_code": content, "language": lang,
            })

        # 写入磁盘
        try:
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(content, encoding="utf-8")
        except OSError as e:
            yield await self._emit_event("error", {"message": f"写入文件失败 {path}: {e}"})
            return

        # 记录变更历史
        self._change_history.append({
            "path": path, "action": action,
            "old_content": old_content, "new_content": content,
        })
        # 限制变更历史最大长度，避免长会话内存泄漏
        if len(self._change_history) > 20:
            self._change_history = self._change_history[-20:]

        yield await self._emit_event("file_done", {"path": path, "code": content, "lines": content.count("\n"), "action": action})

        # LSP 文件变更通知（fire-and-forget，约束 C2）
        await self._notify_lsp_file_change(path, content)

    async def _execute_run_command(self, args: dict):
        """执行 run_command 工具"""
        command = args.get("command", "")
        if not command:
            yield await self._emit_event("error", {"message": "run_command 缺少 command"})
            return

        # 请求授权（根据风险等级决定是否弹窗）
        risk = self._evaluate_command_risk(command)
        if risk == "safe":
            yield await self._emit_event("step_done", {"message": f"执行: {command}"})
        else:
            yield await self._emit_event("step_warn", {"message": f"⚠ 需要授权: {command}"})
            yield await self._emit_event("permission_request", {
                "action": "执行命令", "target": command, "details": f"将执行: {command}",
            })
        approved = await self._request_permission("执行命令", command, f"将执行: {command}", risk=risk, args=args)
        if not approved:
            yield await self._emit_event("step_warn", {"message": "用户跳过命令执行"})
            return

        try:
            result = await self.run_command(command)
            yield await self._emit_event("command", result)
            if result["returncode"] == 0:
                yield await self._emit_event("step_done", {"message": "命令执行成功"})
            else:
                yield await self._emit_event("step_warn", {"message": f"命令退出码: {result['returncode']}"})
        except subprocess.TimeoutExpired:
            yield await self._emit_event("error", {"message": f"命令执行超时: {command}"})
        except asyncio.CancelledError:
            raise
        except (OSError, subprocess.CalledProcessError) as e:
            yield await self._emit_event("error", {"message": f"命令执行失败: {e}"})

    async def _execute_read_file(self, args: dict):
        """执行 read_file 工具（无需授权，支持读文件、分页和列目录）

        read_file 是只读工具，允许读取项目外路径。
        Claude Code 同样允许读任意路径，只对写操作做 path_guard 限制。
        保留 Windows 保留设备名检查和敏感文件检查。
        """
        path = args.get("path", "")
        offset = args.get("offset", 1)  # 从第 1 行开始
        limit = args.get("limit", 200)   # 默认最多 200 行
        if not path:
            yield await self._emit_event("error", {"message": "read_file 缺少 path"})
            return

        try:
            # 读取允许项目外路径，仅做安全检查（保留名 + 敏感文件）
            raw = Path(path)
            if raw.is_absolute():
                full_path = raw.resolve()
            else:
                full_path = (Path(self._get_project_dir()) / raw).resolve()

            # Windows 保留设备名检查
            name_upper = full_path.name.upper().split(".")[0]
            if name_upper in _WIN_RESERVED_NAMES:
                yield await self._emit_event("error", {"message": f"路径包含 Windows 保留设备名: {full_path.name}"})
                return

            if not full_path.exists():
                yield await self._emit_event("error", {"message": f"文件不存在: {path}"})
                return

            # 拦截敏感文件（.env/credentials/secret/password 等），避免泄漏给 LLM
            file_name_lower = full_path.name.lower()
            if file_name_lower in _LOWER_SENSITIVE_NAMES:
                yield await self._emit_event("error", {"message": f"拒绝读取敏感文件: {path}（如需查看请手动操作或通过环境变量）"})
                return
            # 拦截敏感扩展名（*.pem/*.key/*.p12/*.pfx/*.keystore）
            if any(pat.search(file_name_lower) for pat in _SENSITIVE_SUFFIX_PATTERNS):
                yield await self._emit_event("error", {"message": f"拒绝读取敏感文件: {path}（密钥/证书文件）"})
                return

            # 目录：列出内容
            if full_path.is_dir():
                ignore_dirs = {".git", ".idea", ".vscode", "__pycache__", "build", "dist", ".trae-cn", ".iron"}
                items = []
                try:
                    for item in sorted(full_path.iterdir()):
                        if item.name in ignore_dirs:
                            continue
                        prefix = "[DIR] " if item.is_dir() else "      "
                        items.append(f"{prefix}{item.name}")
                except PermissionError:
                    items.append("(无权限访问)")
                content = "\n".join(items) if items else "(空目录)"
                yield await self._emit_event("file_read", {"path": path, "content": content, "language": "text"})
                return

            # 文件：读取内容（支持分页）
            # 读取前检测二进制文件（前 1KB 含 null byte）
            # 对内置支持的文档格式（docx/pdf/excel/pptx）调用专用解析器提取文本，
            # 避免 AI 反复用 run_command 试错（每次命令不同还会触发重复授权询问）。
            try:
                with open(full_path, 'rb') as f:
                    if b'\x00' in f.read(1024):
                        # 二进制文件：检查是否是内置支持的文档格式
                        from iron.utils.doc_reader import is_supported_doc, read_document
                        if is_supported_doc(full_path):
                            content, lang, err = read_document(full_path)
                            if err:
                                # 库未安装或读取失败：返回清晰的安装提示（一次性，不陷入循环）
                                yield await self._emit_event("error", {"message": f"{err}（文件: {path}）"})
                                return
                            if content:
                                # 文档内容较多时只显示前 20000 字符，避免撑爆上下文
                                if len(content) > 20000:
                                    content = content[:20000] + f"\n\n[... 已截断，共 {len(content)} 字符，显示前 20000 字符]"
                                yield await self._emit_event("file_read", {
                                    "path": path,
                                    "content": content,
                                    "language": lang,
                                })
                                return
                            yield await self._emit_event("error", {"message": f"文档内容为空: {path}"})
                            return
                        # 非内置支持的二进制文件：拒绝读取，避免向 LLM 暴露乱码
                        yield await self._emit_event("error", {"message": f"拒绝读取二进制文件: {path}"})
                        return
            except OSError:
                pass
            # 非 UTF-8 文件（GBK/二进制）用 errors="replace" 降级避免 UnicodeDecodeError
            try:
                full_content = full_path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                full_content = full_path.read_text(encoding="utf-8", errors="replace")
            # LSP 文件打开通知（fire-and-forget，约束 C2）
            await self._notify_lsp_file_open(path, full_content)
            lines = full_content.splitlines()
            total_lines = len(lines)

            # 分页：offset 从 1 开始
            start = max(0, offset - 1)
            end = min(total_lines, start + limit)
            selected_lines = lines[start:end]

            # 添加行号
            numbered = []
            for i, line in enumerate(selected_lines, start + 1):
                numbered.append(f"{i:4d}→{line}")
            content = "\n".join(numbered)

            # 如果有截断，添加提示
            if start > 0 or end < total_lines:
                content = f"[显示第 {start+1}-{end} 行，共 {total_lines} 行]\n{content}"

            lang = self._detect_language(path)
            yield await self._emit_event("file_read", {"path": path, "content": content, "language": lang})
        except (OSError, UnicodeDecodeError, RuntimeError) as e:
            yield await self._emit_event("error", {"message": f"读取失败 {path}: {e}"})

    # ── 基础设施方法（保持不变） ───────────────────────────────

    async def run_command(self, command: str, cwd: str = None) -> dict:
        """执行 shell 命令"""
        # 超时值从 IronConfig 读取，默认 300 秒
        # 嵌入式项目编译（platformio/idf.py/cmake）普遍 >30 秒，原硬编码 30 秒会导致编译必失败
        # 同时用进程组 kill 整个进程树，避免孙进程（如 make 调用的 gcc）残留
        timeout = self._get_run_command_timeout()

        def _execute():
            # 已知风险 — shell=True 允许命令拼接，但这里是设计权衡：
            # AI 需要执行复合命令（&&、|、重定向等），且 _evaluate_command_risk 已做元字符拦截
            # 危险命令需经用户授权。保持现状以支持复合命令语义
            # 用 Popen + 进程组，超时时 kill 整个进程树（包含孙进程）
            import signal as _signal
            kwargs = {
                "shell": True,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "cwd": cwd or self._get_project_dir(),
                "encoding": "utf-8",
                "errors": "replace",  # 统一 UTF-8 编码，避免 GBK 崩溃
            }
            # Windows 用 CREATE_NEW_PROCESS_GROUP，Unix 用 setsid，确保可 kill 整个进程组
            if os.name == "nt":
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                kwargs["preexec_fn"] = os.setsid

            proc = subprocess.Popen(command, **kwargs)
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
                return {
                    "command": command,
                    "returncode": proc.returncode,
                    "stdout": stdout,
                    "stderr": stderr,
                }
            except subprocess.TimeoutExpired:
                # 超时时 kill 整个进程组，避免孙进程残留
                try:
                    if os.name == "nt":
                        # Windows: taskkill /T 强制 kill 整个进程树
                        subprocess.run(
                            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                            capture_output=True, timeout=10,
                        )
                    else:
                        # Unix: kill 整个进程组
                        os.killpg(os.getpgid(proc.pid), _signal.SIGKILL)
                except OSError:
                    pass
                try:
                    stdout, stderr = proc.communicate(timeout=5)
                except (OSError, ValueError):
                    stdout, stderr = "", ""
                return {
                    "command": command,
                    "returncode": -1,
                    "stdout": stdout or "",
                    "stderr": (stderr or "") + f"\n[命令执行超时（{timeout} 秒），已强制终止进程树]",
                }
        return await asyncio.to_thread(_execute)

    def _get_run_command_timeout(self) -> int:
        """从 config 读取 run_command 超时值，默认 300 秒"""
        try:
            cfg = getattr(self, "config", None)
            if cfg is not None:
                timeout = getattr(cfg, "run_command_timeout", None)
                if isinstance(timeout, int) and 30 <= timeout <= 3600:
                    return timeout
        except (ValueError, TypeError, AttributeError):
            pass
        return 300

    async def read_file(self, file_path: str):
        """读取文件（供 /read 命令使用）

        允许读取项目外路径（只读操作，无安全风险）。
        """
        try:
            raw = Path(file_path)
            if raw.is_absolute():
                full_path = raw.resolve()
            else:
                full_path = (Path(self._get_project_dir()) / raw).resolve()
            if not full_path.exists():
                yield await self._emit_event("error", {"message": f"文件不存在: {file_path}"})
                return
            content = full_path.read_text(encoding="utf-8", errors="replace")
            language = self._detect_language(file_path)
            yield await self._emit_event("file_read", {"path": file_path, "content": content, "language": language})
        except (OSError, UnicodeDecodeError, RuntimeError) as e:
            yield await self._emit_event("error", {"message": f"读取文件失败 {file_path}: {e}"})

    async def undo_last(self) -> dict | None:
        """撤销最后一次文件变更"""
        if not self._change_history:
            return None

        record = self._change_history.pop()
        path = record.get("path")
        action = record.get("action")
        old_content = record.get("old_content")

        if not path:
            return record

        try:
            full_path = self._resolve_project_path(path)
            if action == "新建":
                if full_path.exists():
                    full_path.unlink()
            elif action == "修改":
                if old_content is not None:
                    full_path.parent.mkdir(parents=True, exist_ok=True)
                    full_path.write_text(old_content, encoding="utf-8")
                else:
                    # old_content 为 None 表示原文件非 UTF-8 编码，备份不可用，
                    # 保留当前内容不删除（避免撤销 = 数据丢失）
                    logging.warning(f"无法撤销 {path}：原文件非 UTF-8 编码，备份不可用，保留当前内容")
            elif action == "edit":
                # 撤销 edit_file：
                # 新格式：edit_file 执行前预存 old_content 全文件快照，直接还原
                # 旧格式（无 old_content 或路径越界）：fallback 到 str.replace(old_string, new_string)
                edit_old = record.get("old_content")
                if full_path.exists() and edit_old is not None:
                    # 有文件快照时直接还原
                    full_path.parent.mkdir(parents=True, exist_ok=True)
                    full_path.write_text(edit_old, encoding="utf-8")
                else:
                    # 无 old_content（旧版本记录）或路径不存在，fallback 到字符串替换
                    old_str = record.get("old_string")
                    new_str = record.get("new_string")
                    if old_str is not None and new_str is not None:
                        try:
                            current = full_path.read_text(encoding="utf-8")
                            restored = current.replace(new_str, old_str, 1)
                            full_path.write_text(restored, encoding="utf-8")
                        except (OSError, ValueError) as e:
                            logging.warning(f"撤销 edit 失败 {path}: {e}")
        except OSError as e:
            logging.warning(f"撤销文件变更失败 {path}: {e}")

        return record

    # ── 权限评估 ──────────────────────────────────────────────

    # P1-3: 安全命令前缀和危险关键词已抽到 iron.agent.risk_evaluator 模块
    # 保留类属性引用以维持向后兼容（外部测试可能访问 self._SAFE_COMMANDS）
    _SAFE_COMMANDS = _MODULE_SAFE_COMMANDS
    _DANGEROUS_KEYWORDS = _MODULE_DANGEROUS_KEYWORDS

    # 只读外部工具（无副作用，可并行执行，参考 Claude Code 并行工具调用）
    # 注意：read_file 有专门处理（含 UI 事件），不走并行分支，但会在执行前 flush pending
    # embed_lint 是只读静态分析工具，可并行执行
    # LSP 5 个工具（diagnostics/definition/references/hover/completion）是只读查询，可并行执行
    _READONLY_EXTERNAL_TOOLS = {
        "search_code", "find_files", "web_search", "embed_lint",
        "lsp_diagnostics", "lsp_definition", "lsp_references", "lsp_hover", "lsp_completion",
        # v3.0: 4 个语义工具均为只读查询
        "semantic_search", "get_callers", "get_callees", "find_dead_code",
        # v4.0 Track 5: 3 个只读 Git 工具（git_add/git_commit 需权限，不在此集合）
        "git_status", "git_diff", "git_log",
    }

    # 只读工具的 action 参数（某些工具的特定 action 才是只读的）
    _READONLY_ACTIONS = {
        "embed_build": {"info"},        # embed_build(action=info) 只读
        "task_track": {"list"},         # task_track(action=list) 只读
        "mcp_config": {"list", "search"},  # mcp_config(action=list/search) 只读
    }

    def _evaluate_command_risk(self, command: str) -> str:
        """评估命令风险等级（P1-3: 委托给 iron.agent.risk_evaluator 模块）

        支持复合命令（&&、||、|、;）拆分检查：
        - 全部子命令安全 → safe
        - 任一子命令危险 → dangerous
        - 有未知子命令 → dangerous

        Returns:
            "safe" — 自动允许
            "dangerous" — 需要用户授权
        """
        return evaluate_command_risk(command)

    def _evaluate_write_risk(self, path: str) -> str:
        """评估文件写入风险等级

        Returns:
            "safe" — 项目目录内写入，自动允许
            "dangerous" — 项目目录外或特殊文件，需要授权
        """
        try:
            full_path = self._resolve_project_path(path).resolve()
            project_dir = Path(self._get_project_dir()).resolve()
            # 用 relative_to 做边界检查，避免 startswith 被同前缀目录绕过
            # （例如项目 C:\\project 可被 C:\\project_evil\\x.txt 绕过）
            full_path.relative_to(project_dir)
            # 项目目录内 → 安全（但 .env、credentials 等敏感文件需要授权）
            name = full_path.name.lower()
            # 复用 _LOWER_SENSITIVE_NAMES，与读取侧保持一致（含 .npmrc/.pypirc）
            if name in _LOWER_SENSITIVE_NAMES:
                return "dangerous"
            # 敏感扩展名检查（*.pem/*.key/*.p12/*.pfx/*.keystore）
            if any(pat.search(name) for pat in _SENSITIVE_SUFFIX_PATTERNS):
                return "dangerous"
            return "safe"
        except ValueError:
            # relative_to 抛 ValueError 表示路径越界 → 项目目录外
            return "dangerous"
        except OSError:
            return "dangerous"

    async def _request_permission(self, action: str, target: str, details: str,
                                   risk: str = "safe", args: dict = None) -> bool:
        """请求用户授权 — 根据风险等级决定是否弹窗

        - risk="safe": 自动允许，不弹窗
        - risk="dangerous": 弹窗请求用户确认

        info dict 增加 args 字段，与 _check_permission_with_callback 保持一致。
        """
        # 安全操作：直接允许
        if risk == "safe":
            return True

        # 没有回调：默认允许
        if self._permission_callback is None:
            return True

        # 危险操作：弹窗请求授权
        info = {"action": action, "target": target, "details": details, "args": args or {}}
        try:
            result = await asyncio.to_thread(self._permission_callback, info)
            return bool(result)
        except asyncio.CancelledError:
            raise
        except (OSError, RuntimeError) as e:
            # fail-safe — 异常时拒绝（False），避免授权回调异常导致危险操作被放行
            logging.warning(f"权限回调异常，按 fail-safe 拒绝: {e}")
            return False

    def _resolve_project_path(self, relative_path: str) -> Path:
        """解析项目相对路径，并强制边界校验防止路径越界"""
        project_dir = self._get_project_dir()
        full_path = (Path(project_dir) / relative_path).resolve()
        project_root = Path(project_dir).resolve()
        try:
            full_path.relative_to(project_root)
        except ValueError:
            raise ValueError(f"路径越界：禁止访问项目目录外的文件: {relative_path}")
        # 检查 Windows 保留设备名（CON/PRN/AUX/NUL/COM1-9/LPT1-9，含 CON.txt 形式）
        # 与 path_guard.validate_path_in_project 保持一致，避免绕过保留名检查
        name_upper = full_path.name.upper().split(".")[0]
        if name_upper in _WIN_RESERVED_NAMES:
            raise ValueError(f"路径包含 Windows 保留设备名: {full_path.name}")
        return full_path

    def _get_project_dir(self) -> str:
        """获取项目目录"""
        project_dir = "."
        if self.config is not None and hasattr(self.config, "project"):
            project_dir = getattr(self.config.project, "project_dir", ".") or "."
        return project_dir

    def _get_console(self):
        """v4.0 Track 6: 懒加载 Rich Console（仅供 diff 预览等工具渲染用）

        延迟 import 避免顶层 rich 依赖影响 headless 测试；测试环境可通过
        直接设置 self._console 注入 mock console。
        """
        if self._console is None:
            try:
                from rich.console import Console
                self._console = Console()
            except ImportError:
                return None
        return self._console

    def _detect_language(self, file_path: str) -> str:
        """检测文件语言"""
        suffix = Path(file_path).suffix.lower()
        mapping = {
            ".c": "c", ".h": "c", ".cpp": "cpp", ".cc": "cpp",
            ".cxx": "cpp", ".hpp": "cpp", ".hh": "cpp",
            ".rs": "rust", ".py": "python", ".js": "javascript",
            ".ts": "typescript", ".go": "go", ".java": "java",
            ".asm": "asm", ".s": "asm", ".S": "asm",
            ".ld": "linker", ".md": "markdown", ".json": "json",
            ".yaml": "yaml", ".yml": "yaml", ".toml": "toml",
            ".txt": "text", ".sh": "bash",
        }
        return mapping.get(suffix, "text")

    def _estimate_input_tokens(self, system: str, messages: list[dict]) -> int:
        """计算输入 token 数

        优先用 tiktoken 精确计数（cl100k_base 编码，兼容 GPT-4/DeepSeek/Qwen 等），
        不可用时 fallback 到字符数 / 4 估算。
        """
        try:
            from iron.utils.token_counter import count_messages_tokens
            return count_messages_tokens(system, messages, self._tools_schema)
        except ImportError:
            total_chars = len(system)
            for msg in messages:
                content = msg.get("content", "")
                if isinstance(content, str):
                    total_chars += len(content)
                elif isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and "text" in part:
                            total_chars += len(part["text"])
            return max(1, total_chars // 4)

    def _build_file_tree(self) -> list[str]:
        """构建项目文件树（带缓存，同一轮 process 只需扫描一次）"""
        if self._file_tree_loaded:
            return self._cached_file_tree

        project_dir = "."
        if self.config is not None and hasattr(self.config, "project"):
            project_dir = getattr(self.config.project, "project_dir", ".") or "."

        root = Path(project_dir)
        if not root.exists() or not root.is_dir():
            return []

        ignore_dirs = {".git", ".idea", ".vscode", "__pycache__", "node_modules",
                       "build", "dist", ".cache", ".trae-cn", ".iron"}
        ignore_suffixes = {".pyc", ".pyo", ".log"}

        files: list[str] = []
        try:
            for item in sorted(root.rglob("*")):
                if not item.is_file():
                    continue
                if any(part in ignore_dirs for part in item.parts):
                    continue
                if item.suffix.lower() in ignore_suffixes:
                    continue
                try:
                    rel = item.relative_to(root)
                    files.append(str(rel).replace("\\", "/"))
                except ValueError:
                    continue
        except OSError:
            return []

        self._cached_file_tree = files
        self._file_tree_loaded = True
        return files


# ── P1-4: 双 Agent 类型（参考 OpenCode Coder/Task 双 Agent 设计） ──────


class CoderAgentEngine(BaseAgentEngine):
    """Coder Agent — 完整工具集（写工具 + 读工具 + 编译/烧录）

    用于实际编码任务，拥有完整权限。与 BaseAgentEngine 行为一致，
    显式声明语义：默认编码 Agent 使用此类。
    """

    def _get_allowed_tools(self) -> set[str] | None:
        """Coder Agent 允许全部工具"""
        return None  # 全部允许

    def _get_system_prompt_prefix(self) -> str:
        """Coder Agent 不需要角色前缀（保持默认系统提示）"""
        return ""


class TaskAgentEngine(BaseAgentEngine):
    """Task Agent — 只读工具集（用于探索/规划/分析）

    参考 OpenCode TaskAgent：限制为只读工具，不能修改文件、
    不能编译/烧录，用于安全的代码库探索和方案规划。

    被阻止的工具调用会在 process() 中产生 tool_blocked 事件，
    并把阻止结果送回 AI，告知其当前处于只读模式。
    """

    # 只读工具集合 — 这些工具不修改源码、不执行命令、不烧录
    # embed_lint 是只读静态分析工具（EmbedGuard），可安全暴露给 Task Agent
    # LSP 5 个工具是只读查询，可安全暴露给 Task Agent
    READONLY_TOOLS = {
        "read_file", "search_code", "find_files", "web_search",
        "task_track", "ask_user", "remember", "chat", "embed_lint",
        "lsp_diagnostics", "lsp_definition", "lsp_references", "lsp_hover", "lsp_completion",
        # v3.0: 语义工具对只读 Agent 也可用
        "semantic_search", "get_callers", "get_callees", "find_dead_code",
        # v4.0 Track 5: 只读 Git 工具对 Task Agent 也可用（git_add/git_commit 不在此集合）
        "git_status", "git_diff", "git_log",
    }

    def _get_allowed_tools(self) -> set[str]:
        """Task Agent 只允许只读工具"""
        return self.READONLY_TOOLS

    def _get_system_prompt_prefix(self) -> str:
        """Task Agent 角色前缀 — 标注只读模式"""
        return (
            "你是只读探索 Agent，只能读取和分析代码，"
            "不能修改文件或执行构建/烧录。"
            "用于代码库探索、方案规划、代码审查。\n\n"
        )


# ── P3-4: 专门化子代理扩展（verify + explore） ────────────────


class VerifyAgent(TaskAgentEngine):
    """验证代理 — 自动跑测试 + 静态分析 + LSP 诊断

    继承 TaskAgentEngine 的只读工具集，额外允许 run_command_readonly
    （只读命令执行：编译检查、lint、test），用于自动验证代码质量。

    verify() 方法内部调用 process()，复用 ReAct 循环，
    不重复实现工具调用逻辑。
    """

    # 验证工具集 = TaskAgent 只读工具 + run_command_readonly（只读命令）
    VERIFY_TOOLS = TaskAgentEngine.READONLY_TOOLS | {"run_command_readonly"}

    def _get_allowed_tools(self) -> set[str]:
        """VerifyAgent 允许只读工具 + run_command_readonly"""
        return self.VERIFY_TOOLS

    def _get_system_prompt_prefix(self) -> str:
        """VerifyAgent 角色前缀 — 标注验证代理身份"""
        return (
            "你是验证代理，专注于发现代码问题。"
            "你能读取代码、运行静态分析、检查 LSP 诊断、执行只读命令。"
            "你不能修改文件。"
            "你的任务是：1) 识别潜在 bug 2) 检查代码规范 "
            "3) 验证逻辑正确性 4) 给出改进建议。\n\n"
        )

    async def verify(self, target: str = "src/") -> dict:
        """执行完整验证流程

        改造后：
        1. 显式收集 source 文件列表（_collect_source_files）
        2. asyncio.gather 并行调用 lsp_diagnostics（约束 C4）
        3. 汇总诊断结果
        4. 通过 process() 让 LLM 综合分析（静态分析 + 编译 + LSP 诊断）

        Args:
            target: 待验证的目标路径或文件，默认 "src/"

        Returns:
            包含 target/events/lsp_diagnostics/status 的汇总字典
        """
        # 阶段 1：显式收集 LSP 诊断（不依赖 LLM 自觉调用）
        lsp_diags_summary = await self._collect_lsp_diagnostics(target)

        # 阶段 2：通过 process() 让 LLM 综合分析
        prompt = (
            f"请验证 {target} 目录的代码质量。按以下步骤执行：\n"
            "1. 用 embed_lint 进行静态分析\n"
            "2. 运行编译检查（platformio run，只读不烧录）\n"
            "3. 给出问题列表（按严重度排序）和整体评估（通过/警告/失败）\n\n"
            f"已收集的 LSP 诊断（供参考，无需重复调用）：\n{lsp_diags_summary}"
        )
        events = []
        async for event in self.process(prompt):
            events.append(event)
        return {
            "target": target,
            "events": events,
            "lsp_diagnostics": lsp_diags_summary,
            "status": "completed",
        }

    async def _collect_source_files(self, target: str) -> list[str]:
        """收集目标路径下的 C/C++ 源文件

        限制最多 50 个文件，避免 LSP 过载。

        Args:
            target: 目标路径（文件或目录）

        Returns:
            文件路径字符串列表（最多 50 个）
        """
        target_path = Path(target)
        if target_path.is_file():
            return [str(target_path)]
        if not target_path.is_dir():
            return []
        files: list[str] = []
        for ext in {".c", ".h", ".cpp", ".cc", ".cxx", ".hpp", ".hh"}:
            files.extend(str(p) for p in target_path.rglob(f"*{ext}"))
        return files[:50]  # 限制最多 50 个文件，避免 LSP 过载

    async def _collect_lsp_diagnostics(self, target: str) -> str:
        """并行收集 LSP 诊断（约束 C4：asyncio.gather）

        LSP 未启动时降级返回跳过提示（约束 C5）。

        Args:
            target: 目标路径（文件或目录）

        Returns:
            诊断汇总字符串（每文件最多 5 条诊断）
        """
        if not self._lsp_client or not getattr(self._lsp_client, "_initialized", False):
            return "LSP 未启动，跳过诊断"
        files = await self._collect_source_files(target)
        if not files:
            return f"未在 {target} 下找到 C/C++ 源文件"
        # 并行调用 get_diagnostics（约束 C4）
        tasks = [self._lsp_client.get_diagnostics(f) for f in files]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        lines: list[str] = []
        for f, diags in zip(files, results):
            if isinstance(diags, Exception):
                lines.append(f"  {f}: 诊断失败 ({diags})")
                continue
            if diags:
                lines.append(f"  {f}: {len(diags)} 个诊断")
                for d in diags[:5]:  # 每文件最多 5 条
                    lines.append(f"    L{d.line}: [{d.severity}] {d.message}")
        return "\n".join(lines) if lines else "无诊断"


class ExploreAgent(TaskAgentEngine):
    """探索代理 — 只读理解代码库，回答架构问题

    纯只读工具集（无 run_command），允许 LSP 跳转/引用/悬停工具，
    用于代码库探索、架构理解、调用链追踪。

    explore() 方法内部调用 process()，复用 ReAct 循环。
    """

    # 探索工具集 = 纯只读工具 + LSP 跳转/引用/悬停
    EXPLORE_TOOLS = {
        "read_file", "list_files", "search_code", "grep", "glob",
        "lsp_definition", "lsp_references", "lsp_hover",
    }

    def _get_allowed_tools(self) -> set[str]:
        """ExploreAgent 允许纯只读工具 + LSP 跳转工具"""
        return self.EXPLORE_TOOLS

    def _get_system_prompt_prefix(self) -> str:
        """ExploreAgent 角色前缀 — 标注探索代理身份"""
        return (
            "你是探索代理，专注于理解代码架构。"
            "你能读取文件、搜索代码、跳转定义、查找引用。"
            "你不能修改文件或执行命令。"
            "你的任务是：1) 理解代码结构 2) 追踪调用链 "
            "3) 回答'这段代码做什么' 4) 生成架构概览。\n\n"
        )

    async def explore(self, query: str) -> dict:
        """探索代码库

        通过 process() 驱动 LLM 回答探索性问题：
        1. 根据查询理解意图
        2. 调用 process() 让 LLM 回答
        3. 返回结构化结果（架构概览 / 调用链 / 关键函数说明）

        Args:
            query: 用户的探索性问题（如"这段代码做什么"、"架构是什么"）

        Returns:
            包含 query/events/status 的汇总字典
        """
        events = []
        # 复用 process() 的 ReAct 循环，不重复实现
        async for event in self.process(query):
            events.append(event)
        return {
            "query": query,
            "events": events,
            "status": "completed",
        }


# ── 向后兼容别名 ─────────────────────────────────────────────
# AgentEngine: 现有所有 AgentEngine(...) 调用无需修改
# 等价于 CoderAgentEngine（全部工具允许），保持原有行为
# P1-4 前 AgentEngine = BaseAgentEngine（非抽象），P1-4 后 BaseAgentEngine 为抽象类，
# 不能直接实例化，因此 AgentEngine 改为指向 CoderAgentEngine（行为等价）
AgentEngine = CoderAgentEngine

# P1-4 命名规范别名（满足任务规范的简短命名）
CoderAgent = CoderAgentEngine
TaskAgent = TaskAgentEngine

