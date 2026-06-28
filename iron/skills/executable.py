"""可执行 Skill 子类 — 4 个内置 Skill 升级为 ExecutableSkill

与 PromptSkill 的区别：
- PromptSkill 仅注入 prompt 指导 AI（被动）
- ExecutableSkill 可注册工具 + 执行预处理/后处理（主动）

4 个可执行 Skill：
- McuInitSkill: mcu-init，注册 MCUInitTool
- DriverGenSkill: driver-gen，注册 DriverGenTool
- BugHuntSkill: bug-hunt，注册 BugHuntTool + pre_execute 调 LSP 诊断
- MisraCheckSkill: misra-check，注册 MisraCheckTool + pre_execute 调 EmbedGuard

其他 4 个保持 PromptSkill（peripheral-setup/rtos-setup/power-optimize/debug-helper）
"""
import asyncio
import logging
from pathlib import Path
from iron.skills.base import ExecutableSkill, SkillContext, SkillResult
from iron.tools.skill_tools import MCUInitTool, DriverGenTool, BugHuntTool, MisraCheckTool

logger = logging.getLogger(__name__)

# pre_execute 超时（秒）— 防止阻塞主循环
_PRE_EXECUTE_TIMEOUT = 5.0


class McuInitSkill(ExecutableSkill):
    """MCU 项目初始化 Skill — 可执行版本

    注册 skill_mcu_init 工具，让 AI 可直接调用生成项目骨架。
    同时保留 prompt 注入（提供初始化指导）。
    """

    def __init__(self):
        self.name = "mcu-init"
        self.description = "初始化嵌入式项目 — 选择 MCU、框架、构建系统"
        self.trigger_patterns = ["新建项目", "初始化", "创建项目", "init", "new project", "搭建"]
        self.icon = "🚀"

    def can_handle(self, user_input, intent=None):
        return any(p in user_input.lower() for p in self.trigger_patterns)

    def get_tools(self):
        return [MCUInitTool()]

    async def pre_execute(self, context: SkillContext) -> SkillResult:
        """预处理：记录初始化意图到 session_data"""
        context.session_data["mcu_init_active"] = True
        return SkillResult(success=True, message="MCU 初始化 Skill 已激活")

    def build_prompt(self, context: SkillContext) -> str:
        return """## MCU 项目初始化指导

按以下步骤执行嵌入式项目初始化：

1. **确认 MCU 型号**：用 ask_user 询问目标 MCU（STM32F407/ESP32/Arduino 等）
2. **确认框架**：推荐 PlatformIO（支持 1500+ 板卡）
3. **生成项目骨架**：调用 skill_mcu_init(mcu="STM32F407", framework="hal")
4. **写文件**：用 write_file 创建 platformio.ini 和 src/main.c
5. **首次编译验证**：embed_build(action="compile")

【铁律】使用 HAL 库，禁用动态内存分配，禁用递归。"""


class DriverGenSkill(ExecutableSkill):
    """外设驱动生成 Skill — 可执行版本

    注册 skill_driver_gen 工具，让 AI 可直接调用生成驱动模板。
    pre_execute 尝试读取 target-mcu.md 获取 MCU 信息。
    """

    def __init__(self):
        self.name = "driver-gen"
        self.description = "生成外设驱动代码 — 根据 MCU 寄存器定义自动生成"
        self.trigger_patterns = ["写驱动", "驱动", "初始化uart", "初始化spi", "初始化i2c", "配置gpio",
                                 "uart驱动", "spi驱动", "i2c驱动", "driver"]
        self.icon = "🔧"

    def can_handle(self, user_input, intent=None):
        return any(p in user_input.lower() for p in self.trigger_patterns)

    def get_tools(self):
        return [DriverGenTool()]

    async def pre_execute(self, context: SkillContext) -> SkillResult:
        """预处理：尝试读取 target-mcu.md 获取 MCU 配置"""
        try:
            mcu_file = Path(context.project_root) / ".iron-agent" / "rules" / "target-mcu.md"
            if mcu_file.exists():
                content = mcu_file.read_text(encoding="utf-8", errors="replace")
                context.session_data["mcu_config"] = content[:2000]  # 限制大小
                return SkillResult(success=True, message=f"已加载 MCU 配置 ({len(content)} 字符)")
        except (OSError, UnicodeDecodeError) as e:
            logger.warning(f"读取 target-mcu.md 失败: {e}")
        return SkillResult(success=True, message="未找到 target-mcu.md，将在 Skill 执行时询问用户")

    def build_prompt(self, context: SkillContext) -> str:
        return """## 外设驱动生成指导

按以下步骤生成外设驱动：

1. **确认外设**：用 ask_user 确认要生成哪个外设的驱动（UART/SPI/I2C/GPIO）
2. **生成驱动模板**：调用 skill_driver_gen(peripheral="uart", mcu="STM32F407")
3. **写文件**：用 write_file 创建 src/{peripheral}_driver.h 和 .c
4. **使用 HAL 库**：优先调用 HAL_xxx_Init，避免直接寄存器操作
5. **volatile 关键字**：所有寄存器指针必须用 volatile
6. **生成后用 embed_lint 检查**：确保符合 MISRA 规则"""


