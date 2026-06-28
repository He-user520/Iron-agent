# Track 4 · LSP 端到端集成子计划

> 子计划归属：Iron CLI 重构主计划 Track 4
> 前置依赖：Track 1（engine.py 拆分）必须完成并合并到 main
> 状态：待实施
> 预估工作量：1.5 人日（含测试）
> 关键文件：`iron/cli/bootstrap.py`、`iron/agent/engine.py`、`iron/cli/main.py`

---

## 1. 目标与约束

### 1.1 目标

LSP 客户端当前已实现（`iron/integrations/lsp_client.py`，558 行）并已封装为 5 个工具（`iron/tools/lsp_tools.py`，351 行），但**完全未接入主循环**。全仓 Grep 确认 `LSPClient` 仅出现在 3 个源文件中：

- `iron/integrations/lsp_client.py`（实现）
- `iron/tools/lsp_tools.py`（工具封装）
- `tests/test_lsp.py`（单元测试，55 个测试用例）

`bootstrap.py`、`engine.py`、`main.py`、`tools/__init__.py` 均无 `LSPClient` / `lsp_client` 引用。本 Track 的目标是打通完整链路：

```
启动（bootstrap 阶段 3）
  → 特性门控检查（features.lsp_tools）
  → LSP 客户端初始化（start() + initialize()）
  → 注入 AgentEngine（__init__ 接收 lsp_client）
  → 注册 5 个 LSP 工具到 _tool_registry
  → 文件变更通知钩子（did_open / did_change）
  → VerifyAgent / ExploreAgent 真实使用 LSP 工具
  → 优雅关闭（main.py 退出时调 stop()）
```

### 1.2 硬约束

| 编号 | 约束 | 说明 |
|------|------|------|
| C1 | LSP 启动失败不能导致 iron 退出 | `start()` 返回 `False` 或抛异常时，`bootstrap.py` 必须 `try/except` 降级为 `lsp_client=None`，主流程继续 |
| C2 | `did_change` / `did_open` 是 fire-and-forget 通知 | 不阻塞主循环，不等待 LSP 响应；通知失败仅 `logger.warning`，不上抛 |
| C3 | 不在 `process()` 主循环中直接调用 `LSPClient` 方法 | 必须通过工具注册（`lsp_diagnostics` 等工具）或 `_execute_*` 钩子间接调用 |
| C4 | VerifyAgent 调用 LSP 用 `asyncio.gather` 并行 | 多文件诊断并行执行，避免串行阻塞 |
| C5 | 工具未启动时降级行为保持 | 5 个 LSP 工具在 `_client is None` 或 `_initialized is False` 时返回 `success=False`（已在 `lsp_tools.py` 实现） |
| C6 | 特性门控默认关闭 | `features.lsp_tools` 默认 `False`（`features.py` line 41），用户需在 `~/.iron/features.yml` 显式启用 |

### 1.3 前置依赖

- **Track 1（engine.py 拆分）必须完成**：Track 1 提取了 `_execute_write_file`（`engine.py` line 1642-1707）和 `_execute_read_file`（line 1744-1837）为独立方法。本 Track 需要在这两个方法中加 `did_change` / `did_open` 钩子。如果在 Track 1 之前改 engine.py，会产生严重合并冲突。
- Track 2 / Track 3 可并行进行（冲突点见第 7 节）。

---

## 2. 现状分析

### 2.1 LSP 客户端已实现 API（`iron/integrations/lsp_client.py`，558 行）

#### 2.1.1 数据类（line 20-67）

```python
# line 20-31
@dataclass
class LSPDiagnostic:
    file: str; line: int; col: int
    end_line: int = 0; end_col: int = 0
    severity: int = 1  # 1=Error, 2=Warning, 3=Info, 4=Hint
    source: str = ""; message: str = ""; code: str = ""

# line 34-39
@dataclass
class LSPPosition:
    file: str; line: int; col: int

# line 42-47
@dataclass
class LSPHover:
    content: str
    range_start: Optional[LSPPosition] = None
    range_end: Optional[LSPPosition] = None

# line 50-57
@dataclass
class LSPCompletion:
    label: str; kind: int
    detail: str = ""; documentation: str = ""; insert_text: str = ""

# line 60-67
@dataclass
class LSPConfig:
    server_command: str = "clangd"
    server_args: list = field(default_factory=list)
    enabled: bool = True
    compile_commands_dir: str = ""
    init_options: dict = field(default_factory=dict)
```

#### 2.1.2 LSPClient 类（line 70-558）

**生命周期方法：**

| 方法 | 行号 | 签名 | 返回 | 说明 |
|------|------|------|------|------|
| `__init__` | 85 | `__init__(self, config: LSPConfig = None, project_root: str = ".")` | - | 初始化，`project_root` 会 `.resolve()` |
| `start` | 143 | `async start() -> bool` | `bool` | 启动 LSP 服务器进程 + initialize 握手；失败返回 `False` |
| `stop` | 188 | `async stop() -> None` | `None` | 取消读取任务 + 发送 exit + terminate/kill 进程 |
| `initialize` | 339 | `async initialize() -> bool` | `bool` | LSP initialize 握手 + initialized 通知 |

**文件通知方法（fire-and-forget）：**

| 方法 | 行号 | 签名 | 说明 |
|------|------|------|------|
| `did_open` | 404 | `async did_open(file_path: str, content: str) -> None` | 通知文件打开，自动识别 `.c/.h/.cpp` 语言 ID |
| `did_change` | 417 | `async did_change(file_path: str, content: str) -> None` | 通知文件修改 |
| `did_close` | 425 | `async did_close(file_path: str) -> None` | 通知文件关闭 |

**查询方法（5 个）：**

| 方法 | 行号 | 签名 | 返回 | 未启动时返回 |
|------|------|------|------|------|
| `get_diagnostics` | 432 | `async get_diagnostics(file_path: str)` | `list[LSPDiagnostic]` | `[]` |
| `definition` | 460 | `async definition(file_path: str, line: int, col: int)` | `list[LSPPosition]` | `[]` |
| `references` | 466 | `async references(file_path: str, line: int, col: int)` | `list[LSPPosition]` | `[]` |
| `hover` | 493 | `async hover(file_path: str, line: int, col: int)` | `Optional[LSPHover]` | `None` |
| `completion` | 526 | `async completion(file_path: str, line: int, col: int)` | `list[LSPCompletion]` | `[]` |

**静态方法：**

| 方法 | 行号 | 签名 | 返回 | 说明 |
|------|------|------|------|------|
| `detect_server` | 99 | `@staticmethod detect_server() -> Optional[str]` | `clangd` 优先，`ccls` 次之；都无返回 `None` | 用 `shutil.which` |
| `find_compile_commands` | 108 | `@staticmethod find_compile_commands(project_root: Path) -> Optional[Path]` | 查找 `build/`、`.pio/build/<env>/`、`cmake-build-*/` 下的 `compile_commands.json` | 未找到返回 `None` |

#### 2.1.3 关键内部状态

- `_initialized: bool`（line 91）：`initialize()` 成功后置 `True`，`stop()` 后置 `False`。所有查询方法首检此标志。
- `_proc: Optional[asyncio.subprocess.Process]`（line 88）：LSP 服务器子进程。
- `_diagnostics: dict[str, list[LSPDiagnostic]]`（line 92）：诊断缓存，由 `_handle_diagnostics`（line 319）维护。

