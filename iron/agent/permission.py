"""权限审批管理 — 三级持久化

参考 Claude Code 的权限持久化设计：
- once: 每次都问（默认）
- session: 会话级批准（内存中保存，进程重启丢失）
- never: 永不允许（持久化到 ~/.iron/permissions.yml）

用户在权限提示时可选择：
  [y] 允许本次
  [a] 允许本次会话（本次会话内不再问）
  [n] 拒绝本次
  [N] 永不（持久化到黑名单）

黑名单持久化到 ~/.iron/permissions.yml：
  denied:
    - tool: embed_flash
      args_hash: "*"
      reason: "用户选择永不"
      denied_at: "2026-06-27T..."
"""
import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

# 黑名单持久化文件路径（与 settings.DEFAULT_CONFIG_DIR 一致）
PERMISSIONS_FILE = Path.home() / ".iron" / "permissions.yml"

# 参数 hash 用的关键字段（与工具参数名对齐）
_KEY_FIELDS = ("file", "path", "command", "firmware", "action")

# 会话授权按工具名通配的工具集合 — 这些工具的参数变化频繁（如 run_command 的 command 内容），
# 用户按 a（会话允许）时的预期是"此工具不再询问"，而非"此精确命令不再询问"。
# 对这些工具，会话授权记录 (tool, "*")，命中时跳过询问。
_SESSION_WILDCARD_TOOLS = {"run_command"}


@dataclass
class DeniedEntry:
    """黑名单条目"""
    tool: str
    args_hash: str = "*"  # "*" 表示该工具所有参数都拒绝
    reason: str = ""
    denied_at: str = ""


@dataclass
class PermissionDecision:
    """权限决策结果"""
    action: str  # allow / deny / ask
    reason: str = ""
    scope: str = "once"  # once / session / never


class PermissionManager:
    """权限管理器 — 三级审批持久化

    用法:
        mgr = PermissionManager()
        decision = mgr.check("embed_flash", {"firmware": "fw.bin"})
        if decision.action == "ask":
            # 显示权限提示，用户选择
            user_choice = await prompt_user()
            mgr.record_decision("embed_flash", args, user_choice)
    """

    def __init__(self, persist_path: Path = None):
        self.persist_path = Path(persist_path) if persist_path else PERMISSIONS_FILE
        # 会话级允许：内存中保存 (tool, args_hash) 元组，进程重启丢失
        self._session_allowed: set[tuple[str, str]] = set()
        # 黑名单：持久化到文件
        self._denied: list[DeniedEntry] = []
        self._load_denied()

    @staticmethod
    def _hash_args(args: dict) -> str:
        """计算参数 hash（用于会话级匹配）

        提取关键字段做 hash，使相同工具+相同关键参数的调用能命中会话允许。
        无关键字段时返回 "*"（匹配该工具的所有参数）。
        """
        if not args:
            return "*"
        relevant = {k: args.get(k, "") for k in _KEY_FIELDS if k in args}
        if not relevant:
            return "*"
        # sorted 保证字典顺序稳定，hash 可复现
        return hashlib.md5(str(sorted(relevant.items())).encode()).hexdigest()[:8]

    def check(self, tool: str, args: dict) -> PermissionDecision:
        """检查权限，返回决策（deny > session-allow > ask）

        - 黑名单命中（never）→ deny
        - 会话级允许命中（session）→ allow
        - 否则 → ask（调用方应弹窗询问用户）
        """
        if not isinstance(args, dict):
            args = {}
        args_hash = self._hash_args(args)

        # 1. 黑名单优先（never）— 精确匹配或通配符匹配
        for entry in self._denied:
            if entry.tool != tool:
                continue
            if entry.args_hash == "*" or entry.args_hash == args_hash:
                return PermissionDecision(
                    action="deny",
                    reason=entry.reason or "已加入黑名单",
                    scope="never",
                )

        # 2. 会话级允许 — 精确匹配或通配符匹配
        if (tool, args_hash) in self._session_allowed or (tool, "*") in self._session_allowed:
            return PermissionDecision(action="allow", scope="session")

        # 3. 需要询问用户
        return PermissionDecision(action="ask")

    def record_decision(self, tool: str, args: dict, choice: str, reason: str = "") -> None:
        """记录用户决策

        Args:
            tool: 工具名
            args: 工具参数（用于计算 args_hash）
            choice: "once" / "session" / "never"
            reason: 拒绝原因（never 时写入黑名单文件）
        """
        if not isinstance(args, dict):
            args = {}
        # 通配工具（如 run_command）按工具名通配记录，避免每次命令内容不同都重新询问
        if tool in _SESSION_WILDCARD_TOOLS:
            args_hash = "*"
        else:
            args_hash = self._hash_args(args)

        if choice == "session":
            self._session_allowed.add((tool, args_hash))
        elif choice == "never":
            entry = DeniedEntry(
                tool=tool,
                args_hash=args_hash,
                reason=reason or "用户选择永不",
                denied_at=datetime.now().isoformat(),
            )
            self._denied.append(entry)
            self._save_denied()
        # "once" 不持久化，仅本次允许（由调用方控制执行）

    def revoke(self, tool: str, args_hash: str = "*") -> bool:
        """撤销黑名单条目

        Args:
            tool: 工具名
            args_hash: 参数 hash，"*" 撤销该工具所有黑名单

        Returns:
            是否撤销成功（找到并删除返回 True，未找到返回 False）
        """
        before = len(self._denied)
        if args_hash == "*":
            # 撤销该工具的所有黑名单
            self._denied = [e for e in self._denied if e.tool != tool]
        else:
            # 撤销特定 args_hash 的黑名单
            self._denied = [
                e for e in self._denied
                if not (e.tool == tool and e.args_hash == args_hash)
            ]
        removed = len(self._denied) < before
        if removed:
            self._save_denied()
        return removed

    def clear_session(self) -> None:
        """清空会话级允许（会话结束时调用）"""
        self._session_allowed.clear()

    def list_denied(self) -> list[DeniedEntry]:
        """列出所有黑名单条目（返回副本，避免外部修改内部列表）"""
        return list(self._denied)

    def _load_denied(self) -> None:
        """从文件加载黑名单"""
        if not self.persist_path.exists():
            return
        try:
            with open(self.persist_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except (OSError, yaml.YAMLError) as e:
            logger.warning("加载黑名单失败 %s: %s", self.persist_path, e)
            return

        if not isinstance(data, dict):
            return
        denied_list = data.get("denied", [])
        if not isinstance(denied_list, list):
            return
        for item in denied_list:
            if not isinstance(item, dict):
                continue
            tool = item.get("tool", "")
            if not tool:
                continue
            self._denied.append(DeniedEntry(
                tool=tool,
                args_hash=item.get("args_hash", "*") or "*",
                reason=item.get("reason", ""),
                denied_at=item.get("denied_at", ""),
            ))

    def _save_denied(self) -> None:
        """保存黑名单到文件"""
        try:
            self.persist_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "denied": [
                    {
                        "tool": e.tool,
                        "args_hash": e.args_hash,
                        "reason": e.reason,
                        "denied_at": e.denied_at,
                    }
                    for e in self._denied
                ]
            }
            with open(self.persist_path, "w", encoding="utf-8") as f:
                yaml.safe_dump(data, f, allow_unicode=True, default_flow_style=False)
        except OSError as e:
            logger.warning("保存黑名单失败 %s: %s", self.persist_path, e)
