# 学生薄弱点知识图谱与图谱建议标记 - 产品需求文档

## Overview
- **Summary**: 在批改报告、讲次报告、纵向分析中把由知识图谱(KG)形成的建议单独标记；为每位学生构建由批改报告薄弱点派生的个人知识图谱子图，在每次人工复核后动态更新；学生端可对自己的薄弱环节子图节点标记「已复习」或「已掌握」。
- **Purpose**: 让图谱建议与普通 AI 建议在视觉上区分；让学生拥有持续累积、可自我管理的薄弱点图谱，提升复习针对性。
- **Target Users**: 教师（查看报告中的图谱建议）、学生（查看并管理自己的薄弱点子图）。

## Goals
- G1: 批改报告、讲次汇总报告、纵向知识点分析中，源自知识图谱的建议有清晰可辨识的视觉标记（如「🔗 图谱依据」徽标、专门的边框/背景色）。
- G2: 学生在每次作业人工复核保存后，其个人薄弱点记录自动更新：聚合跨讲次的薄弱知识点，关联课程 KG 节点，记录判定分布与最近出现时间。
- G3: 学生端有独立页面列出薄弱点子图（含 KG 节点定义、前置知识链），并可对每个薄弱点标记状态：薄弱 / 已复习 / 已掌握，可写备注。
- G4: 状态变更持久化到学生自有记录，不随下次复核覆盖。

## Non-Goals
- 不修改课程级知识图谱本身（不增删 kg_nodes / kg_edges）。
- 不引入新的 AI 调用；薄弱点派生完全基于已有的 question_tags + questions.verdict 数据。
- 不实现图谱可视化图形（力导向图等）；薄弱点以列表+前置链文本形式展示。
- 不改动已有批改/报告的核心评分逻辑。

## Background & Context
- 课程通过 `course_knowledge_graphs` 关联 KG；`kg_nodes`（name, definition）和 `kg_edges`（prerequisite 关系）构成图谱。
- AI 批改后 `questions` 表存每题 `verdict`；`question_tags` 表存讲次级知识点标签（lecture_id, question_no, tag）。
- `_kg_diagnosis(db, sub)` 已实现：把学生错题标签匹配到 KG 节点，查前置知识，返回结构化诊断，用于批改报告「四、知识图谱诊断」。
- `_kg_weakness_chains(db, course_id, weak_tag_names)` 已实现：为讲次汇总/纵向分析的 prompt 注入 KG 前置链文本。
- 人工复核保存路由 `submission_review_save`（POST /submission/<sid>/review/save）更新 questions 后调用 `generate_report`，是更新薄弱点的理想钩子。
- 学生端已有路由：`student_home`、`student_lecture`、`student_report`（查看已发布报告）。

## Functional Requirements

- **FR-1（图谱建议标记·批改报告）**: 批改报告（教师端 `submission_report` + 学生端 `student_report`）中「知识图谱诊断」区块有专属视觉标记（徽标 + 左侧色条/背景），与「失分点归因」「评价」等普通区块明显区分。
- **FR-2（图谱建议标记·讲次报告）**: 讲次汇总报告页面在 AI 生成内容之外，展示「图谱依据」折叠区块，列出本次薄弱标签对应的 KG 前置链，带图谱标记样式。
- **FR-3（图谱建议标记·纵向分析）**: 纵向知识点分析页面在 AI 分析内容之外，展示「图谱依据」折叠区块，列出该生跨讲次薄弱标签的 KG 前置链，带图谱标记样式。
- **FR-4（薄弱点表）**: 新增 `student_weak_points` 表，字段含 student_id, course_id, graph_id, node_id, tag, node_name, definition, source_submission_id, status(weak/reviewed/mastered), wrong_count, partial_count, right_count, total_count, last_seen_at, student_note, created_at, updated_at；唯一键 (student_id, course_id, tag)。
- **FR-5（复核后更新）**: `submission_review_save` 保存后，调用 `_update_student_weak_points(db, submission)`：基于该次提交的 questions.verdict + question_tags，按 tag 聚合计数，匹配课程 KG 节点，UPSERT 到 student_weak_points；已被学生标记为 reviewed/mastered 的记录保留其状态与备注，仅更新计数与 last_seen_at。
- **FR-6（学生薄弱点页面）**: 新增 GET `/student/weak-points`，列出当前学生跨课程的薄弱点（按课程分组），每条显示：知识点名、KG 节点定义、判定统计（对/半对/错/总数）、最近出现讲次、状态徽标、前置知识链（若匹配到 KG）。
- **FR-7（学生标记状态）**: 新增 POST `/student/weak-point/<id>/status`，接受 `status` ∈ {weak, reviewed, mastered} 与可选 `note`，更新该薄弱点记录；仅允许学生操作自己的记录。
- **FR-8（报告内图谱建议可链接到学生薄弱点）**: 批改报告的知识图谱诊断区块中，每个薄弱知识点旁提供「加入我的薄弱点」或「已在薄弱点中」的提示（学生端可见）。

## Non-Functional Requirements
- **NFR-1**: 复核保存的额外耗时 < 200ms（薄弱点更新是纯 SQL 聚合，不调 AI）。
- **NFR-2**: 不依赖外网；所有数据在本地 SQLite。
- **NFR-3**: 历史已批改数据无薄弱点记录时，学生页面显示空状态提示，不报错。
- **NFR-4**: 全部改动通过现有 215 个单元测试且无新增破坏性变更。

