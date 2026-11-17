# 学生薄弱点知识图谱与图谱建议标记 - 实现计划

## Task 1: 新增 student_weak_points 表与迁移
- **Status**: `pending`
- **Priority**: high
- **Depends On**: None
- **Description**:
  - 在 db.py 中 `CREATE TABLE IF NOT EXISTS student_weak_points`，字段：id, student_id, course_id, graph_id, node_id, tag, node_name, definition, source_submission_id, status(默认 'weak'), wrong_count, partial_count, right_count, total_count, last_seen_at, student_note, created_at, updated_at；唯一键 (student_id, course_id, tag)；索引 (student_id, course_id)、(student_id, status)。
- **Acceptance Criteria Addressed**: AC-4
- **Test Requirements**:
  - `rule` TR-1.1: 启动应用后 PRAGMA table_info(student_weak_points) 返回所有预期列；证据：`python -c "import db; ..."` 查询
  - `rule` TR-1.2: 重复启动应用不报错（IF NOT EXISTS）；证据：连续两次 init_db 无异常

## Task 2: 实现 _update_student_weak_points 核心逻辑
- **Status**: `pending`
- **Priority**: high
- **Depends On**: Task 1
- **Description**:
  - 在 app.py 中新增 `_update_student_weak_points(db, submission)`：
    1. 通过 submission.lecture_id 查 course_id
    2. 查 course_knowledge_graphs 拿 graph_ids，加载 kg_nodes
    3. 查该 lecture 的 question_tags，建 qkey→tags
    4. 查该 submission 的 questions（no, verdict），按 tag 聚合 right/partial/wrong/total
    5. 对每个 tag：用 `_kg_match_node` 匹配 KG 节点；UPSERT student_weak_points：
       - 新记录：status='weak'
       - 已存在记录：保留 status 与 student_note；若本次有 wrong/partial 且原 status='mastered'，重置为 'weak'；更新计数（累加还是覆盖？→ 覆盖为该次提交的统计；last_seen_at=now, source_submission_id）
- **Acceptance Criteria Addressed**: AC-4, AC-5, AC-6
- **Test Requirements**:
  - `rule` TR-2.1: 有错题的 tag 生成 weak 记录，计数正确；证据：单元测试断言
  - `rule` TR-2.2: mastered 记录在无新错题时保留 mastered；证据：单元测试
  - `rule` TR-2.3: mastered 记录在有新错题时回退为 weak；证据：单元测试
  - `rule` TR-2.4: student_note 在更新中保留；证据：单元测试

## Task 3: 复核保存路由接入薄弱点更新
- **Status**: `pending`
- **Priority**: high
- **Depends On**: Task 2
- **Description**:
  - 在 `submission_review_save` 的 `db.commit()` 之后、`generate_report` 之前调用 `_update_student_weak_points(db, sub)`，用 try/except 包裹，失败仅 log 不阻塞保存。
- **Acceptance Criteria Addressed**: AC-4, AC-5, AC-6
- **Test Requirements**:
  - `rule` TR-3.1: 复核保存后 student_weak_points 有对应记录；证据：集成测试
  - `rule` TR-3.2: _update_student_weak_points 抛异常时保存仍成功；证据：mock 异常测试

## Task 4: 批改报告图谱区块视觉标记
- **Status**: `pending`
- **Priority**: high
- **Depends On**: None
- **Description**:
  - 在 report.html 模板中，为知识图谱诊断区块添加 `.kg-sourced` class，在标题旁加「🔗 图谱依据」徽标。
  - 在 style.css 中新增 `.kg-sourced` 样式（左侧色条、浅蓝背景、徽标样式）。
  - 批改报告的 Markdown 中「知识图谱诊断」标题加图谱标记前缀（在 build_report_md 中把 `## 四、知识图谱诊断` 改为 `## 四、知识图谱诊断 🔗 图谱依据`，或在模板中包裹）。
  - 确认教师端 `submission_report` 与学生端 `student_report` 都使用同一 report.html，样式一致。
