"""ToolSearchTool — 工具动态发现

参考 Claude Code 的 ToolSearchTool 设计：
- 用关键词匹配 + 描述相似度排序工具
- 提示词超过阈值时自动启用搜索模式
- LLM 先调用 tool_search 获取相关工具的 schema，再调用实际工具

嵌入式定制：
- 优先匹配嵌入式相关工具（read_file/edit_file/embed_build/embed_flash）
- 根据项目阶段（编码/构建/调试）推荐工具集
"""
import re
from dataclasses import dataclass

from iron.tools.base import BaseTool


# 搜索模式阈值：系统提示 + 工具 schema 超过此值时启用搜索模式
SEARCH_MODE_THRESHOLD = 20000


@dataclass
class ToolMatch:
    """工具匹配结果"""
    name: str
    score: float  # 0.0-1.0
    description: str
    schema: dict


class ToolSearchTool(BaseTool):
    """工具搜索工具 — 按查询返回相关工具及其 schema

    用法:
        search = ToolSearchTool(tool_source)
        result = await search.execute({"query": "读取文件", "limit": 5}, {})
        # 返回最相关的 5 个工具及其 schema

    tool_source 支持三种类型：
    - list[dict]: schema 列表（OpenAI function calling 格式）
    - ToolRegistry: 工具注册中心实例
    - dict[str, BaseTool]: 工具名到实例的映射
    """

    # 嵌入式关键词映射：正则模式 → 工具名列表
    # 只映射实际存在的工具（write_file/read_file 等 builtin + edit_file/embed_build 等注册工具）
    KEYWORD_MAP = {
        r"读取|读|read|查看|view|列出|list": ["read_file", "find_files", "search_code"],
        r"写入|写|创建|write|create|新建|保存": ["write_file", "edit_file"],
        r"编辑|修改|edit|modify|替换|replace|更新": ["edit_file", "write_file"],
        r"删除|delete|remove|rm|清理": ["run_command"],
        r"编译|构建|build|make|compile|生成": ["embed_build", "run_command"],
        r"烧录|flash|烧写|download|下载固件": ["embed_flash"],
        r"串口|监视|monitor|serial|uart|log|日志": ["run_command"],
        r"检查|分析|check|analyze|lint|静态|规范": ["embed_lint"],
        r"搜索|查找|find|grep|search|定位": ["search_code", "find_files"],
        r"文件|file|glob|路径": ["find_files", "read_file"],
        r"任务|task|todo|进度": ["task_track"],
        r"记忆|记住|remember|知识": ["remember"],
        r"网页|搜索网页|web|url|链接|文档": ["web_search"],
        r"技能|skill|创建技能": ["skill_create"],
        r"mcp|服务器|外部工具": ["mcp_config"],
        r"提问|确认|ask|询问|用户": ["ask_user"],
        r"命令|shell|执行命令|cmd|终端": ["run_command"],
        r"聊天|回复|chat|回答": ["chat"],
    }

    def __init__(self, tool_source):
        """初始化

        Args:
            tool_source: 工具来源（schema 列表 / ToolRegistry / dict[str, BaseTool]）
        """
        self._tool_source = tool_source

    @property
    def name(self) -> str:
        return "tool_search"

    @property
    def description(self) -> str:
        return "搜索可用工具。当不确定用什么工具时，先调用此工具查找相关工具及其参数说明。"

    @property
    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": "tool_search",
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "搜索查询（如 '读取文件'、'编译'、'烧录'）",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "返回结果数（默认 5，最大 10）",
                            "default": 5,
                        },
                    },
                    "required": ["query"],
                },
            },
        }

    async def execute(self, args: dict, context: dict) -> dict:
        """执行工具搜索

        Args:
            args: {"query": str, "limit": int}
            context: 上下文（未使用，保持与 BaseTool 接口一致）

        Returns:
            {"success": bool, "query": str, "total_matches": int, "tools": list}
        """
        query = (args.get("query") or "").strip()
        limit = min(max(args.get("limit", 5), 1), 10)

        if not query:
            return {"success": True, "tools": [], "message": "请提供搜索查询"}

        query_lower = query.lower()

        # 1. 关键词匹配（满分 1.0）
        matches = self._keyword_match(query_lower)

        # 2. 描述匹配（降权 0.5 以下，补充关键词没匹配到的）
        matches = self._description_match(query_lower, matches)

        # 3. 排序并取前 N
        matches.sort(key=lambda m: m.score, reverse=True)
        top_matches = matches[:limit]

        # 4. 返回带 schema 的结果
        return {
            "success": True,
            "query": query,
            "total_matches": len(matches),
            "tools": [
                {
                    "name": m.name,
                    "description": m.description,
                    "schema": m.schema,
                    "score": round(m.score, 2),
                }
                for m in top_matches
            ],
        }

    def _keyword_match(self, query: str) -> list[ToolMatch]:
        """关键词匹配 — 命中关键词的工具得满分 1.0"""
        matches = []
        seen = set()

        for pattern, tool_names in self.KEYWORD_MAP.items():
            if re.search(pattern, query, re.IGNORECASE):
                for name in tool_names:
                    if name in seen:
                        continue
                    entry = self._get_tool_entry(name)
                    if entry is not None:
                        matches.append(ToolMatch(
                            name=name,
                            score=1.0,
                            description=entry[1],
                            schema=entry[2],
                        ))
                        seen.add(name)
        return matches

    def _description_match(self, query: str, existing: list[ToolMatch]) -> list[ToolMatch]:
        """描述匹配 — 补充关键词没匹配到的工具（按词频降权，上限 0.5）"""
        existing_names = {m.name for m in existing}
        words = query.split()
        if not words:
            return existing

        for name, desc, schema in self._iter_all_entries():
            if name in existing_names:
                continue
            desc_lower = desc.lower()
            matched = sum(1 for w in words if w in desc_lower)
            if matched > 0:
                score = (matched / len(words)) * 0.5
                existing.append(ToolMatch(
                    name=name,
                    score=score,
                    description=desc,
                    schema=schema,
                ))
        return existing

    def _get_tool_entry(self, name: str):
        """获取单个工具的 (name, description, schema)，不存在返回 None"""
        for entry in self._iter_all_entries():
            if entry[0] == name:
                return entry
        return None

    def _iter_all_entries(self):
        """遍历所有工具，yield (name, description, schema)

        支持三种 tool_source 类型：
        - list[dict]: schema 列表（OpenAI function calling 格式）
        - ToolRegistry: 注册中心（用 tool_names() + get()）
        - dict[str, BaseTool]: 工具名到实例的映射
        """
        src = self._tool_source
        # list[dict] — schema 列表
        if isinstance(src, list):
            for schema in src:
                if not isinstance(schema, dict):
                    continue
                fn = schema.get("function", {})
                name = fn.get("name", "")
                desc = fn.get("description", "")
                if name:
                    yield name, desc, schema
            return
        # dict[str, BaseTool]
        if isinstance(src, dict):
            for name, tool in src.items():
                if hasattr(tool, "schema"):
                    schema = tool.schema
                    desc = schema.get("function", {}).get("description", "") if isinstance(schema, dict) else ""
                    yield name, desc, schema
                else:
                    yield name, str(tool), {}
            return
        # ToolRegistry — 用 tool_names() + get()
        if hasattr(src, "tool_names") and hasattr(src, "get"):
            for name in src.tool_names():
                tool = src.get(name)
                if tool is not None:
                    schema = tool.schema
                    desc = schema.get("function", {}).get("description", "") if isinstance(schema, dict) else ""
                    yield name, desc, schema
            return


