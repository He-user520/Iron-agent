# Track 7: MultiEdit 多文件原子编辑工具

> **执行者**：Task A  
> **优先级**：P1  
> **依赖**：⚠️ 弱依赖主会话 Track 6（复用 `_render_diff`）  
> **目标**：提供多文件原子编辑工具，重构场景必备

---

## 1. 背景与价值

- Claude Code：可一次编辑多个文件，原子提交
- **Iron v3.0**：edit_file 单文件，多文件需多次调用

### 本 Track 交付
1 个 `multi_edit` 工具：

```python
# 工具调用示例
{
    "edits": [
        {"path": "src/a.c", "old_string": "foo", "new_string": "bar"},
        {"path": "src/b.c", "old_string": "baz", "new_string": "qux"},
    ]
}
```

**原子性**：要么全部成功，要么全部回滚（备份原内容，失败时恢复）。

---

## 2. 设计原则

1. **原子性**：所有编辑成功才提交，任一失败回滚所有已编辑文件
2. **复用 diff 预览**：每个文件的编辑都调用 Track 6 的 `_render_diff`
3. **权限回调**：与 edit_file 同级，`requires_permission=True`
4. **限制数量**：单次最多 10 个文件（防止误操作）
5. **特性门控**：注册 `multi_edit` 特性（默认 True）

---

## 3. 实施步骤

### Step 1: 创建 multi_edit.py 工具

**文件**：`iron/tools/multi_edit.py`（新建）

```python
"""MultiEdit — 多文件原子编辑工具"""
import logging
import shutil
from pathlib import Path

from iron.tools.base import BaseTool

logger = logging.getLogger(__name__)

MAX_FILES = 10  # 单次最多编辑 10 个文件


class MultiEditTool(BaseTool):
    """multi_edit — 多文件原子编辑"""

    @property
    def name(self) -> str:
        return "multi_edit"

    @property
    def description(self) -> str:
        return "原子编辑多个文件（要么全成功，要么全回滚）"

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "edits": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "old_string": {"type": "string"},
                            "new_string": {"type": "string"},
                        },
                        "required": ["path", "old_string", "new_string"],
                    },
                    "description": "编辑列表（最多 10 个）",
                },
            },
            "required": ["edits"],
        }

    @property
    def requires_permission(self) -> bool:
        return True

    def execute(self, args: dict, context: dict) -> dict:
        edits = args.get("edits", [])
        if not edits:
            return {"success": False, "error": "edits 不能为空",
                    "output": ""}
        if len(edits) > MAX_FILES:
            return {"success": False,
                    "error": f"单次最多编辑 {MAX_FILES} 个文件",
                    "output": ""}

        project_root = context.get("project_root", ".")
        console = context.get("console")

        # 阶段 1：预检查 + 备份 + diff 预览
        backups = []  # [(path, original_content)]
        for edit in edits:
            path = edit["path"]
            old_string = edit["old_string"]
            new_string = edit["new_string"]

            full_path = Path(project_root) / path
            if not full_path.exists():
                # 回滚已备份的文件
                self._rollback(backups)
                return {"success": False,
                        "error": f"文件不存在: {path}",
                        "output": ""}

            try:
                original = full_path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                original = full_path.read_text(encoding="gbk",
                                                errors="replace")

            if old_string not in original:
                self._rollback(backups)
                return {"success": False,
                        "error": f"{path}: 未找到匹配内容",
                        "output": ""}

            # diff 预览
            if console is not None:
                try:
                    from iron.config.features import is_feature_enabled
                    if is_feature_enabled("diff_preview"):
                        from iron.cli.ui import _render_diff
                        new_content = original.replace(old_string, new_string)
                        _render_diff(console, original, new_content,
                                    file_path=path)
                except (ImportError, RuntimeError) as e:
                    logger.debug(f"diff 预览失败: {e}")

            backups.append((full_path, original))

        # 阶段 2：原子执行（所有预检查通过后）
        results = []
        for edit, (full_path, original) in zip(edits, backups):
            try:
                new_content = original.replace(edit["old_string"],
                                                edit["new_string"])
                full_path.write_text(new_content, encoding="utf-8")
                results.append(f"✓ {edit['path']}")
            except OSError as e:
                # 写入失败，回滚所有
                self._rollback(backups)
                return {"success": False,
                        "error": f"写入 {edit['path']} 失败: {e}，已回滚",
                        "output": ""}

        return {
            "success": True,
            "output": f"已原子编辑 {len(results)} 个文件:\n" + "\n".join(results),
            "error": None,
        }

    def _rollback(self, backups: list) -> None:
        """回滚已备份的文件"""
        for full_path, original in backups:
            try:
                full_path.write_text(original, encoding="utf-8")
            except OSError as e:
                logger.error(f"回滚 {full_path} 失败: {e}")


def register_multi_edit_tool(registry) -> None:
    registry.register(MultiEditTool())
```

---

### Step 2: 注册到 engine.py

**文件**：`iron/agent/engine.py`

在 git_tools 注册之后追加：
```python
# v4.0: MultiEdit 工具
try:
    from iron.tools.multi_edit import register_multi_edit_tool
    register_multi_edit_tool(self._tool_registry)
except ImportError:
    logger.warning("multi_edit 模块加载失败")
```

`multi_edit` 不加入只读集合（需权限）。

---

### Step 3: 注册特性门控

**文件**：`iron/config/features.py`

```python
"multi_edit": True,  # v4.0: 多文件原子编辑
```

---

### Step 4: 创建测试

**文件**：`tests/test_multi_edit.py`（新建）

至少 10 个测试：
- 单文件编辑成功
- 多文件编辑成功
- 文件不存在 → 失败 + 回滚
- old_string 不匹配 → 失败 + 回滚
- 超过 MAX_FILES → 失败
- 空 edits → 失败
- 原子性：第 3 个文件写入失败 → 前 2 个回滚
- requires_permission=True
- diff 预览触发
- 特性门控注册

---

### Step 5: 全量验证

```bash
python -m pytest tests/test_multi_edit.py -v
python -m pytest tests/test_engine.py -v  # 回归
```

---

## 4. 完成标准

- [ ] MultiEditTool 实现原子编辑 + 回滚
- [ ] engine.py 注册工具
- [ ] features.py 注册特性
- [ ] 10+ 测试通过
- [ ] 回归测试 0 失败

---

## 5. 风险点

1. **回滚的幂等性**：回滚时文件可能已被外部修改，需 try/except
2. **编码问题**：GBK 文件回滚时需保持原编码（简化：统一 UTF-8 写入）
3. **并发**：多文件编辑期间用户不能手动改文件（工具内不锁，靠用户自律）
