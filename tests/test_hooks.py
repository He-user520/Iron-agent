"""Tool Hooks 单元测试 — P2-2 PreToolUse/PostToolUse Hooks

覆盖：
- HookResult 数据类
- PreToolUseHook / PostToolUseHook 基类
- HookManager 加载、注册、调度逻辑
- 内置 SafetyCheckHook / AuditLogHook
- 异步与同步 hook 双兼容
- 用户脚本从目录加载
- _ 开头文件被忽略
- hook 执行顺序

运行方式: pytest tests/test_hooks.py -v
"""
import textwrap

import pytest

from iron.agent.hooks import (
    HookManager,
    HookResult,
    PreToolUseHook,
    PostToolUseHook,
    SafetyCheckHook,
    AuditLogHook,
)


# ── 1. HookResult 数据类测试 ────────────────────────────────


class TestHookResult:
    """HookResult 默认值与字段"""

    def test_hook_result_allow(self):
        """默认 action=allow"""
        r = HookResult()
        assert r.action == "allow"
        assert r.modified_args is None
        assert r.modified_result is None
        assert r.reason == ""

    def test_hook_result_deny(self):
        """deny 携带 reason"""
        r = HookResult(action="deny", reason="阻止危险操作")
        assert r.action == "deny"
        assert r.reason == "阻止危险操作"

    def test_hook_result_modify(self):
        """modify 携带 modified_args"""
        new_args = {"path": "new.c", "content": "modified"}
        r = HookResult(action="modify", modified_args=new_args)
        assert r.action == "modify"
        assert r.modified_args == new_args


# ── 2. PreToolUseHook / PostToolUseHook 基类测试 ────────────


class TestPreHookBasic:
    """PreToolUseHook 基类"""

    async def test_pre_hook_basic(self):
        """基本 pre hook：默认返回 allow"""
        hook = PreToolUseHook()
        result = await hook.before("write_file", {"path": "a.c"})
        assert result.action == "allow"

    async def test_pre_hook_subclass(self):
        """子类覆盖 before 返回 deny"""
        class MyHook(PreToolUseHook):
            async def before(self, tool_name, args):
                return HookResult(action="deny", reason="forbidden")

        hook = MyHook()
        result = await hook.before("write_file", {})
        assert result.action == "deny"
        assert result.reason == "forbidden"


class TestPostHookBasic:
    """PostToolUseHook 基类"""

    async def test_post_hook_basic(self):
        """基本 post hook：默认原样返回 result"""
        hook = PostToolUseHook()
        original = {"success": True, "data": "ok"}
        result = await hook.after("read_file", {}, original)
        assert result is original  # 默认原样返回

    async def test_post_hook_subclass_modify(self):
        """子类覆盖 after 修改 result"""
        class MyHook(PostToolUseHook):
            async def after(self, tool_name, args, result):
                result = dict(result)
                result["modified"] = True
                return result

        hook = MyHook()
        original = {"success": True, "data": "ok"}
        result = await hook.after("read_file", {}, original)
        assert result["modified"] is True
        assert result["success"] is True


# ── 3. HookManager 基础测试 ─────────────────────────────────


class TestHookManagerAdd:
    """HookManager 注册与计数"""

    def test_hook_manager_add(self):
        """add_pre_hook / add_post_hook / hook_count"""
        manager = HookManager()
        assert manager.hook_count() == (0, 0)

        manager.add_pre_hook(PreToolUseHook())
        manager.add_pre_hook(PreToolUseHook())
        manager.add_post_hook(PostToolUseHook())
        assert manager.hook_count() == (2, 1)

    def test_hook_manager_add_none_ignored(self):
        """add None 不崩溃也不增加计数"""
        manager = HookManager()
        manager.add_pre_hook(None)
        manager.add_post_hook(None)
        assert manager.hook_count() == (0, 0)

    def test_hook_manager_clear(self):
        """clear_hooks 清空所有 hooks"""
        manager = HookManager()
        manager.add_pre_hook(PreToolUseHook())
        manager.add_post_hook(PostToolUseHook())
        assert manager.hook_count() == (1, 1)

        manager.clear_hooks()
        assert manager.hook_count() == (0, 0)


# ── 4. HookManager 执行测试 ─────────────────────────────────


class _DenyHook(PreToolUseHook):
    """总是 deny 的 hook"""

    def __init__(self, reason: str = "blocked"):
        self.reason = reason

    async def before(self, tool_name, args):
        return HookResult(action="deny", reason=self.reason)


class _ModifyHook(PreToolUseHook):
    """修改 args 的 hook"""

    def __init__(self, new_args: dict):
        self.new_args = new_args

    async def before(self, tool_name, args):
        return HookResult(action="modify", modified_args=self.new_args)


class _AllowHook(PreToolUseHook):
    """总是 allow 的 hook"""

    async def before(self, tool_name, args):
        return HookResult(action="allow")


