# Iron 嵌入式 AI 开发 Agent — 项目架构文档

> **版本**: 2.3.1  
> **更新时间**: 2026-06-25  
> **参考项目**: OpenCode、Claude Code、MiMo Code  
> **维护规则**: 每次重大更新后需重新生成本文档

---

## 一、项目总览

Iron 是一个面向嵌入式开发的 AI Agent CLI，参考 OpenCode（工具调用架构）、Claude Code（skill/mcp/记忆设计）、MiMo Code（4 层记忆架构）三大项目设计。

### 三段式架构

```
<project_root>/
├── iron/                    # 主包 — Agent 引擎 + CLI + 工具系统
├── 嵌入式-EmbedForge/       # 内置编译/烧录/仿真工具（MCP Server）
└── 嵌入式-embedguard/       # 内置静态分析工具（MCP Server）
```

- **iron 主包**：Agent 引擎、CLI 界面、工具系统、记忆系统、MCP 客户端
- **EmbedForge**：独立 MCP Server，提供 PlatformIO/CMake/Make/ESP-IDF/Keil/GCC 编译，ST-Link/J-Link/CMSIS-DAP 烧录
- **EmbedGuard**：独立 MCP Server，提供 tree-sitter AST 级静态分析（15 条 MISRA 规则）

### 核心配置

| 文件 | 说明 |
|------|------|
| `pyproject.toml` | 包名 `iron-embedded-cli`，入口 `iron = "iron.cli.main:cli"`，Python >=3.10 |
| `iron/__init__.py` | `__version__ = "2.3.1"` |
| `.iron-agent/` | 项目级配置（编码规则 + MCU 信息 + 指令） |
| `.iron/memory/` | 项目持久记忆（MEMORY.md + checkpoint.md + tasks/） |
| `.iron/skills/` | 用户自定义 skill（.md 文件，YAML frontmatter） |
| `~/.iron/sessions/` | 全局会话历史（JSON 文件） |
| `~/.iron/config.yml` | 全局配置（LLM 后端 + API Key） |

---

## 二、iron 主包详细架构

### 2.1 目录结构

```
iron/
├── __init__.py                    # 版本号
├── __main__.py                    # python -m iron 入口
│
├── agent/                          # Agent 引擎层
│   ├── engine.py                   # AgentEngine — Agentic Loop 核心
│   ├── memory.py                   # 4 层记忆系统
│   ├── conversation.py             # 会话管理（保存/恢复/历史）
│   ├── prompt_builder.py           # 三层 Prompt 构建
│   ├── agent_manager.py            # Agent 管理器（加载/切换）
│   └── agents/                     # 内置 Agent 定义（.md）
│       ├── build.md                # 默认开发 Agent（全权限）
│       ├── embed.md                # 嵌入式专用 Agent
│       └── plan.md                 # 只读分析 Agent
│
├── cli/                            # 命令行界面层
│   ├── main.py                     # Click CLI + 交互式会话 + 20 个斜杠命令
│   ├── ui.py                       # Rich + prompt_toolkit UI 组件
│   └── theme.py                    # 颜色/符号/面板标题常量
│
├── llm/                            # LLM 后端层
│   └── backend.py                  # 4 种后端：OpenAI/Anthropic/Ollama/Echo
│
├── tools/                          # 工具系统（模块化注册）
│   ├── __init__.py                 # create_default_registry() — 注册 12 个工具
│   ├── base.py                     # BaseTool 抽象基类
│   ├── registry.py                 # ToolRegistry 注册中心
│   ├── edit_file.py                # 精确文本替换
│   ├── search_code.py              # 正则代码搜索
│   ├── find_files.py               # glob 文件查找
│   ├── ask_user.py                 # 向用户提问
│   ├── task_track.py               # 任务进度跟踪
│   ├── embed_build.py              # 嵌入式编译（EmbedForge）
│   ├── embed_flash.py              # 嵌入式烧录（EmbedForge）
│   ├── embed_lint.py               # 嵌入式静态分析（EmbedGuard）
│   ├── remember.py                 # 保存记忆到 MEMORY.md（用户主动记忆）
│   └── web_search.py               # 网页搜索/获取（DuckDuckGo + httpx）
│
├── skills/                         # 技能系统
│   ├── base.py                     # BaseSkill 抽象基类
│   └── registry.py                 # SkillRegistry + 8 个内置 skill + FileSkill
│
├── config/                         # 配置管理
│   └── settings.py                 # IronConfig（LLM/Project/MCP 配置）
│
├── rules/                          # 编码规则引擎（三层）
│   ├── iron_rules.py               # Layer 1: 11 条嵌入式铁律
│   ├── ai_antipatterns.py          # Layer 2: 7 条 AI 反模式
│   └── project_rules.py            # Layer 3: 项目级规则加载器
│
├── integrations/                   # 集成桥接
│   ├── embedforge_bridge.py        # EmbedForge 桥接（compile/flash/serial）
│   └── embedguard_bridge.py        # EmbedGuard 桥接（analyze）
│
└── mcp/                            # MCP 客户端
    └── client.py                   # MCPClient + MCPToolWrapper（JSON-RPC 2.0）
```

### 2.2 AgentEngine — Agentic Loop 核心

**文件**: `iron/agent/engine.py`

#### 核心流程

```
用户输入 → AI 返回工具调用 → 执行工具 → 收集结果 → 送回 AI → 循环
                                                          ↓
                                              AI 不再调工具 / chat() 终止
                                                          ↓
                                                        结束
```

#### 关键设计

