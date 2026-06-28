"""CodeIndexer 测试 — tree-sitter 代码索引

策略：
- tree-sitter 未安装时验证降级行为（返回空结果，不崩溃）
- tree-sitter 可用时用 mock AST 验证符号/调用提取逻辑
- 数据库 CRUD 已在 test_db.py 覆盖，这里只测 CodeIndexer 的委托调用
"""
import asyncio
from datetime import datetime
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

from iron.core.db import Database
from iron.integrations.code_indexer import CodeIndexer, _IGNORE_DIRS, _SOURCE_EXTENSIONS


# ── 测试夹具 ──────────────────────────────────────────────────────

@pytest.fixture
def db(tmp_path) -> Database:
    """每个测试用例独立的临时数据库"""
    db_path = tmp_path / "test_indexer.db"
    db = Database(db_path=db_path)
    db.connect()
    yield db
    db.close()


@pytest.fixture
def project_root(tmp_path) -> Path:
    """临时项目根目录"""
    return tmp_path


@pytest.fixture
def indexer(db, project_root):
    """CodeIndexer 实例（可能为降级模式）"""
    return CodeIndexer(db, str(project_root))


@pytest.fixture
def c_file(project_root):
    """创建一个简单的 C 文件用于索引测试"""
    src_dir = project_root / "src"
    src_dir.mkdir()
    c_path = src_dir / "main.c"
    c_path.write_text(
        '#include "main.h"\n'
        '#define MAX_BUF 256\n'
        'typedef int status_t;\n'
        'static int counter = 0;\n'
        '\n'
        'static void delay_ms(uint32_t ms) {\n'
        '    HAL_Delay(ms);\n'
        '}\n'
        '\n'
        'int main(void) {\n'
        '    HAL_Init();\n'
        '    delay_ms(100);\n'
        '    return 0;\n'
        '}\n',
        encoding="utf-8",
    )
    return c_path.relative_to(project_root)


# ── 1. 初始化与降级检测 ──────────────────────────────────────────

class TestCodeIndexerInit:
    """CodeIndexer 初始化和降级检测"""

    def test_init_with_default_db(self, project_root):
        """不传 db 时使用默认数据库路径"""
        indexer = CodeIndexer(None, str(project_root))
        # 默认 db=None 时 _db 为 None，available 由 tree-sitter 决定
        # 这里只验证不崩溃
        assert indexer._project_root == project_root.resolve()

    def test_available_property_reflects_tree_sitter(self, indexer):
        """available 属性应反映 tree-sitter 是否可用"""
        # 实际环境可能没有 tree-sitter，available 应为 False
        # 或者为 True（如果环境装了）
        assert isinstance(indexer.available, bool)
        assert indexer.available == indexer._has_ts

    def test_project_root_resolved_to_absolute(self, db, tmp_path):
        """项目根目录被 resolve 成绝对路径"""
        indexer = CodeIndexer(db, str(tmp_path / "subdir"))
        # 即使 subdir 不存在，resolve() 也会返回绝对路径
        assert indexer._project_root.is_absolute()

    def test_check_tree_sitter_returns_bool(self, indexer):
        """_check_tree_sitter 返回 bool"""
        result = indexer._check_tree_sitter()
        assert isinstance(result, bool)

    def test_init_parser_failure_falls_back_to_degraded(self, db, project_root):
        """tree-sitter 初始化失败时应降级"""
        with patch("iron.integrations.code_indexer.CodeIndexer._check_tree_sitter", return_value=True):
            with patch("iron.integrations.code_indexer.CodeIndexer._init_parser",
                       side_effect=RuntimeError("mock init failure")):
                indexer = CodeIndexer(db, str(project_root))
                # 初始化失败后 _has_ts 应为 False
                assert indexer.available is False


# ── 2. 降级模式行为 ──────────────────────────────────────────────

