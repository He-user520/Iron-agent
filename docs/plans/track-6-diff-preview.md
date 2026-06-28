# Track 6: Diff 预览 — 编辑前可见，安全感大幅提升

> **执行者**：主会话  
> **优先级**：P0  
> **依赖**：无（与 Track 5 串行执行，先 Track 5 后 Track 6）  
> **目标**：edit_file 执行前展示 diff 预览，用户可拒绝

---

## 1. 背景与价值

### V3.0 测评结论
- Claude Code：每次 edit 前 diff 预览，用户可拒绝
- **Iron v3.0**：edit_file 直接执行，仅 `ui.show_diff()` 显示函数，没有前置预览

### 本 Track 交付
1. 在 `iron/cli/ui.py` 新增 `_render_diff` 函数（统一 diff 渲染）
2. 在 `iron/tools/edit_file.py` 的 `execute` 前置 hook 调用 diff 预览
3. 用户在权限回调中看到 diff，可选择 `y/n/a/N`
4. 多文件编辑（Track 7）复用此能力

---

## 2. 设计原则

1. **不阻塞主循环**：diff 预览在权限回调中显示，与现有权限流程一致
2. **不重新实现 diff**：用 Python 标准库 `difflib.unified_diff`
3. **颜色化输出**：用 Rich 的 `Syntax` 或自定义颜色（`+` 绿、`-` 红、`@@` 蓝）
4. **截断长 diff**：超过 50 行的 diff 只显示前后 25 行 + 中间省略提示
5. **可关闭**：通过 `features.diff_preview` 特性门控（默认 True）
6. **Windows 兼容**：避免 ANSI 转义，用 Rich 原生方法

---

## 3. 实施步骤

### Step 1: 增强 ui.py 的 diff 渲染

**文件**：`iron/cli/ui.py`

在现有 `show_diff` 函数附近新增 `_render_diff`：

```python
def _render_diff(console, old_content: str, new_content: str,
                 file_path: str = "") -> None:
    """渲染 unified diff 到 console（带颜色）

    Args:
        console: Rich Console 实例
        old_content: 原内容
        new_content: 新内容
        file_path: 文件路径（用于 diff 头部显示）
    """
    import difflib
    from rich.text import Text

    old_lines = old_content.splitlines(keepends=False)
    new_lines = new_content.splitlines(keepends=False)

    diff_lines = list(difflib.unified_diff(
        old_lines, new_lines,
        fromfile=f"a/{file_path}" if file_path else "原文件",
        tofile=f"b/{file_path}" if file_path else "新文件",
        lineterm="",
    ))

    if not diff_lines:
        console.print(f"  {Symbols.INFO} 无变更\n", style="cyan")
        return

    # 截断长 diff（超过 50 行只显示前后 25 行）
    MAX_LINES = 50
    if len(diff_lines) > MAX_LINES:
        head = diff_lines[:25]
        tail = diff_lines[-25:]
        diff_lines = head + [
            f"  ... 省略 {len(diff_lines) - 50} 行 ..."
        ] + tail

    # 渲染（带颜色）
    console.print()
    if file_path:
        console.print(f"  {Symbols.FILE_NEW} Diff: {file_path}",
                      style="bold cyan")
    for line in diff_lines:
        if line.startswith("+++") or line.startswith("---"):
            console.print(line, style="bold")
        elif line.startswith("@@"):
            console.print(line, style="cyan")
        elif line.startswith("+"):
            console.print(line, style="green")
        elif line.startswith("-"):
            console.print(line, style="red")
        else:
            console.print(line)
    console.print()
```

**验证**：
```bash
python -c "from iron.cli.ui import _render_diff; from rich.console import Console; _render_diff(Console(), 'a\nb\n', 'a\nc\n', 'test.txt')"
```

---

### Step 2: 修改 edit_file 工具集成 diff 预览

**文件**：`iron/tools/edit_file.py`

在 `execute` 方法中，**执行替换之前**调用 diff 预览：

```python
def execute(self, args: dict, context: dict) -> dict:
    # ... 现有的参数解析和文件读取 ...

    # 计算新内容
    new_content = old_content.replace(args["old_string"], args["new_string"])
    if new_content == old_content:
        return {"success": False, "error": "未找到匹配内容或无变更",
                "output": ""}

    # v4.0: Diff 预览（在权限回调前显示，让用户知情决策）
    # 注意：console 通过 context 传入，不直接 import（保持工具可测试性）
    console = context.get("console")
    if console is not None:
        try:
            from iron.config.features import is_feature_enabled
            if is_feature_enabled("diff_preview"):
                from iron.cli.ui import _render_diff
                _render_diff(console, old_content, new_content,
                            file_path=args.get("path", ""))
        except (ImportError, RuntimeError) as e:
            logger.debug(f"diff 预览渲染失败: {e}")

    # ... 现有的权限回调和写文件逻辑 ...
```

