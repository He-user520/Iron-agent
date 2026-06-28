"""启动管道 — 分阶段初始化

参考 Claude Code 的 7 阶段启动管道设计（简化为 3 阶段）：
1. 配置阶段：加载全局/项目配置 + 环境变量
2. 信任阶段：验证 API Key + 扩展可用性
3. 运行阶段：初始化 PromptBuilder + Skills + 主题（MCP/LSP 按需延迟初始化）

每阶段显示进度，失败时优雅降级（配置失败除外，配置失败直接终止）。
阶段间错误隔离：信任阶段或运行阶段失败不阻塞后续阶段（仅记录错误/警告）。

用法:
    bootstrap = Bootstrap(console)
    result = bootstrap.run(project_root, mcu, model, backend, verbose)
    if result.success:
        run_interactive(result.config, project_root)
    else:
        for err in result.errors:
            console.print(err, style="red")
"""
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

logger = logging.getLogger(__name__)


@dataclass
class BootstrapResult:
    """启动结果

    封装启动管道所有阶段的产出，供调用方（cli 函数）使用。
    success=True 时 config/llm/prompt_builder/skills 可用；
    success=False 时 errors 包含失败原因。
    """
    success: bool = False
    config: Optional[object] = None      # IronConfig
    llm: Optional[object] = None          # LLMBackend
    prompt_builder: Optional[object] = None
    skills: Optional[object] = None       # SkillRegistry
    lsp_client: Optional[object] = None   # LSPClient（特性门控 lsp_tools 控制）
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    # 阶段执行顺序记录（用于诊断与测试）
    phases_executed: list = field(default_factory=list)


