# Track 2 · main.py run_interactive() 拆分子计划

> **执行者须知**：本计划基于对 `iron/cli/main.py` 实际源码的逐行阅读编写。任务背景中提到的部分行号与常量名存在已知文档错误，已在 §2.3 与 §2.4 中明确纠正，**以本文件所述行号为准**。

---

## 1. 目标与约束

### 1.1 目标
- 将 `run_interactive()` 从当前 **270 行**（line 388-658）压缩至 **≤ 80 行**
- 每个提取出的子函数 **≤ 60 行**
- 保持外部行为 100% 等价：斜杠命令路由、副作用、退出码、会话保存顺序全部不变
- 提升可测试性：子函数可独立单测（mock 组件构造）

### 1.2 硬约束（不可违反）
| # | 约束 | 说明 |
|---|------|------|
| C1 | 斜杠命令执行顺序与副作用不变 | file → build → session → system 四路串行 `if/elif`，顺序不可调整 |
| C2 | `last_engine` 状态不可丢失 | 必须通过返回值或 `cmd_ctx` 双向同步；`_run_agent` 后赋值、`/undo` 前读取、退出清理前判空，三处时序不可变 |
| C3 | `__UNDO__`（双击 Esc）撤销逻辑保留 | `user_input == "__UNDO__"` 分支必须在主循环内、斜杠命令分发之前 |
| C4 | `while True` 的 break 条件不变 | SIGTERM / 双击 Ctrl+C / EOFError / `_OPTION_QUIT` / `cmd_ctx["should_quit"]` 五条退出路径全部保留 |
| C5 | 主循环退出后资源清理顺序不变 | session.save → db.close → MCP disconnect → llm.aclose → shutdown_asyncgens → 取消 pending tasks → loop.close → set_event_loop(None) |
| C6 | 组件初始化顺序不变 | PromptBuilder → Skills → LLM → SQLite → session → completer；LLM 失败回退 EchoBackend 必须保留 |
| C7 | 事件循环单例贯穿整个会话 | `loop = asyncio.new_event_loop()` 在主循环之前创建，`loop.close()` 在最后；不可改为每次输入 `asyncio.run` |

### 1.3 软约束（建议遵守）
- 优先使用 `NamedTuple` 而非 `dataclass`（不可变、零运行时开销、IDE 友好）
- 子函数不引入新的全局状态，全部通过参数/返回值传递
- 不改变 `cmd_ctx` 字典的 key 集合（已有的 handler 依赖这些 key）

---

## 2. 现状分析

### 2.1 函数定位（关键纠正）
> **⚠ 文档错误纠正**：任务背景称 `run_interactive()` 位于 `main.py` line 374-641（267 行），并称其在 ui.py。
>
> **实际情况**（基于 `d:\嵌入式-Agent\iron\cli\main.py` 真实源码）：
> - `run_interactive()` **位于 `iron/cli/main.py`**（不在 `ui.py`）
> - 起止行号：**line 388 - line 658**，共 **270 行**
> - 下一处定义是 line 661 的 `class _ThinkingSpinner`，可证函数在 658 结束（`asyncio.set_event_loop(None)`）

### 2.2 嵌套层级分析
| 层级 | 范围 | 内容 |
|------|------|------|
| L0 | 388-658 | `def run_interactive(config, project_root):` 函数体 |
| L1 | 394-395 | 事件循环创建 |
| L1 | 397-407 | SIGTERM 信号处理注册（内含 L2 闭包 `_sigterm_handler`） |
| L1 | 410-485 | 组件初始化序列（含 L2 try/except） |
| L1 | 492-514 | 主循环前置提示 + 命令分发模块延迟导入 + `cmd_ctx` 构造 |
| L1 | 516-612 | `while True:` 主循环 |
| L2 | 518-520 | SIGTERM 检查 break |
| L2 | 521-532 | try/except 获取用户输入（KeyboardInterrupt/EOFError） |
| L2 | 535-540 | `__UNDO__` 撤销分支 |
| L2 | 542-543 | 空输入跳过 |
| L2 | 547-561 | 数字选择映射（内含 L3 if/elif） |
| L2 | 563-603 | 斜杠命令分发（内含 L3 if/elif 四路） |
| L2 | 605-612 | 普通输入 → `_run_agent` |
| L1 | 614-658 | 退出清理（session.save + db.close + MCP/llm/loop 清理） |

**最大嵌套深度 = 3**（`while True` → `if text.isdigit()` → `if selected_option == _OPTION_COMPILE`）

### 2.3 已存在的辅助函数清单（真实行号）
| 函数 | 行号 | 签名 | 用途 |
|------|------|------|------|
| `_clear_screen_full()` | 48-62 | `()` | 彻底清屏（含滚动缓冲区），供 `/clear` 调用 |
| `_safe_run_async(coro, fail_msg)` | 120-142 | `(coro, fail_msg="异步操作失败")` | 兼容已有 event loop 的协程执行器 |
| `_cleanup_engine_mcp(last_engine)` | **145-155** | `(last_engine)` | 清理旧 engine 的 MCP 子进程 |
| `_inject_cli_event_to_session(...)` | **158-176** | `(session, event_type, summary, details)` | CLI 事件注入 session |
| `_count_output_tokens(text)` | 179-191 | `(text) -> int` | 输出 token 计数 |
| `_run_agent(...)` | **844-1018** | `async (console, llm, prompt_builder, skills, config, session, user_input, last_options=None, permission_callback=None, prior_conversation=None, engine_class=None) -> AgentEngine` | 运行 Agent 处理用户输入 |
| `_handle_event(...)` | **1019-1241** | `(console, event, session, ...)` | 处理 AgentEvent |
| `_do_undo(...)` | **1242-1259** | `(console, engine)` | 执行撤销 |

