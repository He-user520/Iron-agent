"""Agent Engine 事件与数据类型定义

独立文件，避免循环导入（engine.py ↔ engine_builtins.py）。
"""
from dataclasses import dataclass, field
from enum import Enum


class Phase(Enum):
    THINK = "think"
    EXECUTE = "execute"
    DONE = "done"
    CHAT = "chat"


@dataclass
class FileSpec:
    """文件规格"""
    path: str
    action: str = "新建"
    description: str = ""
    language: str = "c"


@dataclass
class Plan:
    """执行计划"""
    intent: str = ""
    files: list[FileSpec] = field(default_factory=list)
    modules: list[str] = field(default_factory=list)
    questions: list[dict] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)


@dataclass
class AgentEvent:
    """Agent 事件 — 传递给 UI 渲染

    事件类型:
    - thinking: 思考中
    - phase: 阶段切换
    - step_done / step_warn: 步骤完成/警告
    - plan: 执行计划
    - file_start / file_done: 文件开始/完成
    - file_code: 文件代码
    - file_diff: 文件修改 diff
    - file_read: 文件内容
    - file_tree: 项目文件树
    - command: 命令执行结果
    - chat_response: 对话回复
    - permission_request: 授权请求
    - summary: 总结
    - error: 错误
    """
    type: str
    data: dict = field(default_factory=dict)
