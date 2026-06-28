"""Layer 2: AI 常犯的嵌入式代码错误 — 从 EmbedGuard 规则转换"""

AI_ANTIPATTERNS = [
    {
        "id": "EMB001",
        "name": "用静态缓冲区替代 malloc",
        "severity": "error",
        "prompt": "AI 经常在嵌入式代码中使用 malloc。必须用静态数组或预分配缓冲区替代。",
        "bad": "uint8_t *buf = (uint8_t *)malloc(size);",
        "good": "static uint8_t s_buf[MAX_SIZE];",
        "misra": "MISRA C:2012 Rule 21.3",
    },
    {
        "id": "EMB004",
        "name": "ISR 中禁止阻塞调用",
        "severity": "error",
        "prompt": "AI 经常在 ISR 中调用 HAL_Delay() 或 printf()。这些函数会阻塞，导致系统挂起。",
        "bad": "void EXTI0_IRQHandler(void) { HAL_Delay(100); HAL_GPIO_TogglePin(...); }",
        "good": "volatile uint8_t btn_pressed = 0;\nvoid EXTI0_IRQHandler(void) { btn_pressed = 1; }",
    },
    {
        "id": "EMB005",
        "name": "寄存器访问必须 volatile",
        "severity": "warning",
        "prompt": "AI 经常忘记对硬件寄存器地址使用 volatile 限定符，导致编译器优化掉关键的读写操作。",
        "bad": "uint32_t *gpio_odr = (uint32_t *)0x40020014;",
        "good": "volatile uint32_t *gpio_odr = (volatile uint32_t *)0x40020014;",
    },
    {
        "id": "EMB007",
        "name": "禁止魔术数字",
        "severity": "info",
        "prompt": "AI 经常在代码中直接使用数字常量（魔术数字）。所有数字应提取为有意义的宏定义。",
        "bad": "GPIOA->MODER |= (1 << 10);",
        "good": "#define LED_PIN 5\nGPIOA->MODER |= (1U << (LED_PIN * 2));",
    },
    {
        "id": "EMB008",
        "name": "禁止标准库 I/O 函数",
        "severity": "warning",
        "prompt": "AI 经常在嵌入式代码中使用 printf/scanf。这些函数占用大量 Flash 和栈空间。",
        "bad": "printf(\"Hello World\\n\");",
        "good": "uart_send_string(\"Hello World\\r\\n\");",
        "misra": "MISRA C:2012 Rule 21.6",
    },
    {
        "id": "EMB009",
        "name": "栈上大数组",
        "severity": "warning",
        "prompt": "AI 经常在函数内声明大数组（>256 字节），可能导致栈溢出。大缓冲区应为 static。",
        "bad": "void process(void) { uint8_t buffer[1024]; }",
        "good": "void process(void) { static uint8_t buffer[1024]; }",
    },
    {
        "id": "EMB010",
        "name": "ISR 共享变量需 volatile",
        "severity": "warning",
        "prompt": "AI 经常忘记将 ISR 与主循环共享的变量声明为 volatile，导致编译器缓存旧值。",
        "bad": "uint8_t rx_flag; // ISR 中置 1，主循环中检查",
        "good": "volatile uint8_t rx_flag;",
    },
]


def get_antipatterns_prompt() -> str:
    """将 AI 反模式转为 system prompt 文本"""
    lines = ["# AI 常犯的嵌入式代码错误（生成代码时必须避免）\n"]
    for ap in AI_ANTIPATTERNS:
        lines.append(f"## {ap['id']}: {ap['name']} [{ap['severity']}]")
        lines.append(f"{ap['prompt']}")
        lines.append(f"❌ 错误: `{ap['bad']}`")
        lines.append(f"✅ 正确: `{ap['good']}`")
        if ap.get("misra"):
            lines.append(f"参考: {ap['misra']}")
        lines.append("")
    return "\n".join(lines)
