# 开发与维护指南

GradeLogy 使用 Flask、SQLite 和 Jinja2。按业务逐步拆分职责，保持现有教学流程，每一步同时更新相关文档。当前修复状态见 [本地维护记录](maintenance-progress.md)。

## 开发环境

建议使用 Python 3.12；macOS/Linux 可直接运行，Windows 可参考 [Docker 本机部署](deploy-standalone.md)。当前工作副本的配置和启动方法见 [本地开发环境](local-development.md)。新环境执行：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
```

访问 <http://127.0.0.1:5000>，健康检查为 `/healthz`。测试使用临时数据库，不使用真实教学数据或真实 AI 服务：

```bash
AUTO_BACKUP=0 AUTO_MODEL_SYNC=0 AUTO_GRADE=0 .venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m pip check
git diff --check
```

## 应用创建与启动

| 入口 | 职责 |
| --- | --- |
| `create_app(config_overrides)` | 创建独立应用，绑定路由、签名器和队列，准备目录、日志与密钥；不建库、迁移或启动后台任务 |
| `initialize_app(application, start_workers=False)` | 初始化和迁移数据库；不恢复中断任务，不启动后台任务 |
| `initialize_app(application)` | 初始化数据库、恢复中断任务状态，并启动已启用的后台服务 |
| `python app.py` | 显式初始化后启动本地服务 |
| `wsgi:app` | 正式运行入口；Docker 已使用此入口 |

导入 `app` 模块会创建兼容应用对象，但不会初始化数据库或启动线程。测试和工具需要按用途显式初始化。设置 `DATA_DIR` 会推导数据库、附件、备份、导出、日志及密钥路径，单项路径可进一步覆盖。

生产仍使用单进程、多线程。队列归属应用实例，但后台备份和模型同步的启动锁仍属于进程；多进程任务租约、关闭与排空机制尚待实现。不要直接增加 Gunicorn worker 数。

## 必须保持的业务规则

| 范围 | 规则 |
| --- | --- |
| 附件与分析 | 课程与图谱权限受 `TEMPORARY_GLOBAL_TEACHER_ACCESS` 控制；默认 `1` 暂时全局开放老师权限，生产隔离部署应设为 `0`。纵向分析仍限定课程和学生 |
| 重新批阅 | 入队即隐藏旧结果；新题全部复核后再公开 |
| AI 写入 | 业务结果与成功状态一并提交；失败先回滚 |
| 数据恢复 | 撤销全部旧登录，兼容旧数据库迁移；不得覆盖开发者的教学数据做测试 |
| 图谱回滚 | 在写锁内校验应用后状态；后续编辑和旧快照不得直接覆盖 |
| 删除 | 通过统一删除服务清理关联记录，事务由调用方提交 |
| AI 配置 | 环境密钥存在时使用完整环境配置；缺接口或模型应报配置错误 |

## 修改流程

1. 阅读相关手册、业务实现和维护记录，明确触发条件及验收结果。
2. 完成一个可验证的修改，兼容已有数据；不要顺带修改无关模块。
3. 验证相关角色、异常和事务边界；必要时增加能防止缺陷重现的测试。
4. 同步 README、角色手册、模块或部署说明，删去过期和重复表述；操作条件变化时同步更新教师流程表和助手检索回归测试。
5. 检查差异和文件清单。提交、推送及生产部署按用户授权执行；`main` 推送可能触发自动发布。

可使用 [Trae](https://www.trae.cn/)、[Claude Code](https://docs.anthropic.com/en/docs/claude-code/overview) 或 [Codex](https://github.com/openai/codex) 辅助维护。提供需求、约束和验收标准，以代码差异及验证结果判断是否完成。
