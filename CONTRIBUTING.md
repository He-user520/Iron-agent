# 贡献指南

感谢你对 Iron 嵌入式 AI Agent 项目的关注！本文档描述如何参与开发。

## 开发环境

```bash
git clone https://github.com/He-user520/Iron-agent.git
cd iron
pip install -e ".[dev]"
```

## 代码风格

项目使用 [ruff](https://github.com/astral-sh/ruff) 进行代码格式化和静态检查：

```bash
ruff format iron/ tests/
ruff check iron/ tests/ --fix
```

项目 ruff 配置见 `pyproject.toml` 的 `[tool.ruff]` 和 `[tool.ruff.lint]`：

- 行长度：100
- 目标版本：Python 3.10
- 启用规则：E/F/W/I/N/UP

## 提交规范

使用 [Conventional Commits](https://www.conventionalcommits.org/) 格式：

- `feat: 新增 XXX 功能`
- `fix: 修复 XXX 问题`
- `docs: 更新 XXX 文档`
- `refactor: 重构 XXX`
- `test: 新增/修复 XXX 测试`
- `chore: 构建/工具链变更`

## PR 流程

1. Fork 仓库并创建特性分支：`git checkout -b feat/my-feature`
2. 编写代码并补充测试（保持或提高测试覆盖率）
3. 运行测试：`pytest tests/ -v`
4. 运行代码检查：`ruff check iron/ tests/`
5. 提交 PR，描述变更内容和动机

## 测试要求

- 新功能必须配套测试
- Bug 修复必须配套回归测试
- 测试需独立（不依赖执行顺序），使用 `tmp_path` 隔离文件操作

## 版本发布

版本号遵循 [Semantic Versioning](https://semver.org/)：
- MAJOR: 不兼容的 API 变更
- MINOR: 向后兼容的新功能
- PATCH: 向后兼容的 Bug 修复

发布流程：更新 `iron/__init__.py` 和 `pyproject.toml` 版本号，更新 `ARCHITECTURE.md` changelog，打 git tag。

## 问题反馈

- Bug 报告：[GitHub Issues](https://github.com/He-user520/Iron-agent/issues)
- 安全漏洞：请勿公开 Issue，邮件至 security@iron-embedded.dev
