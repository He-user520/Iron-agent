# Track 10: tree-sitter 安装引导 + 一键启用

> **执行者**：Task D  
> **优先级**：P2  
> **依赖**：无  
> **目标**：让 V3.0 代码索引从"降级模式"变为"实战可用"

---

## 1. 背景与价值

- V3.0 代码索引实现完整，但 `tree-sitter` 未安装时降级
- 用户不知道如何启用，需要引导

### 本 Track 交付
1. `iron doctor` 增强：检测 tree-sitter + 给出安装命令
2. `iron code-indexer init` 子命令：一键安装 + 启用特性
3. `code_indexer.py` 增强：安装后自动启用，无需手动改 features.yml

---

## 2. 设计原则

1. **不强制安装**：只提示，不自动执行 `pip install`
2. **保留降级路径**：tree-sitter 不可用时仍能工作
3. **一键启用**：用户确认后自动改 features.yml
4. **Windows 兼容**：pip 命令用 `python -m pip` 形式

---

## 3. 实施步骤

### Step 1: 增强 iron doctor

**文件**：`iron/cli/main.py`（doctor 函数）

在现有检查后追加 tree-sitter 详细检测：
```python
# tree-sitter 详细检测
try:
    import tree_sitter
    import tree_sitter_c
    console.print(f"    ✓ Tree-sitter: {tree_sitter.__version__} + C")
except ImportError as e:
    missing = str(e)
    console.print(f"    ⚠ Tree-sitter: 未安装（{missing}）")
    console.print(f"      安装: python -m pip install tree_sitter tree_sitter_c")
    console.print(f"      启用: iron code-indexer init")
```

---

### Step 2: 添加 code-indexer 子命令

**文件**：`iron/cli/main.py`

新增 `code-indexer` 命令组：
```python
@cli.group(name="code-indexer")
def code_indexer_grp():
    """代码索引管理"""
    pass


@code_indexer_grp.command()
def init():
    """初始化代码索引（安装依赖 + 启用特性）"""
    import subprocess
    import sys
    console.print(f"\n  {Symbols.WRENCH} 代码索引初始化\n")

    # 步骤 1：检测/安装依赖
    try:
        import tree_sitter
        import tree_sitter_c
        console.print(f"  {Symbols.CHECK} tree-sitter 已安装")
    except ImportError:
        console.print(f"  {Symbols.INFO} 安装 tree-sitter...")
        try:
            subprocess.run(
                [sys.executable, "-m", "pip", "install",
                 "tree_sitter", "tree_sitter_c"],
                check=True,
            )
            console.print(f"  {Symbols.CHECK} tree-sitter 安装成功")
        except subprocess.CalledProcessError as e:
            console.print(f"  {Symbols.CROSS} 安装失败: {e}", style="red")
            sys.exit(1)

    # 步骤 2：启用特性
    try:
        from iron.config.features import get_feature_flags
        flags = get_feature_flags()
        flags.enable("code_indexer")
        flags.save()
        console.print(f"  {Symbols.CHECK} 特性 code_indexer=True 已启用")
    except Exception as e:
        console.print(f"  {Symbols.WARN} 启用特性失败: {e}", style="yellow")
        console.print(f"    手动编辑 ~/.iron/features.yml: code_indexer: true")

    console.print(f"\n  {Symbols.DONE} 代码索引已就绪，下次启动 iron 时生效\n")


@code_indexer_grp.command()
def status():
    """查看代码索引状态"""
    console.print(f"\n  {Symbols.WRENCH} 代码索引状态\n")
    try:
        import tree_sitter
        import tree_sitter_c
        console.print(f"  {Symbols.CHECK} tree-sitter: 已安装")
    except ImportError:
        console.print(f"  {Symbols.CROSS} tree-sitter: 未安装", style="red")

    from iron.config.features import is_feature_enabled
    enabled = is_feature_enabled("code_indexer")
    status_str = "已启用" if enabled else "未启用"
    console.print(f"  {Symbols.INFO} 特性 code_indexer: {status_str}")
    console.print()
```

---

### Step 3: 增强 CodeIndexer 初始化提示

**文件**：`iron/integrations/code_indexer.py`

在 `_check_tree_sitter` 失败时，提示安装命令：
```python
def _check_tree_sitter(self) -> bool:
    try:
        import tree_sitter  # noqa: F401
        import tree_sitter_c  # noqa: F401
        return True
    except ImportError:
        logger.info(
            "tree-sitter 未安装，代码索引降级模式。"
            "安装: python -m pip install tree_sitter tree_sitter_c"
            "启用: iron code-indexer init"
        )
        return False
```

---

### Step 4: 创建测试

**文件**：`tests/test_ts_bootstrap.py`（新建）

至少 6 个测试：
- doctor 检测 tree-sitter（mock）
- code-indexer init 流程（mock subprocess）
- code-indexer status 流程
- 特性启用成功
- 特性启用失败时友好提示
- CodeIndexer 降级提示

---

## 4. 完成标准

- [ ] iron doctor 显示 tree-sitter 详细信息 + 安装命令
- [ ] iron code-indexer init 一键安装 + 启用
- [ ] iron code-indexer status 查看状态
- [ ] CodeIndexer 降级时提示安装命令
- [ ] 6+ 测试通过
- [ ] 回归测试 0 失败

---

## 5. 风险点

1. **pip install 可能失败**：网络/权限问题，需友好错误
2. **features.yml 不存在**：save 时需先创建父目录
3. **测试隔离**：subprocess 需 mock，不能真装