| 机制 | 实现位置 | 说明 |
|------|----------|------|
| **任务完成驱动** | L495-506, L578-594 | 所有 task_track 任务完成时提示 AI 用 chat 收尾（参考 Claude Code task:complete） |
| **chat() 终止** | L312-318, L488-493 | chat 是终止性工具，调用后立即 break 循环 |
| **步数预警** | L508-527 | 剩余 5 步提示收尾，剩余 1 步强制收尾 |
| **MAX_STEPS 安全网** | L273-279 | 可配置（iron.yml max_steps），默认 50，最低 10 |
| **doom_loop 检测** | L411-422 | 同一工具连续 3 次相同调用则拦截 |
| **编译命令拦截** | L351-372 | run_command 中的 pio/gcc/make 自动重定向到 embed_build |
| **聊天内容写入源码拦截** | L320-334 | 检测聊天指示词写入 .c/.h 等文件时阻止 |
| **MCP 工具动态合并** | L294-307 | 首次 process 调用时连接 MCP 服务器，合并工具到 registry |
| **上下文压缩** | L277 | 调用 ContextCompactor.compact_if_needed |
| **硬截断兜底** | L547-556 | 步数耗尽时生成进度摘要，避免用户看到无解释的截断 |
| **Skill 自动触发** | L309-316, L698-730 | process() 开头匹配 skill，>0.5 的 prompt 注入 system prompt（参考 Claude Code） |
| **ask_user 回调** | L159, L510-515 | engine 设置 _question_callback，CLI 提供交互式提问 UI |
| **edit_file 权限+撤销** | L433-476, L1069-1080 | edit_file 检查 edit 权限，成功后记录到 _change_history，/undo 支持 edit action |
| **并行工具执行** | L380, L567-583, L623-625, L793-828 | 只读工具（search_code/find_files/web_search）并行执行，写工具前 flush |
| **Dream/Distill 记忆整理** | memory.py L371-610, engine.py L328-333 | 7天 Dream 提炼长期知识，30天 Distill 蒸馏核心洞察（参考 MiMo Code） |
| **MCP 三种传输** | mcp/client.py | stdio（本地子进程）+ SSE（远程）+ HTTP（Streamable HTTP） |
| **task_track 持久化** | L680-687 | 会话结束时调用 save_to_file 持久化任务进度到磁盘 |
| **Ollama tool_calls 解析** | backend.py L157-179 | arguments 可能是 JSON 字符串，自动 json.loads 解析 |

#### 工具调用分发

```
tool_calls = AI 返回的工具调用列表
  ├── [写工具前 flush 只读并行任务] → 保证结果顺序
  ├── chat → yield chat_response, 设置终止标志, continue
  ├── write_file → 权限检查 → 聊天内容检测 → _execute_write_file
  ├── edit_file → 权限检查 → tool.execute → 记录撤销历史(action=edit)
  ├── run_command → 编译命令检测 → _execute_run_command / 重定向 embed_build
  ├── read_file → _execute_read_file
  └── 其他（注册工具）
      ├── 只读工具(search_code/find_files/web_search) → asyncio.Task 并行执行
      └── 写工具 → doom_loop 检测 → tool_registry.get(name).execute()
[for 循环结束] → flush 剩余只读并行任务
```

#### 系统提示构建

系统提示包含：
1. PromptBuilder 构建的基础提示（铁律 + 反模式 + 项目规则）
2. 项目文件列表（前 50 个）
3. 构建系统 + MCU 配置
4. 持久记忆注入（checkpoint + MEMORY.md）
5. Agent 专属 prompt
6. 工具使用说明 + 10 条重要规则 + 6 条强制规则

### 2.3 记忆系统

**文件**: `iron/agent/memory.py`

#### 4 层记忆架构（参考 MiMo Code）

| 层级 | 存储 | 生命周期 | 注入方式 |
|------|------|----------|----------|
| **L1 会话内压缩** | 内存 | 单次会话 | ContextCompactor 超阈值时压缩 |
| **L2 检查点** | `.iron/memory/checkpoint.md` | 跨会话 | build_context_injection 自动注入 |
| **L3 项目记忆** | `.iron/memory/MEMORY.md` | 永久 | build_context_injection 自动注入 |
| **L4 任务进度** | `.iron/memory/tasks/<id>/progress.md` | 任务级 | task_track 工具保存 |

#### Dream/Distill 记忆整理（参考 MiMo Code 7天/30天）

| 机制 | 周期 | 触发 | 说明 |
|------|------|------|------|
| **Dream** | 7 天 | should_dream() | 读取 checkpoint + task progress，用 LLM 提炼长期知识，追加到 MEMORY.md |
| **Distill** | 30 天 | should_distill() | 读取整个 MEMORY.md，用 LLM 蒸馏为 5-10 条核心洞察，重写 MEMORY.md（原始备份到 archive/） |
| **自动触发** | 每次会话 | engine.process() 开头调用 maybe_dream_distill() | 根据 meta.json 时间戳判断 |
| **降级方案** | - | LLM 不可用时 | Dream 用关键词提取，Distill 用截断保留 |

#### 记忆工具

| 工具 | 触发方式 | 说明 |
|------|----------|------|
| `remember(section, content)` | AI 自动调用 / 用户说"记住..." | 写入 MEMORY.md 指定章节 |
| `save_checkpoint()` | engine 自动调用（会话结束） | 保存会话摘要 + 修改文件 |
| `task_track(action, ...)` | AI 调用 | 保存任务进度到 tasks/ |

#### 记忆注入流程

```
AgentEngine._build_system_prompt()
  └── self._memory.build_context_injection(token_budget=3000)
      ├── 读取 checkpoint.md（最近会话摘要）
      └── 读取 MEMORY.md（项目持久记忆）
      → 拼接到系统提示
```

### 2.4 工具系统

**文件**: `iron/tools/`

#### 12 个内置工具

| 工具 | 文件 | 用途 | 授权 |
|------|------|------|------|
| `edit_file` | edit_file.py | 精确文本替换（old→new） | 自动 |
| `search_code` | search_code.py | 正则代码搜索 | 自动 |
| `find_files` | find_files.py | glob 文件查找 | 自动 |
| `ask_user` | ask_user.py | 向用户提问 | - |
| `task_track` | task_track.py | 任务进度管理 | 自动 |
| `embed_build` | embed_build.py | 嵌入式编译（EmbedForge） | - |
| `embed_flash` | embed_flash.py | 嵌入式烧录（EmbedForge） | 需授权 |
| `embed_lint` | embed_lint.py | 嵌入式静态分析（EmbedGuard） | 自动 |
| `remember` | remember.py | 保存记忆到 MEMORY.md | 自动 |
| `web_search` | web_search.py | 网页搜索/获取 | 自动 |
| `skill_create` | skill_create.py | 创建自定义技能 | 自动 |
| `mcp_config` | mcp_config.py | MCP 服务器配置管理 | 自动 |

