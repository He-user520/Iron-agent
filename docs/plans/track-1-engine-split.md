# Track 1 · engine.py process() 拆分子计划

> 本文档是 Iron CLI 重构（Track 1）的执行级子计划。
> 目标对象：`iron/agent/engine.py` 的 `BaseAgentEngine.process()` 方法。
> 基线版本：当前 `main` 分支，`process()` 位于 line 542-1343，共 **802 行**。

---

## 1. 目标与约束

### 1.1 目标

| 指标 | 现状 | 目标 |
|------|------|------|
| `process()` 函数体行数 | 802 行（542-1343） | **≤ 150 行** |
| 单个新子方法行数 | — | **≤ 100 行** |
| 工具分发 if/elif 链长度 | 457 行（747-1203） | 0 行（拆入 `_dispatch_tool_call` 路由器） |
| 圈复杂度（process） | 极高（估算 > 60） | ≤ 15 |

### 1.2 硬约束（不可破坏）

1. **AgentEvent yield 顺序与数量不变**（纯重构，行为等价）
   - 包括：`thinking` / `phase` / `chat_chunk` / `chat_response` / `tool_blocked` / `step_warn` / `step_done` / `file_done` / `file_read` / `command` / `stop_hook` / `summary` / `file_tree` / `cache_hit` / `error` 等所有事件类型。
   - 流式 `chat_chunk` 的实时 yield 必须保持（不可缓冲到末尾）。
2. **`tool_results` 列表的累积逻辑不变**：append 顺序、`tool_call_id`、`role`、`content`（JSON 序列化格式）逐字段等价。
3. **保留 `finally` 块的孤儿任务取消**（line 1208-1212）：异常退出时必须 cancel 未完成的 `_pending_readonly` 任务。
4. **`_should_terminate` / `_chat_message` 的语义保留**：chat 工具终止循环、跨会话保留 assistant 消息。
5. **`self.conversation` 的 append 顺序与内容不变**：user / assistant(+tool_calls) / tool 消息顺序。
6. **不改变 import、不改变公开 API**：`process()` 签名保持 `async def process(self, user_input: str)`。
7. **不引入新依赖**：仅使用已 import 的标准库与项目内模块。

### 1.3 软约束

- 优先复用已存在的子方法（`_emit_event` / `_execute_write_file` / `_flush_readonly_tasks` 等），不重复造轮子。
- 子方法命名以 `_handle_*` / `_dispatch_*` 前缀，与现有 `_execute_*` 区分。
- 每个子方法职责单一（SRP），便于单元测试。
- 保持原注释中的设计意图（doom_loop / 双重权限 / embed_build 重定向等）。

---

## 2. 现状分析

### 2.1 process() 函数概览

| 区域 | 行号范围 | 行数 | 职责 |
|------|----------|------|------|
| 文档字符串 | 543-552 | 10 | 流程说明 |
| 会话前置初始化 | 553-621 | 69 | dream/distill、状态重置、skill 匹配、系统提示、缓存命中、MCP 连接 |
| 主循环 `for step` | 623-1300 | 678 | Agentic Loop 主体 |
| ├ 思考阶段 | 624-698 | 75 | 流式生成 + fallback + chunk 累积 |
| ├ 无工具调用分支 | 700-710 | 11 | 纯聊天回复 → break |
| ├ 只读 Agent 过滤 | 716-740 | 25 | `_get_allowed_tools()` 过滤 |
| ├ 工具分发循环 | 747-1203 | 457 | 巨型 if/elif 链 |
| │  ├ chat | 835-841 | 7 | 终止性工具 |
| │  ├ write_file | 843-912 | 70 | 写文件 + 聊天内容拦截 |
| │  ├ edit_file | 914-994 | 81 | 编辑 + 撤销历史 |
| │  ├ run_command | 996-1081 | 86 | 命令执行 + 编译重定向 |
| │  ├ read_file | 1083-1098 | 16 | 读文件 |
| │  └ 外部工具 else | 1100-1203 | 104 | 注册工具 + 只读并行 + 破坏性授权 |
| ├ finally 取消孤儿任务 | 1208-1212 | 5 | 防泄漏 |
| └ 循环后处理 | 1214-1300 | 87 | 终止检查 / 任务完成 / 步数预警 / 对话历史 / stop_hooks |
| 最大步数兜底 | 1303-1312 | 10 | `for...else` 分支 |
| 总结与持久化 | 1314-1342 | 29 | phase DONE / summary / checkpoint / task 持久化 |

### 2.2 圈复杂度热点

| 行号 | 构造 | 说明 |
|------|------|------|
| 644-670 | `if hasattr(stream_generate)` + `async for` + `if event_type == ...` | 流式分支 3 路 |
| 678-698 | `if resp is None` + `if _stream_chunks_received` + `elif _stream_error` | fallback 三态 |
| 718-740 | `if _allowed_tools is not None` + `for tc` + `if/else` | 只读过滤 |
| 759-833 | 3 层 `if name != "chat"` 嵌套（黑名单/规则/hooks） | 权限三段式 |
| 835-1203 | `if/elif` × 5 + 嵌套 `if/elif/else` | 工具分发主热点 |
| 880-890 | `if SOURCE_EXTENSIONS` + `if CHAT_INDICATORS` | 写文件拦截 |
| 1038-1060 | `if any(kw in cmd_text)` + `if build_tool` | 编译重定向 |
| 1114-1167 | `if _is_readonly_tool` × 2 + `if _EXTERNAL_WRITE_TOOLS` + `if _perm == ask/auto` | 外部工具权限 |
| 1271-1284 | `for tr` + `try/except` + `if failed` | 失败检测 |
| 1288-1300 | `if _stop_decision is not None` | stop_hooks |

### 2.3 已存在的子方法清单（不重复提取）

| 方法 | 行号 | 签名 | 说明 |
|------|------|------|------|
| `_emit_event` | 516 | `async def _emit_event(self, event_type, data=None) -> AgentEvent` | 事件发射统一入口 |
| `_build_system_prompt` | 398 | `def _build_system_prompt(self) -> str` | 系统提示构建 |
| `_maybe_enable_search_mode` | 332 | `def _maybe_enable_search_mode(self, system) -> tuple[str, list[dict]]` | P4-1 搜索模式 |
| `_match_skills` | 1362 | `def _match_skills(self, user_input) -> str` | Skill 自动匹配 |
| `_parse_tool_calls` | 1583 | `def _parse_tool_calls(self, resp) -> list[dict]` | 工具调用解析 |
| `_check_permission_with_callback` | 1417 | `async def _check_permission_with_callback(self, description, tool_name, args)` | 权限回调 |
| `_flush_readonly_tasks` | 1477 | `async def _flush_readonly_tasks(self, pending, tool_results)` | 只读并行 flush |
| `_is_readonly_tool` | 1525 | `def _is_readonly_tool(self, name, args) -> bool` | 只读判定 |
| `_check_doom_loop` | 1543 | `def _check_doom_loop(self, name, args) -> bool` | 重复调用检测 |
| `_filter_tools_schema` | 322 | `def _filter_tools_schema(self, schemas) -> list[dict]` | schema 权限过滤 |
| `_check_task_completion` | 1344 | `def _check_task_completion(self) -> bool` | 任务完成检测 |
| `_build_file_tree` | 2160 | `def _build_file_tree(self) -> list[str]` | 文件树构建 |
| `_build_progress_summary` | 1397 | `def _build_progress_summary(self) -> str` | 进度摘要 |
| `_estimate_input_tokens` | 2139 | `def _estimate_input_tokens(self, system, messages) -> int` | token 估算 |
| `_execute_write_file` | 1642 | `async def _execute_write_file(self, args)` | 写文件执行器（yield 事件） |
| `_execute_run_command` | 1709 | `async def _execute_run_command(self, args)` | 命令执行器（yield 事件） |
| `_execute_read_file` | 1744 | `async def _execute_read_file(self, args)` | 读文件执行器（yield 事件） |

### 2.4 待提取的代码块清单

