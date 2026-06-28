# Track 9: README + 用户文档 + 观测性指标

> **执行者**：Task C  
> **优先级**：P1  
> **依赖**：无  
> **目标**：补齐文档入口 + 提供基础观测性

---

## 1. 背景与价值

- Claude Code：完整文档站 + metrics + tracing + token 仪表盘
- **Iron v3.0**：有 docs/plans/ 但**无 README.md 入口**，仅 logger 无指标采集

### 本 Track 交付

**文档**：
1. `README.md`（项目根目录入口）
2. `docs/USER_GUIDE.md`（用户指南）
3. `docs/ARCHITECTURE.md`（架构说明，L1-L7 七层）

**观测性**：
1. `iron/utils/metrics.py`（指标采集器）
2. CLI 启动时显示 token 用量仪表盘
3. `/metrics` 命令查看会话指标

---

## 2. 设计原则

### 文档
1. **纯技术描述**：不营销话术，不夸张
2. **代码示例可运行**：所有示例必须能复制粘贴运行
3. **中英文混排**：中文为主，技术术语保留英文
4. **链接到源码**：用 `file:///` 协议链接到具体文件

### 观测性
1. **不阻塞主循环**：metrics 收集异步或同步快速
2. **可关闭**：通过 `features.metrics` 特性门控
3. **内存存储**：不引入 Prometheus/StatsD 等外部依赖
4. **简单 API**：`MetricsCollector.counter("tool_calls")`

---

## 3. 实施步骤

### Step 1: 创建 README.md

**文件**：`README.md`（项目根目录，新建）

内容结构：
```markdown
# Iron — 嵌入式 AI 开发 Agent CLI

> 面向 STM32/嵌入式开发的 AI 编码助手，支持代码生成、静态分析、编译烧录、LSP 智能提示

## 特性
- 5 个 Agent 类型（Coder/Task/Verify/Explore/Base）
- 28+ 工具（含 Git/MultiEdit/语义搜索/LSP/MCP）
- 4 个 LLM 后端（OpenAI/Anthropic/Ollama/Echo）
- tree-sitter 代码索引 + 调用图
- 插件系统 + Vim 模式 + 远程 SSH + OS 沙箱

## 安装
pip install -e .

## 快速开始
iron init
iron --mcu stm32f407

## 文档
- [用户指南](docs/USER_GUIDE.md)
- [架构说明](docs/ARCHITECTURE.md)
- [开发计划](docs/plans/COORDINATOR-V4.md)

## 版本
当前版本：4.0.0（见 [iron/__init__.py](file:///iron/__init__.py)）
```

---

### Step 2: 创建用户指南

**文件**：`docs/USER_GUIDE.md`（新建）

内容：
1. 安装与环境配置
2. 第一次运行（`iron init` + 配置 API Key）
3. 斜杠命令速查表（24 个命令）
4. 工具列表（28+ 工具）
5. 特性门控配置（`~/.iron/features.yml`）
6. 插件开发指南
7. 常见问题

---

### Step 3: 创建架构说明

**文件**：`docs/ARCHITECTURE.md`（新建）

内容：
1. L1-L7 七层架构图
2. 模块职责表（14 个顶层包）
3. Agent 类型与协作流程
4. 工具调用流程（engine → tool_registry → BaseTool.execute）
5. LLM 流式恢复机制（三态）
6. 特性门控设计
7. 反模式防护 8 项

---

### Step 4: 创建 metrics.py

**文件**：`iron/utils/metrics.py`（新建）

