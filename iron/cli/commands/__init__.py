"""斜杠命令分组模块

将原 main.py 中过长的命令处理逻辑按功能拆分到独立模块：
- file_cmds: 文件操作命令（/read /write /edit /delete /files /undo）
- build_cmds: 构建相关命令（/code /check /build /flash /monitor /verify /explore）
- session_cmds: 会话管理命令（/history /resume /compact /context /clear）
- system_cmds: 系统命令（/model /skill /rules /config /agent /help /quit）

每个模块提供统一的 handler 函数：
    handle_xxx_commands(cmd: str, args: str, ctx: dict) -> bool
返回 True 表示命令已处理，False 表示未匹配。

ctx 字典携带共享状态（console/config/llm/session/last_engine/loop 等），
handler 可修改其中的可变状态，主循环在分发后同步回本地变量。
"""
from iron.cli.commands.file_cmds import handle_file_commands
from iron.cli.commands.build_cmds import handle_build_commands
from iron.cli.commands.session_cmds import handle_session_commands
from iron.cli.commands.system_cmds import handle_system_commands

# 命令分组映射
COMMAND_GROUPS = {
    "file": ["/read", "/write", "/edit", "/delete", "/files", "/undo"],
    "build": ["/code", "/check", "/build", "/flash", "/monitor", "/verify", "/explore"],
    "session": ["/history", "/resume", "/compact", "/context", "/clear"],
    "system": ["/model", "/skill", "/rules", "/config", "/agent", "/help", "/quit"],
}

__all__ = [
    "handle_file_commands",
    "handle_build_commands",
    "handle_session_commands",
    "handle_system_commands",
    "COMMAND_GROUPS",
]
