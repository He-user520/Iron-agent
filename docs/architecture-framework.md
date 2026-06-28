# Iron Agent 架构开发框架

> **版本**: 1.0
> **创建时间**: 2026-06-27
> **参考项目**: Claude Code (TypeScript) · OpenCode (Go)
> **目标**: 整合两家优点 + 保留 Iron 嵌入式特色，作为后续开发参照标准
> **维护规则**: 每完成一个阶段任务需更新本文档进度标记

---

## 一、当前项目对照分析

### 1.1 六层架构映射

| 层级 | Claude Code | OpenCode | **当前 Iron 项目** | 主要差距 |
|------|-------------|----------|------------------|---------|
| **L1 入口引导** | 7 阶段启动管道 + 88 特性标记 | Cobra + 配置 + DB | Click + 配置级联 + 信号处理 | 无启动管道分阶段、无特性门控、无远程/SSH 模式 |
| **L2 Agent 循环** | query.ts 异步生成器 ReAct + Stop Hooks | processGeneration channel | engine.py `async def process()` ReAct + doom_loop + MAX_STEPS | 无 Stop Hooks、无 end_turn 信号依赖、无 Prompt Caching |
| **L3 工具执行** | 40 工具 + ToolSearchTool 动态发现 | 12 工具 + MCP | 14 工具 + MCP + EmbedForge/EmbedGuard | 无 ToolSearchTool、无 LSP 工具、无 patch 工具 |
| **L4 权限安全** | 5 级 + ML 分类器 + OS 沙箱 + Hooks | 3 级（单次/会话/拒绝） | ask/auto/never + _EXTERNAL_WRITE_TOOLS + path_guard | 无规则评估引擎、无 OS 沙箱、无 PreToolUse Hooks |
| **L5 服务设施** | 36 服务 + 5 层压缩 + 子代理 + Plugins | PubSub + SQLite + LSP | 4 层记忆 + Dream/Distill + MCP + Skills | 无 PubSub、无 SQLite、无 LSP 集成、无 Plugins |
| **L6 终端 UI** | React + Ink + 207 命令 + Vim | Bubble Tea + Lipgloss + Glamour | prompt_toolkit + rich + 20 命令 | 无 Vim 模式、无 Markdown 流式渲染、无主题系统 |

### 1.2 Iron 的嵌入式特色（两参考项目都没有）

- ✅ **嵌入式铁律引擎**（11 条 + 7 反模式 + 项目规则三层）
- ✅ **EmbedForge / EmbedGuard 集成**（编译/烧录/静态分析）
- ✅ **MCU 配置**（STM32G431/F407 profile）
- ✅ **嵌入式专用 Agent**（build / embed / plan）
- ✅ **Dream/Distill 记忆**（7天/30天，参考 MiMo Code）

### 1.3 当前测试基线

- **测试用例**: 328 passed, 1 skipped
- **代码规模**: 51 源文件 + 10 测试文件
- **质量评级**: B+（生产级，存在局部热点）

---

## 二、目标六层架构

### 2.1 设计原则

- **取 Claude Code 之深度**：压缩管道、Hook 系统、专门化子代理、Prompt Caching
- **取 OpenCode 之简洁**：PubSub 事件总线、SQLite 持久化、双 Agent 类型、泛型 Provider
- **保留 Iron 之嵌入式特色**：铁律引擎、EmbedForge/EmbedGuard、MCU profile、Dream/Distill

### 2.2 目标架构图

