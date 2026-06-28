# Iron vs Claude Code vs OpenCode 差距对比

**对比日期：** 2026-06-27
**Iron 版本：** v2.5.0+（含 19 个 P 任务实现）
**参考来源：** [cli-agent-architecture.md](file:///d:/嵌入式-Agent/cli-agent-architecture.md) — Claude Code & OpenCode 架构深度解析
**对比目的：** 量化差距，识别后续演进方向

> 本文档基于 [cli-agent-architecture.md](file:///d:/嵌入式-Agent/cli-agent-architecture.md) 对 Claude Code（TypeScript）和 OpenCode（Go）的架构解析，与 Iron v2.5.0+ 当前实现进行三维对比：**功能完整性** / **架构合理性** / **嵌入式特色**。

---

## 一、总览对比

### 1.1 项目定位

| 维度 | Claude Code | OpenCode | **Iron Agent** |
|------|-------------|----------|---------------|
| 定位 | 通用 AI 编码 CLI | 通用 AI 编码 CLI | **嵌入式 AI 编码 CLI** |
| 语言 | TypeScript (Node.js) | Go | Python |
| UI 框架 | React + Ink | Bubble Tea + Lipgloss | prompt_toolkit + rich |
| 目标场景 | 通用编码 | 通用编码 | **嵌入式 / MCU 开发** |
| 特色 | 深度功能 + 插件 | 简洁架构 + LSP | 铁律引擎 + EmbedForge/EmbedGuard |

### 1.2 核心数据对比

| 数据 | Claude Code | OpenCode | **Iron Agent** |
|------|-------------|----------|---------------|
| 代码规模 | ~120K LoC | ~50K LoC | **16K 源码 + 10K 测试** |
| 工具数量 | 40+ | 12 | **18 内置 + MCP 扩展** |
| 命令数量 | 207 | 30+ | **20+** |
| 测试用例 | 未公开 | 未公开 | **738 passed** |
| 特性标记 | 88 | 无 | **20** |
| 服务数量 | 36 | 8 | **12** |

### 1.3 评级总览

| 层级 | Claude Code | OpenCode | **Iron Agent** |
|------|-------------|----------|---------------|
| L1 入口引导 | ★★★★★ | ★★★★☆ | ★★★★☆ |
| L2 Agent 循环 | ★★★★★ | ★★★★★ | ★★★★★ |
| L3 工具执行 | ★★★★★ | ★★★★☆ | ★★★★☆ |
| L4 权限安全 | ★★★★★ | ★★★☆☆ | ★★★★★ |
| L5 服务设施 | ★★★★★ | ★★★★☆ | ★★★★☆ |
| L6 终端 UI | ★★★★★ | ★★★★☆ | ★★★★☆ |
| **嵌入式特色** | ☆☆☆☆☆ | ☆☆☆☆☆ | **★★★★★** |
| **总分** | ★★★★★ | ★★★★☆ | **★★★★☆** |

> **结论**：Iron 在通用能力上达到 OpenCode 同等水平，在嵌入式特色上独占鳌头。与 Claude Code 的主要差距在于代码规模和高级功能（向量搜索、代码索引）。

---

## 二、L1 入口与引导层对比

### 2.1 启动管道

| 维度 | Claude Code | OpenCode | **Iron Agent** |
|------|-------------|----------|---------------|
| 阶段数 | 7 阶段 | 3 阶段（Cobra + 配置 + DB） | **3 阶段（配置→信任→运行）** |
| 并行加载 | ✅ | ❌ | ✅ |
| 进度显示 | ✅ | ❌ | ✅ |
| 失败不进入下一阶段 | ✅ | ✅ | ✅ |

**差距**：
- Claude Code 的 7 阶段更细粒度（凭证 / 配置 / 扩展签名 / MCP / Skills / 工具 / UI）
- Iron 的 3 阶段已覆盖关键路径，够用

### 2.2 特性门控

| 维度 | Claude Code | OpenCode | **Iron Agent** |
|------|-------------|----------|---------------|
| 特性数量 | 88 | 0 | **20** |
| 用户覆盖 | ✅ | ❌ | ✅（`~/.iron/features.yml`） |
| 运行时读取 | ✅ | ❌ | ✅（`is_feature_enabled`） |
| A/B 测试 | ✅ | ❌ | ❌ |

**差距**：
- Claude Code 用 88 个特性支持渐进式发布和 A/B 测试
- Iron 的 20 个特性覆盖核心开关，无需 A/B 测试

### 2.3 配置系统

| 维度 | Claude Code | OpenCode | **Iron Agent** |
|------|-------------|----------|---------------|
| 多厂商支持 | ✅ | ✅ | ✅（ProviderConfig） |
| 上下键可视化选择 | ✅ | ❌ | ✅ |
| API Key 多策略 | 落盘 | 落盘 | **env / disk / none 三选一** |
| 配置级联 | 全局→项目→环境 | 全局→项目 | **全局→项目→环境变量** |
| 远程/SSH 模式 | ✅ | ❌ | ❌ |

**差距**：
- Iron 的 API Key 多策略（不落盘选项）更安全
- 缺少远程/SSH 模式（Claude Code 独有）

### 2.4 信号处理

| 维度 | Claude Code | OpenCode | **Iron Agent** |
|------|-------------|----------|---------------|
| SIGTERM 处理 | ✅ | ✅ | ✅（保存 session） |
| SIGINT 双击退出 | ✅ | ✅ | ✅ |
| 流式缓冲区 flush | ✅ | ❌ | ✅ |

---

## 三、L2 Agent 循环层对比

### 3.1 ReAct 主循环

| 维度 | Claude Code | OpenCode | **Iron Agent** |
|------|-------------|----------|---------------|
| 异步模型 | async generator (query.ts) | channel (processGeneration) | **async generator (process)** |
| 事件类型 | AgentEvent | channel message | **AgentEvent + PubSub** |
| MAX_STEPS 兜底 | ✅ | ✅ | ✅ |
| doom_loop 检测 | ❌（依赖 Stop Hooks） | ❌ | ✅（二级检测：连续3次 + 长度2/3/4） |

**Iron 优势**：doom_loop 二级检测是 Iron 独有，Claude Code 和 OpenCode 都没有。

### 3.2 压缩管道

| 维度 | Claude Code | OpenCode | **Iron Agent** |
|------|-------------|----------|---------------|
| 层数 | 5 层 | 2 层（compact + summarize） | **5 层** |
| Level 1 microcompact | ✅ | ❌ | ✅（截断 tool 输出 + 合并 thinking） |
| Level 2 threshold | ✅ | ✅ | ✅（动态阈值 context_window × 0.85） |
| Level 3 context_collapse | ✅ | ❌ | ✅（合并连续工具结果） |
| Level 4 auto_compact | ✅ | ✅ | ✅（独立模型摘要） |
| Level 5 budget_reduce | ✅ | ❌ | ✅（按 token 预算裁剪） |

**对比**：Iron 的 5 层管道与 Claude Code 持平，超越 OpenCode。

### 3.3 Stop Hooks

| 维度 | Claude Code | OpenCode | **Iron Agent** |
|------|-------------|----------|---------------|
| 内置检测器 | 多个 | 0 | **4 个** |
| 用户可注册 | ✅ | ❌ | ✅ |
| MaxConsecutiveFailures | ✅ | ❌ | ✅（默认 5） |
| DoomLoopDetector | ❌ | ❌ | ✅（循环模式长度 2/3/4） |
| MaxToolRepetition | ✅ | ❌ | ✅（默认 10） |
| NoProgressDetector | ✅ | ❌ | ✅（默认 8） |

**对比**：Iron 的 Stop Hooks 完整度与 Claude Code 持平，超越 OpenCode。

### 3.4 Prompt Caching

| 维度 | Claude Code | OpenCode | **Iron Agent** |
|------|-------------|----------|---------------|
| 分块缓存 | ✅（两块） | ❌ | ✅（两块：核心指令 + 项目配置） |
| Anthropic 原生 cache_control | ✅ | ❌ | ✅ |
| OpenAI 兼容 hash 遥测 | ✅ | ❌ | ✅ |
| TTL | 配置 | ❌ | **300 秒（可配置）** |
| 预期收益 | ~85% 降低 | ❌ | ~85% 降低 |

**对比**：Iron 与 Claude Code 持平。

### 3.5 Agent 类型

| 维度 | Claude Code | OpenCode | **Iron Agent** |
|------|-------------|----------|---------------|
| 抽象基类 | ✅ | ✅ | ✅（BaseAgentEngine ABC） |
| Coder Agent | ✅ | ✅ | ✅（CoderAgentEngine） |
| Task Agent（只读） | ✅ | ✅ | ✅（TaskAgentEngine） |
| Verify Agent | ✅ | ❌ | ✅（专门化子代理） |
| Explore Agent | ✅ | ❌ | ✅（专门化子代理） |
| 嵌入式专用 Agent | ❌ | ❌ | ✅（build / embed / plan） |

**Iron 优势**：嵌入式专用 Agent 是独有特色。

### 3.6 子代理机制

| 维度 | Claude Code | OpenCode | **Iron Agent** |
|------|-------------|----------|---------------|
| 子代理调度 | ✅（sidechain agent） | ✅ | ✅（AgentManager + markdown 配置） |
| 隔离上下文 | ✅ | ✅ | ✅ |
| 工具集隔离 | ✅ | ✅ | ✅（`_get_allowed_tools`） |
| 结果回传 | ✅ | ✅ | ✅ |

---

## 四、L3 工具执行层对比

### 4.1 工具数量

| 类别 | Claude Code | OpenCode | **Iron Agent** |
|------|-------------|----------|---------------|
| 文件操作 | 5+ | 3 | 3 |
| 代码搜索 | 3+ | 2 | 2 |
| 命令执行 | 2 | 1 | 1 |
| **嵌入式专用** | 0 | 0 | **3（embed_build/flash/lint）** |
| **LSP 工具** | 5+ | 3 | **5（diagnostics/def/hover/refs/completion）** |
| patch 工具 | ✅ | ❌ | ✅（unified diff） |
| ToolSearchTool | ✅ | ❌ | ✅ |
| 输出截断 | ✅ | ❌ | ✅（safe_execute） |
| MCP 扩展 | ✅ | ✅ | ✅ |
| **总计** | **40+** | **12** | **18 + MCP** |

**差距**：
- Claude Code 工具数量最多（40+），覆盖更广
- Iron 的嵌入式专用工具是独有特色
- Iron 的 LSP 工具数量与 Claude Code 持平

### 4.2 工具注册机制

| 维度 | Claude Code | OpenCode | **Iron Agent** |
|------|-------------|----------|---------------|
| 模块化注册 | ✅ | ✅ | ✅（ToolRegistry） |
| 动态发现 | ✅（ToolSearchTool） | ❌ | ✅（ToolSearchTool） |
| 输出截断保护 | ✅ | ❌ | ✅（safe_execute + _truncate_result） |
| 工具过滤 | ✅ | ✅ | ✅（`_get_allowed_tools`） |

### 4.3 patch 工具

| 维度 | Claude Code | OpenCode | **Iron Agent** |
|------|-------------|----------|---------------|
| unified diff | ✅ | ❌ | ✅ |
| 多 hunk | ✅ | ❌ | ✅ |
| 模糊匹配 | ✅ | ❌ | ✅（容忍空白差异） |
| 失败回退 | ✅ | ❌ | ✅（详细错误信息） |

---

## 五、L4 权限与安全层对比

### 5.1 权限层级

| 维度 | Claude Code | OpenCode | **Iron Agent** |
|------|-------------|----------|---------------|
| 层级数 | 5 级 | 3 级 | **4 级（deny/ask/allow + never 黑名单）** |
| deny | ✅ | ✅ | ✅ |
| ask | ✅ | ✅ | ✅ |
| allow | ✅ | ✅ | ✅ |
| always allow | ✅ | ✅ | ✅（session 级） |
| never | ✅ | ❌ | ✅（持久化黑名单） |
| ML 分类器 | ✅ | ❌ | ❌ |
| OS 沙箱 | ✅ | ❌ | ❌ |

**差距**：
- Claude Code 有 ML 分类器和 OS 沙箱，Iron 没有
- Iron 的 `never` 黑名单持久化是 OpenCode 没有的

### 5.2 规则引擎

| 维度 | Claude Code | OpenCode | **Iron Agent** |
|------|-------------|----------|---------------|
| DSL 规则 | ✅ | ❌ | ✅（YAML DSL） |
| 默认规则 | ✅ | ❌ | ✅（4 条嵌入式默认规则） |
| 用户级规则 | ✅ | ❌ | ✅（`~/.iron/rules.yml`） |
| 项目级规则 | ✅ | ❌ | ✅（`.iron-agent/rules.yml`） |
| 自定义规则路径 | ✅ | ❌ | ✅ |
| 优先级 | deny > ask > allow | 无 | **deny > ask > allow** |

**Iron 优势**：DSL 规则引擎是 OpenCode 没有的，且内置 4 条嵌入式专用规则。

### 5.3 Hooks 系统

| 维度 | Claude Code | OpenCode | **Iron Agent** |
|------|-------------|----------|---------------|
| PreToolUse | ✅ | ❌ | ✅ |
| PostToolUse | ✅ | ❌ | ✅ |
| 用户脚本加载 | ✅ | ❌ | ✅（`~/.iron/hooks/`） |
| SafetyCheckHook | ✅ | ❌ | ✅（内置） |
| AuditLogHook | ✅ | ❌ | ✅（内置） |
| PreToolUse 返回 deny 阻止 | ✅ | ❌ | ✅ |

**对比**：Iron 的 Hooks 系统与 Claude Code 持平，超越 OpenCode。

### 5.4 三级审批持久化

| 维度 | Claude Code | OpenCode | **Iron Agent** |
|------|-------------|----------|---------------|
| once | ✅ | ✅ | ✅ |
| session | ✅ | ✅ | ✅ |
| never（持久化） | ✅ | ❌ | ✅（`~/.iron/permissions.yml`） |
| 测试隔离 | ✅ | ❌ | ✅（`permission_persist_path`） |

### 5.5 路径 / 命令 / SSRF 防护

| 防护 | Claude Code | OpenCode | **Iron Agent** |
|------|-------------|----------|---------------|
| 路径穿越 | ✅ | ✅ | ✅ |
| Windows 保留名 | ✅ | ❌ | ✅ |
| symlink 跟随 | ✅ | ❌ | ✅ |
| 命令注入（元字符） | ✅ | ✅ | ✅（含 `\n\r`） |
| 命令注入（子shell） | ✅ | ✅ | ✅ |
| 命令注入（NULL） | ✅ | ❌ | ✅ |
| SSRF（私有 IP） | ✅ | ❌ | ✅ |
| SSRF（十进制/十六进制） | ✅ | ❌ | ✅ |
| SSRF（IPv4-mapped IPv6） | ✅ | ❌ | ✅ |

**Iron 优势**：安全防护完整度与 Claude Code 持平，超越 OpenCode。

---

## 六、L5 服务基础设施层对比

### 6.1 事件总线

| 维度 | Claude Code | OpenCode | **Iron Agent** |
|------|-------------|----------|---------------|
| PubSub | ✅（隐式） | ✅（显式 Broker[T]） | ✅（EventBus 泛型） |
| async/sync 兼容 | ✅ | ✅ | ✅ |
| 错误隔离 | ✅ | ✅ | ✅ |
| 全局单例 | ✅ | ✅ | ✅（`get_default_bus()`） |
| 测试隔离注入 | ✅ | ✅ | ✅ |

**对比**：三者持平。

### 6.2 持久化

| 维度 | Claude Code | OpenCode | **Iron Agent** |
|------|-------------|----------|---------------|
| 存储 | SQLite + 文件 | SQLite | **SQLite WAL + JSON 文件** |
| WAL 模式 | ✅ | ✅ | ✅ |
| 迁移机制 | ✅ | ✅ | ✅（`migrations/001_initial.sql`） |
| 三表结构 | ✅ | ✅ | ✅（sessions / messages / history） |
| 类型安全访问 | ✅ | ✅ | ✅ |

**对比**：三者持平。

### 6.3 记忆系统

| 维度 | Claude Code | OpenCode | **Iron Agent** |
|------|-------------|----------|---------------|
| 层数 | 5 层 | 2 层 | **4 层** |
| 检查点归档 | ✅ | ❌ | ✅（写入前备份） |
| 项目记忆 | ✅ | ❌ | ✅（MEMORY.md） |
| Dream/Distill | ❌ | ❌ | ✅（7天/30天，参考 MiMo Code） |
| 并发保护 | ✅ | ❌ | ✅（asyncio.Lock） |
| 大小上限 | ✅ | ❌ | ✅（50000 字符） |

**Iron 优势**：Dream/Distill 记忆整理是独有特色。

### 6.4 Skills 系统

| 维度 | Claude Code | OpenCode | **Iron Agent** |
|------|-------------|----------|---------------|
| 类型 | 可执行 | prompt 注入 | **prompt 注入（数据驱动）** |
| 用户自建 | ✅ | ❌ | ✅（Markdown 文件） |
| 内置数量 | 多个 | 0 | **8 个** |
| 数据驱动 | ✅ | ❌ | ✅（PromptSkill 数据类） |
| 可忽略 | ❌ | ✅ | ✅（AI 可忽略） |

**差距**：
- Claude Code 的 Skills 是可执行的，Iron 和 OpenCode 都是 prompt 注入
- Iron 的数据驱动重构（8 子类 → 1 数据类）是优化

### 6.5 MCP 客户端

| 维度 | Claude Code | OpenCode | **Iron Agent** |
|------|-------------|----------|---------------|
| stdio | ✅ | ✅ | ✅ |
| SSE | ✅ | ✅ | ✅ |
| HTTP | ✅ | ✅ | ✅ |
| SSRF 防护 | ✅ | ❌ | ✅ |
| 环境变量过滤 | ✅ | ❌ | ✅ |
| 并发锁 | ✅ | ❌ | ✅ |
| 重连机制 | ✅ | ❌ | ✅ |
| API Key 脱敏 | ✅ | ❌ | ✅（7 种格式） |

**Iron 优势**：MCP 客户端安全防护完整度领先。

### 6.6 LSP 客户端

| 维度 | Claude Code | OpenCode | **Iron Agent** |
|------|-------------|----------|---------------|
| 集成 | ✅（深度） | ✅（独立服务） | ✅（客户端 + 5 工具） |
| 服务器 | 多种 | clangd | **clangd / ccls** |
| compile_commands.json | ✅ | ✅ | ✅（自动查找） |
| diagnostics | ✅ | ✅ | ✅ |
| definition | ✅ | ✅ | ✅ |
| hover | ✅ | ✅ | ✅ |
| references | ✅ | ✅ | ✅ |
| completion | ✅ | ✅ | ✅ |
| 主循环集成 | ✅ | ✅ | ❌（仅客户端） |

**差距**：
- Claude Code 和 OpenCode 的 LSP 与主循环深度集成
- Iron 的 LSP 仅客户端实现，未与主循环深度集成（后续改进）

### 6.7 插件系统

| 维度 | Claude Code | OpenCode | **Iron Agent** |
|------|-------------|----------|---------------|
| 插件市场 | ✅ | ❌ | ❌ |
| 插件签名 | ✅ | ❌ | ❌ |
| 第三方扩展 | ✅ | ❌ | ❌（仅 MCP） |

**差距**：Claude Code 的插件系统是独有，Iron 暂未规划。

---

## 七、L6 终端 UI 层对比

### 7.1 UI 框架

| 维度 | Claude Code | OpenCode | **Iron Agent** |
|------|-------------|----------|---------------|
| 框架 | React + Ink | Bubble Tea + Lipgloss | **prompt_toolkit + rich** |
| 流式渲染 | ✅ | ✅（Glamour） | ✅（MarkdownStreamRenderer） |
| 语法高亮 | ✅ | ✅ | ✅（rich.syntax） |
| 表格渲染 | ✅ | ✅ | ✅ |
| Vim 模式 | ✅ | ❌ | ❌（特性门控已就位） |

### 7.2 主题系统

| 维度 | Claude Code | OpenCode | **Iron Agent** |
|------|-------------|----------|---------------|
| 内置主题 | 多套 | 默认 | **3 套（default/catppuccin/dracula）** |
| 动态切换 | ✅ | ❌ | ✅（`_ColorsProxy`） |
| 用户自定义 | ✅ | ❌ | ❌ |

### 7.3 命令系统

| 维度 | Claude Code | OpenCode | **Iron Agent** |
|------|-------------|----------|---------------|
| 命令数量 | 207 | 30+ | **20+** |
| 命令分组 | ✅ | ✅ | ✅（file/build/session/system） |
| 命令补全 | ✅ | ✅ | ✅（WordCompleter + 6 常用） |
| 上下键历史 | ✅ | ✅ | ✅ |
| 回车自动补全 | ✅ | ❌ | ✅（`/` 开头时） |

### 7.4 启动信息

| 维度 | Claude Code | OpenCode | **Iron Agent** |
|------|-------------|----------|---------------|
| 版本显示 | ✅ | ✅ | ✅ |
| 项目元信息 | ✅ | ✅ | ✅（MCU / 模型 / 规则数） |
| API Key 验证 | ✅ | ✅ | ✅（前 4 后 4 字符） |
| 思考时间显示 | ❌ | ❌ | ✅（`⏱ 用时 Xs`，独有） |

**Iron 优势**：思考时间显示是独有特色。

---

## 八、嵌入式特色对比

### 8.1 嵌入式铁律引擎

| 维度 | Claude Code | OpenCode | **Iron Agent** |
|------|-------------|----------|---------------|
| 铁律数量 | 0 | 0 | **11 条** |
| 反模式 | 0 | 0 | **7 个** |
| 项目规则 | 0 | 0 | **3 层（铁律 + 反模式 + 项目）** |
| 默认权限规则 | 0 | 0 | **4 条（*.ld / startup / embed_flash / SystemInit）** |

**Iron 独占**：两参考项目都没有嵌入式铁律。

### 8.2 EmbedForge / EmbedGuard 集成

| 维度 | Claude Code | OpenCode | **Iron Agent** |
|------|-------------|----------|---------------|
| 编译工具 | 通用 | 通用 | **PlatformIO / Keil / CMake / ESP-IDF / GCC** |
| 烧录工具 | 通用 | 通用 | **embed_flash** |
| 静态分析 | 通用 | 通用 | **EmbedGuard（内存/中断/时序/资源）** |
| MCU 配置 | 0 | 0 | **STM32G431 / STM32F407 profile** |

**Iron 独占**：嵌入式工具链集成。

### 8.3 嵌入式专用 Agent

| 维度 | Claude Code | OpenCode | **Iron Agent** |
|------|-------------|----------|---------------|
| build Agent | ❌ | ❌ | ✅ |
| embed Agent | ❌ | ❌ | ✅ |
| plan Agent | ❌ | ❌ | ✅ |
| verify Agent | ✅（通用） | ❌ | ✅ |
| explore Agent | ✅（通用） | ❌ | ✅ |

### 8.4 Dream/Distill 记忆

| 维度 | Claude Code | OpenCode | **Iron Agent** |
|------|-------------|----------|---------------|
| 短期记忆整理 | ❌ | ❌ | ✅（7 天 dream） |
| 长期记忆精炼 | ❌ | ❌ | ✅（30 天 distill） |
| 参考来源 | — | — | MiMo Code |

**Iron 独占**：Dream/Distill 记忆机制。

---

## 九、安全性对比

### 9.1 防护完整度

| 防护维度 | Claude Code | OpenCode | **Iron Agent** |
|----------|-------------|----------|---------------|
| 路径穿越 | ★★★★★ | ★★★☆☆ | ★★★★★ |
| 命令注入 | ★★★★★ | ★★★★☆ | ★★★★★ |
| SSRF | ★★★★★ | ★★☆☆☆ | ★★★★★ |
| API Key 脱敏 | ★★★★★ | ★★★☆☆ | ★★★★★（7 种格式） |
| 规则引擎 | ★★★★★ | ★☆☆☆☆ | ★★★★★（DSL + 4 默认规则） |
| Hooks | ★★★★★ | ★☆☆☆☆ | ★★★★★ |
| 三级审批 | ★★★★★ | ★★★☆☆ | ★★★★★ |
| OS 沙箱 | ★★★★★ | ★☆☆☆☆ | ☆☆☆☆☆ |
| ML 分类器 | ★★★★☆ | ☆☆☆☆☆ | ☆☆☆☆☆ |

**对比**：
- Iron 在路径/命令/SSRF/规则/Hooks/审批上与 Claude Code 持平
- Iron 缺少 OS 沙箱和 ML 分类器（Claude Code 独有）
- Iron 安全防护完整度远超 OpenCode

### 9.2 敏感信息防护

| 防护 | Claude Code | OpenCode | **Iron Agent** |
|------|-------------|----------|---------------|
| API Key 不落盘 | ✅ | ❌ | ✅（多策略） |
| 环境变量优先 | ✅ | ✅ | ✅ |
| MCP env 过滤 | ✅ | ❌ | ✅ |
| MCP headers 脱敏 | ✅ | ❌ | ✅ |
| 错误消息脱敏 | ✅ | ❌ | ✅ |
| 配置文件脱敏 | ✅ | ❌ | ✅ |

---

## 十、测试覆盖对比

| 维度 | Claude Code | OpenCode | **Iron Agent** |
|------|-------------|----------|---------------|
| 测试用例数 | 未公开 | 未公开 | **738 passed** |
| 测试文件数 | 未公开 | 未公开 | **28** |
| 测试代码行数 | 未公开 | 未公开 | **9,977** |
| 测试比例 | 未公开 | 未公开 | **0.61** |
| 集成测试 | ✅ | ✅ | ✅ |
| 安全测试 | ✅ | ❌ | ✅ |
| Mock LLM | ✅ | ✅ | ✅（`_ScriptedLLM`） |
| 测试隔离 | ✅ | ✅ | ✅ |

**Iron 优势**：测试数据透明，测试比例 0.61 达到优秀线。

---

## 十一、差距总结

### 11.1 Iron 已达到/超越的维度

| 维度 | 状态 |
|------|------|
| 5 层压缩管道 | ✅ 与 Claude Code 持平 |
| Stop Hooks | ✅ 与 Claude Code 持平 |
| Prompt Caching | ✅ 与 Claude Code 持平 |
| 双 Agent 类型 | ✅ 与 Claude Code/OpenCode 持平 |
| DSL 规则引擎 | ✅ 与 Claude Code 持平 |
| PreToolUse/PostToolUse Hooks | ✅ 与 Claude Code 持平 |
| 三级审批持久化 | ✅ 与 Claude Code 持平 |
| PubSub 事件总线 | ✅ 与 Claude Code/OpenCode 持平 |
| SQLite WAL 持久化 | ✅ 与 Claude Code/OpenCode 持平 |
| MCP 客户端 | ✅ 安全防护完整度领先 |
| patch 工具 | ✅ 与 Claude Code 持平 |
| ToolSearchTool | ✅ 与 Claude Code 持平 |
| 流式 Markdown 渲染 | ✅ 与 Claude Code/OpenCode 持平 |
| 主题系统 | ✅ 与 Claude Code 持平 |
| 斜杠命令分组 | ✅ 与 Claude Code/OpenCode 持平 |
| 3 阶段启动管道 | ✅ 与 OpenCode 持平 |
| 特性门控 | ✅ 覆盖核心开关 |
| **嵌入式铁律引擎** | ✅ **独占** |
| **EmbedForge/EmbedGuard** | ✅ **独占** |
| **MCU 配置** | ✅ **独占** |
| **Dream/Distill 记忆** | ✅ **独占** |
| **doom_loop 二级检测** | ✅ **独占** |
| **思考时间显示** | ✅ **独占** |
| **API Key 多策略** | ✅ **领先** |

### 11.2 Iron 仍存在的差距

| 差距 | 优先级 | 说明 | 对标项目 |
|------|--------|------|----------|
| **代码规模** | — | 16K vs 120K，工具数量 18 vs 40+ | Claude Code |
| **插件市场** | 低 | 无第三方扩展机制 | Claude Code |
| **OS 沙箱** | 中 | 无操作系统级沙箱 | Claude Code |
| **ML 分类器** | 低 | 无机器学习权限分类 | Claude Code |
| **远程/SSH 模式** | 低 | 无远程运行能力 | Claude Code |
| **Vim 模式** | 低 | 特性门控已就位，未实现 | Claude Code |
| **向量语义搜索** | 长期 | 无 embedding 索引 | Claude Code |
| **代码索引/语义理解** | 长期 | 无真正的代码理解 | Claude Code |
| **Skills 可执行机制** | 中期 | 当前为 prompt 注入 | Claude Code |
| **LSP 主循环集成** | 中期 | 仅客户端，未深度集成 | Claude Code/OpenCode |
| **流式中断恢复** | 中 | 当前 fallback 到非流式 | Claude Code |
| **88 特性标记** | — | Iron 20 个，覆盖核心即可 | Claude Code |
| **207 命令** | — | Iron 20+，覆盖核心即可 | Claude Code |
| **engine.py 拆分** | 中 | 2102 行，圈复杂度待优化 | — |

### 11.3 Iron 的独占优势

| 优势 | 说明 |
|------|------|
| **嵌入式铁律引擎** | 11 条铁律 + 7 反模式 + 4 默认权限规则 |
| **EmbedForge/EmbedGuard** | PlatformIO/Keil/CMake/ESP-IDF + 内存/中断/时序分析 |
| **MCU 配置** | STM32G431/F407 profile |
| **嵌入式专用 Agent** | build/embed/plan/verify/explore |
| **Dream/Distill 记忆** | 7天/30天自动整理，参考 MiMo Code |
| **doom_loop 二级检测** | 连续3次 + 循环模式长度 2/3/4 |
| **API Key 多策略** | env/disk/none 三选一 |
| **思考时间显示** | `⏱ 用时 Xs` |

---

## 十二、演进建议

### 12.1 短期（1-2 周）

| 任务 | 对标 | 说明 |
|------|------|------|
| engine.py 拆分 | — | 拆分 `process()` 的 `_handle_event` 子方法 |
| LSP 主循环集成 | Claude Code/OpenCode | LSP diagnostics 注入到 ReAct 循环 |
| 流式中断恢复 | Claude Code | 实现 resume 而非 fallback |

### 12.2 中期（1-2 月）

| 任务 | 对标 | 说明 |
|------|------|------|
| Skills 可执行机制 | Claude Code | 从 prompt 注入升级为可执行 |
| 向量语义搜索原型 | Claude Code | 引入 chromadb 或纯文本 embedding |
| OS 沙箱 | Claude Code | 至少 Windows/macOS 限制写范围 |

### 12.3 长期（3-6 月）

| 任务 | 对标 | 说明 |
|------|------|------|
| 代码索引/语义理解 | Claude Code | tree-sitter + 语义索引 |
| 插件市场 | Claude Code | 第三方扩展机制 |
| ML 权限分类器 | Claude Code | 机器学习辅助权限决策 |
| 远程/SSH 模式 | Claude Code | 远程运行能力 |

### 12.4 保持优势

| 维度 | 说明 |
|------|------|
| 嵌入式铁律引擎 | 持续扩充规则库 |
| EmbedForge/EmbedGuard | 持续集成新工具链 |
| Dream/Distill | 优化记忆整理算法 |
| doom_loop 检测 | 持续优化检测策略 |

---

## 十三、总结

**Iron Agent v2.5.0+ 在通用能力上已达到 OpenCode 同等水平，在嵌入式特色上独占鳌头。**

**核心结论：**
1. **L1-L6 六层架构完整**，与 Claude Code / OpenCode 对标
2. **安全防护完整度与 Claude Code 持平**，超越 OpenCode
3. **5 层压缩管道 + Stop Hooks + Prompt Caching 与 Claude Code 持平**
4. **嵌入式特色是独占优势**，两参考项目都没有
5. **主要差距在代码规模和高级功能**（向量搜索、代码索引、插件市场）

**与 Claude Code 的差距：**
- 代码规模 16K vs 120K（Iron 更精简，但工具数量少）
- 无 OS 沙箱、ML 分类器、插件市场、远程/SSH 模式
- 无向量语义搜索、代码索引/语义理解
- Skills 仍是 prompt 注入

**与 OpenCode 的差距：**
- 命令数量 20+ vs 30+（差距小）
- 无 Vim 模式（特性门控已就位）
- LSP 主循环集成度待提升

**Iron 的独占优势：**
- 嵌入式铁律引擎 + EmbedForge/EmbedGuard + MCU 配置
- 嵌入式专用 Agent（build/embed/plan/verify/explore）
- Dream/Distill 记忆（7天/30天自动整理）
- doom_loop 二级检测
- API Key 多策略（env/disk/none）
- 思考时间显示

**总体评价：**
- **通用能力**：★★★★☆（与 OpenCode 持平，接近 Claude Code）
- **嵌入式特色**：★★★★★（独占）
- **安全防护**：★★★★★（与 Claude Code 持平）
- **测试覆盖**：★★★★☆（738 passed，测试比例 0.61）
- **架构合理**：★★★★☆（六层完整，部分模块待拆分）

**Iron Agent 是目前唯一专注嵌入式开发、架构对标 Claude Code/OpenCode、且保留独占嵌入式特色的 AI Agent CLI。**

---

## 附录 A · 文档参考

- [evaluation-v3.md](file:///d:/嵌入式-Agent/docs/evaluation-v3.md) — Iron 完整测评报告（A- 评级）
- [ARCHITECTURE-v2.md](file:///d:/嵌入式-Agent/docs/ARCHITECTURE-v2.md) — Iron 当前架构文档
- [architecture-framework.md](file:///d:/嵌入式-Agent/docs/architecture-framework.md) — 19 个 P 任务开发框架
- [cli-agent-architecture.md](file:///d:/嵌入式-Agent/cli-agent-architecture.md) — Claude Code & OpenCode 架构深度解析（参考来源）
- [测评.md](file:///d:/嵌入式-Agent/测评.md) — Iron v2.4.0 评测报告（B+ 评级）

## 附录 B · 外部参考

- [arXiv 2604.14228] Dive into Claude Code — https://arxiv.org/html/2604.14228
- y-agent.github.io — Inside Claude Code — https://y-agent.github.io/inside-claude-code/
- cefboud.com — Inside OpenCode — https://cefboud.com/posts/coding-agents-internals-opencode-deepdive/
- GitHub: sst/opencode — https://github.com/sst/opencode
