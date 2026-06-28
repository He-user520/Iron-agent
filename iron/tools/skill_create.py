"""skill_create 工具 — AI 通过自然语言创建自定义 skill

参考 Claude Code 的 skill 系统：
用户说"创建一个 xxx 技能"，AI 调用此工具生成 .iron/skills/xxx.md 文件，
下次会话自动加载，匹配关键词时注入 prompt。
"""
import re
from pathlib import Path
from iron.tools.base import BaseTool


class SkillCreateTool(BaseTool):
    """创建自定义技能"""

    @property
    def name(self) -> str:
        return "skill_create"

    @property
    def description(self) -> str:
        return ("创建自定义技能（skill）。用户描述需求后，AI 生成 .md 文件保存到 "
                ".iron/skills/，下次会话自动加载。匹配关键词时注入 prompt 指导 AI 执行。")

    @property
    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": "skill_create",
                "description": "创建自定义技能。用户说\"创建一个xxx技能\"时使用。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "技能名称（英文 kebab-case，如 uart-debug）",
                        },
                        "description": {
                            "type": "string",
                            "description": "技能描述（中文，一句话说明用途）",
                        },
                        "trigger_patterns": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "触发关键词列表（匹配到这些词时激活技能）",
                        },
                        "icon": {
                            "type": "string",
                            "description": "技能图标（emoji，如 🔧）",
                        },
                        "prompt": {
                            "type": "string",
                            "description": "技能的 prompt 内容（激活时注入到 AI 系统提示，指导 AI 如何执行）",
                        },
                    },
                    "required": ["name", "description", "prompt"],
                },
            },
        }

    async def execute(self, args: dict, context: dict) -> dict:
        name = args.get("name", "").strip().lower()
        description = args.get("description", "").strip()
        trigger_patterns = args.get("trigger_patterns", [])
        icon = args.get("icon", "📋").strip()
        prompt = args.get("prompt", "").strip()

        if not name or not description or not prompt:
            return {"success": False, "error": "name, description, prompt 不能为空"}

        # 验证名称格式（kebab-case，长度 2-64）
        if not re.match(r'^[a-zA-Z0-9][a-zA-Z0-9_-]{1,63}$', name):
            return {"success": False, "error": "name 必须以字母数字开头，仅含字母数字、连字符、下划线，长度 2-64"}

        # trigger_patterns 类型校验：允许传入单个字符串，自动转为列表
        if isinstance(trigger_patterns, str):
            trigger_patterns = [trigger_patterns]
        elif not isinstance(trigger_patterns, list):
            return {"success": False, "error": "trigger_patterns 必须是字符串或字符串列表"}
        # 过滤非字符串元素
        trigger_patterns = [str(t) for t in trigger_patterns if t]

        # 获取项目目录
        project_dir = Path(context.get("project_dir", "."))
        skills_dir = project_dir / ".iron" / "skills"
        try:
            skills_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return {"success": False, "error": f"创建技能目录失败: {e}"}

        # 生成 .md 文件
        skill_file = skills_dir / f"{name}.md"
        if skill_file.exists():
            return {"success": False, "error": f"技能 {name} 已存在: {skill_file}"}

        # 用 yaml.safe_dump 生成 frontmatter，避免用户输入注入/破坏 YAML 结构
        try:
            import yaml
        except ImportError:
            return {"success": False, "error": "pyyaml 未安装，无法生成技能文件"}

        frontmatter_data = {
            "name": name,
            "description": description,
            "icon": icon,
            "trigger_patterns": trigger_patterns if trigger_patterns else [name],
        }
        # sort_keys=False 保持可读顺序；default_flow_style=False 使用块格式
        frontmatter = yaml.safe_dump(
            frontmatter_data, allow_unicode=True, default_flow_style=False, sort_keys=False
        ).rstrip("\n")
        content = f"---\n{frontmatter}\n---\n\n{prompt}\n"

        try:
            from iron.tools.path_guard import validate_path_in_project
            skill_file = validate_path_in_project(
                str(skills_dir / f"{name}.md"), str(project_dir), allow_create=True
            )
            skill_file.write_text(content, encoding="utf-8")
        except ValueError as e:
            return {"success": False, "error": f"路径校验失败: {e}"}
        except OSError as e:
            return {"success": False, "error": f"写入技能文件失败: {e}"}

        return {
            "success": True,
            "message": f"技能 {name} 已创建: {skill_file}",
            "path": str(skill_file),
            "name": name,
            "description": description,
            "trigger_count": len(trigger_patterns),
            "note": "下次会话启动时自动加载。用户提到匹配关键词时会激活此技能。",
        }
