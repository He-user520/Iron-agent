# Phase 1 短期并行任务协调文档

> **本文件是入口文档**。三个并行任务先读本文件，再根据自己被分配的角色读取对应的子计划文档，然后自动开始执行。
>
> **背景**：Track 1（engine.py 拆分）由主会话执行，本批次的三个任务执行 Track 2/3/4。

---

## 1. 你的角色与对应子计划

用户在 prompt 里会给你一个标识（**Task A** / **Task B** / **Task C**）。按下表对号入座，**只读取你对应的那一份子计划**，不要读其他三份。

| 角色 | 子计划文档 | 改造目标 | 依赖 | 目标版本 |
|------|-----------|---------|------|---------|
| **Task A** | [track-2-main-split.md](file:///d:/嵌入式-Agent/docs/plans/track-2-main-split.md) | `iron/cli/main.py` 的 `run_interactive()` 拆分（270 行 → ≤80 行 + 5 子函数） | 无（可立即开始） | v2.6.0 |
| **Task B** | [track-3-stream-recovery.md](file:///d:/嵌入式-Agent/docs/plans/track-3-stream-recovery.md) | `iron/llm/backend.py` 流式恢复（**仅 Step 1-3**，Step 4-6 等主会话 Track 1 完成后再做） | Step 1-3 无依赖 | v2.6.0 |
| **Task C** | [track-4-lsp-integration.md](file:///d:/嵌入式-Agent/docs/plans/track-4-lsp-integration.md) | LSP 端到端集成（bootstrap/engine/main/lsp_client） | ⚠️ **完全依赖主会话 Track 1 完成** | v2.6.0 |

> ⚠️ **Track 1（engine.py 拆分）由主会话执行，不在本批次三个任务内**。Track 1 的子计划 [track-1-engine-split.md](file:///d:/嵌入式-Agent/docs/plans/track-1-engine-split.md) 仅供主会话使用，Task A/B/C 不要碰。

---

## 2. 通用硬约束（所有任务必须遵守）

1. **项目无 git 仓库** → 不要执行任何 `git commit` / `git tag` 命令。子计划里写的 git 命令全部跳过。
2. **只改子计划指定的文件** → 不要碰其他任务的文件（见下方冲突矩阵）。**特别重要：不要碰 `iron/agent/engine.py`**（主会话正在改它）。
3. **只跑针对性测试** → 只跑子计划里每步指定的测试文件（如 `pytest tests/test_xxx.py -v`），**不要跑全量 `pytest tests/`**（主会话和其他任务可能正在改中间状态，全量测试会误判失败）。
4. **每步实施后立即跑验证** → 不要攒几步一起测。
5. **失败时停下报告** → 不要反复重试同一个失败，连续失败 2 次就停下，报告失败原因和已完成的步骤。
6. **不删除现有代码的注释和文档字符串** → 纯重构，保留原注释。
7. **不引入新依赖** → 仅用已 import 的标准库和项目内模块。
8. **行为等价** → 重构后 AgentEvent yield 顺序、tool_results 累积逻辑、conversation append 顺序必须完全不变。

---

## 3. 文件冲突矩阵

| 任务 | 主改文件 | 新建文件 | 与其他任务/主会话冲突 |
|------|---------|---------|---------------------|
| 主会话（Track 1） | `iron/agent/engine.py` | 无 | — |
| Task A（Track 2） | `iron/cli/main.py` | 无 | 无 |
| Task B Step 1-3（Track 3） | `iron/llm/backend.py` | `iron/llm/stream_buffer.py` | 无 |
| Task C（Track 4） | `iron/cli/bootstrap.py` / `iron/agent/engine.py` / `iron/cli/main.py` / `iron/integrations/lsp_client.py` | 无 | ⚠️ **与主会话 Track 1 冲突 engine.py** |

**关键**：Task C 必须等主会话 Track 1 完成后才能开始。Task A 和 Task B Step 1-3 可以立即开始，与主会话无冲突。

---

## 4. Task C 的特殊说明（重要）

Task C（Track 4 LSP 集成）的子计划有 10 个 Step，**全部依赖主会话 Track 1 完成**，因为 Track 4 要改 engine.py 的 `_execute_write_file` / `_execute_read_file` 方法，而这些方法在 Track 1 中被提取重构。

**Task C 的执行策略**：

| 阶段 | Task C 行为 |
|------|------------|
| 主会话 Track 1 未完成 | 读取子计划文档 + 读取相关源码（lsp_client.py / lsp_tools.py / bootstrap.py / features.py），**但不开始改任何文件**。向用户报告"已就绪，等待主会话 Track 1 完成"。 |
| 主会话 Track 1 完成后 | 按子计划 Step 1-10 顺序执行，每步验证。 |

Task C 的判断标准：向用户询问"主会话 Track 1 是否已完成？"，得到肯定答复后再开始改文件。

---

## 5. Task B 的特殊说明

Task B 的子计划有 6 个 Step，但 **本批次只做 Step 1-3**：

| Step | 内容 | 本批次是否执行 |
|------|------|--------------|
| Step 1 | 创建备份标记 | ✅ 执行（用文件备份代替 git tag） |
| Step 2 | 定义 `StreamBuffer` 类 | ✅ 执行 |
| Step 3 | 改造 `backend.py` 流式方法 | ✅ 执行 |
| Step 4 | 改造 `engine.py` 的 `_handle_thinking_phase` | ❌ **不执行**（依赖主会话 Track 1 把这段代码提取出来） |
| Step 5 | 集成测试 | ❌ 不执行 |
| Step 6 | 全量验证 | ❌ 不执行 |

Task B 完成 Step 1-3 后就报告完成，等主会话协调 Step 4-6。

---

## 6. 执行流程

1. **确认角色**：用户 prompt 里告诉你"你是 Task A/B/C"。
2. **读取子计划**：完整读取第 1 节表格中你对应的子计划文档（从头读到尾，不要跳读）。
3. **检查依赖**：
   - Task A：无依赖，直接开始。
   - Task B：Step 1-3 无依赖，直接开始；Step 4-6 不做。
   - Task C：有依赖，按第 4 节处理。
4. **备份原文件**：在开始改动前，把你要改的文件复制一份 `.bak`（项目无 git，用文件备份做回滚点）。
   - Task A：`Copy-Item "iron\cli\main.py" "iron\cli\main.py.bak"`
   - Task B：`Copy-Item "iron\llm\backend.py" "iron\llm\backend.py.bak"`
   - Task C：`Copy-Item "iron\cli\bootstrap.py" "iron\cli\bootstrap.py.bak"` + 其他要改的文件
5. **按 Step 顺序执行**：严格按子计划的 Step 编号顺序，不要跳步。
6. **每步验证**：每完成一个 Step，立即跑子计划里该 Step 写的 pytest 命令。
7. **遇到失败**：连续失败 2 次就停下，报告失败原因。
8. **全部完成后**：按第 7 节的格式报告。

---

## 7. 完成后报告格式

完成后用以下格式向用户报告（简洁、直接）：

```
任务：Task X（Track N: 一句话目标）
状态：完成 / 部分完成 / 失败

改动文件：
  - iron/xxx/yyy.py（+XX 行 / -YY 行）
新增文件：
  - iron/xxx/zzz.py（NN 行）

测试结果：
  - pytest tests/test_xxx.py -v → N passed, M failed
  - （列出每个 Step 跑的测试）

关键决策：
  - （如果对子计划有偏离，说明原因）

剩余步骤：
  - 无 / 描述还差什么
```

---

## 8. 合并顺序（主会话协调，任务无需关心）

所有任务都完成后，主会话按以下顺序合并：

```
主会话 Track 1 完成 → 跑 pytest tests/ -v 全量验证（必须 ≥ 738 passed）
                ↓
Task A 完成 → 主会话 review main.py 改动 → 跑 pytest tests/ -v 全量验证
                ↓
Task B Step 1-3 完成 → 主会话 review backend.py 改动 → 跑 pytest tests/ -v 全量验证
                ↓
Task B Step 4-6 → 主会话在 engine.py 上接续做（依赖 Track 1 完成）
                ↓
Task C（Track 4）→ 主会话 review LSP 集成改动 → 跑 pytest tests/ -v 全量验证
```

任务本身不需要关心合并，只管改自己的文件、跑自己的测试。

---

## 9. 风险点速查（按任务）

### Task A 风险点（Track 2 · main.py 拆分）
- `last_engine` 状态传递不能丢
- 斜杠命令的副作用（清屏、切模式、保存会话）必须保留
- `_read_engine` 是独立引擎，不要和主引擎混淆

### Task B 风险点（Track 3 · 流式恢复）
- 流式已收到部分 chunk 时不能重发请求（避免双倍 token）
- `asyncio.CancelledError` 必须 raise，不能吞
- `StreamBuffer` 是 fire-and-forget，不阻塞主循环

### Task C 风险点（Track 4 · LSP 集成）
- LSP 启动失败不能导致 iron 退出（必须 try/except 降级）
- `did_change`/`did_open` 是 fire-and-forget 通知，不阻塞主循环
- 特性门控实际名称是 `lsp_tools`（features.py line 41），不是 `lsp_enabled`
- 不在 `process()` 主循环中直接调用 LSPClient 方法（必须通过工具注册或 `_execute_*` 钩子）

---

## 10. 子计划文档速查

所有子计划都在 `d:\嵌入式-Agent\docs\plans\` 目录：

| 文档 | 行数 | 实施步骤 | 测试用例 | 本批次执行者 |
|------|------|---------|---------|------------|
| track-1-engine-split.md | 1124 | 16 步 | 18 个 | 主会话 |
| track-2-main-split.md | 897 | 8 步 | 7 个 | Task A |
| track-3-stream-recovery.md | 1221 | 6 步 | 6 个 | Task B（仅 Step 1-3） |
| track-4-lsp-integration.md | 1333 | 10 步 | 12 个 | Task C |

---

**开始执行**：确认你的角色后，立即读取对应的子计划文档，然后按第 6 节流程开始。
