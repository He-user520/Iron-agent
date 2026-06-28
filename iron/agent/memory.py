"""上下文管理 & 持久记忆系统（参考 OpenCode 压缩 + MiMo Code 4层记忆）

架构：
1. 会话内压缩（OpenCode 风格）— token 超限时自动摘要旧消息
2. 项目记忆（MiMo Code 风格）— MEMORY.md 跨会话持久化
3. 会话检查点 — checkpoint.md 保存当前状态
4. 任务进度 — tasks/<id>/progress.md

文件结构：
  .iron/memory/
  ├── MEMORY.md          ← 项目持久记忆
  ├── checkpoint.md      ← 最近一次会话检查点
  └── tasks/
      └── <id>/
          └── progress.md

ContextCompactor（5 层压缩管道）已拆分到 context_compactor.py，
通过模块级 __getattr__ 懒加载保持 `from iron.agent.memory import ContextCompactor` 向后兼容。
"""
import asyncio
import json
import logging
import re
import shutil
from pathlib import Path
from datetime import datetime

import httpx


# ── 常量 ───────────────────────────────────────────────────────

MAX_CONTEXT_TOKENS = 30000      # 估算的上下文 token 预算（留给历史部分，作为 fallback）
KEEP_RECENT_MESSAGES = 6        # 压缩时保留最近 N 条消息不压缩
SUMMARY_MAX_TOKENS = 2000       # 摘要最大 token
TOOL_OUTPUT_MAX_CHARS = 1500    # 工具输出截断长度
# P1-1 Level 1 Microcompact 参数
KEEP_RECENT_TOOL_RESULTS = 10   # Level 1 截断 tool 输出时，最近 N 条不截断
TOOL_OUTPUT_TRUNCATE_CHARS = 500  # Level 1 截断早期 tool 输出到此长度

SUMMARY_TEMPLATE = """基于以下对话历史，生成结构化摘要。只输出 Markdown，不要其他文字。

## 目标
- [一句话描述用户的主要任务]

## 进度
### 已完成
- [已完成的工作]
### 进行中
- [当前正在做的事]
### 阻塞
- [遇到的问题或阻塞项]

## 关键决策
- [做出的技术决策及原因]

## 下一步
- [接下来要做的事]

## 关键上下文
- [重要的技术细节、错误信息、文件路径]

## 相关文件
- [涉及的文件路径及说明]
"""


# ── Token 估算 ─────────────────────────────────────────────────

def estimate_tokens(text: str) -> int:
    """估算 token 数，优先用 tiktoken 精确计数。

    tiktoken（cl100k_base 编码）兼容 GPT-4/DeepSeek/Qwen 等主流模型。
    不可用时 fallback 到字符数估算（中: 1.5字/token, 英: 4字符/token）。
    """
    text = text or ""
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        return len(enc.encode(text))
    except (ImportError, Exception):
        cn_chars = len(re.findall(r'[\u4e00-\u9fff]', text))
        other_chars = len(text) - cn_chars
        return int(cn_chars / 1.5 + other_chars / 4)


# ── Token 计数器（统一导出，供 engine/main 复用） ─────────────────

def count_tokens(text: str) -> int:
    """供外部调用的统一 token 计数入口。"""
    return estimate_tokens(text)


def estimate_messages_tokens(messages: list[dict]) -> int:
    """估算消息列表的总 token 数"""
    total = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total += estimate_tokens(content)
        # tool_calls 也占 token
        if msg.get("tool_calls"):
            total += estimate_tokens(json.dumps(msg["tool_calls"], ensure_ascii=False))
    return total


# ── 消息序列化 ─────────────────────────────────────────────────

def serialize_message(msg: dict) -> str:
    """将消息序列化为可读文本（用于压缩）"""
    role = msg.get("role", "")
    content = msg.get("content", "")

    if role == "user":
        return f"[用户]: {content}"
    elif role == "assistant":
        parts = []
        if content:
            parts.append(f"[助手]: {content}")
        if msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                func = tc.get("function", {})
                name = func.get("name", "")
                args = func.get("arguments", "{}")
                parts.append(f"[工具调用]: {name}({args})")
        return "\n".join(parts)
    elif role == "tool":
        try:
            data = json.loads(content) if isinstance(content, str) else content
            success = data.get("success", True)
            status = "成功" if success else "失败"
            tool_info = data.get("command", data.get("path", ""))
            error = data.get("error", "")
            result = f"[工具结果]: {status}"
            if tool_info:
                result += f" | {tool_info}"
            if error:
                result += f" | 错误: {error}"
            if data.get("stdout"):
                stdout = (data.get("stdout") or "")[:500]
                result += f"\n[输出]: {stdout}"
            if data.get("content") and isinstance(data["content"], str):
                result += f"\n[内容]: {data['content'][:500]}"
            return result
        except (json.JSONDecodeError, TypeError):
            return f"[工具结果]: {content[:500]}"
    elif role == "system":
        return f"[系统]: {content[:200]}"
    return ""


