"""Layer 1: 嵌入式铁律 — 不可关闭的硬编码规则"""

IRON_RULES = [
    {
        "id": "IRON001",
        "name": "禁止动态内存分配",
        "rule": "禁止使用 malloc/free/calloc/realloc。所有缓冲区必须是静态分配或栈分配（栈数组不超过 256 字节）。",
        "bad": "uint8_t *buf = malloc(1024);",
        "good": "static uint8_t s_buf[1024];",
    },
    {
        "id": "IRON002",
        "name": "禁止递归调用",
        "rule": "嵌入式系统栈空间有限，禁止任何形式的递归调用。必须用迭代替代。",
        "bad": "void traverse(TreeNode *node) { if(node->left) traverse(node->left); }",
        "good": "void traverse(TreeNode *root) { stack[0] = root; while(top > 0) { node = stack[--top]; /* ... */ } }",
    },
    {
        "id": "IRON003",
        "name": "MMIO 必须 volatile",
        "rule": "所有内存映射 I/O 寄存器访问必须通过 volatile 指针。直接地址访问必须使用 volatile 限定符。",
        "bad": "uint32_t *reg = (uint32_t *)0x40020000;",
        "good": "volatile uint32_t *reg = (volatile uint32_t *)0x40020000;",
    },
    {
        "id": "IRON004",
        "name": "ISR 禁止阻塞操作",
        "rule": "中断服务程序(ISR)中禁止调用任何可能阻塞的函数：printf/scanf/HAL_Delay/sleep/malloc/互斥锁获取。ISR 应尽可能短小。",
        "bad": "void USART1_IRQHandler(void) { HAL_Delay(10); printf(\"received\\n\"); }",
        "good": "volatile uint8_t flag = 0;\nvoid USART1_IRQHandler(void) { flag = 1; }",
    },
    {
        "id": "IRON005",
        "name": "优先位操作",
        "rule": "对寄存器的操作优先使用位操作（&, |, ^, ~, <<, >>），避免使用乘除法。",
        "bad": "val = val * 2;",
        "good": "val = val << 1;",
    },
    {
        "id": "IRON006",
        "name": "避免浮点运算",
        "rule": "除非目标 MCU 确认有 FPU 且已启用，否则禁止使用浮点运算。用定点数替代。",
        "bad": "float temp = 3.14 * raw_adc * 3.3 / 4096;",
        "good": "int32_t temp_x100 = (int32_t)raw_adc * 330 / 4096; // 0.01°C 精度",
    },
    {
        "id": "IRON007",
        "name": "数组必须有边界",
        "rule": "所有数组必须有编译期已知的边界。访问前必须检查索引。禁止柔性数组成员。",
        "bad": "int data[n]; // VLA",
        "good": "#define MAX_DATA 64\nint data[MAX_DATA];",
    },
    {
        "id": "IRON008",
        "name": "返回值必须检查",
        "rule": "所有返回错误码的函数，其返回值必须被检查。如果故意忽略，必须用 (void) 显式标注。",
        "bad": "snprintf(buf, sizeof(buf), \"hello\");",
        "good": "(void)snprintf(buf, sizeof(buf), \"hello\");",
    },
    {
        "id": "IRON009",
        "name": "共享变量需临界区",
        "rule": "ISR 与主循环共享的变量必须声明为 volatile，并在主循环端使用临界区保护（关中断/互斥锁）。",
        "bad": "uint32_t counter; // ISR 中自增，主循环中读取",
        "good": "volatile uint32_t counter;\n// 读取时:\n__disable_irq();\nuint32_t val = counter;\n__enable_irq();",
    },
    {
        "id": "IRON010",
        "name": "禁止 goto/setjmp",
        "rule": "禁止使用 goto 语句、setjmp/longjmp。错误处理使用返回码或状态机。",
        "bad": "goto error_cleanup;",
        "good": "return ERR_TIMEOUT;",
    },
    {
        "id": "IRON011",
        "name": "禁止标准库 I/O",
        "rule": "嵌入式代码中禁止使用 printf/scanf/fprintf 等标准库 I/O 函数。使用项目自定义的日志宏或直接操作寄存器。",
        "bad": "printf(\"ADC value: %d\\n\", adc_val);",
        "good": "LOG_INFO(\"ADC value: %d\", adc_val);",
    },
]


def get_iron_rules_prompt() -> str:
    """将铁律转为 system prompt 文本"""
    lines = ["# 嵌入式编码铁律（必须严格遵守）\n"]
    for r in IRON_RULES:
        lines.append(f"## {r['id']}: {r['name']}")
        lines.append(f"规则: {r['rule']}")
        lines.append(f"❌ 错误: `{r['bad']}`")
        lines.append(f"✅ 正确: `{r['good']}`\n")
    return "\n".join(lines)
