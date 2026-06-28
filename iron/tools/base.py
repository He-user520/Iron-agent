"""工具基类 — 所有工具继承此类（P4-3: 增加结果截断保护）"""
import asyncio
import logging
from abc import ABC, abstractmethod

# 默认截断阈值（字符数）
DEFAULT_MAX_OUTPUT_CHARS = 10000

logger = logging.getLogger(__name__)


class BaseTool(ABC):
    """工具基类

    每个工具必须定义：
    - name: 工具名称
    - schema: OpenAI function calling 格式的 schema
    - execute(): 执行逻辑，返回结果 dict

    P4-3: 通过 safe_execute() 包装 execute()，自动截断超大输出，
    避免上下文 token 浪费。截断阈值通过 max_output_chars 实例属性配置，
    默认 DEFAULT_MAX_OUTPUT_CHARS=10000。子类未调用 super().__init__()
    时通过 getattr fallback 到默认值，保持向后兼容。
    """

    def __init__(self, max_output_chars: int = None):
        # 截断阈值：None 时用默认值
        self.max_output_chars = max_output_chars or DEFAULT_MAX_OUTPUT_CHARS

    @property
    @abstractmethod
    def name(self) -> str:
        pass

    @property
    @abstractmethod
    def schema(self) -> dict:
        """OpenAI function calling 格式"""
        pass

    @abstractmethod
    async def execute(self, args: dict, context: dict) -> dict:
        """执行工具

        Args:
            args: 工具参数（来自 AI 的 tool_call）
            context: 上下文信息（engine, project_dir 等）

        Returns:
            结果 dict，至少包含 {"success": bool, ...}
        """
        pass

    async def safe_execute(self, args: dict, context: dict) -> dict:
        """安全执行 — 包装 execute，添加截断保护和异常处理

        - 捕获工具内部异常，返回统一错误格式（不崩溃整个会话）
        - 截断超大输出字段，避免上下文 token 浪费
        - CancelledError 正常传播（不视为错误）
        """
        try:
            result = await self.execute(args, context)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"工具 {getattr(self, 'name', '?')} 执行异常: {e}", exc_info=True)
            return {
                "success": False,
                "error": f"{type(e).__name__}: {e}",
                "truncated": False,
            }
        # 工具可能返回 None，做防御性检查
        if result is None:
            result = {"success": False, "error": f"工具 {getattr(self, 'name', '?')} 返回空结果"}
        return self._truncate_result(result)

    def _truncate_result(self, result: dict) -> dict:
        """截断结果中的大字段，避免上下文 token 浪费

        - 字符串字段超阈值：截断并追加截断提示
        - 列表字段总大小超阈值：保留前 N 个元素并标记
        - 截断后追加 truncated/truncated_fields/message 元数据
        """
        if not isinstance(result, dict):
            return result

        # 子类未调用 super().__init__() 时用 getattr fallback 到默认值
        max_chars = getattr(self, "max_output_chars", None) or DEFAULT_MAX_OUTPUT_CHARS
        modified = False
        truncated_fields = []

        for key in ("stdout", "stderr", "output", "content", "result", "matches", "lines"):
            val = result.get(key)
            if isinstance(val, str) and len(val) > max_chars:
                result[key] = val[:max_chars] + (
                    f"\n...[截断: 原始 {len(val)} 字符，已截断 {len(val) - max_chars} 字符]"
                )
                modified = True
                truncated_fields.append(key)
            elif isinstance(val, list):
                # 列表类型：检查总大小
                total = sum(len(str(item)) for item in val)
                if total > max_chars:
                    kept = []
                    current_size = 0
                    for item in val:
                        item_str = str(item)
                        if current_size + len(item_str) > max_chars:
                            break
                        kept.append(item)
                        current_size += len(item_str)
                    result[key] = kept
                    result[f"{key}_truncated"] = True
                    result[f"{key}_original_count"] = len(val)
                    result[f"{key}_kept_count"] = len(kept)
                    modified = True
                    truncated_fields.append(key)

        if modified:
            result["truncated"] = True
            result["truncated_fields"] = truncated_fields
            result["message"] = (
                f"输出被截断（阈值 {max_chars} 字符）。如需完整结果，请用更具体的查询或参数。"
            )

        return result
