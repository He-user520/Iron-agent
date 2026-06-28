# Iron — 嵌入式 AI 开发 Agent CLI

> 面向 STM32 / 嵌入式开发的 AI 编码助手，支持代码生成、静态分析、编译烧录、LSP 智能提示

## 特性

- 5 个 Agent 类型（Coder / Task / Verify / Explore / Base），按场景切换
- 28+ 工具（含 Git / MultiEdit / 语义搜索 / LSP / MCP / patch / task_track）
- 4 个 LLM 后端（OpenAI 兼容 / Anthropic / Ollama / Echo 测试）
- tree-sitter 代码索引 + 调用图（可选，降级模式可用）
- 插件系统 + Vim 模式 + 远程 SSH + OS 沙箱
- 流式响应中断恢复（三态：complete / partial / failed，避免双倍 token 消耗）
- 上下文渐进压缩（5 层管道，超阈值才触发）
- 特性门控（`~/.iron/features.yml` 运行时开关）
- 观测性指标（`/metrics` 命令查看会话级 counter / gauge / timing）

## 安装

要求 Python 3.11+，推荐 3.12。

```bash
git clone <repo-url> iron
cd iron
pip install -e .
```

可选依赖（按需安装）：

```bash
# tree-sitter 代码索引（降级模式无需安装）
pip install tree-sitter tree-sitter-languages

# Vim 模式（默认关闭，features.yml 启用）
# prompt_toolkit 已随主依赖安装

# MCP 子进程（Model Context Protocol 客户端）
# 无需额外依赖，使用标准库 subprocess
```

## 快速开始

```bash
# 首次运行：初始化配置（写入 ~/.iron/config.yml）
iron init

# 设置 API Key（环境变量，不落盘）
# Windows PowerShell
$env:OPENAI_API_KEY = "sk-..."
# Linux/macOS
export OPENAI_API_KEY=sk-...

# 指定 MCU 启动
iron --mcu stm32f407

# 或在项目目录直接启动（自动检测 platformio.ini）
cd your-stm32-project
iron
```

启动后进入交互式 REPL，输入 `/help` 查看所有斜杠命令，或直接用自然语言描述需求：

```
> /code 给 main.c 添加一个 LED 闪烁任务
> /build
> /flash
```

## 配置

### API Key 保存策略

Iron 支持两种 API Key 保存策略（`/config` 命令切换）：

- **不落盘**（默认）：通过环境变量 `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `IRON_API_KEY` 提供
- **落盘到配置文件**：写入 `~/.iron/config.yml`（不推荐共享环境使用）

多厂商配置（`/config` → 添加厂商）：

```
[openai]
url = https://api.openai.com/v1
api_key_env = OPENAI_API_KEY

[mimo]
url = https://token-plan-cn.xiaomimimo.com/v1
api_key_env = IRON_API_KEY_MIMO
```

### 特性门控

`~/.iron/features.yml` 控制运行时特性开关：

```yaml
# 默认值（部分）
prompt_caching: true         # 系统提示分块缓存
progressive_compaction: true # 上下文渐进压缩
git_tools: true              # v4.0: Git 工具集
diff_preview: true           # v4.0: edit_file 前 diff 预览
multi_edit: true             # v4.0: 多文件原子编辑
metrics: true                # v4.0: 观测性指标采集
vim_mode: false              # Vim 模式（默认关闭）
code_indexer: false          # tree-sitter 代码索引（默认关闭）
plugins: false               # 插件系统（默认关闭）
```

通过 `/features` 命令运行时切换，或直接编辑文件。

## 文档

- [用户指南](docs/USER_GUIDE.md) — 安装、配置、斜杠命令速查、工具列表、插件开发
- [架构说明](docs/ARCHITECTURE.md) — L1-L7 七层架构、模块职责、Agent 协作流程、工具调用流程
- [开发计划](docs/plans/COORDINATOR-V4.md) — V4.0 并行任务协调文档

## 项目结构

```
iron/
+-- agent/           # L2 内核：engine / context / hooks / memory / permission
|   +-- agents/      # Agent 配置（build / embed / explore / plan / verify）
+-- cli/             # L6 UI：main / ui / vim / theme / commands
|   +-- commands/    # 斜杠命令分组（file / build / session / system / git / plugin / metrics）
|   +-- themes/      # 主题（default / catppuccin / dracula）
+-- config/          # L1 入口：settings / features
+-- core/            # L5 服务：db / pubsub / migrations
+-- integrations/    # L7 长期：code_indexer / lsp_client / embedforge / embedguard
+-- llm/             # L3 工具（LLM 层）：backend / prompt_cache
+-- mcp/             # MCP 客户端（Model Context Protocol）
+-- plugins/         # 插件系统
+-- remote/          # 远程 SSH 执行器
+-- rules/           # L4 权限：iron_rules / ai_antipatterns / permission_rules
+-- security/        # OS 沙箱
+-- skills/          # 技能系统
+-- tools/           # L3 工具（工具层）：28+ 工具 + registry
+-- utils/           # 工具函数：metrics / token_counter / doc_reader
+-- __init__.py      # 版本号
+-- __main__.py      # 入口
+-- constants.py     # 常量
```

## 测试

```bash
# 全量测试
pytest tests/ -v

# 针对性测试
pytest tests/test_metrics.py -v
pytest tests/test_backend.py -v
pytest tests/test_engine.py -v
```

## 版本

当前版本：4.0.0（见 [iron/__init__.py](file:///iron/__init__.py)）

V4.0 正在开发中，新增：Git 工具集、Diff 预览、MultiEdit、子 Agent、观测性指标、tree-sitter 引导。

## 许可证

私有项目，未公开发布。
