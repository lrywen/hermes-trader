#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SHADOW 采数进度 / 健康只读巡检脚本
==================================
用途：一次性盘点所有 shadow 臂的 JSONL 落盘情况——记录数、最后写入时间、
轮转副档、坏行（非 JSON）计数——并识别「该采却没在采」的臂：

  * 配置里 mode=shadow/enforce（即应当写数据）但目标文件不存在 / 长时间无更新；
  * 目标路径落在只读挂载（~/.hermes-trading）上 —— 写会静默失败（try/except
    不阻断热路径），表现为文件停在容器启动之前或根本不存在。

只读：不写任何文件、不改配置、不触碰交易热路径。容器内运行：

  python3 scripts/shadow_progress.py            # 盘点 + 健康检查
  python3 scripts/shadow_progress.py --json     # 机读输出
  python3 scripts/shadow_progress.py --dir /data  # 只看某目录

设计说明：
  * 路径解析与运行时一致 —— 优先各臂 shadow_log_path 配置块，其次 *_SHADOW_FILE
    环境变量，最后落 ~/.hermes-trading/<name>（只读挂载，写会失败）。
  * 同一个臂可能在两个目录各有一份（旧残留在 ~/.hermes-trading，新落在 /data），
    按「臂名 + 目录」分别列出，避免重复计数。
