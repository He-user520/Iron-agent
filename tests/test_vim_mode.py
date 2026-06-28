"""Vim 模式状态机测试 — 覆盖 iron/cli/vim.py

运行方式：pytest tests/test_vim_mode.py -v

测试范围：
- VimMode 枚举
- VimState 状态机所有转换
- handle_key() 按键处理
- count 前缀
- 寄存器
- 状态栏文本
"""
from iron.cli.vim import (
    VimMode, VimState, ACTIONS,
    get_status_text, get_mode_color,
)


class TestVimModeEnum:
    """VimMode 枚举测试"""

    def test_modes_exist(self):
        assert VimMode.NORMAL.value == "NORMAL"
        assert VimMode.INSERT.value == "INSERT"
        assert VimMode.VISUAL.value == "VISUAL"

    def test_mode_count(self):
        assert len(list(VimMode)) == 3


class TestVimStateInit:
    """VimState 初始化测试"""

    def test_default_mode_is_normal(self):
        state = VimState()
        assert state.mode == VimMode.NORMAL

    def test_default_count_is_empty(self):
        state = VimState()
        assert state._count == ""
        assert state.count == 1  # 默认 1

    def test_default_register_empty(self):
        state = VimState()
        assert state.register == ""

    def test_default_no_pending_operator(self):
        state = VimState()
        assert state.pending_operator is None

    def test_default_no_visual_start(self):
        state = VimState()
        assert state.visual_start is None


class TestNormalModeMovement:
    """Normal 模式移动命令"""

    def test_h_moves_left(self):
        state = VimState()
        assert state.handle_key("h") == "cursor_left"

    def test_l_moves_right(self):
        state = VimState()
        assert state.handle_key("l") == "cursor_right"

    def test_j_moves_down(self):
        state = VimState()
        assert state.handle_key("j") == "cursor_down"

    def test_k_moves_up(self):
        state = VimState()
        assert state.handle_key("k") == "cursor_up"

    def test_0_to_line_start(self):
        state = VimState()
        assert state.handle_key("0") == "cursor_line_start"

    def test_dollar_to_line_end(self):
        state = VimState()
        assert state.handle_key("$") == "cursor_line_end"

    def test_w_word_forward(self):
        state = VimState()
        assert state.handle_key("w") == "cursor_word_forward"

    def test_b_word_backward(self):
        state = VimState()
        assert state.handle_key("b") == "cursor_word_backward"


class TestNormalModeInsert:
    """Normal 模式进入 Insert"""

    def test_i_enters_insert(self):
        state = VimState()
        result = state.handle_key("i")
        assert result == "enter_insert"
        assert state.mode == VimMode.INSERT

    def test_I_enters_insert_line_start(self):
        state = VimState()
        result = state.handle_key("I")
        assert result == "enter_insert_line_start"
        assert state.mode == VimMode.INSERT

    def test_a_enters_insert_after(self):
        state = VimState()
        result = state.handle_key("a")
        assert result == "enter_insert_after"
        assert state.mode == VimMode.INSERT

    def test_A_enters_insert_line_end(self):
        state = VimState()
        result = state.handle_key("A")
        assert result == "enter_insert_line_end"
        assert state.mode == VimMode.INSERT

    def test_o_newline_below(self):
        state = VimState()
        result = state.handle_key("o")
        assert result == "newline_below"
        assert state.mode == VimMode.INSERT

    def test_O_newline_above(self):
        state = VimState()
        result = state.handle_key("O")
        assert result == "newline_above"
        assert state.mode == VimMode.INSERT


class TestNormalModeDelete:
    """Normal 模式删除命令"""

    def test_x_delete_char(self):
        state = VimState()
        assert state.handle_key("x") == "delete_char"

    def test_X_delete_char_left(self):
        state = VimState()
        assert state.handle_key("X") == "delete_char_left"

    def test_D_delete_to_line_end(self):
        state = VimState()
        assert state.handle_key("D") == "delete_to_line_end"

    def test_dd_delete_line(self):
        state = VimState()
        # 第一次按 d 进入 operator 等待
        assert state.handle_key("d") == "noop"
        assert state.pending_operator == "d"
        # 第二次按 d 执行 dd
        assert state.handle_key("d") == "delete_line"
        assert state.pending_operator is None

    def test_dw_delete_word(self):
        state = VimState()
        state.handle_key("d")
        assert state.handle_key("w") == "delete_word"


