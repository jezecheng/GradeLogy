# GradeLogy — 作业管理与 AI 批改平台

面向老师、学生与教务管理者的轻量级在线作业系统。老师发布作业并上传教师版标准答案 PDF，学生上传手写作答的扫描件，系统把「学生作业 + 标准答案」两份文件一起交给支持 PDF 输入的大模型逐题批阅；老师在「可编辑 OCR + 标准答案」两栏页面复核更正后发布成绩。不限定学科，凡支持 PDF / 图片作答与标准答案对照的场景均可使用。

纯 Flask + SQLite 单体，无前端框架、无外部服务依赖（除 AI 接口），适合个人教师或小型机构自建部署。

## 角色与操作手册

| 角色 | 手册 | 主要能力 |
|------|------|----------|
| 管理员 admin | [管理员手册](docs/admin.md) | 建号与重置密码、数据导出/恢复、一致性检查、日志排查 |
| 老师 teacher | [老师手册](docs/teacher.md) | 课程、学生名单、作业、标准答案、AI 批阅与复核 |
| 学生 student | [学生手册](docs/student.md) | 上传作业、查看批改 |

## 功能概览

- **账号与权限**：admin / teacher / student 三角色；初始密码登录后强制修改
- **课程与名单**：建课、选课；CSV 批量导入学生或一次批量建课，上传后先预览再确认，已有账号直接入课，新用户名自动建号
- **作业与答案**：发布作业、上传教师版 PDF；批阅时直接使用该 PDF，无需预处理
- **AI 批阅**：学生作业与标准答案两份文件一起送入支持 PDF 的模型，逐题输出 OCR、判定（对 / 半对 / 错 / 无法判定）、得分与评语
- **人工复核**：两栏同屏，可修改 OCR、判定、得分与评语；保存即向学生发布
- **工程能力**：配置环境变量化、Docker 部署、日志轮转、每日自动备份、数据整体导出与恢复、一致性检查、访问与审计日志

## 架构

### 整份 PDF 直传批阅

不再切题、不再裁剪图片，也不依赖 PDF 文字层。批阅时把两份文件按顺序放进同一条消息交给模型：

```mermaid
flowchart LR
    A[学生扫描件 PDF / 图片] --> C[支持 PDF 的模型<br/>逐题对照批阅]
    B[教师版标准答案 PDF] --> C
    C --> D[(SQLite questions 表)]
    D --> E[两栏复核页<br/>老师核对更正]
    E --> F[学生端查看成绩与评语]
```

- `utilities/pdfgrade.py`：读取两份文件，按 OpenAI 兼容的 `file` content part 发送（PDF 用 `data:application/pdf;base64,...`）
- 模型一次输出逐题 JSON：题号、OCR、标准答案要点、判定、建议分数、评语，以及总评与薄弱点
- 输出被截断时自动抢救已完整的题目；模型没返回的题不会凭空补
- 批阅结果写入 `questions` 表（`image_path` 留空），复核页只显示文字，不再展示裁剪图

### 技术栈

| 层 | 选型 |
|----|------|
| 后端 | Python 3.12 + Flask 3，Jinja2 模板，原生 CSS |
| 数据 | SQLite（WAL 模式），werkzeug 密码哈希 + Flask session |
| 文件读取 | PDF / 图片以 base64 原样发送给模型，无需本地渲染 |
| 模型调用 | OpenAI 兼容 `/chat/completions`，urllib 直连，内置 429 退避与截断救援 |
| 部署 | gunicorn 单进程 8 线程，Docker / docker compose |

### 目录结构

```
GradeLogy/
├── app.py               # Flask 单体：路由 / 权限 / DB 迁移 / 批阅入口
├── config.py            # 配置中心：路径、端口、密钥、AI 全部走环境变量
├── utilities/           # pdfgrade（整份 PDF 批阅）/ roster / storage / llm
├── templates/           # 登录、三端页面、批阅复核页、系统维护
├── static/              # style.css
├── docs/                # 管理员 / 老师 / 学生操作手册
├── Dockerfile           # 单进程 gunicorn 多线程镜像，数据卷 /data
├── docker-compose.yml   # 一条命令起服务，数据落 ./data
├── data.sqlite          # 首次启动自动创建（已 gitignore）
├── logs/                # app.log，5MB 轮转（已 gitignore）
└── uploads/             # 学生提交与教师版 PDF（已 gitignore）
```

## 快速开始

