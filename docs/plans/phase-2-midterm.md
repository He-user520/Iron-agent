# Phase 2 · 中期开发文档（v2.8.0）

> 本文档是 Iron CLI V3.0 计划的 Phase 2 执行级子计划。
> 基线版本：v2.6.0（Phase 1 完成，760 passed, 1 skipped）
> 目标版本：v2.8.0
> 执行方式：单人串行开发（主会话）

---

## 1. 总体目标

将 Iron CLI 从"工具集合"升级为"可扩展的 Agent 平台"：
- Skills 从 prompt 注入升级为可执行机制（支持工具注册、预处理）
- 引入向量语义搜索（替代 LIKE 查询）
- MCP 客户端增加健康检查与断线重连
- 补齐测试缺口（LSP e2e / 缓存命中 / Windows symlink）

## 2. 任务清单

| 任务 | 优先级 | 预计复杂度 | 依赖 |
|------|--------|-----------|------|
| 2.1 Skills 可执行机制 | P1 | 高 | 无 |
| 2.2 向量语义搜索 | P2 | 高 | 无 |
| 2.3 MCP 健康检查 | P2 | 中 | 无 |
| 2.4 测试缺口补齐 | P2 | 低 | 无 |

执行顺序：2.1 → 2.2 → 2.3 → 2.4

---

## 3. 任务 2.1 · Skills 可执行机制

### 3.1 目标

将 8 个内置 Skill 从纯 prompt 注入升级为可执行机制：
- 支持 Skill 注册工具到 ToolRegistry
- 支持 pre_execute（LLM 调用前预处理）和 post_execute（LLM 调用后处理）
- 保持 PromptSkill 向后兼容（现有 .md skill 仍工作）

### 3.2 设计

#### 3.2.1 ExecutableSkill 抽象基类

```python
# iron/skills/base.py 新增

class ExecutableSkill(BaseSkill):
    """可执行 Skill — 支持注册工具、预处理、后处理"""
    
    def get_tools(self) -> list:
        """返回此 Skill 注册的工具列表（可为空）"""
        return []
    
    async def pre_execute(self, context: 'SkillContext') -> 'SkillResult':
        """预处理：在 LLM 调用前执行"""
        return SkillResult(success=True)
    
    async def post_execute(self, context: 'SkillContext', result: Any) -> 'SkillResult':
        """后处理：在 LLM 调用后执行"""
        return SkillResult(success=True)
    
    def build_prompt(self, context: 'SkillContext') -> str:
        """仍支持 prompt 注入（向后兼容）"""
        return ""
```

#### 3.2.2 SkillContext 数据类

```python
@dataclass
class SkillContext:
    """Skill 执行上下文 — 受控访问 engine 状态"""
    user_input: str
    project_root: str
    tool_registry: Any  # ToolRegistry 引用
    llm: Any           # LLMBackend 引用
    lsp_client: Any    # LSPClient 引用（可为 None）
    session_data: dict  # 会话级数据（Skill 间共享）
```

#### 3.2.3 改造 4 个内置 Skill 为可执行

- **mcu-init**：`pre_execute` 收集 MCU 型号，`get_tools` 返回 MCUInitTool
- **driver-gen**：`pre_execute` 读取 target-mcu.md，`get_tools` 返回 DriverGenTool
- **bug-hunt**：`pre_execute` 调用 LSP 诊断，`get_tools` 返回 BugHuntTool
- **misra-check**：`pre_execute` 调用 EmbedGuard，`get_tools` 返回 MisraCheckTool

其他 4 个保持 PromptSkill（peripheral-setup/rtos-setup/power-optimize/debug-helper）

### 3.3 涉及文件

| 文件 | 改动 |
|------|------|
| iron/skills/base.py | 新增 ExecutableSkill + SkillContext |
| iron/skills/registry.py | match() 返回 (skill, score)，支持可执行 Skill |
| iron/agent/engine.py | __init__ 注册 Skill 工具，process() 调用 pre/post_execute |
| iron/skills/executable.py | **新增** 4 个 ExecutableSkill 子类 |
| iron/tools/skill_tools.py | **新增** 4 个 Skill 专属工具 |
| tests/test_skills_executable.py | **新增** ≥ 15 个测试 |

### 3.4 实施步骤

1. 在 base.py 新增 SkillContext + ExecutableSkill
2. 新建 executable.py，实现 4 个可执行 Skill
3. 新建 skill_tools.py，实现 4 个专属工具
4. 改造 registry.py 的 match() 支持混合匹配
5. 改造 engine.py 的 _init_session 注册 Skill 工具 + 调用 pre/post_execute
6. 编写测试

### 3.5 验证清单

- [ ] ExecutableSkill 抽象基类定义
- [ ] 4 个内置 Skill 改造为可执行
- [ ] PromptSkill 向后兼容（.md skill 仍工作）
- [ ] pytest tests/test_skills_executable.py 全绿
- [ ] pytest tests/test_core.py 全绿（原有不回归）
- [ ] grep pre_execute in engine.py 命中

### 3.6 反模式防护

- pre_execute 不能阻塞超过 5 秒（必须有超时）
- 不能在 Skill 中直接修改 messages 列表（必须通过 SkillContext）
- 不能破坏 PromptSkill 向后兼容

---

## 4. 任务 2.2 · 向量语义搜索

### 4.1 目标