#### 工具注册流程

```
create_default_registry()
  └── ToolRegistry()
      ├── register(EditFileTool())
      ├── register(SearchCodeTool())
      ├── ... 12 个内置工具
      └── [MCP 工具在 process() 首次调用时动态合并]
```

#### 工具 schema 格式

所有工具使用 OpenAI function calling 格式：

```python
{
    "type": "function",
    "function": {
        "name": "tool_name",
        "description": "...",
        "parameters": {
            "type": "object",
            "properties": {...},
            "required": [...]
        }
    }
}
```

### 2.5 MCP 集成

**文件**: `iron/mcp/client.py`

#### MCPClient 架构

```
IronConfig.mcp (YAML 配置)
  └── AgentEngine.__init__()
      └── MCPClient()
          ├── add_server(name, config)
          └── [process() 首次调用时]
              └── connect_all()
                  ├── connect_local(name, command)
                  │   ├── asyncio.create_subprocess_exec
                  │   ├── 发送 initialize (JSON-RPC 2.0)
                  │   ├── 发送 tools/list
                  │   └── 为每个工具创建 MCPToolWrapper
                  └── 合并到 ToolRegistry
```

#### MCP 配置格式（iron.yml）

统一为 Claude Code 风格（command: str + args: list + env: dict）：

```yaml
mcp:
  embedguard:
    type: local
    command: python              # 启动命令（字符串）
    args:                        # 命令参数列表
      - "-m"
      - "embedguard.mcp_server"
    env:                         # 环境变量（可选）
      API_KEY: xxx
    enabled: true
    timeout: 5000
```

MCPConfig.build_command() 返回 `[command] + args` 完整命令列表。
mcp_config 工具写入/读取统一用 `mcp` 键（兼容旧 `mcp_servers` 键）。

#### MCPToolWrapper

将 MCP 工具适配为 iron BaseTool：
- `name`: `{server_name}__{tool_name}`
- `schema`: 转换 MCP inputSchema 为 OpenAI function calling 格式
- `execute()`: 通过 JSON-RPC 调用 MCP 服务器

### 2.6 Skill 系统

**文件**: `iron/skills/registry.py`

#### 内置 Skill（8 个，stub）

| Skill | 触发关键词 | 说明 |
|-------|-----------|------|
| mcu-init | 新建项目/初始化 | MCU 项目初始化 |
| driver-gen | 写驱动/uart驱动 | 外设驱动生成 |
| peripheral-setup | 配置/gpio配置 | 外设配置 |
| bug-hunt | 不工作/hardfault | 问题诊断 |
| rtos-setup | freertos/任务 | RTOS 配置 |
| misra-check | misra/合规 | MISRA 检查 |
| power-optimize | 低功耗/省电 | 低功耗优化 |
| debug-helper | 调试/断点 | 调试助手 |

#### 用户自定义 Skill

从 `.iron/skills/*.md` 加载，格式参考 Claude Code skill：

```markdown
---
name: my-skill
description: 技能描述
trigger_patterns:
  - 关键词1
  - 关键词2
icon: 🎯
---
技能的 prompt 内容...
```

加载流程：
```
CLI 启动
  └── SkillRegistry()
      └── load_from_dir(".iron/skills/")
          └── _parse_skill_md() → FileSkill
```

### 2.7 LLM 后端

**文件**: `iron/llm/backend.py`

| 后端 | 默认模型 | Function Calling | 说明 |
|------|----------|------------------|------|
| OpenAIBackend | gpt-4o | ✅ 完整支持 | 兼容 DeepSeek/Together/国产平台 |
| AnthropicBackend | claude-sonnet-4 | ✅ 已修复 | 解析 tool_use blocks → OpenAI tool_calls |
| OllamaBackend | qwen2.5-coder:7b | ✅ 已修复 | arguments 字符串自动解析为 dict |
| EchoBackend | echo | ✅ 模拟 | 测试用 |

### 2.8 Agent 系统

**文件**: `iron/agent/agent_manager.py`

#### 3 个内置 Agent

| Agent | 文件 | 权限 | 说明 |
|-------|------|------|------|
| build | build.md | 全 allow | 默认开发 Agent |
| embed | embed.md | 全 allow | 嵌入式专用 |
| plan | plan.md | read=allow, edit/bash=ask | 只读分析 |

#### Agent 切换

```
/agent          → 列出所有 primary agent
/agent <name>   → 切换到指定 agent
```

### 2.9 CLI 命令

**文件**: `iron/cli/main.py`

#### 20 个斜杠命令

| 命令 | 说明 |
|------|------|
| `/code <需求>` | 描述需求，开始编码 |
| `/model` | 切换 AI 模型 |
| `/read <file>` | 读取文件内容 |
| `/write <file> <content>` | 写入文件 |
| `/edit <file> <old> <new>` | 编辑文件 |
| `/delete <file>` | 删除文件 |
| `/check` | 运行 EmbedGuard 静态分析 |
| `/build` | 编译项目 |
| `/flash` | 烧录固件 |
| `/monitor` | 串口监视器 |
| `/skill` | 技能中心 |
| `/rules` | 查看/管理编码规则 |
| `/config` | 配置管理 |
| `/history` | 查看历史记录 |
| `/resume [id]` | 恢复历史会话 |
| `/files` | 浏览项目文件 |
| `/undo` | 撤销上次修改 |
| `/agent [name]` | 切换 Agent |
| `/compact` | 手动压缩上下文 |
| `/context` | 查看 token 使用 |
| `/clear` | 清屏 |
| `/help` | 显示帮助 |
| `/quit` | 退出 |