```python
"""观测性指标采集器

不引入外部依赖，内存存储，会话级。
"""
import logging
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class MetricEntry:
    """单个指标条目"""
    name: str
    value: float
    timestamp: float = field(default_factory=time.time)
    tags: dict = field(default_factory=dict)


class MetricsCollector:
    """指标采集器（线程安全单例）

    用法:
        MetricsCollector.counter("tool_calls", tags={"tool": "edit_file"})
        MetricsCollector.gauge("context_tokens", 5000)
        MetricsCollector.timing("llm_response", 2.5)
    """

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._init()
        return cls._instance

    def _init(self):
        self._counters = defaultdict(float)
        self._gauges = {}
        self._timings = defaultdict(list)
        self._lock_data = threading.Lock()

    def counter(self, name: str, value: float = 1, tags: dict = None) -> None:
        """递增计数器"""
        with self._lock_data:
            key = self._key(name, tags)
            self._counters[key] += value

    def gauge(self, name: str, value: float, tags: dict = None) -> None:
        """设置 gauge 值"""
        with self._lock_data:
            key = self._key(name, tags)
            self._gauges[key] = value

    def timing(self, name: str, seconds: float, tags: dict = None) -> None:
        """记录耗时"""
        with self._lock_data:
            key = self._key(name, tags)
            self._timings[key].append(seconds)
            # 只保留最近 100 个采样
            if len(self._timings[key]) > 100:
                self._timings[key] = self._timings[key][-100:]

    def get_summary(self) -> dict:
        """获取指标摘要"""
        with self._lock_data:
            return {
                "counters": dict(self._counters),
                "gauges": dict(self._gauges),
                "timings": {
                    k: {
                        "count": len(v),
                        "avg": sum(v) / len(v) if v else 0,
                        "min": min(v) if v else 0,
                        "max": max(v) if v else 0,
                    }
                    for k, v in self._timings.items()
                },
            }

    def reset(self) -> None:
        """重置所有指标"""
        with self._lock_data:
            self._counters.clear()
            self._gauges.clear()
            self._timings.clear()

    def _key(self, name: str, tags: dict = None) -> str:
        if not tags:
            return name
        tag_str = ",".join(f"{k}={v}" for k, v in sorted(tags.items()))
        return f"{name}|{tag_str}"


# 全局单例便捷函数
def counter(name: str, value: float = 1, tags: dict = None) -> None:
    MetricsCollector().counter(name, value, tags)


def gauge(name: str, value: float, tags: dict = None) -> None:
    MetricsCollector().gauge(name, value, tags)


def timing(name: str, seconds: float, tags: dict = None) -> None:
    MetricsCollector().timing(name, seconds, tags)


def get_summary() -> dict:
    return MetricsCollector().get_summary()
```

---

### Step 5: 在 engine.py 集成 metrics

**文件**：`iron/agent/engine.py`

在工具执行后采集指标：
```python
# 工具执行成功后
try:
    from iron.utils.metrics import counter, timing
    counter("tool_calls", tags={"tool": tool_name, "status": "success"})
    timing("tool_duration", elapsed, tags={"tool": tool_name})
except ImportError:
    pass
```

在 LLM 流式完成后：
```python
try:
    from iron.utils.metrics import counter, timing, gauge
    counter("llm_calls")
    timing("llm_response", elapsed)
    gauge("context_tokens", current_tokens)
except ImportError:
    pass
```

---

### Step 6: 添加 /metrics 命令

**文件**：`iron/cli/commands/metrics_cmds.py`（新建）

```python
"""/metrics 命令 — 显示会话指标"""
from iron.cli.theme import Symbols
from rich.console import Console


def handle_metrics_commands(cmd: str, args: str, ctx: dict) -> bool:
    if cmd != "/metrics":
        return False
    console: Console = ctx.get("console") or Console()
    try:
        from iron.utils.metrics import get_summary
        summary = get_summary()
        console.print(f"\n  {Symbols.WRENCH} 会话指标\n")
        if summary["counters"]:
            console.print("  [bold]计数器:[/bold]")
            for k, v in summary["counters"].items():
                console.print(f"    {k}: {v}")
        if summary["gauges"]:
            console.print("  [bold]Gauge:[/bold]")
            for k, v in summary["gauges"].items():
                console.print(f"    {k}: {v}")
        if summary["timings"]:
            console.print("  [bold]耗时:[/bold]")
            for k, v in summary["timings"].items():
                console.print(f"    {k}: avg={v['avg']:.3f}s, "
                              f"min={v['min']:.3f}s, max={v['max']:.3f}s")
        console.print()
    except ImportError:
        console.print(f"\n  {Symbols.WARN} metrics 模块未加载\n",
                      style="yellow")
    return True
```

**文件**：`iron/cli/main.py`

1. `SLASH_COMMANDS` 新增 `"/metrics"`
2. `NON_CHAT_COMMANDS` 新增 `"/metrics"`
3. `_dispatch_slash_command` 新增 `elif handle_metrics_commands(cmd, args, cmd_ctx): pass`

---

### Step 7: 注册特性 + 测试

**文件**：`iron/config/features.py`

```python
"metrics": True,  # v4.0: 观测性指标
```

**文件**：`tests/test_metrics.py`（新建）

至少 8 个测试：
- counter 递增
- gauge 设置
- timing 记录 + 统计
- 线程安全
- reset 清空
- 单例
- tags 区分
- get_summary 结构

---

## 4. 完成标准

- [ ] README.md 创建
- [ ] docs/USER_GUIDE.md 创建
- [ ] docs/ARCHITECTURE.md 创建
- [ ] iron/utils/metrics.py 实现
- [ ] engine.py 集成 metrics 采集
- [ ] /metrics 命令可用
- [ ] 8+ 测试通过
- [ ] 回归测试 0 失败

---

## 5. 风险点

1. **README 不能有过时信息**：版本号、命令数等需与代码同步
2. **metrics 性能**：高频调用（如每个 chunk）可能影响流式性能，需采样
3. **单例测试隔离**：测试间需 reset 单例，避免污染
