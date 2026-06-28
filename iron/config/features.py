"""特性门控 — 运行时特性开关

参考 Claude Code 的 88 个特性标记设计（简化为统一配置）：
- 所有特性集中管理
- 默认值 + 用户覆盖
- 运行时查询 is_enabled(name)

设计原则：
- 默认值合理：已实现的功能默认 True，可选/实验性功能默认 False
- 用户覆盖：~/.iron/features.yml 文件覆盖默认值
- 全局单例：get_feature_flags() 提供进程级单例
- 安全降级：加载失败时回退到默认值，不阻塞主流程

用法:
    from iron.config.features import is_feature_enabled
    if is_feature_enabled("prompt_caching"):
        # 启用 prompt cache
        ...
"""
import logging
from pathlib import Path

import yaml

DEFAULT_FEATURES_FILE = Path.home() / ".iron" / "features.yml"

logger = logging.getLogger(__name__)

# 特性默认值表
# 已实现的功能默认 True，可选/实验性功能默认 False
DEFAULT_FEATURES = {
    # L2 内核
    "prompt_caching": True,          # P1-3: 系统提示分块缓存
    "stop_hooks": True,              # P1-2: 收敛检测器
    "progressive_compaction": True,  # P3-2: 上下文渐进压缩
    "doom_loop_detection": True,     # P1-5: doom_loop 循环检测
    # L3 工具
    "tool_search": True,             # P4-1: 工具搜索模式
    "patch_tool": True,              # P4-2: patch 工具
    "tool_truncation": True,         # P4-3: 工具输出截断
    "lsp_tools": False,             # 可选：LSP 工具（默认关闭，需要 clangd）
    # L4 权限
    "permission_rules": True,        # P2-1: DSL 驱动的权限规则
    "pre_post_hooks": True,          # P2-2: 工具执行前后 Hook
    "permission_persistence": True,  # P2-3: 三级审批持久化
    # L5 服务
    "pubsub": True,                  # P3-1: 事件总线
    "sqlite_persistence": True,      # P3-3: SQLite 持久化
    "dream_distill": True,           # v2: 记忆整理
    # L6 UI
    "markdown_rendering": True,      # P5-1: Markdown 渲染
    "theme_system": True,            # P5-2: 主题系统
    "vim_mode": False,              # 可选：Vim 模式（默认关闭）
    "command_groups": True,          # 命令分组
    # L7 长期（Phase 3）
    "code_indexer": False,           # v3.0: tree-sitter 代码索引
    "plugins": False,                # v3.0: 插件系统
    "sandbox": False,                # v3.0: OS 沙箱
    # v4.0: 通用编码能力增强（Track 5/6/7）
    "git_tools": True,               # v4.0: Git 工具集（默认启用，通用能力）
    "diff_preview": True,            # v4.0: edit_file 前 diff 预览（默认启用）
    "multi_edit": True,              # v4.0: 多文件原子编辑（默认启用）
    "metrics": True,                 # v4.0: 观测性指标采集（Track 9）
    "sub_agents": True,              # v4.0: 子 Agent 并行编排（默认启用）
    # L1 入口
    "bootstrap_pipeline": True,      # P6-1: 启动管道
    "search_mode": False,           # P4-1: 搜索模式（默认关闭，提示词超阈值才启用）
}