### 2.10 安全防护机制

| 机制 | 实现位置 | 说明 |
|------|----------|------|
| **编译命令重定向** | engine.py L351-372 | run_command 中的 pio/gcc/make → embed_build |
| **聊天内容写入源码拦截** | engine.py L320-334 | 检测聊天指示词写入 .c/.h 等 |
| **doom_loop 检测** | engine.py L411-422 | 同一工具连续 3 次相同调用拦截 |
| **复合命令风险评估** | engine.py _evaluate_command_risk | 拆分 &&/||/｜/; 逐个检查 |
| **安全命令白名单** | engine.py _SAFE_COMMANDS | 60+ 个安全命令（where/pip show/git status 等） |
| **敏感文件保护** | engine.py _evaluate_write_risk | .env/credentials 等需授权 |
| **权限回调** | cli/main.py | y=允许 / n=拒绝 / a=全部允许 |

---

## 三、嵌入式-EmbedForge 架构

### 3.1 目录结构

```
嵌入式-EmbedForge/
├── embedforge/
│   ├── cli/main.py                 # CLI: dev/build/flash/monitor/init/fix/mcp/doctor
│   ├── core/
│   │   ├── orchestrator.py        # 闭环编排（GENERATE→COMPILE→FLASH→TEST→FIX）
│   │   ├── config.py               # 配置管理
│   │   ├── templates.py            # 代码模板
│   │   └── exceptions.py           # 异常定义
│   ├── servers/                    # MCP Server 实现
│   │   ├── build_server/           # 构建编译服务
│   │   │   ├── server.py           # Build MCP Server（compile/clean/info）
│   │   │   ├── platformio.py       # PlatformIO 适配器（含 _find_pio_command）
│   │   │   ├── cmake.py            # CMake 适配器
│   │   │   ├── esp_idf.py          # ESP-IDF 适配器
│   │   │   ├── gcc.py              # GCC 裸机适配器
│   │   │   ├── keil.py             # Keil 适配器
│   │   │   └── base.py             # BuildAdapter 基类
│   │   ├── hardware_server/        # 硬件交互服务
│   │   │   ├── server.py           # Hardware MCP Server
│   │   │   ├── flash.py            # 固件烧录（ST-Link/J-Link/OpenOCD）
│   │   │   ├── serial.py           # 串口通信
│   │   │   ├── debug.py            # 调试接口
│   │   │   └── simulation.py       # Wokwi 仿真
│   │   └── ai_server/             # AI 代码生成服务
│   │       ├── server.py           # AI MCP Server
│   │       ├── llm_backend.py      # LLM 后端
│   │       └── prompts.py          # 系统提示词
│   └── templates/                  # 项目模板
│       ├── arduino_basic/
│       ├── esp32_wifi/
│       ├── stm32_baremetal/
│       └── stm32_freertos/
└── tests/                          # 测试套件
```

### 3.2 PlatformIO 路径自动检测

**文件**: `嵌入式-EmbedForge/embedforge/servers/build_server/platformio.py`

检测顺序：
1. `pio` 在 PATH 中
2. 当前 Python: `python -m platformio`
3. Windows py launcher: `py -m platformio`
4. 常见系统 Python 路径（C:\Program Files\Python*, C:\Users\*\AppData\...）
5. pip scripts 目录: `C:\Users\*\AppData\Roaming\Python\*\Scripts\pio.exe`
6. 回退: `pio`

### 3.3 编译流程

```
embed_build(action="compile")
  └── EmbedForgeBuildServer.call_tool("compile", {project_dir})
      ├── detect_build_system(project_dir)
      │   └── 检测 platformio.ini / CMakeLists.txt / Makefile / ...
      ├── adapter = _ADAPTERS[build_system]
      └── subprocess.run(adapter.build_command())
          ├── encoding="utf-8", errors="replace"  # 修复 GBK 崩溃
          └── 返回 BuildResult(success, stdout, firmware_path, ...)
```

---

## 四、嵌入式-embedguard 架构

### 4.1 目录结构

```
嵌入式-embedguard/
├── embedguard/
│   ├── cli/main.py                 # CLI: check/fix/list-rules/validate/index/search
│   ├── core/
│   │   ├── pipeline.py             # AnalysisPipeline 分析管道
│   │   ├── ast_parser.py           # tree-sitter C AST 解析器
│   │   ├── rule_engine.py          # YAML 规则引擎
│   │   ├── autofixer.py            # 自动修复器
│   │   ├── report.py               # 报告生成器
│   │   └── analyzers/              # 8 个分析器
│   │       ├── memory_safety.py    # EMB001(malloc)/EMB002(递归)
│   │       ├── interrupt_safety.py # EMB004(ISR阻塞)/EMB010/EMB011
│   │       ├── timing.py           # EMB005(volatile)
│   │       ├── code_style.py       # EMB006(goto)/EMB007(魔术数)
│   │       ├── resource_usage.py   # EMB003(浮点)
│   │       ├── extended_memory.py  # EMB014/EMB015
│   │       ├── extended_resource.py # EMB008/EMB009
│   │       └── extended_timing.py  # EMB012/EMB013
│   ├── rules/                      # 15 条规则定义（YAML）
│   ├── datasheet/                  # 数据手册验证
│   │   ├── parser.py               # PDF 解析
│   │   ├── rag_validator.py        # 向量数据库验证
│   │   └── mcus/                   # MCU 定义（stm32f4.yml, stm32g4.yml）
│   └── mcp_server.py               # EmbedGuard MCP Server（6 个工具）
├── tests/                          # 测试套件（10 个测试文件）
├── vscode-extension/               # VS Code 扩展
└── github-action/                  # CI/CD 集成
```

### 4.2 静态分析流程

```
embed_lint(files=["src/"])
  └── 优先调用 EmbedGuard AnalysisPipeline
      ├── AST 解析（tree-sitter-c）
      ├── 8 个分析器并行运行
      └── 返回 findings 列表
  └── [EmbedGuard 不可用时] 降级到内置正则规则
      ├── volatile_missing
      ├── malloc_in_isr
      └── float_usage
```

