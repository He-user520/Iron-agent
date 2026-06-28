"""Vim 模式状态机 — Normal/Insert/Visual 三模式切换

特性门控：features.is_enabled("vim_mode") 为 True 时启用
默认关闭（保持 Emacs 编辑模式）

参考 Vim 行为：
- Normal: 移动光标、删除、复制、粘贴
- Insert: 直接输入字符
- Visual: 选择文本范围

不依赖 prompt_toolkit 的 EditingMode.VI，自实现状态机以便：
1. 完整控制按键映射
2. 支持 count 前缀（如 3w）
3. 支持寄存器（yank/paste）
4. 状态可测试（不依赖 UI）
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class VimMode(Enum):
    """Vim 三种模式"""
    NORMAL = "NORMAL"
    INSERT = "INSERT"
    VISUAL = "VISUAL"


# ── 动作枚举 ───────────────────────────────────────────────────────

# 动作类型：prompt_toolkit 的对应操作
ACTIONS = {
    "cursor_left": "光标左移",
    "cursor_right": "光标右移",
    "cursor_up": "光标上移",
    "cursor_down": "光标下移",
    "cursor_line_start": "光标到行首",
    "cursor_line_end": "光标到行尾",
    "cursor_word_forward": "光标到下一单词开头",
    "cursor_word_backward": "光标到上一单词开头",
    "delete_char": "删除光标处字符",
    "delete_char_left": "删除光标左侧字符",
    "delete_line": "删除整行",
    "delete_word": "删除一个单词",
    "delete_to_line_end": "删除到行尾",
    "newline_below": "下方插入新行并进入 Insert",
    "newline_above": "上方插入新行并进入 Insert",
    "enter_insert": "进入 Insert 模式",
    "enter_insert_line_start": "进入 Insert 模式（行首）",
    "enter_insert_line_end": "进入 Insert 模式（行尾）",
    "enter_insert_after": "进入 Insert 模式（光标后）",
    "enter_visual": "进入 Visual 模式",
    "enter_normal": "进入 Normal 模式",
    "yank_selection": "复制选择到寄存器",
    "delete_selection": "删除选择并复制到寄存器",
    "paste_after": "在光标后粘贴寄存器",
    "paste_before": "在光标前粘贴寄存器",
    "undo": "撤销",
    "noop": "无操作",
}


@dataclass
class VimState:
    """Vim 模式状态机 — 可独立测试，不依赖 UI

    使用方法：
        state = VimState()
        action = state.handle_key("h")  # 返回 "cursor_left"
        action = state.handle_key("i")  # 返回 "enter_insert" + 切换到 INSERT
        action = state.handle_key("x")  # 返回 "delete_char"（Insert 模式下直接输入）
    """

    _mode: VimMode = VimMode.NORMAL
    _count: str = ""  # 数字前缀（如 "3" 表示重复 3 次）
    _register: str = ""  # 寄存器内容（yank/paste）
    _pending_operator: Optional[str] = None  # 待执行操作符（d/c/y 等待动作）
    _visual_start: Optional[int] = None  # Visual 模式选择起点

    @property
    def mode(self) -> VimMode:
        """当前模式"""
        return self._mode

    @property
    def count(self) -> int:
        """解析数字前缀，默认 1"""
        try:
            n = int(self._count) if self._count else 1
            return max(1, n)
        except ValueError:
            return 1

    @property
    def register(self) -> str:
        """寄存器内容"""
        return self._register

    @property
    def visual_start(self) -> Optional[int]:
        """Visual 模式选择起点"""
        return self._visual_start

    @property
    def pending_operator(self) -> Optional[str]:
        """待执行操作符"""
        return self._pending_operator

    def reset(self) -> None:
        """重置状态（保留模式）"""
        self._count = ""
        self._pending_operator = None
        self._visual_start = None

    def enter_insert(self) -> None:
        """进入 Insert 模式"""
        self._mode = VimMode.INSERT
        self.reset()

    def enter_normal(self) -> None:
        """进入 Normal 模式"""
        self._mode = VimMode.NORMAL
        self.reset()

    def enter_visual(self) -> None:
        """进入 Visual 模式"""
        self._mode = VimMode.VISUAL
        self._visual_start = None  # 由 UI 设置当前位置
        self._count = ""
        self._pending_operator = None

    def set_register(self, content: str) -> None:
        """设置寄存器内容"""
        self._register = content

    def handle_key(self, key: str) -> str:
        """处理按键，返回要执行的动作名

        Args:
            key: 按键字符（如 "h" "i" "Esc" "Enter"）

        Returns:
            动作名（见 ACTIONS），或 "noop" 表示无操作
        """
        if not key:
            return "noop"

        # Esc 全局回到 Normal（清空 count 和 pending）
        if key in ("Esc", "Escape", "\x1b"):
            if self._mode == VimMode.INSERT:
                self.enter_normal()
                return "enter_normal"
            if self._mode == VimMode.VISUAL:
                self.enter_normal()
                return "enter_normal"
            # Normal 模式下 Esc 仅清空 count
            self._count = ""
            self._pending_operator = None
            return "noop"

        if self._mode == VimMode.INSERT:
            return self._handle_insert_key(key)
        if self._mode == VimMode.VISUAL:
            return self._handle_visual_key(key)
        return self._handle_normal_key(key)

    # ── Normal 模式处理 ────────────────────────────────────────────

    def _handle_normal_key(self, key: str) -> str:
        """Normal 模式按键处理"""
        # 数字前缀（0 特殊：行首）
        if key.isdigit() and not (key == "0" and not self._count):
            self._count += key
            return "noop"

        # 操作符等待动作时优先处理（如 dw, cw, yw, d$, c$）
        if self._pending_operator:
            return self._handle_operator_action(key)

        # 移动命令
        if key == "h":
            return self._move("cursor_left")
        if key == "l":
            return self._move("cursor_right")
        if key == "j":
            return self._move("cursor_down")
        if key == "k":
            return self._move("cursor_up")
        if key == "0":
            self._count = ""
            return "cursor_line_start"
        if key == "$":
            return "cursor_line_end"
        if key == "w":
            return self._move("cursor_word_forward")
        if key == "b":
            return self._move("cursor_word_backward")

        # 进入 Insert 模式
        if key == "i":
            self.enter_insert()
            return "enter_insert"
        if key == "I":
            self.enter_insert()
            return "enter_insert_line_start"
        if key == "a":
            self.enter_insert()
            return "enter_insert_after"
        if key == "A":
            self.enter_insert()
            return "enter_insert_line_end"
        if key == "o":
            self.enter_insert()
            return "newline_below"
        if key == "O":
            self.enter_insert()
            return "newline_above"

        # 删除操作
        if key == "x":
            return self._action_with_count("delete_char")
        if key == "X":
            return self._action_with_count("delete_char_left")
        if key == "D":
            return "delete_to_line_end"

        # 操作符（等待动作；dd/yy/cc 重复在 _handle_operator_action 中处理）
        if key == "d":
            self._pending_operator = "d"
            return "noop"
        if key == "c":
            self._pending_operator = "c"
            return "noop"
        if key == "y":
            self._pending_operator = "y"
            return "noop"

        # 操作符 + 动作组合已在上方处理（_pending_operator 检查优先）

        # Visual 模式
        if key == "v":
            self.enter_visual()
            return "enter_visual"
        if key == "V":
            self.enter_visual()
            return "enter_visual"  # 行选择由 UI 处理

        # 粘贴
        if key == "p":
            return self._action_with_count("paste_after")
        if key == "P":
            return self._action_with_count("paste_before")

        # 撤销
        if key == "u":
            return "undo"

        # 未知按键
        logger.debug("Vim Normal 模式未知按键: %s", key)
        return "noop"

    def _handle_operator_action(self, key: str) -> str:
        """处理操作符 + 动作组合（如 dw, cw, yw, dd, yy, cc）"""
        op = self._pending_operator
        self._pending_operator = None

        # 重复操作符：dd/yy/cc 表示作用于整行
        if key == op:
            if op == "d":
                return self._action_with_count("delete_line")
            if op == "y":
                self._register = ""
                return "yank_line"
            if op == "c":
                self.enter_insert()
                return "change_line"

        if key == "w":
            if op == "d":
                return "delete_word"
            if op == "c":
                self.enter_insert()
                return "delete_word"
            if op == "y":
                self._register = ""
                return "yank_word"
        if key == "$":
            if op == "d":
                return "delete_to_line_end"
            if op == "c":
                self.enter_insert()
                return "delete_to_line_end"

        logger.debug("Vim 操作符组合未实现: %s + %s", op, key)
        return "noop"

    def _move(self, action: str) -> str:
        """移动命令应用 count"""
        # 移动 count 次由 UI 重复执行
        self._count = ""
        return action

    def _action_with_count(self, action: str) -> str:
        """带 count 的动作"""
        self._count = ""
        return action

    # ── Insert 模式处理 ────────────────────────────────────────────

    def _handle_insert_key(self, key: str) -> str:
        """Insert 模式按键处理 — 直接输入字符"""
        if key == "Esc" or key == "\x1b":
            self.enter_normal()
            return "enter_normal"
        if key == "Backspace":
            return "delete_char_left"
        if key == "Delete":
            return "delete_char"
        if key == "Enter":
            return "newline"  # UI 处理为插入换行
        if key == "Left":
            return "cursor_left"
        if key == "Right":
            return "cursor_right"
        if key == "Up":
            return "cursor_up"
        if key == "Down":
            return "cursor_down"
        if key == "Home":
            return "cursor_line_start"
        if key == "End":
            return "cursor_line_end"
        if key == "Tab":
            return "insert_tab"
        # 普通字符直接输入（UI 处理）
        if len(key) == 1:
            return "insert_char"
        return "noop"

    # ── Visual 模式处理 ────────────────────────────────────────────

    def _handle_visual_key(self, key: str) -> str:
        """Visual 模式按键处理"""
        # 移动扩展选择
        if key == "h":
            return "cursor_left"
        if key == "l":
            return "cursor_right"
        if key == "j":
            return "cursor_down"
        if key == "k":
            return "cursor_up"
        if key == "0":
            return "cursor_line_start"
        if key == "$":
            return "cursor_line_end"
        if key == "w":
            return "cursor_word_forward"
        if key == "b":
            return "cursor_word_backward"

        # 操作选择
        if key == "y":
            self.enter_normal()
            return "yank_selection"
        if key == "d":
            self.enter_normal()
            return "delete_selection"
        if key == "x":
            self.enter_normal()
            return "delete_selection"
        if key == "c":
            self.enter_insert()
            return "delete_selection"

        return "noop"


# ── 状态栏文本 ──────────────────────────────────────────────────────


def get_status_text(state: VimState) -> str:
    """获取状态栏显示文本

    返回格式："-- NORMAL --" / "-- INSERT --" / "-- VISUAL --"
    带 count 前缀时显示："-- NORMAL 3 --"
    """
    mode_text = state.mode.value
    if state._count:
        return f"-- {mode_text} {state._count} --"
    if state._pending_operator:
        return f"-- {mode_text} ({state._pending_operator}) --"
    return f"-- {mode_text} --"


def get_mode_color(mode: VimMode) -> str:
    """获取模式对应的颜色（用于状态栏）"""
    if mode == VimMode.NORMAL:
        return "fg:#88ccff bold"  # 蓝色
    if mode == VimMode.INSERT:
        return "fg:#88ff88 bold"  # 绿色
    if mode == VimMode.VISUAL:
        return "fg:#ffaa88 bold"  # 橙色
    return "fg:#ffffff"