| 编号 | 行号 | 拟提取方法 | 行数 |
|------|------|------------|------|
| B1 | 553-621 | `_init_session(user_input)` | 69 |
| B2 | 624-698 | `_handle_thinking_phase(system, messages, step, _effective_tools)` | 75 |
| B3 | 716-740 | `_filter_tool_calls_by_permission(tool_calls)` | 25 |
| B4 | 757-833 | `_check_pre_tool_gates(call, call_id, tool_results)` | 77 |
| B5 | 835-1203 | `_dispatch_tool_call(...)` 路由器 | 369（再拆 6 子方法） |
| B6 | 1214-1300 | `_handle_post_step(step, resp, tool_results, tool_calls)` | 87 |
| B7 | 1303-1312 | `_handle_max_steps_exceeded()` | 10 |
| B8 | 1314-1342 | `_handle_summary_and_persist(...)` | 29 |

---

## 3. 拆分方案（每个子方法一节）

> 通用约定：
> - 所有新方法均为 `async def`，定义在 `BaseAgentEngine` 内、`process()` 之后、`_check_task_completion()` 之前。
> - 需要 yield `AgentEvent` 的方法以 `AsyncGenerator[AgentEvent, None]` 形式存在；调用方用 `async for ev in self._xxx(...): yield ev` 转发。
> - 不 yield 的方法返回普通值或元组。
> - 所有方法保持原有异常处理边界（`asyncio.CancelledError` 必须 `raise`）。

### 3.1 `_init_session(user_input)`

- **来源行号**：553-621
- **提取内容**：
  - dream/distill 记忆整理（555-558）
  - `_recent_calls` / `_stop_hooks` / 文件树缓存重置（560-568）
  - skill 匹配 + conversation append + system prompt 构建（570-577）
  - 搜索模式切换（579-581）
  - prompt cache 命中检测 + `cache_hit` 事件（583-594）
  - 文件清单与终止标志初始化（596-599）
  - MCP 首次连接 + schema 重建（601-621）
- **返回值**：`tuple[str, list[dict], list, list]` → `(system, _effective_tools, all_files_created, all_files_modified)`
- **yield 行为**：是。会 yield `cache_hit` 事件（line 590-594）。
- **依赖的实例方法**：`self._memory.maybe_dream_distill` / `self._match_skills` / `self._build_system_prompt` / `self._maybe_enable_search_mode` / `self._emit_event` / `self._mcp_client` / `self._tool_registry` / `self._filter_tools_schema`
- **风险点**：
  - MCP 连接的 `try/except/finally` 中 `_mcp_connected` 置位逻辑必须保留，避免每次 process 重试连接。
  - `cache_hit` 事件 yield 在循环外，提取后仍需向上传递。
- **测试验证**：
  - `tests/test_engine_integration.py::TestProcessEndToEnd::test_thinking_event_emitted`（验证前置流程不崩）
  - `tests/test_engine_integration.py::TestProcessEndToEnd::test_chat_response_terminates_loop`（验证 conversation append 正确）
  - 需新增：`test_init_session_emits_cache_hit`（若 prompt_cache 命中）

### 3.2 `_handle_thinking_phase(system, messages, step, effective_tools)`

- **来源行号**：624-698
- **提取内容**：
  - 思考事件 yield（`thinking` + `phase=THINK`）（632-639）
  - 流式生成 `async for event_type, event_data in self.llm.stream_generate(...)`（644-663）
    - chunk 累积 + `chat_chunk` yield
    - response / error 分支
  - 非流式 `self.llm.generate(...)` fallback（664-670）
  - 异常捕获 `asyncio.CancelledError` + `(RuntimeError, OSError, httpx.HTTPError)`（671-674）
  - 流式失败 fallback 三态（678-698）：
    - 已收到 chunk → 用累积内容构造 `LLMResponse`
    - 流式出错 → 切非流式 `generate`
    - 非流式也失败 → yield `error` + return（终止 process）
- **返回值**：`LLMResponse | None` → 返回 None 表示应终止 process（调用方检测后 return）
- **yield 行为**：是。yield `thinking` / `phase` / `chat_chunk` / `error`。
- **依赖的实例方法**：`self._compactor.compact_pipeline` / `self._estimate_input_tokens` / `self._emit_event` / `self.llm`
- **依赖的外部类**：`LLMResponse`（从 `iron.llm.backend` 已 import）
- **风险点**：
  - fallback 中 `yield ... return` 模式：子方法返回 sentinel（None）让调用方执行 `return`，不能在子方法内直接 return 终止 process（async generator 的 return 只结束生成器）。
  - `_accumulated_chunks` / `_stream_chunks_received` / `_stream_error` 三个局部状态必须完整搬移。
  - `step == 0` 时 `thinking` 事件带 `input_tokens`，`step > 0` 不带，此差异必须保留。
- **测试验证**：
  - `tests/test_engine_integration.py::TestProcessEndToEnd::test_thinking_event_emitted`（覆盖 step==0 thinking 事件）
  - `tests/test_engine_integration.py::TestProcessEndToEnd::test_phase_events_emitted`（覆盖 phase=THINK）
  - `tests/test_engine_integration.py::TestProcessEndToEnd::test_chat_response_terminates_loop`（覆盖无工具调用路径）
  - 需新增：`test_thinking_phase_streaming_fallback`（mock stream_generate 抛错，验证非流式 fallback）
  - 需新增：`test_thinking_phase_partial_chunks`（验证部分 chunk 恢复）

### 3.3 `_filter_tool_calls_by_permission(tool_calls)`

- **来源行号**：716-740
- **提取内容**：只读 Agent 工具过滤
  - `_get_allowed_tools()` 返回 None → 直接返回原 tool_calls
  - 否则遍历 tool_calls 分流 `filtered_calls` / `blocked_calls`
  - 对 blocked_calls：yield `tool_blocked` 事件 + 构造 tool_result（告知 AI 只读模式限制）
- **返回值**：`tuple[list[dict], list[dict]]` → `(filtered_tool_calls, blocked_tool_results)`
- **yield 行为**：是。yield `tool_blocked` 事件（line 731）。
- **依赖的实例方法**：`self._get_allowed_tools` / `self._emit_event`
- **风险点**：
  - `blocked_tool_results` 的 `tool_call_id` 用 `call.get("id") or f"blocked_{_bi}"`，索引 `_bi` 必须保留。
  - 调用方需把 `blocked_tool_results` 合并入 `tool_results`，并使用过滤后的 `filtered_tool_calls` 进入分发循环。
- **测试验证**：
  - 现有无直接覆盖（TaskAgent 只读测试散落在其他文件）。
  - 需新增：`test_filter_tool_calls_readonly_blocks_write`（用 TaskAgentEngine 验证 write_file 被阻止）
  - 需新增：`test_filter_tool_calls_coder_allows_all`（用 CoderAgentEngine 验证不过滤）

### 3.4 `_check_pre_tool_gates(call, call_id, tool_results)`

- **来源行号**：757-833（注意：此块在 `for idx, call` 循环内、`if name == "chat"` 之前）
- **提取内容**：三段式前置门控
  - **B4a 黑名单检查**（759-776）：`_permission_mgr.check` → deny 则 append tool_result + yield `tool_blocked` + 返回"跳过此工具"
  - **B4b 规则引擎**（778-804）：`_rule_engine.evaluate` → deny/ask 分支
    - deny → append tool_result + yield `step_warn` + 跳过
    - ask + `_permission_callback` → `_check_permission_with_callback`，用户拒绝则跳过
  - **B4c PreToolUse hooks**（806-833）：`_hook_manager.run_pre_hooks`
    - deny → append tool_result + yield `tool_blocked` + 跳过
    - modify → 替换 `args`
- **返回值**：`tuple[bool, dict | None]` → `(should_skip, modified_args)`
  - `should_skip=True` 表示该工具被门控拦截，调用方 `continue`
  - `modified_args` 非 None 表示 hook 修改了参数，调用方需用新 args