---

## 五、数据流

### 5.1 用户输入到工具执行

```
用户输入
  ↓
cli/main.py: run_interactive()
  ↓
AgentEngine.process(user_input)
  ├── _build_system_prompt()  ← 注入记忆 + 规则 + Agent prompt
  ├── [首次] MCP connect_all() + 合并工具
  └── for step in range(30):
      ├── LLM.generate(system, messages, tools=schema)
      ├── _parse_tool_calls(resp)
      │   ├── 标准 tool_calls (OpenAI/Anthropic)
      │   ├── JSON 文本解析（兼容模式）
      │   └── 代码检测 → 包装为 write_file
      ├── 执行工具调用
      │   ├── chat → 终止循环
      │   ├── write_file → 聊天检测 → 写入
      │   ├── run_command → 编译检测 → 执行/重定向
      │   ├── read_file → 读取
      │   └── 注册工具 → doom_loop 检测 → execute()
      └── [chat 终止] / [无工具调用] / [达到 MAX_STEPS] → 结束
```

### 5.2 记忆持久化

```
会话进行中
  ├── remember(section, content) → MEMORY.md
  ├── task_track(...) → tasks/<id>/progress.md
  └── 上下文超阈值 → ContextCompactor 压缩

会话结束
  └── save_checkpoint(summary, files_changed) → checkpoint.md

下次会话启动
  └── _build_system_prompt()
      └── build_context_injection()
          ├── 读取 checkpoint.md
          └── 读取 MEMORY.md
          → 注入到系统提示
```

---

## 六、与 Claude Code / OpenCode 对比

### 6.1 功能对比

