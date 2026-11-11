# -*- coding: utf-8 -*-
# 基础镜像可覆盖，便于国内网络使用镜像加速：
#   docker build --build-arg BASE_IMAGE=docker.m.daocloud.io/library/python:3.12-slim -t gradelogy .
ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    DATA_DIR=/data \
    HOST=0.0.0.0 \
    PORT=5000

WORKDIR /app

# pypdfium2 自带上游二进制，无需额外系统库；只补时区数据
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 非 root 运行；/data 是唯一需要持久化的目录
RUN useradd -m -u 1000 app && mkdir -p /data && chown -R app:app /app /data
USER app

VOLUME ["/data"]
EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5000/healthz').status==200 else 1)"

# 单进程多线程：SQLite 写入不跨进程，长批改也不会被 worker 超时杀掉
CMD ["gunicorn", "-w", "1", "--threads", "8", "--timeout", "1800", \
     "-b", "0.0.0.0:5000", "--access-logfile", "-", "app:app"]
