# Track 5: Git 工具集 — 让 Iron 通用编码能力从 65% → 80%

> **执行者**：主会话  
> **优先级**：P0  
> **依赖**：无  
> **目标**：为 Iron 提供 5 个 Git 工具，让 AI 能直接在对话中执行 Git 操作，无需切终端

---

## 1. 背景与价值

### V3.0 测评结论
通用编码能力 65%，最大缺口是 **Git 集成缺失**：
- Claude Code：原生支持 `git diff` / `git commit` / `git log` / `git stash`
- OpenCode：深度 Git 工作流
- **Iron v3.0**：0 个 git 工具，用户必须手动切终端

### 本 Track 交付
5 个 Git 工具 + 1 个 `/git` 斜杠命令：

| 工具名 | 功能 | 等价 git 命令 |
|---|---|---|
| `git_status` | 查看工作区状态 | `git status --short` |
| `git_diff` | 查看 diff（staged/unstaged/指定文件） | `git diff` / `git diff --staged` |
| `git_log` | 查看提交历史（可指定条数） | `git log --oneline -n N` |
| `git_commit` | 提交（需用户确认） | `git commit -m "msg"` |
| `git_add` | 暂存文件 | `git add <files>` |

**额外**：`git_stash` 工具（stash + pop）作为加分项，时间不够可跳过。

---

## 2. 设计原则

1. **不假设项目已 git init**：所有 git 命令必须 try/except，失败返回友好错误（`{"success": False, "error": "未初始化 git 仓库"}`）
2. **不引入新依赖**：仅用 `subprocess` + 项目内 `BaseTool`
3. **安全优先**：`git_commit` 必须经过权限回调（与 edit_file 同级）
4. **输出截断**：diff/log 输出超过 `DEFAULT_MAX_OUTPUT_CHARS` 时自动截断
5. **Windows 兼容**：subprocess 用 `shell=False` + list 参数，避免 shell 注入
6. **特性门控**：注册 `git_tools` 特性到 features.py，**默认 True**（Git 是通用能力，非实验性）

---

## 3. 实施步骤

### Step 1: 创建 git_tools.py 工具文件

**文件**：`iron/tools/git_tools.py`（新建）

实现 5 个工具类，全部继承 `BaseTool`：