为历史会话和 MEMORY.md 增加向量语义搜索，替代当前 LIKE 查询。

### 4.2 设计

#### 4.2.1 向量后端选择

**方案 A（推荐）**：纯 Python 余弦相似度（无外部依赖）
- 用 numpy 计算余弦相似度
- embedding 存储为 BLOB（序列化的 numpy 数组）
- 优势：零外部依赖，Windows 兼容性好

**方案 B**：sqlite-vec 扩展
- 优势：性能更好
- 劣势：Windows 安装复杂，可能不可用

**决策**：用方案 A（纯 Python），失败时降级到关键词搜索

#### 4.2.2 Embedding 接口

```python
# iron/llm/backend.py 新增

class LLMBackend(ABC):
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """生成文本向量（默认实现抛 NotImplementedError）
        
        子类可覆盖：
        - OpenAIBackend: 调用 /v1/embeddings
        - EchoBackend: 返回哈希伪向量（测试用）
        """
        raise NotImplementedError("此后端不支持 embedding")
```

#### 4.2.3 数据库迁移

```sql
-- 002_add_embeddings.sql
ALTER TABLE messages ADD COLUMN embedding BLOB DEFAULT NULL;
ALTER TABLE history ADD COLUMN embedding BLOB DEFAULT NULL;

CREATE TABLE embedding_meta (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_name TEXT NOT NULL,
    dimension INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
```

#### 4.2.4 混合检索策略

关键词搜索（LIKE）+ 语义搜索（向量）融合排序：
- 权重：关键词 0.3 + 语义 0.7（可配置）
- 无 embedding 时降级到纯关键词搜索

### 4.3 涉及文件

| 文件 | 改动 |
|------|------|
| iron/core/migrations/002_add_embeddings.sql | **新增**迁移 |
| iron/core/db.py | 新增 search_semantic() + 保存 embedding |
| iron/llm/backend.py | 新增 embed() 接口 |
| iron/llm/openai_backend.py | 实现 embed()（调用 /v1/embeddings）|
| iron/agent/memory.py | MEMORY.md 向量化 |
| tests/test_vector_search.py | **新增** ≥ 10 个测试 |

### 4.4 实施步骤

1. 新建 002_add_embeddings.sql 迁移
2. 在 LLMBackend 新增 embed() 抽象方法
3. OpenAIBackend 实现 embed()（/v1/embeddings 端点）
4. EchoBackend 实现 embed()（伪向量，测试用）
5. Database 新增 search_semantic() + _save_embedding()
6. save_message 异步生成 embedding（fire-and-forget）
7. ProjectMemory.append_to_memory 后向量化
8. 编写测试

### 4.5 验证清单

- [ ] 002_add_embeddings.sql 存在且自动迁移
- [ ] grep embed in backend.py 命中
- [ ] grep search_semantic in db.py 命中
- [ ] pytest tests/test_vector_search.py 全绿
- [ ] pytest tests/test_db.py 全绿（原有不回归）
- [ ] LLM 不可用时降级到关键词搜索

### 4.6 反模式防护

- 不能在 LLM 不可用时阻塞消息保存
- 不能为已有消息批量回填（仅新消息生成 embedding）
- 不能引入闭源 embedding 模型为默认

---

## 5. 任务 2.3 · MCP 健康检查与断线重连

### 5.1 目标

为 MCP 客户端增加主动健康检查，避免连接断开后工具调用失败。

### 5.2 设计

#### 5.2.1 健康检查机制

```python
class MCPClient:
    async def health_check(self) -> bool:
        """检查所有服务器连接健康状态"""
        for name, server in self._servers.items():
            if not await self._ping_server(name):
                await self._reconnect(name)
        return all(s.healthy for s in self._servers.values())
```

#### 5.2.2 断线重连

- stdio 传输：重启子进程
- SSE/HTTP 传输：重新建立连接
- 重连失败 3 次后标记 disconnected，发射 mcp_disconnected 事件

### 5.3 涉及文件

| 文件 | 改动 |
|------|------|
| iron/mcp/client.py | 新增 health_check() + _reconnect() |
| tests/test_mcp_client.py | 新增 ≥ 5 个测试 |

### 5.4 验证清单

- [ ] grep health_check in client.py 命中
- [ ] pytest tests/test_mcp_client.py 全绿
- [ ] 断线后自动重连成功

---

## 6. 任务 2.4 · 测试缺口补齐

### 6.1 目标

补齐 evaluation-v3.md 标记的测试缺口。

### 6.2 新增测试

| 文件 | 内容 |
|------|------|
| tests/test_prompt_cache_hit_rate.py | 缓存命中率测试 |
| tests/test_windows_symlink.py | Windows symlink 路径穿越 |

注：LSP e2e 已在 Track 4 中完成（test_lsp_integration.py）

### 6.3 验证清单

- [ ] 2 个新测试文件存在
- [ ] pytest tests/ 总数 ≥ 780 passed

---

## 7. 最终验证

### 7.1 全量测试

```bash
pytest tests/ -v
# 目标：≥ 800 passed, 0 failed
```

### 7.2 反模式 grep 检查

- grep pre_execute in engine.py 命中
- grep search_semantic in db.py 命中
- grep health_check in client.py 命中
- 002_add_embeddings.sql 存在

### 7.3 版本号更新

- iron/__init__.py: 2.5.0 → 2.8.0
- pyproject.toml: 2.5.0 → 2.8.0
