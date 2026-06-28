"""CodeIndexer — tree-sitter 代码索引核心

负责解析 C/C++ 代码 AST，提取符号定义和调用关系，写入 SQLite。
支持：
- 全量索引：遍历项目所有 .c/.h 文件
- 增量索引：单文件变更时只更新该文件
- 降级模式：tree-sitter 不可用时返回空结果，主流程不崩溃

tree-sitter 安装：pip install tree_sitter tree_sitter_c
"""
import logging
from pathlib import Path
from typing import Optional

from iron.core.db import Database

logger = logging.getLogger(__name__)

# 支持的源文件扩展名
_SOURCE_EXTENSIONS = {".c", ".h", ".cpp", ".hpp", ".cc", ".cxx"}

# 索引时忽略的目录
_IGNORE_DIRS = {
    ".git", "__pycache__", "node_modules", ".iron-agent",
    ".venv", "venv", ".idea", ".vscode", "build", ".cache",
    "CMakeFiles", "Dependencies",
}


class CodeIndexer:
    """tree-sitter 代码索引器

    用法:
        with Database() as db:
            indexer = CodeIndexer(db, "/path/to/project")
            stats = indexer.index_project()
            defs = indexer.get_symbol_definition("HAL_Delay")
    """

    def __init__(self, db: Database, project_root: str):
        self._db = db
        self._project_root = Path(project_root).resolve()
        self._project_path_str = str(self._project_root)
        self._has_ts = self._check_tree_sitter()
        self._parser = None
        self._lang = None
        if self._has_ts:
            try:
                self._init_parser()
            except (RuntimeError, ValueError, OSError) as e:
                logger.warning("tree-sitter 解析器初始化失败，降级模式: %s", e)
                self._has_ts = False

    # ── tree-sitter 检测与初始化 ────────────────────────────────────

    def _check_tree_sitter(self) -> bool:
        """检测 tree-sitter 和 tree_sitter_c 是否可用

        不可用时记录安装/启用命令到日志，提示用户通过 iron code-indexer init 一键启用。
        """
        try:
            import tree_sitter  # noqa: F401
            import tree_sitter_c  # noqa: F401
            return True
        except ImportError:
            logger.info(
                "tree-sitter 未安装，代码索引降级模式。"
                "安装: python -m pip install tree_sitter tree_sitter_c。"
                "一键启用: iron code-indexer init"
            )
            return False

    def _init_parser(self) -> None:
        """初始化 tree-sitter 解析器（兼容新旧 API）"""
        from tree_sitter import Language, Parser
        import tree_sitter_c

        # 新版 API（tree-sitter >= 0.22）：Language(c_language())
        # 旧版 API（tree-sitter < 0.22）：Language(c_language(), "c")
        c_language = tree_sitter_c.language()
        try:
            self._lang = Language(c_language)  # 新版
        except TypeError:
            self._lang = Language(c_language, "c")  # 旧版

        self._parser = Parser(self._lang)
        # 旧版 API 需要 set_language
        if not hasattr(self._parser, "language") or self._parser.language is None:
            try:
                self._parser.set_language(self._lang)
            except (AttributeError, RuntimeError):
                pass  # 新版 API 在构造时已传入

    @property
    def available(self) -> bool:
        """tree-sitter 是否可用"""
        return self._has_ts

    # ── 全量索引 ────────────────────────────────────────────────────

    def index_project(self) -> dict:
        """遍历项目所有 .c/.h 文件，全量索引

        返回:
            {"files_indexed": int, "symbols_found": int,
             "calls_found": int, "errors": list[str]}
        """
        result = {
            "files_indexed": 0,
            "symbols_found": 0,
            "calls_found": 0,
            "errors": [],
        }
        if not self._has_ts:
            result["errors"].append("tree-sitter 未安装")
            return result

        for ext in _SOURCE_EXTENSIONS:
            for file_path in self._project_root.rglob(f"*{ext}"):
                # 跳过忽略目录
                if any(part in _IGNORE_DIRS for part in file_path.parts):
                    continue
                try:
                    file_result = self.index_file(str(file_path))
                    result["files_indexed"] += 1
                    result["symbols_found"] += file_result.get("symbols_found", 0)
                    result["calls_found"] += file_result.get("calls_found", 0)
                    if file_result.get("error"):
                        result["errors"].append(file_result["error"])
                except (OSError, UnicodeDecodeError, RuntimeError) as e:
                    err_msg = f"{file_path}: {e}"
                    result["errors"].append(err_msg)
                    logger.warning("索引失败 %s: %s", file_path, e)

        return result

    # ── 增量索引（单文件） ─────────────────────────────────────────

    def index_file(self, file_path: str) -> dict:
        """增量索引单个文件

        步骤：
        1. 读取文件内容（UTF-8 / GBK 回退）
        2. 删除该文件的旧符号和调用关系
        3. 解析 AST，提取新符号和调用
        4. 批量写入数据库

        返回:
            {"symbols_found": int, "calls_found": int, "error": str | None}
        """
        result = {"symbols_found": 0, "calls_found": 0, "error": None}
        if not self._has_ts:
            result["error"] = "tree-sitter 未安装"
            return result

        abs_path = Path(file_path)
        if not abs_path.is_absolute():
            abs_path = (self._project_root / file_path).resolve()
        if not abs_path.exists():
            result["error"] = f"文件不存在: {file_path}"
            return result

        # 相对项目根的路径（用于数据库存储）
        try:
            rel_path = str(abs_path.relative_to(self._project_root)).replace("\\", "/")
        except ValueError:
            # 文件不在项目内，用绝对路径
            rel_path = str(abs_path).replace("\\", "/")

        # 读取文件内容
        try:
            source = abs_path.read_bytes()
        except OSError as e:
            result["error"] = f"读取失败: {e}"
            return result

        # 增量索引：先删除该文件的旧数据
        self._db.delete_symbols_by_file(rel_path, self._project_path_str)
        self._db.delete_calls_by_file(rel_path, self._project_path_str)

        # 解析 AST
        try:
            tree = self._parser.parse(source)
        except (RuntimeError, ValueError, OSError) as e:
            result["error"] = f"解析失败: {e}"
            return result

        symbols = []
        calls = []
        self._extract_symbols(tree.root_node, source, rel_path, symbols, calls)

        # 批量写入
        if symbols:
            self._db.save_symbols_batch(symbols, self._project_path_str)
        if calls:
            self._db.save_call_edges_batch(calls, self._project_path_str)

        result["symbols_found"] = len(symbols)
        result["calls_found"] = len(calls)
        return result

    # ── AST 遍历（提取符号和调用） ─────────────────────────────────

    def _extract_symbols(
        self,
        node,
        source: bytes,
        file_path: str,
        symbols: list,
        calls: list,
        current_function: Optional[str] = None,
    ) -> None:
        """递归遍历 AST，提取符号定义和调用关系

        Args:
            node: tree-sitter 节点
            source: 源代码字节
            file_path: 相对路径
            symbols: 输出参数，收集符号
            calls: 输出参数，收集调用关系
            current_function: 当前所在的函数名（用于 callgraph）
        """
        node_type = node.type

        if node_type == "function_definition":
            # 函数定义：提取函数名和位置
            func_name, line_start, line_end, col_start, col_end = self._extract_function_info(node, source)
            if func_name:
                symbols.append({
                    "name": func_name,
                    "kind": "function",
                    "file_path": file_path,
                    "line_start": line_start,
                    "line_end": line_end,
                    "col_start": col_start,
                    "col_end": col_end,
                })
                # 递归处理函数体，记录当前函数名用于 callgraph
                for child in node.children:
                    self._extract_symbols(child, source, file_path, symbols, calls, func_name)
                return

        elif node_type == "declaration":
            # 变量/类型声明
            self._extract_declaration(node, source, file_path, symbols)

        elif node_type == "type_definition":
            # typedef
            self._extract_typedef(node, source, file_path, symbols)

        elif node_type in ("preproc_def", "preproc_function_def"):
            # #define MACRO / #define MACRO(args)
            self._extract_macro(node, source, file_path, symbols)

        elif node_type == "call_expression":
            # 函数调用
            callee = self._extract_call_callee(node, source)
            if callee and current_function:
                calls.append({
                    "caller_name": current_function,
                    "callee_name": callee,
                    "caller_file": file_path,
                    "caller_line": node.start_point[0] + 1,  # 1-based
                })
            # call_expression 内部可能还有嵌套调用，继续遍历

        # 递归遍历子节点
        for child in node.children:
            self._extract_symbols(child, source, file_path, symbols, calls, current_function)

    def _extract_function_info(self, node, source: bytes) -> tuple:
        """从 function_definition 节点提取函数名和位置

        Returns: (name, line_start, line_end, col_start, col_end)
                 失败时 name 为空字符串
        """
        # 函数定义结构：type function_declarator compound_statement
        # function_declarator 可能是：identifier 直接标识符
        #                          或 pointer_declarator（指针函数）
        #                          或 function_declarator（带参数）
        name = ""
        declarator = None
        for child in node.children:
            if child.type in ("function_declarator", "identifier", "pointer_declarator"):
                declarator = child
                break

        if declarator is None:
            # 找第一个 declarator 字段
            try:
                declarator = node.child_by_field_name("declarator")
            except (AttributeError, RuntimeError):
                pass

        if declarator is not None:
            name = self._extract_declarator_name(declarator, source)

        if not name:
            return ("", 0, 0, 0, 0)

        # tree-sitter 行列是 0-based，转 1-based
        line_start = node.start_point[0] + 1
        line_end = node.end_point[0] + 1
        col_start = node.start_point[1]
        col_end = node.end_point[1]
        return (name, line_start, line_end, col_start, col_end)

    def _extract_declarator_name(self, node, source: bytes) -> str:
        """从 declarator 节点递归提取标识符名

        function_declarator 嵌套结构：
            function_declarator
              ├── parameters (parameter_list)
              └── declarator (可能是 identifier 或 pointer_declarator)
        """
        if node.type == "identifier":
            try:
                return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")
            except (IndexError, UnicodeDecodeError):
                return ""

        # pointer_declarator / function_declarator 都有 declarator 子字段
        try:
            inner = node.child_by_field_name("declarator")
            if inner is not None:
                return self._extract_declarator_name(inner, source)
        except (AttributeError, RuntimeError):
            pass

        # 退而求其次：找第一个 identifier 类型的子孙节点
        for child in node.children:
            if child.type == "identifier":
                try:
                    return source[child.start_byte:child.end_byte].decode("utf-8", errors="replace")
                except (IndexError, UnicodeDecodeError):
                    continue
            # 递归一层
            name = self._extract_declarator_name(child, source)
            if name:
                return name

        return ""

    def _extract_declaration(self, node, source: bytes, file_path: str, symbols: list) -> None:
        """从 declaration 节点提取变量/类型声明"""
        # declaration: type declarator(s)
        # 简化处理：找所有 identifier 类型的叶子节点
        # 但要排除函数声明（function_declarator 出现时）
        is_function_decl = False
        for child in node.children:
            if child.type == "function_declarator":
                is_function_decl = True
                # 函数声明（prototype）：也作为 function 符号记录
                name = self._extract_declarator_name(child, source)
                if name:
                    symbols.append({
                        "name": name,
                        "kind": "function",
                        "file_path": file_path,
                        "line_start": node.start_point[0] + 1,
                        "line_end": node.end_point[0] + 1,
                        "col_start": node.start_point[1],
                        "col_end": node.end_point[1],
                    })
                break

        if is_function_decl:
            return

        # 普通变量声明：提取所有 identifier（变量名）
        # 注意 declaration 可能有多个 declarator：int a, b, c;
        for child in node.children:
            if child.type in ("init_declarator", "identifier", "pointer_declarator", "array_declarator"):
                name = self._extract_declarator_name(child, source)
                if name:
                    symbols.append({
                        "name": name,
                        "kind": "variable",
                        "file_path": file_path,
                        "line_start": node.start_point[0] + 1,
                        "line_end": node.end_point[0] + 1,
                        "col_start": node.start_point[1],
                        "col_end": node.end_point[1],
                    })

    def _extract_typedef(self, node, source: bytes, file_path: str, symbols: list) -> None:
        """从 type_definition 节点提取 typedef"""
        # typedef <type> <name>;
        # type_definition 的 type 字段是类型，declarator 字段是别名
        try:
            declarator = node.child_by_field_name("declarator")
            if declarator is not None:
                name = self._extract_declarator_name(declarator, source)
                if name:
                    symbols.append({
                        "name": name,
                        "kind": "type",
                        "file_path": file_path,
                        "line_start": node.start_point[0] + 1,
                        "line_end": node.end_point[0] + 1,
                        "col_start": node.start_point[1],
                        "col_end": node.end_point[1],
                    })
        except (AttributeError, RuntimeError):
            pass

    def _extract_macro(self, node, source: bytes, file_path: str, symbols: list) -> None:
        """从 preproc_def / preproc_function_def 节点提取宏定义"""
        # preproc_def: #define NAME value
        #   children: name (identifier), value (optional)
        # preproc_function_def: #define NAME(args) value
        #   children: name (identifier), parameters, value
        for child in node.children:
            if child.type == "identifier":
                try:
                    name = source[child.start_byte:child.end_byte].decode("utf-8", errors="replace")
                except (IndexError, UnicodeDecodeError):
                    continue
                if name:
                    symbols.append({
                        "name": name,
                        "kind": "macro",
                        "file_path": file_path,
                        "line_start": node.start_point[0] + 1,
                        "line_end": node.end_point[0] + 1,
                        "col_start": node.start_point[1],
                        "col_end": node.end_point[1],
                    })
                break  # 只取第一个 identifier（宏名）

    def _extract_call_callee(self, node, source: bytes) -> str:
        """从 call_expression 节点提取被调用的函数名

        call_expression 结构：function arguments
        function 可能是 identifier（HAL_Delay）或 field_expression（obj.method()）
        """
        try:
            func_node = node.child_by_field_name("function")
            if func_node is not None:
                if func_node.type == "identifier":
                    try:
                        return source[func_node.start_byte:func_node.end_byte].decode("utf-8", errors="replace")
                    except (IndexError, UnicodeDecodeError):
                        return ""
        except (AttributeError, RuntimeError):
            pass

        # 退而求其次：找第一个 identifier 子节点
        for child in node.children:
            if child.type == "identifier":
                try:
                    return source[child.start_byte:child.end_byte].decode("utf-8", errors="replace")
                except (IndexError, UnicodeDecodeError):
                    continue
        return ""

    # ── 查询方法（委托给 db） ──────────────────────────────────────

    def get_symbol_definition(self, name: str) -> list[dict]:
        """查找符号定义（可能多处）"""
        return self._db.get_symbol_definition(name, self._project_path_str)

    def search_symbols(self, query: str, limit: int = 20) -> list[dict]:
        """按名称搜索符号"""
        return self._db.search_symbols(query, self._project_path_str, limit)

    def get_callers(self, callee_name: str) -> list[dict]:
        """查找调用某函数的所有位置"""
        return self._db.get_callers(callee_name, self._project_path_str)

    def get_callees(self, caller_name: str) -> list[dict]:
        """查找某函数调用的所有函数"""
        return self._db.get_callees(caller_name, self._project_path_str)

    def find_dead_code(self) -> list[dict]:
        """查找未被任何函数调用的函数（死代码）"""
        return self._db.find_dead_code(self._project_path_str)

    def get_index_stats(self) -> dict:
        """获取索引统计信息"""
        return self._db.get_index_stats(self._project_path_str)