```python
"""Git 工具集 — 让 AI 能直接执行 Git 操作

设计原则：
- 不假设项目已 git init（失败返回友好错误）
- 不引入新依赖（仅 subprocess + BaseTool）
- 安全优先（git_commit 需权限回调）
- 输出截断（diff/log 超 DEFAULT_MAX_OUTPUT_CHARS 截断）
- Windows 兼容（shell=False + list 参数）
"""
import logging
import subprocess
from pathlib import Path
from typing import Optional

from iron.tools.base import BaseTool

logger = logging.getLogger(__name__)


def _run_git(cwd: str, args: list[str], timeout: int = 30) -> dict:
    """运行 git 命令的统一辅助函数

    Returns:
        {"success": bool, "stdout": str, "stderr": str, "returncode": int}
    """
    try:
        proc = subprocess.run(
            ["git"] + args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
        return {
            "success": proc.returncode == 0,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "returncode": proc.returncode,
        }
    except FileNotFoundError:
        return {"success": False, "stdout": "", "stderr": "git 未安装",
                "returncode": -1}
    except subprocess.TimeoutExpired:
        return {"success": False, "stdout": "", "stderr": f"git 命令超时（{timeout}s）",
                "returncode": -1}
    except (OSError, ValueError) as e:
        return {"success": False, "stdout": "", "stderr": str(e),
                "returncode": -1}


def _is_git_repo(cwd: str) -> bool:
    """检测目录是否是 git 仓库"""
    result = _run_git(cwd, ["rev-parse", "--is-inside-work-tree"], timeout=5)
    return result["success"] and result["stdout"].strip() == "true"


class GitStatusTool(BaseTool):
    """git_status — 查看工作区状态"""

    @property
    def name(self) -> str:
        return "git_status"

    @property
    def description(self) -> str:
        return "查看 Git 工作区状态（git status --short）"

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {},
            "required": [],
        }

    def execute(self, args: dict, context: dict) -> dict:
        project_root = context.get("project_root", ".")
        if not _is_git_repo(project_root):
            return {"success": False, "error": "当前目录不是 git 仓库",
                    "output": ""}
        result = _run_git(project_root, ["status", "--short"])
        if not result["success"]:
            return {"success": False, "error": result["stderr"],
                    "output": ""}
        output = result["stdout"].strip() or "(工作区干净)"
        return {"success": True, "output": output, "error": None}


class GitDiffTool(BaseTool):
    """git_diff — 查看 diff"""

    @property
    def name(self) -> str:
        return "git_diff"

    @property
    def description(self) -> str:
        return "查看 Git diff（staged/unstaged/指定文件）"

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "staged": {"type": "boolean", "description": "查看已暂存的 diff（默认 False）"},
                "path": {"type": "string", "description": "指定文件路径（可选）"},
            },
            "required": [],
        }

    def execute(self, args: dict, context: dict) -> dict:
        project_root = context.get("project_root", ".")
        if not _is_git_repo(project_root):
            return {"success": False, "error": "当前目录不是 git 仓库",
                    "output": ""}
        cmd = ["diff"]
        if args.get("staged"):
            cmd.append("--staged")
        path = args.get("path")
        if path:
            cmd.append(path)
        result = _run_git(project_root, cmd)
        if not result["success"]:
            return {"success": False, "error": result["stderr"],
                    "output": ""}
        output = result["stdout"].strip() or "(无 diff)"
        return {"success": True, "output": output, "error": None}


class GitLogTool(BaseTool):
    """git_log — 查看提交历史"""

    @property
    def name(self) -> str:
        return "git_log"

    @property
    def description(self) -> str:
        return "查看 Git 提交历史（默认 10 条）"

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "返回的提交数（默认 10）"},
            },
            "required": [],
        }

    def execute(self, args: dict, context: dict) -> dict:
        project_root = context.get("project_root", ".")
        if not _is_git_repo(project_root):
            return {"success": False, "error": "当前目录不是 git 仓库",
                    "output": ""}
        limit = args.get("limit", 10)
        result = _run_git(project_root, ["log", "--oneline", "-n", str(limit)])
        if not result["success"]:
            return {"success": False, "error": result["stderr"],
                    "output": ""}
        output = result["stdout"].strip() or "(无提交历史)"
        return {"success": True, "output": output, "error": None}


class GitAddTool(BaseTool):
    """git_add — 暂存文件"""

    @property
    def name(self) -> str:
        return "git_add"

    @property
    def description(self) -> str:
        return "暂存文件到 Git 索引（git add）"

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "要暂存的文件路径列表",
                },
            },
            "required": ["paths"],
        }

    def execute(self, args: dict, context: dict) -> dict:
        project_root = context.get("project_root", ".")
        if not _is_git_repo(project_root):
            return {"success": False, "error": "当前目录不是 git 仓库",
                    "output": ""}
        paths = args.get("paths", [])
        if not paths:
            return {"success": False, "error": "paths 不能为空",
                    "output": ""}
        result = _run_git(project_root, ["add"] + paths)
        if not result["success"]:
            return {"success": False, "error": result["stderr"],
                    "output": ""}
        return {"success": True, "output": f"已暂存 {len(paths)} 个文件",
                "error": None}


class GitCommitTool(BaseTool):
    """git_commit — 提交（需权限回调）"""

    @property
    def name(self) -> str:
        return "git_commit"

    @property
    def description(self) -> str:
        return "提交 Git 变更（git commit -m）"

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "提交信息"},
            },
            "required": ["message"],
        }

    @property
    def requires_permission(self) -> bool:
        """提交需要用户确认（与 edit_file 同级）"""
        return True

    def execute(self, args: dict, context: dict) -> dict:
        project_root = context.get("project_root", ".")
        if not _is_git_repo(project_root):
            return {"success": False, "error": "当前目录不是 git 仓库",
                    "output": ""}
        message = args.get("message", "").strip()
        if not message:
            return {"success": False, "error": "提交信息不能为空",
                    "output": ""}
        result = _run_git(project_root, ["commit", "-m", message])
        if not result["success"]:
            return {"success": False, "error": result["stderr"],
                    "output": ""}
        return {"success": True, "output": result["stdout"].strip(),
                "error": None}


def register_git_tools(registry) -> None:
    """批量注册 Git 工具"""
    for tool_cls in [GitStatusTool, GitDiffTool, GitLogTool, GitAddTool, GitCommitTool]:
        registry.register(tool_cls())
```