def should_use_search_mode(system_prompt_tokens: int, total_tool_tokens: int,
                           threshold: int = SEARCH_MODE_THRESHOLD) -> bool:
    """判断是否应该启用搜索模式

    当 系统提示 token + 工具 schema token 超过阈值时启用。
    默认阈值 20000（约 15K 系统提示 + 5K 工具 schema）。

    Args:
        system_prompt_tokens: 系统提示的 token 数
        total_tool_tokens: 工具 schema 的 token 数
        threshold: 启用阈值，默认 SEARCH_MODE_THRESHOLD

    Returns:
        True 表示应该启用搜索模式
    """
    return (system_prompt_tokens + total_tool_tokens) > threshold


def build_search_mode_tools(tool_source) -> list:
    """构建搜索模式的工具列表

    返回 [ToolSearchTool 实例]。调用方需：
    1. 将 ToolSearchTool 注册到 ToolRegistry（使引擎能分发执行）
    2. 构建 schema 列表 = [tool_search.schema] + [chat_schema]（chat 从 BUILTIN_SCHEMAS 取）

    注意：chat 是终止性工具，由引擎内联处理（非 ToolRegistry 分发），
    其 schema 已在 BUILTIN_SCHEMAS 中定义，无需在此创建实例。
    """
    return [ToolSearchTool(tool_source)]