class FeatureFlags:
    """特性门控管理器

    管理 Iron 所有特性开关，支持默认值、用户覆盖、运行时查询。
    全局单例通过 get_feature_flags() 获取。

    用法:
        flags = FeatureFlags()
        if flags.is_enabled("prompt_caching"):
            ...
        flags.disable("vim_mode")
        flags.save()  # 持久化到 ~/.iron/features.yml
    """

    def __init__(self, config_path: Path = None):
        """初始化特性门控

        Args:
            config_path: 特性配置文件路径，默认 ~/.iron/features.yml
        """
        self.config_path = config_path or DEFAULT_FEATURES_FILE
        self._flags: dict[str, bool] = dict(DEFAULT_FEATURES)
        self._load_user_overrides()

    def _load_user_overrides(self) -> None:
        """从 YAML 文件加载用户覆盖

        文件不存在时静默返回（使用默认值）。
        文件格式错误时记录警告并回退到默认值。
        未知特性名记录警告并跳过（避免拼写错误导致意外行为）。
        """
        if not self.config_path.exists():
            return
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            if not isinstance(data, dict):
                logger.warning("特性配置文件格式错误（非字典），使用默认值")
                return
            for key, value in data.items():
                if key in self._flags and isinstance(value, bool):
                    self._flags[key] = value
                else:
                    logger.warning("未知特性或非布尔值: %s=%r", key, value)
        except (OSError, yaml.YAMLError) as e:
            logger.warning("加载特性配置失败: %s", e)

    def is_enabled(self, name: str) -> bool:
        """查询特性是否启用

        未知特性名返回 False（安全默认）。
        """
        return self._flags.get(name, False)

    def enable(self, name: str) -> bool:
        """启用特性，返回是否成功

        未知特性名返回 False（无法启用不存在的特性）。
        """
        if name not in self._flags:
            return False
        self._flags[name] = True
        return True

    def disable(self, name: str) -> bool:
        """禁用特性，返回是否成功

        未知特性名返回 False（无法禁用不存在的特性）。
        """
        if name not in self._flags:
            return False
        self._flags[name] = False
        return True

    def set(self, name: str, value: bool) -> bool:
        """设置特性状态，返回是否成功

        未知特性名返回 False。
        """
        if name not in self._flags:
            return False
        self._flags[name] = bool(value)
        return True

    def save(self) -> None:
        """保存当前特性状态到配置文件

        自动创建父目录。使用原子写入避免文件损坏。
        """
        import tempfile
        import os
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        # 原子写入：先写临时文件，再替换目标文件
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=str(self.config_path.parent), suffix=".tmp"
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                yaml.dump(
                    self._flags, f,
                    default_flow_style=False,
                    allow_unicode=True,
                )
            os.replace(tmp_path, self.config_path)
        except OSError:
            try:
                os.close(tmp_fd)
            except OSError:
                pass
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

    def list_all(self) -> dict[str, bool]:
        """列出所有特性及状态（返回副本，避免外部修改内部状态）"""
        return dict(self._flags)

    def list_enabled(self) -> list[str]:
        """列出已启用的特性名"""
        return [k for k, v in self._flags.items() if v]

    def list_disabled(self) -> list[str]:
        """列出已禁用的特性名"""
        return [k for k, v in self._flags.items() if not v]

    def reset_to_defaults(self) -> None:
        """重置为默认值（丢弃用户覆盖和运行时修改）"""
        self._flags = dict(DEFAULT_FEATURES)


# 全局单例（进程级，懒加载）
_global_flags: FeatureFlags | None = None


def get_feature_flags() -> FeatureFlags:
    """获取全局特性门控单例

    首次调用时从默认路径（~/.iron/features.yml）加载用户覆盖。
    后续调用返回同一实例（进程级缓存）。

    测试隔离：测试可通过直接设置模块级 _global_flags 或构造
    独立 FeatureFlags 实例避免单例污染。
    """
    global _global_flags
    if _global_flags is None:
        _global_flags = FeatureFlags()
    return _global_flags


def reset_global_flags() -> None:
    """重置全局单例（主要用于测试隔离）

    下次 get_feature_flags() 调用会重新加载配置文件。
    """
    global _global_flags
    _global_flags = None


def is_feature_enabled(name: str) -> bool:
    """快捷查询特性是否启用

    等价于 get_feature_flags().is_enabled(name)，
    提供更简洁的调用方式供业务代码使用。
    """
    return get_feature_flags().is_enabled(name)
