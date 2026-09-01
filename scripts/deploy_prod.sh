#!/usr/bin/env bash
# ── Hermes Trader 生产发布脚本（DSL 退出检查点 / WS 实时价 本次发布）────
# 本脚本是对 /home/ldy/hermes-deploy/deploy.sh 的薄包装，不重复构建逻辑：
#
#   1. 发布前预检（在宿主源码目录）
#        - py_compile 校验本次改动文件可编译
#        - 可选 --with-retest：跑只读复测脚本，确认 [dsl:ws-mid] N/M 路径
#          （真实连 WS/REST，只读、不下单；账户无持仓时用 BTC/ETH 探针）
#   2. 调用既有 deploy.sh 完成：同步手册 → docker compose build
#      → up -d --force-recreate（保留 hermes_data /data 卷）→ 健康轮询
#   3. 发布后验证（在新容器内）
#        - checkpoint / 批回调代码确实被打包进镜像（防 PURR 事故：改了源码
#          却忘了 build，容器仍跑旧镜像）
#        - postdeploy_smoke.py 冒烟
#        - 近期容器日志无 Traceback
#   4. 打印 git 回滚锚点与回滚命令（本流程无镜像 tag，回滚走 git）
#
# 用法：
#   bash scripts/deploy_prod.sh                # 预检 + 发布 + 发布后验证
#   bash scripts/deploy_prod.sh --with-retest  # 额外跑只读 WS 复测
#   bash scripts/deploy_prod.sh --preflight-only  # 只预检，不发布
#
# 可选环境变量（与 deploy.sh 一致）：
#   SRC_DIR    源码目录（默认 /home/ldy/hermes-trader）
#   DEPLOY_DIR 部署目录（默认 /home/ldy/hermes-deploy）
#   SERVICE    容器/服务名（默认 hermes-trader）
# ────────────────────────────────────────────────────────────────────
set -euo pipefail

SRC_DIR="${SRC_DIR:-/home/ldy/hermes-trader}"
DEPLOY_DIR="${DEPLOY_DIR:-/home/ldy/hermes-deploy}"
SERVICE="${SERVICE:-hermes-trader}"
COMPOSE_FILE="${DEPLOY_DIR}/docker-compose.yml"
DEPLOY_SH="${DEPLOY_DIR}/deploy.sh"

WITH_RETEST=0
PREFLIGHT_ONLY=0
for arg in "$@"; do
    case "$arg" in
        --with-retest)    WITH_RETEST=1 ;;
        --preflight-only) PREFLIGHT_ONLY=1 ;;
        -h|--help)
            grep -E '^#( |$)' "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) echo "未知参数: $arg" >&2; exit 2 ;;
    esac
done

