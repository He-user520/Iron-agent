"""Layer 3: 项目级规则加载器 — 从 .iron-agent/rules/ 读取"""
from pathlib import Path
import re


# ── MCU 数据库（常见 MCU 规格）─────────────────────────────────
# 用于 create_default_rules 生成 target-mcu.md，避免硬编码 STM32F407
_MCU_PROFILES = {
    "stm32f407": {
        "arch": "ARM Cortex-M4F", "freq": "168 MHz", "flash": "1 MB",
        "ram": "192 KB", "fpu": "是",
        "flash_addr": "0x08000000", "ram_addr": "0x20000000",
    },
    "stm32f103": {
        "arch": "ARM Cortex-M3", "freq": "72 MHz", "flash": "64 KB - 512 KB",
        "ram": "20 KB - 64 KB", "fpu": "否",
        "flash_addr": "0x08000000", "ram_addr": "0x20000000",
    },
    "esp32": {
        "arch": "Xtensa LX6", "freq": "240 MHz", "flash": "4 MB",
        "ram": "520 KB", "fpu": "是（单精度）",
        "flash_addr": "0x3F000000", "ram_addr": "0x3FFAE000",
    },
    "esp32s3": {
        "arch": "Xtensa LX7", "freq": "240 MHz", "flash": "4 MB - 8 MB",
        "ram": "512 KB", "fpu": "是（单精度）",
        "flash_addr": "0x3F000000", "ram_addr": "0x3FC88000",
    },
    "arduino": {
        "arch": "AVR", "freq": "16 MHz", "flash": "32 KB",
        "ram": "2 KB", "fpu": "否",
        "flash_addr": "0x0000", "ram_addr": "0x0100",
    },
}


class ProjectRulesLoader:
    """从项目目录加载自定义规则文件"""

    def __init__(self, project_root: Path):
        self.project_root = project_root
        self.rules_dir = project_root / ".iron-agent" / "rules"
        self.instructions_file = project_root / ".iron-agent" / "instructions.md"

    def has_rules(self) -> bool:
        return self.rules_dir.exists() and any(self.rules_dir.glob("*.md"))

    def load_all(self) -> str:
        """加载所有项目规则，返回拼接后的 prompt 文本"""
        parts = []

        # 加载顶层指令
        if self.instructions_file.exists():
            content = self.instructions_file.read_text(encoding="utf-8").strip()
            if content:
                parts.append(f"# 项目指令\n\n{content}")

        # 加载规则文件（按文件名排序）
        if self.rules_dir.exists():
            for rule_file in sorted(self.rules_dir.glob("*.md")):
                content = rule_file.read_text(encoding="utf-8").strip()
                if content:
                    # 不使用 .title()：对中文（及中英混排）会产生意外的大小写转换
                    name = rule_file.stem.replace("-", " ").replace("_", " ")
                    parts.append(f"# 项目规则: {name}\n\n{content}")

        return "\n\n---\n\n".join(parts) if parts else ""

    def load_mcu_info(self) -> str:
        """加载 MCU 目标信息（从 target-mcu.md）"""
        mcu_file = self.rules_dir / "target-mcu.md"
        if mcu_file.exists():
            return mcu_file.read_text(encoding="utf-8").strip()
        return ""

    def count_rules(self) -> int:
        """计算项目规则文件数量"""
        if not self.rules_dir.exists():
            return 0
        return len(list(self.rules_dir.glob("*.md")))