class _AddFieldHook(PostToolUseHook):
    """给 result 加字段的 hook"""

    def __init__(self, field_name: str = "tagged", value=True):
        self.field_name = field_name
        self.value = value

    async def after(self, tool_name, args, result):
        new_result = dict(result) if isinstance(result, dict) else {"value": result}
        new_result[self.field_name] = self.value
        return new_result


class TestHookManagerPreExec:
    """HookManager.run_pre_hooks 执行逻辑"""

    async def test_hook_manager_pre_allow(self):
        """全部 allow → 返回 allow"""
        manager = HookManager()
        manager.add_pre_hook(_AllowHook())
        manager.add_pre_hook(_AllowHook())
        result = await manager.run_pre_hooks("write_file", {"path": "a.c"})
        assert result.action == "allow"
        assert result.modified_args is None

    async def test_hook_manager_pre_deny(self):
        """deny 阻止后续 hook 执行"""
        manager = HookManager()
        # 第一个 hook deny → 第二个不应执行
        manager.add_pre_hook(_DenyHook(reason="forbidden"))
        # 第二个 hook 会抛异常如果被调用（用于验证未执行）
        class _Tracker(PreToolUseHook):
            called = False
            async def before(self, tool_name, args):
                _Tracker.called = True
                return HookResult(action="allow")
        tracker = _Tracker()
        manager.add_pre_hook(tracker)
        result = await manager.run_pre_hooks("write_file", {"path": "a.c"})
        assert result.action == "deny"
        assert result.reason == "forbidden"
        assert _Tracker.called is False  # 第二个 hook 未被执行

    async def test_hook_manager_pre_modify(self):
        """modify 修改 args 给后续 hook"""
        manager = HookManager()
        new_args = {"path": "modified.c", "content": "new"}
        manager.add_pre_hook(_ModifyHook(new_args))

        # 第二个 hook 检查接收到的 args 是否被修改
        class _CheckArgs(PreToolUseHook):
            received = None
            async def before(self, tool_name, args):
                _CheckArgs.received = args
                return HookResult(action="allow")
        checker = _CheckArgs()
        manager.add_pre_hook(checker)

        result = await manager.run_pre_hooks("write_file", {"path": "a.c"})
        # 最终结果应是 modify（args 被改过）
        assert result.action == "modify"
        assert result.modified_args == new_args
        # 第二个 hook 收到了修改后的 args
        assert _CheckArgs.received == new_args


class TestHookManagerPostExec:
    """HookManager.run_post_hooks 执行逻辑"""

    async def test_hook_manager_post_no_hooks(self):
        """无 hook 时原样返回"""
        manager = HookManager()
        original = {"success": True}
        result = await manager.run_post_hooks("read_file", {}, original)
        assert result is original

    async def test_hook_manager_post_modify(self):
        """post hook 修改 result"""
        manager = HookManager()
        manager.add_post_hook(_AddFieldHook(field_name="tagged", value=True))
        original = {"success": True, "data": "ok"}
        result = await manager.run_post_hooks("read_file", {}, original)
        assert result["tagged"] is True
        assert result["success"] is True
        assert result["data"] == "ok"

    async def test_hook_manager_post_chain(self):
        """多个 post hook 链式应用"""
        manager = HookManager()
        manager.add_post_hook(_AddFieldHook(field_name="a", value=1))
        manager.add_post_hook(_AddFieldHook(field_name="b", value=2))
        original = {"success": True}
        result = await manager.run_post_hooks("read_file", {}, original)
        assert result["a"] == 1
        assert result["b"] == 2
        assert result["success"] is True


# ── 5. 同步/异步双兼容测试 ──────────────────────────────────


class TestSyncAsyncCompat:
    """Hook 同步方法也应工作（manager 用 inspect.iscoroutine 包装）"""

    async def test_sync_pre_hook(self):
        """同步 before 方法"""
        class SyncHook(PreToolUseHook):
            # 注意：覆盖基类的 async before 用同步实现
            def before(self, tool_name, args):  # type: ignore[override]
                return HookResult(action="deny", reason="sync-deny")

        manager = HookManager()
        manager.add_pre_hook(SyncHook())
        result = await manager.run_pre_hooks("write_file", {})
        assert result.action == "deny"
        assert result.reason == "sync-deny"

    async def test_sync_post_hook(self):
        """同步 after 方法"""
        class SyncPost(PostToolUseHook):
            def after(self, tool_name, args, result):  # type: ignore[override]
                result = dict(result)
                result["sync"] = True
                return result

        manager = HookManager()
        manager.add_post_hook(SyncPost())
        result = await manager.run_post_hooks("read_file", {}, {"success": True})
        assert result["sync"] is True


# ── 6. load_hooks_from_dir 测试 ─────────────────────────────