| 功能 | Claude Code | OpenCode | Iron v2.0 |
|------|------------|----------|-----------|
| 工具调用架构 | ✅ | ✅ | ✅ |
| edit_file | ✅ | ✅ | ✅ |
| search/grep | ✅ | ✅ | ✅ (search_code) |
| find_files | ✅ | ✅ | ✅ |
| ask_user | ✅ | ✅ | ✅ |
| task_track (TodoWrite) | ✅ | ✅ | ✅ |
| chat 终止 | ✅ | ✅ | ✅ (已修复) |
| 网页搜索 | ✅ WebSearch | ✅ | ✅ web_search |
| 网页获取 | ✅ WebFetch | ✅ | ✅ web_search(fetch) |
| 记忆持久化 | ✅ CLAUDE.md | ✅ | ✅ MEMORY.md + remember() |
| 上下文压缩 | ✅ | ✅ | ✅ ContextCompactor |
| MCP 集成 | ✅ | ✅ | ✅ MCPClient (已接入) |
| Skill 系统 | ✅ | ✅ | ✅ 8 内置 + 用户自定义 |
| 用户自定义 skill | ✅ | ✅ | ✅ .iron/skills/*.md |
| 会话恢复 | ✅ | ✅ | ✅ /resume |
| Agent 切换 | ✅ | ✅ | ✅ /agent |
| 权限模型 | ✅ | ✅ | ✅ y/n/a + 风险评估 |
| doom_loop 检测 | ✅ | ✅ | ✅ 3 次相同调用 |
| 嵌入式编译 | ❌ | ❌ | ✅ embed_build |
| 嵌入式烧录 | ❌ | ❌ | ✅ embed_flash |
| 嵌入式静态分析 | ❌ | ❌ | ✅ embed_lint |
| MISRA 规则 | ❌ | ❌ | ✅ 15 条 |
| MCU 寄存器验证 | ❌ | ❌ | ✅ EmbedGuard |

### 6.2 Iron 独有优势

1. **嵌入式专属工具**：embed_build/embed_flash/embed_lint 调用内置 EmbedForge/EmbedGuard
2. **编译命令拦截**：AI 用 run_command 调 pio/gcc 时自动重定向到 embed_build
3. **聊天内容写入源码拦截**：防止 AI 把回复写入 .c 文件
4. **PlatformIO 路径自动检测**：扫描系统 Python 路径找 pio
5. **11 条嵌入式铁律**：强制 HAL 库、禁动态内存、禁递归等
6. **MISRA C 静态分析**：tree-sitter AST 级精度

---

## 七、当前实现状态

### 7.1 已完成

| 模块 | 状态 | 说明 |
|------|------|------|
| AgentEngine Agentic Loop | ✅ | 任务完成驱动, chat() 终止, doom_loop 检测, 步数预警 |
| 工具系统（12 个工具） | ✅ | edit/search/find/ask/task/embed_build/flash/lint/remember/web_search/skill_create/mcp_config |
| 并行工具执行 | ✅ | 只读工具（search_code/find_files/web_search）并行执行 |
| 记忆系统（4 层） | ✅ | 压缩 + 检查点 + MEMORY.md + 任务进度持久化 |
| 用户主动记忆 | ✅ | remember(section, content) 工具 |
| task_track 持久化 | ✅ | 会话结束自动 save_to_file 到 .iron/memory/tasks/ |
| MCP 客户端 | ✅ | 已接入 AgentEngine，动态合并工具，支持 stdio/SSE/HTTP 三种传输 + env 环境变量 |
| MCP 配置格式统一 | ✅ | command:str + args:list + env:dict（Claude Code 风格） |
| Skill 系统 | ✅ | 8 内置 + 用户自定义 .md + 自动触发（match_score>0.5） |
| LLM 后端（4 种） | ✅ | OpenAI/Anthropic/Ollama(已修复 tool_calls)/Echo |
| Agent 系统（3 个） | ✅ | build/embed/plan，权限模型接入 engine |
| ask_user 回调 | ✅ | engine _question_callback + CLI 交互式提问 UI |
| edit_file 撤销 | ✅ | /undo 支持 edit action（new_string 替换回 old_string） |
| CLI 命令（20+） | ✅ | 含 /resume /agent /compact /context |
| 安全防护 | ✅ | 编译拦截 + 聊天写入拦截 + 权限模型 + 并行 flush |
| EmbedForge 集成 | ✅ | 编译/烧录/仿真 |
| EmbedGuard 集成 | ✅ | AST 级静态分析 + 降级方案 |
| 网页搜索 | ✅ | DuckDuckGo + httpx + HTML→Markdown 智能转换 |
| 会话恢复 | ✅ | /resume [id] |
| 测试覆盖 | ✅ | 227 个测试用例（test_core + test_backend + test_engine + test_mcp + test_security） |

### 7.2 待完善

| 项目 | 优先级 | 说明 |
|------|--------|------|
| - | - | 所有 P0/P1/P2/P3 项已完成，项目达到产品级标准 |

---

## 八、配置文件格式

### 8.1 iron.yml（项目级配置）

```yaml
llm:
  backend: openai          # openai/anthropic/ollama/echo
  model: gpt-4o
  api_key: sk-xxx
  base_url: https://api.openai.com/v1

project:
  mcu: stm32f407
  build_system: platformio
  project_dir: .

max_steps: 50              # 安全网步数上限（任务完成驱动为主）

mcp:
  embedguard:
    type: local
    command: python
    args:
      - "-m"
      - "embedguard.mcp_server"
    env:
      API_KEY: xxx
    enabled: true
    timeout: 5000
```

### 8.2 用户自定义 Skill 格式

`.iron/skills/my-skill.md`:

```markdown
---
name: my-skill
description: 我的自定义技能
trigger_patterns:
  - 关键词1
  - 关键词2
icon: 🎯
---
当用户提到关键词时，执行以下步骤：
1. 读取相关文件
2. 分析代码
3. 给出建议
```

### 8.3 Agent 定义格式

`.iron/agents/my-agent.md` 或 `iron/agent/agents/*.md`:

```markdown
---
name: my-agent
description: 我的 Agent
mode: primary
permissions:
  read: allow
  edit: allow
  bash: allow
---
Agent 的系统提示内容...
```

---

## 九、开发指南

### 9.1 添加新工具

1. 创建 `iron/tools/my_tool.py`，继承 `BaseTool`
2. 实现 `name`、`schema`、`execute()` 方法
3. 在 `iron/tools/__init__.py` 的 `create_default_registry()` 中注册
4. 在 `engine.py` 系统提示的工具列表中添加说明

### 9.2 添加新 Skill

方式一：创建内置 skill
1. 在 `iron/skills/registry.py` 中添加 skill 类
2. 加入 `BUILTIN_SKILLS` 列表

方式二：用户自定义 skill
1. 在项目 `.iron/skills/` 目录下创建 `.md` 文件
2. 使用 YAML frontmatter 格式

### 9.3 添加新 Agent

1. 在 `iron/agent/agents/` 或 `.iron/agents/` 下创建 `.md` 文件
2. 使用 YAML frontmatter 定义 name/description/mode/permissions

### 9.4 配置 MCP 服务器

在 `iron.yml` 中添加（或用 `mcp_config` 工具让 AI 自动配置）：

```yaml
mcp:
  my-server:
    type: local
    command: npx
    args:
      - "-y"
      - "@modelcontextprotocol/server-filesystem"
      - "."
    env:
      API_KEY: xxx
    enabled: true
    timeout: 5000
```

---

## 十、文件路径速查

### 核心文件

| 文件 | 说明 |
|------|------|
| `iron/agent/engine.py` | AgentEngine — Agentic Loop 核心 |
| `iron/agent/memory.py` | 4 层记忆系统 |
| `iron/cli/main.py` | CLI 主入口 + 交互式会话 |
| `iron/llm/backend.py` | 4 种 LLM 后端 |
| `iron/tools/__init__.py` | 工具注册工厂 |
| `iron/skills/registry.py` | Skill 注册 + 文件加载 |
| `iron/mcp/client.py` | MCP 客户端 |
| `iron/config/settings.py` | 配置管理 |

### 嵌入式工具

| 文件 | 说明 |
|------|------|
| `iron/tools/embed_build.py` | 编译工具（调用 EmbedForge） |
| `iron/tools/embed_flash.py` | 烧录工具（调用 EmbedForge） |
| `iron/tools/embed_lint.py` | 静态分析（调用 EmbedGuard） |
| `嵌入式-EmbedForge/embedforge/servers/build_server/server.py` | Build MCP Server |
| `嵌入式-EmbedForge/embedforge/servers/build_server/platformio.py` | PlatformIO 适配器 |
| `嵌入式-embedguard/embedguard/core/pipeline.py` | 分析管道 |

### 新增工具

| 文件 | 说明 |
|------|------|
| `iron/tools/remember.py` | 用户主动记忆工具 |
| `iron/tools/web_search.py` | 网页搜索/获取工具 |

---

## 十一、更新日志

### v2.3.0 (2026-06-25) — 安全加固与开源准备

**安全漏洞修复（P0 阻断级，12 项）**:
- 路径穿越漏洞修复：新增 `path_guard.py` 统一路径边界校验，修复 engine.py/edit_file/find_files/search_code/embed_lint/CLI /write /edit /delete 共 8 处路径穿越
- 命令注入修复：engine.py 命令风险评估增加 `$(...)`/反引号/`>`/`<`/`&` 元字符检测，危险关键词改用词边界匹配
- SSRF 修复：web_search 新增内网地址过滤（127.0.0.0/8、10.0.0.0/8、172.16.0.0/12、192.168.0.0/16、169.254.0.0/16），follow_redirects 改为 False
- RCE 修复：mcp_config test 动作改用 shutil.which 验证命令存在性，不真正执行
- API Key 安全：save() 不再落盘 API Key，支持环境变量 IRON_API_KEY/OPENAI_API_KEY 优先加载，Unix 下文件权限 600

**安全策略修复（P1，6 项）**:
- 授权回调空输入默认拒绝（safe-by-default）
- 权限异常时返回 False（fail-safe）
- 未知工具默认权限改为 "ask"（最小权限原则）
- ask_user 无 UI 时返回 need_user_input 而非自动选择
- 文件删除前二次确认
- 子进程环境变量过滤敏感信息（KEY/SECRET/TOKEN/PASSWORD）

**MCP 协议修复（P1，5 项）**:
- 添加 notifications/initialized 通知（三种传输）
- JSON-RPC 响应检查 error 字段
- 连接失败时清理子进程和 httpx 客户端
- SSE 解析增加 event 类型跟踪
- 分层超时配置（5s 连接 + 30s 读取）

**LLM 后端修复（P1，2 项）**:
- Ollama tool_calls 统一为 OpenAI 标准格式
- httpx.AsyncClient 添加 aclose() 方法，网络异常包装

**功能缺陷修复（P1，6 项）**:
- 权限回调改为异步（asyncio.to_thread），不再阻塞事件循环
- /flash 添加异常处理，spinner 不再卡死
- 中断机制修复（KeyboardInterrupt 设置 _interrupted 标志）
- 兼容模式移除自动包装（避免误写文件）
- 失败检测改用 json.loads 而非字符串匹配
- edit_file 撤销只替换第一个匹配

**开源准备**:
- 新增 LICENSE 文件（MIT）
- 新增 README.md（项目根目录）
- 新增 iron.example.yml 示例配置
- 完善 .gitignore（忽略 iron.yml/IDE/缓存等）
- 新增 .github/workflows/ci.yml CI 配置
- pyproject.toml 修复：tree-sitter 移为可选依赖，package-data 修正
- 文档清理本地路径泄露

**验证**: 168 个单元测试全部通过

### v2.3.1 (2026-06-25) — 修复完善版本

**安全修复完善**:
- 修复 deny 权限被绕过（engine.py write_file/edit_file/run_command 三个分支）
- 修复 IPv4-mapped IPv6 绕过 SSRF 防护（web_search.py，如 ::ffff:127.0.0.1）
- 修复 _execute_write_file 读取非 UTF-8 文件崩溃
- 修复命令注入未检测换行符（\n \r \x00）
- path_guard 重写：strict=True + 符号链接防护 + 边界校验
- 权限回调统一为 dict 签名 + fail-safe
- 命令注入元字符检测覆盖换行符
- 写文件读取移到授权后 + dangerous 路径硬阻断

**功能修复**:
- task_track notes 默认值改 None（不再清空原值）
- embed_lint/embed_flash f.parts 改 relative_to（修复绝对路径误判）
- mcp_config 旧格式 mcp_servers 迁移
- ESP32/ESP32-S3 FPU 事实修正（否→是单精度）
- embedguard 子项目补充 README.md 和 LICENSE
- 配置 save() 补全字段持久化
- /build /check 加 try/finally 保护 spinner

**MCP/LLM 加固**:
- _call_stdio 加 asyncio.Lock 并发保护
- 环境变量过滤扩展（10 个关键字）
- 超时后清理子进程
- 三种传输错误返回统一 RuntimeError
- httpx 网络异常包装
- 错误响应脱敏扩展到所有后端

**开源准备**:
- 新增 tests/test_security.py（30 用例覆盖 path_guard/SSRF/API Key/命令注入/写文件风险/IPv4-mapped IPv6/python -c/node -e/echo %VAR%）
- .gitignore 追加内部报告文件过滤
- embedguard 子项目开源就绪
- 文档工具表补全
- 测试用例数 168 → 206 → 215 → 227

### v2.2.2 (2026-06-25) — 回归测试遗留建议修复

**回归测试遗留建议**（Iron回归测试报告 4 项遗留，全部修复）:
- 遗留1: `iron check` 环境适配 — tree-sitter_c 未安装时给出具体 `pip install` 安装指引，区分 tree_sitter/embedguard 缺失场景
- 遗留2: subprocess 编码防御 — `run_command()` 统一添加 `encoding="utf-8", errors="replace"`，防止中文路径/错误信息导致 UnicodeDecodeError
- 遗留3: 版本号统一 — `__init__.py` 与 `pyproject.toml` 均为 2.2.x，`iron --version` 从 `__version__` 读取，彻底解决不一致问题
- 遗留4: `iron run` 非交互模式输出 — 检测 `sys.stdout.isatty()`，非 TTY 环境（管道/重定向）用 `Console(no_color=True, highlight=False, width=120)` 纯文本输出，避免 Rich 面板字符污染

**验证**: 168 个单元测试全部通过

### v2.2.1 (2026-06-25) — Hermes 测评 Bug 修复

**P0 必须修复**（Hermes 测评发现的 4 个 Bug）:
- Bug 1: engine.py L556 stderr=None 切片崩溃 — 改用 `(d.get("stderr") or "")[-2000:]`，None 安全
- Bug 2: `iron run` asyncio.run() 嵌套崩溃 — `_run_agent()` 改为 async，`run_single` 用 await，`run_interactive` 用 asyncio.run() 包装
- Bug 3: `iron check` 与 `iron doctor` 结果不一致 — doctor 新增 `_check_deep_import()` 检查核心子模块（embedforge.servers.build_server.server / embedguard.core.pipeline）
- Bug 4: subprocess 编码 UnicodeDecodeError — `run_command()` 添加 `encoding="utf-8", errors="replace"`

**P0 版本号统一**:
- `__init__.py` 版本号 2.0.0 → 2.2.1
- CLI `--version` 从硬编码改为从 `__version__` 读取
- `pyproject.toml` 版本号同步更新

**P1 工程化改进**:
- pyproject.toml 版本号同步
- match_score 经验证无类绑定 bug（FileSkill 正确继承 BaseSkill.match_score）

**P2 体验优化**:
- init 生成的 coding-standards.md 内容从 155 字符扩展到完整规范（命名/HAL/内存安全/中断安全/代码风格）
- init 生成的 instructions.md 内容从 61 字符扩展到完整指令（项目概述/开发要求/构建系统/安全铁律）

### v2.2.0 (2026-06-25)

**P3 产品级提升**:
- MCP SSE/HTTP 传输支持：新增 connect_sse() 和 connect_http() 方法，支持 stdio/SSE/HTTP 三种传输（P3-1）
  - SSE: GET /sse 建立 SSE 连接 + POST /messages 发送请求
  - HTTP: Streamable HTTP，POST 单一端点
  - call_tool 根据传输类型自动分发
  - disconnect_all 支持关闭 httpx 客户端
  - MCPConfig 新增 headers 字段（SSE/HTTP 自定义请求头）
  - engine.py MCP 加载逻辑支持 SSE/HTTP（url + headers）
- Dream/Distill 记忆整理：实现 MiMo Code 的 7天/30天记忆整理机制（P3-2）
  - Dream（7天）：读取 checkpoint + task progress，用 LLM 提炼长期知识，追加到 MEMORY.md
  - Distill（30天）：读取 MEMORY.md，用 LLM 蒸馏为 5-10 条核心洞察，重写 MEMORY.md（原始备份到 archive/）
  - meta.json 记录上次执行时间，maybe_dream_distill() 自动判断是否需要执行
  - engine.process() 开头自动调用 maybe_dream_distill()
  - LLM 不可用时降级：Dream 用关键词提取，Distill 用截断保留
- 并行工具扩展：新增 _is_readonly_tool() 方法和 _READONLY_ACTIONS 配置（P3-3）
  - 完全只读：search_code/find_files/web_search
  - 特定 action 只读：embed_build(action=info)/task_track(action=list)/mcp_config(action=list/search)
  - read_file 有专门处理（含 UI 事件），不走并行分支

### v2.1.0 (2026-06-25)

**P0 阻断级修复**:
- ask_user 回调接入：engine 设置 _question_callback，CLI 提供交互式提问 UI（P0-1）
- last_options 作用域修复：外层声明，_run_agent 签名添加参数（P0-2）
- Skill 自动触发：process() 开头调用 _match_skills()，匹配的 skill prompt 注入 system prompt（P0-3，参考 Claude Code）
- Ollama tool_calls 解析：arguments 可能是 JSON 字符串，自动 json.loads 解析（P0-4）

**P1 核心功能完善**:
- edit_file 接入撤销历史和权限检查：edit_file 检查 edit 权限，成功后记录到 _change_history，/undo 支持 edit action（P1-1）
- task_track 持久化：会话结束时调用 save_to_file 持久化任务进度到 .iron/memory/tasks/（P1-2）
- 并行工具执行：只读工具（search_code/find_files/web_search）用 asyncio.gather 并行执行，写工具前 flush 保证顺序（P1-3，参考 Claude Code）
- MCP 配置格式统一：command:str + args:list + env:dict（Claude Code 风格），MCPConfig.build_command()，mcp_config 工具统一用 mcp 键（P1-4）

**P2 测试覆盖**:
- 新增 test_backend.py：41 个测试，覆盖 LLMResponse/EchoBackend/OllamaBackend tool_calls 解析/create_backend/OpenAIBuildUrl
- 新增 test_engine.py：48 个测试，覆盖 undo_last(edit action)/_check_permission_with_callback/_flush_readonly_tasks/_match_skills/_check_task_completion/MAX_STEPS/_evaluate_command_risk
- 新增 test_mcp.py：46 个测试，覆盖 MCPConfig.build_command/MCPClient/MCPToolWrapper/IronConfig MCP 加载/mcp_config 工具键名统一
- 全部 168 个测试通过

**架构改进**:
- _check_permission_with_callback 从 generator 改为普通函数返回 (allowed, event) 元组
- _match_skills 清理死代码（run_until_complete 在 async 上下文不可用）
- MCPClient.connect_local 支持 env 环境变量
- mcp_config 工具 _list_servers 显示 full_command 和 env_keys（不泄露 value）

### v2.0.0 (2026-06-25)

**新增功能**:
- `remember` 工具：AI 可主动保存知识到 MEMORY.md
- `web_search` 工具：网页搜索（DuckDuckGo）+ 网页内容获取
- `skill_create` 工具：AI 通过自然语言创建自定义技能，保存到 .iron/skills/*.md
- `mcp_config` 工具：AI 搜索 GitHub 找 MCP、添加/列出/测试/移除 MCP 配置
- MCP 客户端接入 AgentEngine：从配置加载外部 MCP 服务器，动态合并工具
- 用户自定义 skill 加载：从 `.iron/skills/*.md` 加载
- 用户自定义 agent：从 `.iron/agents/*.md` 加载
- `/resume` 命令：恢复历史会话
- Anthropic 后端 tool_use 解析修复：Claude function calling 现在可用
- Ollama 后端 tools 参数支持：本地模型也能用工具调用
- 任务完成驱动：task_track 所有任务完成时提示 AI 用 chat 收尾（参考 Claude Code task:complete）
- 步数预警机制：剩余 5 步提示收尾，剩余 1 步强制收尾
- 硬截断兜底：步数耗尽时生成进度摘要，避免用户看到无解释的截断
- Agent 权限模型接入 engine：edit/bash=ask 时触发授权回调
- 8 个内置 skill 提供实际 prompt 注入（不再是 stub）
- CLI /check /flash /monitor 命令真正接入 EmbedForge/EmbedGuard bridge

**修复**:
- chat() 调用后 Agentic Loop 不终止导致重复输出（核心 bug 修复）
- Anthropic 后端不解析 tool_use blocks
- Ollama 后端不支持 tools 参数

**架构改进**:
- MAX_STEPS 从硬编码 30 改为可配置（iron.yml max_steps），默认 50，仅作安全网
- 系统提示新增"任务完成驱动"章节，明确终止语义
- 系统提示新增 chat() 终止性工具说明
- MCP 工具动态合并到 ToolRegistry
- SkillRegistry 支持 load_from_dir 文件加载
- Agent 权限模型（allow/ask/deny）接入 engine 工具执行
- 主包测试覆盖 33 个测试用例（tests/test_core.py）

**测试**:
- 33 个测试用例全部通过，覆盖工具注册/skill 系统/Agent 管理/记忆系统/skill_create/mcp_config/remember