- **yield 行为**：是。yield `tool_blocked` / `step_warn` / 权限回调事件。
- **依赖的实例方法**：`self._permission_mgr` / `self._rule_engine` / `self._check_permission_with_callback` / `self._hook_manager` / `self._emit_event`
- **风险点**：
  - chat 工具豁免三段门控（`if name != "chat"` 判断 × 3），提取后调用方需在 chat 分支前跳过此方法。
  - PreHook 异常按 `allow` 处理（line 816-817），此容错逻辑必须保留。
  - `args` 被 modify 后，后续分发必须用新 args（影响 write_file/edit_file 的 path/content 等）。
- **测试验证**：
  - 现有无直接单测覆盖此三段式组合。
  - 需新增：`test_pre_tool_gates_blacklist_deny`
  - 需新增：`test_pre_tool_gates_rule_deny`
  - 需新增：`test_pre_tool_gates_hook_modify_args`
  - 需新增：`test_pre_tool_gates_chat_exempt`

### 3.5 `_dispatch_tool_call(call, idx, call_id, args, tool_results, _pending_readonly, all_files_created, all_files_modified)`

- **来源行号**：835-1203
- **提取内容**：工具分发路由器，按 `name` 分派到以下 6 个子方法
- **返回值**：`tuple[bool, str | None]` → `(_should_terminate, _chat_message)`
  - `_should_terminate=True` 仅当 chat 工具触发
- **yield 行为**：是。转发各子方法的 yield。
- **依赖的子方法**（本节定义）：
  - `_handle_chat_tool`（3.6）
  - `_handle_write_file_tool`（3.7）
  - `_handle_edit_file_tool`（3.8）
  - `_handle_run_command_tool`（3.9）
  - `_handle_read_file_tool`（3.10）
  - `_handle_external_tool`（3.11）
- **风险点**：
  - 写工具前 flush 只读任务（line 754-755）：此逻辑在 `for idx, call` 循环顶部，**不属于** `_dispatch_tool_call`，应留在 `process()` 的循环骨架中，或抽到 `_maybe_flush_readonly_before_write(name, _pending_readonly, tool_results)`。
  - `args` 可能被 `_check_pre_tool_gates` modify，分发时必须用最新 args。
- **测试验证**：见各子方法。

### 3.6 `_handle_chat_tool(args)`

- **来源行号**：835-841
- **提取内容**：
  - 取 `args.get("message", "")`
  - yield `chat_response` 事件
  - 设置终止标志（返回给调用方）
- **返回值**：`str` → `_chat_message`（调用方据此设 `_should_terminate=True`）
- **yield 行为**：是。yield `chat_response`。
- **依赖的实例方法**：`self._emit_event`
- **风险点**：chat 不 append 到 `tool_results`（设计意图，防止 AI 重复回复），必须保留。
- **测试验证**：
  - `tests/test_engine_integration.py::TestProcessEndToEnd::test_chat_response_terminates_loop`
  - `tests/test_engine_integration.py::TestProcessEndToEnd::test_tool_call_then_chat`

### 3.7 `_handle_write_file_tool(call_id, args, tool_results, all_files_created, all_files_modified)`

- **来源行号**：843-912
- **提取内容**：
  - doom_loop 检测（845-852）：触发则 append tool_result + yield `step_warn` + 跳过
  - Agent 权限三态（855-876）：deny/ask 分支
  - 聊天内容写入源码拦截（877-890）：`SOURCE_EXTENSIONS` + `CHAT_INDICATORS`
  - 执行 `_execute_write_file(args)` 并消费事件（891-905）：
    - `file_done` → 分流 created/modified（依据 `event.data["action"]`）
    - `error` → 记录错误
    - `step_warn` 含"跳过" → 视为用户拒绝
  - PostToolUse hooks（907）
  - append tool_result（908-912）
- **返回值**：无（直接 mutate `tool_results` / `all_files_created` / `all_files_modified`）。返回 `bool` 表示是否被 doom_loop/权限拦截（调用方据此 `continue`）。
- **yield 行为**：是。yield `step_warn` / `file_done` / `error` / 权限回调事件。
- **依赖的实例方法**：`self._check_doom_loop` / `self._agent_manager.get_permission` / `self._check_permission_with_callback` / `self._execute_write_file` / `self._hook_manager.run_post_hooks` / `self._emit_event`
- **依赖的外部常量**：`SOURCE_EXTENSIONS` / `CHAT_INDICATORS`（已 import）
- **风险点**：
  - `file_done` 的 `action` 字段决定 created/modified 分流（line 896-901），不可误判。
  - `step_warn` 含"跳过"的字符串匹配（line 904）较脆弱，保留原逻辑。
- **测试验证**：
  - `tests/test_engine_integration.py::TestProcessEndToEnd::test_write_file_creates_file`
  - `tests/test_engine.py::TestDoomLoop::test_same_call_3_times_trigger`（doom_loop 单测）
  - 需新增：`test_write_file_chat_content_blocked`（验证聊天内容写入源码被拦截）

### 3.8 `_handle_edit_file_tool(call_id, args, tool_results, all_files_modified)`

- **来源行号**：914-994
- **提取内容**：
  - doom_loop 检测（916-923）
  - Agent 权限三态（926-947）
  - 执行 edit_file + 撤销历史快照（948-985）：
    - edit 前读取整个文件内容作为 `old_content`（安全撤销）
    - `tool.safe_execute(args, context)`
    - 成功则 append 到 `_change_history`（限制最大 20 条）
  - PostToolUse hooks（989）
  - append tool_result（990-994）
- **返回值**：`bool`（是否被拦截，调用方据此 `continue`）
- **yield 行为**：是。yield `step_warn` / 权限回调事件。
- **依赖的实例方法**：`self._check_doom_loop` / `self._agent_manager.get_permission` / `self._check_permission_with_callback` / `self._tool_registry.get` / `self._resolve_project_path` / `self._hook_manager.run_post_hooks` / `self._emit_event`
- **依赖的实例属性**：`self._project_dir` / `self._question_callback` / `self._change_history`
- **风险点**：
  - `_change_history` 的 20 条上限截断逻辑（line 984-985）必须保留。
  - `old_file_content` 读取失败的 `try/except (OSError, ValueError)` 容错保留。
  - `safe_execute` 返回 None 的防御性检查（line 969-970）保留。
- **测试验证**：
  - `tests/test_engine.py::TestUndoWithOldContent::test_undo_edit_fallback_string_replace`
  - `tests/test_engine.py::TestUndoWithOldContent::test_undo_new_file_deletes`
  - `tests/test_engine.py::TestUndoWithOldContent::test_undo_returns_record`
  - 需新增：`test_edit_file_doom_loop_blocked`

### 3.9 `_handle_run_command_tool(call_id, args, tool_results)`

- **来源行号**：996-1081
- **提取内容**：
  - doom_loop 检测（998-1005）
  - Agent 权限三态（1011-1032）：bash 权限
  - 编译命令重定向到 embed_build（1033-1060）：
    - `_build_kw` 关键词列表
    - 命中则调 `embed_build.safe_execute({"action": "compile"}, ...)`
    - PostHook 用原 name/args（保留用户原始请求语义）
  - 执行 `_execute_run_command(args)` 消费事件（1061-1074）：
    - `command` 事件 → 截断 stdout/stderr 到 2000 字符
    - `step_warn` 含"跳过" → 用户拒绝
  - PostToolUse hooks（1076）
  - append tool_result（1077-1081）
- **返回值**：`bool`（是否被拦截/重定向，调用方据此 `continue`）
- **yield 行为**：是。yield `step_warn` / `command` / 权限回调事件。
- **依赖的实例方法**：`self._check_doom_loop` / `self._agent_manager.get_permission` / `self._check_permission_with_callback` / `self._tool_registry.get` / `self._execute_run_command` / `self._hook_manager.run_post_hooks` / `self._emit_event`
- **风险点**：
  - 编译重定向分支的 PostHook 用原 `name="run_command"` + 原 args（line 1053-1054 注释强调），不可改成 embed_build。
  - stdout/stderr 截断 `[-2000:]` 与 None 降级 `or ""` 必须保留。
  - `cmd_result` 初始化的 `returncode=-1` 默认值保留。