class TestNormalModeVisual:
    """Normal 模式进入 Visual"""

    def test_v_enters_visual(self):
        state = VimState()
        result = state.handle_key("v")
        assert result == "enter_visual"
        assert state.mode == VimMode.VISUAL

    def test_V_enters_visual(self):
        state = VimState()
        result = state.handle_key("V")
        assert result == "enter_visual"
        assert state.mode == VimMode.VISUAL


class TestNormalModePaste:
    """Normal 模式粘贴"""

    def test_p_paste_after(self):
        state = VimState()
        assert state.handle_key("p") == "paste_after"

    def test_P_paste_before(self):
        state = VimState()
        assert state.handle_key("P") == "paste_before"


class TestNormalModeUndo:
    """Normal 模式撤销"""

    def test_u_undo(self):
        state = VimState()
        assert state.handle_key("u") == "undo"


class TestCountPrefix:
    """count 前缀测试"""

    def test_digit_starts_count(self):
        state = VimState()
        assert state.handle_key("3") == "noop"
        assert state._count == "3"
        assert state.count == 3

    def test_count_then_move(self):
        state = VimState()
        state.handle_key("3")
        result = state.handle_key("w")
        assert result == "cursor_word_forward"
        # count 在移动后清空
        assert state._count == ""

    def test_multi_digit_count(self):
        state = VimState()
        state.handle_key("1")
        state.handle_key("2")
        assert state._count == "12"
        assert state.count == 12

    def test_zero_alone_is_line_start_not_count(self):
        state = VimState()
        # 0 在没有前导数字时是行首命令
        assert state.handle_key("0") == "cursor_line_start"
        assert state._count == ""

    def test_zero_after_digit_extends_count(self):
        state = VimState()
        state.handle_key("1")
        state.handle_key("0")
        assert state._count == "10"
        assert state.count == 10

    def test_count_resets_on_esc(self):
        state = VimState()
        state.handle_key("3")
        state.handle_key("Esc")
        assert state._count == ""


class TestInsertMode:
    """Insert 模式按键"""

    def test_esc_returns_to_normal(self):
        state = VimState()
        state.enter_insert()
        assert state.mode == VimMode.INSERT
        result = state.handle_key("Esc")
        assert result == "enter_normal"
        assert state.mode == VimMode.NORMAL

    def test_char_input(self):
        state = VimState()
        state.enter_insert()
        assert state.handle_key("a") == "insert_char"
        assert state.handle_key("1") == "insert_char"

    def test_backspace(self):
        state = VimState()
        state.enter_insert()
        assert state.handle_key("Backspace") == "delete_char_left"

    def test_delete(self):
        state = VimState()
        state.enter_insert()
        assert state.handle_key("Delete") == "delete_char"

    def test_enter(self):
        state = VimState()
        state.enter_insert()
        assert state.handle_key("Enter") == "newline"

    def test_tab(self):
        state = VimState()
        state.enter_insert()
        assert state.handle_key("Tab") == "insert_tab"

    def test_arrows_in_insert(self):
        state = VimState()
        state.enter_insert()
        assert state.handle_key("Left") == "cursor_left"
        assert state.handle_key("Right") == "cursor_right"
        assert state.handle_key("Up") == "cursor_up"
        assert state.handle_key("Down") == "cursor_down"


class TestVisualMode:
    """Visual 模式按键"""

    def test_esc_returns_to_normal(self):
        state = VimState()
        state.enter_visual()
        assert state.mode == VimMode.VISUAL
        result = state.handle_key("Esc")
        assert result == "enter_normal"
        assert state.mode == VimMode.NORMAL

    def test_movement_extends_selection(self):
        state = VimState()
        state.enter_visual()
        assert state.handle_key("h") == "cursor_left"
        assert state.handle_key("l") == "cursor_right"
        assert state.handle_key("j") == "cursor_down"
        assert state.handle_key("k") == "cursor_up"

    def test_y_yanks(self):
        state = VimState()
        state.enter_visual()
        result = state.handle_key("y")
        assert result == "yank_selection"
        assert state.mode == VimMode.NORMAL

    def test_d_deletes(self):
        state = VimState()
        state.enter_visual()
        result = state.handle_key("d")
        assert result == "delete_selection"
        assert state.mode == VimMode.NORMAL

    def test_x_deletes_in_visual(self):
        state = VimState()
        state.enter_visual()
        result = state.handle_key("x")
        assert result == "delete_selection"