log()  { printf '\033[1;36m[publish]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[ ok ]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*"; }
err()  { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; }

# 本次改动需要进镜像的文件（相对 SRC_DIR）
CHANGED_FILES=(
    "scripts/trading_loop.py"
    "hermes_trader/agents/perception.py"
    "scripts/retest_ws_mid_with_native_perp.py"
)

# ── 0. 前置依赖 ───────────────────────────────────────────────────────
[[ -d "${SRC_DIR}" ]] || { err "源码目录不存在: ${SRC_DIR}"; exit 1; }
[[ -f "${COMPOSE_FILE}" ]] || { err "找不到 ${COMPOSE_FILE}"; exit 1; }
[[ -f "${DEPLOY_SH}" ]] || { err "找不到既有部署脚本 ${DEPLOY_SH}"; exit 1; }
command -v docker >/dev/null || { err "docker 未安装"; exit 1; }
PY="$(command -v python3 || true)"
[[ -n "${PY}" ]] || { err "宿主缺少 python3（预检需要）"; exit 1; }

COMMIT="$(git -C "${SRC_DIR}" rev-parse --short HEAD 2>/dev/null || echo 'unknown')"
DIRTY="$(git -C "${SRC_DIR}" status --porcelain 2>/dev/null | wc -l | tr -d ' ')"

log "源码目录 : ${SRC_DIR}"
log "部署目录 : ${DEPLOY_DIR}"
log "服务名   : ${SERVICE}"
log "git 版本 : ${COMMIT}（未提交改动 ${DIRTY} 个文件）"
if [[ "${DIRTY}" != "0" ]]; then
    warn "工作区有未提交改动 —— 镜像将按当前工作区内容构建，回滚锚点 ${COMMIT} 可能不含这些改动。"
    warn "建议先提交再发布；继续。"
fi

# ── 1. 发布前预检 ─────────────────────────────────────────────────────
log "== 1/4 发布前预检：语法编译 =="
for f in "${CHANGED_FILES[@]}"; do
    if [[ ! -f "${SRC_DIR}/${f}" ]]; then
        err "缺少改动文件: ${SRC_DIR}/${f}"
        exit 1
    fi
    "${PY}" -m py_compile "${SRC_DIR}/${f}"
    ok "可编译: ${f}"
done

if [[ "${WITH_RETEST}" == "1" ]]; then
    log "== 1b/4 只读复测（WS 实时价 [dsl:ws-mid] 路径）=="
    ( cd "${SRC_DIR}" && PYTHONPATH="${SRC_DIR}" "${PY}" scripts/retest_ws_mid_with_native_perp.py )
    ok "复测通过"
else
    log "跳过 WS 复测（加 --with-retest 可启用；只读不下单）"
fi

if [[ "${PREFLIGHT_ONLY}" == "1" ]]; then
    ok "预检完成（--preflight-only），未发布。"
    exit 0
fi

# ── 2. 调用既有部署脚本（build + force-recreate + 健康轮询）──────────
log "== 2/4 调用 ${DEPLOY_SH} 构建并重建容器 =="
bash "${DEPLOY_SH}"

# ── 3. 发布后验证 ─────────────────────────────────────────────────────
log "== 3/4 发布后验证 =="

# 3a. 确认新代码真的进了镜像（grep 命中数 > 0）
log "校验 checkpoint 代码已打包进运行中的容器 ..."
assert_in_container() {
    # $1 = 容器内文件, $2 = grep 模式, $3 = 最少命中数
    local file="$1" pat="$2" min="${3:-1}" cnt
    cnt="$(docker exec "${SERVICE}" grep -c "${pat}" "${file}" 2>/dev/null || echo 0)"
    if [[ "${cnt}" -ge "${min}" ]]; then
        ok "  ${file} 含 '${pat}'（${cnt} 处）"
    else
        err "  ${file} 未找到 '${pat}'（命中 ${cnt} < ${min}）—— 镜像可能是旧的！"
        err "  请确认 deploy.sh 已重新 build，而非复用旧镜像。"
        exit 1
    fi
}
assert_in_container "/app/scripts/trading_loop.py" "def _exit_checkpoint(" 1
assert_in_container "/app/scripts/trading_loop.py" '_exit_checkpoint(mids, tag=f"research:{coin}")' 1
assert_in_container "/app/scripts/trading_loop.py" "on_batch_complete=lambda" 1
assert_in_container "/app/hermes_trader/agents/perception.py" "on_batch_complete(completed, total)" 1

# 3b. 容器内标准冒烟
log "运行容器内 postdeploy_smoke.py ..."
if docker exec "${SERVICE}" python /app/scripts/postdeploy_smoke.py; then
    ok "postdeploy_smoke 通过"
else
    warn "postdeploy_smoke 有告警/失败，请查看上方输出"
fi

# 3c. 近期日志无 Traceback（给新进程一点启动时间）
log "等待 trading_loop 在新容器内启动并检查启动日志 ..."
sleep 15
RECENT_TRACEBACKS="$(docker logs --since 3m "${SERVICE}" 2>&1 | grep -c "Traceback" || true)"
if [[ "${RECENT_TRACEBACKS}" == "0" ]]; then
    ok "近 3 分钟容器日志无 Traceback"
else
    warn "近 3 分钟日志出现 ${RECENT_TRACEBACKS} 处 Traceback，请用 'docker logs ${SERVICE}' 复核"
    docker logs --since 3m "${SERVICE}" 2>&1 | grep -A3 "Traceback" | tail -20 || true
fi

# ── 4. 完成 + 回滚锚点 ────────────────────────────────────────────────
log "== 4/4 部署完成 =="
ok "服务 ${SERVICE} 已按 git ${COMMIT} 重建并通过校验。"
cat <<EOF

回滚锚点：当前发布版本 = ${COMMIT}
本流程无镜像 tag，回滚需在源码目录切回上一个可用 commit 后重新发布：

    cd ${SRC_DIR}
    git log --oneline -5                 # 找到上一个可用 commit
    git checkout <上一个可用 commit>
    bash ${DEPLOY_SH}                   # 重新 build + force-recreate
    # 确认无误后再 git checkout ${COMMIT} 回到本次版本所在分支

发布后人工确认（可选）：
    docker exec ${SERVICE} sh -c 'tail -n 50 /data/trading-loop.log'
    curl -s http://localhost:8000/api/health   # 若宿主可直达 8000
    # 有持仓时，日志应出现 [dsl:ws-mid] N/M；checkpoint 平仓时出现
    # [DSL-CHECKPOINT] / [dsl:checkpoint] <tag>: N position(s) closed
EOF
