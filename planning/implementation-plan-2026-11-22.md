# UX 改进实施计划

- **状态：** `in progress`
- **基线：** `3d7de3f`
- **主计划：** [`ux-improvement-plan.md`](ux-improvement-plan.md)
- **代码核对：** [`ux-improvement-assessment.md`](ux-improvement-assessment.md)

## 目标与完成条件

落实已核对的 UX 改进，同时保持课程/学生权限边界、历史成绩可见性和逐题结果/报告独立发布。每个 workstream 均需有合成数据回归测试、角色手册更新和可复查的完成状态；AI 测试只能 mock，不调用真实服务。预生产不得创建/删除测试记录。

## 阶段与并行工作流

| 顺序 | 条目 | 执行方式 | 主要范围 | 状态 |
|---|---|---|---|---|
| 0 | UX-07 空闲会话 | 已在主工作区完成 | 服务端 45 分钟 / 40 分钟提醒、轮询不续期、跨标签、键盘 | `completed` |
| 1A | UX-01 批量加课 | 专用 worktree / 子代理 | `app.py` enrollment route、`templates/course.html`、隔离回归测试、老师手册 | `completed` |
| 1B | UX-04 作业范围可靠性 | 专用 worktree / 子代理 | `app.py` 答案/页码/范围路由、`templates/lecture.html`、AI 标签显式操作、回归测试、老师手册 | `completed` |
| 1C | UX-06 登录帮助 | 专用 worktree / 子代理 | `templates/login.html`、独立测试文件；不读取账号、不变更登录行为 | `completed` |
| 2 | UX-05 考试状态 | 主工作区串行实施 | `db.py` 幂等迁移、考试录入/学生显示、零分/仅评语/缺考测试、老师/学生手册 | `completed` |
| 3 | UX-03 近期截止列表 | 主工作区串行实施 | 学生路由和首页、时区/提交状态/权限/窄屏相关测试、学生手册 | `completed` |
| 4 | UX-02 发布状态核验 | 已审计 | 现有状态呈现及后端门槛符合计划；保留独立发布，相关回归测试通过 | `completed` |
| 5 | 总体验收、提交、推送 | 主代理 | 全量回归和差异检查已通过；本地集成提交待完成，推送需处理自动部署确认 | `in progress` |

## Worktree 协作规则

- 三个并行代理各自只改表格中列出的功能文件及新增的专属测试，不碰本计划、总评估文档或其他代理的文件。
- worktree 从 UX-07 已验证的本地基线创建；代理不得推送或触碰预生产。每个代理先运行相关测试，再提交一个仅存在于本地 worktree 的功能提交并汇报哈希/改动文件/测试结果。
- 主代理按 UX-01、UX-04、UX-06 顺序集成，解决 `app.py` 不同路由区域可能产生的合并冲突；每次集成后运行受影响的测试。
- 若代理发现需求和当前实现不一致，先报告，不扩大权限、不重写整页、不发起真实 AI 请求。

## 最终验收与发布

1. 在主工作区从干净差异核对完整 UX 变更；运行 `.venv/bin/python -m unittest discover -s tests -v`、`.venv/bin/python -m pip check`、`node --check static/session_timeout.js` 及 `git diff --check`。
2. 更新 [`ux-improvement-assessment.md`](ux-improvement-assessment.md) 的逐项状态、实现证据和测试结果；仅把验收完成的项目标为 `completed`。
3. 检查 `.github/workflows/` 与仓库分支保护/发布路径。若推送 `main` 会自动部署生产，先向用户确认部署窗口/目标；未确认前不推送会触发部署的分支。
4. 所有测试通过后在本地提交集成变更；仅推送到用户确认的远端与分支，不自动部署、不创建 PR，除非用户另行要求。
