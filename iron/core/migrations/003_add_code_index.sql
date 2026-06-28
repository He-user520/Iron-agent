-- 003_add_code_index.sql — 代码索引与调用图
-- symbols 表：函数/变量/类型/宏定义
-- callgraph 表：函数调用关系

-- 符号定义表
CREATE TABLE IF NOT EXISTS symbols (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,                -- 符号名（如 HAL_Delay）
    kind TEXT NOT NULL,                -- function | variable | type | macro
    file_path TEXT NOT NULL,           -- 相对项目根的路径
    line_start INTEGER NOT NULL,
    line_end INTEGER NOT NULL,
    col_start INTEGER NOT NULL,
    col_end INTEGER NOT NULL,
    project_path TEXT NOT NULL,        -- 项目根（多项目隔离）
    indexed_at TEXT NOT NULL,
    UNIQUE(name, file_path, line_start)
);
CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_project ON symbols(project_path);
CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file_path);

-- 调用图表：函数调用关系
CREATE TABLE IF NOT EXISTS callgraph (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    caller_name TEXT NOT NULL,         -- 调用方符号名
    callee_name TEXT NOT NULL,         -- 被调用符号名
    caller_file TEXT NOT NULL,
    caller_line INTEGER NOT NULL,
    project_path TEXT NOT NULL,
    indexed_at TEXT NOT NULL,
    UNIQUE(caller_name, callee_name, caller_file, caller_line)
);
CREATE INDEX IF NOT EXISTS idx_callgraph_callee ON callgraph(callee_name);
CREATE INDEX IF NOT EXISTS idx_callgraph_caller ON callgraph(caller_name);
CREATE INDEX IF NOT EXISTS idx_callgraph_project ON callgraph(project_path);
