"""会话管理 — 保存、恢复、历史记录"""
import json
import logging
import uuid
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from iron.core.db import Database


@dataclass
class ConversationSession:
    """对话会话"""
    id: str = ""
    created_at: str = ""
    messages: list = field(default_factory=list)
    mcu: str = ""
    project_dir: str = ""

    def add_message(self, role: str, content: str):
        self.messages.append({
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat(),
        })

    def save(self, sessions_dir: Path, db: "Optional[Database]" = None) -> Path:
        # P2: sanitize id 防止路径穿越（仅保留字母数字、连字符、下划线）
        safe_id = "".join(c for c in self.id if c.isalnum() or c in "-_")
        if not safe_id or safe_id != self.id:
            safe_id = f"{safe_id}_{uuid.uuid4().hex[:6]}"
            self.id = safe_id
        sessions_dir.mkdir(parents=True, exist_ok=True)
        path = sessions_dir / f"{self.id}.json"
        # JSON 文件始终保存（向后兼容 + 备份）
        with open(path, "w", encoding="utf-8") as f:
            json.dump({
                "id": self.id,
                "created_at": self.created_at or datetime.now().isoformat(),
                "mcu": self.mcu,
                "project_dir": self.project_dir,
                "messages": self.messages,
            }, f, ensure_ascii=False, indent=2)
        # P3-2: 如果传入 db，则同时保存到 SQLite（JSON 文件保留作为备份）
        if db is not None:
            self._save_to_db(db)
        return path

    def _save_to_db(self, db: "Database") -> None:
        """将会话 + 消息保存到 SQLite（P3-2）"""
        # 延迟导入避免循环依赖
        from iron.core.db import SessionRow
        now = datetime.now().isoformat()
        created_at = self.created_at or now
        metadata = json.dumps({"mcu": self.mcu}, ensure_ascii=False)
        session_row = SessionRow(
            session_id=self.id,
            project_path=self.project_dir or "",
            created_at=created_at,
            updated_at=now,
            message_count=len(self.messages),
            metadata=metadata,
        )
        try:
            db.save_session(session_row)
            db.save_messages(self.id, self.messages)
        except Exception as e:
            # SQLite 写入失败不应影响主流程（JSON 已保存）
            logging.warning(f"SQLite 保存会话失败: {e}")

    @classmethod
    def load(cls, path: "Optional[Path]" = None, db: "Optional[Database]" = None) -> "ConversationSession":
        # 优先从 JSON 文件加载（保持向后兼容）
        if path is not None and Path(path).exists():
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            session = cls(
                id=data.get("id", ""),
                created_at=data.get("created_at", ""),
                mcu=data.get("mcu", ""),
                project_dir=data.get("project_dir", ""),
            )
            # P3-1: JSON 中 messages 可能为 null，做防御性处理避免 None 赋值
            messages = data.get("messages") or []
            if not isinstance(messages, list):
                messages = []
            session.messages = messages
            return session
        # P3-2: JSON 文件不存在但传入了 db，从 SQLite 加载
        if db is not None:
            return cls._load_from_db(db, path)
        # 既没有文件也没有 db：抛出与原行为一致的错误
        raise FileNotFoundError(f"会话文件不存在: {path}")

    @classmethod
    def _load_from_db(cls, db: "Database", path: "Optional[Path]" = None) -> "ConversationSession":
        """从 SQLite 加载会话 + 消息（P3-2）

        - 如果 path 提供且文件名是 <session_id>.json，则从文件名提取 session_id
        - 否则列出 db 中最新的会话作为兜底
        """
        session_id = ""
        if path is not None:
            # 从文件名提取 session_id（去掉 .json 后缀）
            session_id = Path(path).stem
        if not session_id:
            # 兜底：取 db 中最新的一条会话
            sessions = db.list_sessions(limit=1)
            if not sessions:
                raise FileNotFoundError("数据库中无可用会话")
            session_id = sessions[0].session_id
        session_row = db.get_session(session_id)
        if session_row is None:
            raise FileNotFoundError(f"SQLite 中未找到会话: {session_id}")
        # 解析 metadata 中的 mcu
        try:
            meta = json.loads(session_row.metadata) if session_row.metadata else {}
        except (json.JSONDecodeError, TypeError):
            meta = {}
        session = cls(
            id=session_row.session_id,
            created_at=session_row.created_at,
            mcu=meta.get("mcu", ""),
            project_dir=session_row.project_path,
        )
        # 从消息表加载
        msg_rows = db.get_messages(session_id)
        messages = []
        for r in msg_rows:
            m = {"role": r.role, "content": r.content, "timestamp": r.created_at}
            # 还原 tool_calls（如果非空数组）
            if r.tool_calls and r.tool_calls not in ("[]", ""):
                try:
                    tc = json.loads(r.tool_calls)
                    if tc:
                        m["tool_calls"] = tc
                except (json.JSONDecodeError, TypeError):
                    pass
            if r.tool_call_id:
                m["tool_call_id"] = r.tool_call_id
            messages.append(m)
        session.messages = messages
        return session

    @classmethod
    def list_sessions(cls, sessions_dir: Path) -> list:
        if not sessions_dir.exists():
            return []
        sessions = []
        for f in sorted(sessions_dir.glob("*.json"), reverse=True):
            try:
                s = cls.load(f)
                sessions.append({"id": s.id, "created_at": s.created_at, "mcu": s.mcu,
                                 "messages": len(s.messages)})
            except (OSError, json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
                logging.warning(f"加载会话 {f} 失败: {e}")
        return sessions
