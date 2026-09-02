FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 2026-09-02 (P3)：基础排障工具。slim 镜像默认无 ps/top/ss，容器内排障只能
# 翻 /proc。procps=ps/top/pgrep；curl=健康检查与 HTTP 排障；iproute2=ss 查
# socket 连接状态。Debian 官方源在国内直连偏慢，这里用阿里云 Debian 镜像。
RUN sed -i 's|deb.debian.org|mirrors.aliyun.com|g; s|security.debian.org|mirrors.aliyun.com|g' \
        /etc/apt/sources.list.d/debian.sources /etc/apt/sources.list 2>/dev/null || true \
    && apt-get update \
    && apt-get install -y --no-install-recommends procps curl iproute2 \
    && rm -rf /var/lib/apt/lists/*

# Install deps first so `pip install` is cached across code changes.
# 使用清华 PyPI 镜像（国内直连可达，无需代理）
COPY pyproject.toml ./
COPY hermes_trader/__init__.py hermes_trader/__init__.py
RUN pip install -e . --index-url https://pypi.tuna.tsinghua.edu.cn/simple

# Copy the rest of the source.
COPY hermes_trader/ hermes_trader/
COPY scripts/ scripts/
COPY conftest.py ./
# Ops docs (e.g. DSL/SL manual) so they ship inside the image.
COPY docs/ docs/

# State lives on a Fly volume mounted at /data; defaults are overridden via env
# in fly.toml so the loop + server + MCP all share one source of truth.
RUN mkdir -p /data
ENV SESSION_LOG_PATH=/data/session-log.jsonl \
    HERMES_DSL_STATE_FILE=/data/.dsl-state.json \
    HERMES_AGENT_CONFIG_FILE=/data/.agent-config.json \
    HERMES_AGENT_MEMORY_FILE=/data/.agent-memory.json

EXPOSE 8000

# Default command runs the FastAPI server (dashboard + API). The trading loop
# runs as a separate Fly process — see [processes] in fly.toml.
CMD ["python3", "-m", "hermes_trader.server"]
