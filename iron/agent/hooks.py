"""Tool Hooks — 工具执行前后介入

参考 Claude Code 的 Hook 系统设计：
- PreToolUse: 返回 deny 可阻止工具执行，modify 可修改参数
- PostToolUse: 可修改工具返回结果
- 用户在 ~/.iron/hooks/ 放 Python 脚本
- 脚本实现 Hook 接口，自动加载

用法（用户脚本示例）：
    # ~/.iron/hooks/log_flash.py
    from iron.agent.hooks import PreToolUseHook, PostToolUseHook, HookResult

    class LogFlash(PreToolUseHook):
        async def before(self, tool_name, args):
            if tool_name == "embed_flash":
                print(f"[Hook] 即将烧录: {args}")
            return HookResult(action="allow")

    class LogFlashResult(PostToolUseHook):
        async def after(self, tool_name, args, result):
            if tool_name == "embed_flash":
                print(f"[Hook] 烧录结果: {result.get('success')}")
            return result

设计要点：
- 异步与同步双兼容：run_pre_hooks / run_post_hooks 会用 inspect.iscoroutine
  自动包装同步 before/after 方法，方便用户写简单的同步 hook
- Hook 加载失败不阻塞主流程：load_hooks_from_dir 内部 try/except，
  单个脚本异常只记录 warning
- Hook 顺序执行：pre hooks 任一返回 deny 即停止；modify 则更新 args 给后续
"""
import asyncio
import importlib.util
import inspect
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class HookResult:
    """Hook 返回结果

    Attributes:
        action: allow / deny / modify
            - allow: 放行（默认）
            - deny: 阻止工具执行（仅 PreToolUse 有效）
            - modify: 修改工具参数（PreToolUse）或修改结果（PostToolUse）
        modified_args: action=modify 时的新参数（PreToolUse 专用）
        modified_result: 修改后的结果（PostToolUse 专用，PostToolUse 也可直接
            通过 after() 返回值修改结果，此字段保留供未来扩展）
        reason: 阻止原因（deny 时由引擎回传给 AI）
    """
    action: str = "allow"
    modified_args: Optional[dict] = None
    modified_result: Optional[dict] = None
    reason: str = ""


class PreToolUseHook:
    """PreToolUse Hook 基类

    子类可实现 before(tool_name, args) -> HookResult，可以是同步或异步方法。
    默认实现返回 allow。
    """

    async def before(self, tool_name: str, args: dict) -> HookResult:
        return HookResult(action="allow")


class PostToolUseHook:
    """PostToolUse Hook 基类

    子类可实现 after(tool_name, args, result) -> dict，可以是同步或异步方法。
    返回的 dict 会替换原 result；默认实现原样返回。
    """

    async def after(self, tool_name: str, args: dict, result: dict) -> dict:
        return result


