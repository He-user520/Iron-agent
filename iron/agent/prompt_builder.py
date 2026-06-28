"""三层 Prompt 构建器 — 将铁律 + 反模式 + 项目规则注入 system prompt"""
from pathlib import Path
from iron.rules.iron_rules import get_iron_rules_prompt
from iron.rules.ai_antipatterns import get_antipatterns_prompt
from iron.rules.project_rules import ProjectRulesLoader


BASE_SYSTEM_PROMPT = """你是 Iron，一个专为嵌入式开发打造的 AI 编程助手。你运行在终端中，帮助工程师编写高质量的嵌入式 C/Rust 代码。

## 你的身份
- 你是嵌入式系统专家，精通 ARM Cortex-M、RISC-V 架构
- 你熟悉 STM32/ESP32/Nordic/Arduino 等主流 MCU 平台
- 你精通 FreeRTOS/Zephyr/裸机开发
- 你理解硬件寄存器、中断、DMA、时钟树等底层概念

## 工作方式
当用户描述一个需求时，你应该：
1. **理解** — 分析用户意图，识别目标 MCU、语言、框架、关键约束
2. **规划** — 列出要创建/修改的文件，设计模块架构
3. **提问** — 对不确定的参数主动提问（一次 1-3 个问题）
4. **编码** — 按规划逐文件生成代码，每文件后检查质量
5. **审查** — 检查内存安全、ISR 安全、寄存器访问、类型安全
6. **完成** — 展示摘要，询问下一步操作

## 输出格式
- 代码用 markdown 代码块包裹，标注语言
- 文件路径用完整相对路径
- 解释用简洁的中文
- 不确定的地方主动提问，不要猜测
"""


class PromptBuilder:
    """三层 Prompt 构建器"""

    def __init__(self, project_root: Path | None = None, mcu: str = "stm32f407"):
        # 注意：project_root 默认使用当前工作目录（Path.cwd()），
        # 调用方在嵌入式中应显式传入项目根目录以避免误用进程 cwd
        self.project_root = project_root or Path.cwd()
        self.mcu = mcu or "unknown"
        self.project_rules = ProjectRulesLoader(self.project_root)

    def build(self) -> str:
        """构建完整的 system prompt"""
        parts = [BASE_SYSTEM_PROMPT]

        # Layer 1: 嵌入式铁律
        parts.append(get_iron_rules_prompt())

        # Layer 2: AI 常犯错误
        parts.append(get_antipatterns_prompt())

        # Layer 3: 项目级规则
        project_rules_text = self.project_rules.load_all()
        if project_rules_text:
            parts.append(project_rules_text)

        # 环境上下文
        parts.append(self._build_context())

        return "\n\n---\n\n".join(parts)

    def _build_context(self) -> str:
        """构建环境上下文"""
        ctx = f"# 当前环境\n\n- 目标 MCU: {self.mcu.upper()}\n"
        ctx += f"- 项目目录: {self.project_root}\n"

        mcu_info = self.project_rules.load_mcu_info()
        if mcu_info:
            ctx += f"\n## MCU 信息\n\n{mcu_info}\n"

        return ctx

    def count_active_rules(self) -> tuple[int, int, int]:
        """返回三层规则数量: (铁律, 反模式, 项目规则)"""
        from iron.rules.iron_rules import IRON_RULES
        from iron.rules.ai_antipatterns import AI_ANTIPATTERNS
        return len(IRON_RULES), len(AI_ANTIPATTERNS), self.project_rules.count_rules()