> **注**：任务背景给出的辅助函数行号（131/144/825/1000/1223）均有偏差，本表为真实行号。`_run_agent` 实际是 `async def`，签名比背景描述多 3 个参数。

### 2.4 关于 `CLEAR_AFTER_EXEC` 的纠正
> **⚠ 文档错误纠正**：任务背景要求"清屏逻辑（CLEAR_AFTER_EXEC）必须保留"。
>
> **实际情况**：代码库中**不存在** `CLEAR_AFTER_EXEC` 常量（已全局搜索确认）。
> 真正的清屏逻辑位于 `iron/cli/commands/session_cmds.py` line 29-32 的 `/clear` handler 内：
> ```python
> if cmd == "/clear":
>     from iron.cli.main import _clear_screen_full
>     _clear_screen_full()
> ```
> 因此本 Track 的"清屏逻辑保留"约束重新表述为：**`/clear` 命令通过 `_clear_screen_full()` 实现的彻底清屏行为不可改变**。由于该逻辑在 `session_cmds.py`（不在 `main.py`），本 Track 的拆分不会触碰它，约束自动满足。

### 2.5 待提取的代码块清单
| 块编号 | 行号 | 内容 | 目标子函数 |
|--------|------|------|------------|
| B1 | 410 | `prompt_builder = PromptBuilder(...)` | `_init_session_components` |
| B2 | 413-414 | `rules_count / total_rules` 计算 | `_init_session_components`（返回 total_rules） |
| B3 | 426-429 | Skills 加载 | `_init_session_components` |
| B4 | 432-438 | LLM 后端创建 + Echo 回退 | `_init_session_components` |
| B5 | 441-471 | Echo 警告 + API Key 显示 + 多厂商显示 | `_show_startup_info` |
| B6 | 415-423 | `ui.show_welcome(...)` 欢迎界面 | `_show_startup_info` |
| B7 | 473-481 | SQLite 初始化 + 降级 | `_init_session_components` |
| B8 | 484-485 | session + completer 创建 | `_init_session_components` |
| B9 | 547-561 | 数字选择映射 | `_handle_numeric_input` |
| B10 | 563-603 | 斜杠命令 4 路分发 + 状态同步 | `_dispatch_slash_command` |
| B11 | 614-658 | 退出清理（session/db/MCP/loop） | `_cleanup_on_exit` |

### 2.6 `cmd_ctx` 字典 key 清单（line 502-514）
```
console, config, project_root, llm, prompt_builder, skills,
last_engine, session, loop, total_rules, should_quit
```
拆分后 `cmd_ctx` 构造逻辑保留在 `run_interactive` 主体内（或下沉到 `_init_session_components` 返回的 bundle 之外），key 集合不变。

---

## 3. 拆分方案

### 3.1 `ComponentBundle` 数据类定义

**位置**：`iron/cli/main.py`，建议放在 `run_interactive` 定义之前（line 386 附近，`# ── 交互式会话 ──` 注释下方）。

**类型选择**：`NamedTuple`（不可变、轻量、与现有 `typing` 风格一致；`dataclass` 会引入额外 import 且可变性带来副作用风险）。

```python
from typing import NamedTuple, Optional, Any

class ComponentBundle(NamedTuple):
    """run_interactive 初始化阶段产出的组件集合。

    所有字段在初始化后不再变更（last_engine / last_options 等可变状态
    不放入 bundle，由主循环局部变量承载）。
    """
    prompt_builder: PromptBuilder
    skills: SkillRegistry
    llm: Any                      # Backend 实例（可能为 EchoBackend）
    db: Optional[Any]             # Database 实例，降级时为 None
    session: ConversationSession
    completer: Any                # ui.CommandCompleter 实例
    total_rules: int
```

**字段数**：7
**设计说明**：
- `llm` 用 `Any` 而非具体类型，避免 main.py 顶部增加 `Backend` 类型导入（现有代码也未导入该类型）
- `db` 用 `Optional[Any]`，因 SQLite 初始化失败时降级为 `None`
- `completer` 用 `Any`，因 `ui.CommandCompleter` 未在 main.py 顶层导入
- `loop` **不放入** bundle：事件循环在组件初始化之前创建（line 394），属于主循环基础设施，由 `run_interactive` 直接持有
- `last_engine` / `last_options` **不放入** bundle：它们是主循环的可变状态，每轮迭代都会变更，放入不可变 bundle 会误导读者

---

### 3.2 `_init_session_components(config, project_root) -> ComponentBundle`

- **来源行号**：410, 413-414, 426-429, 432-438, 473-481, 484-485
- **职责**：创建 PromptBuilder / Skills / LLM / SQLite / session / completer，计算 total_rules
- **不包含**：欢迎界面与 API Key 显示（下沉到 `_show_startup_info`）、事件循环创建、SIGTERM 注册

**签名**：
```python
def _init_session_components(config: IronConfig, project_root: Path) -> ComponentBundle:
```

