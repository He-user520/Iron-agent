"""task_track 工具 — 任务进度跟踪（参考 Claude Code TodoWrite + MiMo Code tasks/）"""
from pathlib import Path
from datetime import datetime
from iron.tools.base import BaseTool


class TaskTrackTool(BaseTool):
    """任务跟踪 — AI 自己维护任务列表，解决上下文腐烂问题"""

    VALID_STATUS = {"pending", "in_progress", "completed", "failed"}

    def __init__(self):
        self._tasks: list[dict] = []

    @property
    def name(self) -> str:
        return "task_track"

    @property
    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": "task_track",
                "description": "管理任务列表。创建、更新、完成任务，跟踪多步骤任务的进度。解决长对话中的上下文遗忘问题。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["create", "update", "complete", "fail", "list"],
                            "description": "操作类型",
                        },
                        "task_id": {"type": "string", "description": "任务 ID（如 'task_001'）"},
                        "title": {"type": "string", "description": "任务标题"},
                        "status": {"type": "string", "description": "状态: pending/in_progress/completed/failed", "enum": ["pending", "in_progress", "completed", "failed"]},
                        "notes": {"type": "string", "description": "备注信息"},
                    },
                    "required": ["action"],
                },
            },
        }

    async def execute(self, args: dict, context: dict) -> dict:
        action = args.get("action", "")
        task_id = args.get("task_id", "")
        title = args.get("title", "")
        status = args.get("status", "")
        notes = args.get("notes")  # 不带默认值，未传时为 None

        if action == "create":
            return self._create(task_id, title, notes)
        elif action == "update":
            return self._update(task_id, status, notes)
        elif action == "complete":
            return self._update(task_id, "completed", notes)
        elif action == "fail":
            return self._update(task_id, "failed", notes)
        elif action == "list":
            return self._list()
        else:
            return {"success": False, "error": f"未知操作: {action}"}

    def _create(self, task_id: str, title: str, notes: str) -> dict:
        if not task_id:
            task_id = f"task_{len(self._tasks) + 1:03d}"
        elif self._find(task_id):
            return {"success": False, "error": f"任务 ID 已存在: {task_id}"}
        if not title:
            return {"success": False, "error": "缺少 title"}

        task = {
            "id": task_id,
            "title": title,
            "status": "pending",
            "notes": notes or "",  # create 时未传 notes 默认空字符串
            "created": datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
        self._tasks.append(task)
        return {"success": True, "task": task, "total": len(self._tasks)}

    def _update(self, task_id: str, status: str, notes: str) -> dict:
        task = self._find(task_id)
        if not task:
            return {"success": False, "error": f"任务不存在: {task_id}"}
        if status:
            if status not in self.VALID_STATUS:
                return {"success": False, "error": f"非法 status: {status}，合法值: {self.VALID_STATUS}"}
            task["status"] = status
        # notes 显式传入时更新（允许传空字符串清空 notes）
        if notes is not None:
            task["notes"] = notes
        return {"success": True, "task": task}

    def _list(self) -> dict:
        return {"success": True, "tasks": self._tasks, "total": len(self._tasks)}

    def _find(self, task_id: str) -> dict | None:
        for t in self._tasks:
            if t["id"] == task_id:
                return t
        return None

    def get_tasks_for_display(self) -> list[dict]:
        """获取任务列表用于 UI 展示"""
        return self._tasks

    def save_to_file(self, project_dir: str, session_summary: str = ""):
        """保存任务进度到磁盘（持久记忆）"""
        if not self._tasks:
            return
        try:
            tasks_dir = Path(project_dir) / ".iron" / "memory" / "tasks" / "session"
            tasks_dir.mkdir(parents=True, exist_ok=True)
            progress_file = tasks_dir / "progress.md"
            now = datetime.now().strftime("%Y-%m-%d %H:%M")
            content = f"# 会话任务进度\n> {now}\n\n"
            for t in self._tasks:
                icon = {"pending": "○", "in_progress": "◎", "completed": "✓", "failed": "✗"}.get(t["status"], "?")
                content += f"- {icon} [{t['id']}] {t['title']}"
                if t.get("notes"):
                    content += f" — {t['notes']}"
                content += "\n"
            if session_summary:
                content += f"\n## 会话摘要\n{session_summary}\n"
            progress_file.write_text(content, encoding="utf-8")
        except OSError as e:
            import logging
            logging.warning(f"任务保存失败: {e}")