class TestDegradedMode:
    """tree-sitter 不可用时的降级行为"""

    def test_index_project_returns_empty_when_no_ts(self, db, project_root):
        """降级模式下 index_project 返回空结果"""
        indexer = CodeIndexer(db, str(project_root))
        if indexer.available:
            pytest.skip("tree-sitter 已安装，跳过降级测试")
        result = indexer.index_project()
        assert result["files_indexed"] == 0
        assert result["symbols_found"] == 0
        assert result["calls_found"] == 0
        assert "tree-sitter 未安装" in result["errors"]

    def test_index_file_returns_error_when_no_ts(self, db, project_root, c_file):
        """降级模式下 index_file 返回错误"""
        indexer = CodeIndexer(db, str(project_root))
        if indexer.available:
            pytest.skip("tree-sitter 已安装")
        result = indexer.index_file(str(c_file))
        assert result["symbols_found"] == 0
        assert result["calls_found"] == 0
        assert result["error"] is not None
        assert "tree-sitter" in result["error"]

    def test_get_symbol_definition_returns_empty_in_degraded(self, indexer):
        """降级模式下查询返回空列表（委托 db，表为空）"""
        if indexer.available:
            pytest.skip("tree-sitter 已安装")
        # 不崩溃即可，结果是空列表
        result = indexer.get_symbol_definition("HAL_Delay")
        assert isinstance(result, list)

    def test_find_dead_code_returns_empty_in_degraded(self, indexer):
        """降级模式下 find_dead_code 不崩溃"""
        if indexer.available:
            pytest.skip("tree-sitter 已安装")
        result = indexer.find_dead_code()
        assert isinstance(result, list)


# ── 3. 增量索引（mock tree-sitter） ─────────────────────────────

class TestIndexFileWithMock:
    """通过 mock tree-sitter 测试索引逻辑"""

    def _make_mock_indexer(self, db, project_root, symbols, calls):
        """构造一个 mock 的 CodeIndexer，跳过真实 AST 解析"""
        indexer = CodeIndexer(db, str(project_root))
        # 强制标记为可用
        indexer._has_ts = True
        indexer._parser = MagicMock()
        # mock parse 返回一个空 tree（_extract_symbols 被 mock 不用真实 AST）
        mock_tree = MagicMock()
        mock_tree.root_node = MagicMock()
        mock_tree.root_node.type = "translation_unit"
        mock_tree.root_node.children = []
        indexer._parser.parse.return_value = mock_tree
        # mock _extract_symbols 直接填充结果
        def fake_extract(node, source, file_path, sym_list, call_list, current_func=None):
            sym_list.extend(symbols)
            call_list.extend(calls)
        indexer._extract_symbols = fake_extract
        return indexer

    def test_index_file_writes_symbols_to_db(self, db, project_root, c_file):
        """索引文件后符号写入数据库"""
        symbols = [{
            "name": "main", "kind": "function",
            "file_path": str(c_file).replace("\\", "/"),
            "line_start": 9, "line_end": 13, "col_start": 0, "col_end": 1,
        }]
        indexer = self._make_mock_indexer(db, project_root, symbols, [])

        result = indexer.index_file(str(c_file))
        assert result["symbols_found"] == 1
        assert result["calls_found"] == 0
        assert result["error"] is None

        # 验证写入数据库
        defs = indexer.get_symbol_definition("main")
        assert len(defs) == 1
        assert defs[0]["name"] == "main"
        assert defs[0]["kind"] == "function"

    def test_index_file_writes_calls_to_db(self, db, project_root, c_file):
        """索引文件后调用关系写入数据库"""
        rel_path = str(c_file).replace("\\", "/")
        symbols = [{
            "name": "main", "kind": "function",
            "file_path": rel_path,
            "line_start": 9, "line_end": 13, "col_start": 0, "col_end": 1,
        }]
        calls = [
            {"caller_name": "main", "callee_name": "HAL_Init",
             "caller_file": rel_path, "caller_line": 10},
            {"caller_name": "main", "callee_name": "delay_ms",
             "caller_file": rel_path, "caller_line": 11},
        ]
        indexer = self._make_mock_indexer(db, project_root, symbols, calls)

        result = indexer.index_file(str(c_file))
        assert result["symbols_found"] == 1
        assert result["calls_found"] == 2

        # 验证调用关系
        callers = indexer.get_callers("HAL_Init")
        assert len(callers) == 1
        assert callers[0]["caller_name"] == "main"

        callees = indexer.get_callees("main")
        assert len(callees) == 2

    def test_index_file_nonexistent_returns_error(self, db, project_root):
        """索引不存在的文件返回错误"""
        indexer = CodeIndexer(db, str(project_root))
        indexer._has_ts = True
        indexer._parser = MagicMock()

        result = indexer.index_file("nonexistent.c")
        assert result["symbols_found"] == 0
        assert result["error"] is not None
        assert "不存在" in result["error"]

    def test_index_file_replaces_old_symbols(self, db, project_root, c_file):
        """增量索引：重新索引文件时替换旧数据"""
        rel_path = str(c_file).replace("\\", "/")
        # 第一次索引
        symbols_v1 = [{
            "name": "old_func", "kind": "function",
            "file_path": rel_path,
            "line_start": 1, "line_end": 2, "col_start": 0, "col_end": 1,
        }]
        indexer = self._make_mock_indexer(db, project_root, symbols_v1, [])
        indexer.index_file(str(c_file))
        assert len(indexer.get_symbol_definition("old_func")) == 1

        # 第二次索引：旧符号被删除，新符号加入
        symbols_v2 = [{
            "name": "new_func", "kind": "function",
            "file_path": rel_path,
            "line_start": 1, "line_end": 2, "col_start": 0, "col_end": 1,
        }]
        indexer._extract_symbols = lambda *a, **k: a[3].extend(symbols_v2)
        indexer.index_file(str(c_file))

        assert len(indexer.get_symbol_definition("old_func")) == 0
        assert len(indexer.get_symbol_definition("new_func")) == 1

    def test_index_file_absolute_path(self, db, project_root, c_file):
        """传入绝对路径也能正确索引"""
        symbols = [{
            "name": "test_abs", "kind": "function",
            "file_path": str(c_file).replace("\\", "/"),
            "line_start": 1, "line_end": 2, "col_start": 0, "col_end": 1,
        }]
        indexer = self._make_mock_indexer(db, project_root, symbols, [])
        abs_path = str(project_root / c_file)
        result = indexer.index_file(abs_path)
        assert result["symbols_found"] == 1
        assert result["error"] is None