### 2.2 LSP 工具已实现（`iron/tools/lsp_tools.py`，351 行）

5 个工具类，全部继承 `BaseTool`，结构统一：

| 工具类 | 行号 | `name` 属性 | `set_client` 行号 | 未启动降级返回 |
|--------|------|-------------|-------------------|----------------|
| `LSPDiagnosticsTool` | 28-93 | `lsp_diagnostics` | 34-36 | `{"success": False, "error": "LSP 服务器未启动", "diagnostics": []}` |
| `LSPDefinitionTool` | 96-152 | `lsp_definition` | 102-103 | `{"success": False, "error": "LSP 服务器未启动", "definitions": []}` |
| `LSPReferencesTool` | 155-211 | `lsp_references` | 161-162 | `{"success": False, "error": "LSP 服务器未启动", "references": []}` |
| `LSPHoverTool` | 214-286 | `lsp_hover` | 220-221 | `{"success": False, "error": "LSP 服务器未启动", "hover": None}` |
| `LSPCompletionTool` | 289-351 | `lsp_completion` | 295-296 | `{"success": False, "error": "LSP 服务器未启动", "completions": []}` |

**降级检测逻辑**（5 个工具的 `execute` 方法第一行统一模式，以 `LSPDiagnosticsTool` line 63 为例）：

```python
if not self._client or not getattr(self._client, "_initialized", False):
    return {"success": False, "error": "LSP 服务器未启动", "diagnostics": []}
```

**所有工具的 `execute` 方法签名统一**：`async execute(self, args: dict, context: dict) -> dict`，`args` 包含 `file` / `line` / `col` 字段。

**所有工具处理 `asyncio.CancelledError`**：re-raise；处理 `(RuntimeError, ValueError, OSError)`：返回 `success=False`。

### 2.3 集成缺失点清单

| 编号 | 缺失点 | 证据（文件 + 行号） | 影响 |
|------|--------|---------------------|------|
| M1 | `bootstrap.py` 阶段 3 未启动 LSP 客户端 | `_phase_run`（line 181-232）仅记录 `lsp_tools` 特性状态（line 207-208），无 `LSPClient` 实例化 | LSP 客户端永远不会被创建 |
| M2 | `tools/__init__.py` `create_default_registry()` 未注册 5 个 LSP 工具 | `tools/__init__.py` line 19-35 只注册 13 个工具，无 LSP 工具 | 即使有 `lsp_client`，工具也无法被 Agent 调用 |
| M3 | `engine.py` `__init__` 未接收 `lsp_client` 参数 | `BaseAgentEngine.__init__`（line 104-105）签名无 `lsp_client` | LSP 客户端无法注入到引擎 |
| M4 | `engine.py` `_execute_write_file` 无 `did_change` 钩子 | line 1642-1707，写入磁盘后（line 1693）无 LSP 通知 | LSP 缓存的文件内容会过期，诊断不准 |
| M5 | `engine.py` `_execute_read_file` 无 `did_open` 钩子 | line 1744-1837，读取后无 LSP 通知 | LSP 不感知文件打开，无法推送诊断 |
| M6 | `main.py` 退出时无 LSP 清理 | `_cleanup_engine_mcp`（line 145-155）只清理 MCP；`run_interactive` 退出清理（line 614-658）只 `disconnect_all()` MCP + `aclose()` LLM | LSP 子进程泄漏（clangd/ccls 成为孤儿进程） |
| M7 | `VerifyAgent.verify()` 仅靠 prompt 指示，未显式调用 LSP 工具 | `verify()`（line 2278-2308）通过 `process(prompt)` 驱动 LLM，prompt 里写"检查 LSP 诊断"，但 LLM 不一定调用 `lsp_diagnostics` 工具 | 验证结果依赖 LLM 自觉性，不稳定 |
| M8 | `ExploreAgent.EXPLORE_TOOLS` 声明了 LSP 工具但工具未注册 | `EXPLORE_TOOLS`（line 2321-2324）包含 `lsp_definition/lsp_references/lsp_hover`，但 M2 导致工具不存在 | `_filter_tools_schema` 过滤后这些工具的 schema 消失，LLM 看不到也调不到 |

### 2.4 特性门控现状

**重要**：任务描述中提到的特性门控名 `lsp_enabled` 与实际代码不符。实际特性名是 **`lsp_tools`**（`features.py` line 41）：

```python
# iron/config/features.py line 31-58
DEFAULT_FEATURES = {
    ...
    "lsp_tools": False,  # 可选：LSP 工具（默认关闭，需要 clangd）
    ...
}
```

**默认值**：`False`（line 41）。用户需在 `~/.iron/features.yml` 显式启用：

```yaml
# ~/.iron/features.yml
lsp_tools: true
```

**用户覆盖机制**（`features.py` line 85-106）：`FeatureFlags._load_user_overrides()` 从 YAML 加载，未知 key 或非 bool 值记录 warning 并跳过。

**bootstrap.py 已有的预检**（line 207-208）：

```python
if flags.is_enabled("lsp_tools"):
    logger.debug("lsp_tools 特性已启用，将在 AgentEngine 中初始化 LSP 客户端")
```

注释说"将在 AgentEngine 中初始化"，但实际 `AgentEngine` 中并无初始化代码 —— 这是本 Track 要补齐的缺口。

---

## 3. 设计方案

### 3.1 bootstrap.py 阶段 3 改造

**目标**：在 `_phase_run`（line 181-232）中初始化 LSP 客户端，并通过 `BootstrapResult` 传递给 `run_interactive`。

**改造点 1：`BootstrapResult` 新增 `lsp_client` 字段**（`bootstrap.py` line 31-47）：

```python
@dataclass
class BootstrapResult:
    success: bool = False
    config: Optional[object] = None
    llm: Optional[object] = None
    prompt_builder: Optional[object] = None
    skills: Optional[object] = None
    lsp_client: Optional[object] = None  # 新增：LSP 客户端实例
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    phases_executed: list = field(default_factory=list)
```

**改造点 2：`_phase_run` 返回值改为 3 元组**（line 181-232）：

当前 `_phase_run` 返回 `(prompt_builder, skills)`。改为返回 `(prompt_builder, skills, lsp_client)`。`run()` 方法（line 117）对应修改：

```python
# line 117 改造前
prompt_builder, skills = self._phase_run(config, project_root)

# line 117 改造后
prompt_builder, skills, lsp_client = self._phase_run(config, project_root)
```

`BootstrapResult` 构造（line 120-129）新增 `lsp_client=lsp_client`。

**改造点 3：`_phase_run` 内部新增 LSP 初始化逻辑**（在 line 213 `prompt_builder = PromptBuilder(...)` 之前插入）：

```python
# LSP 客户端初始化（特性门控 + 启动失败降级）
lsp_client = None
try:
    if flags.is_enabled("lsp_tools"):
        from iron.integrations.lsp_client import LSPClient, LSPConfig
        cc_path = LSPClient.find_compile_commands(project_root)
        lsp_config = LSPConfig(
            enabled=True,
            compile_commands_dir=str(cc_path.parent) if cc_path else "",
        )
        lsp_client = LSPClient(lsp_config, project_root=str(project_root))
        # start() 是 async，用 asyncio.run 包装（bootstrap 是同步函数）
        import asyncio
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
```