class TestHookManagerLoadDir:
    """从目录加载用户 hook 脚本"""

    async def test_hook_manager_load_dir(self, tmp_path):
        """成功加载目录内的 hook 脚本"""
        # 创建一个 hook 脚本
        hook_script = textwrap.dedent("""
            from iron.agent.hooks import PreToolUseHook, PostToolUseHook, HookResult

            class MyPre(PreToolUseHook):
                async def before(self, tool_name, args):
                    return HookResult(action="deny", reason="loaded")

            class MyPost(PostToolUseHook):
                async def after(self, tool_name, args, result):
                    result = dict(result)
                    result["loaded"] = True
                    return result

            # 顶层实例化（约定：扫描模块顶层属性收集实例）
            my_pre = MyPre()
            my_post = MyPost()
        """)
        (tmp_path / "my_hook.py").write_text(hook_script, encoding="utf-8")

        manager = HookManager()
        loaded = manager.load_hooks_from_dir(tmp_path)
        assert loaded == 2  # 1 pre + 1 post
        assert manager.hook_count() == (1, 1)

        # 验证 pre hook 实际生效
        result = await manager.run_pre_hooks("write_file", {})
        assert result.action == "deny"
        assert result.reason == "loaded"

        # 验证 post hook 实际生效
        result = await manager.run_post_hooks("read_file", {}, {"success": True})
        assert result["loaded"] is True

    def test_hook_manager_load_ignored(self, tmp_path):
        """_ 开头的文件被忽略"""
        # _private.py 应被忽略
        (tmp_path / "_private.py").write_text(
            "from iron.agent.hooks import PreToolUseHook\n"
            "hook = PreToolUseHook()\n",
            encoding="utf-8",
        )
        # normal.py 应被加载
        (tmp_path / "normal.py").write_text(
            "from iron.agent.hooks import PreToolUseHook, HookResult\n"
            "class H(PreToolUseHook):\n"
            "    async def before(self, n, a):\n"
            "        return HookResult(action='allow')\n"
            "h = H()\n",
            encoding="utf-8",
        )

        manager = HookManager()
        loaded = manager.load_hooks_from_dir(tmp_path)
        assert loaded == 1  # 只加载 normal.py
        assert manager.hook_count() == (1, 0)

    def test_hook_manager_load_nonexistent_dir(self, tmp_path):
        """不存在的目录返回 0"""
        manager = HookManager()
        loaded = manager.load_hooks_from_dir(tmp_path / "nonexistent")
        assert loaded == 0
        assert manager.hook_count() == (0, 0)

    def test_hook_manager_load_broken_script(self, tmp_path):
        """加载失败的单个脚本不影响其他脚本"""
        # broken.py 有语法错误
        (tmp_path / "broken.py").write_text(
            "def broken(:\n    pass\n", encoding="utf-8"
        )
        # good.py 正常
        (tmp_path / "good.py").write_text(
            "from iron.agent.hooks import PreToolUseHook, HookResult\n"
            "class G(PreToolUseHook):\n"
            "    async def before(self, n, a):\n"
            "        return HookResult(action='allow')\n"
            "g = G()\n",
            encoding="utf-8",
        )

        manager = HookManager()
        loaded = manager.load_hooks_from_dir(tmp_path)
        # broken 失败但 good 仍被加载
        assert loaded == 1
        assert manager.hook_count() == (1, 0)


# ── 7. 内置 hooks 测试 ─────────────────────────────────────


class TestSafetyCheckHook:
    """SafetyCheckHook — 阻止 rm -rf"""

    async def test_safety_check_blocks_rm_rf_root(self):
        """rm -rf / 被阻止"""
        hook = SafetyCheckHook()
        result = await hook.before("run_command", {"command": "rm -rf /"})
        assert result.action == "deny"
        assert "rm -rf" in result.reason

    async def test_safety_check_blocks_rm_rf_home(self):
        """rm -rf ~ 被阻止"""
        hook = SafetyCheckHook()
        result = await hook.before("run_command", {"command": "rm -rf ~"})
        assert result.action == "deny"

    async def test_safety_check_allows_safe_command(self):
        """安全命令放行"""
        hook = SafetyCheckHook()
        result = await hook.before("run_command", {"command": "ls -la"})
        assert result.action == "allow"

    async def test_safety_check_ignores_non_run_command(self):
        """非 run_command 工具不检查"""
        hook = SafetyCheckHook()
        # write_file 即使内容含 rm -rf 也不阻止（不是 run_command）
        result = await hook.before("write_file", {
            "path": "a.sh", "content": "rm -rf /"
        })
        assert result.action == "allow"

    async def test_safety_check_no_command_arg(self):
        """args 无 command 字段不崩溃"""
        hook = SafetyCheckHook()
        result = await hook.before("run_command", {})
        assert result.action == "allow"


