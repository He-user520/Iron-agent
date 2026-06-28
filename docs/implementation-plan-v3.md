# Iron CLI 实施方案计划 v3

**基线版本：** v2.5.0+（A- 评级，738 passed）
**规划日期：** 2026-06-27
**规划依据：** [evaluation-v3.md](file:///d:/嵌入式-Agent/docs/evaluation-v3.md) + [gap-analysis.md](file:///d:/嵌入式-Agent/docs/gap-analysis.md)
**目标版本：** v2.6.0（短期）→ v2.8.0（中期）→ v3.0.0（长期）

---

## Phase 0 · 文档发现与允许 API 清单

### 0.1 已确认的现状（基于源码完整读取）

| 主题 | 关键事实 | 来源 |
|------|---------|------|
| engine.py process() | **802 行**（542-1343），工具分发循环 456 行 if/elif 链是最大热点 | [engine.py](file:///d:/嵌入式-Agent/iron/agent/engine.py#L542-L1343) |
| 类继承结构 | `BaseAgentEngine(ABC)` → `CoderAgentEngine` / `TaskAgentEngine` → `VerifyAgent` / `ExploreAgent` | [engine.py#L88-L2339](file:///d:/嵌入式-Agent/iron/agent/engine.py) |
| run_interactive() | **在 main.py line 374，不在 ui.py**；267 行，3 层嵌套 | [main.py#L374-L641](file:///d:/嵌入式-Agent/iron/cli/main.py) |
| LSP 集成 | **完全未集成到主循环** — Grep 全仓仅命中 `lsp_client.py` 和 `lsp_tools.py`；bootstrap/engine/main 均无 `LSPClient` 引用 | [lsp_client.py](file:///d:/嵌入式-Agent/iron/integrations/lsp_client.py) |
| LSP 工具注册 | 5 个工具类已定义（`LSPDiagnosticsTool` 等），`set_client()` 方法存在但调用方缺失 | [lsp_tools.py](file:///d:/嵌入式-Agent/iron/tools/lsp_tools.py) |
| Skills 机制 | `PromptSkill.execute()` 返回 `next_steps=[self._prompt]`，engine `_match_skills()` 调 `_build_prompt()` 注入 system prompt — **纯 prompt 注入式** | [registry.py#L45-L50](file:///d:/嵌入式-Agent/iron/skills/registry.py) |
| 4 层记忆 | 压缩(ContextCompactor) / MEMORY.md / checkpoint.md / tasks/progress.md | [memory.py#L1-L19](file:///d:/嵌入式-Agent/iron/agent/memory.py) |
| SQLite 三表 | sessions / messages / history；WAL 模式；`ON DELETE CASCADE`；6 个索引 | [001_initial.sql](file:///d:/嵌入式-Agent/iron/core/migrations/001_initial.sql) |
| 向量搜索 | **无任何基础设施** — `search_history()` 仅 `LIKE %query%`；无 embedding 列/向量表/相似度查询 | [db.py#L409-L414](file:///d:/嵌入式-Agent/iron/core/db.py) |
| test_lsp.py | **仅单元测试**，全 mock，4 个测试类 751 行；无端到端/主循环集成测试 | [test_lsp.py](file:///d:/嵌入式-Agent/tests/test_lsp.py) |

### 0.2 允许 API 清单（基于源码确认）

**LSP 客户端可用 API**（[lsp_client.py](file:///d:/嵌入式-Agent/iron/integrations/lsp_client.py)）：
- `LSPClient(config: LSPConfig)` — 构造函数
- `await client.start()` / `await client.stop()` — 生命周期
- `await client.initialize()` — 握手
- `client.did_open(path, content)` / `did_change(path, content)` / `did_close(path)` — 文件通知
- `await client.get_diagnostics(path)` / `get_definition(path, line, char)` / `get_references(...)` / `get_hover(...)` / `get_completion(...)` — 5 个查询
- `LSPClient.detect_server()` / `LSPClient.find_compile_commands(root)` — 静态检测
- `LSPConfig(enabled: bool, server_cmd: list[str], compile_commands_dir: str | None, project_root: str)`

**LSP 工具可用 API**（[lsp_tools.py](file:///d:/嵌入式-Agent/iron/tools/lsp_tools.py)）：
- 5 个工具类：`LSPDiagnosticsTool` / `LSPDefinitionTool` / `LSPReferencesTool` / `SPHoverTool` / `LSPCompletionTool`
- 每个工具有 `set_client(client)` 方法（line 34/102/160/220/295）支持延迟注入客户端

**Skills 可用 API**（[registry.py](file:///d:/嵌入式-Agent/iron/skills/registry.py)）：
- `PromptSkill(name, description, trigger_patterns, icon, prompt)` — 数据驱动构造
- `SkillRegistry.load_from_dir(directory)` — 加载 `.iron/skills/*.md`
- `SkillRegistry.match(user_input)` — 阈值 `MATCH_THRESHOLD = 0.5`
- `BaseSkill` 基类（`iron/skills/base.py`）含 `match_score()` 方法

**Database 可用 API**（[db.py](file:///d:/嵌入式-Agent/iron/core/db.py)）：
- `Database(db_path=None)` — 默认 `~/.iron/iron.db`
- `Database._migrate()` — 自动执行 `migrations/*.sql`（按文件名排序）
- `Database.transaction()` — 上下文管理器，自动 commit/rollback
- `Database.search_history(query, limit)` — **仅 LIKE 查询，需扩展**

**引擎可用 API**（[engine.py](file:///d:/嵌入式-Agent/iron/agent/engine.py)）：
- `BaseAgentEngine.__init__(llm, prompt_builder, skills, config, tools, event_bus)`
- `_get_allowed_tools() -> set[str] | None` — 抽象方法
- `_get_system_prompt_prefix() -> str` — 抽象方法
- `_emit_event(event_type, data)` — PubSub + AgentEvent
- `_match_skills(user_input)` — 返回 prompt 字符串
- `_check_permission_with_callback(...)` — 三级审批
- `_execute_write_file(args)` / `_execute_run_command(args)` / `_execute_read_file(args)` — 已拆分的工具执行器

### 0.3 反模式防护清单

| # | 反模式 | 检查方式 |
|---|--------|---------|
| 1 | 不要在 `process()` 中直接调用 `LSPClient` — 必须通过工具注册 | grep `LSPClient` in `engine.py` 应仅出现在 `__init__` |
| 2 | 不要为 Skills 引入新工具时绕过 `ToolRegistry.safe_execute` | grep `skill.run(` 应在 `safe_execute` 包装内 |
| 3 | 不要直接修改 `001_initial.sql` — 必须新增 `002_*.sql` 迁移 | `migrations/` 目录文件名递增 |
| 4 | 不要在 `run_interactive()` 中加业务逻辑 — 必须拆分到独立函数 | 函数行数 ≤ 100 行 |
| 5 | 不要破坏 `BaseAgentEngine(ABC)` 抽象边界 | 子类只覆盖 `_get_allowed_tools` / `_get_system_prompt_prefix` |
| 6 | 不要引入未经测试的 embedding 模型 — 必须有 fallback | LLM 不可用时降级到关键词搜索 |
| 7 | 不要在 `process()` 拆分时改变 `AgentEvent` yield 顺序 | 拆分后跑 `test_engine_integration.py` 全绿 |
| 8 | 不要在 LSP 集成时阻塞主循环 — 必须异步 | `did_change` 调用用 `asyncio.create_task` |

---

## Phase 1 · 短期（1-2 周，v2.6.0）

### 阶段 1.1 · engine.py process() 函数拆分

**目标：** 将 802 行的 `process()` 函数拆分为职责单一的子方法，降低圈复杂度，保持 `AgentEvent` yield 顺序不变。

**涉及文件：**
- [iron/agent/engine.py](file:///d:/嵌入式-Agent/iron/agent/engine.py) — 主修改
- [tests/test_engine.py](file:///d:/嵌入式-Agent/tests/test_engine.py) — 回归测试
- [tests/test_engine_integration.py](file:///d:/嵌入式-Agent/tests/test_engine_integration.py) — 端到端回归

**实施步骤：**

1. **提取思考阶段处理器** `_handle_thinking_phase(system, messages, step)`
   - 来源：engine.py line 623-712
   - 内容：流式生成 + fallback 到非流式 + chunk 累积
   - 返回：`(resp, thinking_text)` 元组

2. **提取工具调用权限过滤器** `_filter_tool_calls_by_permission(tool_calls)`
   - 来源：engine.py line 716-740
   - 内容：只读 Agent 工具过滤 + `tool_blocked` 事件发射
   - 返回：过滤后的 `tool_calls` 列表

3. **提取单个工具执行分发器** `_dispatch_tool_call(call, idx, tool_results, _pending_readonly)`
   - 来源：engine.py line 747-1203 的巨型 if/elif 链
   - **拆分为 7 个独立方法**（每个工具一个）：
     - `_handle_chat_tool(call, tool_results)` — line 825-841
     - `_handle_write_file_tool(call, tool_results)` — line 843-912（含 doom_loop/权限/敏感文件检查）
     - `_handle_edit_file_tool(call, tool_results)` — line 914-994（含撤销历史快照）
     - `_handle_run_command_tool(call, tool_results)` — line 996-1081（含编译命令重定向）
     - `_handle_read_file_tool(call, tool_results)` — line 1083-1098
     - `_handle_external_tool(call, tool_results, _pending_readonly)` — line 1100-1203（含只读并行/破坏性授权/PostHook）
   - `_dispatch_tool_call` 仅做 `if name == "chat": ... elif name == "write_file": ...` 路由

4. **提取后置处理器** `_handle_post_step(step, resp, tool_results, tool_calls)`
   - 来源：engine.py line 1214-1300
   - 内容：终止检查 + 任务完成 + 步数预警 + Stop Hooks 调用
   - 返回：`should_stop: bool`

5. **提取兜底处理器** `_handle_max_steps_exceeded()` 和 `_handle_summary(all_files_created, all_files_modified)`
   - 来源：engine.py line 1303-1342
   - 内容：步数上限兜底 + 总结事件 + checkpoint 保存

6. **重构后的 `process()` 函数骨架**（目标 ≤ 150 行）：
   ```python
   async def process(self, user_input: str):
       # 初始化（542-622）：system prompt + skill 匹配 + 消息构造 ~80 行
       ...
       for step in range(self.MAX_STEPS):
           resp, thinking = await self._handle_thinking_phase(system, messages, step)
           if not resp.tool_calls:
               yield from self._handle_no_tool_calls(resp, step)
               return
           tool_calls = self._filter_tool_calls_by_permission(resp.tool_calls)
           tool_results = []
           _pending_readonly = []
           try:
               for idx, call in enumerate(tool_calls):
                   yield from self._dispatch_tool_call(call, idx, tool_results, _pending_readonly)
           finally:
               if _pending_readonly:
                   yield from self._flush_readonly_tasks(_pending_readonly, tool_results)
           if await self._handle_post_step(step, resp, tool_results, tool_calls):
               break
       else:
           yield from self._handle_max_steps_exceeded()
       yield from self._handle_summary(all_files_created, all_files_modified)
   ```

**验证清单：**
- [ ] `process()` 函数行数 ≤ 150 行（用 `wc -l` 或 AST 分析）
- [ ] 每个 `_handle_*` 子方法行数 ≤ 100 行
- [ ] `pytest tests/test_engine.py -v` 全绿（336+ 用例）
- [ ] `pytest tests/test_engine_integration.py -v` 全绿（端到端不回归）
- [ ] `pytest tests/ -v` 总数 ≥ 738 passed（无回归）
- [ ] grep 确认 `process()` 内不再有 `if name == "..."` 工具分发逻辑

**反模式防护：**
- 不要改变 `AgentEvent` yield 的顺序和数量（拆分是纯重构，行为不变）
- 不要在子方法中重新连接 `tool_results` 列表（必须通过参数传递）
- 不要遗漏 `finally` 块中的孤儿任务取消逻辑（line 1208-1212）

---

### 阶段 1.2 · LSP 端到端集成

**目标：** 将 LSP 客户端接入主循环，实现"启动 → 工具注册 → 文件通知 → VerifyAgent/ExploreAgent 真实使用 → 优雅关闭"完整链路。

**涉及文件：**
- [iron/cli/bootstrap.py](file:///d:/嵌入式-Agent/iron/cli/bootstrap.py) — 启动管道阶段 3 注入 LSP 客户端
- [iron/agent/engine.py](file:///d:/嵌入式-Agent/iron/agent/engine.py) — `__init__` 接收 LSP 客户端 + 工具注册 + 文件通知钩子
- [iron/tools/__init__.py](file:///d:/嵌入式-Agent/iron/tools/__init__.py) — `create_default_registry()` 增加 LSP 工具
- [iron/cli/main.py](file:///d:/嵌入式-Agent/iron/cli/main.py) — `run_interactive()` 退出时调用 `LSPClient.stop()`
- [iron/integrations/lsp_client.py](file:///d:/嵌入式-Agent/iron/integrations/lsp_client.py) — 增加异步启动支持（如缺失）
- [tests/test_lsp_integration.py](file:///d:/嵌入式-Agent/tests/test_lsp_integration.py) — **新增**端到端集成测试

**实施步骤：**

1. **bootstrap.py 阶段 3 增加 LSP 客户端初始化**
   - 位置：bootstrap.py 现有阶段 3（AgentEngine + MCP 初始化）之后
   - 逻辑：
     ```python
     if features.lsp_enabled:
         lsp_config = LSPConfig(
             enabled=True,
             project_root=str(project_root),
         )
         lsp_client = LSPClient(lsp_config)
         try:
             await lsp_client.start()
             await lsp_client.initialize()
         except Exception as e:
             logger.warning(f"LSP 启动失败，降级到无 LSP 模式: {e}")
             lsp_client = None
     else:
         lsp_client = None
     ```
   - 将 `lsp_client` 传入 `AgentEngine.__init__`

2. **engine.py `__init__` 接收 LSP 客户端并注册工具**
   - 修改 `BaseAgentEngine.__init__` 签名：增加 `lsp_client: LSPClient | None = None` 参数
   - 在 `_tool_registry` 初始化后，注册 5 个 LSP 工具：
     ```python
     if lsp_client is not None:
         from iron.tools.lsp_tools import (
             LSPDiagnosticsTool, LSPDefinitionTool, LSPReferencesTool,
             LSPHoverTool, LSPCompletionTool,
         )
         for tool_cls in [LSPDiagnosticsTool, LSPDefinitionTool, LSPReferencesTool,
                          LSPHoverTool, LSPCompletionTool]:
             tool = tool_cls()
             tool.set_client(lsp_client)
             self._tool_registry.register(tool)
     ```
   - 保存 `self._lsp_client = lsp_client` 供文件通知使用

3. **engine.py 文件变更通知钩子**
   - 在 `_execute_write_file`（line 1642-1707）写入成功后调用：
     ```python
     if self._lsp_client is not None:
         self._lsp_client.did_change(file_path, content)
     ```
   - 在 `_execute_read_file`（line 1744-1837）读取后调用：
     ```python
     if self._lsp_client is not None:
         self._lsp_client.did_open(file_path, content)
     ```
   - **注意**：`did_change`/`did_open` 是同步方法（lsp_client.py line 417/404），不阻塞主循环

4. **engine.py `edit_file` 分支增加文件通知**
   - 在 line 914-994 的 `edit_file` 分支成功后调用 `did_change`

5. **main.py `run_interactive()` 退出时清理 LSP 客户端**
   - 位置：main.py 现有 `_cleanup_engine_mcp(last_engine)` 调用旁
   - 新增 `_cleanup_lsp(lsp_client)` 辅助函数：
     ```python
     def _cleanup_lsp(lsp_client):
         if lsp_client is not None:
             try:
                 asyncio.get_event_loop().run_until_complete(lsp_client.stop())
             except Exception as e:
                 logger.warning(f"LSP 关闭失败: {e}")
     ```
   - 在 `while True` 循环 break 后、`return` 前调用

6. **VerifyAgent.verify() 真实集成 LSP**
   - 位置：engine.py line 2255-2285
   - 修改：在 prompt 指示外，**显式调用** `lsp_diagnostics` 工具：
     ```python
     async def verify(self, target="src/") -> dict:
         results = {"tests": [], "lint": [], "lsp": []}
         # 显式调用 LSP 诊断（而非依赖 LLM 自主决定）
         if self._lsp_client is not None:
             diag_tool = self._tool_registry.get("lsp_diagnostics")
             if diag_tool is not None:
                 for path in self._collect_source_files(target):
                     diag_result = await diag_tool.safe_execute({"file_path": path})
                     results["lsp"].append({"file": path, "diagnostics": diag_result})
         # 原有测试 + lint 逻辑
         ...
         return results
     ```

7. **ExploreAgent 工具可用性保障**
   - 位置：engine.py line 2298-2301 的 `EXPLORE_TOOLS`
   - 已声明 `lsp_definition`/`lsp_references`/`lsp_hover`，但需确认 `_filter_tools_schema()` 不过滤掉未注册工具
   - 在 `__init__` 注册 LSP 工具后，`_filter_tools_schema()` 自然包含这些工具

8. **新增端到端集成测试 `tests/test_lsp_integration.py`**
   - 测试用例：
     - `test_lsp_client_lifecycle_in_bootstrap` — 启动管道创建并初始化 LSP 客户端
     - `test_lsp_tools_registered_in_engine` — engine `_tool_registry` 含 5 个 LSP 工具
     - `test_write_file_triggers_did_change` — write_file 后 LSP 客户端收到通知（mock）
     - `test_edit_file_triggers_did_change` — edit_file 后通知
     - `test_read_file_triggers_did_open` — read_file 后通知
     - `test_verify_agent_calls_lsp_diagnostics` — VerifyAgent 显式调用 LSP 工具
     - `test_explore_agent_has_lsp_tools` — ExploreAgent 工具 schema 含 LSP
     - `test_lsp_disabled_when_feature_off` — 特性门控关闭时不启动 LSP
     - `test_lsp_startup_failure_degrades_gracefully` — LSP 启动失败降级
     - `test_lsp_cleanup_on_exit` — 退出时调用 `stop()`

**验证清单：**
- [ ] grep `LSPClient` in `iron/agent/engine.py` 命中 ≥ 3 处（`__init__`/`did_change`/`did_open`）
- [ ] grep `lsp_client` in `iron/cli/bootstrap.py` 命中 ≥ 1 处
- [ ] grep `lsp_client` in `iron/cli/main.py` 命中 ≥ 1 处（清理调用）
- [ ] `pytest tests/test_lsp.py -v` 全绿（原有 521 用例不回归）
- [ ] `pytest tests/test_lsp_integration.py -v` 全绿（新增 ≥ 10 用例）
- [ ] `pytest tests/ -v` 总数 ≥ 748 passed（+10）
- [ ] 手动测试：在嵌入式项目目录启动 iron，输入 `/build` 后无 LSP 报错

**反模式防护：**
- 不要在 `process()` 主循环中直接调用 `LSPClient` 方法 — 必须通过工具注册或 `_execute_*` 钩子
- 不要让 LSP 启动失败导致 iron 退出 — 必须 try/except 降级
- 不要在 `did_change` 中等待 LSP 响应 — 它是 fire-and-forget 通知
- 不要在 VerifyAgent 中阻塞等待 LSP — 用 `asyncio.gather` 并行调用

---

### 阶段 1.3 · main.py run_interactive() 拆分

**目标：** 将 267 行的 `run_interactive()` 拆分为职责单一的子函数，降低嵌套层级。

**涉及文件：**
- [iron/cli/main.py](file:///d:/嵌入式-Agent/iron/cli/main.py) — 主修改
- [tests/test_cli_commands.py](file:///d:/嵌入式-Agent/tests/test_cli_commands.py) — 回归测试

**实施步骤：**

1. **提取组件初始化函数** `_init_session_components(config, project_root)`
   - 来源：main.py line 395-475
   - 内容：PromptBuilder / Skills / LLM / SQLite / session / completer 初始化
   - 返回：`ComponentBundle` 命名元组（含所有组件引用）

2. **提取启动信息显示函数** `_show_startup_info(config, llm, total_rules)`
   - 来源：main.py line 401-457
   - 内容：版本 / 项目 / MCU / 模型 / 规则数 / API Key 前 4 后 4

3. **提取用户输入处理器** `_handle_user_input(text, last_options)`
   - 来源：main.py line 531-547
   - 内容：数字选择映射（`text.isdigit() and last_options`）

4. **提取斜杠命令分发器** `_dispatch_slash_command(cmd, args, cmd_ctx, _is_non_chat)`
   - 来源：main.py line 549-600
   - 内容：4 路分组分发（file/build/session/system）+ 清屏逻辑

5. **重构后的 `run_interactive()` 骨架**（目标 ≤ 80 行）：
   ```python
   def run_interactive(config: IronConfig, project_root: Path):
       components = _init_session_components(config, project_root)
       _show_startup_info(config, components.llm, components.total_rules)
       last_options = []
       last_engine = None
       while True:
           try:
               user_input = get_user_input(...)
           except (KeyboardInterrupt, EOFError):
               _cleanup_engine_mcp(last_engine)
               break
           text = user_input.strip()
           if not text:
               continue
           if user_input == "__UNDO__":
               _do_undo(console, last_engine)
               continue
           last_options = _handle_user_input(text, last_options)
           if text.startswith("/"):
               cmd_ctx = _dispatch_slash_command(text, ...)
               if cmd_ctx["should_quit"]:
                   _cleanup_engine_mcp(last_engine)
                   break
               last_engine = cmd_ctx.get("engine", last_engine)
               continue
           # 普通输入 → Agent 处理
           last_engine = _run_agent(...)
   ```

**验证清单：**
- [ ] `run_interactive()` 函数行数 ≤ 80 行
- [ ] 每个提取的子函数行数 ≤ 60 行
- [ ] `pytest tests/test_cli_commands.py -v` 全绿（711+ 用例）
- [ ] `pytest tests/ -v` 总数 ≥ 738 passed（无回归）
- [ ] 手动测试：`/help` / `/model` / `/read` / `/build` / `/resume` 全部正常

**反模式防护：**
- 不要改变斜杠命令的执行顺序和副作用（清屏逻辑必须保留）
- 不要在子函数中重新创建 `last_engine`（必须通过返回值或参数传递）
- 不要破坏 `__UNDO__` 双击 Esc 撤销逻辑

---

### 阶段 1.4 · 流式中断恢复增强

**目标：** 当前流式响应中断时 fallback 到非流式（重发请求），改为支持 resume 续传或保留已接收 chunk。

**涉及文件：**
- [iron/llm/backend.py](file:///d:/嵌入式-Agent/iron/llm/backend.py) — 流式响应处理
- [iron/agent/engine.py](file:///d:/嵌入式-Agent/iron/agent/engine.py) — `_handle_thinking_phase` 中的 fallback 逻辑

**实施步骤：**

1. **分析当前 fallback 逻辑**
   - 位置：engine.py line 644-698
   - 问题：流式中断后重发完整请求，导致 token 双重消耗（违反 project_memory.md 硬约束）

2. **实现 chunk 缓存机制**
   - 在 `backend.py` 的流式迭代器中增加 `chunk_buffer`：已接收的 chunk 保留
   - 中断时不重发请求，而是用 `chunk_buffer` 拼接部分响应
   - 触发 `stream_interrupted` 事件，告知用户"已收到 N 字符，可能不完整"

3. **engine.py fallback 策略调整**
   - 流式成功：正常处理
   - 流式中断（已收到部分 chunk）：用部分 chunk 拼接，标记 `partial=True`
   - 流式完全失败（0 chunk）：fallback 到非流式（保留原逻辑）
   - 决策逻辑：
     ```python
     if len(chunk_buffer) > 0:
         # 已有部分内容，不重发
         resp = self._parse_partial_response(chunk_buffer)
         yield AgentEvent("stream_partial", {"received_chars": len(chunk_buffer)})
     else:
         # 完全失败，fallback
         resp = await self.llm.chat(messages, stream=False)
     ```

4. **新增测试 `tests/test_stream_recovery.py`**
   - `test_stream_success_no_fallback`
   - `test_stream_partial_recovery_uses_buffer`
   - `test_stream_total_failure_falls_back`
   - `test_stream_no_double_token_consumption`

**验证清单：**
- [ ] grep `chunk_buffer` in `iron/llm/backend.py` 命中
- [ ] `pytest tests/test_stream_recovery.py -v` 全绿
- [ ] `pytest tests/test_backend.py -v` 全绿（原有用例不回归）
- [ ] grep 确认流式中断时不调用 `llm.chat(messages, stream=False)`（除非 0 chunk）

**反模式防护：**
- 不要在已收到 chunk 后重发请求（违反 project_memory.md 硬约束）
- 不要将不完整的工具调用 JSON 传给 `_parse_tool_calls`（必须 try/except 并标记 partial）
- 不要静默丢弃 partial 响应 — 必须发射事件告知用户

---

## Phase 2 · 中期（1-2 月，v2.8.0）

### 阶段 2.1 · Skills 可执行机制

**目标：** 将 Skills 从纯 prompt 注入升级为可执行机制，支持 Skill 注册工具、修改 engine 状态、执行预处理逻辑。

**涉及文件：**
- [iron/skills/base.py](file:///d:/嵌入式-Agent/iron/skills/base.py) — 新增 `ExecutableSkill` 基类
- [iron/skills/registry.py](file:///d:/嵌入式-Agent/iron/skills/registry.py) — 注册表支持可执行 Skill
- [iron/agent/engine.py](file:///d:/嵌入式-Agent/iron/agent/engine.py) — Skill 执行钩子
- [tests/test_skills_executable.py](file:///d:/嵌入式-Agent/tests/test_skills_executable.py) — **新增**

**实施步骤：**

1. **设计 `ExecutableSkill` 抽象基类**
   - 位置：`iron/skills/base.py`
   - 接口：
     ```python
     class ExecutableSkill(BaseSkill):
         """可执行 Skill — 支持注册工具、预处理、后处理"""
         def get_tools(self) -> list[BaseTool]:
             """返回此 Skill 注册的工具列表（可为空）"""
             return []
         async def pre_execute(self, context: SkillContext) -> SkillResult:
             """预处理：在 LLM 调用前执行（如收集上下文）"""
             return SkillResult(success=True)
         async def post_execute(self, context: SkillContext, result: Any) -> SkillResult:
             """后处理：在 LLM 调用后执行（如清理、验证）"""
             return SkillResult(success=True)
         def build_prompt(self, context: SkillContext) -> str:
             """仍支持 prompt 注入（向后兼容）"""
             return ""
     ```

2. **SkillContext 数据类**
   - 包含：`user_input` / `engine` / `session` / `tool_registry` / `project_root`
   - 让 Skill 能访问 engine 状态（受控的）

3. **改造 8 个内置 Skill 为可执行**
   - **mcu-init**：`pre_execute` 收集 MCU 型号 + 框架，`get_tools` 返回 `MCUInitTool`
   - **driver-gen**：`pre_execute` 收集外设信息，`get_tools` 返回 `DriverGenTool`
   - **bug-hunt**：`pre_execute` 调用 LSP 诊断收集错误，`get_tools` 返回 `BugHuntTool`
   - **misra-check**：`pre_execute` 调用 EmbedGuard，`get_tools` 返回 `MisraCheckTool`
   - 其他 4 个保持 prompt 注入式（向后兼容）

4. **engine.py Skill 执行钩子**
   - 在 `process()` 思考阶段前调用 `skill.pre_execute(context)`
   - 在 `__init__` 中将 Skill 的 `get_tools()` 注册到 `_tool_registry`
   - 在工具调用完成后调用 `skill.post_execute(context, result)`

5. **SkillRegistry.match() 扩展**
   - 返回 `(skill, score)` 而非仅 skill
   - 支持可执行 Skill 和 prompt Skill 混合匹配

**验证清单：**
- [ ] `ExecutableSkill` 抽象基类定义
- [ ] 至少 4 个内置 Skill 改造为可执行（mcu-init/driver-gen/bug-hunt/misra-check）
- [ ] `pytest tests/test_skills_executable.py -v` 全绿（≥ 15 用例）
- [ ] `pytest tests/test_core.py -v` 全绿（原有 Skill 测试不回归）
- [ ] grep `pre_execute` in `engine.py` 命中

**反模式防护：**
- 不要让 Skill 的 `pre_execute` 阻塞主循环超过 5 秒（必须有超时）
- 不要在 Skill 中直接修改 `messages` 列表（必须通过 SkillContext）
- 不要破坏 prompt 注入式 Skill 的向后兼容（`PromptSkill` 必须继续工作）

---

### 阶段 2.2 · 向量语义搜索原型

**目标：** 为历史会话和 MEMORY.md 增加向量语义搜索，替代当前的 `LIKE %query%` 查询。

**涉及文件：**
- [iron/core/db.py](file:///d:/嵌入式-Agent/iron/core/db.py) — 增加 embedding 列和向量查询
- [iron/core/migrations/002_add_embeddings.sql](file:///d:/嵌入式-Agent/iron/core/migrations/002_add_embeddings.sql) — **新增**迁移
- [iron/llm/backend.py](file:///d:/嵌入式-Agent/iron/llm/backend.py) — 增加 embedding 接口
- [iron/agent/memory.py](file:///d:/嵌入式-Agent/iron/agent/memory.py) — MEMORY.md 向量化
- [tests/test_vector_search.py](file:///d:/嵌入式-Agent/tests/test_vector_search.py) — **新增**

**实施步骤：**

1. **新增迁移 `002_add_embeddings.sql`**
   ```sql
   -- messages 表增加 embedding 列
   ALTER TABLE messages ADD COLUMN embedding BLOB DEFAULT NULL;
   CREATE INDEX idx_messages_embedding ON messages(embedding) WHERE embedding IS NOT NULL;

   -- history 表增加 embedding 列
   ALTER TABLE history ADD COLUMN embedding BLOB DEFAULT NULL;

   -- 向量元数据表
   CREATE TABLE embedding_meta (
       id INTEGER PRIMARY KEY AUTOINCREMENT,
       model_name TEXT NOT NULL,
       dimension INTEGER NOT NULL,
       created_at TEXT NOT NULL
   );
   ```

2. **选择向量后端**
   - **方案 A（推荐）**：`sqlite-vec` — 纯 SQLite 扩展，无需额外服务
   - **方案 B**：`chromadb` — 功能完整但引入重依赖
   - **决策**：先用 sqlite-vec，失败时降级到关键词搜索

3. **backend.py 增加 embedding 接口**
   ```python
   class LLMBackend(ABC):
       @abstractmethod
       async def embed(self, texts: list[str]) -> list[list[float]]:
           """生成文本向量"""
           ...
   ```
   - OpenAI 兼容后端：调用 `/v1/embeddings` 端点
   - 模型：`text-embedding-3-small`（1536 维）或 `BAAI/bge-small-zh`（512 维，中文优化）

4. **Database 增加向量查询方法**
   ```python
   async def search_semantic(self, query: str, limit: int = 10) -> list[MessageRow]:
       query_embedding = await self._embed(query)
       # sqlite-vec 余弦相似度查询
       rows = self.conn.execute("""
           SELECT m.*, vec_distance_cosine(m.embedding, ?) AS distance
           FROM messages m
           WHERE m.embedding IS NOT NULL
           ORDER BY distance ASC
           LIMIT ?
       """, (sqlite_vec.serialize(query_embedding), limit)).fetchall()
       return [MessageRow.from_row(r) for r in rows]
   ```

5. **消息保存时生成 embedding**
   - 在 `save_message` 中，user/assistant 消息保存后异步生成 embedding
   - 用 `asyncio.create_task` 不阻塞主流程
   - LLM 不可用时跳过（降级到关键词搜索）

6. **MEMORY.md 向量化**
   - 在 `ProjectMemory.append_to_memory` 后，将新章节向量化存储到 `embedding_meta` 表
   - `build_context_injection` 增加语义检索：根据当前用户输入查询相关记忆片段

7. **混合检索策略**
   - 关键词搜索（`LIKE`）+ 语义搜索（向量）融合排序
   - 权重：关键词 0.3 + 语义 0.7（可配置）

8. **新增测试 `tests/test_vector_search.py`**
   - `test_embedding_generated_on_save`
   - `test_semantic_search_returns_relevant`
   - `test_keyword_fallback_when_no_embedding`
   - `test_hybrid_search_fusion`
   - `test_embedding_failure_degrades_gracefully`
   - `test_memory_md_vectorization`

**验证清单：**
- [ ] `002_add_embeddings.sql` 存在且 `Database._migrate()` 自动执行
- [ ] grep `embed` in `iron/llm/backend.py` 命中
- [ ] grep `search_semantic` in `iron/core/db.py` 命中
- [ ] `pytest tests/test_vector_search.py -v` 全绿（≥ 10 用例）
- [ ] `pytest tests/test_db.py -v` 全绿（原有 466 用例不回归）
- [ ] `pytest tests/test_memory.py -v` 全绿

**反模式防护：**
- 不要在 LLM 不可用时阻塞消息保存（embedding 是异步可选的）
- 不要为已有消息批量回填 embedding（仅在启动时一次性回填，避免每次启动慢）
- 不要引入闭源 embedding 模型为默认（必须有开源 fallback：`BAAI/bge-small-zh`）
- 不要破坏 `search_history` 的现有签名（新增 `search_semantic` 而非替换）

---

### 阶段 2.3 · MCP 健康检查与流式恢复

**目标：** 为 MCP 客户端增加主动健康检查，避免连接断开后工具调用失败。

**涉及文件：**
- [iron/mcp/client.py](file:///d:/嵌入式-Agent/iron/mcp/client.py) — 增加健康检查
- [tests/test_mcp_client.py](file:///d:/嵌入式-Agent/tests/test_mcp_client.py) — 回归测试

**实施步骤：**

1. **MCP 客户端健康检查**
   - 新增 `async def health_check(self) -> bool` 方法
   - 定期 ping（默认 60 秒），失败时标记为 unhealthy
   - 工具调用前检查健康状态，unhealthy 时尝试重连

2. **断线重连机制**
   - stdio 传输：重启子进程
   - SSE/HTTP 传输：重新建立连接
   - 重连失败 3 次后标记为 disconnected，发射 `mcp_disconnected` 事件

3. **测试用例**
   - `test_mcp_health_check_success`
   - `test_mcp_health_check_failure_triggers_reconnect`
   - `test_mcp_reconnect_stdio`
   - `test_mcp_reconnect_sse`
   - `test_mcp_disconnected_event_emitted`

**验证清单：**
- [ ] grep `health_check` in `iron/mcp/client.py` 命中
- [ ] `pytest tests/test_mcp_client.py -v` 全绿
- [ ] `pytest tests/ -v` 总数 ≥ 760 passed

---

### 阶段 2.4 · 测试缺口补齐

**目标：** 补齐 evaluation-v3.md 第 4.3 节标记的测试缺口。

**涉及文件：**
- [tests/test_lsp_e2e.py](file:///d:/嵌入式-Agent/tests/test_lsp_e2e.py) — **新增** LSP 端到端（需真实 clangd）
- [tests/test_prompt_cache_hit_rate.py](file:///d:/嵌入式-Agent/tests/test_prompt_cache_hit_rate.py) — **新增** 缓存命中率
- [tests/test_windows_symlink.py](file:///d:/嵌入式-Agent/tests/test_windows_symlink.py) — **新增** Windows symlink

**实施步骤：**

1. **LSP 端到端测试**
   - 标记 `@pytest.mark.skipif(not shutil.which("clangd"))` — 无 clangd 时跳过
   - 启动真实 clangd 进程，测试 diagnostics/definition/hover
   - 用一个固定的 `.c` 测试文件

2. **Prompt Caching 命中率测试**
   - mock LLM 后端，统计 `cache_control` 标记的块数
   - 验证系统提示分块策略的命中率
   - 测试 hash 遥测逻辑

3. **Windows symlink 测试**
   - 标记 `@pytest.mark.skipif(not IS_WINDOWS)` 或 `@pytest.mark.skipif(IS_WINDOWS)`
   - 创建真实 symlink，验证路径穿越防护

**验证清单：**
- [ ] 3 个新测试文件存在
- [ ] `pytest tests/ -v` 总数 ≥ 775 passed
- [ ] 无 clangd 环境下 LSP e2e 测试 skip 而非 fail

---

## Phase 3 · 长期（3-6 月，v3.0.0）

### 阶段 3.1 · 代码索引与语义理解

**目标：** 引入 tree-sitter 代码解析和语义索引，让 Agent 真正"理解"代码结构。

**涉及文件：**
- [iron/integrations/code_indexer.py](file:///d:/嵌入式-Agent/iron/integrations/code_indexer.py) — **新增**
- [iron/integrations/lsp_client.py](file:///d:/嵌入式-Agent/iron/integrations/lsp_client.py) — 深度集成
- [iron/tools/semantic_tools.py](file:///d:/嵌入式-Agent/iron/tools/semantic_tools.py) — **新增** 语义工具

**实施步骤：**

1. **tree-sitter 代码解析器**
   - 已有依赖：`tree-sitter>=0.21.0` + `tree-sitter-c>=0.21.0`（pyproject.toml line 39）
   - 实现 `CodeIndexer` 类：
     - `index_project(root)` — 遍历 `.c/.h` 文件，解析 AST
     - `get_symbol_definition(name)` — 查找符号定义
     - `get_callers(name)` — 查找函数调用者
     - `get_callgraph()` — 构建调用图

2. **语义索引存储**
   - 复用 SQLite 数据库，新增 `symbols` 和 `callgraph` 表
   - 文件变更时增量更新索引（`did_change` 钩子触发）

3. **新增语义工具**
   - `semantic_search` — 语义搜索代码（"查找所有调用 HAL_Delay 的地方"）
   - `get_callers` — 查找函数调用者
   - `get_callees` — 查找函数被调用者
   - `find_dead_code` — 查找未被调用的函数

4. **LSP + tree-sitter 融合**
   - LSP 提供实时诊断和跳转
   - tree-sitter 提供离线索引和调用图
   - 两者数据融合到统一的 `CodeIndex`

**验证清单：**
- [ ] `CodeIndexer` 类实现
- [ ] 4 个语义工具注册
- [ ] `pytest tests/test_code_indexer.py -v` 全绿
- [ ] 索引构建时间 < 10 秒（10K 行项目）

---

### 阶段 3.2 · 插件市场

**目标：** 支持第三方插件，让用户安装/卸载/更新插件。

**涉及文件：**
- [iron/plugins/](file:///d:/嵌入式-Agent/iron/plugins/) — **新增** 插件系统
- [iron/cli/commands/marketplace.py](file:///d:/嵌入式-Agent/iron/cli/commands/marketplace.py) — **新增** `/plugin` 命令

**实施步骤：**

1. **插件接口设计**
   ```python
   class IronPlugin:
       name: str
       version: str
       def on_load(self, context: PluginContext): ...
       def on_unload(self): ...
       def get_tools(self) -> list[BaseTool]: ...
       def get_skills(self) -> list[BaseSkill]: ...
       def get_hooks(self) -> list[BaseHook]: ...
   ```

2. **插件市场**
   - `/plugin search <keyword>` — 搜索市场
   - `/plugin install <name>` — 安装
   - `/plugin list` — 列出已安装
   - `/plugin update <name>` — 更新
   - `/plugin remove <name>` — 卸载

3. **插件沙箱**
   - 插件运行在受限环境（无法访问文件系统外的资源）
   - 插件崩溃不影响主进程

**验证清单：**
- [ ] 插件接口定义
- [ ] `/plugin` 命令实现
- [ ] 至少 1 个示例插件（如 `iron-plugin-stm32-templates`）

---

### 阶段 3.3 · Vim 模式

**目标：** 实现 Vim 风格的键盘绑定（特性门控 `vim_mode` 已就位）。

**涉及文件：**
- [iron/cli/ui.py](file:///d:/嵌入式-Agent/iron/cli/ui.py) — Vim 键绑定
- [iron/config/features.py](file:///d:/嵌入式-Agent/iron/config/features.py) — 已有 `vim_mode` 开关

**实施步骤：**

1. **Vim 模式状态机**
   - Normal / Insert / Visual 三模式
   - `hjkl` 移动、`i` 进入 Insert、`Esc` 回 Normal、`v` 进入 Visual

2. **prompt_toolkit 集成**
   - 用 `Application` 的 `key_bindings` 实现
   - 底部状态栏显示当前模式

**验证清单：**
- [ ] `features.vim_mode == True` 时启用 Vim 绑定
- [ ] `pytest tests/test_vim_mode.py -v` 全绿

---

### 阶段 3.4 · 远程/SSH 模式

**目标：** 支持通过 SSH 连接远程开发，类似 Claude Code 的远程模式。

**涉及文件：**
- [iron/remote/](file:///d:/嵌入式-Agent/iron/remote/) — **新增** 远程模块
- [iron/cli/main.py](file:///d:/嵌入式-Agent/iron/cli/main.py) — `--remote` 参数

**实施步骤：**

1. **SSH 连接管理**
   - `paramiko` 或 `asyncssh` 库
   - `iron --remote user@host:/path/to/project`

2. **远程工具执行**
   - 文件读写、命令执行通过 SSH 转发
   - LSP 客户端在远程运行

**验证清单：**
- [ ] `--remote` 参数支持
- [ ] 远程文件读写正常
- [ ] 远程命令执行正常

---

### 阶段 3.5 · OS 沙箱（可选）

**目标：** 在 OS 级别隔离工具执行，类似 Claude Code 的沙箱。

**涉及文件：**
- [iron/security/sandbox.py](file:///d:/嵌入式-Agent/iron/security/sandbox.py) — **新增**

**实施步骤：**

1. **平台特定沙箱**
   - Linux：`bwrap` 或 `firejail`
   - macOS：`sandbox-exec`
   - Windows：`AppContainer`（可选）

2. **工具执行包装**
   - `run_command` 在沙箱内执行
   - 文件写入限制在项目目录

**验证清单：**
- [ ] 沙箱启动成功
- [ ] 沙箱内无法访问项目外文件
- [ ] `pytest tests/test_sandbox.py -v` 全绿

---

## Phase 4 · 最终验证

### 4.1 全量回归测试

```bash
# 运行所有测试
pytest tests/ -v

# 覆盖率报告
pytest tests/ --cov=iron --cov-report=term-missing

# 安全测试
pytest tests/test_security.py -v

# LSP 集成测试
pytest tests/test_lsp_integration.py -v

# 向量搜索测试
pytest tests/test_vector_search.py -v
```

### 4.2 反模式 grep 检查

```bash
# 确认 LSPClient 不在 process() 中直接调用
grep -n "LSPClient" iron/agent/engine.py | grep -v "__init__\|_execute_"

# 确认流式中断不重发请求
grep -n "llm.chat.*stream=False" iron/agent/engine.py

# 确认迁移文件递增
ls iron/core/migrations/

# 确认 process() 行数
python -c "import ast; tree = ast.parse(open('iron/agent/engine.py').read()); ..."

# 确认 run_interactive() 行数
python -c "..."
```

### 4.3 性能基准

| 指标 | 基线 (v2.5.0) | 目标 (v3.0.0) |
|------|--------------|--------------|
| 启动时间 | < 2 秒 | < 2 秒 |
| 测试用例数 | 738 | ≥ 800 |
| 测试比例 | 0.61 | ≥ 0.7 |
| engine.py process() 行数 | 802 | ≤ 150 |
| run_interactive() 行数 | 267 | ≤ 80 |
| LSP 工具注册 | 0 | 5 |
| 向量搜索 | 无 | 有 |

### 4.4 文档更新

- [ ] 更新 [ARCHITECTURE-v2.md](file:///d:/嵌入式-Agent/docs/ARCHITECTURE-v2.md) → v3.0.0
- [ ] 更新 [architecture-framework.md](file:///d:/嵌入式-Agent/docs/architecture-framework.md) 进度跟踪
- [ ] 新增 [evaluation-v4.md](file:///d:/嵌入式-Agent/docs/evaluation-v4.md) 评测报告
- [ ] 更新 [gap-analysis.md](file:///d:/嵌入式-Agent/docs/gap-analysis.md) 差距对比

---

## 附录 A · 优先级矩阵

| 任务 | 优先级 | 风险 | 收益 | 建议顺序 |
|------|--------|------|------|---------|
| 1.1 engine.py 拆分 | P0 | 中（重构易引入回归） | 高（可维护性） | 1 |
| 1.2 LSP 端到端集成 | P0 | 低（新增功能） | 高（核心能力） | 2 |
| 1.3 run_interactive 拆分 | P1 | 低 | 中 | 3 |
| 1.4 流式恢复 | P1 | 中（影响核心循环） | 中 | 4 |
| 2.1 Skills 可执行 | P1 | 中 | 高 | 5 |
| 2.2 向量搜索 | P2 | 中（新依赖） | 高 | 6 |
| 2.3 MCP 健康检查 | P2 | 低 | 中 | 7 |
| 2.4 测试缺口 | P2 | 低 | 中 | 8 |
| 3.1 代码索引 | P3 | 高（大功能） | 高 | 9 |
| 3.2 插件市场 | P3 | 高 | 中 | 10 |
| 3.3 Vim 模式 | P3 | 低 | 低 | 11 |
| 3.4 远程/SSH | P3 | 高 | 中 | 12 |
| 3.5 OS 沙箱 | P3 | 高 | 中 | 13 |

## 附录 B · 版本里程碑

| 版本 | 内容 | 周期 |
|------|------|------|
| v2.6.0 | Phase 1（短期 4 任务） | 1-2 周 |
| v2.7.0 | Phase 2.1-2.2（Skills + 向量搜索） | 3-4 周 |
| v2.8.0 | Phase 2.3-2.4（MCP + 测试） | 5-6 周 |
| v2.9.0 | Phase 3.1（代码索引） | 8-12 周 |
| v3.0.0 | Phase 3.2-3.5（插件 + Vim + 远程 + 沙箱） | 16-24 周 |

## 附录 C · 依赖项更新

| 阶段 | 新增依赖 | 用途 |
|------|---------|------|
| 2.2 | `sqlite-vec` | 向量搜索 |
| 2.2 | `BAAI/bge-small-zh`（可选） | 中文 embedding |
| 3.1 | `tree-sitter` + `tree-sitter-c`（已有） | 代码解析 |
| 3.4 | `paramiko` 或 `asyncssh` | SSH 远程 |
| 3.5 | `bwrap` / `firejail` / `sandbox-exec` | OS 沙箱 |
