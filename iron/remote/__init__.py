"""iron/remote — 远程执行子包

提供本地和 SSH 远程执行器，统一抽象文件读写和命令执行。
不引入新依赖，全部通过系统预装的 ssh/scp 命令实现。
"""
from iron.remote.executor import (
    RemoteExecutor, LocalExecutor, SSHExecutor,
    parse_remote_spec, create_executor,
)

__all__ = [
    "RemoteExecutor", "LocalExecutor", "SSHExecutor",
    "parse_remote_spec", "create_executor",
]
