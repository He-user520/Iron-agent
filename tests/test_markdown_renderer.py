"""MarkdownStreamRenderer 测试 — 流式 Markdown 渲染

覆盖纯文本、代码块（C/Python/无语言）、标题、列表、表格、引用、行内代码、
混合内容、流式分块、未闭合代码块、完整文本获取、finish 刷新等场景。

运行: pytest tests/test_markdown_renderer.py -v
"""
from io import StringIO

import pytest
from rich.console import Console

from iron.cli.ui import MarkdownStreamRenderer


def _make_console() -> tuple[Console, StringIO]:
    """构造可捕获输出的 Console（无 ANSI 颜色，便于断言）"""
    buf = StringIO()
    console = Console(
        file=buf,
        force_terminal=False,
        no_color=True,
        highlight=False,
        width=120,
    )
    return console, buf


def _render(text: str) -> str:
    """便捷渲染：把 text 一次性 append 后 finish，返回输出文本"""
    console, buf = _make_console()
    renderer = MarkdownStreamRenderer(console)
    renderer.append(text)
    renderer.finish()
    return buf.getvalue()


# ── 基础渲染测试 ──────────────────────────────────────────────


class TestMarkdownStreamRenderer:
    """流式 Markdown 渲染器测试"""

    def test_plain_text(self):
        """纯文本渲染"""
        out = _render("Hello world\n")
        assert "Hello world" in out

    def test_code_block_c(self):
        """C 代码块渲染 — 闭合后用 Syntax 高亮"""
        out = _render("```c\nint main(void) { return 0; }\n```\n")
        assert "int main" in out
        assert "return 0" in out

    def test_code_block_python(self):
        """Python 代码块渲染"""
        out = _render("```python\nprint('hello')\n```\n")
        assert "print" in out
        assert "hello" in out

    def test_code_block_no_lang(self):
        """无语言代码块 — 回退到 text 语言"""
        out = _render("```\nplain code line\n```\n")
        assert "plain code line" in out

    def test_heading(self):
        """标题渲染 — # 开头立即刷新"""
        out = _render("# My Title\n")
        assert "My Title" in out

    def test_list(self):
        """列表渲染 — 多行累积后整体渲染"""
        out = _render("- item1\n- item2\n- item3\n\n")
        assert "item1" in out
        assert "item2" in out
        assert "item3" in out

    def test_table(self):
        """表格渲染 — 多行表格整体渲染"""
        out = _render("| col1 | col2 |\n| --- | --- |\n| val1 | val2 |\n\n")
        assert "col1" in out
        assert "col2" in out
        assert "val1" in out
        assert "val2" in out

    def test_blockquote(self):
        """引用渲染"""
        out = _render("> this is a quote\n\n")
        assert "this is a quote" in out

    def test_inline_code(self):
        """行内代码渲染"""
        out = _render("Use `code` here\n")
        assert "Use" in out
        assert "code" in out
        assert "here" in out

    def test_mixed_content(self):
        """混合内容 — 标题、段落、代码块、列表"""
        text = (
            "# Section\n"
            "\n"
            "Here is a paragraph.\n"
            "\n"
            "```python\n"
            "x = 1\n"
            "```\n"
            "\n"
            "- list item\n"
        )
        out = _render(text)
        assert "Section" in out
        assert "Here is a paragraph" in out
        assert "x = 1" in out
        assert "list item" in out


# ── 流式行为测试 ──────────────────────────────────────────────


class TestStreamingBehavior:
    """流式分块与状态管理测试"""

    def test_streaming_chunks(self):
        """流式分块接收 — chunk 边界跨多行/多 chunk"""
        console, buf = _make_console()
        renderer = MarkdownStreamRenderer(console)
        # 模拟 LLM 分块输出：跨 chunk 的代码块
        chunks = [
            "```c\n",
            "int x",
            " = ",
            "42;\n",
            "```\n",
        ]
        for c in chunks:
            renderer.append(c)
        renderer.finish()
        out = buf.getvalue()
        # 代码块应被完整渲染（拼接后 "int x = 42;"）
        assert "int x" in out
        assert "42" in out

    def test_unclosed_code_block(self):
        """未闭合代码块 — finish 时应刷新已累积的代码"""
        out = _render("```python\nprint('hi')\n")
        # 即使没有闭合 ```，finish 也会渲染代码块
        assert "print" in out
        assert "hi" in out

    def test_get_full_text(self):
        """获取完整文本 — _buffer 累积所有 chunk"""
        console, _ = _make_console()
        renderer = MarkdownStreamRenderer(console)
        text = "Hello\n```python\ncode\n```\nWorld"
        renderer.append(text)
        renderer.finish()
        assert renderer.get_full_text() == text

    def test_finish(self):
        """完成渲染 — 刷新剩余缓冲区（无尾随换行）"""
        console, buf = _make_console()
        renderer = MarkdownStreamRenderer(console)
        # 不带尾随换行的文本，应被 finish 刷新出来
        renderer.append("text without newline")
        renderer.finish()
        out = buf.getvalue()
        assert "text without newline" in out