# ── 4. 全量索引 ──────────────────────────────────────────────────

class TestIndexProject:
    """index_project 全量索引"""

    def test_index_project_empty_dir(self, db, project_root):
        """空项目目录返回零结果"""
        indexer = CodeIndexer(db, str(project_root))
        if indexer.available:
            pytest.skip("tree-sitter 已安装，跳过降级测试")
        result = indexer.index_project()
        assert result["files_indexed"] == 0

    def test_index_project_skips_ignore_dirs(self, db, project_root):
        """忽略目录中的文件不被索引"""
        # 在忽略目录中创建文件
        ignore_dir = project_root / "build"
        ignore_dir.mkdir()
        (ignore_dir / "ignored.c").write_text("void f(void){}\n", encoding="utf-8")

        indexer = CodeIndexer(db, str(project_root))
        indexer._has_ts = True
        indexer._parser = MagicMock()
        mock_tree = MagicMock()
        mock_tree.root_node = MagicMock()
        mock_tree.root_node.type = "translation_unit"
        mock_tree.root_node.children = []
        indexer._parser.parse.return_value = mock_tree
        indexer._extract_symbols = MagicMock()

        if not indexer._parser:
            pytest.skip("mock setup failed")

        result = indexer.index_project()
        # 忽略目录中的文件不应被索引
        assert result["files_indexed"] == 0

    def test_index_project_collects_errors(self, db, project_root, c_file):
        """全量索引时收集错误"""
        indexer = CodeIndexer(db, str(project_root))
        indexer._has_ts = True
        indexer._parser = MagicMock()
        # parse 抛异常
        indexer._parser.parse.side_effect = RuntimeError("parse error")

        result = indexer.index_project()
        # 应该有错误被收集
        assert len(result["errors"]) > 0
        assert result["files_indexed"] == 1  # 文件被计入（虽然解析失败）


# ── 5. 查询方法委托 ─────────────────────────────────────────────