class Bootstrap:
    """启动管道 — 分阶段初始化 Iron CLI 环境

    设计原则：
    - 配置阶段失败 → 直接终止（后续阶段无 config 可用）
    - 信任阶段失败 → 不阻塞（降级到 None，run_interactive 会用 EchoBackend 兜底）
    - 运行阶段失败 → 不阻塞（记录错误，尽量完成可完成的部分）
    - 每阶段失败记录到 errors/warnings，最终汇总返回

    用法:
        bootstrap = Bootstrap(console)
        result = bootstrap.run(project_root, mcu="stm32f407", backend="echo")
        if result.success:
            run_interactive(result.config, project_root)
    """

    def __init__(self, console: Console = None):
        self.console = console or Console()
        self._errors: list = []
        self._warnings: list = []
        self._phases_executed: list = []

    def run(self, project_root: Path, mcu: str = None, model: str = None,
            backend: str = None, verbose: bool = False) -> BootstrapResult:
        """运行 3 阶段启动管道

        Args:
            project_root: 项目根目录路径
            mcu: 目标 MCU 覆盖值（如 stm32f407）
            model: AI 模型覆盖值
            backend: LLM 后端覆盖值（openai/anthropic/ollama/echo）
            verbose: 是否启用详细输出

        Returns:
            BootstrapResult: 启动结果
        """
        # 重置状态（支持同一实例多次调用）
        self._errors = []
        self._warnings = []
        self._phases_executed = []

        with Progress(
            SpinnerColumn(),
            TextColumn("[bold cyan]{task.description}[/bold cyan]"),
            console=self.console,
            transient=True,
        ) as progress:
            # 阶段 1: 配置（失败则直接终止）
            task1 = progress.add_task("[1/3] 加载配置...", total=None)
            config = self._phase_config(project_root, mcu, model, backend, verbose)
            if config is None:
                progress.update(task1, description="[red]✗ 配置加载失败")
                return BootstrapResult(
                    success=False,
                    errors=self._errors,
                    warnings=self._warnings,
                    phases_executed=self._phases_executed,
                )
            progress.update(task1, description="[green]✓ 配置加载完成")

            # 阶段 2: 信任（失败不阻塞，降级处理）
            task2 = progress.add_task("[2/3] 验证环境...", total=None)
            llm = self._phase_trust(config)
            progress.update(task2, description="[green]✓ 环境验证完成")

            # 阶段 3: 运行（失败不阻塞，降级处理）
            task3 = progress.add_task("[3/3] 初始化引擎...", total=None)
            prompt_builder, skills, lsp_client = self._phase_run(config, project_root)
            progress.update(task3, description="[green]✓ 引擎初始化完成")

        return BootstrapResult(
            success=True,
            config=config,
            llm=llm,
            prompt_builder=prompt_builder,
            skills=skills,
            lsp_client=lsp_client,
            errors=self._errors,
            warnings=self._warnings,
            phases_executed=self._phases_executed,
        )

    def _phase_config(self, project_root: Path, mcu: str, model: str,
                      backend: str, verbose: bool):
        """阶段 1: 加载配置（全局/项目配置 + 环境变量覆盖）

        失败时返回 None，run() 会立即终止后续阶段。
        """
        self._phases_executed.append("config")
        try:
            from iron.config.settings import IronConfig
            config = IronConfig.load(project_root)
            config.verbose = verbose
            # CLI 参数覆盖配置（优先级最高）
            if mcu:
                config.project.mcu = mcu
            if model:
                config.llm.model = model
            if backend:
                config.llm.backend = backend
            logger.debug("配置加载成功: backend=%s, mcu=%s",
                         config.llm.backend, config.project.mcu)
            return config
        except Exception as e:
            logger.exception("配置加载失败")
            self._errors.append(f"配置加载失败: {e}")
            return None

    def _phase_trust(self, config):
        """阶段 2: 验证环境（创建 LLM 后端 + 检查 API Key）

        创建 LLM 后端实例，验证 API Key 是否配置。
        失败时不阻塞后续阶段（返回 None），run_interactive 会用 EchoBackend 兜底。
        """
        self._phases_executed.append("trust")
        try:
            from iron.llm.backend import create_backend
            llm = create_backend(config.llm.backend, config)

            # 验证 API Key（缺失时仅警告，不阻塞启动；
            # EchoBackend/OllamaBackend 不需要 API Key，但统一记录便于诊断）
            if not config.llm.api_key:
                self._warnings.append("API Key 未设置")

            logger.debug("环境验证成功: backend=%s", config.llm.backend)
            return llm
        except Exception as e:
            logger.exception("LLM 后端创建失败")
            self._errors.append(f"LLM 后端创建失败: {e}")
            # 信任阶段失败不阻塞，降级返回 None
            return None

    def _phase_run(self, config, project_root: Path):
        """阶段 3: 初始化引擎共享组件

        初始化 PromptBuilder + SkillRegistry + 应用主题。
        注：AgentEngine 在每次对话时按需创建（参考 run_interactive._run_agent），
        MCP 客户端和 LSP 客户端由 AgentEngine 延迟初始化，此处仅做可用性预检。

        P6-2: 特性门控 — 加载 ~/.iron/features.yml，后续组件初始化受特性开关控制。
        失败时返回 (None, None)，不阻塞启动（run_interactive 会重建）。
        """
        self._phases_executed.append("run")
        try:
            from iron.agent.prompt_builder import PromptBuilder
            from iron.skills.registry import SkillRegistry

            # P6-2: 加载特性门控（失败不阻塞，使用默认值）
            try:
                from iron.config.features import get_feature_flags
                flags = get_feature_flags()
                logger.debug(
                    "特性门控已加载: 启用=%d, 禁用=%d",
                    len(flags.list_enabled()),
                    len(flags.list_disabled()),
                )
                # 特性门控可用性预检：lsp_tools 默认关闭，需要 clangd 才启用
                # 此处仅记录状态，实际 LSP 客户端由 AgentEngine 按需初始化
                if flags.is_enabled("lsp_tools"):
                    logger.debug("lsp_tools 特性已启用，将在 AgentEngine 中初始化 LSP 客户端")
            except Exception as e:
                logger.warning("特性门控加载失败，使用默认值: %s", e)
                self._warnings.append(f"特性门控加载失败: {e}")

            # LSP 客户端初始化（特性门控 lsp_tools + 启动失败降级到 None）
            # 约束 C1：start() 失败或异常时降级为 lsp_client=None，主流程继续
            lsp_client = None
            try:
                from iron.config.features import get_feature_flags
                if get_feature_flags().is_enabled("lsp_tools"):
                    from iron.integrations.lsp_client import LSPClient, LSPConfig
                    import asyncio
                    cc_path = LSPClient.find_compile_commands(project_root)
                    lsp_config = LSPConfig(
                        enabled=True,
                        compile_commands_dir=str(cc_path.parent) if cc_path else "",
                    )
                    lsp_client = LSPClient(lsp_config, project_root=str(project_root))
                    # bootstrap 是同步函数，asyncio.run 安全（run_interactive 尚未启动 loop）
                    started = asyncio.run(lsp_client.start())
                    if not started:
                        logger.warning("LSP 客户端启动失败，降级到无 LSP 模式")
                        self._warnings.append("LSP 客户端启动失败，降级到无 LSP 模式")
                        lsp_client = None
                    else:
                        logger.debug("LSP 客户端启动成功")
                else:
                    logger.debug("lsp_tools 特性未启用，跳过 LSP 初始化")
            except Exception as e:
                logger.exception("LSP 初始化异常")
                self._warnings.append(f"LSP 初始化失败: {e}")
                lsp_client = None

            prompt_builder = PromptBuilder(project_root, config.project.mcu)
            skills = SkillRegistry()

            # 应用主题（失败时降级到默认主题，不阻塞启动）
            try:
                from iron.cli.theme import set_theme
                set_theme(config.theme)
            except Exception as e:
                logger.warning("主题加载失败: %s", e)
                self._warnings.append(f"主题加载失败: {e}")

            # MCP 配置预检（仅验证配置格式，不实际连接）
            self._validate_mcp_configs(config)

            logger.debug("引擎初始化成功")
            return prompt_builder, skills, lsp_client
        except Exception as e:
            logger.exception("引擎初始化失败")
            self._errors.append(f"引擎初始化失败: {e}")
            return None, None, None

    @staticmethod
    def _validate_mcp_configs(config):
        """验证 MCP 服务器配置格式（不实际连接）

        仅检查配置项是否完整，记录警告便于用户排查。
        实际连接在 AgentEngine 首次对话时延迟建立。
        """
        mcp_configs = getattr(config, "mcp", None) or {}
        for name, srv_cfg in mcp_configs.items():
            if not getattr(srv_cfg, "enabled", True):
                continue
            # stdio 类型需要 command
            srv_type = getattr(srv_cfg, "type", "local")
            if srv_type in ("local", "stdio"):
                if not getattr(srv_cfg, "command", ""):
                    logger.warning("MCP 服务器 %s 缺少 command 字段", name)
            # sse/http 类型需要 url
            elif srv_type in ("sse", "http"):
                if not getattr(srv_cfg, "url", ""):
                    logger.warning("MCP 服务器 %s 缺少 url 字段", name)