**注意**：`flags` 变量已在 line 198-211 定义（`flags = get_feature_flags()`），但当前在 `try` 块内。需将 `flags` 提到 `try` 块外，或在本段独立调用 `get_feature_flags()`。

**改造点 4：`cli()` 函数传递 `lsp_client`**（`main.py` line 227-236）：

```python
# main.py line 227-232 改造
config = result.config
ctx.obj["config"] = config
ctx.obj["project_root"] = project_root
ctx.obj["llm"] = result.llm
ctx.obj["prompt_builder"] = result.prompt_builder
ctx.obj["skills"] = result.skills
ctx.obj["lsp_client"] = result.lsp_client  # 新增
```

`run_interactive` 调用处（line 236）传入：

```python
run_interactive(config, project_root, lsp_client=result.lsp_client)
```

### 3.2 engine.py `__init__` 改造

**目标**：`BaseAgentEngine.__init__` 接收 `lsp_client` 参数，注册 5 个 LSP 工具到 `_tool_registry`。

**改造点 1：`__init__` 签名扩展**（line 104-105）：

```python
# 改造前
def __init__(self, llm: LLMBackend, prompt_builder: PromptBuilder, skills: SkillRegistry,
             config=None, tools: dict = None, event_bus: EventBus = None):

# 改造后
def __init__(self, llm: LLMBackend, prompt_builder: PromptBuilder, skills: SkillRegistry,
             config=None, tools: dict = None, event_bus: EventBus = None,
             lsp_client=None):
```

**改造点 2：保存 `lsp_client` 并注册工具**（在 line 137 `self._tool_registry.set_max_output_chars(...)` 之后，line 138 `# v2: MCP 客户端` 之前插入）：

```python
# LSP 客户端 + 工具注册
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
```

**改造点 3：`_run_agent` 调用处传递 `lsp_client`**（`main.py` line 862）：

```python
# main.py line 862 改造前
engine = engine_class(llm=llm, prompt_builder=prompt_builder, skills=skills, config=config)

# main.py line 862 改造后
engine = engine_class(llm=llm, prompt_builder=prompt_builder, skills=skills, config=config,
                      lsp_client=cmd_ctx.get("lsp_client"))
```

`cmd_ctx`（main.py line 502-514）需新增 `"lsp_client": lsp_client` 键。

**向后兼容性**：`lsp_client=None` 时 5 个工具的 `execute` 方法返回 `success=False`（已实现降级，见 2.2 节），不影响现有行为。

### 3.3 文件变更通知钩子

**目标**：在 `_execute_write_file` 和 `_execute_read_file` 中加 `did_change` / `did_open` 通知，fire-and-forget 不阻塞。

**设计原则**：
- 通知是 best-effort：失败仅 `logger.warning`，不上抛
- 用 `asyncio.create_task` 包装为 fire-and-forget，不 `await`（避免阻塞 generator）
- 仅对 C/C++ 源文件通知（`.c/.h/.cpp/.cc/.cxx/.hpp/.hh`），避免对 `.md/.py` 等无意义通知

**改造点 1：`_execute_write_file` 加 `did_change`**（engine.py line 1707，`file_done` 事件之后）：

```python
# line 1707 之后插入
yield await self._emit_event("file_done", {"path": path, "code": content, "lines": content.count("\n"), "action": action})

# 新增：LSP 文件变更通知（fire-and-forget）
await self._notify_lsp_file_change(path, content)
```

**改造点 2：`_execute_read_file` 加 `did_open`**（engine.py line 1839 之后，读取 `full_content` 成功后）：

```python
# 读取成功后，分页处理之前插入
# 新增：LSP 文件打开通知（fire-and-forget）
await self._notify_lsp_file_open(path, full_content)
```

**改造点 3：新增辅助方法 `_notify_lsp_file_change` / `_notify_lsp_file_open`**（在 `_execute_write_file` 之前，约 line 1640）：

```python
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
```

**改造点 4：`edit_file` 分支加 `did_change`**：

`edit_file` 工具最终也调用 `full_path.write_text(...)`。需在 `engine.py` 中搜索 `EditFileTool` 的执行路径，找到写入磁盘的位置，同样加 `await self._notify_lsp_file_change(path, new_content)`。

> 注：`EditFileTool` 在 `iron/tools/edit_file.py` 中实现，其 `execute` 方法返回 dict 而非 generator。engine 通过 `_tool_registry.execute()` 调用。需在 engine 的工具执行回调（搜索 `_tool_registry.execute` 调用处）后，根据工具名判断是否需要发 `did_change`。具体位置在 Step 5 实施时定位。

### 3.4 VerifyAgent 显式 LSP 集成

**目标**：`verify()` 方法显式调用 `lsp_diagnostics` 工具，并行收集多文件诊断，不依赖 LLM 自觉性。

**当前问题**（line 2278-2308）：`verify()` 通过 `process(prompt)` 驱动 LLM，prompt 写"检查 LSP 诊断"，但 LLM 可能不调用 `lsp_diagnostics` 工具，导致验证结果不稳定。

**改造点：`verify()` 方法重构**（line 2278-2308）：

```python
async def verify(self, target: str = "src/") -> dict:
    """执行完整验证流程

    改造后：
    1. 显式收集 source 文件列表（_collect_source_files）
    2. asyncio.gather 并行调用 lsp_diagnostics（约束 C4）
    3. 汇总诊断结果
    4. 通过 process() 让 LLM 综合分析（静态分析 + 编译 + LSP 诊断）
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
    """收集目标路径下的 C/C++ 源文件"""
    from iron.constants import SOURCE_EXTENSIONS
    target_path = Path(target)
    if target_path.is_file():
        return [str(target_path)]
    files = []
    for ext in {".c", ".h", ".cpp", ".cc", ".cxx", ".hpp", ".hh"}:
        files.extend(str(p) for p in target_path.rglob(f"*{ext}"))
    return files[:50]  # 限制最多 50 个文件，避免 LSP 过载

async def _collect_lsp_diagnostics(self, target: str) -> str:
    """并行收集 LSP 诊断（约束 C4：asyncio.gather）"""
    if not self._lsp_client or not getattr(self._lsp_client, "_initialized", False):
        return "LSP 未启动，跳过诊断"
    files = await self._collect_source_files(target)
    if not files:
        return f"未在 {target} 下找到 C/C++ 源文件"
    # 并行调用 get_diagnostics
    tasks = [self._lsp_client.get_diagnostics(f) for f in files]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    lines = []
    for f, diags in zip(files, results):
        if isinstance(diags, Exception):
            lines.append(f"  {f}: 诊断失败 ({diags})")
            continue
        if diags:
            lines.append(f"  {f}: {len(diags)} 个诊断")
            for d in diags[:5]:  # 每文件最多 5 条
                lines.append(f"    L{d.line}: [{d.severity}] {d.message}")
    return "\n".join(lines) if lines else "无诊断"
```

### 3.5 main.py 退出清理

