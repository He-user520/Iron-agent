"""Skill 注册表 — 发现、匹配、执行技能

v2: 内置 skill 提供实际逻辑（不再是 stub），通过 prompt 注入指导 AI。
用户自定义 skill 从 .iron/skills/*.md 加载。

v2.4 重构：8 个内置 Skill 子类改为数据驱动的 PromptSkill 基类 + 配置表，
消除 can_handle/execute/_build_prompt 三段重复模板（原 322 行 → 现 ~200 行）。

v2.8 升级：4 个内置 Skill 升级为 ExecutableSkill（可注册工具 + pre/post_execute），
其余 4 个保持 PromptSkill（向后兼容）。
"""
import logging
from pathlib import Path
from iron.skills.base import BaseSkill, SkillResult


# 匹配阈值：match_score 超过此值才认为技能命中
MATCH_THRESHOLD = 0.5

# 尝试使用 pyyaml 解析 YAML frontmatter，不可用时回退到手写解析器
try:
    import yaml as _yaml  # type: ignore
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False


# ── 数据驱动的 Prompt Skill ────────────────────────────────────

class PromptSkill(BaseSkill):
    """数据驱动的 Prompt Skill — 内置技能的统一基类

    内置技能都是相同模式：匹配触发关键词 → 返回 prompt 注入指导 AI。
    不再有实际执行逻辑差异，因此用一个基类 + 配置数据驱动。
    """

    def __init__(self, name: str, description: str, trigger_patterns: list[str],
                 icon: str, prompt: str):
        self.name = name
        self.description = description
        self.trigger_patterns = trigger_patterns
        self.icon = icon
        self._prompt = prompt

    def can_handle(self, user_input, intent=None):
        return any(p in user_input.lower() for p in self.trigger_patterns)

    async def execute(self, context):
        return SkillResult(
            success=True,
            message=f"{self.name} 技能已触发",
            next_steps=[self._prompt],
        )


# ── 内置技能配置数据 ───────────────────────────────────────────