**提取内容（伪代码）**：
```python
def _init_session_components(config, project_root):
    prompt_builder = PromptBuilder(project_root, config.project.mcu)
    rules_count = prompt_builder.count_active_rules()
    total_rules = sum(rules_count)

    skills = SkillRegistry()
    user_skills_dir = project_root / ".iron" / "skills"
    if user_skills_dir.exists():
        skills.load_from_dir(user_skills_dir)

    try:
        llm = create_backend(config.llm.backend, config)
    except (ValueError, ImportError, TypeError) as e:
        console.print(f"\n  {Symbols.WARN} LLM 后端初始化失败: {e}", style="yellow")
        console.print(f"  使用 Echo 模式（仅返回占位代码）\n", style="dim")
        from iron.llm.backend import EchoBackend
        llm = EchoBackend()

    db = None
    try:
        from iron.core.db import Database
        db = Database()
        db.connect()
    except Exception as e:
        console.print(f"  {Symbols.WARN} SQLite 持久化初始化失败，使用 JSON-only 模式: {e}", style="dim yellow")
        db = None

    session = ConversationSession(mcu=config.project.mcu, project_dir=str(project_root))
    completer = ui.CommandCompleter()

    return ComponentBundle(
        prompt_builder=prompt_builder,
        skills=skills,
        llm=llm,
        db=db,
        session=session,
        completer=completer,
        total_rules=total_rules,
    )
```

**数据流**：`config + project_root` → `ComponentBundle`
**风险点**：
- LLM 初始化失败时的两行 `console.print` 留在 init 内（与 EchoBackend 回退强耦合，抽出会破坏内聚性）
- SQLite 降级 print 同理保留
- `console` 使用模块级全局（与原代码一致，不引入参数传递）
- 依赖顺序：PromptBuilder 无依赖 → Skills 无依赖 → LLM 依赖 `config` → SQLite 无依赖 → session 依赖 `config.project.mcu` + `project_root`

---

### 3.3 `_show_startup_info(config, llm, total_rules, project_root)`

- **来源行号**：415-423（welcome）+ 441-471（Echo 警告 + API Key + 多厂商）
- **职责**：纯显示，无状态变更
- **副作用**：仅 `console.print` / `ui.show_welcome`
- **风险点**：无（纯输出函数）

**签名**：
```python
def _show_startup_info(config: IronConfig, llm, total_rules: int, project_root: Path) -> None:
```

**提取内容（伪代码）**：
```python
def _show_startup_info(config, llm, total_rules, project_root):
    ui.show_welcome(
        console,
        version=__version__,
        mcu=config.project.mcu.upper(),
        model=f"{config.llm.backend}/{config.llm.model}",
        project_dir=str(project_root),
        rules_count=total_rules,
        build_system=config.project.build_system,
    )

    if config.llm.backend == "echo":
        console.print(f"  {Symbols.WARN} [yellow]当前使用 Echo 模式，AI 响应为占位代码，仅用于测试。"
                      f"生产环境请在 iron.yml 中配置真实 LLM 后端（openai/anthropic/ollama）。[/yellow]")
        return

    # API key 前 4 后 4 显示
    _key = config.llm.api_key
    if _key and len(_key) > 12:
        _key_display = f"{_key[:4]}...{_key[-4:]}"
    elif _key:
        _key_display = f"{_key[:4]}***"
    else:
        _key_display = "[red]未设置[/red]"

    _active_name = config.active_provider or (config.providers[0].name if config.providers else "")
    _provider_count = len(config.providers)
    if _provider_count > 1 and _active_name:
        console.print(f"  {Symbols.INFO} 厂商: [cyan]{_active_name}[/cyan] "
                      f"[dim](共 {_provider_count} 个，用 /model 切换)[/dim]", style="dim")
    console.print(f"  {Symbols.INFO} 后端: [cyan]{config.llm.backend}[/cyan]  "
                  f"模型: [cyan]{config.llm.model}[/cyan]  "
                  f"API Key: [dim]{_key_display}[/dim]", style="dim")

    if not _key:
        _active_provider = config.get_active_provider() if config.providers else None
        _env_var = _active_provider.env_var_name if _active_provider else "IRON_API_KEY"
        console.print(f"  [yellow]⚠ API Key 未设置，请用以下任一方式配置后重启终端：[/yellow]")
        console.print(f"  [dim]1. PowerShell 永久：[/dim] [cyan][Environment]::SetEnvironmentVariable('{_env_var}','你的key','User')[/cyan]")
        console.print(f"  [dim]2. CMD 临时：[/dim]      [cyan]set {_env_var}=你的key[/cyan]")
        if _env_var != "IRON_API_KEY":
            console.print(f"  [dim]3. 第一个厂商也兼容：[/dim] [cyan]IRON_API_KEY[/cyan] [dim]或[/dim] [cyan]OPENAI_API_KEY[/cyan]")
```

**注意**：原代码 `echo` 分支后**不 return**，会继续走到 `_key` 显示。但 `echo` 模式下 `_key` 通常为空，会再打印"未设置"提示。为严格保持行为等价，**不提前 return**，而是用 `if/else` 包裹：echo 分支只打印警告，else 分支走 API Key 显示。上方伪代码已用 `return` 简化，实施时需对照原代码确认 echo 分支后是否真的跳过 `_key` 显示——**经复核原代码（line 441-471），echo 分支无 return，会继续执行 `_key` 显示逻辑**。因此实施时**不可加 return**，应改为：