- **测试验证**：
  - `tests/test_engine.py::TestCommandRiskEvaluation::test_safe_commands`
  - `tests/test_engine.py::TestCommandRiskEvaluation::test_dangerous_commands`
  - 需新增：`test_run_command_build_redirect_to_embed_build`
  - 需新增：`test_run_command_doom_loop_blocked`

### 3.10 `_handle_read_file_tool(call_id, args, tool_results)`

- **来源行号**：1083-1098
- **提取内容**：
  - 执行 `_execute_read_file(args)` 消费事件（1085-1091）：
    - `file_read` → 截断 content 到 20000 字符
    - `error` → 记录错误
  - PostToolUse hooks（1093）
  - append tool_result（1094-1098）
- **返回值**：无（直接 mutate `tool_results`）
- **yield 行为**：是。yield `file_read` / `error`。
- **依赖的实例方法**：`self._execute_read_file` / `self._hook_manager.run_post_hooks`
- **风险点**：
  - content 截断 `[:20000]` 与 None 降级 `or ""`（line 1089）保留。
  - read_file 不走只读并行路径（在 if/elif 链中是独立 elif，不是 else 分支的只读并行），此差异必须保留。
- **测试验证**：
  - 需新增：`test_read_file_returns_content`
  - 需新增：`test_read_file_error_path`
  - 需新增：`test_read_file_content_truncated`

### 3.11 `_handle_external_tool(call, call_id, name, args, tool_results, _pending_readonly)`

- **来源行号**：1100-1203
- **提取内容**：注册的外部工具分发
  - 工具未注册 → yield `step_warn` + append 错误 tool_result（1197-1203）
  - 已注册：
    - doom_loop 检测（1105-1112）
    - 只读/写权限判定（1114-1127）：read 或 bash
    - **只读工具并行路径**（1128-1145）：
      - 创建 `asyncio.ensure_future(tool.safe_execute(...))` 任务
      - append 到 `_pending_readonly`，跳过串行执行
    - **破坏性外部工具授权**（1147-1167）：`_EXTERNAL_WRITE_TOOLS` + ask/auto 分支
    - **串行执行**（1168-1196）：
      - `tool.safe_execute(args, context)` + 异常捕获
      - None 防御
      - PostToolUse hooks
      - append tool_result
      - `task_track` 成功时 yield `step_done`（1195-1196）
- **返回值**：`bool`（是否被拦截或转入并行，调用方据此 `continue`）
- **yield 行为**：是。yield `step_warn` / `step_done` / 权限回调事件。
- **依赖的实例方法**：`self._tool_registry.has` / `self._check_doom_loop` / `self._is_readonly_tool` / `self._agent_manager.get_permission` / `self._check_permission_with_callback` / `self._hook_manager.run_post_hooks` / `self._emit_event`
- **依赖的常量**：`_EXTERNAL_WRITE_TOOLS`（模块级）
- **风险点**：
  - 只读并行任务的 `asyncio.ensure_future` 必须用当前事件循环，不可改成 `asyncio.create_task`（兼容性）。
  - `_pending_readonly` 是跨方法共享的可变列表，作为参数传入被 mutate。
  - `task_track` 的特殊 `step_done` 事件（line 1195-1196）不可遗漏。
  - 破坏性工具 auto 模式的 `logging.warning` 不阻塞执行（line 1164-1167）。
- **测试验证**：
  - `tests/test_engine_integration.py::TestProcessEndToEnd::test_unknown_tool_returns_error`
  - `tests/test_engine_integration.py::TestProcessEndToEnd::test_find_files_returns_results`（只读外部工具）
  - `tests/test_engine_integration.py::TestProcessEndToEnd::test_search_code_finds_pattern`（只读并行）
  - `tests/test_engine.py::TestFlushReadonlyLogging::test_single_task_exception_logged`
  - 需新增：`test_external_tool_destructive_ask_denied`

### 3.12 `_handle_post_step(step, resp, tool_results, tool_calls, _should_terminate, _chat_message)`

- **来源行号**：1214-1300
- **提取内容**：
  - chat 终止检查（1215-1219）：`_should_terminate` → append chat_message 到 conversation + break 信号
  - 任务完成检测（1221-1236）：`_check_task_completion` + append system tool_result
  - 步数预警（1238-1257）：remaining==5 / remaining==1 分支
  - 对话历史 append（1259-1267）：assistant 消息（含 tool_calls）+ tool_results
  - 失败工具检测（1269-1284）：JSON 解析 `success is False` + yield `step_done`
  - Stop Hooks 收敛检测（1286-1300）：`_stop_hooks.check_all` + yield `stop_hook` + `chat_response` + break 信号
- **返回值**：`tuple[bool, bool]` → `(should_break, stop_triggered)`
  - `should_break=True` 表示应 break 主循环（chat 终止 或 stop_hook 触发）
- **yield 行为**：是。yield `step_done` / `stop_hook` / `chat_response`。
- **依赖的实例方法**：`self._check_task_completion` / `self._stop_hooks.check_all` / `self._emit_event`
- **依赖的实例属性**：`self.conversation` / `self.MAX_STEPS` / `self._recent_calls`
- **风险点**：
  - assistant 消息的 `tool_calls` 字段仅当 `hasattr(resp, 'tool_calls') and resp.tool_calls` 时附加（line 1262），保留。
  - 失败检测的 JSON 解析容错（`try/except (json.JSONDecodeError, TypeError)`）保留。
  - stop_hook 触发后的 `chat_response` 是强制收尾（line 1296-1299），不可遗漏。
  - 此方法不处理 `for...else` 的 max_steps 分支（由 3.13 处理）。
- **测试验证**：
  - `tests/test_engine_integration.py::TestProcessEndToEnd::test_tool_call_then_chat`（chat 终止）
  - `tests/test_engine_integration.py::TestProcessEndToEnd::test_max_steps_limit`（步数预警路径）
  - `tests/test_engine_integration.py::TestProcessEndToEnd::test_phase_events_emitted`
  - 需新增：`test_post_step_detects_failed_tools`
  - 需新增：`test_post_step_stop_hook_breaks`

### 3.13 `_handle_max_steps_exceeded()`

- **来源行号**：1303-1312
- **提取内容**：`for...else` 的 else 分支
  - yield `step_warn`（步数上限提示）
  - yield `chat_response`（强制收尾，含 `_build_progress_summary()`）
- **返回值**：无
- **yield 行为**：是。yield `step_warn` / `chat_response`。
- **依赖的实例方法**：`self._emit_event` / `self._build_progress_summary`
- **依赖的实例属性**：`self.MAX_STEPS`
- **风险点**：此方法仅在 `for step` 正常耗尽（未 break）时触发，调用方需用 `for...else` 结构或标志位判断。
- **测试验证**：
  - `tests/test_engine_integration.py::TestProcessEndToEnd::test_max_steps_limit`

### 3.14 `_handle_summary_and_persist(user_input, step, all_files_created, all_files_modified)`

- **来源行号**：1314-1342
- **提取内容**：
  - yield `phase=DONE`（1315）
  - 文件清单非空时 yield `summary` + `file_tree`（1316-1321）
  - 保存会话检查点（1323-1333）：`self._memory.save_checkpoint`，OSError 容错
  - 任务进度持久化（1335-1342）：`task_track.save_to_file`，OSError 容错
- **返回值**：无
- **yield 行为**：是。yield `phase` / `summary` / `file_tree`。
- **依赖的实例方法**：`self._emit_event` / `self._build_file_tree` / `self._memory.save_checkpoint` / `self._tool_registry.get`
- **依赖的实例属性**：`self._compactor.last_summary` / `self._project_dir`
- **风险点**：
  - `step` 变量在 `for...else` 之后可能未定义（若 MAX_STEPS=0），需用 `step` 默认值或 `getattr`。原代码 line 1326 用 `step + 1`，若循环未执行会 NameError——保留原行为（不引入新修复，超出本 Track 范围）。
  - `task_track` 工具可能未注册（line 1338 `if task_tool is not None`），保留 None 检查。
