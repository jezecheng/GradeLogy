# 知识图谱建图恢复与进度透明化

## Context

昨晚自动部署重启后，daemon 建图线程和内存等待队列丢失；启动流程只把 `ai_operations` 标为失败，没有恢复 `kg_materials`，导致第三份永久“建图中”、后续资料永久“待建图”。本次需要让重启后的任务自动续建，同时修正知识图谱专用中断提示，并向老师展示本地时间、处理耗时、chunk 进度和逐次 AI Request 状态及剩余时间粗估。

## 实施方案

### 1. 持久化续建状态与中断清理

修改 `db.py`，通过现有轻量迁移方式幂等增加：

- `kg_materials`：`started_at`、`finished_at`，仍用 SQLite UTC 时间。
- `kg_chunks`：`status`、`error`、`extraction_json`、`started_at`、`finished_at`。
- `ai_operations`：`material_id`，用于关联一份资料跨重启的多次建图操作。
- `ai_usage_events`：`chunk_id`，用于把每次请求及重试关联到资料块；补充必要索引。

启动清理由一条通用 SQL 拆成按类型处理：`kg_build` 显示“服务重启，知识图谱建图已中断，已自动重新排队，将从未完成块续建”；批阅等其他任务保留原文案。上次退出时仍为 `running` 的请求事件标为中断错误，避免详情里永久显示进行中。

### 2. 可恢复的 chunk 级建图

修改 `utilities/knowledge_graph.py` 的 `build_material()`：

- 按 `chunk_no` 逐块处理，保留约 3000 字的现有切块方式；单块输出截断时递归拆分文本重试。
- 每块开始前记录 `running/started_at`，结束后记录成功或失败、耗时与错误。
- 成功或部分成功的抽取结果写入该块 `extraction_json`。重启只跳过已有成功结果的块；`pending/running/failed` 块重新执行并覆盖旧结果。
- 所有块处理完后，仍汇总本资料全部抽取结果并调用现有 `merge_extraction()` 一次性合并，保留跨块概念关系，避免改为逐块合图造成关系丢失。
- 若进程在“抽取结果已保存、图尚未合并”时退出，续建直接复用结果再合图；若合并完成但资料状态未更新，现有幂等合并保证可安全重放。
- 资料首次开始记录 `started_at`，最终完成或失败记录 `finished_at`；续建保留原开始时间。

### 3. 启动扫描与防重复调度

修改 `app.py` 现有 `_kg_start_build()` / `_kg_build_worker()`，不引入新任务框架：

- 增加进程内调度锁和资料 ID 集合，防止同一资料重复创建 worker；worker 退出时释放登记。
- worker 获得现有 `_kg_build_lock` 后再核对资料仍存在且状态为“待建图/建图中”，继续保证所有资料串行执行。
- 新增 `start_kg_recovery()`：在 `init_db()` 后按资料 ID 扫描“待建图/建图中”，使用资料的 `uploaded_by` 恢复 AI 配置并逐份入队。每次恢复建立新的 operation，历史请求仍可追溯。
- 沿用当前单进程部署和 daemon 线程模型，不扩展为多 worker 队列。

### 4. 实时 AI Request 记录

复用 `llm.chat()`、`ai_usage.py` 和现有 `ai_usage_events`，不新建重复请求表：

- 知识图谱调用启用请求开始事件；发起 HTTP 请求前先插入 `running` 行并返回事件 ID，完成或异常时更新同一行，避免“进行中”和“完成”重复计数。
- 其他 AI 功能保持原调用行为；知识图谱 usage hook 额外注入 `chunk_id`，重试仍按 `attempt` 分行。
- 请求记录保留开始时间、状态、耗时、token、finish reason 和错误，重启清理由第 1 项统一处理。

### 5. 状态接口与教师页面

扩展 `app.py` 的资料视图和 `status.json`：每份资料返回本地化上传/开始/完成时间、处理耗时、chunk 总数及成功/失败/处理中/待处理数量。仅在老师展开某份资料时，通过 `material_id` 参数返回该资料的 Request 明细，避免每次轮询传输全部历史。

ETA 使用本资料最近 5 个成功 chunk 的实际总耗时中位数，包含重试和拆批开销：待处理块数乘以中位数，加上当前块预计剩余时间；样本不足时显示“首个文本块完成后估算”，并明确标注为粗估。

修改 `templates/knowledge_graph.html`：

- 上传时间使用项目已有 `localtime` 过滤器，与管理员日志保持相同时区；增加处理开始/完成及耗时。
- 状态列附近显示“共 N 块 · 成功 X · 处理中 Y · 待处理 Z · 失败 W”和剩余时间粗估。
- 文件名改为可展开控件；展开行展示每次 AI Request 的 chunk 序号、attempt、状态、开始时间、耗时、token 和错误。
- 页面无活动任务时也允许展开历史明细；活动任务沿用 4 秒局部轮询，不整页刷新，不打断继续上传。动态错误文本继续使用 `textContent`。

## 关键文件

- `app.py`：恢复调度、worker 防重、资料进度视图与状态接口。
- `db.py`：字段迁移、知识图谱专用中断文案、遗留 running 请求清理。
- `utilities/knowledge_graph.py`：chunk 状态、抽取结果持久化、断点续建与最终合图。
- `utilities/llm.py`：知识图谱请求开始/完成同一行更新。
- `ai_usage.py`：operation/material 关联、事件 ID 与 chunk 关联。
- `templates/knowledge_graph.html`：本地时间、处理耗时、chunk 进度、展开请求明细及 ETA。
- `tests/test_knowledge_graph.py`、`tests/test_admin_insights.py`：回归覆盖。

## 验证

1. 单元测试覆盖迁移幂等、知识图谱与批阅文案互不影响、running 请求清理。
2. 模拟请求成功后、结果保存后、合图前后等中断点，验证成功块不重复调用模型，未完成块自动续跑，节点和边不重复。
3. 验证恢复按 ID 串行、重复启动不入队、资料删除后 worker 安全退出、每次恢复创建独立 operation。
4. 验证请求开始插入和结束更新同一行、重试 attempt/chunk 关联、token/耗时记录。
5. 验证 UTC 转本地时间、处理耗时、chunk 统计、ETA 无样本/运行中/部分失败场景，以及展开接口权限和轮询停止。
6. 运行知识图谱测试及完整 `.venv/bin/python -m unittest discover` 回归，确保截断拆批、部分失败、上传去重、既有 AI 用量汇总不受影响。
