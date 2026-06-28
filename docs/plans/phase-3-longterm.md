# Phase 3 长期开发执行级子计划

**基线版本：** v2.8.0（Phase 2 已完成：向量搜索 / MCP 健康检查 / Skills 可执行机制 / 测试补齐）
**目标版本：** v3.0.0
**规划日期：** 2026-06-28
**总负责：** 单 Agent 串行执行
**依据：** [implementation-plan-v3.md](file:///d:/嵌入式-Agent/docs/implementation-plan-v3.md) Phase 3 章节（行 654-810）

---

## 0. 总体原则

### 0.1 设计哲学

| 原则 | 含义 |
|------|------|
| **MVP 优先** | 每个任务先实现最小可用版本，确保可验证、可测试，再迭代增强 |
| **零外部依赖增量** | 优先使用 Python 标准库 + 已声明依赖；新增依赖必须加入 pyproject.toml 的 optional-dependencies |
| **测试即文档** | 每个新模块必须有对应测试文件，覆盖率不低于 80% |
| **特性门控** | 所有新功能默认 `False`（实验性），通过 `features.yml` 显式启用 |
| **降级路径** | 任何可选功能不可用时，主流程不能崩溃，必须降级到原有行为 |
| **Windows 优先** | 用户主力环境为 Windows，所有路径处理、子进程调用必须 Windows 兼容 |

### 0.2 反模式防护清单

| # | 反模式 | 检查方式 |
|---|--------|---------|
| 1 | 不要在 `engine.py process()` 中直接调用 `CodeIndexer` — 必须通过工具注册 | grep `CodeIndexer` in `engine.py` 应仅出现在 `__init__` 或 `_match_skills` |
| 2 | 不要破坏 `BaseTool` 抽象边界 — 新工具必须继承 `BaseTool` | grep `class.*Tool.*:` 应继承 `BaseTool` |
| 3 | 不要直接修改 `001/002_*.sql` — 必须新增 `003_*.sql` 迁移 | `migrations/` 目录文件名递增 |
| 4 | 不要在 `process()` 中加业务逻辑 — Vim 模式必须由 UI 层处理 | grep `vim_mode` in `engine.py` 应为空 |
| 5 | 不要在远程模式中硬编码 SSH 密码 — 必须用 SSH Agent 或密钥文件 | grep `password=` in `iron/remote/` 应仅出现在文档字符串 |
| 6 | 不要在沙箱中阻塞主循环 — 沙箱执行必须异步或子进程 | grep `subprocess.run` 应为 `asyncio.create_subprocess_exec` |
| 7 | 不要破坏 `FeatureFlags` 单例 — 新特性必须注册到 `DEFAULT_FEATURES` | grep `DEFAULT_FEATURES` 应包含新特性 |
| 8 | 插件不能直接访问 `engine` 内部状态 — 必须通过 `PluginContext` | grep `engine\.` in `iron/plugins/` 应仅出现在 context 中 |

### 0.3 验收标准

- 全量测试：`pytest tests/ -v` 全绿，新增测试 ≥ 30 个，0 回归
- 反模式 grep：所有 8 项检查通过
- 版本号：`iron/__init__.py` 和 `pyproject.toml` 更新到 `3.0.0`
- 文档：本文档所有验证清单打勾

---

## 1. 任务 3.1 · 代码索引与语义理解

**目标：** 引入 tree-sitter 代码解析，让 Agent 真正"理解"代码结构，提供符号查找、调用图、死代码检测。

### 1.1 涉及文件

| 文件 | 操作 | 说明 |
|------|------|------|
| [iron/integrations/code_indexer.py](file:///d:/嵌入式-Agent/iron/integrations/code_indexer.py) | **新增** | tree-sitter 代码索引核心 |
| [iron/tools/semantic_tools.py](file:///d:/嵌入式-Agent/iron/tools/semantic_tools.py) | **新增** | 4 个语义工具 |
| [iron/core/migrations/003_add_code_index.sql](file:///d:/嵌入式-Agent/iron/core/migrations/003_add_code_index.sql) | **新增** | symbols + callgraph 表 |
| [iron/core/db.py](file:///d:/嵌入式-Agent/iron/core/db.py) | 修改 | 新增 symbols/callgraph CRUD 方法 |
| [iron/agent/engine.py](file:///d:/嵌入式-Agent/iron/agent/engine.py) | 修改 | 注入 `CodeIndexer` 到工具 context |
| [iron/config/features.py](file:///d:/嵌入式-Agent/iron/config/features.py) | 修改 | 新增 `code_indexer` 特性 |
| [tests/test_code_indexer.py](file:///d:/嵌入式-Agent/tests/test_code_indexer.py) | **新增** | 索引器测试 |
| [tests/test_semantic_tools.py](file:///d:/嵌入式-Agent/tests/test_semantic_tools.py) | **新增** | 语义工具测试 |

### 1.2 实施步骤

#### 步骤 1.2.1 · 数据库迁移

新增 `003_add_code_index.sql`：

```sql
-- 符号表：函数/变量/类型定义
CREATE TABLE IF NOT EXISTS symbols (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,                -- 符号名（如 HAL_Delay）
    kind TEXT NOT NULL,                -- function | variable | type | macro
    file_path TEXT NOT NULL,           -- 相对项目根的路径
    line_start INTEGER NOT NULL,
    line_end INTEGER NOT NULL,
    col_start INTEGER NOT NULL,
    col_end INTEGER NOT NULL,
    project_path TEXT NOT NULL,        -- 项目根（多项目隔离）
    indexed_at TEXT NOT NULL,
    UNIQUE(name, file_path, line_start)
);
CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_project ON symbols(project_path);

-- 调用图表：函数调用关系
CREATE TABLE IF NOT EXISTS callgraph (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    caller_name TEXT NOT NULL,         -- 调用方符号名
    callee_name TEXT NOT NULL,         -- 被调用符号名
    caller_file TEXT NOT NULL,
    caller_line INTEGER NOT NULL,
    project_path TEXT NOT NULL,
    indexed_at TEXT NOT NULL,
    UNIQUE(caller_name, callee_name, caller_file, caller_line)
);
CREATE INDEX IF NOT EXISTS idx_callgraph_callee ON callgraph(callee_name);
CREATE INDEX IF NOT EXISTS idx_callgraph_caller ON callgraph(caller_name);
```

#### 步骤 1.2.2 · CodeIndexer 类

```python
class CodeIndexer:
    """tree-sitter 代码索引器

    负责解析 C 代码 AST，提取符号定义和调用关系，写入 SQLite。
    支持：增量索引（文件变更时只更新该文件）、降级模式（tree-sitter 不可用时返回空结果）。
    """

    def __init__(self, db: Database, project_root: str):
        self._db = db
        self._project_root = Path(project_root).resolve()
        self._has_ts = self._check_tree_sitter()

    def _check_tree_sitter(self) -> bool:
        """检测 tree-sitter 是否可用"""
        try:
            from tree_sitter import Language, Parser
            from tree_sitter_c import language
            return True
        except ImportError:
            return False

    def index_project(self) -> dict:
        """遍历项目所有 .c/.h 文件，全量索引

        返回：{"files_indexed": int, "symbols_found": int, "calls_found": int, "errors": list}
        """
        if not self._has_ts:
            return {"files_indexed": 0, "symbols_found": 0, "calls_found": 0,
                    "errors": ["tree-sitter 未安装"]}
        # 遍历 + 解析 + 写入 DB
        ...

    def index_file(self, file_path: str) -> dict:
        """增量索引单个文件（did_change 钩子触发）"""
        ...

    def get_symbol_definition(self, name: str) -> list[dict]:
        """查找符号定义（可能多处）"""
        ...

    def get_callers(self, callee_name: str) -> list[dict]:
        """查找调用某函数的所有位置"""
        ...

    def get_callees(self, caller_name: str) -> list[dict]:
        """查找某函数调用的所有函数"""
        ...

    def find_dead_code(self) -> list[dict]:
        """查找未被任何函数调用的函数（死代码）"""
        ...

    def semantic_search(self, query: str, limit: int = 20) -> list[dict]:
        """语义搜索：根据查询关键词匹配符号名和位置"""
        ...
```

#### 步骤 1.2.3 · 4 个语义工具

| 工具名 | 功能 | 权限 |
|--------|------|------|
| `semantic_search` | 按关键词搜索符号（"HAL_Delay"） | 只读 |
| `get_callers` | 查找函数调用者 | 只读 |
| `get_callees` | 查找函数被调用者 | 只读 |
| `find_dead_code` | 查找未被调用的函数 | 只读 |

每个工具继承 `BaseTool`，通过 `context["code_indexer"]` 获取索引器实例。

#### 步骤 1.2.4 · Engine 集成

- `AgentEngine.__init__` 接受可选 `code_indexer: CodeIndexer | None`
- 工具执行 context 中注入 `code_indexer`
- 当 `features.is_enabled("code_indexer")` 为 True 时自动索引新写入的 .c/.h 文件（在 `_handle_edit_file_tool` 后置 hook 中触发 `index_file`）

### 1.3 验证清单

- [ ] `003_add_code_index.sql` 迁移文件存在并自动执行
- [ ] `CodeIndexer` 类实现，tree-sitter 不可用时降级
- [ ] 4 个语义工具注册到 `ToolRegistry`
- [ ] `engine.py` 通过 context 注入 `code_indexer`
- [ ] `features.py` 新增 `code_indexer` 特性（默认 False）
- [ ] `tests/test_code_indexer.py` ≥ 15 个测试全绿
- [ ] `tests/test_semantic_tools.py` ≥ 12 个测试全绿
- [ ] 索引 10K 行项目时间 < 15 秒（tree-sitter 可用时）

---

## 2. 任务 3.2 · 插件系统

**目标：** 设计可扩展的插件接口，支持第三方工具/Skill/Hook 注入，提供 `/plugin` 命令管理。

### 2.1 涉及文件

| 文件 | 操作 | 说明 |
|------|------|------|
| [iron/plugins/__init__.py](file:///d:/嵌入式-Agent/iron/plugins/__init__.py) | **新增** | 包初始化 |
| [iron/plugins/base.py](file:///d:/嵌入式-Agent/iron/plugins/base.py) | **新增** | 插件接口定义 |
| [iron/plugins/manager.py](file:///d:/嵌入式-Agent/iron/plugins/manager.py) | **新增** | 插件管理器 |
| [iron/plugins/context.py](file:///d:/嵌入式-Agent/iron/plugins/context.py) | **新增** | 插件上下文（受控访问） |
| [iron/cli/commands/plugin_cmds.py](file:///d:/嵌入式-Agent/iron/cli/commands/plugin_cmds.py) | **新增** | `/plugin` 命令 |
| [iron/cli/main.py](file:///d:/嵌入式-Agent/iron/cli/main.py) | 修改 | 注册 `/plugin` 命令 |
| [iron/config/features.py](file:///d:/嵌入式-Agent/iron/config/features.py) | 修改 | 新增 `plugins` 特性 |
| [tests/test_plugins.py](file:///d:/嵌入式-Agent/tests/test_plugins.py) | **新增** | 插件系统测试 |

### 2.2 实施步骤

#### 步骤 2.2.1 · 插件接口

```python
from dataclasses import dataclass
from typing import Any

@dataclass
class PluginManifest:
    """插件清单（plugin.json）"""
    name: str
    version: str
    description: str
    author: str = ""
    homepage: str = ""
    min_iron_version: str = "2.8.0"
    permissions: list[str] = None  # ["file_read", "file_write", "run_command", "network"]

class IronPlugin:
    """插件基类 — 所有插件必须继承"""

    manifest: PluginManifest

    def on_load(self, context: "PluginContext") -> None:
        """插件加载时调用（仅一次）"""
        raise NotImplementedError

    def on_unload(self) -> None:
        """插件卸载时调用（仅一次）"""
        raise NotImplementedError

    def get_tools(self) -> list:
        """返回插件提供的工具列表（继承 BaseTool）"""
        return []

    def get_skills(self) -> list:
        """返回插件提供的 Skill 列表"""
        return []

    def get_hooks(self) -> list:
        """返回插件提供的 Hook 列表"""
        return []
```

#### 步骤 2.2.2 · PluginContext（受控访问）

```python
@dataclass
class PluginContext:
    """插件运行时上下文 — 限制插件对引擎内部的访问"""
    project_root: str
    config: Any  # IronConfig 实例（只读视图）
    feature_flags: Any  # FeatureFlags 实例
    event_bus: Any  # PubSub 实例（仅订阅，不发布）
    logger: Any  # 插件专属 logger

    # 受限方法：所有操作都经过 path_guard 校验
    def read_file(self, path: str) -> str: ...
    def write_file(self, path: str, content: str) -> bool: ...
    def run_command(self, cmd: str, timeout: int = 30) -> dict: ...
```

#### 步骤 2.2.3 · PluginManager

```python
class PluginManager:
    """插件管理器 — 加载/卸载/查询"""

    def __init__(self, plugins_dir: str, context: PluginContext):
        self._plugins_dir = Path(plugins_dir)
        self._context = context
        self._loaded: dict[str, IronPlugin] = {}

    def discover(self) -> list[PluginManifest]:
        """扫描插件目录，返回所有可用插件清单"""
        ...

    def load(self, name: str) -> bool:
        """加载指定插件（importlib + 实例化 + on_load）"""
        ...

    def unload(self, name: str) -> bool:
        """卸载插件（on_unload + 移除注册）"""
        ...

    def get_all_tools(self) -> list:
        """聚合所有已加载插件的工具"""
        ...

    def get_plugin(self, name: str) -> IronPlugin | None:
        """获取已加载的插件实例"""
        ...
```

#### 步骤 2.2.4 · `/plugin` 命令

```
/plugin list              — 列出已安装插件
/plugin search <keyword>  — 搜索本地插件目录
/plugin install <name>    — 从本地目录加载插件
/plugin enable <name>     — 启用插件
/plugin disable <name>    — 禁用插件（不卸载）
/plugin remove <name>     — 卸载插件
/plugin info <name>       — 显示插件详情
```

#### 步骤 2.2.5 · 沙箱限制

- 插件代码通过 `importlib.import_module` 加载，捕获所有异常
- `PluginContext` 的所有文件操作经过 `validate_path_in_project` 校验
- 插件 `on_load` 失败不影响主进程，记录日志后跳过
- 插件崩溃的 `BaseTool.safe_execute` 自动捕获，返回错误结果

### 2.3 验证清单

- [ ] `iron/plugins/` 目录及 4 个文件创建
- [ ] `PluginManager` 类实现，支持 load/unload/discover
- [ ] `/plugin` 命令注册到 `SLASH_COMMANDS`
- [ ] `features.py` 新增 `plugins` 特性（默认 False）
- [ ] `tests/test_plugins.py` ≥ 15 个测试全绿
- [ ] 一个示例插件清单（不实际发布，仅作为测试夹具）

---

## 3. 任务 3.3 · Vim 模式

**目标：** 实现 Vim 风格的键盘绑定，Normal/Insert/Visual 三模式切换。

### 3.1 涉及文件

| 文件 | 操作 | 说明 |
|------|------|------|
| [iron/cli/vim.py](file:///d:/嵌入式-Agent/iron/cli/vim.py) | **新增** | Vim 状态机 + 键绑定 |
| [iron/cli/ui.py](file:///d:/嵌入式-Agent/iron/cli/ui.py) | 修改 | 集成 Vim 模式到 `pt_prompt` |
| [tests/test_vim_mode.py](file:///d:/嵌入式-Agent/tests/test_vim_mode.py) | **新增** | Vim 模式测试 |

### 3.2 实施步骤

#### 步骤 3.2.1 · Vim 状态机

```python
from enum import Enum

class VimMode(Enum):
    NORMAL = "NORMAL"
    INSERT = "INSERT"
    VISUAL = "VISUAL"

class VimState:
    """Vim 模式状态机

    - Normal: h/j/k/l 移动光标，i 进入 Insert，v 进入 Visual， Esc 回 Normal
    - Insert: 直接输入字符，Esc 回 Normal
    - Visual: v 切换字符选择，V 切换行选择
    """

    def __init__(self):
        self._mode = VimMode.NORMAL
        self._count = ""  # 数字前缀（如 3w 表示移动 3 个单词）
        self._register = ""  # 寄存器（yank/paste）

    @property
    def mode(self) -> VimMode:
        return self._mode

    def handle_key(self, key: str) -> str | None:
        """处理按键，返回要执行的动作（如 'cursor_left' / 'delete_char' / None）"""
        ...

    def enter_insert(self) -> None: ...
    def enter_normal(self) -> None: ...
    def enter_visual(self) -> None: ...
```

#### 步骤 3.2.2 · prompt_toolkit 集成

在 `pt_prompt` 中检测 `features.is_enabled("vim_mode")`：
- True：使用 `EditingMode.VI` + 自定义 key_bindings（基于 VimState）
- False：保持 `EditingMode.EMACS`（默认）

底部状态栏显示当前模式（NORMAL/INSERT/VISUAL），用 prompt_toolkit 的 `FormattedTextControl` 实现。

#### 步骤 3.2.3 · 支持的按键

| 模式 | 按键 | 动作 |
|------|------|------|
| Normal | `h` `l` | 左右移动光标 |
| Normal | `0` `$` | 行首/行尾 |
| Normal | `w` `b` | 下一单词/上一单词 |
| Normal | `i` `a` | 进入 Insert（光标前/后） |
| Normal | `I` `A` | 进入 Insert（行首/行尾） |
| Normal | `o` `O` | 下方/上方新行进入 Insert |
| Normal | `dd` | 删除当前行 |
| Normal | `dw` | 删除单词 |
| Normal | `x` | 删除字符 |
| Normal | `v` | 进入 Visual |
| Normal | `Esc` | 取消计数/寄存器 |
| Insert | 任意字符 | 输入 |
| Insert | `Esc` | 回 Normal |
| Visual | `h/j/k/l` | 扩展选择 |
| Visual | `y` | 复制选择 |
| Visual | `d` | 删除选择 |
| Visual | `Esc` | 回 Normal |

### 3.3 验证清单

- [ ] `iron/cli/vim.py` 创建，`VimState` 类实现
- [ ] `ui.py` 集成 Vim 模式（feature 门控）
- [ ] `features.vim_mode == True` 时启用 Vim 绑定
- [ ] 底部状态栏显示当前模式
- [ ] `tests/test_vim_mode.py` ≥ 20 个测试全绿（覆盖状态机所有转换）

---

## 4. 任务 3.4 · 远程/SSH 模式

**目标：** 支持通过 SSH 连接远程项目目录，文件读写和命令执行转发到远程主机。

### 4.1 涉及文件

| 文件 | 操作 | 说明 |
|------|------|------|
| [iron/remote/__init__.py](file:///d:/嵌入式-Agent/iron/remote/__init__.py) | **新增** | 包初始化 |
| [iron/remote/executor.py](file:///d:/嵌入式-Agent/iron/remote/executor.py) | **新增** | 远程执行器抽象 + SSH 实现 |
| [iron/remote/ssh_client.py](file:///d:/嵌入式-Agent/iron/remote/ssh_client.py) | **新增** | SSH 客户端封装（subprocess + ssh） |
| [iron/cli/main.py](file:///d:/嵌入式-Agent/iron/cli/main.py) | 修改 | 新增 `--remote` 参数 |
| [iron/agent/engine.py](file:///d:/嵌入式-Agent/iron/agent/engine.py) | 修改 | 工具执行时检测远程模式 |
| [tests/test_remote.py](file:///d:/嵌入式-Agent/tests/test_remote.py) | **新增** | 远程模式测试（mock） |

### 4.2 设计决策

**不引入 paramiko/asyncssh 等新依赖**，理由：
1. 用户主力环境为 Windows，paramiko 在 Windows 上有兼容性问题（Cryptography 库编译）
2. SSH 客户端在大多数系统已预装（Windows 10+ OpenSSH、Linux、macOS）
3. 使用 `subprocess` 调用 `ssh`/`scp` 命令，零依赖且稳定

### 4.3 实施步骤

#### 步骤 4.3.1 · RemoteExecutor 抽象

```python
from abc import ABC, abstractmethod

class RemoteExecutor(ABC):
    """远程执行器抽象 — 本地和远程实现都遵守此接口"""

    @abstractmethod
    async def read_file(self, path: str) -> str:
        """读取远程文件内容"""
        ...

    @abstractmethod
    async def write_file(self, path: str, content: str) -> bool:
        """写入远程文件"""
        ...

    @abstractmethod
    async def run_command(self, cmd: str, timeout: int = 30) -> dict:
        """执行远程命令，返回 {"returncode": int, "stdout": str, "stderr": str}"""
        ...

    @abstractmethod
    async def list_dir(self, path: str) -> list[str]:
        """列出目录内容"""
        ...

    @abstractmethod
    async def file_exists(self, path: str) -> bool:
        """检查文件是否存在"""
        ...

    @abstractmethod
    async def close(self) -> None:
        """关闭连接"""
        ...

class LocalExecutor(RemoteExecutor):
    """本地执行器 — 直接调用文件系统和 subprocess"""
    ...

class SSHExecutor(RemoteExecutor):
    """SSH 远程执行器 — 通过 ssh/scp 命令转发

    依赖：系统已安装 ssh 客户端
    认证：SSH Agent / 密钥文件 / 密码提示（由 ssh 处理）
    """

    def __init__(self, host: str, user: str, port: int = 22,
                 key_file: str = None, project_path: str = ""):
        self._host = host
        self._user = user
        self._port = port
        self._key_file = key_file
        self._project_path = project_path

    async def read_file(self, path: str) -> str:
        # ssh user@host "cat path"
        ...

    async def write_file(self, path: str, content: str) -> bool:
        # 通过 stdin 传输：echo content | ssh user@host "cat > path"
        ...
```

#### 步骤 4.3.2 · CLI 参数

```bash
iron --remote user@host:/path/to/project
iron --remote user@host:22:/path/to/project
iron --remote host:/path/to/project  # 默认当前用户
```

解析格式：`[user@]host[:port]:/path`

#### 步骤 4.3.3 · Engine 集成

- `AgentEngine.__init__` 接受可选 `executor: RemoteExecutor`
- 所有工具的文件/命令操作通过 `executor` 而非直接 `Path` / `subprocess`
- 默认 `LocalExecutor`，行为与现有完全一致

### 4.4 验证清单

- [ ] `iron/remote/` 目录及 3 个文件创建
- [ ] `RemoteExecutor` 抽象类 + `LocalExecutor` + `SSHExecutor` 实现
- [ ] `--remote` 参数解析（支持 user@host:port:path 格式）
- [ ] `engine.py` 通过 `executor` 抽象执行文件操作
- [ ] `tests/test_remote.py` ≥ 15 个测试全绿（mock SSH 调用）
- [ ] 无新依赖引入（pyproject.toml 不变）

---

## 5. 任务 3.5 · OS 沙箱

**目标：** 在 OS 级别隔离工具执行，限制文件访问范围和系统调用。

### 5.1 涉及文件

| 文件 | 操作 | 说明 |
|------|------|------|
| [iron/security/__init__.py](file:///d:/嵌入式-Agent/iron/security/__init__.py) | **新增** | 包初始化 |
| [iron/security/sandbox.py](file:///d:/嵌入式-Agent/iron/security/sandbox.py) | **新增** | 沙箱抽象 + 平台实现 |
| [iron/config/features.py](file:///d:/嵌入式-Agent/iron/config/features.py) | 修改 | 新增 `sandbox` 特性 |
| [tests/test_sandbox.py](file:///d:/嵌入式-Agent/tests/test_sandbox.py) | **新增** | 沙箱测试 |

### 5.2 实施步骤

#### 步骤 5.2.1 · 沙箱抽象

```python
from abc import ABC, abstractmethod

class Sandbox(ABC):
    """OS 沙箱抽象 — 不同平台不同实现"""

    @abstractmethod
    async def execute(self, cmd: list[str], cwd: str = None,
                      timeout: int = 30) -> dict:
        """在沙箱内执行命令

        返回：{"returncode": int, "stdout": str, "stderr": str}
        """
        ...

    @abstractmethod
    def validate_path(self, path: str) -> bool:
        """校验路径是否在沙箱允许范围内"""
        ...

class NoopSandbox(Sandbox):
    """无沙箱 — 直接执行（默认）"""
    async def execute(self, cmd, cwd=None, timeout=30):
        # 直接 asyncio.create_subprocess_exec
        ...

    def validate_path(self, path):
        return True

class WindowsSandbox(Sandbox):
    """Windows 沙箱 — 使用受限令牌 + AppContainer（如果可用）

    降级策略：
    1. 检测 Windows 版本，支持 AppContainer 时使用
    2. 不支持时降级到路径校验 + 子进程超时
    """

    async def execute(self, cmd, cwd=None, timeout=30):
        # 路径校验 + 超时控制
        ...

class LinuxSandbox(Sandbox):
    """Linux 沙箱 — 使用 bwrap（bubblewrap）或 firejail

    降级策略：
    1. 优先 bwrap（Flatpak 同款，无 setuid）
    2. 退回 firejail
    3. 都不可用时降级到 NoopSandbox
    """

    def __init__(self, project_root: str):
        self._project_root = Path(project_root).resolve()
        self._backend = self._detect_backend()

    def _detect_backend(self) -> str:
        """检测可用的沙箱后端"""
        for cmd in ["bwrap", "firejail"]:
            if shutil.which(cmd):
                return cmd
        return "noop"
```

#### 步骤 5.2.2 · 工具执行包装

- `run_command` 工具的 `execute()` 检测 `features.is_enabled("sandbox")`
- True：通过 `Sandbox.execute()` 执行命令
- False：保持原有 `asyncio.create_subprocess_exec` 路径

### 5.3 验证清单

- [ ] `iron/security/sandbox.py` 创建，3 个类实现（NoopSandbox/WindowsSandbox/LinuxSandbox）
- [ ] `features.py` 新增 `sandbox` 特性（默认 False）
- [ ] Windows 下路径校验生效（项目外路径被拒绝）
- [ ] Linux 下 bwrap/firejail 检测正确
- [ ] `tests/test_sandbox.py` ≥ 12 个测试全绿

---

## 6. 执行顺序与依赖

```
任务 3.1 (代码索引) ──┐
                      ├─→ 任务 3.5 (沙箱) ─→ 最终验证
任务 3.2 (插件系统) ──┤
                      │
任务 3.3 (Vim 模式) ──┤（独立）
                      │
任务 3.4 (远程 SSH) ──┘（独立）
```

**串行执行顺序（单 Agent）：**
1. 任务 3.3（Vim 模式）— 最小且独立，快速完成
2. 任务 3.5（OS 沙箱）— 中等复杂度，独立
3. 任务 3.4（远程 SSH）— 中等复杂度，独立
4. 任务 3.1（代码索引）— 较大，需 tree-sitter 集成
5. 任务 3.2（插件系统）— 最大，最后完成
6. 最终验证 — 全量测试 + 反模式检查 + 版本号更新

---

## 7. 最终验证

### 7.1 全量回归测试

```bash
pytest tests/ -v
# 期望：≥ 928 passed（898 基线 + 30 新增），0 failed，0 regressions
```

### 7.2 反模式 grep 检查

| # | 检查项 | 命令 | 期望 |
|---|--------|------|------|
| 1 | CodeIndexer 不在 process() 中直接调用 | `grep "CodeIndexer" iron/agent/engine.py` | 仅在 `__init__` 或 `_match_skills` |
| 2 | 新工具继承 BaseTool | `grep "class.*Tool.*:" iron/tools/semantic_tools.py` | 所有类继承 BaseTool |
| 3 | 迁移文件递增 | `ls iron/core/migrations/` | 001/002/003 |
| 4 | Vim 模式不在 engine.py | `grep "vim_mode" iron/agent/engine.py` | 空 |
| 5 | 远程无硬编码密码 | `grep "password=" iron/remote/` | 仅出现在 docstring |
| 6 | 沙箱异步执行 | `grep "subprocess.run" iron/security/sandbox.py` | 空（应用 create_subprocess_exec） |
| 7 | 新特性注册 | `grep "DEFAULT_FEATURES" iron/config/features.py` | 含 code_indexer/plugins/sandbox |
| 8 | 插件不直接访问 engine | `grep "engine\." iron/plugins/` | 仅出现在 PluginContext |

### 7.3 版本号更新

- `iron/__init__.py`: `__version__ = "3.0.0"`
- `pyproject.toml`: `version = "3.0.0"`

### 7.4 文档更新

- 本文档所有验证清单打勾
- 更新 `docs/ARCHITECTURE-v2.md` 添加 Phase 3 章节（可选）
