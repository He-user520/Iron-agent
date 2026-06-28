"""Agent 管理器 — 加载、切换、管理多个 Agent（参考 OpenCode agent 系统）

每个 Agent 是一个 .md 文件，包含 YAML frontmatter（配置）和 Markdown body（prompt）。
"""
import re
from pathlib import Path
from dataclasses import dataclass, field


@dataclass
class AgentConfig:
    """Agent 配置"""
    name: str
    description: str = ""
    mode: str = "primary"  # primary / subagent
    permissions: dict = field(default_factory=lambda: {
        "read": "allow", "edit": "allow", "bash": "allow"
    })
    prompt: str = ""  # Markdown body


class AgentManager:
    """Agent 管理器"""

    # P1-4: Agent → 引擎类型映射（参考 OpenCode Coder/Task 双 Agent 设计）
    # coder = 全工具（CoderAgentEngine），task = 只读工具（TaskAgentEngine）
    AGENT_ENGINE_TYPES = {
        "build": "coder",    # 默认开发 Agent → CoderAgentEngine
        "embed": "coder",    # 嵌入式专用 Agent → CoderAgentEngine
        "plan": "task",      # 只读分析 Agent → TaskAgentEngine
        "explore": "task",   # 只读探索 Agent → TaskAgentEngine（P1-4 新增）
    }

    def __init__(self, project_dir: str = "."):
        self._project_dir = project_dir
        self._agents: dict[str, AgentConfig] = {}
        self._current: str = "build"
        self._load_builtin_agents()
        self._load_project_agents()

    def _load_builtin_agents(self):
        """加载内置 Agent 定义"""
        agents_dir = Path(__file__).parent / "agents"
        self._load_from_dir(agents_dir)
        # P1-4: 注册内置只读 explore Agent（无 .md 文件，编程式注册）
        # 对应 TaskAgentEngine，用于安全的代码库探索和方案规划
        if "explore" not in self._agents:
            self._agents["explore"] = AgentConfig(
                name="explore",
                description="只读探索 Agent，用于代码库探索和分析（无写权限）",
                mode="primary",
                permissions={"read": "allow", "edit": "deny", "bash": "deny"},
                prompt=(
                    "你是 Iron 的只读探索助手。你只能使用只读工具"
                    "（read_file/search_code/find_files/web_search），"
                    "不能修改文件、不能编译/烧录。"
                    "用于安全的代码库探索和方案规划。"
                ),
            )

    def get_engine_type(self, agent_name: str = None) -> str:
        """获取 Agent 对应的引擎类型（coder/task）

        未映射的 Agent 默认返回 "coder"（全工具），保持向后兼容。
        """
        name = agent_name or self._current
        return self.AGENT_ENGINE_TYPES.get(name, "coder")

    def _load_project_agents(self):
        """加载项目级 Agent 定义（.iron/agents/）"""
        project_agents = Path(self._project_dir) / ".iron" / "agents"
        if project_agents.exists():
            self._load_from_dir(project_agents)

    def _load_from_dir(self, directory: Path):
        """从目录加载 Agent 定义"""
        for md_file in sorted(directory.glob("*.md")):
            try:
                content = md_file.read_text(encoding="utf-8")
                config = self._parse_agent_md(md_file.stem, content)
                self._agents[config.name] = config
            except (OSError, UnicodeDecodeError, ValueError) as e:
                import logging
                logging.warning(f"Agent {md_file} 加载失败: {e}")
                continue

    def _parse_agent_md(self, name: str, content: str) -> AgentConfig:
        """解析 Agent Markdown 文件

        格式：
        ---
        description: ...
        mode: primary
        permissions:
          read: allow
          edit: ask
          bash: ask
        ---
        # Agent Name
        prompt content...
        """
        config = AgentConfig(name=name)

        # 解析 YAML frontmatter
        frontmatter_match = re.match(r'^---\s*\n(.*?)\n---\s*\n', content, re.DOTALL)
        if frontmatter_match:
            fm_text = frontmatter_match.group(1)
            body = content[frontmatter_match.end():]

            # 简单 YAML 解析（不依赖 pyyaml）
            for line in fm_text.split("\n"):
                line = line.strip()
                if ":" in line:
                    key, _, value = line.partition(":")
                    key = key.strip()
                    value = value.strip()
                    if key == "description":
                        config.description = value
                    elif key == "mode":
                        config.mode = value
                    elif key in ("read", "edit", "bash"):
                        config.permissions[key] = value
            # 解析 permissions 子块
            in_permissions = False
            for line in fm_text.split("\n"):
                stripped = line.strip()
                if stripped == "permissions:":
                    in_permissions = True
                    continue
                if in_permissions and stripped.startswith(("read:", "edit:", "bash:", "task:")):
                    # 注意：task 权限键当前未在 engine 中生效（engine 从未调用 get_permission("task")）
                    k, _, v = stripped.partition(":")
                    config.permissions[k.strip()] = v.strip()
                elif in_permissions and not stripped.startswith("-") and stripped and ":" in stripped:
                    # 遇到新的顶层键，退出 permissions 块
                    in_permissions = False

            config.prompt = body.strip()
        else:
            config.prompt = content.strip()

        return config

    def get_current(self) -> AgentConfig:
        """获取当前 Agent

        注意：当当前 Agent 不存在时，返回一个默认的 build Agent（prompt 为空），
        调用方应自行处理 prompt 为空的情况。
        """
        return self._agents.get(self._current, AgentConfig(name="build"))

    def get_current_name(self) -> str:
        return self._current

    def switch(self, name: str) -> bool:
        """切换 Agent"""
        if name in self._agents:
            self._current = name
            return True
        return False

    def list_agents(self) -> list[dict]:
        """列出所有可用 Agent"""
        return [
            {
                "name": a.name,
                "description": a.description,
                "mode": a.mode,
                "current": a.name == self._current,
            }
            for a in self._agents.values()
            if a.mode == "primary"
        ]

    def get_permission(self, tool_name: str) -> str:
        """获取当前 Agent 对某工具的权限"""
        agent = self.get_current()
        return agent.permissions.get(tool_name, "ask")

    def build_agent_prompt(self, base_prompt: str) -> str:
        """将 Agent prompt 合并到系统提示"""
        agent = self.get_current()
        if agent.prompt:
            return f"{base_prompt}\n\n[Agent: {agent.name}]\n{agent.prompt}"
        return base_prompt