**目标**：新增 `_cleanup_lsp` 函数，在 `run_interactive` 退出时调用 `lsp_client.stop()`，避免 clangd/ccls 子进程泄漏。

**改造点 1：新增 `_cleanup_lsp` 函数**（main.py line 145 附近，`_cleanup_engine_mcp` 之后）：

```python
def _cleanup_lsp(lsp_client):
    """清理 LSP 客户端子进程，避免 clangd/ccls 孤儿进程

    LSP 客户端在 bootstrap 阶段 3 创建，需在 run_interactive 退出时显式 stop()，
    否则 clangd/ccls 子进程会成为孤儿（仅靠 GC 无法可靠终止）。
    """
    if lsp_client is not None:
        _safe_run_async(
            lsp_client.stop(),
            fail_msg="清理 LSP 客户端失败",
        )
```

**改造点 2：`run_interactive` 签名扩展**（main.py line 388）：

```python
# 改造前
def run_interactive(config: IronConfig, project_root: Path):

# 改造后
def run_interactive(config: IronConfig, project_root: Path, lsp_client=None):
```

**改造点 3：退出清理调用**（main.py line 628-658，MCP 清理旁）：

```python
# line 630-632 改造前
try:
    if last_engine is not None and getattr(last_engine, "_mcp_client", None):
        loop.run_until_complete(last_engine._mcp_client.disconnect_all())
    if llm is not None and hasattr(llm, "aclose"):
        loop.run_until_complete(llm.aclose())

# 改造后
try:
    if last_engine is not None and getattr(last_engine, "_mcp_client", None):
        loop.run_until_complete(last_engine._mcp_client.disconnect_all())
    if llm is not None and hasattr(llm, "aclose"):
        loop.run_until_complete(llm.aclose())
    # LSP 客户端清理（约束 C1：失败不阻塞退出）
    if lsp_client is not None:
        try:
            loop.run_until_complete(lsp_client.stop())
        except (RuntimeError, OSError) as e:
            console.print(f"  LSP 清理失败: {e}", style="dim yellow")
except (RuntimeError, OSError) as e:
    console.print(f"  资源清理失败: {e}", style="dim yellow")
```

**改造点 4：`_run_agent` 创建新 engine 前清理旧 engine 的 LSP**（main.py line 611，`_cleanup_engine_mcp` 旁）：

```python
# line 611 改造前
_cleanup_engine_mcp(last_engine)

# 改造后（LSP 客户端是全局单例，不随 engine 重建；此处不清理 LSP）
_cleanup_engine_mcp(last_engine)
# 注：lsp_client 是 bootstrap 创建的全局实例，所有 engine 共享，不在此处清理
```

### 3.6 ExploreAgent 工具可用性保障

**目标**：确保 `EXPLORE_TOOLS` 声明的 LSP 工具在 `_filter_tools_schema` 后仍可见。

**当前状态**：`EXPLORE_TOOLS`（line 2321-2324）已包含 `lsp_definition/lsp_references/lsp_hover`。`_filter_tools_schema`（line 322-330）逻辑是"只保留 `allowed` 集合中的工具"，所以只要工具注册到 `_tool_registry`（3.2 节改造后），schema 自然可见。

**无需额外改造**：3.2 节在 `__init__` 中注册 5 个 LSP 工具后，`_filter_tools_schema` 会自动保留 `EXPLORE_TOOLS` 中声明的 3 个 LSP 工具。仅需验证（Step 9）。

**潜在问题**：`EXPLORE_TOOLS` 包含 `list_files` / `grep` / `glob`，但 `create_default_registry()` 未注册这些工具（`tools/__init__.py` line 19-35 只有 `find_files` / `search_code`）。这是 Track 4 范围外的预存问题，本 Track 不修复，仅在验证清单中记录。

---

## 4. 实施步骤（按顺序执行，每步带验证）

### Step 1: 创建 git tag pre-lsp-integration

**前置**：Track 1 已合并到 main。

```bash
git checkout main
git pull origin main
git tag pre-lsp-integration
git push origin pre-lsp-integration
```

**验证**：
```bash
git tag -l "pre-lsp-integration"
# 应输出：pre-lsp-integration
```

### Step 2: 新增 tests/test_lsp_integration.py 测试骨架

**TDD 原则**：先写测试再实现。测试用例先全部 fail（或 skip），逐步实现后变绿。

**文件**：`tests/test_lsp_integration.py`

**内容**：见第 8 节"测试用例详细设计"（10 个测试用例骨架）。

**验证**：
```bash
pytest tests/test_lsp_integration.py -v
# 预期：10 个测试全部 fail（或 skip，如果用了 @pytest.mark.skip）
```

**commit**：
```bash
git add tests/test_lsp_integration.py
git commit -m "test(lsp): 新增 LSP 集成测试骨架（10 用例，TDD）"
```

### Step 3: bootstrap.py 阶段 3 增加 LSP 初始化

**改动文件**：
- `iron/cli/bootstrap.py`（`BootstrapResult` 加 `lsp_client` 字段；`_phase_run` 返回 3 元组；新增 LSP 初始化逻辑）
- `iron/cli/main.py`（`cli()` 函数传递 `lsp_client`；`run_interactive` 签名加 `lsp_client=None`）

**代码片段**：见 3.1 节。

**验证**：
```bash
pytest tests/test_bootstrap.py -v
# 预期：原有测试全绿（确保未破坏启动管道）

pytest tests/test_lsp_integration.py::TestLSPLifecycleInBootstrap -v
# 预期：test_lsp_client_lifecycle_in_bootstrap 通过
```

**commit**：
```bash
git add iron/cli/bootstrap.py iron/cli/main.py
git commit -m "feat(bootstrap): 阶段 3 初始化 LSP 客户端，特性门控 lsp_tools 控制"
```

### Step 4: engine.py `__init__` 接收 `lsp_client` 并注册工具

**改动文件**：
- `iron/agent/engine.py`（`BaseAgentEngine.__init__` 签名加 `lsp_client=None`；新增 LSP 工具注册逻辑）
- `iron/cli/main.py`（`_run_agent` 调用处传 `lsp_client`；`cmd_ctx` 加 `lsp_client` 键）

**代码片段**：见 3.2 节。

**验证**：
```bash
pytest tests/test_engine.py -v
# 预期：原有测试全绿（lsp_client=None 默认值保证向后兼容）

pytest tests/test_lsp_integration.py::TestLSPToolsRegistration -v
# 预期：test_lsp_tools_registered_in_engine 通过
```

**commit**：
```bash
git add iron/agent/engine.py iron/cli/main.py
git commit -m "feat(engine): __init__ 接收 lsp_client 并注册 5 个 LSP 工具"
```

### Step 5: engine.py 文件变更通知钩子

**改动文件**：
- `iron/agent/engine.py`（新增 `_notify_lsp_file_change` / `_notify_lsp_file_open` 辅助方法；`_execute_write_file` 加 `did_change`；`_execute_read_file` 加 `did_open`；edit_file 路径加 `did_change`）

**代码片段**：见 3.3 节。

**验证**：
```bash
pytest tests/test_lsp_integration.py::TestLSPFileNotifications -v
# 预期：
#   test_write_file_triggers_did_change 通过
#   test_read_file_triggers_did_open 通过

pytest tests/test_engine.py -v
# 预期：原有测试全绿（fire-and-forget 不影响主流程）
```