```
┌─────────────────────────────────────────────────────────────────┐
│ L6 · 终端 UI 层                                                  │
│   • prompt_toolkit + rich (保留)                                  │
│   • 流式 Markdown 渲染 (新, Glamour 风格)                        │
│   • Vim 模式 (新, 可选)                                          │
│   • 主题系统 (新, Catppuccin/Dracula)                            │
│   • 斜杠命令分组 (改, _handle_slash_*)                           │
├─────────────────────────────────────────────────────────────────┤
│ L5 · 服务基础设施层                                              │
│   • PubSub 事件总线 (新, 解耦所有服务)                          │
│   • SQLite 持久化 (新, 会话/消息/历史)                           │
│   • 4 层记忆 + Dream/Distill (保留)                              │
│   • Skills 系统 (保留, PromptSkill 数据驱动)                    │
│   • MCP 客户端 (保留, 三种传输)                                  │
│   • EmbedForge/EmbedGuard 桥接 (保留, 嵌入式核心)                │
│   • LSP 客户端 (新, 嵌入式 LSP, ccls/clangd)                     │
├─────────────────────────────────────────────────────────────────┤
│ L4 · 权限与安全层                                                │
│   • 三级审批 (单次/会话/永不, 改)                                │
│   • 规则评估引擎 (新, 铁律驱动)                                  │
│   • PreToolUse/PostToolUse Hooks (新)                            │
│   • path_guard (保留)                                            │
│   • 嵌入式危险操作识别 (保留, _EXTERNAL_WRITE_TOOLS)            │
├─────────────────────────────────────────────────────────────────┤
│ L3 · 工具执行层                                                  │
│   • BaseTool + Registry (保留)                                  │
│   • 14 嵌入式工具 (保留)                                         │
│   • ToolSearchTool 动态发现 (新)                                  │
│   • patch 工具 (新, diff 补丁)                                   │
│   • LSP diagnostics 工具 (新)                                    │
│   • 工具结果截断保护 (新)                                        │
├─────────────────────────────────────────────────────────────────┤
│ L2 · Agent 循环层 (内核)                                         │
│   • ReAct async generator (保留)                                 │
│   • 5 层压缩管道 (扩展, 当前 Level 1+2)                          │
│   • Stop Hooks (新, 收敛检测)                                    │
│   • Prompt Caching (新, 系统提示分块)                            │
│   • 双 Agent 类型 (新, Coder + Task 只读)                       │
│   • 专门化子代理 (扩展, build/embed/plan/verify)                │
│   • doom_loop + MAX_STEPS (保留)                                 │
├─────────────────────────────────────────────────────────────────┤
│ L1 · 入口与引导层                                                │
│   • Click CLI (保留)                                             │
│   • 多厂商配置 (保留)                                            │
│   • 启动管道分阶段 (新, 3 阶段: 配置→信任→运行)                 │
│   • 特性门控 (新, 运行时特性开关)                                │
│   • 配置级联 (保留, 全局→项目→环境变量)                          │
└─────────────────────────────────────────────────────────────────┘
```

---

## 三、开发路线图

### 阶段 1 — 内核强化（L2，最高优先级）

#### P1-1 5 层压缩管道（对标 Claude Code）
- **当前状态**: Level 1 微压缩 + Level 2 阈值压缩
- **目标**: 完整 5 层管道
- **待加**:
  - Level 3 Context Collapse（合并连续工具结果）
  - Level 4 Auto-compact（独立模型摘要）
  - Level 5 Budget Reduction（按 token 预算裁剪）
- **文件**: `iron/agent/context_compactor.py`（新拆分自 engine.py）
- **测试**: 新增 `tests/test_context_compactor.py`
- **状态**: ⬜ 未开始

#### P1-2 Stop Hooks（对标 Claude Code）
- **当前状态**: 仅 MAX_STEPS 兜底
- **目标**: 用户可注册收敛检测器
- **示例规则**: "3 次相同工具结果 → break"、"连续 5 次 edit 失败 → break"
- **文件**: `iron/agent/stop_hooks.py`（新）
- **测试**: 新增 `tests/test_stop_hooks.py`
- **状态**: ⬜ 未开始

#### P1-3 Prompt Caching（对标 Claude Code）
- **当前状态**: 每次请求重新发系统提示
- **目标**: 系统提示分两块（核心指令 + 项目配置），缓存命中标识
- **预期收益**: 降低 ~85% 重复计算成本
- **文件**: `iron/llm/backend.py`（修改）
- **测试**: 扩展 `tests/test_backend.py`
- **状态**: ⬜ 未开始

#### P1-4 双 Agent 类型（对标 OpenCode）
- **当前状态**: 单一 AgentEngine
- **目标**: TaskAgent（只读工具集，用于探索/规划）
- **文件**: `iron/agent/engine.py`（拆分为 BaseAgentEngine + CoderAgent + TaskAgent）
- **测试**: 扩展 `tests/test_engine.py`
- **状态**: ⬜ 未开始

---

### 阶段 2 — 权限安全（L4）

