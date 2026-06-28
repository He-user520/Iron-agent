"""系统提示分块缓存 — 参考 Claude Code 的两块缓存策略

将冗长的系统提示（铁律 + 项目规则 + 工具说明，约 15K token）拆分为两个缓存块，
跨请求复用以减少重复计算的 token 成本。

- Block A: 核心指令（铁律 + 工具说明）— TTL 5 分钟，几乎不变
- Block B: 项目配置（文件列表 + 记忆）— TTL 5 分钟，会话内稳定
"""
import hashlib
import logging
import threading
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# 系统提示切分用的分隔符（按优先级排序）
# 找到任一分隔符即在此处切分：之前为 Block A（核心指令），之后为 Block B（项目配置）
_SPLIT_SEPARATORS = (
    "[项目配置]",
    "## 项目配置",
    "# 项目配置",
    "## 当前环境",
    "当前项目已有文件",
    "构建系统:",
    "目标 MCU:",
)


@dataclass
class CachedPromptBlock:
    """缓存块"""
    cache_key: str        # 内容 hash（SHA256 前 16 位）
    content: str
    created_at: float
    hit_count: int = 0   # 命中次数


class PromptCache:
    """系统提示分块缓存

    参考 Claude Code 的两块缓存策略：
    - Block A: 核心指令（铁律 + 工具说明）— TTL 5 分钟，几乎不变
    - Block B: 项目配置（文件列表 + 记忆）— TTL 5 分钟，会话内稳定

    线程安全：用 threading.Lock 保护内部 _blocks 字典。
    """

    def __init__(self, ttl_seconds: int = 300):
        self._blocks: dict[str, CachedPromptBlock] = {}
        self._ttl = ttl_seconds
        self._lock = threading.Lock()
        # 统计：总查询次数和命中次数（用于计算命中率）
        self._total_queries = 0
        self._total_hits = 0

    @staticmethod
    def _compute_cache_key(content: str) -> str:
        """计算内容的 SHA256 hash 前 16 位作为 cache_key"""
        return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]

    def split_prompt(self, system_prompt: str) -> list[dict]:
        """将系统提示分为两个缓存块

        策略：找到 "[项目配置]" 或类似分隔符切分
        返回 [{"role": "system", "content": block_a, "cache_key": "..."}, ...]

        若找不到分隔符，则整体作为一个块返回。
        """
        if not system_prompt:
            return []

        # 查找切分点（按优先级匹配第一个出现的分隔符）
        split_pos = -1
        for sep in _SPLIT_SEPARATORS:
            pos = system_prompt.find(sep)
            if pos != -1:
                split_pos = pos
                break

        if split_pos == -1:
            # 找不到分隔符 → 整体作为一个块
            cache_key = self._compute_cache_key(system_prompt)
            return [{"role": "system", "content": system_prompt, "cache_key": cache_key}]

        # 切分为两个块：分隔符之前为核心指令，分隔符起为项目配置
        block_a_content = system_prompt[:split_pos].rstrip()
        block_b_content = system_prompt[split_pos:]

        blocks = []
        if block_a_content:
            blocks.append({
                "role": "system",
                "content": block_a_content,
                "cache_key": self._compute_cache_key(block_a_content),
            })
        if block_b_content:
            blocks.append({
                "role": "system",
                "content": block_b_content,
                "cache_key": self._compute_cache_key(block_b_content),
            })
        return blocks

    def get_or_create(self, content: str) -> CachedPromptBlock:
        """获取或创建缓存块，命中时增加 hit_count

        - 命中（缓存存在且未过期）：hit_count +1，返回已有块
        - 未命中或已过期：创建新块并返回（hit_count=0）
        """
        cache_key = self._compute_cache_key(content)
        now = time.time()
        with self._lock:
            self._total_queries += 1
            block = self._blocks.get(cache_key)
            if block is not None:
                # 检查 TTL 是否过期
                if now - block.created_at > self._ttl:
                    # 过期 → 删除后重建
                    del self._blocks[cache_key]
                    block = None
            if block is None:
                # 未命中 → 创建新块
                block = CachedPromptBlock(
                    cache_key=cache_key,
                    content=content,
                    created_at=now,
                    hit_count=0,
                )
                self._blocks[cache_key] = block
            else:
                # 命中 → 增加计数
                block.hit_count += 1
                self._total_hits += 1
            return block

    def invalidate(self, cache_key: str = None):
        """使缓存失效（全部或指定 key）

        - cache_key=None：清空所有缓存块
        - cache_key 指定：仅删除该 key 对应的块
        """
        with self._lock:
            if cache_key is None:
                self._blocks.clear()
            else:
                self._blocks.pop(cache_key, None)

    def stats(self) -> dict:
        """返回缓存统计：总块数、命中次数、命中率"""
        with self._lock:
            total_blocks = len(self._blocks)
            total_hits = self._total_hits
            total_queries = self._total_queries
            hit_rate = (total_hits / total_queries) if total_queries > 0 else 0.0
            return {
                "total_blocks": total_blocks,
                "total_hits": total_hits,
                "total_queries": total_queries,
                "hit_rate": hit_rate,
            }