```python
    if config.llm.backend == "echo":
        console.print(...)  # echo 警告
    # 注意：无 else，_key 显示对 echo 与非 echo 均执行（保持原行为）
    _key = config.llm.api_key
    ...
```

---

### 3.4 `_handle_numeric_input(text, last_options) -> tuple[str | None, bool]`

- **来源行号**：547-561
- **职责**：数字选择映射，返回 `(映射后的 text, should_quit)`
- **返回值**：
  - `(None, False)` — text 不是数字或越界，调用方应继续原逻辑
  - `(mapped_text, False)` — 映射成功，调用方用 mapped_text 继续
  - `(None, True)` — 选中 `_OPTION_QUIT`，调用方应 break

**签名**：
```python
def _handle_numeric_input(text: str, last_options: list) -> tuple[str | None, bool]:
```

**提取内容**：
```python
def _handle_numeric_input(text, last_options):
    """数字选择 → 映射为选项文本。

    返回 (mapped_text, should_quit)：
    - 非数字 / 越界：(None, False)，调用方按原 text 处理
    - 映射成功：(mapped_text_or_None_when_quit, should_quit)
      - _OPTION_COMPILE → ("/build", False)
      - _OPTION_QUIT    → (None, True)
      - 其他            → (selected_option, False)
    """
    if not (text.isdigit() and last_options):
        return None, False
    idx = int(text) - 1
    if not (0 <= idx < len(last_options)):
        return None, False
    selected_option = last_options[idx]
    console.print(f"  → {selected_option}", style="dim cyan")
    if selected_option == _OPTION_COMPILE:
        return "/build", False
    if selected_option == _OPTION_QUIT:
        return None, True
    return selected_option, False
```

**风险点**：
- `console.print(f"  → {selected_option}")` 副作用必须保留（用户视觉反馈）
- 越界时返回 `(None, False)` 而非继续走原 text——**等价性核对**：原代码 `if text.isdigit() and last_options:` 为 False 时整个 if 块跳过，text 不变；越界时原代码 `if 0 <= idx < len(last_options):` 为 False，if 块跳过，text 不变。两者均等价于"用原 text 继续"。本函数返回 `None` 表示"未映射，用原 text"，调用方判断 `if mapped is not None: text = mapped`。

---

### 3.5 `_dispatch_slash_command(text, cmd_ctx, last_options) -> SlashResult`

- **来源行号**：563-603
- **职责**：斜杠命令 4 路分发 + 状态双向同步
- **返回值**：`SlashResult(should_quit: bool, last_engine, last_options)` —— 见 §3.6

**签名**：
```python
def _dispatch_slash_command(text: str, cmd_ctx: dict, last_options: list) -> "SlashResult":
```

**提取内容**：
```python
def _dispatch_slash_command(text, cmd_ctx, last_options):
    cmd = text.split()[0].lower()
    args = text[len(cmd):].strip()

    _is_non_chat = cmd in NON_CHAT_COMMANDS
    session = cmd_ctx["session"]
    if not _is_non_chat:
        session.add_message("user", text)

    # 同步本地状态到 ctx
    cmd_ctx["last_engine"] = cmd_ctx.get("last_engine")  # 由调用方预先写入
    # （调用方在调用前已把 last_engine/session/llm/config 写入 cmd_ctx）

    if handle_file_commands(cmd, args, cmd_ctx):
        pass
    elif handle_build_commands(cmd, args, cmd_ctx):
        pass
    elif handle_session_commands(cmd, args, cmd_ctx):
        pass
    elif handle_system_commands(cmd, args, cmd_ctx):
        pass
    else:
        console.print(f"  未知命令: {cmd}，输入 /help 查看可用命令", style="yellow")

    should_quit = cmd_ctx["should_quit"]
    if should_quit:
        console.print(f"\n  再见! {Symbols.DONE}\n")
    return SlashResult(
        should_quit=should_quit,
        last_engine=cmd_ctx["last_engine"],
        last_options=last_options,  # 斜杠路径不修改 last_options
    )
```

**调用方使用方式**：
```python
result = _dispatch_slash_command(text, cmd_ctx, last_options)
last_engine = result.last_engine
session = cmd_ctx["session"]      # session/llm/config 仍从 ctx 同步
llm = cmd_ctx["llm"]
config = cmd_ctx["config"]
if result.should_quit:
    break
continue
```

**风险点**：
- **状态同步方向**：调用前必须把 `last_engine` 写入 `cmd_ctx`（原代码 line 577），调用后从 `cmd_ctx` 读回 `last_engine/session/llm/config`（原代码 line 594-597）。本设计把 `last_engine` 纳入返回值以显式表达，但 `session/llm/config` 仍通过 `cmd_ctx` 回传——因为 handler 可能替换 session/llm/config 实例（如 `/model` 切换 llm、`/resume` 替换 session），这些是 dict 内的可变替换，保持原机制最安全。
- **`/clear` 清屏**：由 `handle_session_commands` 内部调用 `_clear_screen_full()`，本函数不感知，约束 C1 自动满足。
- **`should_quit` 打印"再见"**：原代码在 line 600-601 打印后 break。本函数把"再见"打印移入函数内（仅当 should_quit=True），确保 break 前打印顺序不变。

---

### 3.6 `SlashResult` 数据类定义

```python
class SlashResult(NamedTuple):
    """_dispatch_slash_command 的返回值。"""
    should_quit: bool
    last_engine: Optional[AgentEngine]
    last_options: list
```