class TestQueryDelegation:
    """查询方法正确委托给 Database"""

    def test_search_symbols_delegates_to_db(self, db, project_root):
        """search_symbols 委托给 db.search_symbols"""
        # 先写入一些符号
        db.save_symbol("HAL_Delay", "function", "src/hal.c",
                       10, 15, 0, 5, str(project_root))
        db.save_symbol("HAL_Init", "function", "src/hal.c",
                       20, 25, 0, 5, str(project_root))

        indexer = CodeIndexer(db, str(project_root))
        results = indexer.search_symbols("HAL")
        assert len(results) == 2
        names = [r["name"] for r in results]
        assert "HAL_Delay" in names
        assert "HAL_Init" in names

    def test_get_callers_delegates_to_db(self, db, project_root):
        """get_callers 委托给 db.get_callers"""
        db.save_call_edge("main", "HAL_Delay", "src/main.c", 12, str(project_root))
        db.save_call_edge("loop", "HAL_Delay", "src/loop.c", 5, str(project_root))

        indexer = CodeIndexer(db, str(project_root))
        callers = indexer.get_callers("HAL_Delay")
        assert len(callers) == 2
        caller_names = [c["caller_name"] for c in callers]
        assert "main" in caller_names
        assert "loop" in caller_names

    def test_get_callees_delegates_to_db(self, db, project_root):
        """get_callees 委托给 db.get_callees"""
        db.save_call_edge("main", "HAL_Init", "src/main.c", 10, str(project_root))
        db.save_call_edge("main", "delay_ms", "src/main.c", 11, str(project_root))

        indexer = CodeIndexer(db, str(project_root))
        callees = indexer.get_callees("main")
        assert len(callees) == 2
        callee_names = [c["callee_name"] for c in callees]
        assert "HAL_Init" in callee_names
        assert "delay_ms" in callee_names

    def test_find_dead_code_delegates_to_db(self, db, project_root):
        """find_dead_code 委托给 db.find_dead_code"""
        # main 被调用（不是死代码），unused_func 未被调用（死代码）
        db.save_symbol("main", "function", "src/main.c", 1, 5, 0, 1, str(project_root))
        db.save_symbol("unused_func", "function", "src/util.c", 1, 3, 0, 1, str(project_root))
        db.save_call_edge("_start", "main", "src/start.S", 1, str(project_root))

        indexer = CodeIndexer(db, str(project_root))
        dead = indexer.find_dead_code()
        # main 被调用不是死代码，unused_func 是死代码
        dead_names = [d["name"] for d in dead]
        assert "unused_func" in dead_names
        assert "main" not in dead_names

    def test_get_index_stats_delegates_to_db(self, db, project_root):
        """get_index_stats 委托给 db.get_index_stats"""
        db.save_symbol("func1", "function", "a.c", 1, 5, 0, 1, str(project_root))
        db.save_symbol("var1", "variable", "a.c", 1, 1, 0, 1, str(project_root))
        db.save_call_edge("func1", "func2", "a.c", 2, str(project_root))

        indexer = CodeIndexer(db, str(project_root))
        stats = indexer.get_index_stats()
        assert stats["symbols"] == 2
        assert stats["calls"] == 1
        assert stats["files_indexed"] == 1

    def test_get_symbol_definition_delegates_to_db(self, db, project_root):
        """get_symbol_definition 委托给 db.get_symbol_definition"""
        db.save_symbol("HAL_Delay", "function", "src/hal.c", 10, 15, 0, 5, str(project_root))

        indexer = CodeIndexer(db, str(project_root))
        defs = indexer.get_symbol_definition("HAL_Delay")
        assert len(defs) == 1
        assert defs[0]["name"] == "HAL_Delay"
        assert defs[0]["line_start"] == 10


# ── 6. 模块常量 ────────────────────────────────────────────────

class TestModuleConstants:
    """模块级常量验证"""

    def test_source_extensions_includes_c_h(self):
        """源文件扩展名包含 .c 和 .h"""
        assert ".c" in _SOURCE_EXTENSIONS
        assert ".h" in _SOURCE_EXTENSIONS

    def test_ignore_dirs_includes_build_and_git(self):
        """忽略目录包含 build 和 .git"""
        assert "build" in _IGNORE_DIRS
        assert ".git" in _IGNORE_DIRS
        assert "__pycache__" in _IGNORE_DIRS