- **测试验证**：
  - `tests/test_engine_integration.py::TestProcessEndToEnd::test_chat_response_terminates_loop`（验证 DONE phase）
  - `tests/test_engine_integration.py::TestProcessEndToEnd::test_write_file_creates_file`（验证 summary + file_tree）
  - `tests/test_engine.py::TestLastSummaryProperty::test_last_summary_property`

---

## 4. 重构后的 process() 骨架

> 目标行数 ≤ 150 行。以下为重构后的 `process()` 完整骨架（含注释约 130 行）。

```python
async def process(self, user_input: str):
    """主处理流程 — Agentic Loop（参考 OpenCode 架构）

    核心模式：
    1. 用户输入 → AI 返回工具调用
    2. 执行工具 → 收集结果（含错误）
    3. 把结果送回 AI → AI 决定下一步
    4. 循环直到 AI 不再调工具，或达到最大步数
    """
    # 会话前置初始化：记忆整理 / 状态重置 / skill / MCP / 缓存命中
    async for ev in self._init_session(user_input):
        yield ev
    # _init_session 返回的上下文（通过实例属性或返回元组）
    system = self._current_system
    _effective_tools = self._current_effective_tools
    all_files_created = self._current_files_created
    all_files_modified = self._current_files_modified

    _should_terminate = False
    _chat_message = None

    for step in range(self.MAX_STEPS):
        # 思考阶段：流式生成 + fallback，返回 None 表示终止
        messages = await self._compactor.compact_pipeline(self.conversation, system)
        resp = await self._handle_thinking_phase(system, messages, step, _effective_tools)
        if resp is None:
            return  # 流式+非流式均失败，error 已 yield

        # 解析工具调用
        tool_calls = self._parse_tool_calls(resp)

        # 无工具调用 → 纯聊天回复，循环结束
        if not tool_calls:
            if step == 0:
                yield await self._emit_event("phase", {"phase": Phase.CHAT.value})
            yield await self._emit_event("chat_response", {"message": resp.content or ""})
            self.conversation.append({"role": "assistant", "content": resp.content or ""})
            break

        # 执行工具调用
        yield await self._emit_event("phase", {"phase": Phase.EXECUTE.value})
        tool_results = []

        # 只读 Agent 工具过滤
        async for ev in self._filter_tool_calls_by_permission(tool_calls):
            yield ev
        tool_calls, _blocked_results = self._last_filter_result
        tool_results.extend(_blocked_results)

        # 只读工具并行任务队列
        _pending_readonly: list = []

        try:
            for idx, call in enumerate(tool_calls):
                name = call.get("name", "")
                args = call.get("arguments", {})
                call_id = call.get("id") or f"call_{idx}"

                # 写工具执行前 flush 只读并行任务（保证顺序）
                if not self._is_readonly_tool(name, args) and _pending_readonly:
                    await self._flush_readonly_tasks(_pending_readonly, tool_results)

                # 三段式前置门控（黑名单 / 规则 / PreHook），chat 豁免
                if name != "chat":
                    should_skip, modified_args, gate_events = await self._check_pre_tool_gates(
                        call, call_id, tool_results
                    )
                    async for ev in gate_events:
                        yield ev
                    if should_skip:
                        continue
                    if modified_args is not None:
                        args = modified_args

                # 工具分发路由器
                _terminate, _msg = await self._dispatch_tool_call(
                    call, idx, call_id, name, args, tool_results,
                    _pending_readonly, all_files_created, all_files_modified,
                )
                # 转发分发器内部 yield 的事件（实际通过 async for 在上方消费）
                if _terminate:
                    _should_terminate = True
                    _chat_message = _msg

            # 循环结束后 flush 剩余只读并行任务
            if _pending_readonly:
                await self._flush_readonly_tasks(_pending_readonly, tool_results)
        finally:
            # 异常退出时取消未完成的只读并行任务（防止孤儿任务泄漏）
            for _, _, _, _t in _pending_readonly:
                if not _t.done():
                    _t.cancel()

        # 循环后处理：终止检查 / 任务完成 / 步数预警 / 对话历史 / stop_hooks
        should_break = await self._handle_post_step(
            step, resp, tool_results, tool_calls, _should_terminate, _chat_message
        )
        async for ev in self._post_step_events:
            yield ev
        if should_break:
            break

    else:
        # 达到最大步数（安全网触发）
        async for ev in self._handle_max_steps_exceeded():
            yield ev

    # 总结与持久化
    async for ev in self._handle_summary_and_persist(
        user_input, step, all_files_created, all_files_modified
    ):
        yield ev
```

> **实现备注**：
> - 上例中 `_current_system` / `_last_filter_result` / `_post_step_events` 等实例属性用于跨方法传递上下文。**实际实现时优先用返回元组**，仅在 async generator 无法直接返回值时用实例属性（Python 3.8+ 的 `StopIteration.value` 可在 `async for` 后通过捕获获取，但可读性差）。
> - 推荐策略：**yield 事件的方法返回 `(events_list, return_value)` 元组**，调用方先 `await` 收集 events 再取返回值；或拆成两个方法（一个纯 yield，一个纯计算）。
> - 最终实现风格由实施者根据可读性决定，但必须满足"yield 顺序不变 + tool_results 累积不变"硬约束。

---

## 5. 实施步骤（按顺序执行，每步带验证）

> 每步独立 commit，便于 `git bisect` 定位回归。每步失败可 `git reset --hard <tag>` 回滚。

### Step 0: 准备基线

- **操作**：
  ```powershell
  git checkout main
  git pull
  git tag pre-engine-split
  git checkout -b refactor/track-1-engine-split
  ```
- **验证**：
  ```powershell
  pytest tests/test_engine.py tests/test_engine_integration.py -v
  ```
  记录基线通过数（应 ≥ 现有总数，约 738+）。
- **目的**：建立回滚点 + 独立分支。

### Step 1: 提取 `_handle_max_steps_exceeded`（最简单，热身）

- **操作**：
  - 将 line 1303-1312 的 `else` 分支提取为 `async def _handle_max_steps_exceeded(self)`（AsyncGenerator）。
  - `process()` 中改为：
    ```python
    else:
        async for ev in self._handle_max_steps_exceeded():
            yield ev
    ```
- **验证**：
  ```powershell
  pytest tests/test_engine_integration.py::TestProcessEndToEnd::test_max_steps_limit -v
  pytest tests/test_engine.py -v
  pytest tests/test_engine_integration.py -v
  ```
- **失败回滚**：
  ```powershell
  git reset --hard pre-engine-split
  ```
- **commit**：`refactor(engine): extract _handle_max_steps_exceeded`
- **tag**：`git tag step-1-max-steps`

### Step 2: 提取 `_handle_summary_and_persist`

- **操作**：
  - 将 line 1314-1342 提取为 `async def _handle_summary_and_persist(self, user_input, step, all_files_created, all_files_modified)`。
  - 注意 `step` 变量作用域：若 `for...else` 未进入循环（MAX_STEPS=0），`step` 未定义——保留原行为（不修复，超出本 Track）。
- **验证**：
  ```powershell
  pytest tests/test_engine_integration.py::TestProcessEndToEnd::test_chat_response_terminates_loop -v
  pytest tests/test_engine_integration.py::TestProcessEndToEnd::test_write_file_creates_file -v
  pytest tests/test_engine.py -v
  pytest tests/test_engine_integration.py -v
  ```
- **失败回滚**：`git reset --hard step-1-max-steps`
- **commit**：`refactor(engine): extract _handle_summary_and_persist`
- **tag**：`git tag step-2-summary`

### Step 3: 提取 `_filter_tool_calls_by_permission`

- **操作**：
  - 将 line 716-740 提取为 `async def _filter_tool_calls_by_permission(self, tool_calls)`，返回 `(filtered_calls, blocked_results)`，同时 yield `tool_blocked` 事件。
  - 用元组返回 + 单独 yield 的混合模式，或拆成两方法。