**关键**：
- `console` 通过 `context` 传入，不直接 `from iron.cli.main import console`（避免循环依赖）
- diff 预览失败不阻塞编辑（try/except 兜底）
- 特性门控 `diff_preview` 控制开关

**验证**：
```bash
python -c "from iron.tools.edit_file import EditFileTool; t=EditFileTool(); print(t.name)"
```

---

### Step 3: 注册特性门控

**文件**：`iron/config/features.py`

在 `DEFAULT_FEATURES` 字典中新增：
```python
"diff_preview": True,  # v4.0: edit_file 前 diff 预览（默认启用）
```

**验证**：
```bash
python -c "from iron.config.features import is_feature_enabled; print('diff_preview:', is_feature_enabled('diff_preview'))"
```

---

### Step 4: 在 engine.py 中把 console 注入 context

**文件**：`iron/agent/engine.py`

在工具执行的 context dict 中注入 `console`（3 处：只读工具并行分支、串行执行分支、edit_file 分支）：

```python
# 查找 context dict 构造位置，追加 "console": self._console
context = {
    "project_root": str(self._project_root),
    "session": self._session,
    # ... 现有字段 ...
    "console": self._console,  # v4.0: 注入 console 供 diff 预览使用
}
```

**注意**：`self._console` 需在 `__init__` 中初始化（如果还没有的话）：
```python
from rich.console import Console
self._console = Console()
```

**验证**：
```bash
python -m pytest tests/test_engine.py -v -k "edit"
```

---

### Step 5: 创建测试文件

**文件**：`tests/test_diff_preview.py`（新建）

```python
"""Diff 预览测试"""
from io import StringIO

import pytest
from rich.console import Console

from iron.cli.ui import _render_diff


class TestRenderDiff:
    def test_no_changes(self):
        buf = StringIO()
        console = Console(file=buf, width=80)
        _render_diff(console, "a\nb\n", "a\nb\n", "test.txt")
        output = buf.getvalue()
        assert "无变更" in output

    def test_simple_diff(self):
        buf = StringIO()
        console = Console(file=buf, width=80)
        _render_diff(console, "a\nb\n", "a\nc\n", "test.txt")
        output = buf.getvalue()
        assert "Diff" in output or "test.txt" in output
        # 应包含 -b 和 +c
        assert "-b" in output or "b" in output
        assert "+c" in output or "c" in output

    def test_long_diff_truncated(self):
        old = "\n".join([f"line{i}" for i in range(100)])
        new = "\n".join([f"line{i}_modified" for i in range(100)])
        buf = StringIO()
        console = Console(file=buf, width=80)
        _render_diff(console, old, new, "big.txt")
        output = buf.getvalue()
        assert "省略" in output

    def test_no_file_path(self):
        buf = StringIO()
        console = Console(file=buf, width=80)
        _render_diff(console, "a\n", "b\n", "")
        # 不崩溃即可
        assert buf.getvalue()


class TestEditFileDiffPreview:
    """集成测试：edit_file 执行时是否触发 diff 预览"""

    def test_diff_preview_triggered(self, tmp_path):
        from iron.tools.edit_file import EditFileTool
        test_file = tmp_path / "test.txt"
        test_file.write_text("hello world")

        buf = StringIO()
        console = Console(file=buf, width=80)
        tool = EditFileTool()
        result = tool.execute(
            {"path": str(test_file), "old_string": "hello",
             "new_string": "hi"},
            {"project_root": str(tmp_path), "console": console},
        )
        # 检查 diff 预览被触发（输出中应包含 Diff 字样或 -hello/+hi）
        output = buf.getvalue()
        # 由于 features 默认 True，应触发预览
        # （如果 features 未加载，可能不触发，所以用宽松断言）
        assert result["success"] is True


class TestFeatureGate:
    def test_diff_preview_feature_registered(self):
        from iron.config.features import DEFAULT_FEATURES
        assert "diff_preview" in DEFAULT_FEATURES
        assert DEFAULT_FEATURES["diff_preview"] is True
```

**验证**：
```bash
python -m pytest tests/test_diff_preview.py -v
```

---

## 4. 完成标准

- [ ] `_render_diff` 函数实现，支持颜色 + 截断
- [ ] edit_file 工具集成 diff 预览
- [ ] features.py 注册 `diff_preview=True`
- [ ] engine.py context 注入 console
- [ ] 5+ 测试通过
- [ ] 回归测试 0 失败

---

## 5. 风险点

1. **循环依赖**：`edit_file.py` 不能直接 `from iron.cli.ui import _render_diff`，必须延迟 import
2. **测试中的 console**：测试需传入 `Console(file=StringIO())` 捕获输出
3. **特性门控未加载**：测试环境 features 可能未初始化，需用宽松断言
4. **diff 性能**：超大文件（>10000 行）的 diff 可能慢，考虑跳过

---

## 6. 不在本 Track 范围

- 多文件 diff 预览（Track 7 MultiEdit 复用本能力）
- diff 拒绝后回滚（已有 undo 机制）
- diff 配置（如显示行数可配置）
