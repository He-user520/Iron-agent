# Iron 用户指南

> 面向嵌入式开发者的 AI 编码助手使用手册

## 目录

1. [安装与环境配置](#1-安装与环境配置)
2. [第一次运行](#2-第一次运行)
3. [斜杠命令速查表](#3-斜杠命令速查表)
4. [工具列表](#4-工具列表)
5. [特性门控配置](#5-特性门控配置)
6. [插件开发指南](#6-插件开发指南)
7. [常见问题](#7-常见问题)

---

## 1. 安装与环境配置

### 系统要求

- Python 3.11+（推荐 3.12）
- Windows 10+ / Linux / macOS（Windows 优先）
- 网络：访问 LLM API（OpenAI / Anthropic / 自建 Ollama）

### 安装步骤

```bash
git clone <repo-url> iron
cd iron
pip install -e .
```

安装后 `iron` 命令可用：

```bash
iron --version
iron --help
```

### 可选依赖

```bash
# tree-sitter 代码索引（启用 code_indexer 特性时需要）
pip install tree-sitter tree-sitter-languages

# EmbedForge 桥接（嵌入式领域专用，可选）
# 详见 iron/integrations/embedforge_bridge.py
```

### LLM 后端选择

Iron 支持 4 个 LLM 后端，通过 `/config` 或 `/model` 命令切换：

| 后端 | 适用场景 | API Key 环境变量 |
|------|---------|------------------|
| OpenAI 兼容 | OpenAI / DeepSeek / Together / 国产平台 | `OPENAI_API_KEY` 或 `IRON_API_KEY_<NAME>` |
| Anthropic | Claude 系列 | `ANTHROPIC_API_KEY` |
| Ollama | 本地模型（qwen2.5-coder / llama3 等） | 无需 |
| Echo | 测试（返回模板代码） | 无需 |

---

## 2. 第一次运行

### 初始化配置

```bash
iron init
```

生成 `~/.iron/config.yml`（默认配置）和 `~/.iron/features.yml`（特性门控）。

### 设置 API Key

推荐用环境变量（不落盘）：

```bash
# Windows PowerShell
$env:OPENAI_API_KEY = "sk-..."

# Linux/macOS
export OPENAI_API_KEY=sk-...
```

也可通过 `/config` 命令交互式配置（含多厂商支持）。

### 添加多厂商配置

```
> /config
选择「添加厂商」
输入厂商名称：mimo
输入 URL：https://token-plan-cn.xiaomimimo.com/v1
选择 API Key 保存策略：不落盘，用环境变量
```

切换厂商：

```
> /model
用上下箭头选择厂商
用上下箭头选择模型
```

### 启动会话

```bash
# 在 STM32 项目目录启动（自动检测 platformio.ini）
cd your-stm32-project
iron

# 或指定 MCU
iron --mcu stm32f407

# 指定项目目录
iron --project /path/to/project
```

启动后显示：

```
Iron v3.0.0
MCU: STM32F407
Model: openai/mimo-v2.5-pro
Rules: 18 loaded
Build tool: platformio
API Key: sk-...xxxx (前 4 + 后 4 字符确认)

> _
```

### 第一次对话

直接用自然语言描述需求：

```
> /code 给 main.c 添加一个 LED 闪烁任务，频率 2Hz
```

或用斜杠命令：

```
> /read main.c            # 读取文件
> /check                  # 静态分析
> /build                  # 编译
> /flash                  # 烧录
> /monitor                # 串口监视器
```

---

## 3. 斜杠命令速查表

输入 `/` 显示常用命令（最多 6 个），继续输入字符可筛选。

### 编码与文件

| 命令 | 说明 |
|------|------|
| `/code <需求>` | 描述需求，开始编码（默认进入 Coder Agent） |
| `/read <file>` | 读取文件内容（不污染对话历史） |
| `/write <file>` | 写入文件 |
| `/edit <file>` | 编辑文件（替换内容） |
| `/delete <file>` | 删除文件 |
| `/files` | 浏览项目文件树 |
| `/undo` | 撤销上次修改 |

### 编译与烧录

| 命令 | 说明 |
|------|------|
| `/check` | 运行 EmbedGuard 静态分析 |
| `/build` | 编译项目（platformio / make / keil） |
| `/flash` | 烧录固件 |
| `/monitor` | 串口监视器 |

### Agent 与会话

| 命令 | 说明 |
|------|------|
| `/agent` | 切换 / 列出 Agent（Coder / Task / Verify / Explore） |
| `/explore <query>` | 只读探索代码库（Task Agent） |
| `/verify` | 验证代码质量（静态分析 + LSP + 编译） |
| `/compact` | 压缩上下文 |
| `/context` | 查看上下文使用情况 |
| `/history` | 查看历史记录 |
| `/resume` | 恢复历史会话 |

### 配置与系统

| 命令 | 说明 |
|------|------|
| `/model` | 切换 AI 模型（两阶段：选厂商 → 选模型） |
| `/config` | 配置管理（厂商 / API Key / 超时等） |
| `/features` | 特性门控开关 |
| `/theme` | 切换主题（default / catppuccin / dracula） |
| `/rules` | 查看 / 管理编码规则 |
| `/skill` | 技能中心 |
| `/plugin` | 插件管理（list / search / install / remove / info） |
| `/git` | Git 操作（status / diff / log / add / commit） |
| `/metrics` | 查看会话指标（counter / gauge / timing） |
| `/clear` | 清屏 |
| `/help` | 显示帮助 |
| `/quit` | 退出（Ctrl+D 也可） |

### 命令补全

- 输入 `/` 显示 6 个最常用命令
- 继续输入字符筛选匹配（如 `/bu` 匹配 `/build`）
- 上下箭头浏览命令历史（仅在 `/` 模式下拦截）
- Enter 自动补全到高亮选项

---

## 4. 工具列表

Iron 内置 28+ 工具，AI 在对话中按需调用。工具按类别分组：

### 文件操作

| 工具 | 说明 |
|------|------|
| `read_file` | 读取文件内容（支持截断大文件） |
| `write_file` | 写入文件（含路径越界检查） |
| `edit_file` | 编辑文件（含 diff 预览，v4.0） |
| `multi_edit` | 多文件原子编辑（v4.0，要么全成功要么全回滚） |
| `patch_tool` | patch 格式编辑 |
| `find_files` | 文件查找（glob 模式） |
| `delete_file` | 删除文件 |

### 代码搜索

| 工具 | 说明 |
|------|------|
| `search_code` | 正则搜索代码 |
| `semantic_search` | 语义搜索（需要 code_indexer 启用） |
| `tool_search` | 工具搜索模式（提示词超阈值时启用） |

### 编译与嵌入式

| 工具 | 说明 |
|------|------|
| `embed_build` | 编译项目 |
| `embed_flash` | 烧录固件 |
| `embed_lint` | EmbedGuard 静态分析 |

### Git（v4.0）

| 工具 | 说明 |
|------|------|
| `git_status` | 工作区状态 |
| `git_diff` | 查看 diff |
| `git_log` | 查看提交历史 |
| `git_add` | 暂存文件 |
| `git_commit` | 提交 |

### LSP（可选，默认关闭）

| 工具 | 说明 |
|------|------|
| `lsp_diagnostics` | LSP 诊断（需要 clangd） |
| `lsp_hover` | 悬停信息 |
| `lsp_definition` | 跳转定义 |

### 其他

| 工具 | 说明 |
|------|------|
| `run_command` | 执行 shell 命令（含权限检查） |
| `web_search` | 网络搜索 |
| `ask_user` | 询问用户 |
| `remember` | 记忆持久化 |
| `task_track` | 任务跟踪 |
| `skill_create` | 创建技能 |
| `chat` | 终止性工具：向用户回复并结束当前轮 |
| `task` | 子 Agent 编排（v4.0，并行执行） |

### MCP 工具

通过 MCP（Model Context Protocol）动态注册的外部工具，由 `~/.iron/mcp.yml` 配置。

---

## 5. 特性门控配置

`~/.iron/features.yml` 控制运行时特性开关。可通过 `/features` 命令切换，或直接编辑文件。

### 默认启用的特性

```yaml
# L2 内核
prompt_caching: true           # 系统提示分块缓存（P1-3）
stop_hooks: true               # 收敛检测器（P1-2）
progressive_compaction: true   # 上下文渐进压缩（P3-2，5 层管道）
doom_loop_detection: true      # doom_loop 循环检测（P1-5）

# L3 工具
tool_search: true              # 工具搜索模式（P4-1）
patch_tool: true               # patch 工具（P4-2）
tool_truncation: true          # 工具输出截断（P4-3）

# L4 权限
permission_rules: true         # DSL 驱动的权限规则（P2-1）
pre_post_hooks: true           # 工具执行前后 Hook（P2-2）
permission_persistence: true   # 三级审批持久化（P2-3）

# L5 服务
pubsub: true                   # 事件总线（P3-1）
sqlite_persistence: true       # SQLite 持久化（P3-3）
dream_distill: true            # 记忆整理

# L6 UI
markdown_rendering: true       # Markdown 渲染（P5-1）
theme_system: true             # 主题系统（P5-2）
command_groups: true           # 命令分组

# v4.0 通用编码能力
git_tools: true                # Git 工具集
diff_preview: true             # edit_file 前 diff 预览
multi_edit: true               # 多文件原子编辑
metrics: true                  # 观测性指标采集
```

### 默认关闭的特性

```yaml
lsp_tools: false               # LSP 工具（需要 clangd）
vim_mode: false                # Vim 模式
code_indexer: false            # tree-sitter 代码索引
plugins: false                 # 插件系统
sandbox: false                 # OS 沙箱
search_mode: false             # 搜索模式（提示词超阈值才启用）
```

### 启用 tree-sitter 代码索引

```bash
# 1. 安装依赖
pip install tree-sitter tree-sitter-languages

# 2. 编辑 ~/.iron/features.yml
code_indexer: true

# 3. 重启 iron，用 /doctor 检查
> /doctor
```

### 启用 Vim 模式

```yaml
# ~/.iron/features.yml
vim_mode: true
```

启动后按 `i` 进入插入模式，`Esc` 回到普通模式。支持 hjkl / w / b / dd / yy / p 等基本操作。

---

## 6. 插件开发指南

> 插件系统默认关闭，需先启用 `plugins: true`。

### 插件结构

```
my_plugin/
+-- __init__.py
+-- manifest.yml     # 元信息
+-- hooks.py         # 钩子实现
```

### manifest.yml 示例

```yaml
name: my-plugin
version: 1.0.0
description: 示例插件
author: your-name

# 钩子声明
hooks:
  - PreToolUse       # 工具执行前
  - PostToolUse      # 工具执行后
  - OnSessionStart   # 会话开始
```

### hooks.py 示例

```python
"""示例插件钩子实现"""
from iron.plugins.base import Plugin


class MyPlugin(Plugin):
    """插件主类，名称必须与 manifest.yml 的 name 一致"""

    def pre_tool_use(self, tool_name: str, args: dict) -> dict:
        """工具执行前钩子

        返回修改后的 args，或返回 {"_block": True, "reason": "..."} 阻止执行。
        """
        if tool_name == "run_command" and "rm -rf" in args.get("command", ""):
            return {"_block": True, "reason": "禁止 rm -rf"}
        return args

    def post_tool_use(self, tool_name: str, args: dict, result: dict) -> dict:
        """工具执行后钩子

        返回修改后的 result。
        """
        return result

    def on_session_start(self, ctx: dict) -> None:
        """会话开始时触发"""
        print(f"Hello from MyPlugin! Project: {ctx.get('project_root')}")
```

### 安装插件

```bash
# 方式 1：从本地目录安装
> /plugin install /path/to/my_plugin

# 方式 2：从 Git 仓库安装
> /plugin install https://github.com/user/iron-plugin-x

# 方式 3：从插件市场搜索
> /plugin search lint
> /plugin install iron-plugin-lint
```

### 管理插件

```
> /plugin list              # 列出已安装
> /plugin info my-plugin    # 查看详情
> /plugin remove my-plugin  # 卸载
> /plugin disable my-plugin # 临时禁用
> /plugin enable my-plugin  # 重新启用
```

### 插件 API

详见 [iron/plugins/base.py](file:///iron/plugins/base.py) 和 [iron/plugins/context.py](file:///iron/plugins/context.py)。

可用钩子点：

| 钩子 | 触发时机 | 可否阻止 |
|------|---------|---------|
| `on_session_start` | 会话开始 | 否 |
| `on_session_end` | 会话结束 | 否 |
| `pre_tool_use` | 工具执行前 | 是（返回 `_block`） |
| `post_tool_use` | 工具执行后 | 否（只能修改 result） |
| `on_chat_chunk` | LLM 流式 chunk | 否 |
| `on_error` | 错误发生 | 否 |

---

## 7. 常见问题

### Q: 启动后显示「API Key is not set」

A: API Key 未通过环境变量提供。运行：

```bash
# Windows PowerShell
$env:OPENAI_API_KEY = "sk-..."

# Linux/macOS
export OPENAI_API_KEY=sk-...
```

或用 `/config` 命令配置落盘保存（不推荐共享环境）。

### Q: LLM 请求超时

A: 默认超时 120 秒，可通过 `/config` 调整。Ollama 本地模型建议设为 300 秒。

### Q: 流式响应中断，显示「[流式不完整]」

A: Iron 已自动保留已接收内容（不重发请求，避免双倍 token 消耗）。可继续对话，AI 会基于已接收内容继续。

### Q: 中文乱码（Windows）

A: Iron 已用 UTF-8 处理所有 I/O。若仍有乱码，运行：

```powershell
chcp 65001
```

### Q: 编译失败但 Iron 没识别错误

A: 用 `/check` 运行 EmbedGuard 静态分析，或把编译输出贴给 AI：「编译报错：xxx，帮我修复」。

### Q: 如何查看会话指标？

A: 输入 `/metrics` 查看会话级 counter / gauge / timing：

```
> /metrics

  [WRENCH] 会话指标

  计数器:
    llm_calls: 12
    tool_calls|tool=edit_file,status=success: 5
    tool_calls|tool=run_command,status=success: 3

  Gauge:
    context_tokens: 5432

  耗时:
    llm_response: avg=2.341s, min=0.812s, max=5.234s
    tool_duration|tool=edit_file: avg=0.234s, min=0.123s, max=0.456s
```

### Q: 如何重置会话？

A: `/clear` 清屏（不清对话历史），`/quit` 退出后重新启动则开始新会话。`/resume` 可恢复历史会话。

### Q: 子进程泄漏？

A: 已在 v3.0 修复。若仍遇到 MCP 子进程未退出，用 `/quit` 正常退出（会触发 `disconnect_all`），避免直接关闭终端。

### Q: 如何贡献代码？

A: 私有项目，暂未接受外部贡献。内部开发流程见 [docs/plans/COORDINATOR-V4.md](file:///docs/plans/COORDINATOR-V4.md)。

---

## 反馈

遇到问题请记录：

1. 复现步骤
2. 期望行为
3. 实际行为
4. `/metrics` 输出（如相关）
5. 日志（`~/.iron/logs/` 目录）

然后联系项目维护者。