def create_default_rules(project_root: Path, mcu: str = "stm32f407"):
    """在项目中创建默认的 .iron-agent/rules/ 目录和模板文件"""
    rules_dir = project_root / ".iron-agent" / "rules"
    rules_dir.mkdir(parents=True, exist_ok=True)

    # 从 MCU 数据库获取规格（未命中则回退到 stm32f407）
    profile = _MCU_PROFILES.get(mcu.lower(), _MCU_PROFILES["stm32f407"])

    # target-mcu.md
    mcu_file = rules_dir / "target-mcu.md"
    if not mcu_file.exists():
        mcu_file.write_text(
            f"# 目标 MCU\n\n"
            f"- 型号: {mcu.upper()}\n"
            f"- 架构: {profile['arch']}\n"
            f"- FPU: {profile['fpu']}\n"
            f"- 最高频率: {profile['freq']}\n"
            f"- Flash: {profile['flash']}\n"
            f"- RAM: {profile['ram']}\n\n"
            f"## 内存布局\n\n"
            f"| 区域 | 起始地址 | 大小 |\n"
            f"|------|---------|------|\n"
            f"| Flash | {profile['flash_addr']} | {profile['flash']} |\n"
            f"| SRAM | {profile['ram_addr']} | {profile['ram']} |\n",
            encoding="utf-8",
        )

    # coding-standards.md
    std_file = rules_dir / "coding-standards.md"
    if not std_file.exists():
        std_file.write_text(
            "# 编码规范\n\n"
            "## 命名规范\n\n"
            "- 变量命名: snake_case\n"
            "- 宏定义: UPPER_SNAKE_CASE\n"
            "- 函数名: module_action() 格式（如 uart_send_data）\n"
            "- 全局变量: g_ 前缀\n"
            "- 静态变量: s_ 前缀\n"
            "- 指针变量: p_ 前缀\n"
            "- 布尔变量: is_/has_/can_ 前缀\n"
            "- 中断处理函数: xxx_IRQHandler 格式\n\n"
            "## HAL 库使用规范\n\n"
            "- 优先使用 HAL 库函数，避免直接寄存器操作\n"
            "- 所有寄存器指针必须用 volatile 关键字\n"
            "- 外设句柄: hxxx 格式（如 huart1, hspi1）\n"
            "- HAL 初始化: HAL_xxx_Init() / HAL_xxx_DeInit()\n"
            "- 错误处理: 检查 HAL 返回值，HAL_OK 才继续\n\n"
            "## 内存安全规范\n\n"
            "- 禁止 malloc/free（用静态分配）\n"
            "- 禁止递归（栈溢出风险）\n"
            "- 禁止 goto 语句\n"
            "- 大数组用 static 避免栈分配\n"
            "- 栈大小: 最小 128 字，含浮点运算的 256 字\n\n"
            "## 中断安全规范\n\n"
            "- ISR 中禁止阻塞操作（不能有 while 等待）\n"
            "- ISR 中禁止动态内存分配\n"
            "- ISR 中禁止浮点运算（除非 FPU 配置正确）\n"
            "- ISR 尽量短小，只做标志位设置\n"
            "- 共享变量必须用 volatile 声明\n\n"
            "## 代码风格\n\n"
            "- 注释语言: 中文\n"
            "- 缩进: 4 空格\n"
            "- 大括号: K&R 风格\n"
            "- 每个函数必须有 doxygen 注释\n"
            "- 魔术数字必须用 #define 定义\n",
            encoding="utf-8",
        )

    # instructions.md (顶层指令)
    inst_file = project_root / ".iron-agent" / "instructions.md"
    if not inst_file.exists():
        inst_file.write_text(
            "# 项目指令\n\n"
            "## 项目概述\n\n"
            "这是一个嵌入式 C 项目，使用 HAL 库开发。\n"
            "所有代码必须通过 EmbedGuard 静态分析检查。\n\n"
            "## 开发要求\n\n"
            "- 使用 HAL 库，禁止直接寄存器操作（除性能关键路径）\n"
            "- 所有代码必须通过 MISRA C 检查\n"
            "- 编译用 embed_build，烧录用 embed_flash，分析用 embed_lint\n"
            "- 修改文件优先用 edit_file，新建文件用 write_file\n"
            "- 复杂任务先用 task_track 创建任务列表\n\n"
            "## 构建系统\n\n"
            "- 默认使用 PlatformIO（支持 1500+ 板卡）\n"
            "- 配置文件: platformio.ini\n"
            "- 源码目录: src/\n"
            "- 头文件目录: include/\n"
            "- 库目录: lib/\n\n"
            "## 安全铁律\n\n"
            "- 禁止 malloc/free（用静态分配）\n"
            "- 禁止递归（栈溢出风险）\n"
            "- 禁止 goto\n"
            "- 寄存器访问必须用 volatile\n"
            "- ISR 中禁止阻塞操作\n",
            encoding="utf-8",
        )

    return rules_dir