"""
import argparse
import glob
import json
import os
import sys
import time
from datetime import datetime, timezone

# 臂名 -> (配置块名, 环境变量名, 默认文件名, mode 键, 路径键)
# 覆盖 9 个公共 shadow_log 臂 + xs_reversal / regime_overlay 专用臂。
# Audit 2026-09-07 (shadow-progress fix):
#  * trend_filter 的写入侧 env 是 HERMES_TREND_FILTER_SHADOW_FILE /
#    HERMES_TREND_FILTER_MODE（risk_gates.py），巡检表此前误写 _200MA_，
#    导致该臂 env 覆盖路径/模式巡检失效 —— 已对齐写入侧。
#  * sizing_v2 并非独立配置块：mode 寄生在 atr_risk_sizing.sizing_v2_mode
#    （legacy 布尔 sizing_v2_enabled=true → enforce），路径键是
#    atr_risk_sizing.sizing_v2_shadow_log_path（executor.py）。此前按
#    cfg["sizing_v2"] 解析永远读不到，巡检把它误报为 off —— 已显式指定。
# Audit 2026-09-08 (arm registry fix):
#  * pullback 不是顶层配置块：开关在 runner_entry_gate.pullback_long 的
#    enabled + shadow_mode 双布尔（无 mode 键，shadow_mode 默认 False；
#    executor.py L4298/L4351）。生产配置 enabled=true/shadow_mode=true，
#    /data/pullback_shadow.jsonl 一直在写，巡检却按不存在的 cfg["pullback"]
#    恒报 off —— _arm_mode/_arm_path 对该臂特判，精确镜像运行时。
#  * regime_overlay 块是 regime_risk_overlay 的 enabled + shadow_mode 双布尔
#    （无 mode 键，shadow_mode 默认 True；regime_overlay.py L159-163）。此前
#    按 mode 键解析恒得 off（当前 enabled=False 结果碰巧正确，路径/模式键均
#    不匹配）—— 同型特判。
#  两臂均无 env mode 覆盖；映射：enabled 假→off；enabled 真 + shadow 真→
#  shadow；enabled 真 + shadow 假→enforce。
ARMS = [
    ("pullback",            "pullback",            "HERMES_PULLBACK_SHADOW_FILE",            "pullback_shadow.jsonl",            "mode",      "shadow_log_path"),
    ("ta_late_entry",       "ta_late_entry",       "HERMES_TA_LATE_ENTRY_SHADOW_FILE",       "ta_late_entry_shadow.jsonl",       "mode",      "shadow_log_path"),
    ("atr_regime_calib",    "atr_regime_calibration", "HERMES_ATR_REGIME_CALIB_SHADOW_FILE", "atr_regime_calib_shadow.jsonl",  "mode",      "shadow_log_path"),
    ("sizing_v2",           "atr_risk_sizing",     "HERMES_SIZING_V2_SHADOW_FILE",           "sizing_v2_shadow.jsonl",           "sizing_v2_mode", "sizing_v2_shadow_log_path"),
    ("confidence_decay",    "confidence_decay",    "HERMES_CONFIDENCE_DECAY_SHADOW_FILE",    "confidence_decay_shadow.jsonl",    "mode",      "shadow_log_path"),
    ("market_circuit",      "market_circuit",      "HERMES_MARKET_CIRCUIT_SHADOW_FILE",      "market_circuit_shadow.jsonl",      "mode",      "shadow_log_path"),
    ("signal_age_decay",    "signal_age_decay",    "HERMES_SIGNAL_AGE_DECAY_SHADOW_FILE",    "signal_age_decay_shadow.jsonl",    "mode",      "shadow_log_path"),
    ("daily_extension_cap", "daily_extension_cap", "HERMES_DAILY_EXTENSION_CAP_SHADOW_FILE", "daily_extension_cap_shadow.jsonl", "mode",      "shadow_log_path"),
    ("reentry_cap",         "reentry_cap",         "HERMES_REENTRY_CAP_SHADOW_FILE",         "reentry_cap_shadow.jsonl",         "mode",      "shadow_log_path"),
    ("trend_filter_200ma",  "trend_filter_200ma",  "HERMES_TREND_FILTER_SHADOW_FILE",        "trend_filter_shadow.jsonl",        "mode",      "shadow_log_path"),
    ("xs_reversal",         "xs_reversal",         "HERMES_XS_REVERSAL_SHADOW_FILE",         "xs_reversal_shadow.jsonl",         "mode",      "shadow_log_path"),
    ("regime_overlay",      "regime_risk_overlay", "HERMES_REGIME_OVERLAY_SHADOW_FILE",      "regime_overlay_shadow.jsonl",      "mode",      "shadow_log_path"),
]

# 只读挂载：落在这里的 shadow 文件写不进去（容器内 ro）。
READONLY_HOME = os.path.expanduser("~/.hermes-trading")
WRITABLE_DATA = "/data"


def _arm_mode(cfg: dict, blk_name: str, env_name: str, mode_key: str = "mode") -> str:
    """Mirror the runtime mode resolution: env override > config block > off.

    ``mode_key`` lets parasitic arms override the config field (sizing_v2 lives
    under atr_risk_sizing with mode key ``sizing_v2_mode`` and a legacy boolean
    ``sizing_v2_enabled`` that maps to enforce)."""
    # Audit 2026-09-08 (arm registry fix): dual-boolean arms have no mode key
    # and no env mode override -- resolve enabled/shadow_mode exactly like the
    # runtime does.
    if blk_name == "pullback":
        gate = cfg.get("runner_entry_gate")
        pb = gate.get("pullback_long") if isinstance(gate, dict) else None
        pb = pb if isinstance(pb, dict) else {}
        if not bool(pb.get("enabled", False)):
            return "off"
        return "shadow" if bool(pb.get("shadow_mode", False)) else "enforce"
    if blk_name == "regime_risk_overlay":
        ov = cfg.get("regime_risk_overlay")
        ov = ov if isinstance(ov, dict) else {}
        if not bool(ov.get("enabled", False)):
            return "off"
        return "shadow" if bool(ov.get("shadow_mode", True)) else "enforce"
    env_val = os.environ.get(env_name.replace("_SHADOW_FILE", "_MODE"), "").strip().lower()
    if env_val in ("off", "shadow", "enforce"):
        return env_val
    blk = cfg.get(blk_name)
    if isinstance(blk, dict):
        m = str(blk.get(mode_key, "")).strip().lower()
        if m in ("off", "shadow", "enforce"):
            return m
        # Legacy sizing_v2 boolean: enabled=true historically meant enforce.
        if mode_key == "sizing_v2_mode" and bool(blk.get("sizing_v2_enabled", False)):
            return "enforce"
        return "off"
    if isinstance(blk, bool):
        return "shadow" if blk else "off"
    return "off"


def _arm_path(cfg: dict, blk_name: str, env_file: str, default_name: str,
              path_key: str = "shadow_log_path") -> str:
    """Resolve exactly like the runtime: config path key > env > home default.

    ``path_key`` lets parasitic arms override the config field (sizing_v2 uses
    ``sizing_v2_shadow_log_path`` under the atr_risk_sizing block)."""
    blk = cfg.get(blk_name)
    if isinstance(blk, dict):
        p = str(blk.get(path_key) or "").strip()
        if p:
            return os.path.expanduser(p)
    env_p = os.environ.get(env_file, "").strip()
    if env_p:
        return os.path.expanduser(env_p)
    return os.path.join(READONLY_HOME, default_name)


def _file_stat(path: str) -> dict:
    """Count lines + bad (non-JSON) lines on the active file; count rotated siblings."""
    info = {"path": path, "exists": os.path.exists(path), "lines": 0,
            "bad_lines": 0, "last_mod": None, "age_min": None, "rotated": 0, "size": 0}
    if not info["exists"]:
        return info
    try:
        st = os.stat(path)
        info["size"] = st.st_size
        info["last_mod"] = datetime.fromtimestamp(st.st_mtime, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        info["age_min"] = round((time.time() - st.st_mtime) / 60.0, 1)
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for ln in fh:
                ln = ln.strip()
                if not ln:
                    continue
                info["lines"] += 1
                try:
                    json.loads(ln)
                except (json.JSONDecodeError, ValueError):
                    info["bad_lines"] += 1
        siblings = glob.glob(path + ".*") + glob.glob(path + ".bak-*")
        info["rotated"] = len(siblings)
    except OSError as e:
        info["error"] = str(e)
    return info


def collect() -> dict:
    cfg = {}
    try:
        from hermes_trader.agents.config_store import read_agent_config
        cfg = read_agent_config() or {}
    except Exception as e:
        cfg = {}
        print(f"[warn] 读取 agent 配置失败（将仅按 env/默认路径判断）：{e}", file=sys.stderr)

    arms, alerts = [], []
    for label, blk_name, env_file, default_name, mode_key, path_key in ARMS:
        mode = _arm_mode(cfg, blk_name, env_file, mode_key)
        path = _arm_path(cfg, blk_name, env_file, default_name, path_key)
        stat = _file_stat(path)
        on_readonly = os.path.dirname(os.path.abspath(path)) == os.path.abspath(READONLY_HOME)
        arms.append({"arm": label, "mode": mode, "path": path,
                     "on_readonly_mount": on_readonly, **stat})

        # ── 健康判定 ──────────────────────────────────────────────
        if mode in ("shadow", "enforce"):
            if on_readonly:
                alerts.append(f"[{label}] mode={mode} 但路径落在只读挂载 {path} —— 写入静默失败，采不到数！")
            elif not stat["exists"]:
                alerts.append(f"[{label}] mode={mode} 但文件不存在（尚无触发，或路径不可写）：{path}")
            elif stat["lines"] == 0:
                alerts.append(f"[{label}] mode={mode} 但 {path} 为空（尚未触发过）。")
        if stat.get("bad_lines"):
            alerts.append(f"[{label}] {path} 有 {stat['bad_lines']} 行非 JSON（坏行）。")
    return {"arms": arms, "alerts": alerts}


def _fmt_row(a: dict) -> str:
    if not a["exists"]:
        state = "缺失"
        last = "-"
        age = "-"
        rot = "-"
    else:
        state = str(a["lines"])
        last = a["last_mod"] or "-"
        age = (f"{a['age_min']:.0f}min" if a["age_min"] is not None else "-")
        rot = str(a["rotated"])
    flag = " [只读挂载!]" if a["on_readonly_mount"] and a["mode"] in ("shadow", "enforce") else ""
    return (f"{a['arm']:20s} {a['mode']:8s} {state:>7s} 行  最后写入 {last:16s} "
            f"({age:>8s}前)  轮转{rot:>2s}{flag}")


def main() -> int:
    ap = argparse.ArgumentParser(description="SHADOW 采数进度 / 健康只读巡检")
    ap.add_argument("--json", action="store_true", help="机读 JSON 输出")
    ap.add_argument("--dir", default=None, help="只盘点该目录下的 shadow 文件")
    args = ap.parse_args()

    data = collect()
    arms = data["arms"]
    if args.dir:
        arms = [a for a in arms if os.path.dirname(os.path.abspath(a["path"])) == os.path.abspath(args.dir)]

    if args.json:
        print(json.dumps({"arms": arms, "alerts": data["alerts"]}, ensure_ascii=False, indent=2))
        return 1 if data["alerts"] else 0

    print("=" * 96)
    print("SHADOW 采数进度盘点（只读）  " + datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"))
    print("=" * 96)
    print(f"{'臂':20s} {'mode':8s} {'记录':>8s}  {'最后写入 / 距今':30s} {'轮转':5s}")
    print("-" * 96)
    for a in arms:
        print(_fmt_row(a))
    print("-" * 96)
    active = [a for a in arms if a["mode"] in ("shadow", "enforce")]
    print(f"活跃臂（shadow/enforce）：{len(active)} / {len(arms)}")
    if data["alerts"]:
        print()
        print("⚠️  健康告警：")
        for al in data["alerts"]:
            print("  " + al)
    else:
        print()
        print("✓ 所有活跃臂路径可写且无坏行。")
    return 1 if data["alerts"] else 0


if __name__ == "__main__":
    sys.exit(main())
