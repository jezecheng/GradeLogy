# 本地开发环境

此工作副本已配置 Python 3.12.16 和项目运行依赖，虚拟环境位于 `.venv/`。系统 Python 不受影响。

启动脚本通过 `python app.py` 显式初始化数据库与后台服务。正式部署入口已改为 `wsgi:app`；单纯导入 `app` 不再建库或启动任务。详见 [开发与维护指南](ai-development.md) 和 [本地修复记录](maintenance-progress.md)。

## 启动

在项目目录中打开终端，运行：

```bash
sh run-local.sh
```

打开 <http://127.0.0.1:5000>。首次登录用户名为 `admin`，初始密码为 `admin123`，登录后按页面提示修改密码。终端按 `Control+C` 停止服务；修改代码后停止并重新启动。

如果 5000 端口被占用：

```bash
PORT=5001 sh run-local.sh
```

此时访问 <http://127.0.0.1:5001>。

## 本地数据与自动任务

启动脚本默认只监听本机，数据库、上传文件和日志保存在 `data/local/`。Python 安装与下载缓存保存在 `data/runtime/`。这两个目录和 `.venv/` 都已被 Git 忽略。

本地启动脚本默认关闭定时自动备份、自动模型同步和定时自动批阅扫描。注意 `AUTO_GRADE=0` 只关闭周期扫描；学生提交、保存范围或标签生成成功时仍可能触发即时自动批阅（还须满足完整数据准备、标签、AI 配置及次数限制）。需要测试定时任务时可显式开启：

```bash
AUTO_BACKUP=1 AUTO_MODEL_SYNC=1 AUTO_GRADE=1 sh run-local.sh
```

AI 批阅需要在应用中配置自己的模型服务和 API Key；环境验证未调用真实 AI 服务。

## 测试与依赖

运行项目现有的隔离测试：

```bash
AUTO_BACKUP=0 AUTO_MODEL_SYNC=0 AUTO_GRADE=0 .venv/bin/python -m unittest discover -s tests -v
```

检查依赖，或按项目需求安装依赖：

```bash
.venv/bin/python -m pip check --no-cache-dir
.venv/bin/python -m pip install --no-cache-dir -r requirements.txt
```

本次安装的具体版本记录在 `data/runtime/requirements-installed.txt`，可用于重现当前环境：

```bash
.venv/bin/python -m pip install --no-cache-dir -r data/runtime/requirements-installed.txt
```
