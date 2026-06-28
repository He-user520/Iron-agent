# Iron CLI v2.0 设计文档

> 基于 OpenCode、MiMo Code、Claude Code 三大项目的深度分析，结合 iron 现状制定的第二版大更新方案。

---

## 一、研究来源与分析方法

| 项目 | 仓库 | 核心定位 |
|------|------|---------|
| OpenCode | github.com/anomalyco/opencode | 开源终端 AI Agent，模型无关，MIT 协议 |
| MiMo Code | github.com/XiaomiMiMo/MiMo-Code | 基于 OpenCode fork，专注长程任务与持久记忆 |
| Claude Code | Anthropic 官方 CLI | 闭源，单循环 Agent，极简设计哲学 |

**分析维度**：工具系统 / Agent 架构 / 权限模型 / 上下文管理 / 持久记忆 / 配置系统 / 任务管理 / 事件系统 / MCP 集成 / 嵌入式专属能力

---

## 二、三项目核心架构对比

### 2.1 Agent 架构

| 维度 | OpenCode | MiMo Code | Claude Code | **iron v2 方案** |
|------|----------|-----------|-------------|----------------|
| 循环模型 | Agentic Loop | Agentic Loop + Fork Agent | 单循环 + 一层子Agent | **Agentic Loop + 子Agent** |
| 多Agent | build/plan 两个主Agent + 子Agent | 同 OpenCode + compose | 单主Agent + 子Agent | **build/plan/embed 三个主Agent** |
| 子Agent | general/explore/scout | 同 OpenCode + checkpoint-writer | 最多一层派生 | **最多一层，结果写回主历史** |
| 步数控制 | 可配置 steps | 可配置 + goal 判断 | 无硬限制 | **可配置 MAX_STEPS，默认 15** |

**决策**：采用 Claude Code 的简洁哲学 + OpenCode 的多 Agent 体系。iron 特有 `embed` Agent 处理嵌入式专属任务。

### 2.2 工具系统

| 工具 | OpenCode | MiMo Code | Claude Code | **iron v2** |
|------|----------|-----------|-------------|-------------|
| 读文件 | `read` (分页) | 同 OpenCode | `read` (分页) | ✅ `read_file` (分页+目录) |
| 写文件 | `write` | 同 OpenCode | `write` | ✅ `write_file` |
| 精确编辑 | `edit` (old→new) | 同 OpenCode | `edit` (old→new) | ✅ `edit_file` (**新增**) |
| 命令执行 | `bash` | 同 OpenCode | `bash` | ✅ `run_command` |
| 代码搜索 | `grep` (ripgrep) | 同 OpenCode | `grep` (ripgrep) | ✅ `search_code` (**新增**) |
| 文件查找 | `glob` | 同 OpenCode | `glob` | ✅ `find_files` (**新增**) |
| 补丁应用 | `apply_patch` | 同 OpenCode | 无 | ❌ 暂不需要 |
| 网页获取 | `webfetch` | 同 OpenCode | `webfetch` | ⏳ v2.1 |
| 网页搜索 | `websearch` | 同 OpenCode | 无 | ⏳ v2.1 |
| 提问 | `question` | 同 OpenCode | 无 | ✅ `ask_user` (**新增**) |
| 任务列表 | `todowrite` | 同 OpenCode + 任务树 | `todowrite` | ✅ `task_track` (**新增**) |
| 嵌入式编译 | 无 | 无 | 无 | ✅ `embed_build` (**iron独有**) |
| 嵌入式烧录 | 无 | 无 | 无 | ✅ `embed_flash` (**iron独有**) |
| 静态分析 | 无 | 无 | 无 | ✅ `embed_lint` (**iron独有**) |

**决策**：补齐 `edit_file`、`search_code`、`find_files`、`ask_user`、`task_track` 五个核心工具，加上 iron 独有的嵌入式工具。

### 2.3 edit_file 设计（参考 OpenCode 精确编辑）

```python
{
    "name": "edit_file",
    "parameters": {
        "path": "文件路径",
        "old_string": "要替换的精确文本（必须完全匹配）",
        "new_string": "替换后的文本",
        "replace_all": false  # 是否替换所有匹配
    }
}
```