_BUILTIN_SKILL_CONFIGS = [
    {
        "name": "mcu-init",
        "description": "初始化嵌入式项目 — 选择 MCU、框架、构建系统",
        "trigger_patterns": ["新建项目", "初始化", "创建项目", "init", "new project", "搭建"],
        "icon": "🚀",
        "prompt": """## MCU 项目初始化指导

按以下步骤执行嵌入式项目初始化：

1. **确认 MCU 型号**：用 ask_user 询问目标 MCU（STM32F407/ESP32/Arduino 等）
2. **确认框架**：推荐 PlatformIO（支持 1500+ 板卡）
3. **创建项目结构**：
   - platformio.ini（含 MCU 配置）
   - src/main.c（HAL 库模板）
   - .iron-agent/rules/target-mcu.md（MCU 内存布局）
4. **使用 embed_build(action="scaffold")** 自动检测并创建 platformio.ini
5. **首次编译验证**：embed_build(action="compile")

【铁律】使用 HAL 库，禁用动态内存分配，禁用递归。""",
    },
    {
        "name": "driver-gen",
        "description": "生成外设驱动代码 — 根据 MCU 寄存器定义自动生成",
        "trigger_patterns": ["写驱动", "驱动", "初始化uart", "初始化spi", "初始化i2c", "配置gpio",
                             "uart驱动", "spi驱动", "i2c驱动", "driver"],
        "icon": "🔧",
        "prompt": """## 外设驱动生成指导

按以下步骤生成外设驱动：

1. **读取 MCU 配置**：read_file(".iron-agent/rules/target-mcu.md") 获取寄存器地址
2. **确认外设**：用 ask_user 确认要生成哪个外设的驱动（UART/SPI/I2C/GPIO）
3. **生成驱动文件**：write_file 创建 src/{peripheral}_driver.h 和 .c
4. **驱动结构**：
   - 头文件：函数声明 + 寄存器宏定义
   - 源文件：初始化函数 + 读/写函数 + 中断处理函数
5. **使用 HAL 库**：优先调用 HAL_xxx_Init，避免直接寄存器操作
6. **volatile 关键字**：所有寄存器指针必须用 volatile
7. **生成后用 embed_lint 检查**：确保符合 MISRA 规则""",
    },
    {
        "name": "peripheral-setup",
        "description": "配置外设参数 — GPIO/UART/SPI/I2C/DMA/定时器",
        "trigger_patterns": ["配置", "设置引脚", "gpio配置", "时钟配置", "dma配置", "定时器配置"],
        "icon": "⚙️",
        "prompt": """## 外设配置指导

按以下步骤配置外设：

1. **读取现有配置**：read_file("platformio.ini") 或 read_file("src/main.c")
2. **确认配置参数**：用 ask_user 询问具体参数（波特率/时钟频率/引脚号）
3. **修改代码**：用 edit_file 精确修改配置部分
4. **配置项**：
   - GPIO：模式（输入/输出/复用/模拟）、上下拉、速度
   - UART：波特率、数据位、停止位、校验
   - SPI：模式（Master/Slave）、时钟极性/相位、分频
   - DMA：通道、方向、数据宽度、循环模式
5. **编译验证**：embed_build(action="compile")""",
    },
    {
        "name": "bug-hunt",
        "description": "问题诊断 — 分析串口日志、HardFault、死锁等问题",
        "trigger_patterns": ["不工作", "没输出", "卡死", "hardfault", "死锁", "bug", "错误",
                             "异常", "重启", "崩溃", "不正常"],
        "icon": "🔍",
        "prompt": """## 问题诊断指导

按以下步骤诊断嵌入式问题：

1. **收集症状**：用 ask_user 询问具体现象（HardFault/无输出/卡死/重启）
2. **读取代码**：read_file 读取相关源文件
3. **静态分析**：embed_lint 检查常见问题（volatile 缺失/ISR 阻塞/动态内存）
4. **常见原因排查**：
   - HardFault：空指针/数组越界/栈溢出/未初始化变量
   - 无输出：时钟未使能/引脚配置错误/波特率不匹配
   - 卡死：死循环/ISR 优先级冲突/看门狗未喂狗
   - 重启：看门狗超时/栈溢出/电源不稳
5. **给出修复建议**：用 chat() 说明问题原因和修复方案
6. **修复后验证**：embed_build(action="compile") + embed_lint""",
    },
    {
        "name": "rtos-setup",
        "description": "RTOS 任务管理 — 创建任务、信号量、队列、互斥锁",
        "trigger_patterns": ["freertos", "rtos", "任务", "信号量", "队列", "互斥锁", "task"],
        "icon": "🧵",
        "prompt": """## RTOS 配置指导

按以下步骤配置 FreeRTOS：

1. **确认任务需求**：用 ask_user 询问需要多少任务、各自职责
2. **生成任务模板**：write_file 创建 src/tasks.c
3. **任务结构**：
   - 任务函数：void Task_xxx(void *arg)
   - 创建函数：xTaskCreate(vTaskCode, "name", stackSize, arg, priority, &handle)
   - 信号量：xSemaphoreCreateBinary/CreateMutex
   - 队列：xQueueCreate(len, itemSize)
4. **优先级规划**：高优先级给实时性任务（控制），低优先级给非实时任务（日志）
5. **栈大小**：最小 128 字，含浮点运算的 256 字
6. **临界区保护**：用 taskENTER_CRITICAL/taskEXIT_CRITICAL
7. **编译验证**：embed_build(action="compile")""",
    },
    {
        "name": "misra-check",
        "description": "MISRA C 合规性检查 — 静态分析 + 偏差报告",
        "trigger_patterns": ["misra", "合规", "静态分析", "代码检查", "lint"],
        "icon": "🛡️",
        "prompt": """## MISRA 合规检查指导

按以下步骤执行 MISRA C 检查：

1. **调用 EmbedGuard**：embed_lint(files=["src/"]) 进行 AST 级分析
2. **检查规则**（15 条 EMB 规则）：
   - EMB001: 禁止 malloc/free（用静态分配）
   - EMB002: 禁止递归（栈溢出风险）
   - EMB003: 限制浮点数使用（性能影响）
   - EMB004: ISR 中禁止阻塞操作
   - EMB005: 寄存器访问必须用 volatile
   - EMB006: 禁止 goto
   - EMB007: 禁止魔术数字（用 #define）
   - EMB008: 禁止 stdio.h（printf 等）
   - EMB009: 限制大数组（RAM 占用）
   - EMB010: ISR 中禁止动态内存
   - EMB011: ISR 中禁止浮点运算
   - EMB012: 禁止忙等待（用定时器）
   - EMB013: 限制除法运算（性能影响）
   - EMB014: 禁止未初始化变量
   - EMB015: 禁止 setjmp/longjmp
3. **生成报告**：用 chat() 汇总违规项和修复建议
4. **自动修复**：对 EMB001/005/014 可用 EmbedGuard autofixer""",
    },
    {
        "name": "power-optimize",
        "description": "低功耗优化 — 休眠模式、时钟门控、外设电源管理",
        "trigger_patterns": ["低功耗", "省电", "休眠", "功耗", "power", "sleep", "standby"],
        "icon": "🔋",
        "prompt": """## 低功耗优化指导

按以下步骤优化功耗：

1. **读取代码**：read_file 读取主循环和外设初始化代码
2. **分析功耗热点**：
   - 主循环是否有忙等待 → 改为中断驱动 + WFI
   - 未使用的外设时钟是否关闭 → __HAL_RCC_xxx_CLK_DISABLE
   - GPIO 是否配置为模拟输入 → 降低漏电流
   - 是否使用低功耗定时器 → LPTIM/LPTIM1
3. **休眠模式选择**：
   - Sleep：CPU 停止，外设运行（最浅）
   - Stop：所有时钟停止，SRAM 保持（中等）
   - Standby：SRAM 丢失，仅备份寄存器保持（最深）
4. **优化建议**：用 chat() 给出具体代码修改方案
5. **修改代码**：用 edit_file 精确修改
6. **编译验证**：embed_build(action="compile")""",
    },
    {
        "name": "debug-helper",
        "description": "调试助手 — OpenOCD 连接、断点、内存读取、寄存器查看",
        "trigger_patterns": ["调试", "断点", "读内存", "寄存器", "debug", "openocd"],
        "icon": "🐛",
        "prompt": """## 调试助手指导

按以下步骤协助调试：

1. **确认调试器**：用 ask_user 询问使用的调试器（ST-Link/J-Link/CMSIS-DAP）
2. **生成调试配置**：
   - OpenOCD 配置文件（interface + target）
   - GDB 启动脚本
3. **常用调试命令**（通过 run_command 执行）：
   - arm-none-eabi-gdb 连接：target remote localhost:3333
   - 设置断点：break main.c:42
   - 读寄存器：info registers
   - 读内存：x/16xw 0x20000000
   - 单步：step / next
4. **HardFault 分析**：读取 fault 寄存器（CFSR/HFSR/MMFAR/BFAR）
5. **变量监视**：watch/print 变量值
6. **调试建议**：用 chat() 说明调试步骤和预期结果""",
    },
]


