# V4.0 L4 工业生产级冲刺 — 并行任务协调文档

> **本文件是入口文档**。所有并行任务先读本文件，再根据自己被分配的角色读取对应的子计划文档，然后自动开始执行。
>
> **背景**：V3.0 已通过测评（1196 测试通过，嵌入式领域 L5、通用编码 L3）。V4.0 目标是补齐通用编码能力三大件（Git/Diff/子 Agent），冲刺 L4 工业生产级。
>
> **本批次由主会话执行 Track 5 + Track 6（P0 最关键），其他任务执行 Track 7-10。**

---

## 1. V4.0 总体目标

| 维度 | V3.0 现状 | V4.0 目标 | 关键缺口 |
|---|---|---|---|
| 通用编码能力 | 65% | 90% | Git 工具 / Diff 预览 / 子 Agent 编排 |
| 文档完整性 | 缺 README | 完整 | README + 用户文档 + 观测性 |
| 代码索引可用性 | 降级模式 | 实战可用 | tree-sitter 安装引导 |
| 多文件编辑 | 单文件 | 原子多文件 | MultiEdit 工具 |

**版本号目标**：3.0.0 → **4.0.0**

---

## 2. 你的角色与对应子计划

用户在 prompt 里会给你一个标识（**Task A / Task B / Task C / Task D**）。按下表对号入座，**只读取你对应的那一份子计划**。