**验证**：`python -c "from iron.tools.git_tools import register_git_tools; print('ok')"`

---

### Step 2: 注册特性门控 + 工具到 engine.py

**文件**：`iron/config/features.py`

在 `DEFAULT_FEATURES` 字典中新增：
```python
"git_tools": True,  # v4.0: Git 工具集（默认启用，通用能力）
```

**文件**：`iron/agent/engine.py`

在 `__init__` 中工具注册区域（紧跟 semantic_tools 注册之后）追加：
```python
# v4.0: Git 工具集（默认启用，通用能力）
try:
    from iron.tools.git_tools import register_git_tools
    register_git_tools(self._tool_registry)
except ImportError:
    logger.warning("git_tools 模块加载失败，跳过")
```

将 5 个 git 工具名加入 `_READONLY_EXTERNAL_TOOLS`（git_status/git_diff/git_log 只读）和 `TaskAgentEngine.READONLY_TOOLS`。`git_add` 和 `git_commit` 不加入只读集合（需权限）。

**验证**：
```bash
python -c "from iron.config.features import is_feature_enabled; print('git_tools:', is_feature_enabled('git_tools'))"
# 应输出 git_tools: True
```

---

### Step 3: 创建测试文件

**文件**：`tests/test_git_tools.py`（新建）

至少 15 个测试：