#### P2-1 规则评估引擎
- **当前状态**: 仅 _EXTERNAL_WRITE_TOOLS 白名单
- **目标**: 规则 DSL，用户可配置
- **示例规则**: "*.ld 文件禁止写"、"flash 工具必须确认"
- **文件**: `iron/rules/permission_rules.py`（新）
- **测试**: 新增 `tests/test_permission_rules.py`
- **状态**: ⬜ 未开始

#### P2-2 PreToolUse/PostToolUse Hooks
- **当前状态**: 无
- **目标**: 用户在 `~/.iron/hooks/` 放 Python 脚本，PreToolUse 返回 deny 可阻止
- **文件**: `iron/agent/hooks.py`（新）
- **测试**: 新增 `tests/test_hooks.py`
- **状态**: ⬜ 未开始

#### P2-3 三级审批持久化
- **当前状态**: 每次都问
- **目标**: 会话级批准（"本次会话内允许 embed_flash"）
- **文件**: `iron/agent/permission.py`（新拆分）
- **测试**: 新增 `tests/test_permission.py`
- **状态**: ⬜ 未开始

---

### 阶段 3 — 服务设施（L5）

#### P3-1 PubSub 事件总线（对标 OpenCode）
- **当前状态**: engine yield AgentEvent + main.py 同步处理
- **目标**: 泛型 Broker[T]，memory/mcp/skills 内嵌
- **文件**: `iron/core/pubsub.py`（新）
- **测试**: 新增 `tests/test_pubsub.py`
- **状态**: ⬜ 未开始

#### P3-2 SQLite 持久化（对标 OpenCode）
- **当前状态**: JSON 文件
- **目标**: 会话/消息/历史/任务存 SQLite，支持类型安全访问
- **文件**: `iron/core/db.py`（新）+ `iron/core/migrations/`（SQL 迁移）
- **测试**: 新增 `tests/test_db.py`
- **状态**: ⬜ 未开始

#### P3-3 LSP 客户端（嵌入式定制）
- **当前状态**: 无
- **目标**: ccls/clangd 集成，提供诊断、跳转、补全
- **嵌入式场景**: 自动检测 compile_commands.json，配置 LSP
- **文件**: `iron/integrations/lsp_client.py`（新）
- **测试**: 新增 `tests/test_lsp.py`
- **状态**: ⬜ 未开始

#### P3-4 专门化子代理扩展
- **当前状态**: build/embed/plan
- **目标**: 增加 verify（自动跑测试 + 静态分析）、explore（只读探索）
- **文件**: `iron/agent/agents/verify.md`（新）+ `iron/agent/agents/explore.md`（新）
- **测试**: 扩展 `tests/test_engine.py`
- **状态**: ⬜ 未开始

---

### 阶段 4 — 工具层（L3）

#### P4-1 ToolSearchTool（对标 Claude Code）
- **当前状态**: 14 工具全注入
- **目标**: 按需动态发现，提示词太长时只暴露相关工具
- **文件**: `iron/tools/tool_search.py`（新）
- **测试**: 新增 `tests/test_tool_search.py`
- **状态**: ⬜ 未开始

#### P4-2 patch 工具
- **当前状态**: edit_file 单行/多行替换
- **目标**: unified diff 补丁应用
- **文件**: `iron/tools/patch.py`（新）
- **测试**: 新增 `tests/test_patch.py`
- **状态**: ⬜ 未开始

#### P4-3 工具结果截断保护
- **当前状态**: 依赖 LLM 自己控制
- **目标**: 单工具输出超阈值自动截断，告知模型
- **阈值**: 默认 10000 字符，可配置
- **文件**: `iron/tools/base.py`（修改）
- **测试**: 扩展 `tests/test_tools.py`
- **状态**: ⬜ 未开始

---

### 阶段 5 — 终端 UI（L6）

#### P5-1 流式 Markdown 渲染
- **当前状态**: 纯文本输出
- **目标**: rich.markdown 流式渲染（代码块高亮 + 表格 + 列表）
- **文件**: `iron/cli/ui.py`（修改）
- **测试**: 手动验证 + 扩展 `tests/test_ui.py`
- **状态**: ⬜ 未开始