class HookManager:
    """Hook 管理器 — 加载和执行用户 hooks

    用法:
        manager = HookManager()
        manager.load_hooks_from_dir("~/.iron/hooks")
        result = await manager.run_pre_hooks("write_file", {"file": "src/main.c"})
        if result.action == "deny":
            return {"success": False, "error": result.reason}
    """

    def __init__(self):
        self._pre_hooks: list[PreToolUseHook] = []
        self._post_hooks: list[PostToolUseHook] = []

    def add_pre_hook(self, hook: PreToolUseHook) -> None:
        """添加 PreToolUse hook"""
        if hook is None:
            return
        self._pre_hooks.append(hook)

    def add_post_hook(self, hook: PostToolUseHook) -> None:
        """添加 PostToolUse hook"""
        if hook is None:
            return
        self._post_hooks.append(hook)

    def load_hooks_from_dir(self, dir_path: str | Path) -> int:
        """从目录加载 hook 脚本

        扫描 *.py 文件（文件名以 _ 开头的不加载），动态导入并查找
        PreToolUseHook / PostToolUseHook 的子类实例（用户在脚本里直接实例化）。

        加载约定（参考模块示例）：
        - 文件内顶层实例化的 Hook 子类对象会被收集
        - 类定义本身不会被实例化（避免副作用），需用户自行 new
        - 单个文件加载失败仅记录 warning，不影响其他文件

        Args:
            dir_path: 目录路径（支持 ~ 展开）

        Returns:
            成功加载的 hook 数量
        """
        path = Path(dir_path).expanduser()
        if not path.exists() or not path.is_dir():
            return 0

        loaded = 0
        for py_file in sorted(path.glob("*.py")):
            # 文件名以 _ 开头的不加载（约定：_utils.py / _base.py 等辅助模块）
            if py_file.name.startswith("_"):
                continue
            try:
                hooks = self._load_hooks_from_file(py_file)
                for hook in hooks:
                    if isinstance(hook, PreToolUseHook):
                        self.add_pre_hook(hook)
                        loaded += 1
                    elif isinstance(hook, PostToolUseHook):
                        self.add_post_hook(hook)
                        loaded += 1
            except (OSError, ImportError, SyntaxError, AttributeError,
                    ValueError, TypeError) as e:
                logger.warning("加载 hook 文件失败 %s: %s",
                               py_file.name, e, exc_info=True)
                continue  # 单文件失败不影响主流程
        return loaded

    @staticmethod
    def _load_hooks_from_file(py_file: Path) -> list:
        """从单个 .py 文件加载 hook 实例

        通过 importlib 动态加载模块，然后扫描模块顶层变量，
        收集所有 PreToolUseHook / PostToolUseHook 实例。
        """
        module_name = f"_iron_hook_{py_file.stem}"
        spec = importlib.util.spec_from_file_location(module_name, py_file)
        if spec is None or spec.loader is None:
            return []
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        hooks: list = []
        # 扫描模块顶层属性，收集 Hook 实例（用户在文件内直接实例化）
        for attr_name in dir(module):
            if attr_name.startswith("_"):
                continue
            try:
                attr = getattr(module, attr_name)
            except (AttributeError, TypeError):
                continue
            # 是 Hook 实例（不是类本身）才收集
            if isinstance(attr, (PreToolUseHook, PostToolUseHook)):
                hooks.append(attr)
        return hooks

    async def run_pre_hooks(self, tool_name: str, args: dict) -> HookResult:
        """按顺序执行所有 pre hooks

        - 任一返回 deny 即停止并返回该结果
        - 任一返回 modify 则更新 args 给后续 hook
        - allow 继续下一个 hook
        - 单个 hook 异常仅记录 warning，不影响其他 hook 和主流程

        Returns:
            最后生效的 HookResult（默认 allow）
        """
        current_args = args
        for hook in self._pre_hooks:
            try:
                result = hook.before(tool_name, current_args)
                # 兼容同步 hook：如果返回的不是协程，直接用
                if inspect.iscoroutine(result):
                    result = await result
            except asyncio.CancelledError:
                raise
            except (TypeError, ValueError, AttributeError, KeyError,
                    RuntimeError, OSError) as e:
                logger.warning("PreHook %s 执行异常: %s",
                               type(hook).__name__, e, exc_info=True)
                continue  # 异常不阻塞主流程

            if result is None:
                continue  # hook 返回 None 视为 allow

            if result.action == "deny":
                return result
            if result.action == "modify" and result.modified_args is not None:
                # 更新 args 给后续 hook，但返回的 result 仍带 modify 标志
                current_args = result.modified_args
        # 返回最后的 args（可能被 modify 过）和最终 action
        # 用一个新 HookResult 表达：如果有 modify 过，modified_args 携带最新 args
        if current_args is not args:
            return HookResult(action="modify", modified_args=current_args)
        return HookResult(action="allow")

    async def run_post_hooks(self, tool_name: str, args: dict,
                              result: dict) -> dict:
        """按顺序执行所有 post hooks

        - 每个 hook 的返回值替换 result 给下一个 hook
        - 单个 hook 异常仅记录 warning，不影响其他 hook 和主流程

        Returns:
            处理后的 result（默认原样返回）
        """
        current_result = result
        for hook in self._post_hooks:
            try:
                ret = hook.after(tool_name, args, current_result)
                if inspect.iscoroutine(ret):
                    ret = await ret
            except asyncio.CancelledError:
                raise
            except (TypeError, ValueError, AttributeError, KeyError,
                    RuntimeError, OSError) as e:
                logger.warning("PostHook %s 执行异常: %s",
                               type(hook).__name__, e, exc_info=True)
                continue  # 异常不阻塞主流程

            if ret is None:
                continue  # hook 返回 None 视为不修改
            if isinstance(ret, dict):
                current_result = ret
            # 非 dict 返回值忽略（避免污染 result）
        return current_result

    def clear_hooks(self) -> None:
        """清空所有 hooks"""
        self._pre_hooks.clear()
        self._post_hooks.clear()

    def hook_count(self) -> tuple[int, int]:
        """返回 (pre_count, post_count)"""
        return (len(self._pre_hooks), len(self._post_hooks))


# ── 内置 hooks ──────────────────────────────────────────────


class SafetyCheckHook(PreToolUseHook):
    """安全检查 hook — 阻止明显危险操作

    拦截 run_command 中的 rm -rf /、rm -rf ~ 等可能导致数据丢失的命令。
    其他命令风险等级评估由 rule_engine + risk_evaluator 模块处理，
    本 hook 仅作为最后一道兜底防线。
    """

    async def before(self, tool_name: str, args: dict) -> HookResult:
        # 仅检查 run_command 工具
        if tool_name != "run_command":
            return HookResult(action="allow")
        cmd = str(args.get("command", ""))
        # 阻止 rm -rf / 或 rm -rf ~（home 目录递归删除）
        if "rm -rf /" in cmd or "rm -rf ~" in cmd:
            return HookResult(
                action="deny",
                reason=f"阻止危险命令: rm -rf（命令: {cmd[:80]}）",
            )
        return HookResult(action="allow")


class AuditLogHook(PostToolUseHook):
    """审计日志 hook — 记录所有工具调用

    维护内存中的调用日志，可用于调试和审计。
    不修改 result，仅记录。
    """

    def __init__(self):
        self.log: list[dict] = []

    async def after(self, tool_name: str, args: dict, result: dict) -> dict:
        # 兼容 result 为 None 的情况
        success = True
        if isinstance(result, dict):
            success = result.get("success", True)
        self.log.append({
            "tool": tool_name,
            "args": dict(args) if isinstance(args, dict) else args,
            "success": success,
        })
        return result