```python
"""Git 工具集测试"""
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from iron.tools.git_tools import (
    GitStatusTool, GitDiffTool, GitLogTool, GitAddTool, GitCommitTool,
    register_git_tools, _run_git, _is_git_repo,
)


@pytest.fixture
def git_repo(tmp_path):
    """创建临时 git 仓库"""
    subprocess.run(["git", "init"], cwd=str(tmp_path), check=True,
                   capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"],
                   cwd=str(tmp_path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"],
                   cwd=str(tmp_path), check=True, capture_output=True)
    return tmp_path


@pytest.fixture
def git_repo_with_commit(git_repo):
    """创建带一个提交的 git 仓库"""
    (git_repo / "README.md").write_text("# Test\n")
    subprocess.run(["git", "add", "."], cwd=str(git_repo), check=True,
                   capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(git_repo),
                   check=True, capture_output=True)
    return git_repo


class TestRunGit:
    def test_run_git_success(self, git_repo):
        result = _run_git(str(git_repo), ["status", "--short"])
        assert result["success"] is True
        assert result["returncode"] == 0

    def test_run_git_not_a_repo(self, tmp_path):
        result = _run_git(str(tmp_path), ["status"])
        assert result["success"] is False

    def test_run_git_timeout(self, git_repo):
        # 用极短超时触发
        result = _run_git(str(git_repo), ["status"], timeout=0)
        # 超时或快速完成都可能，主要看不崩溃
        assert "success" in result

    def test_is_git_repo_true(self, git_repo):
        assert _is_git_repo(str(git_repo)) is True

    def test_is_git_repo_false(self, tmp_path):
        assert _is_git_repo(str(tmp_path)) is False


class TestGitStatusTool:
    def test_clean_repo(self, git_repo_with_commit):
        tool = GitStatusTool()
        result = tool.execute({}, {"project_root": str(git_repo_with_commit)})
        assert result["success"] is True
        assert "干净" in result["output"]

    def test_dirty_repo(self, git_repo_with_commit):
        (git_repo_with_commit / "new.txt").write_text("new")
        tool = GitStatusTool()
        result = tool.execute({}, {"project_root": str(git_repo_with_commit)})
        assert result["success"] is True
        assert "new.txt" in result["output"]

    def test_not_a_repo(self, tmp_path):
        tool = GitStatusTool()
        result = tool.execute({}, {"project_root": str(tmp_path)})
        assert result["success"] is False
        assert "git 仓库" in result["error"]


class TestGitDiffTool:
    def test_no_diff(self, git_repo_with_commit):
        tool = GitDiffTool()
        result = tool.execute({}, {"project_root": str(git_repo_with_commit)})
        assert result["success"] is True
        assert "无 diff" in result["output"]

    def test_unstaged_diff(self, git_repo_with_commit):
        (git_repo_with_commit / "README.md").write_text("# Modified\n")
        tool = GitDiffTool()
        result = tool.execute({}, {"project_root": str(git_repo_with_commit)})
        assert result["success"] is True
        assert "Modified" in result["output"] or "README" in result["output"]


class TestGitLogTool:
    def test_log_with_commit(self, git_repo_with_commit):
        tool = GitLogTool()
        result = tool.execute({}, {"project_root": str(git_repo_with_commit)})
        assert result["success"] is True
        assert "init" in result["output"]

    def test_log_limit(self, git_repo_with_commit):
        tool = GitLogTool()
        result = tool.execute({"limit": 1},
                              {"project_root": str(git_repo_with_commit)})
        assert result["success"] is True


class TestGitAddTool:
    def test_add_file(self, git_repo_with_commit):
        (git_repo_with_commit / "new.txt").write_text("new")
        tool = GitAddTool()
        result = tool.execute({"paths": ["new.txt"]},
                              {"project_root": str(git_repo_with_commit)})
        assert result["success"] is True

    def test_add_empty_paths(self, git_repo_with_commit):
        tool = GitAddTool()
        result = tool.execute({"paths": []},
                              {"project_root": str(git_repo_with_commit)})
        assert result["success"] is False


class TestGitCommitTool:
    def test_commit_requires_permission(self):
        tool = GitCommitTool()
        assert tool.requires_permission is True

    def test_commit_success(self, git_repo_with_commit):
        (git_repo_with_commit / "new.txt").write_text("new")
        # 先 add
        GitAddTool().execute({"paths": ["new.txt"]},
                             {"project_root": str(git_repo_with_commit)})
        tool = GitCommitTool()
        result = tool.execute({"message": "add new file"},
                              {"project_root": str(git_repo_with_commit)})
        assert result["success"] is True

    def test_commit_empty_message(self, git_repo_with_commit):
        tool = GitCommitTool()
        result = tool.execute({"message": ""},
                              {"project_root": str(git_repo_with_commit)})
        assert result["success"] is False


class TestRegisterGitTools:
    def test_register_all(self):
        from iron.tools.registry import ToolRegistry
        reg = ToolRegistry()
        register_git_tools(reg)
        names = [t.name for t in reg.list_all()]
        assert "git_status" in names
        assert "git_diff" in names
        assert "git_log" in names
        assert "git_add" in names
        assert "git_commit" in names
```

**验证**：`python -m pytest tests/test_git_tools.py -v`

---

### Step 4: 添加 /git 斜杠命令

**文件**：`iron/cli/commands/git_cmds.py`（新建）