关键行为：
- `old_string` 必须在文件中精确找到匹配，否则返回错误
- 找到多个匹配且 `replace_all=false` 时，要求提供更多上下文
- 保留原文件的行尾符（CRLF/LF）和 BOM
- 返回替换数量和 diff 预览

### 2.4 search_code / find_files 设计

```python
# search_code — 基于 ripgrep 的代码搜索
{
    "name": "search_code",
    "parameters": {
        "pattern": "正则表达式",
        "glob": "*.c",       # 可选：文件类型过滤
        "path": "src/",      # 可选：搜索目录
        "max_results": 20    # 可选：最大结果数
    }
}

# find_files — 基于 glob 的文件查找
{
    "name": "find_files",
    "parameters": {
        "pattern": "**/*.h",
        "path": ".",          # 可选
        "max_results": 50
    }
}
```

---

## 三、权限模型

### 3.1 三项目对比

| 维度 | OpenCode | MiMo Code | Claude Code |
|------|----------|-----------|-------------|
| 默认策略 | 全部 allow | 同 OpenCode | 大部分 allow |
| 编辑文件 | `edit` 权限控制 | 同 OpenCode | 自动 allow |
| 执行命令 | `bash` 权限控制（支持通配符） | 同 OpenCode | 自动 allow |
| 读文件 | `read` 权限（.env 默认 deny） | 同 OpenCode | 自动 allow |
| 敏感文件 | .env 默认拒绝 | 同 OpenCode | 无特殊处理 |
| 外部目录 | `external_directory` 独立控制 | 同 OpenCode | 无特殊处理 |
| 死循环保护 | `doom_loop`（同一工具连续3次相同输入） | 同 OpenCode | 无 |

### 3.2 iron v2 权限方案

```
权限层级：
├── 读取类（read_file, find_files, search_code）→ 自动 allow
├── 项目内写入（write_file, edit_file）→ 自动 allow
├── 安全命令（gcc, make, cmake, pio, python, cargo 等）→ 自动 allow
├── 危险命令（rm, sudo, git push, pip install, curl, wget）→ ask
├── 敏感文件（.env, credentials, secret）→ ask
├── 项目目录外操作 → ask
└── doom_loop 保护（同一工具连续3次相同输入）→ ask
```

配置文件 `.iron/permissions.json`：
```json
{
    "read": "allow",
    "edit": "allow",
    "bash": {
        "*": "allow",
        "rm *": "ask",
        "sudo *": "deny",
        "git push *": "ask",
        "pip install *": "ask"
    },
    "external_directory": "ask"
}
```

**新增**：`doom_loop` 保护 — 防止 AI 陷入重复调用同一工具的死循环。

---

## 四、上下文管理

### 4.1 三项目对比

| 机制 | OpenCode | MiMo Code | Claude Code |
|------|----------|-----------|-------------|
| 自动压缩 | ✅ token 超限自动摘要 | ✅ 同 OpenCode | ✅ `/compact` 命令 |
| 摘要模板 | 结构化 Markdown（Goal/Progress/Decisions/Next Steps/Critical Context/Files） | 同 OpenCode | 简单摘要 |
| 压缩触发 | 自动（token 接近上限时） | 自动 + 手动 | 手动为主 |
| 思考块剥离 | 无（无扩展思考） | 无 | ✅ 自动剥离历史 thinking |
| 工具输出截断 | ✅ 2000 字符 | ✅ 同 OpenCode | ✅ 自动截断 |
| 上下文可视化 | 无 | 无 | ✅ `/context` 命令 |

### 4.2 iron v2 上下文方案

```
上下文管理流程：
1. 每次请求前，估算消息总 token 数
2. 如果超过 MAX_CONTEXT_TOKENS（12000）：
   a. 保留最近 KEEP_RECENT_MESSAGES（8）条不压缩
   b. 将旧消息序列化为文本
   c. 用 LLM（小模型）生成结构化摘要
   d. 用摘要替换旧消息
3. 工具输出自动截断到 2000 字符
4. /compact 命令手动触发压缩
5. /context 命令显示 token 使用情况
```

