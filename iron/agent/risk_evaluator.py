"""命令风险评估 — 从 engine.py 抽出的纯函数模块（P1-3 Phase 1）

职责：
- 评估 shell 命令的风险等级（safe / dangerous）
- 维护安全命令前缀和危险关键词常量

设计原则：
- 纯函数，不依赖实例状态，便于单元测试
- engine.py 保留 _evaluate_command_risk 薄包装方法以维持向后兼容
"""
import re
from pathlib import Path


# 安全的构建/运行命令前缀（自动允许）
SAFE_COMMANDS = {
    # 编译工具链
    "gcc", "g++", "cc", "c++", "cl",
    "arm-none-eabi-gcc", "arm-none-eabi-g++", "arm-none-eabi-objcopy",
    "arm-none-eabi-size", "arm-none-eabi-nm",
    "xtensa-esp32-elf-gcc",
    "make", "cmake", "ninja", "nmake",
    # PlatformIO
    "pio", "platformio", "python -m platformio", "python3 -m platformio",
    # Cargo/Go/Rust
    "cargo", "cargo build", "cargo run", "cargo test", "cargo flash",
    "go build", "go run", "go test",
    # Python/Node（不含 pip install）
    "python", "python3", "node", "npm run", "npm test", "npm start",
    "dotnet build", "dotnet run",
    "javac", "java",
    # 运行程序
    "./main", ".\\main", ".\\build", "./build",
    # 环境检测
    "where", "which",
    # 信息查询
    "pip show", "pip list", "pip --version",
    "pip3 show", "pip3 list", "pip3 --version",
    "python --version", "python3 --version", "node --version",
    "pio --version", "platformio --version",
    "gcc --version", "arm-none-eabi-gcc --version",
    "git status", "git log", "git diff", "git branch",
    "git show", "git tag", "git remote -v",
    # 目录操作
    "dir", "ls", "tree", "pwd", "cd",
    # 移除 cat/head/tail/more/findstr/grep/type — 这些命令可读任意文件绕过 path_guard，
    # 统一改用 read_file 工具走边界校验
    # 移除 set/env — 无参数运行会打印全部环境变量泄漏密钥
    "echo", "path",
    # 编译产物分析
    "size ", "objdump", "nm ", "readelf",
}

# 危险命令关键词（必须授权）
DANGEROUS_KEYWORDS = {
    "rm ", "rm\t", "del ", "rmdir",
    "sudo", "su ",
    "git push", "git remote",
    "pip install", "pip uninstall", "pip3 install",
    "npm install", "npm uninstall", "yarn add",
    "curl", "wget", "Invoke-WebRequest",
    "chmod", "chown", "chgrp",
    "mkfs", "fdisk", "mount",
    "shutdown", "reboot", "restart",
    "format ",
    "> /dev/", ">\\\\.\\",
    "registry", "reg ",
}

# 命令注入危险元字符（子shell、反引号、重定向、后台执行、换行、空字节、%变量）
# 注意：& 检测需排除合法的 && 逻辑与，只拦截单独的 & 后台执行
_CMD_METACHARS = ["$", "`", ">", "<", "\n", "\r", "\x00", "%"]
_BG_PROCESS_RE = re.compile(r'(?<!&)&(?!&)')

# 复合命令分隔符：&&、||、|、;、换行符
_COMPOUND_SPLIT_RE = re.compile(r'\s*(?:&&|\|\||[|;\n\r])\s*')

# python -c / node -e 可执行任意代码，强制 dangerous
_PYTHON_CODE_RE = re.compile(r'^(?:python[3]?|py)\b')
_PYTHON_CODE_FLAG_RE = re.compile(r'(^|\s)-c')
_NODE_CODE_RE = re.compile(r'^node\b')
_NODE_CODE_FLAG_RE = re.compile(r'(^|\s)-e')

# 单词型危险关键词（字母开头）用词边界，非单词型（含符号）用子串
_WORD_KEYWORD_RE = re.compile(r'^\w')

# 带引号的可执行路径提取：("C:\Program Files\...\python.exe" -m platformio run)
_QUOTED_EXE_RE = re.compile(r'^"([^"]+)"\s*(.*)')


def evaluate_command_risk(command: str,
                          safe_commands: set = None,
                          dangerous_keywords: set = None) -> str:
    """评估命令风险等级（纯函数，不依赖实例状态）

    支持复合命令（&&、||、|、;）拆分检查：
    - 全部子命令安全 → safe
    - 任一子命令危险 → dangerous
    - 有未知子命令 → dangerous

    Args:
        command: 待评估的 shell 命令字符串
        safe_commands: 安全命令前缀集合（默认用本模块 SAFE_COMMANDS）
        dangerous_keywords: 危险关键词集合（默认用本模块 DANGEROUS_KEYWORDS）

    Returns:
        "safe" — 自动允许
        "dangerous" — 需要用户授权
    """
    if safe_commands is None:
        safe_commands = SAFE_COMMANDS
    if dangerous_keywords is None:
        dangerous_keywords = DANGEROUS_KEYWORDS

    # 1. 命令注入防护 — 检测危险元字符
    for pattern in _CMD_METACHARS:
        if pattern in command:
            return "dangerous"
    if _BG_PROCESS_RE.search(command):
        return "dangerous"

    # 2. 拆分复合命令
    sub_cmds = _COMPOUND_SPLIT_RE.split(command.strip())
    sub_cmds = [c.strip() for c in sub_cmds if c.strip()]

    if not sub_cmds:
        return "dangerous"

    # 3. 逐个检查子命令
    for sub in sub_cmds:
        sub_lower = sub.lower()

        # 3a. python -c / node -e 可执行任意代码
        if _PYTHON_CODE_RE.match(sub_lower) and (
            _PYTHON_CODE_FLAG_RE.search(sub_lower) or '--command' in sub_lower
        ):
            return "dangerous"
        if _NODE_CODE_RE.match(sub_lower) and (
            _NODE_CODE_FLAG_RE.search(sub_lower) or '--eval' in sub_lower
        ):
            return "dangerous"

        # 3b. 危险关键词检查
        is_dangerous = False
        for danger in dangerous_keywords:
            danger_stripped = danger.rstrip()
            if danger_stripped and _WORD_KEYWORD_RE.match(danger_stripped):
                # 单词型关键词（字母开头）用词边界
                if re.search(r'\b' + re.escape(danger_stripped) + r'\b', sub_lower):
                    is_dangerous = True
                    break
            else:
                # 非单词型（含符号前缀）用子串匹配
                if danger in sub_lower:
                    is_dangerous = True
                    break
        if is_dangerous:
            return "dangerous"

        # 3c. 安全前缀检查（处理带引号的路径）
        check_cmd = sub_lower
        m = _QUOTED_EXE_RE.match(sub)
        if m:
            exe_path = m.group(1)
            rest = m.group(2)
            exe_name = Path(exe_path).name.lower()
            check_cmd = f"{exe_name} {rest}" if rest else exe_name

        is_safe = False
        for safe in safe_commands:
            if check_cmd.startswith(safe):
                is_safe = True
                break
        if not is_safe:
            # 未知子命令 → 需要授权
            return "dangerous"

    return "safe"