class TestAuditLogHook:
    """AuditLogHook — 记录所有工具调用"""

    async def test_audit_log_records_call(self):
        """记录工具调用"""
        hook = AuditLogHook()
        assert hook.log == []
        await hook.after("write_file", {"path": "a.c"}, {"success": True})
        assert len(hook.log) == 1
        entry = hook.log[0]
        assert entry["tool"] == "write_file"
        assert entry["args"] == {"path": "a.c"}
        assert entry["success"] is True

    async def test_audit_log_records_failure(self):
        """记录失败调用"""
        hook = AuditLogHook()
        await hook.after("run_command", {"command": "bad"},
                         {"success": False, "error": "exit code 1"})
        assert len(hook.log) == 1
        assert hook.log[0]["success"] is False

    async def test_audit_log_does_not_modify_result(self):
        """不修改 result（仅记录）"""
        hook = AuditLogHook()
        original = {"success": True, "data": "ok"}
        result = await hook.after("read_file", {}, original)
        assert result is original  # 原样返回

    async def test_audit_log_handles_none_result(self):
        """result 为 None 时不崩溃"""
        hook = AuditLogHook()
        result = await hook.after("read_file", {}, None)
        # 原样返回 None
        assert result is None
        # 但仍记录（success=True，默认值）
        assert len(hook.log) == 1


# ── 8. Hook 执行顺序测试 ───────────────────────────────────


class TestHookOrder:
    """Hook 按注册顺序执行"""

    async def test_pre_hook_order(self):
        """pre hooks 按注册顺序执行"""
        manager = HookManager()
        order = []

        class _Hook(PreToolUseHook):
            def __init__(self, name):
                self.name = name
            async def before(self, tool_name, args):
                order.append(self.name)
                return HookResult(action="allow")

        manager.add_pre_hook(_Hook("first"))
        manager.add_pre_hook(_Hook("second"))
        manager.add_pre_hook(_Hook("third"))

        await manager.run_pre_hooks("write_file", {})
        assert order == ["first", "second", "third"]

    async def test_post_hook_order(self):
        """post hooks 按注册顺序执行，result 链式传递"""
        manager = HookManager()
        order = []

        class _Hook(PostToolUseHook):
            def __init__(self, name):
                self.name = name
            async def after(self, tool_name, args, result):
                order.append(self.name)
                # 每个 hook 给 result 加上自己的 name
                result = dict(result)
                result[self.name] = True
                return result

        manager.add_post_hook(_Hook("first"))
        manager.add_post_hook(_Hook("second"))
        manager.add_post_hook(_Hook("third"))

        result = await manager.run_post_hooks("read_file", {}, {"success": True})
        assert order == ["first", "second", "third"]
        # 链式：每个 hook 都看到了前一个修改过的 result
        assert result["first"] is True
        assert result["second"] is True
        assert result["third"] is True

    async def test_pre_hook_deny_short_circuits(self):
        """deny 短路：后续 hook 不执行"""
        manager = HookManager()
        executed = []

        class _Hook(PreToolUseHook):
            def __init__(self, name, action):
                self.name = name
                self.action = action
            async def before(self, tool_name, args):
                executed.append(self.name)
                return HookResult(action=self.action)

        manager.add_pre_hook(_Hook("first", "allow"))
        manager.add_pre_hook(_Hook("second", "deny"))
        manager.add_pre_hook(_Hook("third", "allow"))  # 不应执行

        result = await manager.run_pre_hooks("write_file", {})
        assert result.action == "deny"
        assert executed == ["first", "second"]  # third 未执行


# ── 9. 异常处理测试 ─────────────────────────────────────────


class TestHookExceptionHandling:
    """Hook 异常不影响主流程"""

    async def test_pre_hook_exception_does_not_block(self):
        """pre hook 抛异常时按 allow 处理"""
        class _Broken(PreToolUseHook):
            async def before(self, tool_name, args):
                raise RuntimeError("broken hook")

        manager = HookManager()
        manager.add_pre_hook(_Broken())
        manager.add_pre_hook(_AllowHook())  # 异常后此 hook 应正常执行
        result = await manager.run_pre_hooks("write_file", {})
        # broken hook 异常被吞掉，allow hook 执行后返回 allow
        assert result.action == "allow"

    async def test_post_hook_exception_does_not_block(self):
        """post hook 抛异常时不影响其他 hook"""
        class _Broken(PostToolUseHook):
            async def after(self, tool_name, args, result):
                raise RuntimeError("broken post")

        manager = HookManager()
        manager.add_post_hook(_Broken())
        manager.add_post_hook(_AddFieldHook(field_name="ok", value=True))

        original = {"success": True}
        result = await manager.run_post_hooks("read_file", {}, original)
        # broken hook 异常被吞掉，第二个 hook 仍执行
        assert result["ok"] is True