```python
"""/git 命令分组

子命令：
- /git status    查看状态
- /git diff      查看 diff
- /git log       查看历史
- /git add <f>   暂存文件
- /git commit -m "msg"  提交
"""
from iron.cli.theme import Symbols
from rich.console import Console


def handle_git_commands(cmd: str, args: str, ctx: dict) -> bool:
    """处理 /git 命令"""
    console: Console = ctx.get("console") or Console()
    if cmd != "/git":
        return False
    parts = args.split(None, 1) if args else []
    subcmd = parts[0] if parts else "status"
    subarg = parts[1] if len(parts) > 1 else ""
    project_root = ctx.get("project_root", ".")

    # 委托给对应的工具
    from iron.tools.git_tools import (
        GitStatusTool, GitDiffTool, GitLogTool, GitAddTool, GitCommitTool
    )
    tool_map = {
        "status": (GitStatusTool, {}),
        "diff": (GitDiffTool, {"staged": "--staged" in subarg}),
        "log": (GitLogTool, {}),
        "add": (GitAddTool, {"paths": subarg.split()}),
        "commit": (GitCommitTool, {"message": subarg}),
    }
    if subcmd not in tool_map:
        console.print(f"\n  {Symbols.WARN} 未知子命令: {subcmd}\n", style="yellow")
        console.print("  可用: status / diff / log / add / commit\n")
        return True

    tool_cls, tool_args = tool_map[subcmd]
    tool = tool_cls()
    result = tool.execute(tool_args, {"project_root": str(project_root)})
    if result.get("success"):
        console.print(f"\n  {Symbols.CHECK} {result.get('output', '')}\n",
                      style="green")
    else:
        console.print(f"\n  {Symbols.CROSS} {result.get('error', '失败')}\n",
                      style="red")
    return True
```

**文件**：`iron/cli/main.py`

1. 在 `SLASH_COMMANDS` 新增 `"/git": {"desc": "Git 操作（status/diff/log/add/commit）", "handler": "handle_git"}`
2. 在 `NON_CHAT_COMMANDS` 新增 `"/git"`
3. 在 `_dispatch_slash_command` 新增 `elif handle_git_commands(cmd, args, cmd_ctx): pass`

**验证**：
```bash
python -c "from iron.cli.commands.git_cmds import handle_git_commands; print('ok')"
```

---

### Step 5: 在 ToolRegistry 中注册（如果需要）

**文件**：`iron/tools/registry.py`（如有需要）

确保 `ToolRegistry.register` 和 `list_all` 方法能正确处理新工具。

**验证**：`python -c "from iron.tools.git_tools import register_git_tools; from iron.tools.registry import ToolRegistry; r=ToolRegistry(); register_git_tools(r); print(len(r.list_all()))"`

---

### Step 6: 全量验证

```bash
# 1. 针对性测试
python -m pytest tests/test_git_tools.py -v

# 2. 回归测试（确保不破坏现有功能）
python -m pytest tests/test_engine.py tests/test_cli_commands.py tests/test_features.py -v

# 3. CLI 冒烟
python -m iron --version
python -m iron --help | findstr git
```

**预期结果**：
- test_git_tools.py: 15+ passed
- 回归测试: 0 failed
- CLI help 显示 `/git` 命令

---

## 4. 完成标准

- [ ] 5 个 Git 工具实现并通过测试
- [ ] features.py 注册 `git_tools=True`
- [ ] engine.py 注册 5 个工具
- [ ] /git 斜杠命令可用
- [ ] 15+ 测试通过
- [ ] 回归测试 0 失败
- [ ] CLI help 显示 /git 命令

---

## 5. 风险点

1. **subprocess 编码**：Windows 上 git 输出可能是 GBK，必须用 `errors="replace"` 兜底
2. **超时**：git log 在大仓库可能慢，默认 30s 超时
3. **权限回调**：`git_commit.requires_permission=True` 必须与 engine 的权限流程对接
4. **空仓库**：`git log` 在空仓库会失败，必须友好处理

---

## 6. 不在本 Track 范围

- `git_stash` / `git_branch` / `git_checkout` 等高级 Git 操作（留给 V4.1）
- Git hook 集成
- Git 工作流自动化（如自动 commit message 生成）
- PR/MR 集成

**这些能力放到 V4.1 或由用户后续需求驱动。**
