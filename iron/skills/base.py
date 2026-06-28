"""Skill 基类 — 所有内置技能继承此类"""
from dataclasses import dataclass, field
from abc import ABC, abstractmethod
from typing import Any


# match_score 权重常量
PATTERN_MATCH_SCORE = 0.6  # 命中触发关键词的分数
CAN_HANDLE_SCORE = 0.4    # can_handle 返回 True 的分数


@dataclass
class SkillResult:
    """技能执行结果"""
    success: bool = True
    message: str = ""
    files_created: list = field(default_factory=list)
    files_modified: list = field(default_factory=list)
    next_steps: list = field(default_factory=list)


@dataclass
class SkillContext:
    """Skill 执行上下文 — 受控访问 engine 状态

    Skill 通过此上下文访问 engine 资源，避免直接修改 engine 内部状态。
    """
    user_input: str = ""
    project_root: str = "."
    tool_registry: Any = None      # ToolRegistry 引用
    llm: Any = None                # LLMBackend 引用
    lsp_client: Any = None         # LSPClient 引用（可为 None）
    session_data: dict = field(default_factory=dict)  # 会话级数据（Skill 间共享）


class BaseSkill(ABC):
    """技能基类"""
    name: str = ""
    description: str = ""
    trigger_patterns: list[str] = []
    icon: str = "🔧"

    @abstractmethod
    def can_handle(self, user_input: str, intent: dict = None) -> bool:
        """判断是否能处理该用户输入"""
        pass

    @abstractmethod
    async def execute(self, context: dict) -> SkillResult:
        """执行技能，返回结果"""
        pass

    def match_score(self, user_input: str) -> float:
        """计算匹配分数 (0.0 ~ 1.0)"""
        score = 0.0
        text = user_input.lower()
        for pattern in self.trigger_patterns:
            if pattern.lower() in text:
                score += PATTERN_MATCH_SCORE
                break
        if self.can_handle(user_input):
            score += CAN_HANDLE_SCORE
        return min(score, 1.0)


class ExecutableSkill(BaseSkill):
    """可执行 Skill — 支持注册工具、预处理、后处理

    与 PromptSkill 的区别：
    - PromptSkill 仅注入 prompt 指导 AI（被动）
    - ExecutableSkill 可注册工具 + 执行预处理/后处理（主动）

    向后兼容：ExecutableSkill 仍可覆盖 build_prompt() 提供 prompt 注入。
    """

    def get_tools(self) -> list:
        """返回此 Skill 注册到 ToolRegistry 的工具列表

        返回空列表表示此 Skill 不注册新工具。
        工具实例应是 BaseTool 子类（含 name/description/schema/execute）。
        """
        return []

    async def pre_execute(self, context: SkillContext) -> SkillResult:
        """预处理：在 LLM 调用前执行

        用途：
        - 收集上下文（读取配置文件、查询 LSP 诊断）
        - 预处理用户输入（参数提取、意图细化）
        - 注册动态工具到 tool_registry

        超时约束：必须在 5 秒内完成，否则主循环会跳过。
        """
        return SkillResult(success=True)

    async def post_execute(self, context: SkillContext, result: Any) -> SkillResult:
        """后处理：在 LLM 调用后执行

        用途：
        - 验证 LLM 输出（编译检查、lint 检查）
        - 清理临时资源
        - 记录执行结果到 session_data
        """
        return SkillResult(success=True)

    def build_prompt(self, context: SkillContext) -> str:
        """构建 prompt 注入内容（向后兼容）

        ExecutableSkill 可同时提供 prompt 注入和工具注册。
        默认返回空字符串（不注入 prompt）。
        """
        return ""

    async def execute(self, context: dict) -> SkillResult:
        """执行技能（兼容旧接口，ExecutableSkill 用 pre/post_execute 替代）

        向后兼容：返回 build_prompt() 内容到 next_steps，让旧调用方仍能获取 prompt。
        """
        # 构建 SkillContext（兼容旧 dict 调用方）
        from iron.skills.base import SkillContext
        ctx = SkillContext(
            user_input=context.get("user_input", "") if isinstance(context, dict) else "",
            project_root=context.get("project_root", ".") if isinstance(context, dict) else ".",
        )
        prompt = self.build_prompt(ctx)
        return SkillResult(
            success=True,
            message=f"{self.name} 可执行技能已触发",
            next_steps=[prompt] if prompt else [],
        )
