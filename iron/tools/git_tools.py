"""Git 工具集 — 让 AI 能直接执行 Git 操作（v4.0 Track 5）

设计原则：
- 不假设项目已 git init（失败返回友好错误）
- 不引入新依赖（仅 subprocess + BaseTool）
- 安全优先（git_commit 需权限回调）
- 输出截断（由 BaseTool.safe_execute 统一处理）
- Windows 兼容（shell=False + list 参数，errors="replace" 兜底 GBK 输出）

工具清单：
- git_status  查看工作区状态
- git_diff    查看 diff（staged/unstaged/指定文件）
- git_log     查看提交历史
- git_add     暂存文件
- git_commit  提交（需权限回调）
"""
import logging
import subprocess

from iron.tools.base import BaseTool

logger = logging.getLogger(__name__)


def _run_git(cwd: str, args: list, timeout: int = 30) -> dict:
    """运行 git 命令的统一辅助函数

    Args:
        cwd: 工作目录（通常是项目根）
        args: git 子命令及参数列表（如 ["status", "--short"]）
        timeout: 超时秒数

    Returns:
        {"success": bool, "stdout": str, "stderr": str, "returncode": int}
        git 未安装/超时/异常时 success=False，stderr 含错误信息。
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
    """检测目录是否是 git 仓库（快速 rev-parse 检测）"""
    result = _run_git(cwd, ["rev-parse", "--is-inside-work-tree"], timeout=5)
    return result["success"] and result["stdout"].strip() == "true"


class GitStatusTool(BaseTool):
    """git_status — 查看工作区状态"""

    @property
    def name(self) -> str:
        return "git_status"

    @property
    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": "git_status",
                "description": "查看 Git 工作区状态（git status --short）。无参数。",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
            },
        }

    async def execute(self, args: dict, context: dict) -> dict:
        project_root = context.get("project_dir") or context.get("project_root") or "."
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
    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": "git_diff",
                "description": "查看 Git diff（默认 unstaged，staged=true 查看已暂存，path 指定文件）",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "staged": {"type": "boolean", "description": "查看已暂存的 diff（默认 false）"},
                        "path": {"type": "string", "description": "指定文件路径（可选）"},
                    },
                    "required": [],
                },
            },
        }

    async def execute(self, args: dict, context: dict) -> dict:
        project_root = context.get("project_dir") or context.get("project_root") or "."
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
    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": "git_log",
                "description": "查看 Git 提交历史（默认 10 条，--oneline 格式）",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "description": "返回的提交数（默认 10）"},
                    },
                    "required": [],
                },
            },
        }

    async def execute(self, args: dict, context: dict) -> dict:
        project_root = context.get("project_dir") or context.get("project_root") or "."
        if not _is_git_repo(project_root):
            return {"success": False, "error": "当前目录不是 git 仓库",
                    "output": ""}
        limit = args.get("limit", 10)
        try:
            limit_int = int(limit)
        except (TypeError, ValueError):
            limit_int = 10
        if limit_int < 1:
            limit_int = 1
        result = _run_git(project_root, ["log", "--oneline", "-n", str(limit_int)])
        if not result["success"]:
            # 空仓库：git log 会失败，stderr 含 "does not have any commits"
            return {"success": False, "error": result["stderr"].strip() or "无提交历史",
                    "output": ""}
        output = result["stdout"].strip() or "(无提交历史)"
        return {"success": True, "output": output, "error": None}


class GitAddTool(BaseTool):
    """git_add — 暂存文件"""

    @property
    def name(self) -> str:
        return "git_add"

    @property
    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": "git_add",
                "description": "暂存文件到 Git 索引（git add <paths>）",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "paths": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "要暂存的文件路径列表",
                        },
                    },
                    "required": ["paths"],
                },
            },
        }

    async def execute(self, args: dict, context: dict) -> dict:
        project_root = context.get("project_dir") or context.get("project_root") or "."
        if not _is_git_repo(project_root):
            return {"success": False, "error": "当前目录不是 git 仓库",
                    "output": ""}
        paths = args.get("paths", [])
        if not paths:
            return {"success": False, "error": "paths 不能为空",
                    "output": ""}
        # 防御性：确保所有路径都是字符串
        str_paths = [str(p) for p in paths]
        result = _run_git(project_root, ["add"] + str_paths)
        if not result["success"]:
            return {"success": False, "error": result["stderr"],
                    "output": ""}
        return {"success": True, "output": f"已暂存 {len(str_paths)} 个文件",
                "error": None}


class GitCommitTool(BaseTool):
    """git_commit — 提交（需权限回调）"""

    @property
    def name(self) -> str:
        return "git_commit"

    @property
    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": "git_commit",
                "description": "提交 Git 变更（git commit -m <message>）。需要用户确认。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "message": {"type": "string", "description": "提交信息"},
                    },
                    "required": ["message"],
                },
            },
        }

    @property
    def requires_permission(self) -> bool:
        """提交需要用户确认（与 edit_file 同级）"""
        return True

    async def execute(self, args: dict, context: dict) -> dict:
        project_root = context.get("project_dir") or context.get("project_root") or "."
        if not _is_git_repo(project_root):
            return {"success": False, "error": "当前目录不是 git 仓库",
                    "output": ""}
        message = args.get("message", "").strip()
        if not message:
            return {"success": False, "error": "提交信息不能为空",
                    "output": ""}
        # 用 list 参数传 message，避免 shell 注入
        result = _run_git(project_root, ["commit", "-m", message])
        if not result["success"]:
            return {"success": False, "error": result["stderr"].strip() or "提交失败",
                    "output": ""}
        return {"success": True, "output": result["stdout"].strip(),
                "error": None}


def register_git_tools(registry) -> None:
    """批量注册 Git 工具到 registry"""
    for tool_cls in [GitStatusTool, GitDiffTool, GitLogTool, GitAddTool, GitCommitTool]:
        registry.register(tool_cls())