class TestEscKey:
    """Esc 键全局行为"""

    def test_esc_in_normal_clears_count(self):
        state = VimState()
        state.handle_key("3")
        state.handle_key("Esc")
        assert state._count == ""

    def test_esc_in_normal_clears_pending(self):
        state = VimState()
        state.handle_key("d")
        state.handle_key("Esc")
        assert state.pending_operator is None

    def test_escape_variants_accepted(self):
        state = VimState()
        state.enter_insert()
        assert state.handle_key("Escape") == "enter_normal"
        state.enter_insert()
        assert state.handle_key("\x1b") == "enter_normal"


class TestRegisterAndYank:
    """寄存器测试"""

    def test_set_register(self):
        state = VimState()
        state.set_register("hello")
        assert state.register == "hello"

    def test_yy_yank_line(self):
        state = VimState()
        state.handle_key("y")
        assert state.pending_operator == "y"
        result = state.handle_key("y")
        assert result == "yank_line"


class TestUnknownKeys:
    """未知按键处理"""

    def test_unknown_normal_returns_noop(self):
        state = VimState()
        # ~ 是 Vim 的反转大小写，我们未实现
        assert state.handle_key("~") == "noop"

    def test_empty_key_returns_noop(self):
        state = VimState()
        assert state.handle_key("") == "noop"

    def test_multi_char_unknown_in_insert(self):
        state = VimState()
        state.enter_insert()
        # 多字符 key（如功能键）在 Insert 模式下返回 noop
        assert state.handle_key("F1") == "noop"


class TestStateReset:
    """状态重置测试"""

    def test_reset_clears_count(self):
        state = VimState()
        state.handle_key("3")
        state.reset()
        assert state._count == ""

    def test_reset_clears_pending(self):
        state = VimState()
        state.handle_key("d")
        state.reset()
        assert state.pending_operator is None

    def test_reset_keeps_mode(self):
        state = VimState()
        state.enter_insert()
        state.reset()
        assert state.mode == VimMode.INSERT


class TestEnterMethods:
    """模式切换方法"""

    def test_enter_insert_resets_state(self):
        state = VimState()
        state.handle_key("3")
        state.enter_insert()
        assert state._count == ""
        assert state.mode == VimMode.INSERT

    def test_enter_normal_resets_state(self):
        state = VimState()
        state.enter_visual()
        state._count = "5"
        state.enter_normal()
        assert state._count == ""
        assert state.mode == VimMode.NORMAL
        assert state.visual_start is None

    def test_enter_visual_resets_count(self):
        state = VimState()
        state.handle_key("3")
        state.enter_visual()
        assert state._count == ""
        assert state.mode == VimMode.VISUAL


class TestStatusText:
    """状态栏文本测试"""

    def test_normal_status(self):
        state = VimState()
        assert get_status_text(state) == "-- NORMAL --"

    def test_insert_status(self):
        state = VimState()
        state.enter_insert()
        assert get_status_text(state) == "-- INSERT --"

    def test_visual_status(self):
        state = VimState()
        state.enter_visual()
        assert get_status_text(state) == "-- VISUAL --"

    def test_status_with_count(self):
        state = VimState()
        state.handle_key("3")
        text = get_status_text(state)
        assert "3" in text
        assert "NORMAL" in text

    def test_status_with_pending_operator(self):
        state = VimState()
        state.handle_key("d")
        text = get_status_text(state)
        assert "d" in text
        assert "NORMAL" in text


class TestModeColor:
    """模式颜色测试"""

    def test_normal_color(self):
        assert "bold" in get_mode_color(VimMode.NORMAL)

    def test_insert_color(self):
        assert "bold" in get_mode_color(VimMode.INSERT)

    def test_visual_color(self):
        assert "bold" in get_mode_color(VimMode.VISUAL)


class TestActionsConstant:
    """ACTIONS 常量测试"""

    def test_actions_has_all_keys(self):
        required = [
            "cursor_left", "cursor_right", "cursor_up", "cursor_down",
            "delete_char", "delete_line", "delete_word",
            "enter_insert", "enter_visual", "enter_normal",
            "yank_selection", "delete_selection",
            "paste_after", "paste_before",
            "undo", "noop",
        ]
        for action in required:
            assert action in ACTIONS, f"缺少动作: {action}"

    def test_actions_are_descriptions(self):
        for action, desc in ACTIONS.items():
            assert isinstance(desc, str)
            assert len(desc) > 0
