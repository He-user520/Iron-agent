"""共享常量 — 避免关键词在多处硬编码而不同步"""
from typing import FrozenSet

# ── EchoBackend 工具选择关键词 ──────────────────────────────────

# 触发 run_command 工具的关键词
ECHO_COMPILE_KEYWORDS: FrozenSet[str] = frozenset({
    "编译", "build", "运行", "run", "执行", "烧录", "flash", "deploy",
})

# 触发 chat 工具的关键词
ECHO_CHAT_KEYWORDS: FrozenSet[str] = frozenset({
    "你好", "hi", "hello", "介绍", "你是谁", "帮忙", "help", "？",
})

# ── engine.py 聊天内容检测指示符 ───────────────────────────────
# 检测 AI 是否试图将聊天内容写入源码文件（需要重定向到 chat）
SOURCE_EXTENSIONS: FrozenSet[str] = frozenset({
    ".c", ".h", ".cpp", ".hpp", ".rs", ".py", ".js", ".ts", ".go", ".java",
})

# 聊天指示符（避免误判正常 markdown 标题）
CHAT_INDICATORS: FrozenSet[str] = frozenset({
    "是的，我会", "我会使用", "需要我现在", "**编译**", "**方案",
    "好的，我来", "我来帮你", "我来为你",
})