- **验证**：
  ```powershell
  pytest tests/test_engine_integration.py -v
  pytest tests/test_engine.py -v
  ```
  + 新增 `test_filter_tool_calls_readonly_blocks_write` / `test_filter_tool_calls_coder_allows_all`。
- **失败回滚**：`git reset --hard step-2-summary`
- **commit**：`refactor(engine): extract _filter_tool_calls_by_permission`
- **tag**：`git tag step-3-filter`

### Step 4: 提取 `_handle_thinking_phase`

- **操作**：
  - 将 line 624-698 提取为 `async def _handle_thinking_phase(self, system, messages, step, effective_tools)`。
  - 返回 `LLMResponse | None`（None 表示应终止 process）。
  - 流式 + fallback 三态逻辑完整搬移。
- **验证**：
  ```powershell
  pytest tests/test_engine_integration.py::TestProcessEndToEnd::test_thinking_event_emitted -v
  pytest tests/test_engine_integration.py::TestProcessEndToEnd::test_phase_events_emitted -v
  pytest tests/test_engine_integration.py::TestProcessEndToEnd::test_chat_response_terminates_loop -v
  pytest tests/test_engine.py -v
  pytest tests/test_engine_integration.py -v
  ```
  + 新增 `test_thinking_phase_streaming_fallback` / `test_thinking_phase_partial_chunks`。
- **失败回滚**：`git reset --hard step-3-filter`
- **commit**：`refactor(engine): extract _handle_thinking_phase`
- **tag**：`git tag step-4-thinking`

### Step 5: 提取 `_handle_chat_tool` + `_handle_read_file_tool`（低风险两个）

- **操作**：
  - 提取 line 835-841 为 `_handle_chat_tool(self, args)` → 返回 `_chat_message` 字符串。
  - 提取 line 1083-1098 为 `_handle_read_file_tool(self, call_id, args, tool_results)`。
- **验证**：
  ```powershell
  pytest tests/test_engine_integration.py::TestProcessEndToEnd::test_chat_response_terminates_loop -v
  pytest tests/test_engine_integration.py::TestProcessEndToEnd::test_tool_call_then_chat -v
  pytest tests/test_engine.py -v
  pytest tests/test_engine_integration.py -v
  ```
- **失败回滚**：`git reset --hard step-4-thinking`
- **commit**：`refactor(engine): extract _handle_chat_tool and _handle_read_file_tool`
- **tag**：`git tag step-5-chat-read`

### Step 6: 提取 `_handle_write_file_tool`

- **操作**：提取 line 843-912。
- **验证**：
  ```powershell
  pytest tests/test_engine_integration.py::TestProcessEndToEnd::test_write_file_creates_file -v
  pytest tests/test_engine.py::TestDoomLoop -v
  pytest tests/test_engine.py -v
  pytest tests/test_engine_integration.py -v
  ```
  + 新增 `test_write_file_chat_content_blocked`。
- **失败回滚**：`git reset --hard step-5-chat-read`
- **commit**：`refactor(engine): extract _handle_write_file_tool`
- **tag**：`git tag step-6-write`

### Step 7: 提取 `_handle_edit_file_tool`

- **操作**：提取 line 914-994。
- **验证**：
  ```powershell
  pytest tests/test_engine.py::TestUndoWithOldContent -v
  pytest tests/test_engine.py -v
  pytest tests/test_engine_integration.py -v
  ```
  + 新增 `test_edit_file_doom_loop_blocked`。
- **失败回滚**：`git reset --hard step-6-write`
- **commit**：`refactor(engine): extract _handle_edit_file_tool`
- **tag**：`git tag step-7-edit`

### Step 8: 提取 `_handle_run_command_tool`

- **操作**：提取 line 996-1081。
- **验证**：
  ```powershell
  pytest tests/test_engine.py::TestCommandRiskEvaluation -v
  pytest tests/test_engine.py -v
  pytest tests/test_engine_integration.py -v
  ```
  + 新增 `test_run_command_build_redirect_to_embed_build`。
- **失败回滚**：`git reset --hard step-7-edit`
- **commit**：`refactor(engine): extract _handle_run_command_tool`
- **tag**：`git tag step-8-run`

### Step 9: 提取 `_handle_external_tool`

- **操作**：提取 line 1100-1203（含只读并行 + 破坏性授权）。
- **验证**：
  ```powershell
  pytest tests/test_engine_integration.py::TestProcessEndToEnd::test_find_files_returns_results -v
  pytest tests/test_engine_integration.py::TestProcessEndToEnd::test_search_code_finds_pattern -v
  pytest tests/test_engine_integration.py::TestProcessEndToEnd::test_unknown_tool_returns_error -v
  pytest tests/test_engine.py::TestFlushReadonlyLogging -v
  pytest tests/test_engine.py -v
  pytest tests/test_engine_integration.py -v
  ```
  + 新增 `test_external_tool_destructive_ask_denied`。
- **失败回滚**：`git reset --hard step-8-run`
- **commit**：`refactor(engine): extract _handle_external_tool`
- **tag**：`git tag step-9-external`

### Step 10: 提取 `_check_pre_tool_gates`

- **操作**：提取 line 757-833（三段式门控：黑名单 + 规则 + PreHook）。
- **验证**：
  ```powershell
  pytest tests/test_engine.py -v
  pytest tests/test_engine_integration.py -v
  ```
  + 新增 `test_pre_tool_gates_blacklist_deny` / `test_pre_tool_gates_rule_deny` / `test_pre_tool_gates_hook_modify_args` / `test_pre_tool_gates_chat_exempt`。
- **失败回滚**：`git reset --hard step-9-external`
- **commit**：`refactor(engine): extract _check_pre_tool_gates`
- **tag**：`git tag step-10-gates`

### Step 11: 提取 `_dispatch_tool_call` 路由器

- **操作**：将 line 835-1203 的 if/elif 链替换为对 step 5-9 子方法的路由调用。
- **验证**：
  ```powershell
  pytest tests/test_engine.py -v
  pytest tests/test_engine_integration.py -v
  ```
- **失败回滚**：`git reset --hard step-10-gates`
- **commit**：`refactor(engine): extract _dispatch_tool_call router`
- **tag**：`git tag step-11-dispatch`

### Step 12: 提取 `_handle_post_step`

- **操作**：提取 line 1214-1300。
- **验证**：
  ```powershell
  pytest tests/test_engine_integration.py::TestProcessEndToEnd::test_tool_call_then_chat -v
  pytest tests/test_engine_integration.py::TestProcessEndToEnd::test_max_steps_limit -v
  pytest tests/test_engine.py -v
  pytest tests/test_engine_integration.py -v
  ```
  + 新增 `test_post_step_detects_failed_tools` / `test_post_step_stop_hook_breaks`。
- **失败回滚**：`git reset --hard step-11-dispatch`
- **commit**：`refactor(engine): extract _handle_post_step`
- **tag**：`git tag step-12-post-step`

### Step 13: 提取 `_init_session`

- **操作**：提取 line 553-621（前置初始化）。
- **验证**：
  ```powershell
  pytest tests/test_engine.py -v
  pytest tests/test_engine_integration.py -v
  ```
- **失败回滚**：`git reset --hard step-12-post-step`
- **commit**：`refactor(engine): extract _init_session`
- **tag**：`git tag step-13-init`

### Step 14: 最终清理与行数校验

- **操作**：
  - 检查 `process()` 行数 ≤ 150。
  - 检查每个子方法行数 ≤ 100。
  - 移除原内联注释中已过时的段落（保留设计意图注释）。
  - 运行 AST 行数分析脚本（见 §6）。
- **验证**：
  ```powershell
  pytest tests/ -v
  ```
  总数应 ≥ 738 passed。
- **commit**：`refactor(engine): finalize process() split, cleanup`
- **tag**：`git tag track-1-done`

### Step 15: 合并到 main

- **操作**：
  ```powershell
  git checkout main
  git merge --no-ff refactor/track-1-engine-split
  ```
  保留完整 commit 历史便于 bisect。