#### P5-2 主题系统
- **当前状态**: 硬编码 cyan/yellow
- **目标**: 可切换主题（Catppuccin/Dracula/默认）
- **文件**: `iron/cli/theme.py`（扩展）+ `iron/cli/themes/`（新目录）
- **测试**: 新增 `tests/test_theme.py`
- **状态**: ⬜ 未开始

#### P5-3 斜杠命令分组
- **当前状态**: run_interactive 内集中处理
- **目标**: `_handle_slash_file_*` / `_handle_slash_build_*` / `_handle_slash_session_*` 分组
- **文件**: `iron/cli/commands/`（新目录）+ 拆分 main.py
- **测试**: 扩展 `tests/test_cli.py`
- **状态**: ⬜ 未开始

---

### 阶段 6 — 入口引导（L1）

#### P6-1 启动管道分阶段
- **当前状态**: 同步加载
- **目标**: 并行加载（凭证/配置/扩展），显示进度
- **三阶段**:
  1. 配置阶段: 加载全局/项目配置 + 环境变量
  2. 信任阶段: 验证 API Key + 扩展签名
  3. 运行阶段: 初始化 AgentEngine + MCP + LSP
- **文件**: `iron/cli/bootstrap.py`（新）
- **测试**: 新增 `tests/test_bootstrap.py`
- **状态**: ⬜ 未开始

#### P6-2 特性门控
- **当前状态**: 硬编码
- **目标**: `~/.iron/features.yml` 特性开关，运行时读取
- **示例特性**: `lsp_enabled`, `pubsub_enabled`, `prompt_caching`, `vim_mode`
- **文件**: `iron/config/features.py`（新）
- **测试**: 新增 `tests/test_features.py`
- **状态**: ⬜ 未开始

---

## 四、开发原则

### 4.1 核心原则