# 所有内置技能（从配置表生成 + 可执行 Skill 替换）
# v2.8: mcu-init/driver-gen/bug-hunt/misra-check 升级为 ExecutableSkill
def _build_builtin_skills() -> list[BaseSkill]:
    """构建内置 Skill 列表：按 _BUILTIN_SKILL_CONFIGS 顺序，可执行 Skill 替换对应 PromptSkill"""
    from iron.skills.executable import EXECUTABLE_SKILLS
    # 可执行 Skill 的名称 → 实例映射
    executable_map = {s.name: s for s in EXECUTABLE_SKILLS}
    # 按 _BUILTIN_SKILL_CONFIGS 顺序构建，保持原有顺序
    skills = []
    for cfg in _BUILTIN_SKILL_CONFIGS:
        if cfg["name"] in executable_map:
            # 用 ExecutableSkill 替换
            skills.append(executable_map[cfg["name"]])
        else:
            # 保持 PromptSkill
            skills.append(PromptSkill(
                name=cfg["name"],
                description=cfg["description"],
                trigger_patterns=cfg["trigger_patterns"],
                icon=cfg["icon"],
                prompt=cfg["prompt"],
            ))
    return skills


BUILTIN_SKILLS: list[BaseSkill] = _build_builtin_skills()


class SkillRegistry:
    """技能注册与匹配"""

    def __init__(self, custom_skills: list[BaseSkill] = None):
        self.skills = list(BUILTIN_SKILLS)
        if custom_skills:
            self.skills.extend(custom_skills)

    def load_from_dir(self, directory: str | Path):
        """从目录加载用户自定义 skill（.md 文件，YAML frontmatter 格式）

        文件格式参考 Claude Code skill：
            ---
            name: my-skill
            description: 技能描述
            trigger_patterns:
              - 关键词1
              - 关键词2
            icon: 🎯
            ---
            技能的 prompt 内容...
        """
        dir_path = Path(directory)
        if not dir_path.exists():
            return

        for md_file in sorted(dir_path.glob("*.md")):
            try:
                skill = self._parse_skill_md(md_file)
                if skill:
                    # 移除同名内置 skill
                    self.skills = [s for s in self.skills if s.name != skill.name]
                    self.skills.append(skill)
            except (OSError, UnicodeDecodeError, ValueError) as e:
                logging.warning("解析 skill %s 失败: %s", md_file, e)

    def _parse_skill_md(self, md_file: Path) -> BaseSkill | None:
        """解析 skill markdown 文件"""
        content = md_file.read_text(encoding="utf-8")
        if not content.startswith("---"):
            return None

        # 提取 YAML frontmatter
        parts = content.split("---", 2)
        if len(parts) < 3:
            return None

        yaml_block = parts[1].strip()
        prompt_body = parts[2].strip()

        # 优先使用 pyyaml（更健壮），不可用时回退到手写解析器
        if _HAS_YAML:
            try:
                data = _yaml.safe_load(yaml_block) or {}
            except _yaml.YAMLError:
                data = None
            if isinstance(data, dict):
                meta = {
                    "name": data.get("name", ""),
                    "description": data.get("description", ""),
                    "trigger_patterns": list(data.get("trigger_patterns") or []),
                    "icon": data.get("icon", "📋"),
                }
            else:
                # pyyaml 解析结果非 dict，回退到手写解析
                meta = self._parse_yaml_fallback(yaml_block)
        else:
            meta = self._parse_yaml_fallback(yaml_block)

        if not meta.get("name"):
            return None

        return PromptSkill(
            name=meta["name"],
            description=meta.get("description", ""),
            trigger_patterns=meta.get("trigger_patterns", []),
            icon=meta.get("icon", "📋"),
            prompt=prompt_body,
        )

    @staticmethod
    def _parse_yaml_fallback(yaml_block: str) -> dict:
        """手写 YAML 解析（不依赖 pyyaml 时的回退方案）"""
        meta = {"trigger_patterns": [], "icon": "📋"}
        in_triggers = False
        for line in yaml_block.split("\n"):
            line = line.strip()
            if line.startswith("name:"):
                meta["name"] = line[5:].strip()
                in_triggers = False
            elif line.startswith("description:"):
                meta["description"] = line[12:].strip()
                in_triggers = False
            elif line.startswith("icon:"):
                meta["icon"] = line[5:].strip()
                in_triggers = False
            elif line.startswith("trigger_patterns:"):
                in_triggers = True
            elif line.startswith("- ") and in_triggers:
                meta["trigger_patterns"].append(line[2:].strip())
            elif line and not line.startswith("-") and not line.startswith(" "):
                in_triggers = False
        return meta

    def match(self, user_input: str) -> list[BaseSkill]:
        """匹配用户输入，返回按分数排序的技能列表"""
        matched = []
        for skill in self.skills:
            score = skill.match_score(user_input)
            if score > MATCH_THRESHOLD:
                matched.append((score, skill))
        matched.sort(key=lambda x: x[0], reverse=True)
        return [s for _, s in matched]

    def list_all(self) -> list[BaseSkill]:
        """列出所有可用技能"""
        return self.skills

    def get_by_name(self, name: str) -> BaseSkill | None:
        """按名称查找技能"""
        for skill in self.skills:
            if skill.name == name:
                return skill
        return None


# 向后兼容：FileSkill 改为 PromptSkill 的别名（v2.4 统一为 PromptSkill）
FileSkill = PromptSkill
