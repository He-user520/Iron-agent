# CLI UX 可视化改进计划方案

> **目标**：修复命令补全体验 + 非对话操作清除 + 思考计时稳定性 + 工具执行可视化（模仿 Claude Code）
> **基于调研**：[ui.py](file:///d:/嵌入式-Agent/iron/cli/ui.py) · [main.py](file:///d:/嵌入式-Agent/iron/cli/main.py) · [engine.py](file:///d:/嵌入式-Agent/iron/agent/engine.py)
> **参考**：Claude Code 的 ToolViews + MessageStream + InputArea 设计

---

## 一、问题诊断

### 问题 1：命令补全回车体验

**现状**（[ui.py:1187-1199](file:///d:/嵌入式-Agent/iron/cli/ui.py)）：
```python
@custom_bindings.add("enter", eager=True)
def _(event):
    text = event.current_buffer.text
    if text.startswith("/"):
        matches = _get_matches(text)
        if matches:
            sel = _hint_state["selected"] % len(matches)
            if text not in matches:
                text = matches[sel]  # 只改本地变量
    event.current_buffer.append_to_history()  # 存的是原始输入 "mo"
    event.app.exit(result=text)
```

**缺陷**：
1. `text = matches[sel]` 只修改本地变量，**未更新缓冲区** → 用户看到的还是 `/mo`
2. `append_to_history()` 存的是缓冲区原始文本（`mo`），而非补全后的 `/model` → 按上键回溯看到的是 `mo`
3. 对比 Tab 键（ui.py:1213）会更新 `buffer.text`，行为不一致

**期望**：回车后视图直接显示完整命令 `/model`，历史记录也存 `/model`

### 问题 2：非对话操作污染历史

**现状**（[main.py:538](file:///d:/嵌入式-Agent/iron/cli/main.py)）：
```python
session.add_message("user", text)  # 在命令分发之前注入
if text.startswith("/"):
    cmd = text.split()[0].lower()
    ...
```

**缺陷**：
1. `/model`、`/config`、`/features` 等非对话命令被注入 `session.conversation`，AI 后续能看到
2. 命令执行后只打印一行成功提示，**不清屏**，残留在对话流中
3. FileHistory（`~/.iron/history`）也存了这些命令

**期望**：非对话命令执行后清除该界面，不保留在对话历史

### 问题 3：思考计时重新计时

**现状**（[main.py:975-982](file:///d:/嵌入式-Agent/iron/cli/main.py) + [main.py:642-645](file:///d:/嵌入式-Agent/iron/cli/main.py)）：
```python
# main.py:975-982
if etype == "thinking":
    spinner.start(data.get("message", "思考中..."))  # 每次都 start

# main.py:642-645 (start 方法)
def start(self, message, input_tokens=0):
    self._message = message
    self._start_time = _time.time()   # 重置计时起点！
    self._output_tokens = 0           # 清零输出 token！
```

**缺陷**：
- 每个 thinking 事件都调 `spinner.start()`，重置 `_start_time` 和清零 `_output_tokens`
- 多步 agentic loop 中，`chat_response` 显示的 `⏱ Xs` 只反映**最后一步**耗时，非整个请求总耗时

**期望**：计时从首次启动开始，后续 thinking 只更新消息不重置计时

### 问题 4：思考过程可视化简陋

**现状**：
- spinner 固定显示 "思考中..."，一直转圈
- 工具执行时：
  - 读取文件：**完全静默**（main.py:1039-1042 显式 pass）
  - 写入文件：`{HAMMER} {path} — 写入中...` → `⎿ 写入 {path} (N 行)`
  - 命令执行：`⎿ {cmd} (N 行输出)`
- 没有像 Claude Code 那样的"正在读取 xxx"/"正在修改 xxx"状态行

**期望**（模仿 Claude Code）：
- spinner 简洁，不一直显示"思考中..."
- 读取文件时显示一行状态（如 `⎿ 读取 main.py`）
- 修改文件时显示进度（如 `⎿ 修改 ui.py`）
- 执行命令时显示命令名
- 完成后简洁摘要

---

## 二、设计参考（Claude Code 模式）

基于 [cli-agent-architecture.md](file:///d:/嵌入式-Agent/cli-agent-architecture.md) 调研，Claude Code 的可视化模式：

1. **Thinking 块折叠显示**：思维链用折叠区域呈现，不占主视觉
2. **ToolViews 组件**：专门负责工具执行的 UI 展示
3. **Text 块流式渲染**：直接流式渲染到终端
4. **MessageStream**：消息流主区域
5. **StatusBar**：状态栏

**Iron 适配设计**（不用 React/Ink，用 prompt_toolkit + rich）：

```
用户输入: /model
[命令执行后清屏，不残留]

用户输入: 帮我修改 main.py 的启动逻辑
⠋ 思考中...                                    ← 简洁 spinner
⎿ 读取 main.py (320 行)                        ← 工具状态行
⎿ 读取 ui.py (1493 行)                          ← 工具状态行
⎿ 修改 main.py                                  ← 工具状态行
✓ 已修改 main.py (12 处)                        ← 完成摘要

[流式输出 AI 回复]
...
⏱ 用时 8.3s · ↓ 450 tokens                     ← 总耗时（不重置）
```

---

## 三、分阶段实现计划

### 阶段 1：命令补全回车体验修复（L6 UI）

**目标**：回车后视图显示完整命令 + 历史记录存完整命令

**修改文件**：[ui.py](file:///d:/嵌入式-Agent/iron/cli/ui.py)

**实现要点**：

1. **Enter 键补全时更新缓冲区**（ui.py:1187-1199）：
   ```python
   @custom_bindings.add("enter", eager=True)
   def _(event):
       text = event.current_buffer.text
       if text.startswith("/"):
           matches = _get_matches(text)
           if matches:
               sel = _hint_state["selected"] % len(matches)
               if text not in matches:
                   text = matches[sel]
                   # 新增：更新缓冲区显示，让用户看到完整命令
                   event.current_buffer.text = text
                   event.current_buffer.cursor_position = len(text)
       event.current_buffer.append_to_history()  # 现在存的是完整命令
       event.app.exit(result=text)
   ```

2. **历史记录存完整命令**：`append_to_history()` 在缓冲区更新后调用，自动存完整命令

**验证清单**：
- [ ] 输入 `/mo` 回车后，输入框最后一刻显示 `/model`
- [ ] `~/.iron/history` 中存的是 `/model` 而非 `mo`
- [ ] 按上键回溯历史，看到的是 `/model`
- [ ] Tab 补全行为不受影响

**反模式防护**：
- 不要修改 Tab 补全逻辑（已正确）
- 不要在缓冲区更新后再次触发补全（避免无限循环）

---

### 阶段 2：非对话操作清除界面（L6 UI + L1 入口）

**目标**：非对话命令执行后清屏 + 不注入 session

**修改文件**：[main.py](file:///d:/嵌入式-Agent/iron/cli/main.py) · [commands/system_cmds.py](file:///d:/嵌入式-Agent/iron/cli/commands/system_cmds.py)

**实现要点**：

1. **定义非对话命令集合**（main.py 顶部）：
   ```python
   # 非对话命令：执行后清屏，不注入 session，不记录到对话历史
   NON_CHAT_COMMANDS = {
       "/model", "/config", "/features", "/theme",
       "/clear", "/help", "/quit", "/files",
   }
   ```

2. **修改命令分发逻辑**（main.py:535-545）：
   ```python
   # 原逻辑：先注入 session 再分发
   # 新逻辑：非对话命令跳过 session 注入
   if text.startswith("/"):
       cmd = text.split()[0].lower()
       if cmd in NON_CHAT_COMMANDS:
           # 非对话命令：不注入 session，执行后清屏
           args = text[len(cmd):].strip()
           _dispatch_command(cmd, args, cmd_ctx)
           console.clear()
           ui.show_status_bar(console, ...)
           continue
   # 对话输入才注入 session
   session.add_message("user", text)
   ```

3. **FileHistory 过滤**（ui.py append_to_history 调用处）：
   - 非对话命令不加入 FileHistory（可选，影响较小）

**验证清单**：
- [ ] `/model` 切换后界面清屏，对话历史中无 `/model`
- [ ] `/config` 配置后清屏
- [ ] 对话输入（如"帮我写代码"）仍正常注入 session
- [ ] `/build`、`/flash` 等对话命令仍正常工作（结果注入 session）

**反模式防护**：
- `/build`、`/flash` 结果需注入 session（AI 需感知构建结果），不能误归类
- `/clear` 本身就清屏，不要重复清屏
- 保留 `/help` 的输出（用户需看到帮助）

---

### 阶段 3：思考计时稳定性修复（L6 UI）

**目标**：计时从首次启动开始，后续 thinking 不重置

**修改文件**：[main.py](file:///d:/嵌入式-Agent/iron/cli/main.py) — `_ThinkingSpinner` 类

**实现要点**：

1. **新增 `_first_start` 标志**（main.py:621-634）：
   ```python
   def __init__(self, console):
       ...
       self._first_start_time = 0.0  # 首次启动时间（整个请求）
       self._total_output_tokens = 0  # 整个请求累计 token
       ...

   def start(self, message, input_tokens=0):
       self._message = message
       if self._first_start_time == 0.0:
           # 首次启动：记录起始时间
           self._first_start_time = _time.time()
           self._input_tokens = input_tokens
       # 不重置 _start_time 和 _output_tokens
       self._start_time = self._first_start_time  # 计时基于首次启动
       # 启动 spinner 渲染（如果未启动）
       if self._status is None:
           self._status = self._console.status(...)
           self._status.__enter__()
       # 启动计时线程（如果未启动）
       if self._timer_thread is None:
           ...
   ```

2. **`update()` 不重置计时**（已正确，确认即可）

3. **`stop()` 计算总耗时**（main.py:750-767）：
   ```python
   def stop(self):
       ...
       # 用 _first_start_time 计算总耗时
       if self._first_start_time > 0:
           self._elapsed = _time.time() - self._first_start_time
       # 重置首次启动时间（下次 start 重新开始）
       self._first_start_time = 0.0
       self._total_output_tokens = self._output_tokens
       ...
   ```

4. **`add_tokens()` 累加**（已正确，确认即可）

**验证清单**：
- [ ] 多步 agentic loop 中，`⏱ Xs` 显示总耗时而非最后一步
- [ ] 首次 thinking 启动计时
- [ ] 后续 thinking 只更新消息，不重置计时
- [ ] `↓ N tokens` 显示整个请求累计 token

**反模式防护**：
- 不要在 `update()` 中触碰 `_start_time`
- `stop()` 后必须重置 `_first_start_time`，否则下次请求计时错误

---

### 阶段 4：工具执行可视化（L6 UI — 模仿 Claude Code）

**目标**：读取/修改/执行命令时显示简洁状态行

**修改文件**：[main.py](file:///d:/嵌入式-Agent/iron/cli/main.py) — `_handle_event` 函数

**设计**：新增 ToolActivityBar 概念（不新建类，直接在 `_handle_event` 渲染）

**实现要点**：

1. **读取文件可视化**（main.py:1039-1042）：
   ```python
   # 原逻辑：完全静默 pass
   # 新逻辑：显示一行状态
   elif etype == "file_read":
       path = data.get("path", "")
       # 显示简洁状态行（不停止 spinner，覆盖式显示）
       console.print(f"  [dim cyan]⎿ 读取[/dim cyan] {Path(path).name}", highlight=False)
       # 读取操作不停止 spinner
   ```

2. **工具调用开始时显示**（新增事件处理）：
   - 在 engine.py 工具执行前 yield `tool_start` 事件：
     ```python
     yield "tool_start", {"name": tool_name, "args_summary": ...}
     ```
   - main.py 处理：
     ```python
     elif etype == "tool_start":
         name = data.get("name", "")
         # 显示工具开始（不停止 spinner）
         # 如 "⎿ 读取 main.py" / "⎿ 修改 ui.py" / "⎿ 执行 pio run"
     ```

3. **修改文件可视化**（main.py:1025-1028）：
   ```python
   elif etype == "file_start":
       path = data.get("path", "")
       action = data.get("action", "写入")
       # 简洁状态行
       console.print(f"  [dim cyan]⎿ {action}[/dim cyan] {Path(path).name}", highlight=False)
       # 不重启 spinner，保持计时连续
   ```

4. **命令执行可视化**（main.py:1060-1075）：
   ```python
   elif etype == "command":
       cmd = data.get("command", "")
       # 显示命令开始
       console.print(f"  [dim cyan]⎿ 执行[/dim cyan] {cmd[:50]}", highlight=False)
       # ... 然后显示结果摘要
   ```

5. **完成摘要**（保持现有 `⎿` 前缀风格）：
   ```python
   # file_done
   console.print(f"  [dim green]✓ 写入 {Path(path).name} ({lines} 行)[/dim green]")
   # command 完成
   console.print(f"  [dim green]✓ {cmd[:50]} ({n} 行输出)[/dim green]")
   ```

**视觉规范**：
- 状态行：`  ⎿ 动作 文件名`（dim cyan，2 空格缩进）
- 完成行：`  ✓ 动作 文件名 (详情)`（dim green）
- 不破坏 spinner 计时
- 工具执行不停止 spinner（保持思考状态连续）

**验证清单**：
- [ ] 读取文件时显示 `⎿ 读取 main.py`
- [ ] 修改文件时显示 `⎿ 修改 ui.py` → `✓ 写入 ui.py (12 行)`
- [ ] 执行命令时显示 `⎿ 执行 pio run` → `✓ pio run (15 行输出)`
- [ ] spinner 计时不被工具事件重置
- [ ] 多工具并行执行时状态行不混乱

**反模式防护**：
- 不要显示完整路径（太长影响视觉），只显示文件名
- 不要在工具执行时停止 spinner（保持思考连续性）
- 不要显示工具的完整输出（已有截断机制）
- 不要破坏现有的 `file_done` 注入 session 逻辑

---

### 阶段 5：思考过程可视化精简（L6 UI）

**目标**：spinner 简洁，不一直显示"思考中..."

**修改文件**：[main.py](file:///d:/嵌入式-Agent/iron/cli/main.py) · [engine.py](file:///d:/嵌入式-Agent/iron/agent/engine.py)

**实现要点**：

1. **精简 spinner 显示文本**（main.py `_render_status`）：
   ```python
   @staticmethod
   def _render_status(message, elapsed, output_tokens):
       # 原逻辑：显示 "思考中... (X.Ys · ↓ N)"
       # 新逻辑：精简，只显示 spinner + 计时
       if elapsed < 1.0:
           return f"{message}"  # 第一秒不显示计时
       elif elapsed < 10.0:
           return f"{message} ({elapsed:.1f}s)"
       else:
           return f"{message} ({int(elapsed)}s)"
       # 不显示 token 数（太详细），只在完成时显示
   ```

2. **thinking 消息优化**（engine.py:633-638）：
   ```python
   # 原逻辑：
   # step==0: "正在理解你的需求...（步骤 1）"
   # step>0: "正在处理...（步骤 N）"
   # 新逻辑：精简，不显示步骤号
   if step == 0:
       message = "思考中"  # 简洁
   else:
       message = "继续思考"  # 不显示步骤号
   ```

3. **phase 事件消息优化**（main.py:984-995）：
   ```python
   # 原逻辑：update("正在{label}...")  # 正在思考.../正在执行.../正在回复...
   # 新逻辑：精简
   phase_labels = {
       "THINK": "思考中",
       "EXECUTE": "执行中",
       "DONE": "完成",
       "CHAT": "回复中",
   }
   spinner.update(phase_labels.get(phase, "处理中"))
   ```

4. **移除冗余 thinking 事件**（engine.py）：
   - 流式不完整/失败的 thinking 事件改为日志（不打扰用户）
   - 只在关键步骤 yield thinking

**验证清单**：
- [ ] spinner 显示简洁（`⠋ 思考中 (3.2s)` 而非 `⠋ 正在理解你的需求...（步骤 1）(3.2s · ↓ 0)`)
- [ ] 不显示 token 数（只在完成时显示）
- [ ] 多步执行不显示步骤号
- [ ] 流式失败不打扰用户（日志记录）

**反模式防护**：
- 不要完全移除 spinner（用户需要知道在思考）
- 不要隐藏计时（用户需要感知响应速度）
- 保留 `⏱ 用时 Xs · ↓ N tokens` 完成摘要

---

### 阶段 6：集成验证与测试

**目标**：确保所有改动不破坏现有功能

**验证清单**：

1. **命令补全**：
   - [ ] `/mo` → 回车 → 显示 `/model` → 历史存 `/model`
   - [ ] `/` → 显示 6 个 POPULAR → 上下键选择 → 回车执行
   - [ ] Tab 补全仍正常

2. **非对话命令**：
   - [ ] `/model` 切换后清屏
   - [ ] `/config` 配置后清屏
   - [ ] `/build` 结果仍注入 session（AI 能感知）
   - [ ] 对话输入正常注入 session

3. **思考计时**：
   - [ ] 多步执行 `⏱ Xs` 显示总耗时
   - [ ] 首次启动计时
   - [ ] 后续 thinking 不重置

4. **工具可视化**：
   - [ ] 读取文件显示状态行
   - [ ] 修改文件显示进度
   - [ ] 执行命令显示命令名
   - [ ] 完成摘要简洁

5. **spinner 精简**：
   - [ ] 显示简洁
   - [ ] 不显示 token 数（完成时显示）
   - [ ] 不显示步骤号

6. **回归测试**：
   - [ ] `pytest tests/ -q` 全部通过
   - [ ] 手动测试 `/model`、`/build`、对话流程

---

## 四、实现顺序与依赖

```
阶段 1 (命令补全) ──┐
                    ├─→ 阶段 6 (集成验证)
阶段 2 (非对话清除) ─┤
                    │
阶段 3 (计时修复) ──┼─→ 阶段 5 (spinner 精简)
                    │
阶段 4 (工具可视化) ─┘
```

**建议顺序**：1 → 2 → 3 → 4 → 5 → 6

- 阶段 1-2 独立，可并行
- 阶段 3 是阶段 4-5 的基础（计时修复后才能做可视化）
- 阶段 4 依赖阶段 3（工具事件不重置计时）
- 阶段 5 依赖阶段 3-4（spinner 精简需基于稳定计时）
- 阶段 6 最后集成验证

---

## 五、风险与缓解

| 风险 | 影响 | 缓解 |
|------|------|------|
| 非对话命令误归类 | AI 无法感知 `/build` 结果 | 明确 NON_CHAT_COMMANDS 集合，`/build`/`/flash` 不在其中 |
| 缓冲区更新触发重入 | 补全死循环 | 只在 `text not in matches` 时更新，且更新后立即 exit |
| 计时修复破坏流式 | 流式 token 计数错误 | 保留 `add_tokens()` 逻辑，只改 `start()` 的计时重置 |
| 工具状态行与 spinner 冲突 | 视觉混乱 | 状态行用 `console.print` 独立行，spinner 在底部独立区域 |
| 测试覆盖不足 | 回归风险 | 每阶段完成后跑全量测试 |

---

## 六、预期效果

### 改进前

```
用户输入: /mo
[视图显示 /mo，回车后执行 /model]
✓ 已切换到 mimo-v2.5-pro

用户输入: 帮我修改 main.py
⠋ 正在理解你的需求...（步骤 1）(2.3s · ↓ 0)      ← 冗长
⠋ 正在处理...（步骤 2）(1.5s · ↓ 0)              ← 重置计时
⎿ 写入 main.py — 写入中...
⠋ 生成代码中... (3.2s · ↓ 0)
⎿ 写入 main.py (12 行)
[AI 回复]
⏱ 3.2s · ↓ 50 tokens                            ← 只反映最后一步
```

### 改进后

```
用户输入: /mo
[视图显示 /model，回车执行]
[清屏，无残留]

用户输入: 帮我修改 main.py
⠋ 思考中 (2.3s)                                  ← 简洁
  ⎿ 读取 main.py                                 ← 工具状态行
  ⎿ 修改 main.py
  ✓ 写入 main.py (12 行)
[AI 回复]
⏱ 用时 8.3s · ↓ 450 tokens                       ← 总耗时
```

---

## 七、文档参考

- [evaluation-v3.md](file:///d:/嵌入式-Agent/docs/evaluation-v3.md) — 完整测评报告
- [ARCHITECTURE-v2.md](file:///d:/嵌入式-Agent/docs/ARCHITECTURE-v2.md) — 当前架构文档
- [cli-agent-architecture.md](file:///d:/嵌入式-Agent/cli-agent-architecture.md) — Claude Code & OpenCode 架构解析
- [测评.md](file:///d:/嵌入式-Agent/测评.md) — v2.4.0 评测报告
- [architecture-framework.md](file:///d:/嵌入式-Agent/docs/architecture-framework.md) — 19 个 P 任务框架