**改进点**（对比 v1）：
- `MAX_CONTEXT_TOKENS` 从 8000 提升到 12000
- `KEEP_RECENT_MESSAGES` 从 6 提升到 8
- 新增 `/compact` 和 `/context` 命令
- 新增工具输出自动截断

---

## 五、持久记忆（MiMo Code 核心创新）

### 5.1 MiMo Code 4层记忆架构

```
项目目录/
├── MEMORY.md              ← 项目持久记忆（架构决策、规则、知识）
├── checkpoint.md          ← 会话检查点（结构化状态快照）
├── notes.md               ← Agent 临时笔记
└── tasks/
    └── <id>/
        └── progress.md    ← 任务进度日志
```

**Cycle 机制**：上下文窗口 20%→45%→70% 时主动 checkpoint → 快满时 rebuild 新窗口

**Dream/Distill**：Dream 每7天整理记忆，Distill 每30天将重复行为固化为技能

### 5.2 iron v2 持久记忆方案

```
.iron/
├── memory/
│   ├── MEMORY.md          ← 项目持久记忆
│   ├── checkpoint.md      ← 最近会话检查点
│   └── tasks/
│       └── <id>/
│           └── progress.md
├── permissions.json       ← 权限配置
├── instructions.md        ← 项目指令
└── rules/                 ← 编码规则
```

**MEMORY.md 内容**：
```markdown
# 项目记忆

## 架构决策
- 使用工具调用模式代替意图分类
- 采用 Agentic Loop 处理错误恢复

## 开发规范
- 嵌入式 C 代码必须通过 EmbedGuard 检查
- 所有寄存器访问使用 volatile

## 已知问题
- Windows 下 gcc 路径需要配置
```

**注入策略**：每次会话开始时，按 token 预算注入 checkpoint + MEMORY.md 到系统提示。

---

## 六、Agent 系统

### 6.1 三项目对比

| 维度 | OpenCode | MiMo Code | Claude Code |
|------|----------|-----------|-------------|
| 主Agent | build + plan | build + plan + compose | 单主 Agent |
| 子Agent | general/explore/scout | 同 OpenCode + checkpoint-writer | 自身克隆 |
| 切换方式 | Tab 键 | Tab 键 | 无（单一模式） |
| Agent 配置 | JSON/Markdown | JSON/Markdown | 无 |
| 权限差异 | plan: edit=ask, bash=ask | 同 OpenCode | 无 |

### 6.2 iron v2 Agent 方案

| Agent | 模式 | 权限 | 用途 |
|-------|------|------|------|
| **build** | 主Agent（默认） | 全部 allow | 日常开发，写代码+编译+运行 |
| **plan** | 主Agent | read=allow, edit=ask, bash=ask | 只读分析，代码审查 |
| **embed** | 主Agent | 全部 allow + 嵌入式工具 | 嵌入式专属：编译、烧录、静态分析 |

切换方式：`/agent build` `/agent plan` `/agent embed`

---

## 七、任务管理

### 7.1 Claude Code 的 TodoWrite 设计

Claude Code 让 AI 自己维护 To-Do 列表，解决"上下文腐烂"问题：
- AI 在任务开始时创建 todo 列表
- 执行过程中动态增删任务
- 用户能实时看到进度
- Prompt 中反复强调"参考 todo 列表"

### 7.2 iron v2 任务管理方案

```python
# task_track 工具
{
    "name": "task_track",
    "parameters": {
        "action": "create/update/complete/list",
        "task_id": "task_001",
        "title": "实现 edit_file 工具",
        "status": "in_progress",  # pending/in_progress/completed/failed
        "notes": "已完成 old_string 匹配逻辑"
    }
}
```

UI 展示：
```
  📋 任务列表
  ├── ✓ 分析 OpenCode 架构
  ├── ✓ 实现 edit_file 工具
  ├── ◎ 实现 search_code 工具    ← 进行中
  └── ○ 实现 Agent 切换
```

---

## 八、配置系统

### 8.1 OpenCode 配置架构

