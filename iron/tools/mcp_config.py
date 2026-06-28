"""mcp_config 工具 — AI 搜索 GitHub 找 MCP 并配置

参考 Claude Code 的 MCP 配置能力：
用户说"添加 xxx MCP"，AI 搜索 GitHub，找到 MCP 服务器，配置到 iron.yml，
下次启动时自动连接。
"""
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from iron.tools.base import BaseTool


class McpConfigTool(BaseTool):
    """MCP 服务器配置工具"""

    @property
    def name(self) -> str:
        return "mcp_config"

    @property
    def description(self) -> str:
        return ("配置 MCP 服务器。用户说\"添加 xxx MCP\"时使用。"
                "支持：搜索 GitHub、添加已有服务器、列出已配置、测试连接。")

    @property
    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": "mcp_config",
                "description": "配置 MCP 服务器。支持搜索 GitHub、添加、列出、测试连接。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["search", "add", "list", "test", "remove"],
                            "description": ("search: 搜索 GitHub MCP 服务器；"
                                            "add: 添加已知 MCP 服务器；"
                                            "list: 列出已配置的 MCP；"
                                            "test: 测试 MCP 连接；"
                                            "remove: 移除 MCP 配置"),
                        },
                        "query": {
                            "type": "string",
                            "description": "search 模式：搜索关键词（如 'github mcp server'）",
                        },
                        "name": {
                            "type": "string",
                            "description": "add/remove/test 模式：MCP 服务器名称",
                        },
                        "command": {
                            "type": "string",
                            "description": "add 模式：启动命令（如 'npx -y @modelcontextprotocol/server-filesystem'）",
                        },
                        "args": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "add 模式：命令参数列表",
                        },
                        "env": {
                            "type": "object",
                            "description": "add 模式：环境变量（如 API key）",
                        },
                    },
                    "required": ["action"],
                },
            },
        }

    async def execute(self, args: dict, context: dict) -> dict:
        action = args.get("action", "")
        project_dir = Path(context.get("project_dir", "."))

        if action == "search":
            return await self._search_github(args.get("query", ""))
        elif action == "add":
            return await self._add_server(args, project_dir)
        elif action == "list":
            return await self._list_servers(project_dir)
        elif action == "test":
            return await self._test_server(args.get("name", ""), project_dir)
        elif action == "remove":
            return await self._remove_server(args.get("name", ""), project_dir)
        else:
            return {"success": False, "error": f"未知 action: {action}"}

    async def _search_github(self, query: str) -> dict:
        """搜索 GitHub 上的 MCP 服务器"""
        if not query:
            return {"success": False, "error": "query 不能为空"}

        # 使用 web_search 工具搜索 GitHub
        try:
            from iron.tools.web_search import WebSearchTool
            search_tool = WebSearchTool()
            result = await search_tool.execute({
                "query": f"site:github.com modelcontextprotocol {query}",
                "max_results": 10,
            }, {})

            if not result.get("success"):
                return {"success": False, "error": "搜索失败"}

            # 解析搜索结果，提取 GitHub 仓库
            results = result.get("results", [])
            mcp_servers = []
            for r in results:
                url = r.get("url", "")
                title = r.get("title", "")
                if "github.com" in url and "modelcontextprotocol" in url.lower():
                    # 提取仓库名
                    parts = url.replace("https://github.com/", "").split("/")
                    repo = "/".join(parts[:2]) if len(parts) >= 2 else ""
                    mcp_servers.append({
                        "repo": repo,
                        "title": title,
                        "url": url,
                        "install_hint": f"npx -y @{repo}/mcp-server" if repo else "",
                    })

            return {
                "success": True,
                "query": query,
                "found": len(mcp_servers),
                "servers": mcp_servers[:5],
                "message": f"找到 {len(mcp_servers)} 个 MCP 服务器，用 mcp_config(action='add') 添加",
            }
        except (ImportError, ValueError, KeyError, IndexError) as e:
            return {"success": False, "error": f"搜索失败: {e}"}

    async def _add_server(self, args: dict, project_dir: Path) -> dict:
        """添加 MCP 服务器配置"""
        name = args.get("name", "").strip()
        command = args.get("command", "").strip()
        cmd_args = args.get("args", [])
        env = args.get("env", {})

        if not name or not command:
            return {"success": False, "error": "name 和 command 不能为空"}

        # 校验 name 仅含字母数字和连字符（防止配置键注入），长度 2-64
        if not re.match(r'^[a-zA-Z0-9][a-zA-Z0-9_-]{1,63}$', name):
            return {"success": False, "error": "name 必须以字母数字开头，仅含字母数字、连字符、下划线，长度 2-64"}

        # 读取现有配置
        config_path = project_dir / "iron.yml"
        config = self._read_config(config_path)

        # 添加 MCP 配置（统一用 "mcp" 键，与 IronConfig._merge_yaml 一致）
        if "mcp" not in config:
            config["mcp"] = {}

        config["mcp"][name] = {
            "command": command,
            "args": cmd_args,
            "env": {k: "${" + k + "}" for k in (env or {})} if env else {},  # 占位符，启动时从 os.environ 解析
            "type": "local",
            "enabled": True,
        }

        # 写回配置
        self._write_config(config_path, config)

        return {
            "success": True,
            "message": f"已添加 MCP 服务器 {name}",
            "name": name,
            "command": command,
            "warning": "env 已以 ${KEY} 占位符形式写入 iron.yml，请确保对应环境变量已设置" if env else None,
            "note": "下次启动 iron 时自动连接。可用 mcp_config(action='test') 测试连接。",
        }

    async def _list_servers(self, project_dir: Path) -> dict:
        """列出已配置的 MCP 服务器"""
        config_path = project_dir / "iron.yml"
        config = self._read_config(config_path)
        # 兼容旧格式 mcp_servers，但优先用 mcp
        servers = config.get("mcp", config.get("mcp_servers", {}))

        return {
            "success": True,
            "count": len(servers),
            "servers": [
                {
                    "name": name,
                    "command": cfg.get("command", ""),
                    "args": cfg.get("args", []),
                    "env_keys": list(cfg.get("env", {}).keys()),  # 只显示 key，不泄露 value
                    "full_command": self._format_command(cfg),
                }
                for name, cfg in servers.items()
            ],
        }

    def _format_command(self, cfg: dict) -> str:
        """格式化完整命令用于展示"""
        cmd = cfg.get("command", "")
        args = cfg.get("args", [])
        if args:
            return f"{cmd} {' '.join(str(a) for a in args)}"
        return cmd

    async def _test_server(self, name: str, project_dir: Path) -> dict:
        """测试 MCP 服务器连接（仅校验命令存在性，不真正执行任意命令）"""
        config_path = project_dir / "iron.yml"
        config = self._read_config(config_path)
        # 兼容旧格式 mcp_servers
        servers = config.get("mcp", config.get("mcp_servers", {}))

        if name not in servers:
            return {"success": False, "error": f"MCP 服务器 {name} 未配置"}

        cfg = servers[name]
        command = cfg.get("command", "")
        cmd_args = cfg.get("args", [])

        if not command:
            return {"success": False, "error": f"MCP 服务器 {name} 未配置 command"}

        # 仅校验命令在 PATH 中是否存在，不真正执行（避免任意命令执行风险）
        resolved = shutil.which(command)
        if resolved is None:
            return {
                "success": False,
                "error": f"命令不存在于 PATH 中: {command}",
                "name": name,
            }

        return {
            "success": True,
            "name": name,
            "command": command,
            "resolved_path": resolved,
            "args_count": len(cmd_args),
            "env_keys": list(cfg.get("env", {}).keys()),
            "message": f"MCP 服务器 {name} 命令可用（{resolved}）",
        }

    async def _remove_server(self, name: str, project_dir: Path) -> dict:
        """移除 MCP 服务器配置"""
        config_path = project_dir / "iron.yml"
        config = self._read_config(config_path)
        # 兼容旧格式 mcp_servers
        servers = config.get("mcp", config.get("mcp_servers", {}))

        if name not in servers:
            return {"success": False, "error": f"MCP 服务器 {name} 未配置"}

        del servers[name]
        # 写回正确的键
        if "mcp" in config:
            config["mcp"] = servers
        else:
            config["mcp_servers"] = servers
        self._write_config(config_path, config)

        return {
            "success": True,
            "message": f"MCP 服务器 {name} 已移除",
        }

    def _read_config(self, config_path: Path) -> dict:
        """读取 iron.yml 配置（强制使用 pyyaml，避免降级解析器损坏多服务器场景）"""
        if not config_path.exists():
            return {}
        try:
            import yaml
        except ImportError as e:
            raise RuntimeError("pyyaml 未安装，无法解析 iron.yml 配置（pip install pyyaml）") from e
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = yaml.safe_load(f) or {}
        except yaml.YAMLError as e:
            raise RuntimeError(f"iron.yml 解析失败: {e}") from e
        # 统一迁移旧格式 mcp_servers → mcp
        if "mcp_servers" in config:
            old = config.pop("mcp_servers")
            if isinstance(old, dict):
                config.setdefault("mcp", {}).update(old)
        return config

    def _write_config(self, config_path: Path, config: dict):
        """写入 iron.yml 配置（原子写入：临时文件 + os.replace，避免写入中途损坏）"""
        try:
            import yaml
        except ImportError as e:
            raise RuntimeError("pyyaml 未安装，无法写入 iron.yml 配置（pip install pyyaml）") from e
        # 序列化内容
        content = yaml.safe_dump(config, allow_unicode=True, default_flow_style=False, sort_keys=False)
        # 原子写入：先写临时文件，再 os.replace 替换（os.replace 在同文件系统下是原子操作）
        config_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            prefix=".iron_yml_", suffix=".tmp", dir=str(config_path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp_path, config_path)
        except OSError:
            # 写入失败时清理临时文件
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