**字段数**：3
**说明**：`last_options` 在斜杠路径中实际不变，纳入返回值仅为统一接口契约，便于未来扩展（如 `/compact` 后清空选项）。

---

### 3.7 `_cleanup_on_exit(session, db, last_engine, llm, loop)`

- **来源行号**：614-658
- **职责**：主循环退出后的资源清理
- **副作用**：session.save、db.close、MCP disconnect、llm.aclose、loop 关闭

**签名**：
```python
def _cleanup_on_exit(session, db, last_engine, llm, loop) -> None:
```

**提取内容**：直接搬运 line 614-658，不改一个字符。完整代码见 §4 骨架中的实现。

**风险点**：
- **顺序敏感**：必须严格按 `session.save → db.close → MCP disconnect → llm.aclose → shutdown_asyncgens → cancel pending → loop.close → set_event_loop(None)` 顺序
- `last_engine._mcp_client` 用 `getattr(..., None)` 判空，避免 AttributeError
- 所有清理步骤均 try/except 包裹，单步失败不阻断后续清理

---

## 4. 重构后的 run_interactive() 骨架

> 目标行数 ≤ 80。下方骨架（含注释）约 75 行。

```python
def run_interactive(config: IronConfig, project_root: Path):
    """启动交互式会话"""
    # 事件循环单例贯穿整个会话（避免 httpx.AsyncClient 跨 loop 崩溃）
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # SIGTERM 信号处理（Unix only）
    _sigterm_received = _install_sigterm_handler()

    # 初始化组件 + 显示启动信息
    bundle = _init_session_components(config, project_root)
    _show_startup_info(config, bundle.llm, bundle.total_rules, project_root)
    console.print(f"  输入需求开始编码，或输入 [bold cyan]/help[/bold cyan] 查看命令")
    console.print()

    # 延迟导入命令分组（避免循环依赖）
    from iron.cli.commands import (
        handle_file_commands, handle_build_commands,
        handle_session_commands, handle_system_commands,
    )

    # 主循环状态
    last_engine: AgentEngine | None = None
    last_options: list[str] = []

    cmd_ctx = {
        "console": console, "config": config, "project_root": project_root,
        "llm": bundle.llm, "prompt_builder": bundle.prompt_builder,
        "skills": bundle.skills, "last_engine": last_engine,
        "session": bundle.session, "loop": loop,
        "total_rules": bundle.total_rules, "should_quit": False,
    }

    while True:
        if _sigterm_received["flag"]:
            console.print(f"\n  {Symbols.WARN} 收到退出信号，正在保存会话...", style="yellow")
            break
        try:
            user_input = ui.get_user_input(console, bundle.completer)
        except KeyboardInterrupt:
            if _sigterm_received["flag"]:
                break
            _sigterm_received["flag"] = True
            console.print(f"\n  {Symbols.WARN} 再按一次 Ctrl+C 退出（会话将保存），或继续输入", style="dim yellow")
            continue
        except EOFError:
            console.print(f"\n  再见! {Symbols.DONE}\n")
            break

        if user_input == "__UNDO__":
            if last_engine is not None:
                _do_undo(console, last_engine)
            else:
                console.print(f"  {Symbols.WARN} 没有可撤销的操作", style="dim yellow")
            continue

        if not user_input.strip():
            continue
        text = user_input.strip()

        # 数字选择映射
        mapped, quit_flag = _handle_numeric_input(text, last_options)
        if quit_flag:
            console.print(f"\n  再见! {Symbols.DONE}\n")
            break
        if mapped is not None:
            text = mapped

        # 斜杠命令分发
        if text.startswith("/"):
            cmd_ctx["last_engine"] = last_engine
            cmd_ctx["session"] = cmd_ctx["session"]
            cmd_ctx["llm"] = cmd_ctx["llm"]
            cmd_ctx["config"] = config
            result = _dispatch_slash_command(text, cmd_ctx, last_options)
            last_engine = result.last_engine
            config = cmd_ctx["config"]
            if result.should_quit:
                break
            continue

        # 普通输入 → Agent
        cmd_ctx["session"].add_message("user", text)
        console.print()
        _prior_conv = last_engine.conversation if last_engine else None
        _cleanup_engine_mcp(last_engine)
        last_engine = loop.run_until_complete(_run_agent(
            console, cmd_ctx["llm"], bundle.prompt_builder, bundle.skills,
            config, cmd_ctx["session"], text, last_options,
            prior_conversation=_prior_conv,
        ))

    # 退出清理
    _cleanup_on_exit(cmd_ctx["session"], bundle.db, last_engine, cmd_ctx["llm"], loop)
```

**行数核算**：约 75 行（含空行与注释），满足 ≤ 80 行目标。

**等价性说明**：
- `session` 不再作为局部变量，改为 `cmd_ctx["session"]` 访问（原代码 line 595 `session = cmd_ctx["session"]` 同步回局部变量；重构后直接读 dict，行为等价）
- `llm` 同理改为 `cmd_ctx["llm"]`
- `last_options` 仍为局部变量（数字选择 + `_run_agent` 共享）

---

## 5. 实施步骤（按顺序执行，每步带验证）

