# -*- coding: utf-8 -*-
# 基础镜像可覆盖，便于国内网络使用镜像加速：
#   docker build --build-arg BASE_IMAGE=docker.m.daocloud.io/library/python:3.12-slim -t gradelogy .
ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}

# 构建时注入版本库信息，供「帮助 → 关于」显示；不传也能正常启动
ARG GIT_COMMIT=""
ARG GIT_BRANCH=""
ARG GIT_COMMIT_DATE=""

# 国内构建加速：默认走腾讯云镜像；构建时传空值可切回官方源
#   docker build --build-arg APT_MIRROR= --build-arg PIP_INDEX_URL= ...
ARG APT_MIRROR=mirrors.cloud.tencent.com
ARG PIP_INDEX_URL=https://mirrors.cloud.tencent.com/pypi/simple

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    DATA_DIR=/data \
    HOST=0.0.0.0 \
    PORT=5000 \
    GIT_COMMIT=${GIT_COMMIT} \
    GIT_BRANCH=${GIT_BRANCH} \
    GIT_COMMIT_DATE=${GIT_COMMIT_DATE}

WORKDIR /app

# 只补时区数据；pypdfium2 与 pillow 用 manylinux wheel，无需额外系统库
# trixie 起用 deb822 格式（debian.sources），旧版则是 sources.list，两种都兼容
RUN if [ -n "$APT_MIRROR" ]; then \
      if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i "s|deb.debian.org|$APT_MIRROR|g; s|security.debian.org|$APT_MIRROR|g" \
          /etc/apt/sources.list.d/debian.sources; \
      elif [ -f /etc/apt/sources.list ]; then \
        sed -i "s|deb.debian.org|$APT_MIRROR|g; s|security.debian.org|$APT_MIRROR|g" \
          /etc/apt/sources.list; \
      fi; \
    fi \
    && apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN if [ -n "$PIP_INDEX_URL" ]; then \
      pip install --no-cache-dir --index-url "$PIP_INDEX_URL" -r requirements.txt; \
    else \
      pip install --no-cache-dir -r requirements.txt; \
    fi

COPY . .

# 非 root 运行；/data 是唯一需要持久化的目录
RUN useradd -m -u 1000 app && mkdir -p /data && chown -R app:app /app /data
USER app

VOLUME ["/data"]
EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5000/healthz').status==200 else 1)"

# 单进程多线程：SQLite 写入不跨进程，长批改也不会被 worker 超时杀掉
# 批阅单次上限 300 秒，worker 超时留一倍余量即可
CMD ["gunicorn", "-w", "1", "--threads", "8", "--timeout", "600", \
     "-b", "0.0.0.0:5000", "--access-logfile", "-", "app:app"]
