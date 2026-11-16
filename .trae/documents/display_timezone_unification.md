# 全系统 UI 时区统一实施计划

## Context

知识图谱列表和详情直接输出 SQLite UTC 时间，其他页面也存在直出 UTC、服务器本地 naive 时间和浏览器本地时间混用的问题。目标是在不迁移历史数据的前提下，让所有 UI 时间默认按 Asia/Shanghai（GMT+8）显示，并允许每位登录用户设置自己的显示时区，避免简单替换后产生二次加 8 小时。

## 实施方案

### 1. 建立明确的显示时区与时间来源边界

在 `config.py` 增加：

- `DISPLAY_TIMEZONE`：全系统默认显示时区，默认 `Asia/Shanghai`；
- `LEGACY_TIMEZONE`：旧本地 naive 字段的来源时区，默认读取 `TZ`，否则 `Asia/Shanghai`。

在 `app.py` 扩展现有 `_localtime`，使用标准库 `zoneinfo.ZoneInfo`：

- SQLite `CURRENT_TIMESTAMP` 字段按 UTC 解析，再转换为当前用户显示时区；
- `ai_operations.started_at`、`lectures.summary_at/deadline`、模型同步时间、启动时间等旧本地 naive 字段按 `LEGACY_TIMEZONE` 解析；
- 带 offset 的 ISO 时间尊重原 offset；
- 非法或缺失的用户时区回退到默认上海时区；
- 保留 `localtime` 过滤器名称，新增旧本地时间和 `datetime-local` 回显过滤器，避免各模板自行拼接转换。

不修改数据库表结构，不批量迁移历史时间，不改变统计查询和外部存储口径。

### 2. 提供所有角色可用的个人时区设置

复用 `settings` 表，以 `display_timezone` 为 key 保存每用户偏好，无需新增字段。

在现有 `/password` 账号页面增加“显示时区”卡片，提供常见 IANA 时区选项，同时允许输入其他合法 IANA 名称；新增一个带现有 CSRF 保护的保存路由，学生、教师、管理员均可使用。非法时区不保存并给出提示，保存后新页面立即生效。

在 `base.html` 注入当前显示时区 meta，`static/admin_logs.js` 的“最后更新”使用该时区，而不再隐式使用浏览器时区。

### 3. 修正全站 UI 时间输出

统一修改已盘点的时间显示点，纯业务日期如 `exam_date` 不转换：

- 知识图谱：`knowledge_graphs.html`、`knowledge_graph.html` 的创建/更新时间，以及资料首屏、轮询 JSON 和 AI Request 时间共用同一用户时区；
- 教学页面：`lecture.html`、`review.html`、`student_lecture.html`、`student_home.html`、`student_detail.html`、`course.html` 的提交、更新、汇总与截止时间；
- 其他页面：`plan_edit.html`、`prompts.html`、`setup_import.html`、`consistency.html`、模型清单同步时间；
- AI 页面：`base.html` 最近操作、`admin_ai_usage.html` 最近操作与请求日志；请求事件使用 UTC，缺少事件时间而回退到 operation 时按旧本地来源处理；
- 应用元信息：登录页/帮助中的版本时间与启动时间。

截止时间采用闭环转换：历史及数据库存储仍按 `LEGACY_TIMEZONE` 的 naive 文本；教师编辑时先转换为其显示时区，保存时从显示时区转换回旧存储口径，学生查看时再转换为学生自己的时区。

### 4. 回归测试

补充并更新测试，覆盖：

1. 固定 UTC `2026-01-02 03:04:05` 默认显示 `2026-01-02 11:04`，不再依赖测试机器时区；
2. 自定义 `UTC`、`America/New_York` 等时区后，知识图谱列表、详情、资料 JSON 和请求明细同步变化；
3. 旧本地字段不会被再次加 8 小时，UTC event 与本地 operation 回退分别正确；
4. 三角色均可保存时区，非法时区被拒绝，不同用户偏好互不影响；
5. 截止时间在默认时区保持原值，自定义时区下显示、编辑和保存往返正确；
6. 扫描模板确保已知数据库时间字段不再直接输出，并运行完整 `unittest discover`。

## 关键文件

- `config.py`、`app.py`、`ai_usage.py`
- `templates/password.html`、`templates/base.html`
- `templates/knowledge_graphs.html`、`templates/knowledge_graph.html`
- 上述教学、管理和设置模板
- `static/admin_logs.js`
- `tests/test_admin_insights.py`、`tests/test_knowledge_graph.py`，必要时增加相邻路由测试

## 验证

- 使用固定时间断言，不能用被测过滤器生成期望值；
- 在默认上海、自定义 UTC 和美东时区下检查同一页面；
- 验证知识图谱首屏与 4 秒轮询结果一致；
- 验证非法时区、无设置用户和未登录页面均安全回退；
- 完整测试套件通过后交付，不提交或推送，除非用户另行要求。
