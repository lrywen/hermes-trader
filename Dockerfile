FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_NO_CACHE=1 \
    UV_PYTHON_DOWNLOADS=0 \
    UV_PROJECT_ENVIRONMENT=/usr/local \
    UV_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

WORKDIR /app

# 2026-09-02 (P3)：基础排障工具。slim 镜像默认无 ps/top/ss，容器内排障只能
# 翻 /proc。procps=ps/top/pgrep；curl=健康检查与 HTTP 排障；iproute2=ss 查
# socket 连接状态。Debian 官方源在国内直连偏慢，这里用阿里云 Debian 镜像。
RUN sed -i 's|deb.debian.org|mirrors.aliyun.com|g; s|security.debian.org|mirrors.aliyun.com|g' \
        /etc/apt/sources.list.d/debian.sources /etc/apt/sources.list 2>/dev/null || true \
    && apt-get update \
    && apt-get install -y --no-install-recommends procps curl iproute2 \
    && rm -rf /var/lib/apt/lists/*

# P1-5: reproducible image build. Install uv (from the tuna mirror, no proxy
# needed) and resolve strictly from uv.lock via `uv sync --frozen` — the image
# never re-resolves, so deployed deps exactly match CI (P1-1). uv uses the
# base image's system Python (UV_PYTHON_DOWNLOADS=0) and installs into
# /usr/local (UV_PROJECT_ENVIRONMENT) so the server/loop entrypoints see the
# packages without activating a venv.
RUN pip install uv --index-url https://pypi.tuna.tsinghua.edu.cn/simple

# Install the locked dependencies first (cached across code changes).
# --no-install-project skips installing the project itself — only its locked
# deps land in this layer; the editable project install happens after the full
# source is copied, so running code is always fresh while the heavy dep layer
# stays cached. --no-dev keeps the runtime image slim.
COPY pyproject.toml uv.lock ./
# Hatchling needs the package dir to exist even when the project itself is not
# installed yet (--no-install-project); copy just the marker file to satisfy it.
COPY hermes_trader/__init__.py hermes_trader/__init__.py
RUN uv sync --frozen --no-dev --no-install-project

# Copy the rest of the source.
COPY hermes_trader/ hermes_trader/
# P0-1: ship ONLY the scripts with a proven in-container runtime caller — no
# blanket `COPY scripts/ scripts/` (that dragged nine backtest_* research
# scripts and dozens of one-off ops tools into the production image). The set
# is the single source of truth in scripts/runtime_whitelist.py and is pinned
# by tests/test_runtime_scripts_whitelist.py (existence + exact-Dockerfile
# match + no backtest/calibration scripts). docker-entrypoint.sh is copied
# separately below; host-run scripts (backup_state.py, cron_*.sh,
# weekly_*.sh, deploy_prod.sh, calibrate_regime_thresholds.py) are excluded.
COPY scripts/trading_loop.py scripts/trading_loop.py
COPY scripts/ip_drift_watch.py scripts/ip_drift_watch.py
COPY scripts/scheduler.py scripts/scheduler.py
COPY scripts/daily_report.py scripts/daily_report.py
COPY scripts/pullback_shadow_daily.py scripts/pullback_shadow_daily.py
COPY scripts/reconcile_ta_late_entry_shadow.py scripts/reconcile_ta_late_entry_shadow.py
COPY scripts/reconcile_xs_reversal_shadow.py scripts/reconcile_xs_reversal_shadow.py
COPY scripts/reconcile_relax_tier_shadow.py scripts/reconcile_relax_tier_shadow.py
COPY scripts/macro_regime_watch.py scripts/macro_regime_watch.py
COPY scripts/regen_param_sweep.py scripts/regen_param_sweep.py
COPY scripts/shadow_grade.py scripts/shadow_grade.py
# Shared scripts/-local helper imported by shadow_grade.py and
# reconcile_change_arms_shadow.py (`import shadow_progress`).
COPY scripts/shadow_progress.py scripts/shadow_progress.py
COPY scripts/reconcile_fills.py scripts/reconcile_fills.py
COPY scripts/reconcile_change_arms_shadow.py scripts/reconcile_change_arms_shadow.py
COPY scripts/reconcile_pullback_shadow.py scripts/reconcile_pullback_shadow.py
COPY scripts/reconcile_daily_extension_cap_shadow.py scripts/reconcile_daily_extension_cap_shadow.py
COPY scripts/postdeploy_smoke.py scripts/postdeploy_smoke.py
# Privilege-drop entrypoint (consumed by the `cp` in the RUN step below).
COPY scripts/docker-entrypoint.sh scripts/docker-entrypoint.sh
COPY conftest.py ./
# Ops docs (e.g. DSL/SL manual) so they ship inside the image.
COPY docs/ docs/

# Editable-install the project itself now that all source is present.
RUN uv sync --frozen --no-dev

# State lives on a Fly volume mounted at /data; defaults are overridden via env
# in fly.toml so the loop + server + MCP all share one source of truth.
# P1-5: create the unprivileged runtime user (uid/gid 1000, matching the k8s
# pod fsGroup) and hand /app + /data to it. No `USER` directive on purpose —
# see scripts/docker-entrypoint.sh for why privilege drop happens at runtime.
RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin hermes \
    && mkdir -p /data \
    && chown -R hermes:hermes /app /data \
    && cp scripts/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh \
    && chmod +x /usr/local/bin/docker-entrypoint.sh
# P1-5 fix (2026-09-17): setpriv keeps the inherited HOME=/root, so the
# dropped user (uid 1000) could not traverse /root (0700) and crashed at
# import on ~/.hermes/universe_cache and ~/.hermes locks. Pin HOME to the
# account home useradd created; the read-only shared config must therefore be
# mounted at /home/hermes/.hermes-trading (see docker-compose.yml).
ENV HOME=/home/hermes
ENV SESSION_LOG_PATH=/data/session-log.jsonl \
    HERMES_DSL_STATE_FILE=/data/.dsl-state.json \
    HERMES_AGENT_CONFIG_FILE=/data/.agent-config.json \
    HERMES_AGENT_MEMORY_FILE=/data/.agent-memory.json \
    HERMES_MARKET_CIRCUIT_STATE_FILE=/data/.market-circuit.state

EXPOSE 8000

# Default command runs the FastAPI server (dashboard + API). The trading loop
# runs as a separate Fly process — see [processes] in fly.toml.
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python3", "-m", "hermes_trader.server"]