- **Acceptance Criteria Addressed**: AC-1
- **Test Requirements**:
  - `rule` TR-4.1: 有 kg_diagnosis 的报告页面包含 `kg-sourced` class；证据：测试客户端请求
  - `rule` TR-4.2: 无 kg_diagnosis 时不显示图谱区块；证据：断言

## Task 5: 讲次报告与纵向分析图谱依据区块
- **Status**: `pending`
- **Priority**: medium
- **Depends On**: Task 4
- **Description**:
  - 讲次汇总报告模板（lecture_summary 相关）：在 AI 生成内容旁新增「图谱依据」折叠区块，渲染 `kg_weakness_chains`，带 `.kg-sourced`。
  - 纵向分析模板（tag_vertical 结果展示）：同样新增「图谱依据」折叠区块。
  - 需检查对应路由是否把 kg_chains 传给模板（目前只传给 prompt 没传给模板，需补充）。
- **Acceptance Criteria Addressed**: AC-2, AC-3
- **Test Requirements**:
  - `rule` TR-5.1: 讲次报告页含「图谱依据」+ `.kg-sourced`；证据：测试客户端
  - `rule` TR-5.2: 纵向分析页含「图谱依据」+ `.kg-sourced`；证据：测试客户端

## Task 6: 学生薄弱点页面路由与模板
- **Status**: `pending`
- **Priority**: high
- **Depends On**: Task 1
- **Description**:
  - 新增 GET `/student/weak-points` 路由（role_required student），查当前学生的 student_weak_points（按 course_id 分组，status 升序 weak 优先），每条补充前置知识链（用 kg_utils.concept_neighbors）。
  - 新建 templates/student_weak_points.html：按课程分组列出，每条卡片含：知识点名、KG 定义、判定统计、最近讲次、状态徽标、前置链、状态切换按钮（薄弱/已复习/已掌握）+ 备注表单。
  - 在 student_home 页加导航链接「我的薄弱点图谱」。
- **Acceptance Criteria Addressed**: AC-7
- **Test Requirements**:
  - `rule` TR-6.1: 学生访问 /student/weak-points 返回 200 且列出薄弱点；证据：测试客户端
  - `rule` TR-6.2: 无薄弱点时显示空状态；证据：断言

## Task 7: 学生标记状态路由
- **Status**: `pending`
- **Priority**: high
- **Depends On**: Task 1
- **Description**:
  - 新增 POST `/student/weak-point/<int:wid>/status`（role_required student），校验 wid 属于当前学生，更新 status（weak/reviewed/mastered）与可选 student_note，重定向回薄弱点页。
  - 非本人记录返回 404。
- **Acceptance Criteria Addressed**: AC-8, AC-9
- **Test Requirements**:
  - `rule` TR-7.1: 本人标记成功，status 更新；证据：单元测试
  - `rule` TR-7.2: 他人记录返回 404 且不变；证据：单元测试

## Task 8: 报告内图谱建议链接到学生薄弱点（学生端）
- **Status**: `pending`
- **Priority**: low
- **Depends On**: Task 4, Task 6
- **Description**:
  - 学生端报告页的知识图谱诊断区块，每个薄弱知识点旁显示状态提示（已在薄弱点列表中 / 未出现），链接到 /student/weak-points。
  - 可在 student_report 路由中查询该学生该课程的薄弱点集合，传入模板。
- **Acceptance Criteria Addressed**: AC-8（辅助）
- **Test Requirements**:
  - `rule` TR-8.1: 学生端报告有薄弱点提示链接；证据：测试客户端

## Task 9: 全量回归与性能验证
- **Status**: `pending`
- **Priority**: high
- **Depends On**: Task 1-8
- **Description**:
  - 运行 `python -m unittest discover -s tests` 确认 215+ 测试通过。
  - 对 20 题提交执行复核保存，计时确认额外耗时 < 500ms。
- **Acceptance Criteria Addressed**: AC-10
- **Test Requirements**:
  - `rule` TR-9.1: 全部已有测试通过；证据：测试输出 OK
  - `rubric` TR-9.2: 复核额外耗时；scale 1-3；阈值 >=2；证据：计时输出