**commit**：
```bash
git add iron/agent/engine.py
git commit -m "feat(engine): 文件读写钩子通知 LSP（did_open/did_change，fire-and-forget）"
```

### Step 6: VerifyAgent 显式 LSP 集成

**改动文件**：
- `iron/agent/engine.py`（`VerifyAgent.verify()` 重构；新增 `_collect_source_files` / `_collect_lsp_diagnostics` 辅助方法）

**代码片段**：见 3.4 节。

**验证**：
```bash
pytest tests/test_lsp_integration.py::TestVerifyAgentLSPIntegration -v
# 预期：test_verify_agent_calls_lsp_diagnostics 通过

pytest tests/ -k "verify" -v
# 预期：原有 verify 相关测试不回归
```

**commit**：
```bash
git add iron/agent/engine.py
git commit -m "feat(verify): VerifyAgent 显式并行调用 LSP 诊断（asyncio.gather）"
```

### Step 7: main.py 退出清理

**改动文件**：
- `iron/cli/main.py`（新增 `_cleanup_lsp` 函数；`run_interactive` 退出清理加 LSP stop）

**代码片段**：见 3.5 节。

**验证**：
```bash
pytest tests/test_lsp_integration.py::TestLSPCleanup -v
# 预期：test_lsp_cleanup_on_exit 通过
```

**commit**：
```bash
git add iron/cli/main.py
git commit -m "feat(main): 退出时清理 LSP 客户端，避免 clangd/ccls 子进程泄漏"
```

### Step 8: 特性门控与降级测试

**改动文件**：无（纯测试验证）。

**验证**：
```bash
pytest tests/test_lsp_integration.py::TestFeatureGateAndDegradation -v
# 预期：
#   test_lsp_disabled_when_feature_off 通过（features.lsp_tools=False 时 lsp_client=None）
#   test_lsp_startup_failure_degrades_gracefully 通过（start() 失败时降级）
```

**commit**：
```bash
git add tests/test_lsp_integration.py
git commit -m "test(lsp): 特性门控与降级场景测试"
```

### Step 9: ExploreAgent 工具可用性验证

**改动文件**：无（纯测试验证）。

**验证**：
```bash
pytest tests/test_lsp_integration.py::TestExploreAgentLSPTools -v
# 预期：test_explore_agent_has_lsp_tools 通过
```

**commit**：
```bash
git add tests/test_lsp_integration.py
git commit -m "test(lsp): ExploreAgent LSP 工具可用性验证"
```

### Step 10: 全量回归测试

**命令**：
```bash
pytest tests/ -v
```

**预期**：
- 总数 ≥ 748 passed（738 原有 + 10 新增）
- 0 failed
- 0 error

**额外验证**：
```bash
# 确认集成点存在
grep -c "LSPClient" iron/agent/engine.py        # 应 ≥ 0（通过工具间接引用）
grep -c "_lsp_client" iron/agent/engine.py      # 应 ≥ 3
grep -c "lsp_client" iron/cli/bootstrap.py      # 应 ≥ 1
grep -c "lsp_client" iron/cli/main.py           # 应 ≥ 3
```

**commit**（如有调整）：
```bash
git add -A
git commit -m "test(lsp): 全量回归验证通过（≥748 passed）"
```

---

## 5. 验证清单

### 5.1 代码集成验证

- [ ] `grep LSPClient in iron/agent/engine.py` 命中 ≥ 0 处（通过 `from iron.tools.lsp_tools import ...` 间接引用）
- [ ] `grep _lsp_client in iron/agent/engine.py` 命中 ≥ 3 处（`__init__` 保存 + 2 个通知辅助方法检查）
- [ ] `grep lsp_client in iron/cli/bootstrap.py` 命中 ≥ 3 处（`BootstrapResult` 字段 + `_phase_run` 初始化 + `run()` 传递）
- [ ] `grep lsp_client in iron/cli/main.py` 命中 ≥ 3 处（`cli()` 传递 + `run_interactive` 签名 + `_cleanup_lsp` + 退出清理）
- [ ] `grep LSPDiagnosticsTool in iron/agent/engine.py` 命中 ≥ 1 处（工具注册）

### 5.2 测试验证

- [ ] `pytest tests/test_lsp.py -v` 全绿（原有 55 个测试函数不回归）
- [ ] `pytest tests/test_lsp_integration.py -v` 全绿（新增 ≥ 10 个测试用例）
- [ ] `pytest tests/ -v` 总数 ≥ 748 passed
- [ ] `pytest tests/test_bootstrap.py -v` 全绿（启动管道不回归）
- [ ] `pytest tests/test_engine.py -v` 全绿（引擎不回归）

### 5.3 手动测试

- [ ] 在嵌入式项目目录（有 `build/compile_commands.json` + 已装 clangd）启动 iron，`~/.iron/features.yml` 设 `lsp_tools: true`，启动日志显示"LSP 客户端启动成功"
- [ ] 在无 clangd 的项目启动 iron，`features.yml` 设 `lsp_tools: true`，启动日志显示"LSP 客户端启动失败，降级到无 LSP 模式"，iron 正常运行
- [ ] 在有 clangd 的项目启动 iron，`features.yml` 不设 `lsp_tools`（默认 False），启动日志显示"lsp_tools 特性未启用"，iron 正常运行
- [ ] 执行 `/code` 写入一个 `.c` 文件后，LSP 日志显示 `did_change` 通知
- [ ] 执行 `/verify src/`，VerifyAgent 输出包含"LSP 诊断"汇总
- [ ] 退出 iron（`/quit` 或 Ctrl+C 双击），LSP 客户端 `stop()` 被调用，无 clangd 孤儿进程

---

## 6. 回滚策略

### 6.1 Tag 回滚

```bash
# Step 1 已创建 tag
git tag pre-lsp-integration

# 任何步骤失败，回滚到 Track 1 完成后的状态
git reset --hard pre-lsp-integration
```

### 6.2 按步骤回滚

每个 Step 独立 commit，可针对性回滚：

```bash
# 查看本 Track 所有 commit
git log pre-lsp-integration..HEAD --oneline

# 回滚到某个 Step
git reset --hard <commit-sha>
```

### 6.3 部分回滚策略

- **Step 3 失败**（bootstrap 改造导致启动崩溃）：回滚 Step 3，保留 Step 2 测试骨架
- **Step 4 失败**（engine `__init__` 改造导致工具注册崩溃）：回滚 Step 3-4，bootstrap 不再初始化 LSP
- **Step 5 失败**（通知钩子导致主循环阻塞）：回滚 Step 5，保留 Step 3-4（LSP 工具可用但无文件通知，诊断可能过期）
- **Step 6 失败**（VerifyAgent 重构导致 verify 崩溃）：回滚 Step 6，保留 Step 3-5（VerifyAgent 退回到 prompt 驱动模式）
- **Step 7 失败**（退出清理导致退出崩溃）：回滚 Step 7，保留 Step 3-6（LSP 子进程可能泄漏，但不影响功能）

---

## 7. 与其他 Track 的接口契约

### 7.1 前置依赖：Track 1（engine.py 拆分）