- **验证**：CI 全绿 + 手动冒烟测试（一次完整对话 + 一次写文件 + 一次 run_command）。

---

## 6. 验证清单

### 6.1 功能验证

- [ ] `process()` 行数 ≤ 150（AST 分析脚本，见下）
- [ ] 每个子方法行数 ≤ 100
- [ ] `pytest tests/test_engine.py -v` 全绿
- [ ] `pytest tests/test_engine_integration.py -v` 全绿
- [ ] `pytest tests/ -v` 总数 ≥ 738 passed
- [ ] `grep` 确认 `process()` 内不再有工具分发 `if/elif`（即不再出现 `name == "chat"` / `name == "write_file"` 等字面量比较）
  ```powershell
  Select-String -Path iron\agent\engine.py -Pattern 'name == "(chat|write_file|edit_file|run_command|read_file)"' | Select-Object LineNumber, Line
  ```
  上述命令在 `process()` 函数体范围内应无匹配（子方法内可以有）。

### 6.2 行为等价验证

- [ ] 手动对比重构前后的事件序列：对同一 user_input，重构前后 yield 的 `AgentEvent` 列表（type + data）完全一致。
- [ ] 手动对比重构前后的 `tool_results` 列表：`tool_call_id` / `role` / `content` 逐字段一致。
- [ ] 验证 `finally` 块仍能取消孤儿只读任务（构造一个会抛异常的工具调用，检查 `_pending_readonly` 任务被 cancel）。

### 6.3 AST 行数分析脚本

新建 `scripts/check_engine_split.py`（一次性脚本，不入库）：

```python
"""检查 engine.py process() 及子方法的行数约束。"""
import ast
import sys
from pathlib import Path

TARGET = Path("iron/agent/engine.py")
MAX_PROCESS = 150
MAX_SUBMETHOD = 100

tree = ast.parse(TARGET.read_text(encoding="utf-8"))

for node in ast.walk(tree):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        if node.name == "process":
            lines = node.end_lineno - node.lineno + 1
            status = "OK" if lines <= MAX_PROCESS else "FAIL"
            print(f"[{status}] process(): {lines} lines (limit {MAX_PROCESS})")
        elif node.name.startswith("_handle_") or node.name.startswith("_dispatch_") \
             or node.name in ("_init_session", "_filter_tool_calls_by_permission",
                              "_check_pre_tool_gates", "_handle_max_steps_exceeded",
                              "_handle_summary_and_persist", "_handle_post_step"):
            lines = node.end_lineno - node.lineno + 1
            status = "OK" if lines <= MAX_SUBMETHOD else "FAIL"
            print(f"[{status}] {node.name}(): {lines} lines (limit {MAX_SUBMETHOD})")
```

运行：
```powershell
python scripts/check_engine_split.py
```

### 6.4 回归测试矩阵

| 测试文件 | 用例 | 覆盖路径 |
|----------|------|----------|
| test_engine_integration.py | test_chat_response_terminates_loop | chat 终止 / DONE phase |
| test_engine_integration.py | test_tool_call_then_chat | 工具调用后 chat 收尾 |
| test_engine_integration.py | test_write_file_creates_file | write_file / summary / file_tree |
| test_engine_integration.py | test_find_files_returns_results | 只读外部工具并行 |
| test_engine_integration.py | test_search_code_finds_pattern | 只读并行 / flush |
| test_engine_integration.py | test_doom_loop_blocked | doom_loop 检测 |
| test_engine_integration.py | test_max_steps_limit | for...else 兜底 |
| test_engine_integration.py | test_unknown_tool_returns_error | 外部工具 else 分支 |
| test_engine_integration.py | test_thinking_event_emitted | thinking 事件 |
| test_engine_integration.py | test_phase_events_emitted | phase 事件序列 |
| test_engine.py | TestDoomLoop | doom_loop 单测 |
| test_engine.py | TestReadonlyToolClassification | 只读判定 |
| test_engine.py | TestUndoWithOldContent | edit_file 撤销历史 |
| test_engine.py | TestFlushReadonlyLogging | flush 异常处理 |
| test_engine.py | TestLastSummaryProperty | last_summary 属性 |

---

## 7. 回滚策略

### 7.1 Tag 序列

每步独立 tag，便于 `git bisect` 或回滚：

| Tag | 含义 |
|-----|------|
| `pre-engine-split` | 重构前基线（回滚终点） |
| `step-1-max-steps` | Step 1 完成 |
| `step-2-summary` | Step 2 完成 |
| `step-3-filter` | Step 3 完成 |
| `step-4-thinking` | Step 4 完成 |
| `step-5-chat-read` | Step 5 完成 |
| `step-6-write` | Step 6 完成 |
| `step-7-edit` | Step 7 完成 |
| `step-8-run` | Step 8 完成 |
| `step-9-external` | Step 9 完成 |
| `step-10-gates` | Step 10 完成 |
| `step-11-dispatch` | Step 11 完成 |
| `step-12-post-step` | Step 12 完成 |
| `step-13-init` | Step 13 完成 |
| `track-1-done` | Track 1 全部完成 |

### 7.2 回滚命令

- 回滚单步：
  ```powershell
  git reset --hard step-N-xxx
  ```
- 回滚到基线：
  ```powershell
  git reset --hard pre-engine-split
  ```
- bisect 定位回归：
  ```powershell
  git bisect start
  git bisect bad HEAD
  git bisect good pre-engine-split
  # 跑测试，标记 good/bad，直到定位到具体 commit
  ```

### 7.3 风险等级与回滚优先级

| 步骤 | 风险等级 | 原因 |
|------|----------|------|
| Step 1 (max_steps) | 低 | 独立 else 分支，无状态 |
| Step 2 (summary) | 低 | 循环后纯 yield |
| Step 3 (filter) | 中 | 涉及 tool_results 累积 |
| Step 4 (thinking) | **高** | 流式/fallback 三态，yield 顺序敏感 |
| Step 5 (chat/read) | 低 | 小块独立 |
| Step 6 (write) | 中 | 权限 + 拦截 + 事件消费 |
| Step 7 (edit) | 中 | 撤销历史状态 |
| Step 8 (run) | **高** | 编译重定向分支 |
| Step 9 (external) | **高** | 只读并行 + 破坏性授权 |
| Step 10 (gates) | **高** | 三段式门控组合 |
| Step 11 (dispatch) | 中 | 路由器整合 |
| Step 12 (post_step) | **高** | stop_hooks + 失败检测 |
| Step 13 (init) | 中 | MCP + 缓存命中 |

---

## 8. 与其他 Track 的接口契约

### 8.1 Track 3（backend.py chunk_buffer）

- **接入点**：`_handle_thinking_phase`（3.2）中的流式 `chat_chunk` 累积逻辑。
- **当前实现**：`_accumulated_chunks.append(event_data)` + 局部 `"".join()`。
- **Track 3 完成后**：替换为 `self._chunk_buffer.append(event_data)`，由 backend 维护缓冲区。
- **契约**：本 Track 提取 `_handle_thinking_phase` 后，其内部仍直接操作 `_accumulated_chunks`；Track 3 在此方法内替换为 chunk_buffer 调用，**不改变 `_handle_thinking_phase` 的返回值与 yield 行为**。
- **依赖方向**：Track 3 依赖本 Track 完成（否则要在 802 行 process() 内改 chunk_buffer，风险过大）。

### 8.2 Track 1.2（LSP 集成）

- **接入点**：
  - `_handle_write_file_tool`（3.7）：文件写入后发 `did_change` 通知
  - `_handle_edit_file_tool`（3.8）：文件编辑后发 `did_change` 通知
  - `_handle_read_file_tool`（3.10）：文件首次读取时触发 `did_open`（可选）
- **当前实现**：无 LSP 钩子。
- **Track 1.2 完成后**：在上述子方法的 PostToolUse hooks 之后、append tool_result 之前插入 `await self._lsp_manager.notify_change(path)`。
- **契约**：本 Track 提取子方法后，Track 1.2 只需在 3 个 `_handle_*_tool` 方法内加钩子，**不触碰 `process()` 主体**。
- **依赖方向**：Track 1.2 的 engine.py 部分依赖本 Track 完成。

