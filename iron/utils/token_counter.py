"""Token 计数工具 — 优先 tiktoken 精确计数，不可用时 fallback 到字符估算

P4 修复（第七轮）：Claude Code 风格的精确 token 计数。
- 输入 token：system + messages + tools schema 全部编码后计数
- 输出 token：流式输出时每个 delta 编码后累加
- 没装 tiktoken 时：字符数 / 4 粗略估算（英文较准，中文偏保守）
"""
import logging

logger = logging.getLogger(__name__)

# 懒加载 tiktoken，避免强依赖
_tiktoken_encoding = None
_tiktoken_available = None  # True / False / None（未检测）

# 默认模型编码：cl100k_base 是 GPT-4 / GPT-3.5 / DeepSeek / Qwen 等的通用编码
_DEFAULT_ENCODING = "cl100k_base"


def _get_encoding():
    """懒加载 tiktoken 编码器，失败返回 None"""
    global _tiktoken_encoding, _tiktoken_available
    if _tiktoken_available is not None:
        return _tiktoken_encoding
    try:
        import tiktoken
        _tiktoken_encoding = tiktoken.get_encoding(_DEFAULT_ENCODING)
        _tiktoken_available = True
        logger.info(f"已加载 tiktoken 编码: {_DEFAULT_ENCODING}")
        return _tiktoken_encoding
    except ImportError:
        _tiktoken_available = False
        logger.info("tiktoken 未安装，使用字符估算 token 数")
        return None
    except (OSError, ValueError) as e:
        _tiktoken_available = False
        logger.warning(f"tiktoken 加载失败，使用字符估算: {e}")
        return None


def count_tokens(text: str) -> int:
    """计算文本的 token 数

    优先用 tiktoken 精确计数，不可用时 fallback 到字符数 / 4。
    """
    if not text:
        return 0
    enc = _get_encoding()
    if enc is not None:
        try:
            return len(enc.encode(text))
        except (ValueError, TypeError):
            pass
    # fallback: 字符数 / 4（粗略估算，英文约 4 字符 = 1 token，中文约 2-3 字符 = 1 token）
    return max(1, len(text) // 4)


def count_messages_tokens(system: str, messages: list[dict], tools: list[dict] | None = None) -> int:
    """计算一次请求的总输入 token 数（system + messages + tools）

    参考 OpenAI 的 token 计费方式：
    - system 内容算
    - 每条 message 的 role + content + name 都算
    - tools schema 也算（模型需要理解工具定义）
    """
    total = count_tokens(system)

    for msg in messages:
        # 每条消息有固定的格式开销（粗略加 4 tokens 模拟）
        total += 4
        role = msg.get("role", "")
        content = msg.get("content", "")
        if role:
            total += count_tokens(role)
        if isinstance(content, str):
            total += count_tokens(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    if "text" in part:
                        total += count_tokens(part["text"])
        # tool_calls 也算（如果有的话）
        tool_calls = msg.get("tool_calls")
        if tool_calls and isinstance(tool_calls, list):
            for tc in tool_calls:
                if isinstance(tc, dict):
                    fn = tc.get("function", {})
                    if isinstance(fn, dict):
                        total += count_tokens(fn.get("name", ""))
                        total += count_tokens(fn.get("arguments", ""))

    # tools schema 的 token 开销（粗略估算）
    if tools:
        import json as _json
        total += count_tokens(_json.dumps(tools, ensure_ascii=False))

    return total


def is_tiktoken_available() -> bool:
    """返回 tiktoken 是否可用（用于 UI 标注"估算"还是"精确"）"""
    if _tiktoken_available is None:
        _get_encoding()  # 触发检测
    return bool(_tiktoken_available)