```bash
# 1. 安装依赖（Python 3.10+）
pip install -r requirements.txt

# 2. 启动（首次启动自动建库、写入 admin 账号）
python app.py

# 3. 访问 http://127.0.0.1:5000
#    默认管理员：admin / admin123（登录后必须修改）
```

> macOS 系统 Python 若缺少 CA 证书，项目会自动用 `certifi` 兜底，避免调用 HTTPS AI 接口时报 SSL 错误。

## 文件存储

批阅不再生成任何切题图片，`uploads/` 只保存原件：

```
uploads/
├── <学生提交的 PDF / 图片>
└── <教师版标准答案 PDF>
```

OCR 文本、逐题判定与评语保存在数据库 `questions` 表；`questions.image_path` 留空。

## 部署与配置

```bash
docker compose up -d --build
```

国内网络拉不到 Docker Hub 时，取消 `docker-compose.yml` 里 `args.BASE_IMAGE` 的注释，或：

```bash
docker build --build-arg BASE_IMAGE=docker.m.daocloud.io/library/python:3.12-slim -t gradelogy .
```

- 数据统一落在 `./data`（容器内 `/data`），备份即拷走该目录
- 健康检查：`GET /healthz`（数据库与存储状态，异常返回 503）
- gunicorn 单进程 8 线程：SQLite 不适合多进程写入；批改长请求 `--timeout 1800`

所有部署参数集中在 `config.py`，均可用环境变量覆盖：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `DATA_DIR` | 项目目录 | 数据根目录，数据库/附件/日志默认都在其下 |
| `DB_PATH` | `$DATA_DIR/data.sqlite` | SQLite 文件路径 |
| `STORAGE_DIR` | `$DATA_DIR/uploads` | 上传件目录（学生作业与标准答案） |
| `LOG_DIR` | `$DATA_DIR/logs` | 日志目录（`app.log`，5MB 轮转 5 份） |
| `HOST` / `PORT` | `0.0.0.0` / `5000` | 监听地址与端口 |
| `MAX_UPLOAD_MB` | `20` | 单次上传上限 |
| `MAX_RESTORE_MB` | `2048` | 从备份恢复时解压总量上限 |
| `BACKUP_DIR` | `$DATA_DIR/backups` | 自动备份、手动备份与恢复前快照目录 |
| `BACKUP_KEEP` | `7` | 保留最近几份备份，超出的自动清理 |
| `AUTO_BACKUP` | `1` | 是否启用每日自动备份（`0` 关闭） |
| `SECRET_KEY` | 自动生成并存入 `$DATA_DIR/.secret_key` | 生产环境建议显式设置 |
| `AI_API_URL` / `AI_API_KEY` / `AI_MODEL` | 空 | 设置后优先于页面 AI 设置，容器部署不必把 Key 存库 |
| `LOG_LEVEL` | `INFO` | 日志级别；设为 `WARNING` 可关闭访问日志 |

## 数据备份与迁移

管理员「系统维护」提供完整备份与恢复：导出 zip（数据库在线快照 + 全部附件），在新系统上传即可整体还原。恢复前自动备份当前数据，恢复后重新登录；旧版本备份会自动执行数据库迁移。

系统还会每天自动生成一份同样的完整备份到 `$DATA_DIR/backups`，保留最近 `BACKUP_KEEP` 份；管理员也可在「系统维护」页随时「立即备份」并下载历史备份。

同版本换目录也可直接停机拷贝整个数据目录（数据库与附件必须同源）。详细步骤、手动兜底与注意事项见 [管理员手册 · 备份与迁移](docs/admin.md#数据备份与迁移)。

## 路线图 / 已知限制

- [ ] 整份 PDF 一次请求，页数过多时受模型上下文与输出上限限制 → 计划支持分批批阅
- [ ] 不同厂商对 PDF content part 的支持差异较大，需要按模型验证
- [ ] 批改结果导出 Excel
- [x] 学生名单 CSV 批量导入
- [x] 每日自动备份与一键下载
- [x] 局域网访问、Docker 部署与配置解耦
- [x] 系统数据整体导出、恢复、一致性检查与修复
- [x] 访问日志、登录与账号审计、AI 调用日志

## 安全提示

- `data.sqlite`（含 API Key 与密码哈希）、`uploads/`、`.secret_key`、`logs/`、`backups/` 均在 `.gitignore` 中
- 部署到公网前请：修改默认 admin 密码、配置 HTTPS 反向代理、显式设置 `SECRET_KEY`、把 AI Key 改为环境变量注入
- 备份包含敏感信息，请按机密文件保管
