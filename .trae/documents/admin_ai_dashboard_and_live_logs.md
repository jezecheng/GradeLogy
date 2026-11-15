# Context

目前 AI 消耗只在教师相关页面按讲次/提交展示，管理员无法查看全局成本、模型用量和失败情况；应用日志虽集中写入 `logs/app.log`，但只能登录服务器查看。此次将在管理员后台增加统一的“用户管理 / AI 使用 / 系统日志 / 系统维护”标签页，提供全局 AI dashboard 和安全的实时日志查看，不引入新的前端或服务端依赖。

## 1. 统一管理员标签页

- 新增 `templates/_admin_tabs.html`，根据 `request.endpoint` 高亮四个标签，并纳入用户管理、AI 使用、系统日志、系统维护四个页面。
- 调整 `templates/base.html` 的管理员主导航为“系统管理”入口，避免与页内四个标签重复；保留现有路由和书签。
- 在 `static/style.css` 复用现有 card、filter-form、stats-kpis、bar-table 样式，补充标签、dashboard 分布条和日志终端样式及移动端适配。

## 2. AI 使用 Dashboard

- 在 `app.py` 新增 `@role_required("admin")` 的 `/admin/ai-usage` 路由与 `_ai_dashboard_data()` 聚合查询。
- 默认统计近 30 天，支持近 7/30/90 天及全部；可组合筛选教师、课程、操作类型、模型和状态，筛选选项从现有数据读取。
- 指标卡展示：操作数、成功/失败/进行中数量与成功率、模型请求数、输入/输出/总 token、预估费用、已知请求的平均耗时；补充缓存/推理 token 汇总。
- 展示按日用量趋势、按操作类型/模型/教师分布，以及最近 20 次操作（含教师、课程、状态、token、耗时、费用和错误）。最近操作复用 `_ai_usage_text()` 与现有操作/状态标签。
- SQL 先按 operation 聚合 `ai_usage_events`，再做全局汇总和分组，避免一次操作的多次请求/重试重复计数；为 `started_at` 增加轻量索引。
- 保留 `NULL` 语义：已知 token/费用求和，同时显示未知用量、未知费用请求数；没有事件显示“未发起请求”，不能将缺失数据写成 0。运行中操作不参与成功率分母，费用直接使用事件中的价格快照结果，不按当前价格重算。
- 使用 Jinja 表格和 CSS 横条呈现，不引入图表库。

## 3. 实时系统日志

- 新增 `/admin/logs` 页面、`/admin/logs/tail` 增量 JSON 接口和 `/admin/logs/download/<name>` 下载路由，全部限制管理员访问并设置 `no-store`。
- 新增 `utilities/logview.py` 封装安全文件枚举与分块读取：只允许 `app.log` 和 `app.log.1`—`app.log.5`，校验真实路径仍位于 `cfg.LOG_DIR`，拒绝符号链接和任意路径，不提供删除或写日志功能。
- 初次只读文件末尾约 200 行；浏览器每 3 秒串行轮询，通过 `offset + dev + ino` 游标获取新增内容。单次读取限制字节数和行数，UTF-8 容错解码，不加载完整大文件。
- 文件 inode 改变、大小小于游标或日志被截断时重新读取末尾，并返回“日志已轮转/截断”的状态提示。
- 页面支持选择当前/轮转日志、暂停/恢复、清空当前视图、自动滚动、级别和关键词过滤、下载原文件；清空只影响浏览器视图，不修改文件。前端用 `textContent` 渲染，最多保留 1000 行，页面隐藏时暂停轮询。
- `_access_log()` 排除 tail 轮询端点，避免查看日志本身每 3 秒制造新日志。页面提示日志可能包含 IP、账号和异常信息，仅限管理员使用。

## 4. 文档与测试

- 更新 `docs/admin.md`：说明四个管理员标签、dashboard 的统计/未知值口径、日志轮询和轮转文件范围。
- 新增 `tests/test_admin_insights.py`，使用临时 `DATA_DIR`、临时日志目录和 Flask test client，不触碰真实数据。

关键修改文件：`app.py`、`templates/base.html`、`templates/admin_users.html`、`templates/consistency.html`、`static/style.css`、`docs/admin.md`。
关键新增文件：`templates/_admin_tabs.html`、`templates/admin_ai_usage.html`、`templates/admin_logs.html`、`static/admin_logs.js`、`utilities/logview.py`、`tests/test_admin_insights.py`。

## 验证

1. 运行 `python -m unittest discover -s tests -v` 和 Python 语法编译、`git diff --check`。
2. 集成测试覆盖：匿名跳转、教师/学生 403；日期与组合筛选；成功/失败/运行中；重试不重复计操作；无事件及 `NULL` token/费用不被当作 0；趋势和分组计数正确。
3. 日志测试覆盖：初次 tail、增量游标、级别/关键词展示、截断与轮转、大文件上限、坏 UTF-8、路径穿越、编码斜杠、符号链接及下载权限。
4. 浏览器人工验证四个标签、dashboard 筛选与空状态，以及日志暂停/恢复/清空/过滤/下载、页面隐藏暂停和轮转提示。
5. 不自动提交或推送，完成后汇报变更与测试结果。
