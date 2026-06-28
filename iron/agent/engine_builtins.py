"""内置工具 schema 定义

注意：工具执行逻辑已内联在 engine.py 的 _execute_write_file / _execute_run_command /
_execute_read_file 私有方法中。本模块仅维护工具 schema（单一数据源），
避免 schema 与实现分离导致的同步问题。
"""

# ── 内置工具 schema（engine.py 通过 BUILTIN_SCHEMAS 引用） ──────────────

BUILTIN_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "创建或写入文件到项目目录（完整覆盖）",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件相对路径，如 main.c"},
                    "content": {"type": "string", "description": "文件完整内容"},
                    "action": {"type": "string", "enum": ["新建", "修改"], "description": "新建或修改"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "执行 shell 命令（需要用户授权）。注意：这是 Windows 系统，用 dir 而不是 ls，用 del 而不是 rm",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的 Windows 命令"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取项目中的文件内容或列出目录内容（无需授权，可自由使用）",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件或目录的相对路径"},
                    "offset": {"type": "integer", "description": "起始行号（从1开始，用于分页读取大文件）"},
                    "limit": {"type": "integer", "description": "最大读取行数（默认 200）"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "chat",
            "description": "直接回复用户（不需要操作文件或执行命令时使用）",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "回复内容"},
                },
                "required": ["message"],
            },
        },
    },
]