> **前置**：所有命令在 `d:\嵌入式-Agent\` 目录下执行。PowerShell 环境。

### Step 1: 创建 git tag 与工作分支

**操作**：
```powershell
cd d:\嵌入式-Agent
git status                       # 确认工作区干净
git tag pre-main-split           # 回滚锚点
git checkout -b refactor/track-2-main-split
```

**验证**：
```powershell
git tag --list "pre-main-split"  # 应输出 pre-main-split
git branch --show-current        # 应输出 refactor/track-2-main-split
```

**回滚策略**：`git checkout main && git branch -D refactor/track-2-main-split && git tag -d pre-main-split`

---

### Step 2: 定义 `ComponentBundle` 与 `SlashResult` 数据类

**文件**：`iron/cli/main.py`
**位置**：line 386（`# ── 交互式会话 ──` 注释下方，`def run_interactive` 之前）
**操作**：新增两个 NamedTuple 定义（见 §3.1 与 §3.6）

**验证**：
```powershell
python -c "from iron.cli.main import ComponentBundle, SlashResult; print(ComponentBundle._fields); print(SlashResult._fields)"
```
预期输出：
```
('prompt_builder', 'skills', 'llm', 'db', 'session', 'completer', 'total_rules')
('should_quit', 'last_engine', 'last_options')
```

**回滚**：`git checkout iron/cli/main.py`

---

### Step 3: 提取 `_init_session_components`

**文件**：`iron/cli/main.py`
**操作**：
1. 在 `ComponentBundle` 定义下方新增 `_init_session_components(config, project_root) -> ComponentBundle`（见 §3.2）
2. 将 `run_interactive` 内 line 410-485 的组件初始化代码替换为：
   ```python
   bundle = _init_session_components(config, project_root)
   ```
3. 后续引用 `prompt_builder` / `skills` / `llm` / `db` / `session` / `completer` / `total_rules` 的位置改为 `bundle.xxx`

**验证**：
```powershell
python -m pytest tests/test_cli_commands.py -v 2>&1 | Select-Object -Last 5
python -c "from iron.cli.main import _init_session_components; print('import ok')"
```
预期：测试全绿，import 无报错。

**提交**：
```powershell
git add iron/cli/main.py
git commit -m "refactor(main): extract _init_session_components"
```

**回滚**：`git reset --hard HEAD~1`

---

### Step 4: 提取 `_show_startup_info`

**文件**：`iron/cli/main.py`
**操作**：
1. 新增 `_show_startup_info(config, llm, total_rules, project_root) -> None`（见 §3.3，**注意 echo 分支不加 return**）
2. 将 `run_interactive` 内 line 415-423（welcome）+ 441-471（API Key 显示）替换为：
   ```python
   _show_startup_info(config, bundle.llm, bundle.total_rules, project_root)
   ```

**验证**：
```powershell
python -m pytest tests/test_cli_commands.py -v 2>&1 | Select-Object -Last 5
```
**手动验证**（重要）：
```powershell
echo "y" | python -m iron.cli --project . --backend echo
```
观察启动信息是否完整显示：版本、MCU、模型、规则数、Echo 警告。

**提交**：
```powershell
git add iron/cli/main.py
git commit -m "refactor(main): extract _show_startup_info"
```

**回滚**：`git reset --hard HEAD~1`

---

### Step 5: 提取 `_handle_numeric_input`

**文件**：`iron/cli/main.py`
**操作**：
1. 新增 `_handle_numeric_input(text, last_options) -> tuple[str | None, bool]`（见 §3.4）
2. 将 `run_interactive` 内 line 547-561 替换为：
   ```python
   mapped, quit_flag = _handle_numeric_input(text, last_options)
   if quit_flag:
       console.print(f"\n  再见! {Symbols.DONE}\n")
       break
   if mapped is not None:
       text = mapped
   ```

**验证**：
```powershell
python -m pytest tests/test_cli_commands.py -v 2>&1 | Select-Object -Last 5
python -c "from iron.cli.main import _handle_numeric_input; print(_handle_numeric_input('1', ['编译试试','退出']))"
```
预期：`('/build', False)`

**提交**：
```powershell
git add iron/cli/main.py
git commit -m "refactor(main): extract _handle_numeric_input"
```

**回滚**：`git reset --hard HEAD~1`

---

### Step 6: 提取 `_dispatch_slash_command`

**文件**：`iron/cli/main.py`
**操作**：
1. 新增 `_dispatch_slash_command(text, cmd_ctx, last_options) -> SlashResult`（见 §3.5）
2. 将 `run_interactive` 内 line 563-603 替换为调用代码（见 §4 骨架斜杠命令段）

**验证**：
```powershell
python -m pytest tests/test_cli_commands.py -v 2>&1 | Select-Object -Last 5
python -m pytest tests/ -v 2>&1 | Select-String "passed|failed" | Select-Object -Last 3
```
预期：`test_cli_commands.py` 全绿；总用例数 ≥ 738 passed。

**手动验证**：
```powershell
python -m iron.cli --project . --backend echo
# 在交互界面依次输入：/help /model /read <文件> /build /resume /clear /quit
# 确认每个命令正常响应，/clear 清屏，/quit 退出
```

**提交**：
```powershell
git add iron/cli/main.py
git commit -m "refactor(main): extract _dispatch_slash_command"
```

**回滚**：`git reset --hard HEAD~1`

---

### Step 7: 提取 `_cleanup_on_exit`

