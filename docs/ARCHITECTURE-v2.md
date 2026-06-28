# Iron Agent 架构文档 v2

**版本：** v2.5.0+（含 19 个 P 任务实现）
**更新日期：** 2026-06-27
**基线测试：** 738 passed, 1 skipped
**架构原则：** 取 Claude Code 之深度 + 取 OpenCode 之简洁 + 保留 Iron 嵌入式特色

> 本文档描述 Iron Agent **当前实现状态**的六层架构。如需了解开发路线图与历史决策，参见 [architecture-framework.md](file:///d:/嵌入式-Agent/docs/architecture-framework.md)。

---

## 一、架构总览

Iron Agent 采用**六层架构**，每层职责单一、向下依赖、向上提供服务：

```
┌─────────────────────────────────────────────────────────────────┐
│ L6 · 终端 UI 层                                                  │
│   • MarkdownStreamRenderer（流式 Markdown + 语法高亮）          │
│   • 主题系统（default / catppuccin / dracula，_ColorsProxy）    │
│   • 斜杠命令分组（file / build / session / system）             │
│   • WordCompleter + 上下键历史 + 6 常用命令                     │
├─────────────────────────────────────────────────────────────────┤
│ L5 · 服务基础设施层                                              │
│   • PubSub 事件总线（泛型 EventBus，async/sync 兼容）           │
│   • SQLite 持久化（WAL + 三表 + 迁移）                          │
│   • 4 层记忆（压缩 → checkpoint → project → dream/distill）    │
│   • Skills 系统（PromptSkill 数据驱动，8 个内置）               │
│   • MCP 客户端（stdio / SSE / HTTP + SSRF 防护）                │
│   • LSP 客户端（clangd / ccls + compile_commands.json）         │
│   • EmbedForge / EmbedGuard 桥接（嵌入式特色）                  │
├─────────────────────────────────────────────────────────────────┤
│ L4 · 权限与安全层                                                │
│   • 规则评估引擎（DSL：deny > ask > allow + 4 默认规则）       │
│   • PreToolUse / PostToolUse Hooks（用户脚本 + 内置）           │
│   • 三级审批持久化（once / session / never）                    │
│   • Path Guard（穿越 / 保留名 / symlink）                       │
│   • 命令注入防御（元字符 / 子shell / NULL）                     │
│   • SSRF 防护（私有 IP / 环回 / 十六进制）                      │
├─────────────────────────────────────────────────────────────────┤
│ L3 · 工具执行层                                                  │
│   • 18 内置工具（文件/搜索/执行/嵌入式/LSP/patch）              │
│   • ToolSearchTool 动态发现（按需暴露工具 schema）              │
│   • patch 工具（unified diff，多 hunk，模糊匹配）               │
│   • 5 个 LSP 工具（diagnostics/def/hover/refs/completion）       │
│   • safe_execute 输出截断保护（默认 10000 字符）                │
├─────────────────────────────────────────────────────────────────┤
│ L2 · Agent 循环层（内核）                                         │
│   • ReAct async generator（工具调用主循环）                      │
│   • 5 层压缩管道（micro → compact_if_needed → collapse → auto → budget） │
│   • 4 个 Stop Hooks（Failures / DoomLoop / Repetition / NoProgress） │
│   • Prompt Caching（系统提示分块 + cache_control）              │
│   • 双 Agent 类型（CoderAgent 全工具 + TaskAgent 只读）         │
│   • 专门化子代理（build / embed / plan / verify / explore）    │
│   • doom_loop + MAX_STEPS 兜底                                  │
├─────────────────────────────────────────────────────────────────┤
│ L1 · 入口与引导层                                                │
│   • 3 阶段启动管道（配置 → 信任 → 运行，bootstrap.py）          │
│   • 20 个特性门控（features.yml + is_feature_enabled）          │
│   • 多厂商配置（ProviderConfig + 上下键可视化选择）             │
│   • 配置级联（全局 ~/.iron → 项目 .iron-agent → 环境变量）      │
│   • SIGTERM / SIGINT 信号处理                                   │
└─────────────────────────────────────────────────────────────────┘
```

**目录结构：**
```
iron/
├── cli/                  # L1 入口 + L6 UI
│   ├── bootstrap.py      # 3 阶段启动管道
│   ├── main.py           # CLI 主入口 + 命令分发
│   ├── ui.py             # MarkdownStreamRenderer + 补全 + 主题
│   ├── theme.py          # _ColorsProxy 动态代理
│   ├── themes/           # default / catppuccin / dracula
│   └── commands/         # file_cmds / build_cmds / session_cmds / system_cmds
├── agent/                # L2 内核
│   ├── engine.py         # BaseAgentEngine(ABC) + Coder + Task + Verify + Explore
│   ├── context_compactor.py  # 5 层压缩管道
│   ├── stop_hooks.py     # 4 个收敛检测器 + StopHookManager
│   ├── hooks.py          # PreToolUse / PostToolUse Hook 系统
│   ├── permission.py     # 三级审批持久化
│   ├── memory.py         # 4 层记忆 + Dream/Distill
│   ├── prompt_builder.py # 系统提示构建
│   ├── risk_evaluator.py # 命令风险评估
│   ├── agent_manager.py  # 专门化子代理管理
│   ├── engine_events.py  # AgentEvent 数据类
│   ├── engine_builtins.py # BUILTIN_SCHEMAS
│   └── agents/           # build.md / embed.md / plan.md / verify.md / explore.md
├── llm/                  # L2 后端
│   ├── backend.py        # LLMBackend(ABC) + 4 后端 + Circuit Breaker
│   └── prompt_cache.py   # PromptCache 系统提示分块缓存
├── tools/                # L3 工具层
│   ├── base.py           # BaseTool + safe_execute + _truncate_result
│   ├── registry.py       # ToolRegistry
│   ├── tool_search.py    # ToolSearchTool 动态发现
│   ├── patch_tool.py     # unified diff 补丁
│   ├── lsp_tools.py      # 5 个 LSP 工具
│   ├── path_guard.py     # 路径越界防护
│   └── ... (14 其他工具)
├── rules/                # L4 权限
│   ├── permission_rules.py # DSL 规则引擎 + 4 默认规则
│   ├── iron_rules.py     # 11 条嵌入式铁律
│   ├── ai_antipatterns.py # 7 反模式
│   └── project_rules.py  # 项目规则
├── core/                 # L5 服务
│   ├── pubsub.py         # EventBus 泛型事件总线
│   ├── db.py             # SQLite WAL 持久化
│   └── migrations/       # SQL 迁移（001_initial.sql）
├── integrations/         # L5 集成
│   ├── lsp_client.py     # LSP 客户端（clangd / ccls）
│   ├── embedforge_bridge.py  # EmbedForge 桥接
│   └── embedguard_bridge.py  # EmbedGuard 桥接
├── mcp/                  # L5 MCP
│   └── client.py         # MCP 客户端（stdio / SSE / HTTP）
├── skills/               # L5 Skills
│   ├── base.py           # PromptSkill 数据类
│   └── registry.py       # SkillRegistry
├── config/               # L1 配置
│   ├── settings.py       # ProviderConfig + 多厂商配置
│   └── features.py       # 20 个特性开关
└── constants.py          # 常量
```

---

## 二、L1 · 入口与引导层

### 2.1 3 阶段启动管道（[bootstrap.py](file:///d:/嵌入式-Agent/iron/cli/bootstrap.py)）

启动管道分为 3 个阶段，每阶段失败不进入下一阶段：

```python
# 阶段 1: 配置阶段（并行加载）
- 加载全局配置 ~/.iron/config.yml
- 加载项目配置 .iron-agent/instructions.md
- 加载环境变量覆盖

# 阶段 2: 信任阶段（验证）
- 验证 API Key（显示前 4 后 4 字符）
- 加载扩展签名（hooks / skills / mcp）
- 验证特性门控配置

# 阶段 3: 运行阶段（初始化）
- 初始化 AgentEngine
- 连接 MCP 服务器
- 启动 LSP 客户端（如启用）
- 注入事件总线订阅者
```

### 2.2 特性门控（[features.py](file:///d:/嵌入式-Agent/iron/config/features.py)）

20 个特性开关，用户可通过 `~/.iron/features.yml` 覆盖默认值：

| 特性 | 默认 | 说明 |
|------|------|------|
| `stop_hooks` | true | Stop Hooks 收敛检测 |
| `prompt_caching` | true | 系统提示分块缓存 |
| `lsp_enabled` | true | LSP 客户端 |
| `pubsub_enabled` | true | PubSub 事件总线 |
| `tool_search` | true | ToolSearchTool 动态发现 |
| `patch_tool` | true | unified diff 补丁 |
| `markdown_render` | true | 流式 Markdown 渲染 |
| `themes` | true | 主题系统 |
| `permission_rules` | true | DSL 规则引擎 |
| `tool_hooks` | true | PreToolUse/PostToolUse |
| `permission_persist` | true | 三级审批持久化 |
| `sqlite_persist` | true | SQLite 持久化 |
| `vim_mode` | false | Vim 模式（未实现） |
| ... | ... | 共 20 个 |

读取接口：
```python
from iron.config.features import is_feature_enabled
if is_feature_enabled("lsp_enabled"):
    # 启用 LSP 客户端
```

### 2.3 多厂商配置（[settings.py](file:///d:/嵌入式-Agent/iron/config/settings.py)）

```python
@dataclass
class ProviderConfig:
    name: str               # 厂商名（mimo / openai / anthropic）
    backend_type: str       # openai / anthropic / ollama / echo
    base_url: str
    api_key: str            # 占位符（不落盘时）
    models: list[str]       # 可用模型列表
    save_api_key: str       # "env" / "disk" / "none"

@dataclass
class IronConfig:
    providers: dict[str, ProviderConfig]
    default_provider: str
    # ... 其他配置项
```

- **配置级联**：全局 `~/.iron/config.yml` → 项目 `.iron-agent/` → 环境变量
- **API Key 多策略**：环境变量 / 落盘 / 不落盘三选一
- **可视化选择**：上下键选择厂商和模型（[ui.py](file:///d:/嵌入式-Agent/iron/cli/ui.py) `_arrow_select`）

### 2.4 信号处理

- **SIGTERM / SIGINT**：收到信号后保存 session 再退出
- **双击 Ctrl+C**：强制退出并保存 session
- **KeyboardInterrupt**：流式缓冲区 flush 已缓存的 chunk

---

## 三、L2 · Agent 循环层（内核）

### 3.1 双 Agent 类型（[engine.py](file:///d:/嵌入式-Agent/iron/agent/engine.py)）

```python
class BaseAgentEngine(ABC):
    """抽象基类，强制子类实现：
    - _get_allowed_tools() → set[str] | None  (None=全部允许)
    - _get_system_prompt_prefix() → str       (角色前缀)
    """
    # 共享逻辑：压缩管道 / StopHooks / PromptCache / PubSub / 规则评估
    # 共享逻辑：PrePostHooks / 权限管理 / safe_execute / 特性门控

class CoderAgentEngine(BaseAgentEngine):
    """编码 Agent — 完整工具集（默认）"""
    def _get_allowed_tools(self): return None  # 全部允许
    def _get_system_prompt_prefix(self): return ""

class TaskAgentEngine(BaseAgentEngine):
    """只读 Agent — 探索/规划/审查（参考 OpenCode Task Agent）"""
    def _get_allowed_tools(self):
        return {"read_file", "search_code", "find_files", "web_search", "ask_user"}
    def _get_system_prompt_prefix(self):
        return "你是只读探索 Agent，只能查看代码不能修改..."

# 向后兼容别名
AgentEngine = CoderAgentEngine
CoderAgent = CoderAgentEngine
TaskAgent = TaskAgentEngine
```

**专门化子代理**（通过 AgentManager 加载 markdown 配置）：
- `build` — 嵌入式构建专家
- `embed` — 嵌入式优化专家
- `plan` — 任务规划专家
- `verify` — 自动跑测试 + 静态分析
- `explore` — 只读代码探索

### 3.2 5 层压缩管道（[context_compactor.py](file:///d:/嵌入式-Agent/iron/agent/context_compactor.py)）

```python
class ContextCompactor:
    """5 层压缩管道（对标 Claude Code）

    Level 1 microcompact（轻量，不调 LLM）：
        - 截断早期 tool 输出到 500 字符
        - 保留最近 10 条 tool 结果不截断
        - 合并连续 thinking 消息

    Level 2 compact_if_needed（中量，按阈值触发）：
        - 动态阈值：context_window × 0.85，fallback 30K
        - 超阈值时调用 LLM 摘要旧消息
        - 保留最近 6 条消息不压缩

    Level 3 context_collapse（中量，合并工具结果）：
        - 合并连续同类型工具结果
        - 提取关键信息（路径、错误、stdout）

    Level 4 auto_compact（重量，独立模型摘要）：
        - 用独立 LLM 调用生成结构化摘要
        - 摘要模板：目标/进度/阻塞/决策/下一步/上下文/文件

    Level 5 budget_reduce（重量，按 token 预算裁剪）：
        - 按 token 预算从最旧开始裁剪
        - 保留 system + 最近 N 条 + 摘要
    """
```

**触发顺序**：每次 ReAct 循环开始时按 Level 1 → 2 → 3 → 4 → 5 顺序检查，越往后成本越高。

### 3.3 4 个 Stop Hooks（[stop_hooks.py](file:///d:/嵌入式-Agent/iron/agent/stop_hooks.py)）

```python
class StopHookManager:
    """管理多个收敛检测器，任一触发即停止 Agent 循环"""

# 4 个内置检测器：
class MaxConsecutiveFailures:    # 连续失败超阈值（默认 5）
class DoomLoopDetector:          # 循环模式检测（长度 2/3/4）
class MaxToolRepetition:         # 工具重复调用上限（默认 10）
class NoProgressDetector:        # 无进展检测（默认 8 步）
```

**注册接口**：
```python
engine._stop_hooks.register(MyCustomHook())
```

### 3.4 Prompt Caching（[prompt_cache.py](file:///d:/嵌入式-Agent/iron/llm/prompt_cache.py)）

```python
class PromptCache:
    """系统提示分块缓存（参考 Claude Code 两块策略）

    Block 1: 核心指令（不变）→ 缓存命中
    Block 2: 项目配置（少变）→ 缓存命中
    Block 3: 用户消息（每轮变）→ 不缓存

    Anthropic 原生支持 cache_control 标记
    OpenAI 兼容后端用 hash 遥测（无原生支持）
    """
    ttl_seconds: int = 300  # 默认 5 分钟
```

**预期收益**：降低 ~85% 重复计算成本（系统提示部分）。

### 3.5 ReAct 主循环

```python
async def process(self, user_input: str) -> AsyncGenerator[AgentEvent, None]:
    # 1. 构建 system prompt（注入铁律 + 项目规则 + 记忆 + Skills）
    # 2. 压缩管道检查（5 层）
    # 3. 调用 LLM（流式 + Prompt Caching）
    # 4. 解析响应（text / tool_calls）
    # 5. 工具过滤（_get_allowed_tools）+ 规则评估 + Hooks + 审批
    # 6. 并行执行只读工具
    # 7. safe_execute 截断
    # 8. doom_loop 检测 + Stop Hooks 检查
    # 9. yield AgentEvent → PubSub publish
    # 10. 循环直到 LLM 无 tool_calls 或 Stop Hook 触发
```

---

## 四、L3 · 工具执行层

### 4.1 工具系统（[base.py](file:///d:/嵌入式-Agent/iron/tools/base.py)）

```python
class BaseTool(ABC):
    @abstractmethod
    def schema(self) -> dict: ...
    @abstractmethod
    async def execute(self, **kwargs) -> dict: ...

    # safe_execute 包装：自动捕获异常 + 截断输出
    async def safe_execute(self, **kwargs) -> dict:
        try:
            result = await self.execute(**kwargs)
            return _truncate_result(result, self._max_output_chars)
        except (RuntimeError, OSError, ValueError, TypeError) as e:
            return {"success": False, "error": str(e)}

def _truncate_result(result: dict, max_chars: int = 10000) -> dict:
    """超阈值自动截断 stdout/content，告知模型已截断"""
```

### 4.2 18 个内置工具

| 类别 | 工具 | 说明 |
|------|------|------|
| 文件 | write_file / edit_file / read_file | 基础文件操作 |
| 搜索 | search_code / find_files | 代码探索 |
| 执行 | run_command | 命令执行（含注入防御） |
| 嵌入式 | embed_build / embed_flash / embed_lint | 嵌入式专用 |
| LSP | lsp_diagnostics / lsp_definition / lsp_hover / lsp_references / lsp_completion | 5 个 LSP 工具 |
| 补丁 | patch | unified diff 应用 |
| 辅助 | task_track / ask_user / remember / web_search | 辅助工具 |
| 扩展 | skill_create / mcp_config | 扩展管理 |

### 4.3 ToolSearchTool 动态发现（[tool_search.py](file:///d:/嵌入式-Agent/iron/tools/tool_search.py)）

```python
class ToolSearchTool(BaseTool):
    """当工具 schema 超过阈值时，按需动态暴露相关工具

    匹配策略：
    1. 关键词匹配（工具名包含查询）
    2. 描述匹配（工具描述包含查询）
    3. 超阈值切换搜索模式（只暴露 ToolSearchTool + 匹配的工具）
    """
```

### 4.4 patch 工具（[patch_tool.py](file:///d:/嵌入式-Agent/iron/tools/patch_tool.py)）

```python
class PatchTool(BaseTool):
    """unified diff 补丁应用

    特性：
    - 多 hunk 应用
    - 模糊匹配（容忍空白差异）
    - splitlines() 解析（修复 v2.4.0 的 \n 结尾 bug）
    - 失败时返回详细错误信息
    """
```

---

## 五、L4 · 权限与安全层

### 5.1 规则评估引擎（[permission_rules.py](file:///d:/嵌入式-Agent/iron/rules/permission_rules.py)）

```yaml
# ~/.iron/rules.yml 示例
rules:
  - pattern: "*.ld"
    action: deny
    reason: "链接脚本禁止写"

  - pattern: "startup_*.s"
    action: ask
    reason: "启动文件修改需确认"

  - tool: embed_flash
    action: ask
    reason: "烧录操作需确认"

  - function: "SystemInit"
    action: ask
    reason: "系统初始化函数修改需确认"
```

```python
class PermissionRuleEngine:
    """DSL 规则引擎

    优先级：deny > ask > allow
    匹配维度：pattern（文件名）/ tool（工具名）/ function（函数名）
    加载顺序：默认规则 → 用户级 ~/.iron/rules.yml → 项目级 .iron-agent/rules.yml → 自定义路径
    """
    def evaluate(self, context: dict) -> RuleDecision:
        # deny 优先，其次 ask，最后 allow
```

**4 条嵌入式默认规则**：
1. `*.ld` 文件禁止写
2. `startup_*.s/.S` 需确认
3. `embed_flash` 工具需确认
4. `SystemInit` 函数修改需确认

### 5.2 PreToolUse / PostToolUse Hooks（[hooks.py](file:///d:/嵌入式-Agent/iron/agent/hooks.py)）

```python
class HookManager:
    """工具执行前后介入

    PreToolUse：返回 deny 可阻止工具执行
    PostToolUse：审计日志、结果处理
    """
    def add_pre_hook(self, hook: PreToolUseHook): ...
    def add_post_hook(self, hook: PostToolUseHook): ...
    def load_hooks_from_dir(self, path: Path): ...  # ~/.iron/hooks/

# 内置 Hook
class SafetyCheckHook(PreToolUseHook):
    """路径检查 + 命令注入检查"""

class AuditLogHook(PostToolUseHook):
    """审计日志，记录所有工具调用"""
```

用户脚本示例（`~/.iron/hooks/my_hook.py`）：
```python
from iron.agent.hooks import PreToolUseHook, HookResult

class MyHook(PreToolUseHook):
    def check(self, tool_name: str, args: dict) -> HookResult:
        if tool_name == "embed_flash" and "--verify" not in args.get("command", ""):
            return HookResult(deny=True, reason="烧录必须带 --verify")
        return HookResult(allow=True)
```

### 5.3 三级审批持久化（[permission.py](file:///d:/嵌入式-Agent/iron/agent/permission.py)）

```python
class PermissionManager:
    """三级审批：once / session / never

    - once：单次允许（下次再问）
    - session：本次会话允许（内存，不落盘）
    - never：永久拒绝（持久化到 ~/.iron/permissions.yml 黑名单）
    """
    def check(self, tool_name: str, args: dict) -> str:
        # 返回 "allow" / "ask" / "deny"

    def record_decision(self, tool_name: str, args: dict, decision: str):
        # decision: "once" / "session" / "never"
```

**测试隔离**：`config.permission_persist_path` 支持注入临时路径，避免测试污染用户配置。

### 5.4 路径 / 命令 / SSRF 防护（保留 v2.4.0）

- **Path Guard**：`../` 穿越 / 绝对路径越界 / symlink / Windows 保留名
- **命令注入**：元字符 `\n\r` / 反引号 `$()` / 重定向 `>` / NULL 字节 / `python -c` / `node -e` / `%VAR%`
- **SSRF**：私有 IP / 环回 / 十进制 / 十六进制 / IPv4-mapped IPv6 / trailing-dot

---

## 六、L5 · 服务基础设施层

### 6.1 PubSub 事件总线（[pubsub.py](file:///d:/嵌入式-Agent/iron/core/pubsub.py)）

```python
class EventBus:
    """泛型事件总线

    特性：
    - async/sync 兼容（自动检测 handler 类型）
    - 错误隔离（单个 subscriber 异常不影响其他订阅者）
    - 默认全局单例 get_default_bus()
    - 支持注入独立实例（测试隔离）
    """
    def subscribe(self, event_type: str, handler: Callable): ...
    def publish(self, event_type: str, payload: Any): ...
    def unsubscribe(self, event_type: str, handler: Callable): ...

# 使用示例
bus = get_default_bus()
bus.subscribe("tool.executed", lambda payload: log.info(f"工具执行: {payload}"))
bus.publish("tool.executed", {"tool": "edit_file", "path": "main.py"})
```

**engine 集成**：`_emit_event` 方法同时 yield AgentEvent（兼容旧代码）和 publish 到 PubSub（新代码）。

### 6.2 SQLite 持久化（[db.py](file:///d:/嵌入式-Agent/iron/core/db.py)）

```python
class Database:
    """SQLite WAL 模式持久化

    特性：
    - WAL 模式（并发读不阻塞写）
    - 三表：sessions / messages / history
    - SQL 迁移机制（migrations/001_initial.sql）
    - 类型安全访问层
    """
    def create_session(self, ...) -> int: ...
    def add_message(self, session_id: int, role: str, content: str): ...
    def get_messages(self, session_id: int) -> list[dict]: ...
```

**迁移文件**：`iron/core/migrations/001_initial.sql`

### 6.3 4 层记忆系统（[memory.py](file:///d:/嵌入式-Agent/iron/agent/memory.py)）

```
.iron/memory/
├── MEMORY.md          ← 项目持久记忆（MAX_MEMORY_CHARS = 50000）
├── checkpoint.md      ← 最近一次会话检查点（写入前自动备份）
├── checkpoint_backup.md  ← 检查点备份
└── tasks/
    └── <id>/
        └── progress.md  ← 任务进度
```

- **Dream/Distill**：7天/30天自动记忆整理，`asyncio.Lock` 并发保护
- **token 截断**：按 token 截断而非字符（避免中文字符切半）
- **task_id 路径穿越防护**：正则 `^[a-zA-Z0-9_\-]+$`

### 6.4 Skills 系统（[registry.py](file:///d:/嵌入式-Agent/iron/skills/registry.py)）

```python
@dataclass
class PromptSkill:
    """数据驱动 Skill（v2.5.0 重构，消除 8 个子类重复）"""
    name: str
    description: str
    trigger_keywords: list[str]
    prompt: str
```

8 个内置 Skills 覆盖常见场景：build / flash / debug / refactor / review / test / docs / migrate。

### 6.5 MCP 客户端（[client.py](file:///d:/嵌入式-Agent/iron/mcp/client.py)）

- **三种传输**：stdio / SSE / HTTP
- **SSRF 防护**：仅允许同源 endpoint
- **环境变量过滤**：敏感关键字过滤（KEY/SECRET/TOKEN 等）
- **并发锁**：三种传输全部串行化
- **重连机制**：`reconnect()` 允许网络错误后恢复

### 6.6 LSP 客户端（[lsp_client.py](file:///d:/嵌入式-Agent/iron/integrations/lsp_client.py)）

```python
class LSPClient:
    """LSP 客户端（clangd / ccls）

    特性：
    - 自动检测 clangd / ccls
    - 自动查找 compile_commands.json
    - 提供 diagnostics / definition / hover / references / completion
    - 异步通信（JSON-RPC over stdio）
    """
```

### 6.7 EmbedForge / EmbedGuard 桥接（嵌入式特色）

- **EmbedForge**：编译 / 烧录 / 仿真（PlatformIO / Keil / CMake / ESP-IDF / GCC）
- **EmbedGuard**：静态分析（内存安全 / 中断安全 / 时序 / 资源 / 代码风格）
- 两参考项目（Claude Code / OpenCode）都没有

---

## 七、L6 · 终端 UI 层

### 7.1 流式 Markdown 渲染（[ui.py](file:///d:/嵌入式-Agent/iron/cli/ui.py)）

```python
class MarkdownStreamRenderer:
    """流式 Markdown 渲染

    特性：
    - 流式渲染，边接收边显示
    - 代码块语法高亮（rich.syntax）
    - 表格 / 列表 / 引用块完整渲染
    - 中断时 flush 已缓存的内容
    """
```

### 7.2 主题系统（[themes/](file:///d:/嵌入式-Agent/iron/cli/themes)）

```python
# iron/cli/themes/default.py
COLORS = {
    "primary": "cyan",
    "success": "green",
    "warning": "yellow",
    "error": "red",
    # ...
}

# iron/cli/theme.py
class _ColorsProxy:
    """动态代理，运行时切换主题"""
    def __getitem__(self, key):
        return _active_theme[key]
```

三套内置主题：`default` / `catppuccin` / `dracula`。

### 7.3 斜杠命令分组（[commands/](file:///d:/嵌入式-Agent/iron/cli/commands)）

```
iron/cli/commands/
├── file_cmds.py      # /read /write /edit /files
├── build_cmds.py     # /build /flash /lint
├── session_cmds.py   # /resume /save /clear
└── system_cmds.py    # /model /config /help /features
```

### 7.4 命令补全

- **WordCompleter**：输入 `/` 显示 6 个常用命令，进一步输入缩小匹配
- **上下键历史**：仅在 `/` 命令模式拦截，正常模式交给基础绑定
- **回车自动补全**：输入 `/` 开头且有匹配时，回车自动补全到高亮选项

### 7.5 启动信息

- 显示版本号、项目元信息（MCU / 模型 / 规则数 / 构建工具）
- 显示 API Key 前 4 后 4 字符确认有效性
- 401/403 错误检查含环境变量覆盖逻辑

---

## 八、嵌入式特色

Iron 保留并加强了嵌入式开发特色（两参考项目都没有）：

### 8.1 嵌入式铁律引擎（[iron_rules.py](file:///d:/嵌入式-Agent/iron/rules/iron_rules.py)）

11 条铁律 + 7 反模式 + 项目规则三层：
- 禁止裸 `delay()` 阻塞
- 禁止中断中调用 `printf`
- 必须 volatile 标记共享变量
- ... 共 11 条

### 8.2 MCU 配置（[.iron-agent/target-mcu.md](file:///d:/嵌入式-Agent/.iron-agent/target-mcu.md)）

支持 STM32G431 / STM32F407 profile，包含：
- 内存映射
- 外设地址
- 中断向量表
- 编译器选项

### 8.3 嵌入式专用 Agent

- `build` — 嵌入式构建专家（PlatformIO / Keil / CMake）
- `embed` — 嵌入式优化专家（内存 / 时序 / 资源）
- `plan` — 任务规划专家
- `verify` — 自动跑测试 + 静态分析
- `explore` — 只读代码探索

### 8.4 Dream/Distill 记忆

- 7 天自动 dream：整理短期记忆
- 30 天自动 distill：精炼长期记忆
- 参考 MiMo Code 设计
- `asyncio.Lock` 并发保护

---

## 九、配置文件

### 9.1 全局配置（`~/.iron/config.yml`）

```yaml
providers:
  mimo:
    backend_type: openai
    base_url: https://token-plan-cn.xiaomimimo.com/v1
    api_key: "<placeholder>"
    save_api_key: env  # env / disk / none
    models:
      - mimo-v2.5-pro
      - mimo-v2.5-mini
default_provider: mimo

# 嵌入式配置
project:
  mcu: STM32F407
  build_tool: platformio
  project_dir: .

# LLM 配置
llm:
  timeout: 120
  max_retries: 5

# 工具配置
tool_output_max_chars: 10000

# Stop Hooks 配置
stop_hooks_enabled: true
max_consecutive_failures: 5
max_tool_repetition: 10
no_progress_steps: 8

# 权限配置
permission_rules_enabled: true
permission_persist_path: ~/.iron/permissions.yml

# Prompt Caching 配置
prompt_caching_enabled: true
prompt_cache_ttl: 300
```

### 9.2 项目配置（`.iron-agent/`）

```
.iron-agent/
├── instructions.md       # 项目指令
├── rules/
│   ├── coding-standards.md  # 编码规范
│   └── target-mcu.md       # 目标 MCU 配置
└── rules.yml            # 项目级权限规则（覆盖全局）
```

### 9.3 特性配置（`~/.iron/features.yml`）

```yaml
# 用户级特性覆盖（默认值见 features.py）
lsp_enabled: true
vim_mode: false
prompt_caching: true
# ... 共 20 个
```

### 9.4 权限持久化（`~/.iron/permissions.yml`）

```yaml
# 黑名单（never 决策持久化）
deny:
  - tool: embed_flash
    pattern: "*"
  - tool: write_file
    pattern: "*.ld"
```

### 9.5 用户 Hooks（`~/.iron/hooks/`）

```
~/.iron/hooks/
└── my_hook.py    # 用户 PreToolUseHook / PostToolUseHook 脚本
```

### 9.6 用户规则（`~/.iron/rules.yml`）

```yaml
# 用户级权限规则（覆盖默认规则）
rules:
  - pattern: "*.ld"
    action: deny
    reason: "链接脚本禁止写"
```

---

## 十、性能与可观测性

### 10.1 性能优化

| 优化点 | 机制 |
|--------|------|
| 5 层压缩管道 | 按需触发，从轻量到重量 |
| Prompt Caching | 系统提示分块缓存，降低 ~85% 重复成本 |
| SQLite WAL | 并发读不阻塞写 |
| ToolSearchTool | 动态暴露相关工具，减少 token 浪费 |
| safe_execute 截断 | 避免大输出挤爆上下文 |
| 并行只读工具 | 减少串行等待 |
| 文件树缓存 | 避免重复扫描 |
| 进程组 kill | 避免孙进程残留 |

### 10.2 可观测性

| 维度 | 机制 |
|------|------|
| AuditLogHook | 记录所有工具调用（PostToolUse） |
| PubSub 事件总线 | 解耦事件生产者与消费者 |
| 思考时间显示 | AI 响应完成后显示 `⏱ 用时 Xs` |
| API Key 验证 | 启动时显示前 4 后 4 字符 |
| 日志记录 | logging 模块，含 exc_info |

---

## 十一、测试架构

### 11.1 测试组织

```
tests/
├── 单元测试（每个模块独立）
│   ├── test_backend.py           # LLM 后端
│   ├── test_engine.py            # Agent 引擎
│   ├── test_memory.py            # 记忆系统
│   ├── test_context_compactor.py # 5 层压缩
│   ├── test_stop_hooks.py        # Stop Hooks
│   ├── test_prompt_cache.py      # Prompt Caching
│   ├── test_pubsub.py            # 事件总线
│   ├── test_db.py                # SQLite
│   ├── test_lsp.py               # LSP 客户端
│   ├── test_permission_rules.py  # 规则引擎
│   ├── test_hooks.py             # PreToolUse/PostToolUse
│   ├── test_permission.py        # 三级审批
│   ├── test_tool_search.py       # ToolSearchTool
│   ├── test_patch.py             # patch 工具
│   ├── test_tool_truncation.py   # 输出截断
│   ├── test_markdown_renderer.py # Markdown 渲染
│   ├── test_theme.py             # 主题系统
│   ├── test_cli_commands.py      # 斜杠命令
│   ├── test_bootstrap.py         # 启动管道
│   └── test_features.py          # 特性门控
├── 集成测试
│   ├── test_engine_integration.py # 端到端
│   ├── test_mcp_client.py         # MCP stdio/SSE/HTTP
│   ├── test_task_agent.py         # 双 Agent
│   └── test_verify_explore_agent.py # 子代理
├── 安全测试
│   └── test_security.py          # 路径/SSRF/命令注入
└── 其他
    ├── test_core.py              # 工具/Skill/Agent
    ├── test_mcp.py               # MCP 配置
    ├── test_progressive_compaction.py
    └── test_p1_p2_enhancements.py
```

### 11.2 测试基线

- **测试用例**：738 passed, 1 skipped
- **测试文件**：28 个
- **测试代码**：9,977 行
- **测试比例**：0.61（达到优秀线 ≥ 0.6）
- **Mock LLM**：`_ScriptedLLM` 精确控制工具调用链
- **测试隔离**：`permission_persist_path` 等参数支持注入临时路径

---

## 十二、运行命令

```bash
# 启动
iron                        # 默认启动
iron --provider mimo        # 指定厂商
iron --model mimo-v2.5-pro  # 指定模型

# 配置
iron config                 # 交互式配置向导
iron config --list           # 列出所有配置

# 测试
pytest tests/ -v             # 全量测试
pytest tests/test_context_compactor.py -v  # 特定 P 任务

# 覆盖率
pytest tests/ --cov=iron --cov-report=term-missing
```

---

## 十三、文档参考

- [evaluation-v3.md](file:///d:/嵌入式-Agent/docs/evaluation-v3.md) — 完整测评报告（A- 评级）
- [gap-analysis.md](file:///d:/嵌入式-Agent/docs/gap-analysis.md) — 与 Claude Code/OpenCode 差距对比
- [architecture-framework.md](file:///d:/嵌入式-Agent/docs/architecture-framework.md) — 19 个 P 任务开发框架
- [测评.md](file:///d:/嵌入式-Agent/测评.md) — v2.4.0 评测报告（B+ 评级）
- [cli-agent-architecture.md](file:///d:/嵌入式-Agent/cli-agent-architecture.md) — Claude Code & OpenCode 架构深度解析

---

## 十四、变更记录

| 日期 | 版本 | 变更 |
|------|------|------|
| 2026-06-27 | v2.5.0+ | 完成 19 个 P 任务，L1-L6 六层架构完整落地 |
| 2026-06-27 | v2.4.0 | B+ 评级基线（详见 [测评.md](file:///d:/嵌入式-Agent/测评.md)） |