| 依赖项 | Track 1 产出 | 本 Track 使用点 |
|--------|-------------|----------------|
| `_execute_write_file` 提取为独立方法 | line 1642-1707 | Step 5 加 `did_change` 钩子 |
| `_execute_read_file` 提取为独立方法 | line 1744-1837 | Step 5 加 `did_open` 钩子 |

**冲突说明**：如果在 Track 1 之前改 engine.py，Track 1 的方法提取会与本 Track 的钩子插入产生严重合并冲突。**必须等 Track 1 合并到 main 后再开始本 Track**。

### 7.2 与 Track 2（main.py 拆分）的冲突点

| 冲突文件 | 冲突函数 | Track 4 改动 | Track 2 改动 | 冲突级别 |
|----------|---------|-------------|-------------|----------|
| `main.py` | `run_interactive` | 退出清理加 LSP stop（line 628-658） | 拆分 `run_interactive` 到多个模块 | 中 |
| `main.py` | `_cleanup_engine_mcp` 附近 | 新增 `_cleanup_lsp` 函数（line 145 后） | 可能迁移清理函数到独立模块 | 低 |
| `main.py` | `_run_agent` 调用处 | 传 `lsp_client` 参数（line 862） | 可能拆分 `_run_agent` | 低 |

**缓解措施**：Track 4 的 main.py 改动集中在退出清理和参数传递，Track 2 主要拆分命令分发逻辑。改动不同函数，冲突可控。

### 7.3 与 Track 3（流式恢复）的冲突点

| 冲突文件 | 冲突方法 | Track 4 改动 | Track 3 改动 | 冲突级别 |
|----------|---------|-------------|-------------|----------|
| `engine.py` | `_execute_write_file` | Step 5 加 `did_change` | 可能改流式输出逻辑 | 低 |
| `engine.py` | `_execute_read_file` | Step 5 加 `did_open` | 可能改流式输出逻辑 | 低 |
| `engine.py` | `__init__` | Step 4 加 `lsp_client` 参数 | 可能改 `__init__` 签名 | 中 |
| `engine.py` | `process()` 主循环 | **不改**（约束 C3） | 改 `_handle_thinking_phase` 等 | 无 |

**缓解措施**：Track 4 不改 `process()` 主循环，Track 3 不改 `__init__` 签名（若改，需协调）。

### 7.4 推荐合并顺序

```
Track 1（engine.py 拆分）
    ↓
    ↓ （Track 1 合并到 main）
    ↓
Track 4 Step 1-7（LSP 集成核心）
    ↓
    ↓ （Track 4 Step 1-7 合并到 main）
    ↓
Track 3（流式恢复，可基于已合并的 Track 4 engine.py）
    ↓
Track 2（main.py 拆分，最后做，避免与 Track 4 main.py 改动冲突）
    ↓
Track 4 Step 8-10（测试验证，最后回归）
```

**替代方案**：Track 2 与 Track 4 可并行开发（不同分支），合并时手动解决 `main.py` 冲突。

---

## 8. 测试用例详细设计

### 8.1 测试文件结构

**文件**：`tests/test_lsp_integration.py`

**设计原则**：
- 所有测试用 mock，不依赖真实 clangd/ccls 安装
- 每个测试隔离（独立 `lsp_client` 实例，不污染全局单例）
- 覆盖集成链路：启动 → 工具注册 → 文件通知 → VerifyAgent → 退出清理 → 降级

### 8.2 测试用例骨架