**文件**：`iron/cli/main.py`
**操作**：
1. 新增 `_cleanup_on_exit(session, db, last_engine, llm, loop) -> None`（见 §3.7，原样搬运 line 614-658）
2. 将 `run_interactive` 内 line 614-658 替换为：
   ```python
   _cleanup_on_exit(cmd_ctx["session"], bundle.db, last_engine, cmd_ctx["llm"], loop)
   ```

**验证**：
```powershell
python -m pytest tests/ -v 2>&1 | Select-String "passed|failed" | Select-Object -Last 3
```
预期：总数 ≥ 738 passed。

**手动验证**（退出清理）：
```powershell
python -m iron.cli --project . --backend echo
# 输入几条消息后，双击 Ctrl+C 退出
# 观察是否打印"收到退出信号，正在保存会话..."，且无 "Task was destroyed" 警告
```

**提交**：
```powershell
git add iron/cli/main.py
git commit -m "refactor(main): extract _cleanup_on_exit"
```

**回滚**：`git reset --hard HEAD~1`

---

### Step 8: 最终行数核验

**操作**：
```powershell
python -c "import ast,inspect; from iron.cli import main; src=inspect.getsource(main.run_interactive); print('run_interactive lines:', len(src.splitlines()))"
```
预期：`run_interactive lines: ≤ 80`

```powershell
# 检查每个子函数行数
python -c "import inspect; from iron.cli.main import _init_session_components,_show_startup_info,_handle_numeric_input,_dispatch_slash_command,_cleanup_on_exit; [print(f.__name__, len(inspect.getsource(f).splitlines())) for f in [_init_session_components,_show_startup_info,_handle_numeric_input,_dispatch_slash_command,_cleanup_on_exit]]"
```
预期：每个 ≤ 60 行。

**最终提交**：
```powershell
git tag track-2-complete
git log --oneline pre-main-split..HEAD   # 应有 6 个 commit
```

---

## 6. 验证清单

### 6.1 静态验证
- [ ] `run_interactive()` 行数 ≤ 80
- [ ] `_init_session_components` 行数 ≤ 60
- [ ] `_show_startup_info` 行数 ≤ 60
- [ ] `_handle_numeric_input` 行数 ≤ 60
- [ ] `_dispatch_slash_command` 行数 ≤ 60
- [ ] `_cleanup_on_exit` 行数 ≤ 60
- [ ] `ComponentBundle` 字段数 = 7
- [ ] `SlashResult` 字段数 = 3

### 6.2 自动化测试
- [ ] `python -m pytest tests/test_cli_commands.py -v` 全绿（711+ 用例）
- [ ] `python -m pytest tests/ -v` 总数 ≥ 738 passed
- [ ] 无新增 warning / deprecation
- [ ] `python -c "from iron.cli.main import run_interactive"` 无报错

### 6.3 手动功能测试（Echo 模式）
- [ ] 启动显示：版本 / 项目路径 / MCU / 模型 / 规则数 / API Key 前4后4
- [ ] `/help` 列出全部命令
- [ ] `/model` 切换模型菜单正常
- [ ] `/read <文件>` 读取文件内容
- [ ] `/build` 触发编译流程
- [ ] `/resume` 列出历史会话
- [ ] `/clear` 彻底清屏（含滚动缓冲区）
- [ ] `/quit` 退出并保存会话
- [ ] 双击 Esc → `__UNDO__` 撤销上次修改（无 engine 时提示"没有可撤销的操作"）
- [ ] 数字选择映射：AI 返回选项后输入数字，正确映射为选项文本
- [ ] 数字选择映射：输入 `_OPTION_QUIT` 对应数字，正常退出
- [ ] 双击 Ctrl+C 退出，打印"收到退出信号"并保存会话
- [ ] 退出后无 "Task was destroyed but it is pending" 警告

### 6.4 等价性回归
- [ ] `git diff pre-main-split..HEAD -- iron/cli/main.py` 仅在 main.py 有改动
- [ ] `iron/cli/commands/*.py` 无改动
- [ ] `iron/cli/ui.py` 无改动
- [ ] 行为对比：拆分前后对同一输入序列的输出文本完全一致（可用 script 录制对比）

---

## 7. 回滚策略

### 7.1 整体回滚
```powershell
cd d:\嵌入式-Agent
git checkout main
git branch -D refactor/track-2-main-split
git tag -d track-2-complete      # 如已打
# pre-main-split tag 保留以便对照
```

### 7.2 单步回滚
每个 Step 独立 commit，单步回滚不影响其他步骤：
```powershell
git revert <commit-hash>         # 推荐用 revert 而非 reset，保留历史
```

### 7.3 锚点
- `pre-main-split` tag —— 拆分前最后一个稳定状态
- `track-2-complete` tag —— 拆分完成状态
- 两 tag 之间共 6 个 commit，可逐个 cherry-pick 或 revert

---

## 8. 与其他 Track 的接口契约

### 8.1 与 Track 1（engine.py 重构）
- **冲突评估**：无冲突
- **原因**：Track 1 改 `iron/agent/engine.py`，本 Track 改 `iron/cli/main.py`，文件不重叠
- **接口点**：`_run_agent` 内部创建 `AgentEngine`，Track 1 若改 `AgentEngine.__init__` 签名，需同步更新 `_run_agent` 调用——但 `_run_agent` 不在本 Track 拆分范围内，本 Track 仅搬运 `_run_agent` 的调用语句，不改其内部
- **并行性**：可完全并行