- 5级优先级：远程 → 全局 → 自定义 → 项目 → .opencode 目录
- JSONC 格式（支持注释）
- 支持变量替换 `{env:VAR}` `{file:path}`
- Agent/Command/Skill/Theme/Plugin 全部可配置

### 8.2 iron v2 配置方案

```
配置优先级（后覆盖前）：
1. 全局配置 ~/.config/iron/iron.yaml
2. 项目配置 .iron/config.yaml
3. 命令行参数 --model, --mcu 等

.iron/config.yaml 示例：
```yaml
project:
  name: "my-stm32-project"
  mcu: "stm32f407"
  build_system: "cmake"
  toolchain: "arm-none-eabi-gcc"

llm:
  backend: "openai"
  model: "gpt-4o"
  small_model: "gpt-4o-mini"    # 用于压缩/摘要等轻量任务
  api_key: "{env:OPENAI_API_KEY}"
  base_url: "{env:OPENAI_BASE_URL}"

agent:
  default: "build"
  max_steps: 15

permissions:
  bash:
    "*": "allow"
    "rm *": "ask"

mcp:
  embedforge:
    type: "local"
    command: ["python", "-m", "embedforge"]
```

---

## 九、事件系统

### 9.1 三项目对比

| 维度 | OpenCode | MiMo Code | Claude Code |
|------|----------|-----------|-------------|
| 事件驱动 | ✅ Effect-based 事件流 | 同 OpenCode | 内部事件流 |
| UI 渲染 | TUI (Go) | TUI (Go) | TUI (Node.js) |
| 流式输出 | ✅ 流式文本 + 工具状态 | 同 OpenCode | ✅ 流式输出 |

### 9.2 iron v2 事件系统

保留 v1 的 `AgentEvent` 异步生成器模式，新增事件类型：

```python
# 新增事件类型
AgentEvent("task_update", {"task_id": "...", "status": "..."})  # 任务状态变化
AgentEvent("agent_switch", {"from": "build", "to": "plan"})    # Agent 切换
AgentEvent("compact", {"before_tokens": 8000, "after_tokens": 2000})  # 上下文压缩
AgentEvent("memory_save", {"type": "checkpoint"})               # 记忆保存
AgentEvent("doom_loop", {"tool": "...", "count": 3})            # 死循环检测
```

---

## 十、MCP 集成

### 10.1 OpenCode MCP 设计

- 支持本地和远程 MCP 服务器
- JSON 配置 `mcp.server-name.type = "local" | "remote"`
- OAuth 自动认证
- 按 Agent 粒度控制 MCP 工具可用性
- 工具自动注册为 LLM 可调用工具

### 10.2 iron v2 MCP 方案

```yaml
# .iron/config.yaml
mcp:
  embedforge:
    type: "local"
    command: ["python", "-m", "embedforge.mcp_server"]
    enabled: true
  embedguard:
    type: "local"
    command: ["python", "-m", "embedguard.mcp_server"]
    enabled: true