```python
"""LSP 端到端集成测试 — 覆盖 bootstrap/engine/main 全链路集成

运行方式: pytest tests/test_lsp_integration.py -v

测试策略：
- 所有测试用 mock，不依赖真实 clangd/ccls
- 每个测试类覆盖一个集成点
- 共 10 个测试用例，对应 Step 2-9 的验证
"""
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from iron.integrations.lsp_client import LSPClient, LSPConfig


# ── 测试夹具 ──────────────────────────────────────────────────

@pytest.fixture
def mock_lsp_client():
    """构造已初始化的 mock LSP 客户端"""
    client = MagicMock(spec=LSPClient)
    client._initialized = True
    client.start = AsyncMock(return_value=True)
    client.stop = AsyncMock(return_value=None)
    client.did_open = AsyncMock(return_value=None)
    client.did_change = AsyncMock(return_value=None)
    client.did_close = AsyncMock(return_value=None)
    client.get_diagnostics = AsyncMock(return_value=[])
    client.definition = AsyncMock(return_value=[])
    client.references = AsyncMock(return_value=[])
    client.hover = AsyncMock(return_value=None)
    client.completion = AsyncMock(return_value=[])
    return client


@pytest.fixture
def disabled_lsp_client():
    """构造未初始化的 mock LSP 客户端（_initialized=False）"""
    client = MagicMock(spec=LSPClient)
    client._initialized = False
    return client


# ── 1. bootstrap 生命周期 ─────────────────────────────────────

class TestLSPLifecycleInBootstrap:
    """验证 bootstrap 阶段 3 的 LSP 客户端初始化"""

    @pytest.mark.asyncio
    async def test_lsp_client_lifecycle_in_bootstrap(self, tmp_path, mock_lsp_client):
        """Test 1: 特性门控开启 + 启动成功 → lsp_client 注入 BootstrapResult

        断言要点：
        - BootstrapResult.lsp_client 不为 None
        - LSPClient.start() 被调用
        - phases_executed 包含 "run"
        """
        # TODO: 实现
        # 1. patch get_feature_flags 返回 lsp_tools=True
        # 2. patch LSPClient.start 返回 True
        # 3. 调用 Bootstrap().run(tmp_path)
        # 4. 断言 result.lsp_client is not None
        # 5. 断言 "run" in result.phases_executed
        pass


# ── 2. engine 工具注册 ────────────────────────────────────────

class TestLSPToolsRegistration:
    """验证 engine.__init__ 注册 5 个 LSP 工具"""

    def test_lsp_tools_registered_in_engine(self, mock_lsp_client):
        """Test 2: lsp_client 注入后，_tool_registry 包含 5 个 LSP 工具

        断言要点：
        - _tool_registry.get_all_schemas() 包含 lsp_diagnostics/lsp_definition/
          lsp_references/lsp_hover/lsp_completion
        - 5 个工具的 _client 指向传入的 lsp_client
        """
        # TODO: 实现
        # 1. 构造 mock llm, prompt_builder, skills, config
        # 2. engine = AgentEngine(..., lsp_client=mock_lsp_client)
        # 3. tool_names = [s["function"]["name"] for s in engine._tool_registry.get_all_schemas()]
        # 4. assert "lsp_diagnostics" in tool_names
        # 5. assert "lsp_definition" in tool_names
        # 6. assert "lsp_references" in tool_names
        # 7. assert "lsp_hover" in tool_names
        # 8. assert "lsp_completion" in tool_names
        # 9. assert engine._lsp_diagnostics_tool._client is mock_lsp_client
        pass

    def test_lsp_tools_registered_with_none_client(self):
        """Test 3: lsp_client=None 时，5 个工具仍注册（降级模式）

        断言要点：
        - 工具注册成功（schema 存在）
        - 工具 execute 返回 success=False
        """
        # TODO: 实现
        # 1. engine = AgentEngine(..., lsp_client=None)
        # 2. tool_names = [...]
        # 3. assert "lsp_diagnostics" in tool_names
        # 4. result = asyncio.run(engine._lsp_diagnostics_tool.execute({"file": "x.c"}, {}))
        # 5. assert result["success"] is False
        pass


# ── 3. 文件通知钩子 ──────────────────────────────────────────

class TestLSPFileNotifications:
    """验证 _execute_write_file / _execute_read_file 触发 LSP 通知"""

    @pytest.mark.asyncio
    async def test_write_file_triggers_did_change(self, mock_lsp_client, tmp_path):
        """Test 4: 写入 .c 文件后，lsp_client.did_change 被调用

        断言要点：
        - did_change 至少被调用一次
        - 参数包含正确 path 和 content
        """
        # TODO: 实现
        # 1. engine = AgentEngine(..., lsp_client=mock_lsp_client)
        # 2. 写文件到 tmp_path/src/main.c
        # 3. async for event in engine._execute_write_file({"path": "src/main.c", "content": "int main(){}"}):
        #        pass
        # 4. await asyncio.sleep(0.1)  # 等 fire-and-forget 任务
        # 5. mock_lsp_client.did_change.assert_called()
        pass

    @pytest.mark.asyncio
    async def test_read_file_triggers_did_open(self, mock_lsp_client, tmp_path):
        """Test 5: 读取 .c 文件后，lsp_client.did_open 被调用

        断言要点：
        - did_open 至少被调用一次
        - 参数包含正确 path 和 content
        """
        # TODO: 实现
        # 1. engine = AgentEngine(..., lsp_client=mock_lsp_client)
        # 2. 在 tmp_path/src/main.c 写入 "int main(){}"
        # 3. async for event in engine._execute_read_file({"path": "src/main.c"}):
        #        pass
        # 4. await asyncio.sleep(0.1)
        # 5. mock_lsp_client.did_open.assert_called()
        pass

    @pytest.mark.asyncio
    async def test_write_non_c_file_no_notification(self, mock_lsp_client, tmp_path):
        """Test 6: 写入 .md 文件不触发 LSP 通知（扩展名过滤）

        断言要点：
        - did_change 未被调用
        """
        # TODO: 实现
        # 1. engine = AgentEngine(..., lsp_client=mock_lsp_client)
        # 2. async for event in engine._execute_write_file({"path": "README.md", "content": "# hi"}):
        #        pass
        # 3. await asyncio.sleep(0.1)
        # 4. mock_lsp_client.did_change.assert_not_called()
        pass


# ── 4. VerifyAgent LSP 集成 ──────────────────────────────────

class TestVerifyAgentLSPIntegration:
    """验证 VerifyAgent.verify() 显式调用 LSP 诊断"""

    @pytest.mark.asyncio
    async def test_verify_agent_calls_lsp_diagnostics(self, mock_lsp_client, tmp_path):
        """Test 7: verify() 并行调用 lsp_client.get_diagnostics 收集诊断

        断言要点：
        - get_diagnostics 被调用（且并行，调用次数 = 文件数）
        - 返回结果包含 lsp_diagnostics 字段
        """
        # TODO: 实现
        # 1. 构造 VerifyAgent(lsp_client=mock_lsp_client)
        # 2. 在 tmp_path/src/ 下创建 main.c, util.c
        # 3. mock_lsp_client.get_diagnostics = AsyncMock(return_value=[...])
        # 4. result = await verify_agent.verify(str(tmp_path / "src"))
        # 5. assert "lsp_diagnostics" in result
        # 6. assert mock_lsp_client.get_diagnostics.call_count >= 2
        pass

    @pytest.mark.asyncio
    async def test_verify_agent_no_lsp_degrades(self, disabled_lsp_client, tmp_path):
        """Test 8: LSP 未启动时，verify() 返回 "LSP 未启动"，不崩溃

        断言要点：
        - lsp_diagnostics 字段为 "LSP 未启动，跳过诊断"
        - get_diagnostics 未被调用
        """
        # TODO: 实现
        # 1. VerifyAgent(lsp_client=disabled_lsp_client)
        # 2. result = await verify_agent.verify(str(tmp_path / "src"))
        # 3. assert "LSP 未启动" in result["lsp_diagnostics"]
        pass


# ── 5. 退出清理 ──────────────────────────────────────────────

class TestLSPCleanup:
    """验证 main.py 退出时清理 LSP 客户端"""

    @pytest.mark.asyncio
    async def test_lsp_cleanup_on_exit(self, mock_lsp_client):
        """Test 9: _cleanup_lsp 调用 lsp_client.stop()

        断言要点：
        - stop() 被调用一次
        - 异常不传播（约束 C1）
        """
        # TODO: 实现
        # 1. from iron.cli.main import _cleanup_lsp
        # 2. _cleanup_lsp(mock_lsp_client)
        # 3. await asyncio.sleep(0.1)  # _safe_run_async 内部 asyncio.run
        # 4. mock_lsp_client.stop.assert_called_once()
        pass


# ── 6. 特性门控与降级 ────────────────────────────────────────

class TestFeatureGateAndDegradation:
    """验证特性门控和启动失败降级"""

    def test_lsp_disabled_when_feature_off(self, tmp_path):
        """Test 10: features.lsp_tools=False 时，bootstrap 不创建 LSP 客户端

        断言要点：
        - BootstrapResult.lsp_client is None
        - LSPClient.start 未被调用
        """
        # TODO: 实现
        # 1. patch get_feature_flags 返回 lsp_tools=False
        # 2. result = Bootstrap().run(tmp_path)
        # 3. assert result.lsp_client is None
        pass

    def test_lsp_startup_failure_degrades_gracefully(self, tmp_path):
        """Test 11: LSP start() 失败时，降级到 lsp_client=None，iron 不崩溃

        断言要点：
        - BootstrapResult.lsp_client is None
        - result.warnings 包含降级提示
        - result.success is True（不阻塞启动）
        """
        # TODO: 实现
        # 1. patch get_feature_flags 返回 lsp_tools=True
        # 2. patch LSPClient.start 返回 False
        # 3. result = Bootstrap().run(tmp_path)
        # 4. assert result.lsp_client is None
        # 5. assert result.success is True
        # 6. assert any("LSP" in w for w in result.warnings)
        pass


# ── 7. ExploreAgent 工具可用性 ───────────────────────────────

class TestExploreAgentLSPTools:
    """验证 ExploreAgent 能看到 LSP 工具 schema"""

    def test_explore_agent_has_lsp_tools(self, mock_lsp_client):
        """Test 12: ExploreAgent 的 _tools_schema 包含 LSP 工具

        断言要点：
        - _tools_schema 包含 lsp_definition/lsp_references/lsp_hover
        - 不包含 lsp_diagnostics/lsp_completion（不在 EXPLORE_TOOLS 中）
        """
        # TODO: 实现
        # 1. from iron.agent.engine import ExploreAgent
        # 2. agent = ExploreAgent(..., lsp_client=mock_lsp_client)
        # 3. tool_names = [s["function"]["name"] for s in agent._tools_schema]
        # 4. assert "lsp_definition" in tool_names
        # 5. assert "lsp_references" in tool_names
        # 6. assert "lsp_hover" in tool_names
        # 7. assert "lsp_diagnostics" not in tool_names  # 不在 EXPLORE_TOOLS
        pass
```

