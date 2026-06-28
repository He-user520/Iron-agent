"""Iron 配置管理"""
import logging
import os
import re
from pathlib import Path
from dataclasses import dataclass, field
import yaml


DEFAULT_CONFIG_DIR = Path.home() / ".iron"
DEFAULT_PROJECT_DIR = ".iron-agent"

# 环境变量占位符正则：${VAR_NAME}（仅匹配大写字母/数字/下划线，避免误伤 shell 变量）
_ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")

logger = logging.getLogger(__name__)


def _expand_env(value):
    """递归展开 ${VAR} 占位符为环境变量值

    用于 MCP 配置 round-trip（save→load）后恢复 env 真实值：
    settings.save() 将 env 值替换为 ${KEY} 占位符落盘，
    加载时通过本函数从 os.environ 还原真实值。
    未定义的环境变量展开为空字符串（并记录 warning）。
    """
    if isinstance(value, str):
        def _replace(m):
            var_name = m.group(1)
            val = os.environ.get(var_name, "")
            if not val:
                logger.warning("环境变量 %s 未设置，${%s} 展开为空字符串", var_name, var_name)
            return val
        return _ENV_PATTERN.sub(_replace, value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def _mask_env_values(value, env_reverse_cache):
    """如果 value 等于某个环境变量的值，替换为 ${VAR_NAME} 占位符

    用于 save() 时防止 headers/url 等字段明文落盘。
    env 字段用 KEY 名直接生成占位符（env KEY 名通常就是环境变量名），
    但 headers KEY 名是 HTTP header 名（如 Authorization），不是环境变量名，
    所以用反向映射：如果 headers 值等于某个环境变量的值，替换为 ${VAR_NAME}。
    非 environment 变量值保留原值（支持用户手动写明文 headers）。

    注意：返回值必须用 ${...} 包裹，与 env 字段占位符格式一致，
    这样 load() 时 __post_init__ 的 _expand_env 才能正确展开还原。
    """
    if not isinstance(value, str) or not value:
        return value
    var_name = env_reverse_cache.get(value)
    if var_name:
        return "${" + var_name + "}"
    return value


@dataclass
class LLMConfig:
    backend: str = "openai"  # openai / anthropic / ollama / echo
    model: str = "gpt-4o"
    small_model: str = ""    # v2: 轻量任务用的小模型（压缩/摘要）
    api_key: str = ""
    base_url: str = ""
    temperature: float = 0.3
    max_tokens: int = 4096
    request_timeout: int = 120   # LLM 请求超时（秒）
    available_models: list = field(default_factory=list)


def _normalize_provider_name(name: str) -> str:
    """标准化厂商名为环境变量可用形式（大写 ASCII 字母数字/下划线）

    例如 "OpenAI" → "OPENAI"，"小米 MiMo" → "MIMO"（去除非 ASCII）。
    """
    kept = "".join(c for c in name if (c.isascii() and (c.isalnum() or c == "_")))
    return kept.upper()


@dataclass
class ProviderConfig:
    """单个 LLM 厂商配置（多厂商支持）

    一个厂商 = 一个 base_url + 一组 available_models + 一个默认 model。
    API Key 不落盘（安全考虑），运行时通过环境变量 IRON_API_KEY_<NAME> 加载。
    """
    name: str = ""              # 厂商友好名（如 "OpenAI", "MiMo"）
    backend: str = "openai"     # openai/anthropic/ollama/echo
    base_url: str = ""
    api_key: str = ""           # 不落盘（运行时从 env 加载）
    available_models: list = field(default_factory=list)
    model: str = ""             # 该厂商的默认模型

    @property
    def env_var_name(self) -> str:
        """该 provider 对应的环境变量名 IRON_API_KEY_<NAME>

        name 为空时回退到 IRON_API_KEY（兼容旧配置）。
        """
        normalized = _normalize_provider_name(self.name)
        if not normalized:
            return "IRON_API_KEY"
        return f"IRON_API_KEY_{normalized}"


@dataclass
class ProjectConfig:
    name: str = ""
    mcu: str = "stm32f407"
    language: str = "c"
    framework: str = "hal"
    build_system: str = "platformio"
    rules_enabled: bool = True
    skills_enabled: bool = True


@dataclass
class MCPConfig:
    """MCP 服务器配置（统一为 Claude Code 风格：command + args + env / url + headers）"""
    type: str = "local"           # local/stdio | sse | http
    command: str = ""            # stdio: 启动命令（如 "npx" 或 "python"）
    args: list = field(default_factory=list)  # stdio: 命令参数列表
    env: dict = field(default_factory=dict)  # stdio: 环境变量
    url: str = ""                 # sse/http: 远程 MCP URL
    headers: dict = field(default_factory=dict)  # sse/http: 自定义请求头（如 Authorization）
    enabled: bool = True
    timeout: int = 5000  # 毫秒

    def __post_init__(self):
        """字段类型规范化（兼容旧格式）"""
        # 旧格式兼容：command 是 list 时，第一个元素为 command，其余并入 args
        if isinstance(self.command, list):
            parts = self.command
            self.command = parts[0] if parts else ""
            self.args = list(parts[1:]) + list(self.args or [])
        if not isinstance(self.command, str):
            self.command = str(self.command) if self.command is not None else ""
        if not isinstance(self.args, list):
            self.args = []
        if not isinstance(self.env, dict):
            self.env = {}
        if not isinstance(self.headers, dict):
            self.headers = {}
        if not isinstance(self.type, str):
            self.type = "local" if self.type else "local"
        if not isinstance(self.enabled, bool):
            self.enabled = bool(self.enabled)
        # 展开 env / headers / url / command / args 中的 ${VAR} 占位符
        # （settings.save() 落盘时 env 值存为 ${KEY} 占位符，加载时在此还原）
        self.env = _expand_env(self.env)
        self.headers = _expand_env(self.headers)
        self.url = _expand_env(self.url)
        self.command = _expand_env(self.command)
        self.args = _expand_env(self.args)

    def build_command(self) -> list[str]:
        """构建完整的启动命令列表（command + args）"""
        cmd = [self.command] if self.command else []
        cmd.extend(str(a) for a in self.args)
        return cmd


@dataclass
class IronConfig:
    llm: LLMConfig = field(default_factory=LLMConfig)
    project: ProjectConfig = field(default_factory=ProjectConfig)
    config_dir: Path = field(default_factory=lambda: DEFAULT_CONFIG_DIR)
    project_dir: Path = field(default_factory=lambda: Path(DEFAULT_PROJECT_DIR))
    max_fix_rounds: int = 3
    verbose: bool = False
    # v2 新增
    default_agent: str = "build"
    max_steps: int = 50  # 任务完成驱动，步数仅作安全网（参考 Claude Code）
    # run_command 超时可配置，默认 300 秒（5 分钟）
    # 嵌入式项目编译（platformio/idf.py/cmake）普遍 >30 秒，原硬编码 30 秒会导致编译必失败
    run_command_timeout: int = 300
    # P1-2: Stop Hooks 收敛检测器配置（向后兼容，全部带默认值）
    # stop_hooks_enabled=False 时跳过所有 stop_hook 检查
    stop_hooks_enabled: bool = True
    max_consecutive_failures: int = 5    # 连续失败上限
    max_tool_repetition: int = 10        # 同工具连续调用上限
    no_progress_steps: int = 8           # 无新信息步数上限
    # P1-3: 系统提示分块缓存（参考 Claude Code 两块缓存策略）
    # prompt_caching_enabled=False 时关闭缓存，每次请求重新计算
    prompt_caching_enabled: bool = True
    prompt_cache_ttl: int = 300  # 缓存 TTL（秒），默认 5 分钟
    mcp: dict = field(default_factory=dict)  # name -> MCPConfig
    # 多厂商支持：providers 列表存储所有厂商配置，active_provider 标记当前活跃厂商
    # llm 字段保留为活跃 provider 的运行时快照，向后兼容
    providers: list = field(default_factory=list)  # list[ProviderConfig]
    active_provider: str = ""  # 厂商名（对应 ProviderConfig.name）
    # API Key 保存策略：True=落盘到 config.yml（方便但有风险，文件权限 0o600）
    # False=不落盘，需通过环境变量 IRON_API_KEY_<NAME> 提供（默认，安全）
    # 用户在 setup_interactive 中选择
    save_api_key: bool = False
    # P2-1: 规则评估引擎配置 — DSL 驱动的权限规则
    # permission_rules_enabled=False 时跳过规则评估，回退到 _EXTERNAL_WRITE_TOOLS 白名单
    # permission_rules_file 指定自定义规则文件路径（空=使用默认 ~/.iron/rules.yml + .iron-agent/rules.yml）
    permission_rules_enabled: bool = True
    permission_rules_file: str = ""
    # P4-3: 工具输出截断阈值（字符数），超过自动截断避免上下文 token 浪费
    tool_output_max_chars: int = 10000
    # P5-2: 主题系统 — default / catppuccin / dracula
    theme: str = "default"

    @classmethod
    def load(cls, project_root: Path | None = None) -> "IronConfig":
        config = cls()

        # 1. 加载全局配置
        global_config = DEFAULT_CONFIG_DIR / "config.yml"
        if global_config.exists():
            config._merge_yaml(global_config)

        # 2. 加载项目级配置
        if project_root:
            for name in ("iron.yml", "iron.yaml", ".iron.yml"):
                project_config = project_root / name
                if project_config.exists():
                    config._merge_yaml(project_config)
                    break

        # 3. 环境变量覆盖（优先级高于文件配置）
        if os.environ.get("IRON_BACKEND"):
            config.llm.backend = os.environ["IRON_BACKEND"]
        if os.environ.get("IRON_MODEL"):
            config.llm.model = os.environ["IRON_MODEL"]
        if os.environ.get("IRON_BASE_URL"):
            config.llm.base_url = os.environ["IRON_BASE_URL"]
        if os.environ.get("IRON_MCU"):
            config.project.mcu = os.environ["IRON_MCU"]

        # 4. 多厂商支持：从环境变量加载所有 provider 的 API Key
        #    每个 provider 按 name 标准化为 IRON_API_KEY_<NAME>；
        #    第一个 provider（或 active provider）额外兼容旧 IRON_API_KEY / OPENAI_API_KEY
        if config.providers:
            config._resolve_provider_keys_from_env()
            # 同步 active provider 到 llm 字段（向后兼容字段）
            config._apply_active_provider_to_llm()
        else:
            # 旧配置兼容：未配置 providers，从环境变量加载 llm.api_key
            api_key_env = os.environ.get("IRON_API_KEY") or os.environ.get("OPENAI_API_KEY")
            if api_key_env:
                config.llm.api_key = api_key_env

        return config

    def get_active_provider(self):
        """获取当前活跃的 provider；若 active_provider 名不存在则回退到第一个"""
        for p in self.providers:
            if p.name == self.active_provider:
                return p
        return self.providers[0] if self.providers else None

    def _apply_active_provider_to_llm(self):
        """将 active_provider 同步到 self.llm（运行时快照，向后兼容字段）

        保留 llm 中与 provider 无关的字段（temperature/max_tokens/request_timeout/small_model）。
        """
        provider = self.get_active_provider()
        if not provider:
            return
        self.llm.backend = provider.backend
        self.llm.model = provider.model
        self.llm.api_key = provider.api_key
        self.llm.base_url = provider.base_url
        self.llm.available_models = provider.available_models

    def _resolve_provider_keys_from_env(self):
        """从环境变量加载所有 provider 的 API Key（运行时填充，不落盘）

        环境变量优先级总是高于配置文件中的 api_key（安全设计）：
        用户可通过环境变量覆盖配置文件的 key，无需修改配置文件。
        """
        for i, p in enumerate(self.providers):
            env_var = p.env_var_name
            val = os.environ.get(env_var, "")
            if val:
                p.api_key = val
                continue
            # 第一个 provider 兼容旧环境变量 IRON_API_KEY / OPENAI_API_KEY
            if i == 0:
                val = os.environ.get("IRON_API_KEY") or os.environ.get("OPENAI_API_KEY")
                if val:
                    p.api_key = val

    def _merge_yaml(self, path: Path):
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        if "llm" in data:
            for k, v in data["llm"].items():
                if hasattr(self.llm, k):
                    # 关键字段基本类型校验，避免错误类型覆盖默认值
                    if k == "temperature" and (isinstance(v, bool) or not isinstance(v, (int, float))):
                        continue
                    if k == "max_tokens" and (isinstance(v, bool) or not isinstance(v, int)):
                        continue
                    setattr(self.llm, k, v)
        if "project" in data:
            for k, v in data["project"].items():
                if hasattr(self.project, k):
                    setattr(self.project, k, v)
        if "max_fix_rounds" in data:
            try:
                rounds = int(data["max_fix_rounds"])
                if 0 <= rounds <= 20:
                    self.max_fix_rounds = rounds
            except (TypeError, ValueError):
                pass  # 保留默认值
        if "default_agent" in data:
            self.default_agent = data["default_agent"]
        if "max_steps" in data:
            try:
                steps = int(data["max_steps"])
                if 10 <= steps <= 5000:
                    self.max_steps = steps
                else:
                    logger.warning("max_steps=%s 超出 [10,5000]，保留默认值 50", steps)
            except (TypeError, ValueError):
                pass  # 保留默认值
        # 加载 run_command_timeout 配置
        if "run_command_timeout" in data:
            try:
                timeout = int(data["run_command_timeout"])
                if 30 <= timeout <= 3600:
                    self.run_command_timeout = timeout
                else:
                    logger.warning("run_command_timeout=%s 超出 [30,3600]，保留默认值 300", timeout)
            except (TypeError, ValueError):
                pass  # 保留默认值
        # P1-2: Stop Hooks 配置加载
        if "stop_hooks_enabled" in data:
            self.stop_hooks_enabled = bool(data["stop_hooks_enabled"])
        if "max_consecutive_failures" in data:
            try:
                v = int(data["max_consecutive_failures"])
                if 1 <= v <= 100:
                    self.max_consecutive_failures = v
            except (TypeError, ValueError):
                pass
        if "max_tool_repetition" in data:
            try:
                v = int(data["max_tool_repetition"])
                if 1 <= v <= 1000:
                    self.max_tool_repetition = v
            except (TypeError, ValueError):
                pass
        if "no_progress_steps" in data:
            try:
                v = int(data["no_progress_steps"])
                if 1 <= v <= 1000:
                    self.no_progress_steps = v
            except (TypeError, ValueError):
                pass
        # P1-3: Prompt Caching 配置加载
        if "prompt_caching_enabled" in data:
            self.prompt_caching_enabled = bool(data["prompt_caching_enabled"])
        if "prompt_cache_ttl" in data:
            try:
                v = int(data["prompt_cache_ttl"])
                if 10 <= v <= 86400:  # 10 秒 ~ 1 天
                    self.prompt_cache_ttl = v
            except (TypeError, ValueError):
                pass
        if "verbose" in data:
            self.verbose = data["verbose"]
        mcp_data = data.get("mcp")
        if isinstance(mcp_data, dict):
            for name, srv in mcp_data.items():
                if not isinstance(srv, dict):
                    continue
                self.mcp[name] = MCPConfig(**{k: v for k, v in srv.items() if k in MCPConfig.__dataclass_fields__})
        # 多厂商配置加载（providers 列表）
        providers_data = data.get("providers")
        if isinstance(providers_data, list):
            self.providers = []
            for p_data in providers_data:
                if not isinstance(p_data, dict):
                    continue
                valid_fields = {k: v for k, v in p_data.items()
                                if k in ProviderConfig.__dataclass_fields__}
                try:
                    self.providers.append(ProviderConfig(**valid_fields))
                except TypeError:
                    continue
        if "active_provider" in data:
            self.active_provider = str(data["active_provider"])
        if "save_api_key" in data:
            self.save_api_key = bool(data["save_api_key"])
        # P2-1: 规则评估引擎配置加载
        if "permission_rules_enabled" in data:
            self.permission_rules_enabled = bool(data["permission_rules_enabled"])
        if "permission_rules_file" in data:
            _rules_file = data["permission_rules_file"]
            if isinstance(_rules_file, str):
                self.permission_rules_file = _rules_file
        # P4-3: 工具输出截断阈值加载
        if "tool_output_max_chars" in data:
            try:
                v = int(data["tool_output_max_chars"])
                if 100 <= v <= 1000000:  # 合理范围：100 字符 ~ 1M 字符
                    self.tool_output_max_chars = v
            except (TypeError, ValueError):
                pass
        # P5-2: 主题配置加载（仅接受内置主题名，否则保留默认值）
        if "theme" in data:
            _theme = data["theme"]
            if isinstance(_theme, str) and _theme in ("default", "catppuccin", "dracula"):
                self.theme = _theme

    def save(self, path: Path | None = None):
        import tempfile
        target = path or (DEFAULT_CONFIG_DIR / "config.yml")
        target.parent.mkdir(parents=True, exist_ok=True)
        # 根据 save_api_key 标志决定是否真实写入 api_key
        # True：方便但有风险（文件权限 0o600 保护）
        # False：不落盘，需通过环境变量 IRON_API_KEY_<NAME> 提供
        _key_to_save = self.llm.api_key if self.save_api_key else ""
        llm_data = {
            "backend": self.llm.backend,
            "model": self.llm.model,
            "small_model": self.llm.small_model,
            "api_key": _key_to_save,
            "base_url": self.llm.base_url,
            "max_tokens": self.llm.max_tokens,
            "temperature": self.llm.temperature,
        }
        if self.llm.available_models:
            llm_data["available_models"] = self.llm.available_models
        # 多厂商配置（api_key 根据 save_api_key 标志决定是否落盘）
        providers_data = []
        for p in self.providers:
            providers_data.append({
                "name": p.name,
                "backend": p.backend,
                "base_url": p.base_url,
                "api_key": p.api_key if self.save_api_key else "",
                "available_models": p.available_models,
                "model": p.model,
            })
        # 构建环境变量反向映射，用于 save 时检测 headers/url 等字段
        # 是否等于环境变量值，若是则替换为 ${VAR_NAME} 占位符，避免明文落盘。
        # env 字段用 KEY 名直接生成占位符（env KEY 名通常就是环境变量名）；
        # headers 字段 KEY 名是 HTTP header 名，用反向映射更准确。
        env_reverse = {v: k for k, v in os.environ.items() if v and k.isupper()}
        data = {
            "llm": llm_data,
            "providers": providers_data,
            "active_provider": self.active_provider,
            "save_api_key": self.save_api_key,
            "project": {
                "name": self.project.name,
                "mcu": self.project.mcu,
                "language": self.project.language,
                "framework": self.project.framework,
                "build_system": self.project.build_system,
                "rules_enabled": self.project.rules_enabled,
                "skills_enabled": self.project.skills_enabled,
            },
            "mcp": {
                name: {
                    # headers/url 用反向映射占位符化（防止明文 token 落盘）
                    "headers": {k: _mask_env_values(v, env_reverse) for k, v in (srv.headers or {}).items()} if srv.headers else {},
                    "url": _mask_env_values(srv.url, env_reverse),
                    "command": srv.command,
                    "args": srv.args,
                    "type": srv.type,
                    "enabled": srv.enabled,
                    "timeout": srv.timeout,
                    "env": {k: "${" + k + "}" for k in (srv.env or {})} if srv.env else {},  # 占位，启动时从 os.environ 解析
                }
                for name, srv in self.mcp.items()
            } if self.mcp else {},
            "default_agent": self.default_agent,
            "max_steps": self.max_steps,
            "max_fix_rounds": self.max_fix_rounds,
            "run_command_timeout": self.run_command_timeout,
            "stop_hooks_enabled": self.stop_hooks_enabled,
            "max_consecutive_failures": self.max_consecutive_failures,
            "max_tool_repetition": self.max_tool_repetition,
            "no_progress_steps": self.no_progress_steps,
            "prompt_caching_enabled": self.prompt_caching_enabled,
            "prompt_cache_ttl": self.prompt_cache_ttl,
            "permission_rules_enabled": self.permission_rules_enabled,
            "permission_rules_file": self.permission_rules_file,
            "tool_output_max_chars": self.tool_output_max_chars,
            "theme": self.theme,
            "verbose": self.verbose,
        }
        # 原子写入：先写临时文件，再替换目标文件，避免写入中途崩溃导致配置损坏
        tmp_fd, tmp_path = tempfile.mkstemp(dir=str(Path(target).parent), suffix=".tmp")
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                yaml.dump(data, f, default_flow_style=False, allow_unicode=True)
            os.replace(tmp_path, target)
        except OSError:
            # os.fdopen 失败时 fd 仍需关闭，避免 fd 泄漏；
            # fdopen 成功后 fd 由 with 块接管并关闭，此处 os.close 会抛 OSError 被吞掉
            try:
                os.close(tmp_fd)
            except OSError:
                pass
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise
        # Unix 下设置文件权限为 0o600（仅属主可读写），保护敏感配置
        if os.name == "posix":
            try:
                os.chmod(target, 0o600)
            except OSError:
                pass

    @staticmethod
    def detect_backend(url: str) -> str:
        """根据 URL 自动推断后端类型"""
        url_lower = url.lower()
        if "anthropic" in url_lower:
            return "anthropic"
        elif "deepseek" in url_lower:
            return "openai"
        elif "minimax" in url_lower:
            return "openai"
        elif "xiaomimimo.com" in url_lower:
            return "openai"
        elif "ollama" in url_lower or "localhost" in url_lower or "127.0.0.1" in url_lower:
            return "ollama"
        elif "volcengine" in url_lower or "ark.cn" in url_lower:
            return "openai"
        elif "openai" in url_lower:
            return "openai"
        else:
            return "openai"

    @staticmethod
    def fetch_available_models(base_url: str, api_key: str = "", backend: str = "openai") -> list[str]:
        """从 API 拉取可用模型列表

        支持 OpenAI 兼容接口 (GET /v1/models) 和 Ollama (GET /api/tags)
        """
        import httpx
        models = []

        try:
            if backend == "ollama":
                # Ollama: GET /api/tags
                resp = httpx.get(f"{base_url}/api/tags", timeout=10.0)
                if resp.status_code == 200:
                    data = resp.json()
                    for m in data.get("models", []):
                        models.append(m.get("name", ""))
            elif backend == "anthropic":
                # Anthropic 没有 models 列表接口，返回常用模型
                models = [
                    "claude-sonnet-4-20250514",
                    "claude-opus-4-20250514",
                    "claude-3.5-sonnet-20241022",
                    "claude-3.5-haiku-20241022",
                ]
            else:
                # OpenAI 兼容: GET /v1/models
                url = base_url.rstrip("/")
                if not url.endswith("/models"):
                    if not url.endswith("/v1"):
                        url = f"{url}/v1"
                    url = f"{url}/models"
                headers = {}
                if api_key:
                    headers["Authorization"] = f"Bearer {api_key}"
                resp = httpx.get(url, headers=headers, timeout=10.0)
                if resp.status_code == 200:
                    data = resp.json()
                    for m in data.get("data", []):
                        mid = m.get("id", "")
                        if mid:
                            models.append(mid)
        except (httpx.HTTPError, ValueError) as e:
            import logging
            logging.warning(f"获取模型列表失败: {e}")

        return sorted(models)

    @staticmethod
    def _infer_provider_name(url: str) -> str:
        """根据 URL 推断厂商友好名"""
        url_lower = url.lower()
        if "anthropic" in url_lower:
            return "Anthropic"
        elif "deepseek" in url_lower:
            return "DeepSeek"
        elif "minimax" in url_lower:
            return "MiniMax"
        elif "xiaomimimo" in url_lower or "mimo" in url_lower:
            return "MiMo"
        elif "volcengine" in url_lower or "ark.cn" in url_lower:
            return "VolcEngine"
        elif "ollama" in url_lower or "localhost" in url_lower or "127.0.0.1" in url_lower:
            return "Ollama"
        elif "openai" in url_lower:
            return "OpenAI"
        else:
            # 从 URL 提取域名作为名称
            from urllib.parse import urlparse
            try:
                host = urlparse(url).hostname or url
                return host.split(".")[0].title() or "Custom"
            except (ValueError, TypeError):
                return "Custom"

    @staticmethod
    def setup_interactive() -> "IronConfig":
        """交互式配置向导 — 多厂商管理（上下键可视化选择）

        全部菜单用 select_with_arrows 上下键选择，回车确认。
        支持添加/编辑/删除/设默认/重新扫描/设置 API Key 保存策略。
        """
        from rich.console import Console
        from rich.panel import Panel
        from prompt_toolkit import prompt as pt_prompt
        from iron.cli.ui import select_with_arrows

        console = Console()
        # 加载已有配置（保留已配置的 providers 和 save_api_key 标志）
        config = IronConfig.load()

        # 旧配置迁移：如果 providers 为空但 llm 有内容，构造一个默认 provider
        if not config.providers and config.llm.base_url:
            migrated = ProviderConfig(
                name=IronConfig._infer_provider_name(config.llm.base_url),
                backend=config.llm.backend,
                base_url=config.llm.base_url,
                api_key=config.llm.api_key,
                available_models=config.llm.available_models,
                model=config.llm.model,
            )
            config.providers.append(migrated)
            config.active_provider = migrated.name

        console.print(Panel(
            "[bold cyan]Iron 多厂商模型配置[/bold cyan]\n\n"
            "上下键选择，回车确认，Esc 取消。\n"
            "只需提供 API URL 和 API Key 即可添加厂商，后端类型自动推断。",
            border_style="cyan",
            padding=(0, 1),
        ))

        while True:
            # 显示当前已配置厂商
            IronConfig._render_providers_list(config, console)

            # 主菜单（上下键选择）
            menu_options = [
                ("add", "➕ 添加新厂商"),
                ("edit", "✏️  编辑厂商"),
                ("default", "⭐ 设为默认厂商"),
                ("remove", "🗑️  删除厂商"),
                ("rescan", "🔄 重新扫描模型"),
                ("key_policy", f"🔑 API Key 保存策略: "
                               + ("落盘到配置文件（当前）" if config.save_api_key
                                  else "不落盘，用环境变量（当前）")),
                ("quit", "✓ 完成并保存"),
            ]

            action = select_with_arrows(menu_options, title="选择操作", console=console)
            if action is None or action == "quit":
                break
            elif action == "add":
                IronConfig._add_provider_interactive(config, console, pt_prompt, select_with_arrows)
            elif action == "edit":
                IronConfig._edit_provider_interactive(config, console, pt_prompt, select_with_arrows)
            elif action == "default":
                IronConfig._set_default_provider_interactive(config, console, select_with_arrows)
            elif action == "remove":
                IronConfig._remove_provider_interactive(config, console, select_with_arrows)
            elif action == "rescan":
                IronConfig._rescan_provider_interactive(config, console, pt_prompt, select_with_arrows)
            elif action == "key_policy":
                IronConfig._toggle_save_api_key_interactive(config, console, select_with_arrows)

        # 保存前检查：如果用户本次输入了 API Key 但 save_api_key=False，
        # 主动询问是否切换到落盘模式，避免输入的 key 丢失
        if not config.save_api_key:
            _has_input_keys = any(p.api_key for p in config.providers)
            _has_env_keys = any(
                os.environ.get(p.env_var_name)
                for p in config.providers
            )
            if _has_input_keys and not _has_env_keys:
                console.print(f"\n  [yellow]⚠ 你输入了 API Key，但当前策略是「不落盘」[/yellow]")
                console.print(f"  [dim]下次启动时这些 key 会丢失（除非设置环境变量）。[/dim]")
                from iron.cli.ui import select_with_arrows as _swa
                _choice = _swa(
                    [
                        (True,  "📄 切换为落盘模式，保存 key 到配置文件（推荐）"),
                        (False, "🔒 保持不落盘，本次输入的 key 仅本次会话有效"),
                    ],
                    title="是否切换为落盘模式？",
                    default_idx=0,
                    console=console,
                )
                if _choice:
                    config.save_api_key = True
                    console.print(f"\n  [green]✓ 已切换为落盘模式[/green]\n")

        # 保存
        config._apply_active_provider_to_llm()
        config.save()
        console.print(f"\n  [green]✓ 配置已保存到 ~/.iron/config.yml[/green]")

        # 显示 API Key 状态总结
        # 关键：判断逻辑必须区分两种模式
        # - 落盘模式：检查 provider.api_key（内存值=落盘值）
        # - 不落盘模式：检查环境变量 os.environ.get(p.env_var_name)
        if config.save_api_key:
            missing_keys = [p for p in config.providers if not p.api_key]
        else:
            missing_keys = [p for p in config.providers
                            if not os.environ.get(p.env_var_name)]
        if missing_keys:
            console.print(f"\n  [yellow]⚠ 以下厂商的 API Key 未设置：[/yellow]")
            for p in missing_keys:
                console.print(f"    [bold]{p.name}[/bold]")
            if config.save_api_key:
                console.print(f"\n  [dim]当前策略：落盘到配置文件，但以上厂商未输入 key。[/dim]")
                console.print(f"  [dim]用「编辑厂商」补充 API Key 即可保存。[/dim]\n")
            else:
                console.print(f"\n  [dim]当前策略：不落盘。需设置环境变量：[/dim]")
                for p in missing_keys:
                    console.print(f"    [cyan]{p.env_var_name}[/cyan]")
                console.print(f"\n  [dim]或用「API Key 保存策略」切换为落盘模式，然后用「编辑厂商」输入 key。[/dim]\n")
        elif config.providers:
            if config.save_api_key:
                console.print(f"  [green]✓ 所有厂商 API Key 已落盘到配置文件[/green]\n")
            else:
                console.print(f"  [green]✓ 所有厂商 API Key 已通过环境变量加载[/green]\n")

        return config

    @staticmethod
    def _render_providers_list(config: "IronConfig", console):
        """渲染当前已配置厂商列表

        key 状态图标区分三种情况：
        - ✓ (绿) = 环境变量已加载（持久，推荐）
        - 📝 (黄) = 本次输入未保存（保存时会丢失，需切换为落盘模式）
        - ⚠ (黄) = 未设置 key
        """
        if not config.providers:
            console.print(f"\n  [dim]尚未配置任何厂商[/dim]\n")
            return

        console.print(f"\n  [bold]已配置厂商 ({len(config.providers)} 个):[/bold]")
        for i, p in enumerate(config.providers, 1):
            is_active = (p.name == config.active_provider)
            marker = " ◄ 当前" if is_active else ""

            # 区分 key 来源：环境变量 / 本次输入未保存 / 未设置
            _env_key = os.environ.get(p.env_var_name) if not config.save_api_key else None
            if p.api_key and config.save_api_key:
                key_icon = "✓"  # 落盘模式：内存值即落盘值
                key_note = ""
            elif p.api_key and _env_key:
                key_icon = "✓"  # 不落盘模式：key 来自环境变量
                key_note = ""
            elif p.api_key and not _env_key:
                key_icon = "📝"  # 不落盘模式：本次输入但未保存
                key_note = " [dim yellow](未保存，切换落盘模式以持久化)[/dim yellow]"
            else:
                key_icon = "⚠"
                key_note = ""

            console.print(f"    {i}. [{key_icon}] [bold]{p.name}[/bold] "
                          f"({p.backend}) — {p.model}{marker}{key_note}")
            console.print(f"       URL: {p.base_url}", style="dim")
        console.print()

    @staticmethod
    def _add_provider_interactive(config: "IronConfig", console, pt_prompt, select_with_arrows):
        """添加新厂商（上下键选择模型）"""
        url = pt_prompt("  API URL: ").strip()
        if not url:
            console.print(f"  [yellow]URL 不能为空[/yellow]\n")
            return

        backend = IronConfig.detect_backend(url)

        # 厂商名称（可选，自动推断）
        default_name = IronConfig._infer_provider_name(url)
        name = pt_prompt(f"  厂商名称 [{default_name}]: ", default=default_name).strip()
        if not name:
            name = default_name

        # 检查重名
        existing_names = [p.name for p in config.providers]
        if name in existing_names:
            console.print(f"  [yellow]厂商 '{name}' 已存在，请用其他名称[/yellow]\n")
            return

        # API Key（Ollama 不需要）
        api_key = ""
        if backend != "ollama":
            api_key = pt_prompt("  API Key (留空稍后用编辑功能补充): ", is_password=True).strip()

        # 创建 provider
        provider = ProviderConfig(
            name=name, backend=backend, base_url=url, api_key=api_key, model=""
        )

        # 扫描可用模型
        console.print(f"\n  [dim]正在扫描可用模型...[/dim]", end="")
        available = IronConfig.fetch_available_models(url, api_key, backend)

        if available:
            provider.available_models = available
            console.print(f"\r  找到 [bold]{len(available)}[/bold] 个可用模型")
            # 用上下键选择模型
            model_options = [(m, m) for m in available]
            selected = select_with_arrows(
                model_options, title=f"选择 {name} 默认模型", console=console
            )
            provider.model = selected if selected else available[0]
        else:
            console.print(f"\r  [dim]未获取到模型列表，请手动输入模型名称[/dim]")
            default_model = "gpt-4o"
            model = pt_prompt(f"  模型名称 [{default_model}]: ", default=default_model).strip()
            provider.model = model or default_model

        # 加入配置
        config.providers.append(provider)

        # 第一个厂商自动设为 active
        if not config.active_provider or not any(p.name == config.active_provider for p in config.providers):
            config.active_provider = provider.name

        console.print(f"  [green]✓ 已添加厂商 [bold]{name}[/bold] (模型: {provider.model})[/green]\n")

    @staticmethod
    def _edit_provider_interactive(config: "IronConfig", console, pt_prompt, select_with_arrows):
        """编辑已存在厂商（name/url/api_key/model/重新扫描模型）"""
        if not config.providers:
            console.print(f"  [yellow]尚未配置任何厂商[/yellow]\n")
            return

        # 上下键选择要编辑的厂商
        provider_options = [(p, f"{p.name} ({p.backend}) — {p.model}") for p in config.providers]
        provider = select_with_arrows(provider_options, title="选择要编辑的厂商", console=console)
        if provider is None:
            return

        console.print(f"\n  [bold]编辑 [cyan]{provider.name}[/cyan]（回车保持原值）：[/bold]")

        # 编辑 name
        new_name = pt_prompt(f"  厂商名称 [{provider.name}]: ", default=provider.name).strip()
        if new_name and new_name != provider.name:
            # 检查重名
            if any(p.name == new_name for p in config.providers if p is not provider):
                console.print(f"  [yellow]名称 '{new_name}' 已被其他厂商占用，保持原名[/yellow]")
            else:
                old_name = provider.name
                provider.name = new_name
                if config.active_provider == old_name:
                    config.active_provider = new_name

        # 编辑 URL
        new_url = pt_prompt(f"  API URL [{provider.base_url}]: ", default=provider.base_url).strip()
        if new_url and new_url != provider.base_url:
            provider.base_url = new_url
            provider.backend = IronConfig.detect_backend(new_url)

        # 编辑 API Key（仅在非 ollama 时询问）
        if provider.backend != "ollama":
            _cur = provider.api_key
            _hint = f"({_cur[:4]}...{_cur[-4:]})" if _cur and len(_cur) > 12 else ("(已设置)" if _cur else "(未设置)")
            new_key = pt_prompt(f"  API Key {_hint} [留空保持原值]: ", is_password=True).strip()
            if new_key:
                provider.api_key = new_key
                console.print(f"  [green]✓ API Key 已更新[/green]")

        # 编辑模型：可以选择重新扫描或保持原值
        console.print()
        model_action = select_with_arrows(
            [
                ("keep", f"保持当前模型: {provider.model}"),
                ("rescan", "🔄 重新扫描可用模型"),
                ("manual", "✏️  手动输入模型名"),
            ],
            title="模型操作",
            default_idx=0,
            console=console,
        )

        if model_action == "rescan":
            console.print(f"\n  [dim]正在扫描 {provider.name} 可用模型...[/dim]", end="")
            available = IronConfig.fetch_available_models(
                provider.base_url, provider.api_key, provider.backend
            )
            if not available:
                console.print(f"\r  [yellow]未获取到模型列表（可能 API Key 未设置）[/yellow]\n")
                return
            provider.available_models = available
            console.print(f"\r  找到 [bold]{len(available)}[/bold] 个可用模型")
            # 用上下键选择模型
            model_options = [(m, m) for m in available]
            default_idx = 0
            if provider.model in available:
                default_idx = available.index(provider.model)
            selected = select_with_arrows(
                model_options, title=f"选择 {provider.name} 默认模型",
                default_idx=default_idx, console=console
            )
            if selected:
                provider.model = selected
        elif model_action == "manual":
            new_model = pt_prompt(f"  模型名称 [{provider.model}]: ", default=provider.model).strip()
            if new_model:
                provider.model = new_model

        # 如果编辑的是 active provider，同步到 llm
        if provider.name == config.active_provider:
            config._apply_active_provider_to_llm()
        console.print(f"\n  [green]✓ {provider.name} 已更新[/green]\n")

    @staticmethod
    def _set_default_provider_interactive(config: "IronConfig", console, select_with_arrows):
        """设为默认厂商（上下键选择）"""
        if not config.providers:
            console.print(f"  [yellow]尚未配置任何厂商[/yellow]\n")
            return

        provider_options = []
        default_idx = 0
        for i, p in enumerate(config.providers):
            desc = f"{p.name} ({p.backend})"
            if p.name == config.active_provider:
                desc += "  ◄ 当前"
                default_idx = i
            provider_options.append((p, desc))

        provider = select_with_arrows(
            provider_options, title="选择默认厂商",
            default_idx=default_idx, console=console
        )
        if provider is None:
            return
        config.active_provider = provider.name
        config._apply_active_provider_to_llm()
        console.print(f"\n  [green]✓ 已设为默认: [bold]{provider.name}[/bold][/green]\n")

    @staticmethod
    def _remove_provider_interactive(config: "IronConfig", console, select_with_arrows):
        """删除厂商（上下键选择）"""
        if not config.providers:
            console.print(f"  [yellow]尚未配置任何厂商[/yellow]\n")
            return
        if len(config.providers) == 1:
            console.print(f"  [yellow]至少保留一个厂商，不能删除[/yellow]\n")
            return

        provider_options = [(p, f"{p.name} ({p.backend}) — {p.model}") for p in config.providers]
        removed = select_with_arrows(provider_options, title="选择要删除的厂商", console=console)
        if removed is None:
            return

        config.providers.remove(removed)
        if config.active_provider == removed.name:
            config.active_provider = config.providers[0].name
            config._apply_active_provider_to_llm()
        console.print(f"\n  [green]✓ 已删除: [bold]{removed.name}[/bold][/green]\n")

    @staticmethod
    def _rescan_provider_interactive(config: "IronConfig", console, pt_prompt, select_with_arrows):
        """重新扫描某厂商的模型列表（上下键选择）"""
        if not config.providers:
            console.print(f"  [yellow]尚未配置任何厂商[/yellow]\n")
            return

        provider_options = [(p, f"{p.name} ({p.backend})") for p in config.providers]
        provider = select_with_arrows(provider_options, title="选择要重新扫描的厂商", console=console)
        if provider is None:
            return

        console.print(f"\n  [dim]正在扫描 {provider.name} 可用模型...[/dim]", end="")
        available = IronConfig.fetch_available_models(
            provider.base_url, provider.api_key, provider.backend
        )
        if not available:
            console.print(f"\r  [yellow]未获取到模型列表（可能 API Key 未设置）[/yellow]\n")
            return

        provider.available_models = available
        console.print(f"\r  找到 [bold]{len(available)}[/bold] 个可用模型")

        # 用上下键选择模型
        model_options = [(m, m) for m in available]
        default_idx = 0
        if provider.model in available:
            default_idx = available.index(provider.model)
        selected = select_with_arrows(
            model_options, title=f"选择 {provider.name} 默认模型",
            default_idx=default_idx, console=console
        )
        if selected:
            provider.model = selected

        if provider.name == config.active_provider:
            config._apply_active_provider_to_llm()
        console.print(f"\n  [green]✓ {provider.name} 已更新[/green]\n")

    @staticmethod
    def _toggle_save_api_key_interactive(config: "IronConfig", console, select_with_arrows):
        """切换 API Key 保存策略"""
        current_desc = "落盘到配置文件（方便但有风险）" if config.save_api_key else "不落盘，用环境变量（安全）"
        options = [
            (False, "🔒 不落盘，用环境变量（推荐，安全）"),
            (True,  "📄 落盘到配置文件（方便，文件权限 0o600）"),
        ]
        default_idx = 1 if config.save_api_key else 0
        selected = select_with_arrows(
            options, title=f"API Key 保存策略（当前: {current_desc})",
            default_idx=default_idx, console=console
        )
        if selected is None:
            return
        config.save_api_key = selected
        if selected:
            console.print(f"\n  [green]✓ 已切换为落盘模式[/green]")
            console.print(f"  [dim]下次保存时，所有厂商的 API Key 会写入 ~/.iron/config.yml[/dim]")
            console.print(f"  [dim]用「编辑厂商」可以为未设置 key 的厂商补充 API Key[/dim]\n")
        else:
            console.print(f"\n  [green]✓ 已切换为不落盘模式[/green]")
            console.print(f"  [dim]需为每个厂商设置环境变量 IRON_API_KEY_<NAME>[/dim]")
            console.print(f"  [dim]第一个厂商也兼容 IRON_API_KEY / OPENAI_API_KEY[/dim]\n")