```

MCP 工具自动合并到工具列表，与内置工具统一管理。

---

## 十一、iron 独有：嵌入式专属能力

### 11.1 embed_build 工具

```python
{
    "name": "embed_build",
    "parameters": {
        "target": "flash",      # flash/debug/test
        "clean": false          # 是否先清理
    }
}
```

行为：调用 EmbedForge 编译工具链，返回 Flash/RAM 占用、编译输出、错误信息。

### 11.2 embed_flash 工具

```python
{
    "name": "embed_flash",
    "parameters": {
        "probe": "stlink",      # 调试探针
        "firmware": "build/firmware.hex"
    }
}
```

### 11.3 embed_lint 工具

```python
{
    "name": "embed_lint",
    "parameters": {
        "files": ["main.c"],
        "rules": ["memory_safety", "interrupt_safety"]
    }
}
```

行为：调用 EmbedGuard 静态分析，返回问题列表和修复建议。

---

## 十二、实施计划

### Phase 1：工具系统升级（1周）
- [ ] 实现 `edit_file`（精确编辑，参考 OpenCode write.ts/edit.ts）
- [ ] 实现 `search_code`（基于 ripgrep 或 Python fallback）
- [ ] 实现 `find_files`（基于 glob）
- [ ] 实现 `ask_user`（向用户提问）
- [ ] 升级 `read_file` 支持分页

### Phase 2：Agent 系统（1周）
- [ ] 实现 build/plan/embed 三个主 Agent
- [ ] 实现 `/agent` 切换命令
- [ ] 每个 Agent 独立的权限配置
- [ ] 子 Agent 支持（最多一层）

### Phase 3：任务管理 + 记忆增强（1周）
- [ ] 实现 `task_track` 工具
- [ ] 实现 doom_loop 检测
- [ ] 升级记忆系统（MEMORY.md + checkpoint 自动维护）
- [ ] 实现 `/compact` 和 `/context` 命令

### Phase 4：配置系统 + MCP（1周）
- [ ] 实现 YAML 配置系统
- [ ] 实现配置优先级合并
- [ ] 实现 MCP 客户端
- [ ] 嵌入式工具通过 MCP 集成

### Phase 5：嵌入式专属能力（1周）
- [ ] embed_build / embed_flash / embed_lint 工具
- [ ] 嵌入式 Agent 专用 prompt
- [ ] EmbedForge/EmbedGuard MCP 适配

### Phase 6：测试与优化（持续）
- [ ] 端到端测试
- [ ] 性能优化（token 估算精确化）
- [ ] 文档编写

---

## 十三、技术选型总结

| 决策 | 来源 | 理由 |
|------|------|------|
| Agentic Loop | OpenCode | 工具失败不终止，AI 自动恢复 |
| 工具调用模式 | OpenCode | 不做意图分类，AI 自主决策 |
| 结构化摘要模板 | OpenCode | Goal/Progress/Decisions 比简单文本更有效 |
| 4层持久记忆 | MiMo Code | MEMORY.md + checkpoint 跨会话连续 |
| Task 进度树 | MiMo Code | 任务跟踪 + 检查点集成 |
| 单循环 + 一层子Agent | Claude Code | 简洁、可调试、不易出错 |
| TodoWrite 自维护 | Claude Code | 解决上下文腐烂，AI 自己管进度 |
| .env 默认拒绝 | OpenCode | 安全最佳实践 |
| doom_loop 保护 | OpenCode | 防止 AI 死循环 |
| 精确编辑（old→new） | OpenCode + Claude Code | 比全文件覆盖更安全 |
| ripgrep 搜索 | Claude Code | LLM 搜索 > RAG 搜索 |
| 多模型分级 | Claude Code | 小模型处理压缩/摘要，省钱 |
| 嵌入式专属工具 | iron 独有 | EmbedForge 编译/烧录 + EmbedGuard 静态分析 |

---

## 十四、iron v1 → v2 变更清单

### 新增文件
```
iron/
├── tools/
│   ├── __init__.py
│   ├── edit_file.py       ← 精确编辑
│   ├── search_code.py     ← 代码搜索
│   ├── find_files.py      ← 文件查找
│   ├── ask_user.py        ← 向用户提问
│   ├── task_track.py      ← 任务管理
│   ├── embed_build.py     ← 嵌入式编译
│   ├── embed_flash.py     ← 嵌入式烧录
│   └── embed_lint.py      ← 嵌入式静态分析
├── agent/
│   ├── memory.py          ← (已有) 升级
│   ├── engine.py          ← (已有) 重构
│   └── agents/            ← Agent 定义
│       ├── build.md
│       ├── plan.md
│       └── embed.md
├── config/
│   └── settings.py        ← (已有) 重构为 YAML
└── mcp/
    ├── __init__.py
    └── client.py           ← MCP 客户端
```

### 变更文件
```
iron/
├── agent/engine.py        ← 工具注册改为模块化，新增 agentic loop 增强
├── cli/main.py            ← 新增 /agent, /compact, /context 命令
├── llm/backend.py         ← 支持 small_model 分级调用
└── config/settings.py     ← YAML 配置 + 优先级合并
```

---

*文档生成时间：2026-06-24*
*研究范围：OpenCode v1.17+ / MiMo Code v0.1.0 / Claude Code (2026)*