| 角色 | 子计划文档 | 改造目标 | 依赖 | 优先级 |
|---|---|---|---|---|
| **主会话** | [track-5-git-tools.md](file:///d:/嵌入式-Agent/docs/plans/track-5-git-tools.md) + [track-6-diff-preview.md](file:///d:/嵌入式-Agent/docs/plans/track-6-diff-preview.md) | Git 工具集 + edit_file 前 diff 预览 | 无 | **P0** |
| **Task A** | [track-7-multi-edit.md](file:///d:/嵌入式-Agent/docs/plans/track-7-multi-edit.md) | MultiEdit 多文件原子编辑工具 | 弱依赖主会话 Track 6（diff 预览复用） | P1 |
| **Task B** | [track-8-sub-agent.md](file:///d:/嵌入式-Agent/docs/plans/track-8-sub-agent.md) | 子 Agent 并行编排（Task 工具） | 无 | P1 |
| **Task C** | [track-9-docs-observability.md](file:///d:/嵌入式-Agent/docs/plans/track-9-docs-observability.md) | README + 用户文档 + 观测性指标 | 无 | P1 |
| **Task D** | [track-10-tree-sitter-bootstrap.md](file:///d:/嵌入式-Agent/docs/plans/track-10-tree-sitter-bootstrap.md) | tree-sitter 安装引导 + 一键启用 | 无 | P2 |

> ⚠️ **Track 5 + Track 6 由主会话执行**，其他任务不要碰。Task A 的 MultiEdit 可复用主会话 Track 6 的 diff 预览能力（弱依赖）。

---

## 3. 通用硬约束（所有任务必须遵守）

1. **不执行 `git commit` / `git tag`** → 项目无 git 仓库，子计划里的 git 命令全部跳过，用 `.bak` 文件做回滚点。
2. **只改子计划指定的文件** → 不要碰其他任务的文件（见下方冲突矩阵）。
3. **只跑针对性测试** → 只跑子计划里每步指定的测试文件，**不要跑全量 `pytest tests/`**（主会话和其他任务可能正在改中间状态）。
4. **每步实施后立即跑验证** → 不要攒几步一起测。
5. **失败时停下报告** → 连续失败 2 次就停下，报告失败原因和已完成的步骤。
6. **不删除现有代码的注释和文档字符串** → 保留原注释。
7. **不引入新依赖** → 仅用已 import 的标准库和项目内模块（除非子计划明确要求）。
8. **行为等价 / 向后兼容** → 不能破坏 V3.0 现有 1196 个测试。
9. **特性门控** → V4.0 新功能默认 `False`，通过 `features.yml` 显式启用。
10. **Windows 优先** → 所有路径、子进程、终端操作必须 Windows 兼容。

---

## 4. 文件冲突矩阵

| 任务 | 主改文件 | 新建文件 | 冲突 |
|---|---|---|---|
| 主会话（Track 5） | `iron/tools/git_tools.py`（新建）, `iron/agent/engine.py`（注册）, `iron/cli/main.py`（命令） | `tests/test_git_tools.py` | 无 |
| 主会话（Track 6） | `iron/tools/edit_file.py`, `iron/cli/ui.py`（diff 渲染）, `iron/agent/engine.py`（前置 hook） | `tests/test_diff_preview.py` | 无 |
| Task A（Track 7） | `iron/tools/multi_edit.py`（新建）, `iron/agent/engine.py`（注册） | `tests/test_multi_edit.py` | ⚠️ **与主会话 Track 6 共享 edit_file.py**（弱依赖，等主会话完成） |
| Task B（Track 8） | `iron/agent/sub_agent.py`（新建）, `iron/agent/engine.py`（Task 工具） | `tests/test_sub_agent.py` | ⚠️ **与主会话共享 engine.py**（注册位置不同，错开即可） |
| Task C（Track 9） | `README.md`（新建）, `docs/`（新建）, `iron/utils/metrics.py`（新建） | `tests/test_metrics.py` | 无 |
| Task D（Track 10） | `iron/cli/doctor.py`（增强）, `iron/integrations/code_indexer.py`（增强） | `tests/test_ts_bootstrap.py` | 无 |

**关键**：Task A 必须等主会话 Track 6 完成后才能开始（共享 edit_file.py）。Task B 与主会话在 engine.py 上的注册位置不同（Task B 注册 `task` 工具，主会话注册 `git_*` 工具），可并行但需小心冲突。

---

## 5. 主会话特殊说明

主会话执行 **Track 5 + Track 6 两个 P0 任务**，是 V4.0 的核心价值所在：

| 任务 | 价值 | 工作量 |
|---|---|---|
| Track 5 Git 工具集 | 让 Iron 通用编码能力从 65% → 80% | 中等（5 个工具 + 命令注册） |
| Track 6 Diff 预览 | 安全感大幅提升，编辑前可见 | 小（edit_file 前置 hook） |

主会话完成后向用户报告，由用户决定是否启动 Task A-D 的并行执行。

---

## 6. Task A 特殊说明（弱依赖主会话）

Task A 的 MultiEdit 工具会复用 Track 6 的 diff 预览能力：

| 阶段 | Task A 行为 |
|---|---|
| 主会话 Track 6 未完成 | 读取子计划 + 读取 `iron/tools/edit_file.py` 现有代码，**但不开始改任何文件**。向用户报告"已就绪，等待主会话 Track 6 完成"。 |
| 主会话 Track 6 完成后 | 按子计划顺序执行，复用 `_render_diff` 等函数。 |

判断标准：向用户询问"主会话 Track 6 是否已完成？"，得到肯定答复后再开始改文件。

---

## 7. 执行流程

1. **确认角色**：用户 prompt 里告诉你"你是 Task A/B/C/D"，或"主会话"。
2. **读取子计划**：完整读取第 2 节表格中你对应的子计划文档（从头读到尾，不要跳读）。
3. **检查依赖**：
   - 主会话：无依赖，直接开始 Track 5，完成后开始 Track 6。
   - Task A：弱依赖主会话 Track 6，按第 6 节处理。
   - Task B/C/D：无依赖，直接开始。
4. **备份原文件**：在开始改动前，把要改的文件复制一份 `.bak`。
5. **按 Step 顺序执行**：严格按子计划的 Step 编号顺序，不要跳步。
6. **每步验证**：每完成一个 Step，立即跑子计划里该 Step 写的 pytest 命令。
7. **遇到失败**：连续失败 2 次就停下，报告失败原因。
8. **全部完成后**：按第 8 节的格式报告。

---

## 8. 完成后报告格式

```
任务：Task X（Track N: 一句话目标）
状态：完成 / 部分完成 / 失败

改动文件：
  - iron/xxx/yyy.py（+XX 行 / -YY 行）
新增文件：
  - iron/xxx/zzz.py（NN 行）

测试结果：
  - pytest tests/test_xxx.py -v → N passed, M failed

关键决策：
  - （如果对子计划有偏离，说明原因）

剩余步骤：
  - 无 / 描述还差什么
```

---

## 9. 合并顺序（主会话协调）

```
主会话 Track 5 完成 → 跑 pytest tests/test_git_tools.py -v 全量验证
                ↓
主会话 Track 6 完成 → 跑 pytest tests/test_diff_preview.py -v + tests/test_engine.py -v
                ↓
Task A/B/C/D 并行 → 各自跑针对性测试
                ↓
全部完成 → 主会话跑全量 pytest tests/ -v（必须 ≥ 1196 passed + V4.0 新增）
                ↓
版本号 3.0.0 → 4.0.0
```

---

## 10. 风险点速查

### 主会话风险（Track 5 + 6）
- **Git 工具不能假设项目已 git init**：所有 git 命令必须 try/except，失败返回友好错误
- **Diff 预览不能阻塞主循环**：用户拒绝时跳过编辑，不能死等
- **engine.py 注册位置**：在 `_register_tools` 末尾追加，不要插入到现有工具中间

### Task A 风险（Track 7 · MultiEdit）
- 多文件编辑必须原子：要么全成功，要么全回滚
- 复用 edit_file 的 diff 预览，不要重新实现

### Task B 风险（Track 8 · 子 Agent）
- 子 Agent 不能共享父 Agent 的 conversation（避免污染）
- 子 Agent 超时必须 cancel，不能 hang
- 子 Agent 工具调用结果必须序列化回父 Agent

### Task C 风险（Track 9 · 文档 + 观测性）
- README 不能有营销话术，纯技术描述
- metrics 收集不能阻塞 LLM 流式输出

### Task D 风险（Track 10 · tree-sitter）
- 安装引导不能强制执行 pip install，只提示
- 降级路径必须保留（tree-sitter 不可用时仍能工作）

---

## 11. 子计划文档速查

所有子计划都在 `d:\嵌入式-Agent\docs\plans\` 目录：

| 文档 | 优先级 | 实施步骤 | 执行者 |
|---|---|---|---|
| track-5-git-tools.md | P0 | 6 步 | 主会话 |
| track-6-diff-preview.md | P0 | 4 步 | 主会话 |
| track-7-multi-edit.md | P1 | 5 步 | Task A |
| track-8-sub-agent.md | P1 | 6 步 | Task B |
| track-9-docs-observability.md | P1 | 5 步 | Task C |
| track-10-tree-sitter-bootstrap.md | P2 | 4 步 | Task D |

---

**开始执行**：确认你的角色后，立即读取对应的子计划文档，然后按第 7 节流程开始。