1. **不破坏现有功能**: 每个阶段完成都跑全量测试（基线 328 passed）
2. **嵌入式优先**: 所有通用功能增强嵌入式场景（如 LSP 选 ccls/clangd，权限规则含 flash 工具）
3. **可独立交付**: 每个 P 任务都能独立完成 + 测试 + 合并
4. **文档同步**: 每完成一个阶段更新本文档进度标记 + [ARCHITECTURE.md](file:///d:/嵌入式-Agent/ARCHITECTURE.md)
5. **对标而非照搬**: 参考设计但按 Iron 现有架构演进，不重写

### 4.2 代码规范

- **职责单一**: 单文件应聚焦一个核心职责（如 engine.py 聚焦 Agent 循环，ui.py 聚焦终端 UI），不硬性限制行数
- **函数复杂度**: 圈复杂度不超过 15，超过则拆分
- **嵌套深度**: 不超过 5 层
- **测试覆盖**: 新增代码必须有对应测试，测试比例 ≥ 0.8
- **异常处理**: 禁止裸 `except Exception`，必须捕获具体异常
- **死代码**: 不留 unused variables/functions/imports

### 4.3 提交规范

- **commit message**: `feat(scope): description` 或 `fix(scope): description`
- **scope 示例**: `engine`, `cli`, `tools`, `memory`, `llm`, `config`
- **测试要求**: 提交前必须 `python -m pytest tests/ -q` 通过

### 4.4 依赖关系

```
P1-1 (压缩管道) ──┐
P1-2 (Stop Hooks) ─┤
P1-3 (Prompt Cache)┼─→ 阶段 1 完成
P1-4 (双 Agent) ──┘
                   │
                   ↓
P3-1 (PubSub) ────┐
P3-2 (SQLite) ────┼─→ 阶段 3 完成
P3-3 (LSP) ───────┤
P3-4 (子代理) ────┘
                   │
                   ↓
P2-1 (规则引擎) ──┐
P2-2 (Hooks) ─────┤
P2-3 (审批持久化) ┼─→ 阶段 2 完成
                   │
                   ↓
P4-* (工具层) ─────→ 阶段 4 完成
                   │
                   ↓
P5-* (UI 层) ──────→ 阶段 5 完成
                   │
                   ↓
P6-* (入口层) ─────→ 阶段 6 完成
```

**建议顺序**: P1 → P3 → P2 → P4 → P5 → P6

- **阶段 1（L2 内核）**: 是其他改动的基础设施，必须先做
- **阶段 3（L5 服务）**: PubSub + SQLite 是后续 Hooks/权限持久化的基础
- **阶段 2（L4 权限）**: 依赖 PubSub 的事件路由
- **阶段 4-6**: 可并行进行，互不依赖

---

## 五、参考来源

### 5.1 架构文档
- [cli-agent-architecture.md](file:///d:/嵌入式-Agent/cli-agent-architecture.md) — Claude Code & OpenCode 架构深度解析
- [ARCHITECTURE.md](file:///d:/嵌入式-Agent/ARCHITECTURE.md) — Iron 项目当前架构
- [测评.md](file:///d:/嵌入式-Agent/测评.md) — v2.4.0 评测报告（B+ 评级）
- [CLI交互层分析.md](file:///d:/嵌入式-Agent/CLI交互层分析.md) — CLI 交互层深度分析

### 5.2 外部参考
- [arXiv 2604.14228] Dive into Claude Code — https://arxiv.org/html/2604.14228
- y-agent.github.io — Inside Claude Code — https://y-agent.github.io/inside-claude-code/
- cefboud.com — Inside OpenCode — https://cefboud.com/posts/coding-agents-internals-opencode-deepdive/
- GitHub: sst/opencode — https://github.com/sst/opencode

---

## 六、进度跟踪

### 已完成
- ✅ 多厂商配置（L1）— 2026-06-27
- ✅ Dream/Distill 并发锁（L5）— 2026-06-27
- ✅ _EXTERNAL_WRITE_TOOLS 全模式检查（L4）— 2026-06-27
- ✅ Skills 数据驱动重构（L5）— 2026-06-27
- ✅ LLM Backend __init__ 公共逻辑抽基类（L2）— 2026-06-27
- ✅ CLI 交互层 9 个问题修复（L6）— 2026-06-27
- ✅ Progressive Context Compression Level 1+2（L2）— 2026-06-27
- ✅ risk_evaluator.py 拆分（L2）— 2026-06-27
- ✅ P1-1 5层压缩管道（L2）— 2026-06-27，336 passed
- ✅ P1-2 Stop Hooks 收敛检测器（L2）— 2026-06-27，361 passed
- ✅ P1-3 Prompt Caching（L2）— 2026-06-27，379 passed
- ✅ P1-4 双 Agent 类型（L2）— 2026-06-27，393 passed
- ✅ P3-1 PubSub 事件总线（L5）— 2026-06-27，424 passed
- ✅ P3-2 SQLite 持久化（L5）— 2026-06-27，466 passed
- ✅ P3-3 LSP 客户端（L5）— 2026-06-27，521 passed
- ✅ P3-4 专门化子代理扩展（L5）— 2026-06-27，533 passed
- ✅ P2-1 规则评估引擎（L4）— 2026-06-27，583 passed
- ✅ P2-2 PreToolUse/PostToolUse Hooks（L4）— 2026-06-27，619 passed
- ✅ P2-3 三级审批持久化（L4）— 2026-06-27，633 passed
- ✅ P4-1 ToolSearchTool 动态发现（L3）— 2026-06-27，645 passed
- ✅ P4-2 patch 工具（L3）— 2026-06-27，658 passed
- ✅ P4-3 工具结果截断保护（L3）— 2026-06-27，674 passed
- ✅ P5-1 流式 Markdown 渲染（L6）— 2026-06-27，688 passed
- ✅ P5-2 主题系统（L6）— 2026-06-27，701 passed
- ✅ P5-3 斜杠命令分组（L6）— 2026-06-27，711 passed
- ✅ P6-1 启动管道分阶段（L1）— 2026-06-27，724 passed
- ✅ P6-2 特性门控（L1）— 2026-06-27，738 passed

### 进行中
- ⬜ 无

### 待开始
- ⬜ 无（全部完成）

### 已知遗留（后续迭代）
- ⚠️ engine.py 圈复杂度待评估（process() 函数较长，后续可考虑拆分 _handle_event 等子方法）
- ⚠️ ui.py 圈复杂度待评估（run_interactive 嵌套较深）

---

## 七、变更记录

| 日期 | 版本 | 变更 |
|------|------|------|
| 2026-06-27 | 1.0 | 初始版本，基于 cli-agent-architecture.md 对照分析生成 |