### 8.3 Track 1.4（其他 engine.py 重构）

- **接入点**：`_handle_post_step`（3.12）的 stop_hooks、`_check_pre_tool_gates`（3.4）的 hook 链。
- **契约**：本 Track 完成后，`process()` 主体 ≤ 150 行，Track 1.4 在子方法层级继续优化（如把 stop_hooks 拆到独立模块），不再触碰 `process()`。
- **依赖方向**：Track 1.4 依赖本 Track 完成。

### 8.4 并行兼容性

- 本 Track 与 Track 2（CLI UI 层）**无冲突**：Track 2 消费 `AgentEvent`，本 Track 保持事件序列不变，UI 层无感知。
- 本 Track 与 Track 5（MCP 增强）在 `_init_session` 的 MCP 连接块（line 601-621）有交集：本 Track 仅搬运不改逻辑，Track 5 后续在 `_init_session` 内扩展 MCP 工具过滤。

---

## 附录 A：关键行号速查表

| 标识 | 行号 | 说明 |
|------|------|------|
| process 起始 | 542 | `async def process(self, user_input: str):` |
| dream/distill | 555-558 | 记忆整理 |
| skill 匹配 | 570-577 | `_match_skills` |
| 缓存命中 yield | 590-594 | `cache_hit` 事件 |
| MCP 连接 | 601-621 | 首次连接 + schema 重建 |
| for step 循环 | 623 | `for step in range(self.MAX_STEPS):` |
| thinking yield | 632-639 | thinking + phase=THINK |
| 流式生成 | 644-670 | stream_generate |
| fallback 三态 | 678-698 | chunk 恢复 / 非流式 / error |
| 无工具调用 break | 700-710 | 纯聊天 |
| 只读过滤 | 716-740 | `_get_allowed_tools` |
| for idx, call 循环 | 747 | 工具分发循环 |
| 写前 flush | 754-755 | `_flush_readonly_tasks` |
| 黑名单 | 759-776 | `_permission_mgr.check` |
| 规则引擎 | 778-804 | `_rule_engine.evaluate` |
| PreHook | 806-833 | `_hook_manager.run_pre_hooks` |
| chat 分支 | 835-841 | 终止性工具 |
| write_file 分支 | 843-912 | 写文件 |
| edit_file 分支 | 914-994 | 编辑 + 撤销 |
| run_command 分支 | 996-1081 | 命令 + 重定向 |
| read_file 分支 | 1083-1098 | 读文件 |
| 外部工具 else | 1100-1203 | 注册工具 + 并行 + 授权 |
| finally cancel | 1208-1212 | 孤儿任务取消 |
| chat 终止检查 | 1215-1219 | `_should_terminate` |
| 任务完成检测 | 1221-1236 | `_check_task_completion` |
| 步数预警 | 1238-1257 | remaining 5/1 |
| 对话历史 append | 1259-1267 | assistant + tool_results |
| 失败检测 | 1269-1284 | JSON 解析 |
| stop_hooks | 1286-1300 | 收敛检测 + 强制 chat |
| for...else | 1303-1312 | max_steps 兜底 |
| DONE phase | 1315 | `phase=DONE` |
| summary | 1316-1321 | 文件清单 + file_tree |
| checkpoint | 1323-1333 | `_memory.save_checkpoint` |
| task 持久化 | 1335-1342 | `task_track.save_to_file` |
| process 结束 | 1343 | 函数体末行 |

---

## 附录 B：AgentEvent yield 顺序契约（必须保持）

以"工具调用 → 工具执行 → 下一步思考"为例，重构前后必须 yield 完全相同的事件序列：

```
[init]
  cache_hit?            (仅当 prompt_cache 命中)

[step 0]
  thinking              (带 input_tokens)
  phase=THINK
  chat_chunk*           (流式，0 或多个)
  phase=EXECUTE
  tool_blocked*         (只读过滤 + 黑名单 + 规则 + PreHook，0 或多个)
  step_warn*            (doom_loop / 权限拒绝 / 规则拒绝，0 或多个)
  file_done | command | file_read | error*   (工具执行事件)
  step_done             (步骤完成，可能带失败计数)
  stop_hook?            (收敛检测命中时)
  chat_response?        (stop_hook 强制收尾)

[step 1...N]
  thinking              (不带 input_tokens)
  phase=THINK
  ... (同上)

[终止]
  phase=CHAT            (仅当 step 0 无工具调用)
  chat_response         (最终回复)
  OR
  step_warn + chat_response  (max_steps 兜底)

[收尾]
  phase=DONE
  summary?              (仅当有文件变更)
  file_tree?            (仅当有文件变更)
```

> **验证方法**：重构前后分别跑同一测试用例，捕获全部 AgentEvent 到列表，`assert event_list_before == event_list_after`。

---

## 附录 C：新增测试用例清单

| 测试文件 | 用例名 | 覆盖子方法 |
|----------|--------|------------|
| test_engine.py | test_filter_tool_calls_readonly_blocks_write | 3.3 |
| test_engine.py | test_filter_tool_calls_coder_allows_all | 3.3 |
| test_engine.py | test_pre_tool_gates_blacklist_deny | 3.4 |
| test_engine.py | test_pre_tool_gates_rule_deny | 3.4 |
| test_engine.py | test_pre_tool_gates_hook_modify_args | 3.4 |
| test_engine.py | test_pre_tool_gates_chat_exempt | 3.4 |
| test_engine.py | test_write_file_chat_content_blocked | 3.7 |
| test_engine.py | test_edit_file_doom_loop_blocked | 3.8 |
| test_engine.py | test_run_command_build_redirect_to_embed_build | 3.9 |
| test_engine.py | test_read_file_returns_content | 3.10 |
| test_engine.py | test_read_file_error_path | 3.10 |
| test_engine.py | test_read_file_content_truncated | 3.10 |
| test_engine.py | test_external_tool_destructive_ask_denied | 3.11 |
| test_engine.py | test_post_step_detects_failed_tools | 3.12 |
| test_engine.py | test_post_step_stop_hook_breaks | 3.12 |
| test_engine.py | test_thinking_phase_streaming_fallback | 3.2 |
| test_engine.py | test_thinking_phase_partial_chunks | 3.2 |
| test_engine.py | test_init_session_emits_cache_hit | 3.1 |

> 共 18 个新增用例，确保每个子方法至少有 1 个针对性单测。

---

## 附录 D：实施者备忘

1. **async generator 返回值问题**：Python 的 `async def` + `yield` 方法无法用 `return value` 返回值（会 raise `StopIteration` with value，但 `async for` 不直接暴露）。建议策略：
   - 纯 yield 方法：不返回值，调用方从 `tool_results` 等可变参数读取副作用。
   - 需返回值的方法：拆成 `_xxx_yield(self, ...)`（yield 事件）+ `_xxx_calc(self, ...)`（返回值）两个方法，或用 dataclass 封装结果。
2. **`_pending_readonly` 共享**：此列表在 `process()` 的 `try/finally` 作用域内，多个子方法需读写。作为参数传入是安全的（Python 引用语义），但需确保 finally 块仍能访问到它——**不要**把它移到子方法内部。
3. **`all_files_created` / `all_files_modified` 共享**：同上，作为参数传入子方法，子方法直接 `append`。
4. **`step` 变量作用域**：`for...else` 后 `step` 可能未定义（MAX_STEPS=0），`_handle_summary_and_persist` 需要它。原代码有此潜在 bug，本 Track **不修复**（超出范围），但需在文档中标注。
5. **不要合并相邻的 try/except**：原代码每段 except 的异常类型列表都是精心选择的（如 line 696 vs line 1181），合并会改变容错边界。

---

**文档版本**：v1.0
**基线 commit**：`pre-engine-split` tag
**预期完成行数**：process() ≤ 150 行，新增 13 个子方法，新增 18 个测试用例