class BugHuntSkill(ExecutableSkill):
    """问题诊断 Skill — 可执行版本

    注册 skill_bug_hunt 工具，让 AI 可直接调用收集诊断信息。
    pre_execute 调用 LSP 诊断收集错误（若有 LSP 客户端）。
    """

    def __init__(self):
        self.name = "bug-hunt"
        self.description = "问题诊断 — 分析串口日志、HardFault、死锁等问题"
        self.trigger_patterns = ["不工作", "没输出", "卡死", "hardfault", "死锁", "bug", "错误",
                                 "异常", "重启", "崩溃", "不正常"]
        self.icon = "🔍"

    def can_handle(self, user_input, intent=None):
        return any(p in user_input.lower() for p in self.trigger_patterns)

    def get_tools(self):
        return [BugHuntTool()]

    async def pre_execute(self, context: SkillContext) -> SkillResult:
        """预处理：若有 LSP 客户端，收集诊断信息"""
        if context.lsp_client is None:
            return SkillResult(success=True, message="LSP 未启用，跳过诊断收集")

        try:
            # 尝试收集 LSP 诊断（5 秒超时）
            diagnostics = await asyncio.wait_for(
                context.lsp_client.get_diagnostics(""),
                timeout=_PRE_EXECUTE_TIMEOUT,
            )
            if diagnostics:
                context.session_data["lsp_diagnostics"] = str(diagnostics)[:2000]
                return SkillResult(success=True, message=f"已收集 {len(diagnostics)} 条 LSP 诊断")
        except asyncio.TimeoutError:
            logger.warning("LSP 诊断收集超时（5秒），跳过")
        except (RuntimeError, OSError, AttributeError) as e:
            logger.warning(f"LSP 诊断收集失败: {e}")
        return SkillResult(success=True, message="无 LSP 诊断信息")

    def build_prompt(self, context: SkillContext) -> str:
        diag_hint = ""
        if context.session_data.get("lsp_diagnostics"):
            diag_hint = f"\n\n### LSP 诊断信息（已收集）\n{context.session_data['lsp_diagnostics']}"
        return f"""## 问题诊断指导

按以下步骤诊断嵌入式问题：

1. **收集症状**：用 ask_user 询问具体现象（HardFault/无输出/卡死/重启）
2. **调用诊断工具**：skill_bug_hunt(symptom="hardfault", file_path="src/main.c")
3. **读取代码**：read_file 读取相关源文件
4. **静态分析**：embed_lint 检查常见问题（volatile 缺失/ISR 阻塞/动态内存）
5. **给出修复建议**：用 chat() 说明问题原因和修复方案
6. **修复后验证**：embed_build(action="compile") + embed_lint{diag_hint}"""


class MisraCheckSkill(ExecutableSkill):
    """MISRA 合规检查 Skill — 可执行版本

    注册 skill_misra_check 工具，让 AI 可直接调用执行 MISRA 检查。
    pre_execute 标记检查意图，post_execute 可记录违规统计。
    """

    def __init__(self):
        self.name = "misra-check"
        self.description = "MISRA C 合规性检查 — 静态分析 + 偏差报告"
        self.trigger_patterns = ["misra", "合规", "静态分析", "代码检查", "lint"]
        self.icon = "🛡️"

    def can_handle(self, user_input, intent=None):
        return any(p in user_input.lower() for p in self.trigger_patterns)

    def get_tools(self):
        return [MisraCheckTool()]

    async def pre_execute(self, context: SkillContext) -> SkillResult:
        """预处理：标记 MISRA 检查激活"""
        context.session_data["misra_check_active"] = True
        return SkillResult(success=True, message="MISRA 检查已激活")

    async def post_execute(self, context: SkillContext, result) -> SkillResult:
        """后处理：记录违规统计到 session_data"""
        if isinstance(result, dict) and result.get("violations"):
            violation_count = len(result["violations"])
            context.session_data["misra_violation_count"] = violation_count
            return SkillResult(
                success=True,
                message=f"MISRA 检查完成，发现 {violation_count} 个违规",
            )
        return SkillResult(success=True, message="MISRA 检查完成，无违规")

    def build_prompt(self, context: SkillContext) -> str:
        return """## MISRA 合规检查指导

按以下步骤执行 MISRA C 检查：

1. **调用 MISRA 检查工具**：skill_misra_check(files=["src/main.c", "src/uart_driver.c"])
2. **检查规则**（15 条 EMB 规则）：详见工具返回的 rules_applied
3. **生成报告**：用 chat() 汇总违规项和修复建议
4. **自动修复**：对 EMB001/005/014 可用 edit_file 修复"""


# 可执行 Skill 实例列表（替代部分 PromptSkill）
EXECUTABLE_SKILLS: list[ExecutableSkill] = [
    McuInitSkill(),
    DriverGenSkill(),
    BugHuntSkill(),
    MisraCheckSkill(),
]
