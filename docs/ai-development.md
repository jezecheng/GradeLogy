# 使用 Trae、Claude Code 或 Codex 继续开发

本项目通过字节跳动 Trae 工具协同开发完成。欢迎 Fork 后继续增强，也可以使用其他 AI 编程工具。工具只负责辅助阅读和修改代码，**需求确认、代码审查、测试和数据安全仍由开发者负责**。

## 三种工具的基本用法

### 字节跳动 Trae

官网：<https://www.trae.cn/>

1. 用 Trae 打开 Fork 后的项目文件夹。
2. 在对话或 Builder 中描述需求，要求它先阅读 `README.md`、相关 `docs/*.md` 和涉及的代码，再提出修改。
3. 修改完成后查看 Diff，让它解释关键变更并运行相关验证。
4. 确认无误后，由你决定是否提交和推送。

适合希望在图形界面中持续对话、查看文件修改和运行项目的人。

### Claude Code

官方文档：<https://docs.anthropic.com/en/docs/claude-code/overview>

1. 按官方文档完成安装和登录。
2. 在终端进入项目目录，启动 `claude`。
3. 先让它分析项目结构和相关实现，再给出具体需求。
4. 对涉及数据库、权限、AI Key 或部署的修改，要求它先说明影响范围，并检查最终 Diff。

### OpenAI Codex

官方项目：<https://github.com/openai/codex>

1. 按官方说明安装并登录 Codex。
2. 在终端进入项目目录，启动 `codex`。
3. 将需求、限制条件和验收标准一起提供。
4. 让它运行针对性检查，但不要在未审查的情况下允许其推送 `main` 或操作生产服务器。

## 推荐的二次开发流程

1. **Fork 并创建功能分支**，例如 `feature/export-grades`。
2. **先读文档再改代码**，重点了解角色权限、SQLite 数据结构、文件存储和部署方式。
3. **一次只完成一个明确需求**，避免让 AI 顺手重构无关模块。
4. **本地验证**，至少覆盖相关页面、异常情况和 `/healthz`；涉及账号时分别测试 admin、teacher、student。
5. **审查变更**，确认没有密钥、学生数据、数据库或上传文件被加入 Git。
6. **通过 Pull Request 合并到 `main`**。本项目 `main` 是发布分支，合并可能触发自动部署。

本地开发环境可按下面的方式准备：

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows PowerShell 使用 .venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

访问 `http://127.0.0.1:5000`，健康检查为 `http://127.0.0.1:5000/healthz`。部署到本机或腾讯云时，分别参考 `docs/deploy-standalone.md` 和 `docs/deploy-tencent.md`。

## 可直接复用的任务提示词

```text
你正在维护 GradeLogy，一个 Flask 3 + SQLite + Jinja2 的教学管理系统。

先阅读 README.md、相关 docs 文档和将涉及的代码，不要立即修改。
本次需求是：<在这里填写一个明确需求>
验收标准是：<在这里填写可检查的结果>

约束：
1. 保持现有单体架构，不引入前端框架或无关依赖。
2. 不改变 admin / teacher / student 的权限边界。
3. 涉及数据库时考虑已有数据迁移和备份恢复，不删除用户数据。
4. 不修改与需求无关的代码，不提交 .env、data/、上传件或任何密钥。
5. 完成后说明修改了哪些文件、为什么，并运行针对性验证。
6. 不要自行推送 main，也不要操作生产服务器。

先给出简短实现思路，等待我确认后再修改。
```

## 需要特别审查的改动

- **数据库**：是否兼容旧数据，恢复旧备份后是否仍能启动。
- **权限**：是否可能让学生看到其他学生或教师、管理员的数据。
- **文件与隐私**：学生 PDF、日志、API Key 是否可能进入 Git、镜像或公开日志。
- **部署**：重建后 `data/` 是否仍然持久化，健康检查是否通过。
- **AI 提示词**：输出格式是否仍可解析，是否会把标准答案误当成学生作答。

如果 AI 工具的解释与实际 Diff 不一致，以代码和测试结果为准，不要直接接受修改。
