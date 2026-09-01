#!/usr/bin/env bash
# config_master_address.sh — 幂等写入 HYPERLIQUID_MASTER_ADDRESS 到仓库根 .env.local
#
# 用途：trading_loop.py 启动时从 CWD 的 .env.local 读取环境变量，
#       resolve_user_address() 优先取 HYPERLIQUID_MASTER_ADDRESS，
#       用于 WebSocket userFills 订阅（只读，不需要私钥）。
#
# 用法：
#   bash scripts/config_master_address.sh 0xYourMasterAddress
#   bash scripts/config_master_address.sh            # 无参数则交互提示输入
#
# 特性：
#   - 校验 EVM 地址格式（0x + 40 位十六进制）
#   - 幂等：已存在则原地更新（含注释行），不存在则追加
#   - 写入前自动备份 .env.local -> .env.local.bak
#   - 只动 MASTER_ADDRESS 一行，绝不触碰私钥 / 其它配置
set -euo pipefail

# ── 定位仓库根（脚本在 scripts/ 下）─────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
ENV_FILE="${REPO_ROOT}/.env.local"

KEY="HYPERLIQUID_MASTER_ADDRESS"

# ── 获取地址（参数 或 交互输入）─────────────────────────────────────────────
ADDR="${1:-}"
if [[ -z "${ADDR}" ]]; then
  read -r -p "请输入 Hyperliquid master 地址 (0x + 40 hex): " ADDR
fi
ADDR="$(echo "${ADDR}" | xargs)"  # trim 首尾空白

# ── 校验 EVM 地址 ───────────────────────────────────────────────────────────
if [[ ! "${ADDR}" =~ ^0x[0-9a-fA-F]{40}$ ]]; then
  echo "ERROR: 地址格式非法: '${ADDR}'" >&2
  echo "       期望 0x 开头 + 40 位十六进制（共 42 字符），例: 0x1234...abcd" >&2
  exit 1
fi

# ── 备份 ───────────────────────────────────────────────────────────────────
if [[ -f "${ENV_FILE}" ]]; then
  cp -p "${ENV_FILE}" "${ENV_FILE}.bak"
  echo "[backup] ${ENV_FILE} -> ${ENV_FILE}.bak"
else
  echo "[info] ${ENV_FILE} 不存在，将新建"
fi

# ── 幂等写入 ───────────────────────────────────────────────────────────────
LINE="${KEY}=${ADDR}"

if [[ -f "${ENV_FILE}" ]] && grep -qE "^[[:space:]]*#?[[:space:]]*${KEY}=" "${ENV_FILE}"; then
  # 已存在（含被注释的行）：用 sed 原地替换第一个匹配行
  #   匹配可选前导空白 + 可选 # 注释符 + KEY=，整行替换为新值
  sed -i -E "s|^[[:space:]]*#?[[:space:]]*${KEY}=.*|${LINE}|" "${ENV_FILE}"
  echo "[update] 已更新 ${KEY}"
else
  # 不存在：追加（若文件非空且末尾无换行，先补一个换行）
  if [[ -f "${ENV_FILE}" ]] && [[ -s "${ENV_FILE}" ]] && [[ "$(tail -c1 "${ENV_FILE}" | wc -l)" -eq 0 ]]; then
    printf '\n' >> "${ENV_FILE}"
  fi
  printf '%s\n' "${LINE}" >> "${ENV_FILE}"
  echo "[append] 已新增 ${KEY}"
fi

# ── 脱敏确认（前 6 后 4）────────────────────────────────────────────────────
MASKED="${ADDR:0:6}...${ADDR: -4}"
echo "[ok] ${KEY}=${MASKED} 已写入 ${ENV_FILE}"
echo
echo "下一步：在仓库根目录重启 trading_loop 验证 userFills 订阅，日志应出现："
echo "  [hl] start_ws_user_fills -> subscribed (user=${MASKED})"