# ── 项目持久记忆（MiMo Code 风格） ─────────────────────────────

class ProjectMemory:
    """项目持久记忆 — 跨会话保存在 .iron/memory/ 目录

    并发安全：dream()/distill() 操作用 asyncio.Lock 保护，
    防止同一进程内并发调用导致文件竞争（多进程场景仍需外部文件锁）。
    """

    def __init__(self, project_dir: str = "."):
        self.memory_dir = Path(project_dir) / ".iron" / "memory"
        self.memory_file = self.memory_dir / "MEMORY.md"
        self.checkpoint_file = self.memory_dir / "checkpoint.md"
        self.tasks_dir = self.memory_dir / "tasks"
        # dream/distill 并发锁：防止同一进程内并发整理记忆导致文件竞争
        self._dream_lock = asyncio.Lock()

    def ensure_dirs(self):
        """确保目录存在"""
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self.tasks_dir.mkdir(parents=True, exist_ok=True)

    # ── MEMORY.md（项目记忆）────────────────────────────────

    def load_memory(self) -> str:
        """加载项目记忆"""
        if self.memory_file.exists():
            return self.memory_file.read_text(encoding="utf-8")
        return ""

    # MEMORY.md 字符上限（约 12K tokens，避免撑满上下文）
    MAX_MEMORY_CHARS = 50000

    def save_memory(self, content: str):
        """保存项目记忆（超过 MAX_MEMORY_CHARS 自动截断）"""
        self.ensure_dirs()
        if len(content) > self.MAX_MEMORY_CHARS:
            content = content[:self.MAX_MEMORY_CHARS]
            logging.warning(f"MEMORY.md 超过 {self.MAX_MEMORY_CHARS} 字符，已自动截断")
        self.memory_file.write_text(content, encoding="utf-8")

    def append_to_memory(self, section: str, content: str):
        """追加内容到项目记忆的指定章节"""
        existing = self.load_memory()
        if not existing:
            existing = "# 项目记忆\n\n"

        # 查找章节（用正则匹配行首 ## header，避免匹配到正文中的字符串）
        header = f"## {section}"
        header_re = re.compile(r'^##\s+' + re.escape(section) + r'\s*$', re.MULTILINE)
        match = header_re.search(existing)
        if match:
            # 在章节末尾追加
            idx = match.end()
            next_section = existing.find("\n## ", idx)
            if next_section == -1:
                next_section = len(existing)
            insert_point = next_section
            existing = existing[:insert_point] + f"\n- {content}" + existing[insert_point:]
        else:
            # 新建章节
            existing += f"\n{header}\n- {content}\n"

        self.save_memory(existing)

    async def append_to_memory_with_embedding(self, section: str, content: str,
                                              llm=None, db=None,
                                              project_path: str = "") -> bool:
        """追加内容并向量化存入 db.history（fire-and-forget 降级）

        - 先同步追加到 MEMORY.md（保证数据不丢）
        - 若 llm 和 db 提供，则异步生成 embedding 并写入 history 表
        - LLM 调用失败时不影响 MEMORY.md，只记录 warning
        - 返回是否成功生成 embedding
        """
        # 1. 同步追加（保证 MEMORY.md 一定写入）
        self.append_to_memory(section, content)

        # 2. 向量化（可选，失败不影响主流程）
        if llm is None or db is None:
            return False

        try:
            from iron.core.db import HistoryRow
            from datetime import datetime as _dt
            # 生成 embedding
            embeddings = await llm.embed([content])
            if not embeddings:
                return False
            vec = embeddings[0]
            # 写入 history 表（带 embedding）
            history = HistoryRow(
                user_input=content,
                timestamp=_dt.now().isoformat(),
                project_path=project_path or "",
            )
            db.save_history_with_embedding(history, vec)
            return True
        except asyncio.CancelledError:
            raise
        except (NotImplementedError, RuntimeError, Exception) as e:
            # NotImplementedError: 后端不支持 embedding
            # RuntimeError: LLM 服务异常
            # 其他异常：db 写入失败
            logging.warning(f"向量化记忆失败（不影响主流程）: {e}", exc_info=True)
            return False

    # ── checkpoint.md（会话检查点）───────────────────────────

    def save_checkpoint(self, summary: str, files_changed: list[str] = None,
                        current_task: str = ""):
        """保存会话检查点（写入前自动备份旧检查点，避免崩溃丢失）"""
        self.ensure_dirs()
        # 写入前备份旧检查点
        if self.checkpoint_file.exists():
            backup = self.memory_dir / "checkpoint_backup.md"
            try:
                shutil.copy2(self.checkpoint_file, backup)
            except OSError as e:
                logging.warning(f"checkpoint 备份失败: {e}", exc_info=True)
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        checkpoint = f"""# 会话检查点
> 自动保存于 {now}

## 当前任务
{current_task or '(无)'}

## 会话摘要
{summary}

## 修改的文件
{chr(10).join(f'- {f}' for f in (files_changed or [])) or '(无)'}
"""
        self.checkpoint_file.write_text(checkpoint, encoding="utf-8")

    def load_checkpoint(self) -> str:
        """加载最近的会话检查点"""
        if self.checkpoint_file.exists():
            return self.checkpoint_file.read_text(encoding="utf-8")
        return ""

    # ── 任务进度 ─────────────────────────────────────────────

    def save_task_progress(self, task_id: str, progress: str):
        """保存任务进度"""
        self.ensure_dirs()
        # 拒绝路径穿越字符，仅允许字母数字下划线短横线
        if not re.match(r'^[a-zA-Z0-9_\-]+$', task_id or ""):
            raise ValueError(f"非法 task_id: {task_id}")
        task_dir = self.tasks_dir / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        progress_file = task_dir / "progress.md"

        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        existing = ""
        if progress_file.exists():
            existing = progress_file.read_text(encoding="utf-8")

        existing += f"\n\n### {now}\n{progress}\n"
        progress_file.write_text(existing, encoding="utf-8")

    def load_task_progress(self, task_id: str) -> str:
        """加载任务进度"""
        progress_file = self.tasks_dir / task_id / "progress.md"
        if progress_file.exists():
            return progress_file.read_text(encoding="utf-8")
        return ""

    # ── 上下文注入 ──────────────────────────────────────────

    def build_context_injection(self, token_budget: int = 3000) -> str:
        """构建注入到系统提示的记忆上下文

        按重要性排序，在 token 预算内尽量多地注入：
        1. 会话检查点（最近的状态）
        2. 项目记忆（长期知识）
        """
        parts = []
        used_tokens = 0

        # 1. 会话检查点
        checkpoint = self.load_checkpoint()
        if checkpoint:
            cp_tokens = estimate_tokens(checkpoint)
            if used_tokens + cp_tokens <= token_budget:
                parts.append(f"[上次会话状态]\n{checkpoint}")
                used_tokens += cp_tokens

        # 2. 项目记忆
        memory = self.load_memory()
        if memory:
            mem_tokens = estimate_tokens(memory)
            if used_tokens + mem_tokens <= token_budget:
                parts.append(f"[项目记忆]\n{memory}")
                used_tokens += mem_tokens
            elif used_tokens < token_budget:
                # 按 token 截断（精确计算，避免中文字符被切半）
                remaining = token_budget - used_tokens
                # 宽松上界：4 字符/token，然后逐次缩减到 token 预算内
                chars = min(len(memory), remaining * 4)
                truncated = memory[:chars]
                while estimate_tokens(truncated) > remaining and len(truncated) > 100:
                    truncated = truncated[:-100]
                parts.append(f"[项目记忆（截断）]\n{truncated}...")

        return "\n\n".join(parts)

    # ── Dream/Distill 记忆整理（参考 MiMo Code 7天/30天）─────────

    DREAM_INTERVAL_DAYS = 7    # Dream 整理周期（7 天）
    DISTILL_INTERVAL_DAYS = 30  # Distill 深度蒸馏周期（30 天）

    def _meta_file(self) -> Path:
        """元数据文件路径（记录上次 dream/distill 时间）"""
        return self.memory_dir / "meta.json"

    def _load_meta(self) -> dict:
        """加载元数据"""
        meta_path = self._meta_file()
        if meta_path.exists():
            try:
                return json.loads(meta_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        return {"last_dream": None, "last_distill": None}

    def _save_meta(self, meta: dict):
        """保存元数据"""
        self.ensure_dirs()
        self._meta_file().write_text(
            json.dumps(meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def should_dream(self) -> bool:
        """是否需要执行 Dream 整理（距上次 >= 7 天）"""
        meta = self._load_meta()
        last = meta.get("last_dream")
        if not last:
            # 首次：如果有 checkpoint 但从未 dream 过，则触发
            return self.checkpoint_file.exists()
        try:
            last_dt = datetime.fromisoformat(last)
            return (datetime.now() - last_dt).days >= self.DREAM_INTERVAL_DAYS
        except (ValueError, TypeError):
            return True

    def should_distill(self) -> bool:
        """是否需要执行 Distill 深度蒸馏（距上次 >= 30 天）"""
        meta = self._load_meta()
        last = meta.get("last_distill")
        if not last:
            return False  # 首次不自动 distill，需要先有 dream 产生的素材
        try:
            last_dt = datetime.fromisoformat(last)
            return (datetime.now() - last_dt).days >= self.DISTILL_INTERVAL_DAYS
        except (ValueError, TypeError):
            return True

    async def dream(self, llm=None) -> str:
        """Dream 整理 — 将短期记忆（checkpoint + task progress）提炼为长期知识

        并发安全：用 _dream_lock 保护，防止并发调用导致 MEMORY.md 竞争写入。

        参考 MiMo Code 的 Dream 机制：
        - 读取最近的 checkpoint 和 task progress
        - 用 LLM 提炼出可复用的知识、决策、教训
        - 追加到 MEMORY.md 的 "## 长期知识" 章节
        - 清理过期的 checkpoint（保留最近一次）

        Returns:
            生成的知识摘要
        """
        async with self._dream_lock:
            return await self._dream_impl(llm)

    async def _dream_impl(self, llm=None) -> str:
        """dream 实际实现（在 _dream_lock 保护下执行）"""
        self.ensure_dirs()

        # 收集短期记忆素材
        materials = []

        # 1. 会话检查点
        checkpoint = self.load_checkpoint()
        if checkpoint:
            materials.append(f"### 会话检查点\n{checkpoint}")

        # 2. 任务进度
        if self.tasks_dir.exists():
            for task_dir in sorted(self.tasks_dir.iterdir()):
                if task_dir.is_dir():
                    progress_file = task_dir / "progress.md"
                    if progress_file.exists():
                        try:
                            progress = progress_file.read_text(encoding="utf-8")
                        except (OSError, UnicodeDecodeError):
                            continue  # 单个文件读取失败不影响其他任务
                        materials.append(f"### 任务 {task_dir.name}\n{progress[:2000]}")

        if not materials:
            return ""

        material_text = "\n\n".join(materials)

        # 用 LLM 提炼长期知识
        knowledge = ""
        if llm:
            try:
                prompt = f"""从以下项目短期记忆中提炼可复用的长期知识。只输出结构化 Markdown，不要其他文字。

要求：
1. 提取技术决策、踩过的坑、最佳实践
2. 删除一次性的任务细节，保留可复用的模式
3. 每条知识用一句话概括

短期记忆素材：
{material_text}

输出格式：
## 长期知识（Dream {datetime.now().strftime('%Y-%m-%d')}）
- [知识点1]
- [知识点2]
"""
                resp = await llm.generate(
                    "你是记忆整理器。从项目历史中提炼可复用的长期知识。",
                    [{"role": "user", "content": prompt}],
                    temperature=0.1,
                    max_tokens=1500,
                )
                knowledge = resp.content.strip()
            except asyncio.CancelledError:
                raise
            except (RuntimeError, httpx.HTTPError) as e:
                logging.warning(f"dream LLM 调用失败: {e}", exc_info=True)

        # LLM 不可用时降级：简单提取
        if not knowledge:
            knowledge = self._simple_dream(material_text)

        # 追加到 MEMORY.md
        if knowledge:
            existing = self.load_memory()
            if not existing:
                existing = "# 项目记忆\n"
            existing += f"\n{knowledge}\n"
            self.save_memory(existing)

        # 更新元数据
        meta = self._load_meta()
        meta["last_dream"] = datetime.now().isoformat()
        self._save_meta(meta)

        return knowledge

    def _simple_dream(self, material_text: str) -> str:
        """Dream 降级方案（LLM 不可用时）：简单提取关键信息"""
        lines = material_text.split("\n")
        knowledge_lines = []
        for line in lines:
            # 提取包含关键信息的行
            if any(kw in line for kw in ["决策", "教训", "注意", "铁律", "规则", "最佳实践"]):
                knowledge_lines.append(f"- {line.strip()}")

        if not knowledge_lines:
            return ""

        return f"## 长期知识（Dream {datetime.now().strftime('%Y-%m-%d')}）\n" + \
               "\n".join(knowledge_lines[:20]) + "\n"

    async def distill(self, llm=None) -> str:
        """Distill 深度蒸馏 — 将 MEMORY.md 蒸馏为核心洞察

        并发安全：用 _dream_lock 保护，防止与 dream() 并发执行导致 MEMORY.md 竞争重写。

        参考 MiMo Code 的 Distill 机制：
        - 读取整个 MEMORY.md
        - 用 LLM 蒸馏为 5-10 条核心洞察
        - 重写 MEMORY.md（保留核心洞察 + 清理冗余）
        - 备份原始 MEMORY.md 到 archive/

        Returns:
            蒸馏后的核心洞察
        """
        async with self._dream_lock:
            return await self._distill_impl(llm)

    async def _distill_impl(self, llm=None) -> str:
        """distill 实际实现（在 _dream_lock 保护下执行）"""
        self.ensure_dirs()

        memory = self.load_memory()
        if not memory or len(memory) < 500:
            return ""  # 记忆太少不值得蒸馏

        # 备份原始记忆
        archive_dir = self.memory_dir / "archive"
        archive_dir.mkdir(exist_ok=True)
        archive_file = archive_dir / f"MEMORY_{datetime.now().strftime('%Y%m%d')}.md"
        archive_file.write_text(memory, encoding="utf-8")

        # 用 LLM 蒸馏
        distilled = ""
        if llm:
            try:
                prompt = f"""将以下项目记忆蒸馏为 5-10 条核心洞察。只输出结构化 Markdown，不要其他文字。

要求：
1. 合并重复的知识点
2. 保留最重要的铁律和最佳实践
3. 删除过时的、一次性的信息
4. 每条洞察用一句话概括

原始记忆：
{memory}

输出格式：
# 项目记忆（Distill {datetime.now().strftime('%Y-%m-%d')}）

## 核心铁律
- [最重要的规则]

## 最佳实践
- [可复用的实践]

## 技术决策
- [关键决策及原因]
"""
                resp = await llm.generate(
                    "你是记忆蒸馏器。将冗长的项目记忆蒸馏为精炼的核心洞察。",
                    [{"role": "user", "content": prompt}],
                    temperature=0.1,
                    max_tokens=2000,
                )
                distilled = resp.content.strip()
            except asyncio.CancelledError:
                raise
            except (RuntimeError, httpx.HTTPError) as e:
                logging.warning(f"distill LLM 调用失败: {e}", exc_info=True)

        # LLM 不可用时降级：保留前 2000 字符
        if not distilled:
            distilled = f"# 项目记忆（Distill {datetime.now().strftime('%Y-%m-%d')}）\n\n" + \
                        memory[:2000] + "\n\n[已蒸馏，原始记忆见 archive/]\n"

        # 重写 MEMORY.md
        self.save_memory(distilled)

        # 更新元数据
        meta = self._load_meta()
        meta["last_distill"] = datetime.now().isoformat()
        self._save_meta(meta)

        return distilled

    async def maybe_dream_distill(self, llm=None):
        """检查并执行 Dream/Distill（在 engine 启动时调用）

        - 如果 should_distill() 为 True，先执行 distill
        - 如果 should_dream() 为 True，执行 dream
        - 两者可以同时触发（distill 后 dream 仍可执行，补充 distill 未处理的素材）
        """
        try:
            if self.should_distill():
                await self.distill(llm)
            if self.should_dream():
                await self.dream(llm)
        except asyncio.CancelledError:
            raise
        except (RuntimeError, httpx.HTTPError) as e:
            logging.warning(f"记忆整理失败: {e}", exc_info=True)


# ── ContextCompactor 懒加载（向后兼容） ────────────────────────
# ContextCompactor 已拆分到 context_compactor.py。
# 使用 PEP 562 模块级 __getattr__ 实现懒加载，避免循环导入：
# - context_compactor.py 导入 memory.py 的 estimate_tokens 等函数
# - memory.py 通过 __getattr__ 懒加载 ContextCompactor
# 这样无论先导入哪个模块都不会产生循环依赖。

def __getattr__(name):
    """模块级懒加载：访问 ContextCompactor 时才从 context_compactor.py 导入。

    PEP 562（Python 3.7+）支持模块级 __getattr__，
    当 `from iron.agent.memory import ContextCompactor` 时，
    若 ContextCompactor 不在模块命名空间，则调用此函数。
    """
    if name == "ContextCompactor":
        from iron.agent.context_compactor import ContextCompactor
        return ContextCompactor
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