## Constraints
- **Technical**: Flask + SQLite（sqlite3.Row），不使用 Blueprint；遵循现有迁移模式（PRAGMA table_info + ALTER TABLE）。
- **Business**: 学生只能操作自己的薄弱点记录；教师复核更新是唯一的自动更新入口。
- **Dependencies**: 复用 `_kg_match_node`、`_kg_diagnosis`、`kg_utils.concept_neighbors`、`tag_utils.student_mastery_stats`。

## Assumptions
- 知识点标签（question_tags.tag）与 KG 节点名的匹配复用 `_kg_match_node` 的精确+包含匹配策略。
- 学生端薄弱点状态由学生自主维护，复核更新只刷新计数，不重置学生已设置的状态。
- 「已掌握」状态的薄弱点在下次复核若再次出错，自动回退为 `weak`（因为新的失分证据表明尚未掌握）。

## Acceptance Criteria

### AC-1: 批改报告图谱区块视觉标记
- **Type**: `rule`
- **Given**: 教师或学生打开一份有 KG 诊断的批改报告
- **When**: 报告页面渲染
- **Then**: 「知识图谱诊断」区块带有可识别的图谱标记（徽标文字「图谱依据」+ 专属背景/边框 CSS class `.kg-sourced`），与其他区块视觉不同
- **Pass Condition**: 报告 HTML 中存在 `.kg-sourced` class 且包含「图谱」字样的可见标记
- **Evidence**: `submission_report` 路由响应 HTML 包含 `class="kg-sourced"`

### AC-2: 讲次报告图谱依据区块
- **Type**: `rule`
- **Given**: 课程挂载了 KG，讲次有薄弱标签
- **When**: 打开讲次汇总报告页面
- **Then**: 页面存在「图谱依据」折叠区块，列出 KG 前置链，带 `.kg-sourced` 样式
- **Pass Condition**: 讲次报告模板渲染出 `kg-sourced` 区块且非空
- **Evidence**: 讲次报告路由响应包含「图谱依据」与 `.kg-sourced`

### AC-3: 纵向分析图谱依据区块
- **Type**: `rule`
- **Given**: 课程挂载了 KG，学生有跨讲次薄弱标签
- **When**: 打开该生纵向分析结果页
- **Then**: 页面存在「图谱依据」折叠区块，带 `.kg-sourced` 样式
- **Pass Condition**: 纵向分析模板渲染出 `kg-sourced` 区块
- **Evidence**: 纵向分析路由响应包含「图谱依据」与 `.kg-sourced`

### AC-4: 复核后自动更新薄弱点
- **Type**: `rule`
- **Given**: 某学生提交有 question_tags，教师执行复核保存
- **When**: `submission_review_save` 完成
- **Then**: `student_weak_points` 表中存在该学生对应 tag 的记录，计数与 questions.verdict 一致，last_seen_at 为当前时间
- **Pass Condition**: 数据库中存在 (student_id, course_id, tag) 记录且 wrong_count/right_count 与题目判定一致
- **Evidence**: 单元测试 + 直接查询 student_weak_points

### AC-5: 学生状态不被复核覆盖
- **Type**: `rule`
- **Given**: 学生将某薄弱点标记为 `mastered` 并写了备注
- **When**: 该知识点在新一次复核中未出错
- **Then**: 记录的 status 仍为 `mastered`，student_note 保留
- **Pass Condition**: UPDATE 后 status 与 student_note 不变
- **Evidence**: 单元测试断言 status 和 note 未变

### AC-6: 已掌握节点再次出错自动回退
- **Type**: `rule`
- **Given**: 某薄弱点 status=mastered
- **When**: 该知识点在新一次复核中出现错题
- **Then**: status 自动重置为 `weak`，student_note 保留
- **Pass Condition**: 出错后 status='weak'
- **Evidence**: 单元测试

### AC-7: 学生薄弱点页面可访问
- **Type**: `rule`
- **Given**: 学生登录，有薄弱点记录
- **When**: GET `/student/weak-points`
- **Then**: 返回 200，列出薄弱点列表，含知识点名、统计、状态、前置链
- **Pass Condition**: 响应 200 且包含薄弱点 tag 文本
- **Evidence**: 测试客户端请求

### AC-8: 学生可标记状态
- **Type**: `rule`
- **Given**: 学生有薄弱点记录
- **When**: POST `/student/weak-point/<id>/status` with status=reviewed
- **Then**: 记录 status 更新为 reviewed，返回重定向到薄弱点页
- **Pass Condition**: 数据库 status=reviewed
- **Evidence**: 单元测试

### AC-9: 学生越权防护
- **Type**: `rule`
- **Given**: 学生 A 的薄弱点 id=X
- **When**: 学生 B 尝试 POST 标记 id=X
- **Then**: 返回 403/404，记录不变
- **Pass Condition**: 状态码非 2xx 且记录未变
- **Evidence**: 单元测试

### AC-10: 复核性能
- **Type**: `rubric`
- **Dimension**: 复核保存接口在更新薄弱点后的额外耗时
- **Scale**: 1-3
- **Anchors**: 1 = 额外 >500ms；2 = 200-500ms；3 = <200ms
- **Pass Threshold**: >= 2
- **Evidence**: 本地对 20 题提交执行复核保存计时

## Open Questions
- [ ] 学生薄弱点是否需要按课程隔离显示，还是跨课程统一列表？→ 默认按课程分组，全部列出。
- [ ] 「已掌握」回退是否需要通知学生？→ 本次不做通知，仅状态变化。