### 8.3 测试用例与实施步骤映射

| 测试用例 | 对应 Step | 验证集成点 | 优先级 |
|----------|----------|-----------|--------|
| Test 1: `test_lsp_client_lifecycle_in_bootstrap` | Step 3 | bootstrap LSP 初始化 | 高 |
| Test 2: `test_lsp_tools_registered_in_engine` | Step 4 | engine 工具注册 | 高 |
| Test 3: `test_lsp_tools_registered_with_none_client` | Step 4 | 降级模式工具注册 | 中 |
| Test 4: `test_write_file_triggers_did_change` | Step 5 | write_file 钩子 | 高 |
| Test 5: `test_read_file_triggers_did_open` | Step 5 | read_file 钩子 | 高 |
| Test 6: `test_write_non_c_file_no_notification` | Step 5 | 扩展名过滤 | 中 |
| Test 7: `test_verify_agent_calls_lsp_diagnostics` | Step 6 | VerifyAgent 并行诊断 | 高 |
| Test 8: `test_verify_agent_no_lsp_degrades` | Step 6 | VerifyAgent 降级 | 中 |
| Test 9: `test_lsp_cleanup_on_exit` | Step 7 | 退出清理 | 高 |
| Test 10: `test_lsp_disabled_when_feature_off` | Step 8 | 特性门控关闭 | 高 |
| Test 11: `test_lsp_startup_failure_degrades_gracefully` | Step 8 | 启动失败降级 | 高 |
| Test 12: `test_explore_agent_has_lsp_tools` | Step 9 | ExploreAgent 可见性 | 中 |

> 注：实际为 12 个测试用例（超出最低 10 个要求），覆盖更全面。

---

## 9. 手动验证场景

### 场景 1：有 clangd 的项目，启动 iron，LSP 自动启动

**前置条件**：
- 系统已安装 clangd（`which clangd` 有输出）
- 项目目录有 `build/compile_commands.json`
- `~/.iron/features.yml` 内容：`lsp_tools: true`

**操作步骤**：
1. `cd` 到项目目录
2. 运行 `iron`
3. 观察启动日志

**预期**：
- 阶段 3 进度显示"✓ 引擎初始化完成"
- 后台日志（`--verbose`）显示："LSP 客户端启动成功"
- 进入交互模式后，输入 `/explore 这个项目的 main 函数在哪`，ExploreAgent 调用 `lsp_definition` 工具
- 输入 `/verify src/`，VerifyAgent 输出包含"LSP 诊断"汇总

### 场景 2：无 clangd 的项目，降级到无 LSP 模式

**前置条件**：
- 系统未安装 clangd / ccls
- `~/.iron/features.yml` 内容：`lsp_tools: true`

**操作步骤**：
1. 运行 `iron`
2. 观察启动日志

**预期**：
- 启动警告："LSP 客户端启动失败，降级到无 LSP 模式"
- iron 正常进入交互模式
- 调用 `lsp_diagnostics` 工具返回 `{"success": False, "error": "LSP 服务器未启动"}`
- `/verify src/` 输出 "LSP 未启动，跳过诊断"

### 场景 3：LSP 启动失败，iron 正常运行

**前置条件**：
- 系统已安装 clangd
- 项目目录无 `compile_commands.json`（clangd 启动后可能崩溃）
- `~/.iron/features.yml` 内容：`lsp_tools: true`

**操作步骤**：
1. 运行 `iron`

**预期**：
- 启动警告："LSP 客户端启动失败，降级到无 LSP 模式"
- iron 正常进入交互模式（约束 C1 验证）
- 不影响 `/code`、`/build` 等核心功能

### 场景 4：write_file 后 LSP 收到 did_change

**前置条件**：
- 场景 1 的环境
- iron 已启动，LSP 客户端已初始化

**操作步骤**：
1. 输入 `/code 创建 src/hello.c，内容为 #include <stdio.h>\nint main(){printf("hi");return 0;}`
2. AI 调用 `write_file` 写入 `src/hello.c`
3. 观察 LSP 客户端日志

**预期**：
- `did_change` 通知被发送（约束 C2 验证）
- 后续调用 `lsp_diagnostics src/hello.c` 能返回最新诊断（如 `printf` 缺少 `\n` 的警告）

### 场景 5：退出 iron 时 LSP 客户端 stop() 被调用

**前置条件**：
- 场景 1 的环境
- iron 已启动，LSP 客户端已初始化

**操作步骤**：
1. 输入 `/quit`（或 Ctrl+C 双击）
2. 观察退出日志
3. 执行 `ps aux | grep clangd`（Linux）或 `tasklist | findstr clangd`（Windows）

**预期**：
- 退出日志无"LSP 清理失败"
- 系统进程列表无残留 clangd 进程（约束 C1 验证：清理失败不阻塞，但正常情况应清理成功）

---

## 10. 附录

### 10.1 涉及文件清单

| 文件 | 改动类型 | Step | 改动量（行） |
|------|---------|------|-------------|
| `tests/test_lsp_integration.py` | 新建 | 2 | ~250 |
| `iron/cli/bootstrap.py` | 修改 | 3 | ~40 |
| `iron/cli/main.py` | 修改 | 3, 4, 7 | ~30 |
| `iron/agent/engine.py` | 修改 | 4, 5, 6 | ~80 |
| `~/.iron/features.yml` | 配置 | - | 1 |

### 10.2 风险评估

| 风险 | 概率 | 影响 | 缓解措施 |
|------|------|------|---------|
| `asyncio.run` 在 bootstrap 中嵌套调用 | 中 | 启动崩溃 | bootstrap 是同步函数，`asyncio.run` 安全；若已有 event loop 则用 `_safe_run_async` |
| `fire-and-forget` 任务在 engine 销毁前未完成 | 低 | did_change 丢失 | LSP 通知是 best-effort，丢失不影响功能 |
| LSP 工具 schema 与 MCP 工具 schema 冲突 | 低 | schema 重复 | `_tool_registry.register` 按 name 去重，后注册覆盖 |
| ExploreAgent 的 `list_files/grep/glob` 工具不存在 | 中 | ExploreAgent 部分工具不可用 | 预存问题（非本 Track 引入），记录在 3.6 节 |
| clangd 启动慢导致 bootstrap 超时 | 低 | 启动卡顿 | `LSPClient.start` 无超时（line 143），但 `initialize` 有 30s 超时（line 83） |

### 10.3 与现有测试的关系

| 现有测试 | 本 Track 影响 | 验证方式 |
|---------|-------------|---------|
| `tests/test_lsp.py`（55 个测试） | 无影响（仅测 client + tools 单元） | Step 10 全量回归 |
| `tests/test_bootstrap.py` | 需验证不回归（bootstrap 改造） | Step 3 验证 |
| `tests/test_engine.py` | 需验证不回归（engine `__init__` 改造） | Step 4 验证 |
| `tests/test_main.py`（若存在） | 需验证不回归（main 退出清理改造） | Step 7 验证 |

---

**文档版本**：v1.0
**创建日期**：2026-06-28
**最后更新**：2026-06-28
**作者**：Track 4 规划专家
**审核状态**：待审核