### 8.2 与 Track 3（backend.py 重构）
- **冲突评估**：无冲突
- **原因**：Track 3 改 `iron/llm/backend.py`，本 Track 仅通过 `create_backend()` 与 `llm.aclose()` 接口交互
- **接口点**：`_init_session_components` 调用 `create_backend(config.llm.backend, config)`；`_cleanup_on_exit` 调用 `llm.aclose()`。只要 Track 3 保持这两个接口签名不变，本 Track 无需调整
- **并行性**：可完全并行

### 8.3 与 Track 1.2（LSP 集成）
- **影响点**：Track 1.2 计划在 `_cleanup_engine_mcp` 旁增加 `_cleanup_lsp`，在退出清理阶段调用
- **本 Track 应对**：`_cleanup_on_exit` 的参数列表预留 `last_engine`，Track 1.2 可在本函数内增加 `_cleanup_lsp(last_engine)` 调用，或扩展参数
- **建议**：Track 1.2 实施时，在 `_cleanup_on_exit` 的 MCP disconnect 之后、`llm.aclose` 之前插入 LSP 清理，保持顺序：`session.save → db.close → MCP disconnect → LSP cleanup → llm.aclose → ...`

### 8.4 与未来 `/compact` 清空 last_options 的扩展
- **当前**：`_dispatch_slash_command` 返回的 `SlashResult.last_options` 与传入的相同（未修改）
- **未来**：若 `/compact` 需清空选项，只需在 `handle_session_commands` 内或 `_dispatch_slash_command` 内将 `last_options.clear()`，由于返回的是同引用，调用方 `last_options` 自动同步
- **无需**修改本 Track 的接口契约

---

## 9. 附录：原始行号映射表

| 原始行号 | 内容 | 拆分后归属 |
|----------|------|------------|
| 388-393 | 函数签名 + docstring + 事件循环说明 | `run_interactive` 保留 |
| 394-395 | `loop = asyncio.new_event_loop()` | `run_interactive` 保留 |
| 397-407 | SIGTERM 注册 | `_install_sigterm_helper`（可选提取，或保留在主函数） |
| 410 | `prompt_builder = PromptBuilder(...)` | `_init_session_components` |
| 413-414 | `rules_count / total_rules` | `_init_session_components` |
| 415-423 | `ui.show_welcome(...)` | `_show_startup_info` |
| 426-429 | Skills 加载 | `_init_session_components` |
| 432-438 | LLM 创建 + 回退 | `_init_session_components` |
| 441-471 | Echo 警告 + API Key 显示 | `_show_startup_info` |
| 473-481 | SQLite 初始化 | `_init_session_components` |
| 484-485 | session + completer | `_init_session_components` |
| 488-489 | `last_engine / last_options` 初始化 | `run_interactive` 保留 |
| 492-493 | 主循环前置提示 | `run_interactive` 保留 |
| 496-499 | 命令分组延迟导入 | `run_interactive` 保留 |
| 502-514 | `cmd_ctx` 构造 | `run_interactive` 保留 |
| 516-612 | `while True` 主循环 | `run_interactive` 保留（内部调用子函数） |
| 518-520 | SIGTERM break | `run_interactive` 保留 |
| 521-532 | 用户输入获取 | `run_interactive` 保留 |
| 535-540 | `__UNDO__` 撤销 | `run_interactive` 保留 |
| 542-545 | 空输入跳过 + text 赋值 | `run_interactive` 保留 |
| 547-561 | 数字选择映射 | `_handle_numeric_input` |
| 563-603 | 斜杠命令分发 | `_dispatch_slash_command` |
| 605-612 | 普通输入 → `_run_agent` | `run_interactive` 保留 |
| 614-658 | 退出清理 | `_cleanup_on_exit` |

---

## 10. 风险登记册

| 风险 ID | 描述 | 概率 | 影响 | 缓解措施 |
|---------|------|------|------|----------|
| R1 | `_show_startup_info` 中 echo 分支误加 return 导致 API Key 提示丢失 | 中 | 中 | §3.3 已明确标注不可加 return；Step 4 手动验证启动信息完整性 |
| R2 | `cmd_ctx["session"]` 替换后局部变量 `session` 未同步 | 中 | 高 | 重构后取消局部变量 `session`，统一用 `cmd_ctx["session"]`；Step 6 手动测试 `/resume`（会替换 session） |
| R3 | `_cleanup_on_exit` 参数顺序写反导致 db/loop 错乱 | 低 | 高 | 参数命名清晰；Step 7 手动验证退出无警告 |
| R4 | `ComponentBundle` 未包含 `loop`，开发者误以为 loop 在 bundle 内 | 低 | 低 | §3.1 已明确说明 loop 不放入 bundle 的原因 |
| R5 | `_dispatch_slash_command` 返回 `last_options` 未被调用方使用，误导读者 | 低 | 低 | §3.6 已说明纳入原因（契约统一、未来扩展） |
| R6 | 测试用例数因环境差异未达 738 | 中 | 中 | Step 6/8 记录实际用例数，若 < 738 需排查是否有测试被误删 |
| R7 | Track 1.2 LSP 清理插入位置错误 | 低 | 中 | §8.3 已给出推荐插入位置 |

---

**文档版本**：v1.0
**基于源码版本**：`iron/cli/main.py` 当前 HEAD
**编写依据**：实际逐行阅读 line 1-658 + line 844-859 + line 1242-1259 + `iron/cli/commands/` 全模块
