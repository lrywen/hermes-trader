#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SHADOW 风控臂夜间评级 / 建议器（INERT，只读）
==============================================
Audit 2026-09-07 (Pathia 夜间自动评级器吸收):
Pathia 每晚由 scripts/autonomous_cycle.py 读 forward ledger，自动把表现转负的
book 降级（shadow_only）。Hermes 此前缺这一环 —— shadow 臂只会默默采数，
"该不该从 shadow 升 enforce / 该不该关掉"全靠人记得 24/72/168h 人工对账。

本脚本补上"自动评级"这半环，但**刻意不做自动降级/升级**（红线：不自动改生产
风控姿态；新闸门默认 inert；质量门 fail-closed 不削弱）。它：

  * 只读各臂 shadow JSONL（复用 shadow_progress 的 ARMS / 路径 / mode 解析，
    含 sizing_v2 寄生块与 trend_filter env 修正）+ 真实成交账本（memory）；
  * 在 24h / 72h / 168h 三窗口统计每条臂的触发量、would_block/would_change
    命中率、以及（若已由 reconcile_* 脚本回填）outcome 反事实盈亏；
  * 给出保守评级：PROMOTE_CANDIDATE（可人工考虑升 enforce）/ COLLECTING
    （继续采数）/ INSUFFICIENT_DATA（样本不足）/ DATA_GAP（该采却没采，
    最危险的"门变盲"变体）/ REVIEW（回填结果提示该臂无效或有害，建议复核）；
  * 输出中文报告 + 结构化 JSON；--push 时发飞书风险卡片（绝不抛异常、不阻断）。

**评级器永远不写配置、不下单、不改任何闸门状态**。PROMOTE_CANDIDATE 仅是
"样本够且信号健康，请人工拍板"的建议；真正切 enforce 仍走 config_store
权威写 + 人工确认。

容器内运行：
  python3 scripts/shadow_grade.py                 # 文本报告（dry-run，不推送）
  python3 scripts/shadow_grade.py --json          # 机读 JSON
  python3 scripts/shadow_grade.py --push          # 并发飞书风险卡片
  python3 scripts/shadow_grade.py --windows 24 72 168
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

# 复用同目录 shadow_progress 的臂表与解析逻辑（路径已在 Audit 2026-09-07 修正）。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import shadow_progress as sp  # noqa: E402

# ── 评级阈值（保守；只决定"建议"，不自动执行任何动作）───────────────────────
MIN_SAMPLES_PROMOTE = 60     # 168h 窗口内至少这么多条记录才谈晋升
MIN_SAMPLES_REVIEW = 30      # 达到这个量且回填结果差，才提示 REVIEW
MIN_MATURE_OUTCOMES = 20     # 反事实 outcome 至少这么多条才纳入盈亏判定
MIN_BLOCK_RATE = 0.03        # 拦截率低于此值 = 几乎从不触发，晋升无意义
MAX_FUTILE_BLOCK_RATE = 0.5  # 拦截命中率>此值 = 拦太宽，先复核别升级

# 评级档位
PROMOTE = "PROMOTE_CANDIDATE"
COLLECTING = "COLLECTING"
INSUFFICIENT = "INSUFFICIENT_DATA"
DATA_GAP = "DATA_GAP"
REVIEW = "REVIEW"
OFF = "OFF"

_VERDICT_CN = {
    PROMOTE: "可考虑升enforce(待人工拍板)",
    COLLECTING: "继续采数",
    INSUFFICIENT: "样本不足",
    DATA_GAP: "采数缺口(该采没采!)",
    REVIEW: "建议复核(疑似无效/有害)",
    OFF: "未启用(mode=off)",
}

# 臂分类：决定统计哪个"命中"字段。
#   block  —— "会拦"类：命中字段为 *would_block*（True 表示本该拦下）
#   change —— "会改"类：命中字段为 would_change / would_block_gate
#   signal —— 信号/状态类：只有 is_candidate / tripped，统计候选量
ARM_KIND = {
    "pullback": "signal",
    "ta_late_entry": "block",
    "atr_regime_calib": "change",
    "sizing_v2": "change",
    "confidence_decay": "change",
    "market_circuit": "signal",
    "signal_age_decay": "change",
    "daily_extension_cap": "block",
    "reentry_cap": "block",
    "trend_filter_200ma": "block",
    "xs_reversal": "signal",
    "regime_overlay": "signal",
}

_BLOCK_FIELDS = ("ext_would_block", "reentry_would_block", "trend_would_block",
                 "tight_would_block", "chase_would_block", "blocked", "would_block")
_CHANGE_FIELDS = ("would_change", "would_block_gate", "would_block")
_SIGNAL_FIELDS = ("is_candidate", "tripped", "would_apply")

# Audit 2026-09-07 (M1 frontend grading center): nightly run persists one slim
# verdict snapshot per arm so the dashboard can draw verdict/maturity trends.
# Written ONLY by the nightly cron path (main), never by the API refresh —
# manual operator refreshes must not fabricate "nightly" history. Best-effort:
# a write failure is swallowed and never changes the report/exit code.
HISTORY_FILE = os.environ.get("HERMES_SHADOW_GRADE_HISTORY",
                              os.path.join(sp.WRITABLE_DATA, "shadow_grade_history.jsonl"))
HISTORY_MAX_LINES = 400   # ~13 months at one line/night; cap enforced best-effort


def _sizing_v2_changed(rec: dict):
    """sizing_v2 logs no boolean flag; it "changes sizing" when the v2 computed
    notional differs materially from the v1 baseline."""
    v1, v2 = rec.get("v1_notional_usd"), rec.get("v2_notional_usd")
    if not isinstance(v1, (int, float)) or not isinstance(v2, (int, float)):
        return None
    if v1 <= 0:
        return None
    return abs(v2 - v1) / v1 > 0.01   # >1% notional shift counts as a change


# ── §8.1 sizing_v2 成本上限（CS-G）168h 六条件闸门（只读聚合，永不改姿态）────
# docs/sizing-v2-rollout.md §8.1：六项全满足才允许把 sizing_v2 成本上限列入
# PROMOTE_CANDIDATE（仍需 §4 六条 + 人工晋升）。任何一项不满足都只延长观察，
# 不调闸门、不改阈值、不改 trading_loop。口径全部来自 shadow JSONL，离线可复算。
SV2_COST_WINDOW_H = 168
SV2_COST_MIN_PER_SIDE = 10          # 条件1：long / short 各 ≥10 笔，不跨方向凑
SV2_COST_MAX_COLD_RATE = 0.20       # 条件2：冷启动来源占比 ≤20%
SV2_COST_MAX_CAP_BIND_RATE = 0.50   # 条件3：cap_binds=true 占比 >50% 阻断
SV2_COST_RATIO_P50_LOW = 0.5        # 条件4：ratio P50 ∈ [0.5, 1.0]
SV2_COST_RATIO_P50_HIGH = 1.0
SV2_COST_MAX_GT1_RATE = 0.05        # 条件4：系统性 ratio>1（>5%）视为阻断
SV2_COST_CARRY_TOL_PCT = 0.005      # 条件5：carry 重算与埋点偏差容忍（百分点）
# 文档 §8.1 写 `fallback`；实际埋点（memory.avg_exit_slip_bps_side /
# avg_hold_hours_side 降级链）冷启动取值为 default，executor 捕获异常时滑点为
# legacy。二者均代表"没有同币种/同方向实测、回退到默认/旧口径"，统一按冷启动计。
SV2_COST_COLD_SOURCES = frozenset({"default", "legacy"})

SV2_COST_NUMERIC_FIELDS = (
    "v2_cost_slip_bps", "v2_cost_slip_extra_pct", "v2_cost_fee_rt_pct",
    "v2_cost_fee_measured_bps", "v2_cost_hold_hours", "v2_cost_carry_pct",
    "v2_cost_borrow_bps", "v2_cost_denom_pct", "v2_cost_notional_usd",
    "v2_cost_notional_clamped_usd", "v2_cost_vs_v2_ratio",
)
SV2_COST_SOURCE_FIELDS = ("v2_cost_slip_source", "v2_cost_hold_source")
SV2_COST_BOOL_FIELD = "v2_cost_cap_binds"


def _is_finite_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) \
        and v == v and v not in (float("inf"), float("-inf"))


def _percentile(values: list[float], q: float) -> float | None:
    """Nearest-rank percentile (q ∈ [0,1]); None for an empty series."""
    if not values:
        return None
    s = sorted(values)
    idx = max(0, min(len(s) - 1, int(round(q * (len(s) - 1)))))
    return s[idx]


def _sv2_is_cost_record(rec: dict) -> bool:
    """A post-CS-G record is identifiable by the cost block marker
    (``v2_cost_slip_source`` is written atomically with the whole v2_cost dict;
    pre-CS-G records never carry it). Use the source marker rather than a
    numeric field so a record with a missing/NaN numeric field is kept and
    counted as a c6 anomaly instead of being dropped silently."""
    return rec.get("v2_cost_slip_source") is not None


def _sv2_cold(rec: dict) -> bool:
    """条件2口径：滑点或持仓时长任一来源是冷启动（default/legacy）即计冷启动。"""
    return str(rec.get("v2_cost_slip_source", "")) in SV2_COST_COLD_SOURCES \
        or str(rec.get("v2_cost_hold_source", "")) in SV2_COST_COLD_SOURCES


def _sv2_field_anomalies(rec: dict) -> list[str]:
    """条件6（埋点异常子项）：返回该记录的字段级异常描述（空=正常）。"""
    out = []
    for f in SV2_COST_NUMERIC_FIELDS:
        v = rec.get(f)
        if not _is_finite_num(v):
            out.append(f"{f}非数值")
    for f in SV2_COST_SOURCE_FIELDS:
        if not isinstance(rec.get(f), str) or not rec.get(f):
            out.append(f"{f}缺失")
    if not isinstance(rec.get(SV2_COST_BOOL_FIELD), bool):
        out.append("v2_cost_cap_binds非布尔")
    side = rec.get("side")
    if side not in ("long", "short"):
        out.append(f"side非法({side!r})")
    denom = rec.get("v2_cost_denom_pct")
    if _is_finite_num(denom) and denom <= 0:
        out.append("denom_pct<=0")
    n1, n2 = rec.get("v2_cost_notional_usd"), rec.get("v2_cost_notional_clamped_usd")
    if _is_finite_num(n1) and _is_finite_num(n2) and n2 > n1 + 1e-6:
        out.append("clamped>notional")
    cap = rec.get(SV2_COST_BOOL_FIELD)
    if cap is False and _is_finite_num(n1) and _is_finite_num(n2) and n2 < n1 - 1e-6:
        out.append("cap_binds=false但notional被夹")
    return out


def _sv2_carry_ok(rec: dict) -> tuple[bool, str]:
    """条件5：carry 与 funding×hold×side 同号同量级（按埋点公式离线复算）。

    funding_rate_hr 为 None 时期望 carry=0（无资金费率输入）；borrow_bps=0
    只加"未计借币"标注，不在此否决。"""
    f = rec.get("v2_cost_funding_rate_hr")
    hold = rec.get("v2_cost_hold_hours")
    carry = rec.get("v2_cost_carry_pct")
    if f is None:
        if _is_finite_num(carry) and abs(carry) <= SV2_COST_CARRY_TOL_PCT:
            return True, ""
        return False, "无funding却有carry"
    if not (_is_finite_num(f) and _is_finite_num(hold) and _is_finite_num(carry)):
        return False, "carry/funding/hold非数值"
    sign = 1.0 if rec.get("side") == "long" else -1.0
    # 与 executor 同式：max(0, funding*hold*sign)，单位百分点。
    expected = max(0.0, f * hold * sign) * 100.0
    if abs(expected - carry) <= SV2_COST_CARRY_TOL_PCT:
        return True, ""
    return False, (f"carry复算不符({carry:.4f} vs {expected:.4f})")


def _sv2_cost_side_block(rows: list[dict]) -> dict:
    """Per-direction aggregates used by conditions 1/3/4."""
    n = len(rows)
    cap = sum(1 for r in rows if r.get("v2_cost_cap_binds") is True)
    ratios = [float(r["v2_cost_vs_v2_ratio"]) for r in rows
              if _is_finite_num(r.get("v2_cost_vs_v2_ratio"))]
    gt1 = sum(1 for x in ratios if x > SV2_COST_RATIO_P50_HIGH + 1e-9)
    return {
        "n": n,
        "cap_binds": cap,
        "cap_bind_rate": round(cap / n, 4) if n else 0.0,
        "ratio_p50": round(_percentile(ratios, 0.5), 4) if ratios else None,
        "ratio_gt1_rate": round(gt1 / len(ratios), 4) if ratios else 0.0,
    }


def grade_sizing_v2_cost(records: list[dict], window_h: int = SV2_COST_WINDOW_H,
                         now_ms: float | None = None) -> dict:
    """§8.1 六条件纯函数聚合（只读，不 I/O）。

    输入为 sizing_v2_shadow.jsonl 全部记录（含轮转）；旧记录（无 v2_cost_*）
    与窗口外记录自动忽略。样本为 0 时给 COLLECTING（继续采数），绝不报
    DATA_GAP —— 成本块刚上线，无信号 ≠ 门变盲。返回逐项 pass/fail/hold。"""
    now_ms = now_ms if now_ms is not None else time.time() * 1000.0
    cutoff = now_ms - window_h * 3600.0 * 1000.0
    rows = []
    for rec in records:
        ts = _record_ts_ms(rec)
        if ts is None or ts < cutoff or not _sv2_is_cost_record(rec):
            continue
        rows.append(rec)

    checks = {}
    n = len(rows)
    by_side = {s: [r for r in rows if r.get("side") == s] for s in ("long", "short")}
    sides = {s: _sv2_cost_side_block(v) for s, v in by_side.items()}
    n_long, n_short = sides["long"]["n"], sides["short"]["n"]

    # 条件1：分方向样本量（不足延长窗口，不跨方向凑数）
    c1 = n_long >= SV2_COST_MIN_PER_SIDE and n_short >= SV2_COST_MIN_PER_SIDE
    checks["c1_sample_per_side"] = {
        "pass": c1, "hold": not c1,
        "detail": f"long {n_long}/{SV2_COST_MIN_PER_SIDE}，short {n_short}/{SV2_COST_MIN_PER_SIDE}",
    }

    # 条件2：来源成熟度（冷启动 default/legacy 占比 ≤20%）
    cold = sum(1 for r in rows if _sv2_cold(r))
    cold_rate = cold / n if n else 0.0
    c2 = n > 0 and cold_rate <= SV2_COST_MAX_COLD_RATE
    checks["c2_source_maturity"] = {
        "pass": c2, "hold": not c2,
        "detail": f"冷启动来源 {cold}/{n} = {cold_rate:.1%}（上限 {SV2_COST_MAX_COLD_RATE:.0%}）",
    }

    # 条件3：上限绑定率（>50% 阻断；分方向率供"差异可解释"人工核对）
    cap_all = sum(1 for r in rows if r.get("v2_cost_cap_binds") is True)
    cap_rate = cap_all / n if n else 0.0
    c3 = n > 0 and cap_rate <= SV2_COST_MAX_CAP_BIND_RATE
    checks["c3_cap_bind_rate"] = {
        "pass": c3, "hold": not c3,
        "detail": (f"cap_binds {cap_all}/{n} = {cap_rate:.1%}（阻断线 >"
                   f"{SV2_COST_MAX_CAP_BIND_RATE:.0%}）；分方向 long "
                   f"{sides['long']['cap_bind_rate']:.1%} / short "
                   f"{sides['short']['cap_bind_rate']:.1%}（差异须可解释）"),
    }

    # 条件4：比率合理性 P50 ∈ [0.5,1.0]，且无系统性 ratio>1
    ratios = [float(r["v2_cost_vs_v2_ratio"]) for r in rows
              if _is_finite_num(r.get("v2_cost_vs_v2_ratio"))]
    p50 = _percentile(ratios, 0.5)
    gt1 = sum(1 for x in ratios if x > SV2_COST_RATIO_P50_HIGH + 1e-9)
    gt1_rate = gt1 / len(ratios) if ratios else 0.0
    p50_ok = p50 is not None and SV2_COST_RATIO_P50_LOW <= p50 <= SV2_COST_RATIO_P50_HIGH
    c4 = n > 0 and p50_ok and gt1_rate <= SV2_COST_MAX_GT1_RATE
    checks["c4_ratio_sanity"] = {
        "pass": c4, "hold": not c4,
        "detail": (f"ratio P50={p50 if p50 is None else round(p50, 4)} "
                   f"（区间 [{SV2_COST_RATIO_P50_LOW}, {SV2_COST_RATIO_P50_HIGH}]），"
                   f"ratio>1 占比 {gt1_rate:.1%}（系统性 >{SV2_COST_MAX_GT1_RATE:.0%} 阻断）；"
                   f"short P50={sides['short']['ratio_p50']}（须与空头 carry 符号方向一致）"),
    }

    # 条件5：carry 校验（同号同量级）+ borrow 标注
    carry_bad = []
    for r in rows:
        ok, why = _sv2_carry_ok(r)
        if not ok:
            carry_bad.append(f"{r.get('coin', '?')}:{why}")
    borrow_zero = all((_is_finite_num(r.get("v2_cost_borrow_bps"))
                       and float(r["v2_cost_borrow_bps"]) == 0.0) for r in rows)
    c5 = n > 0 and not carry_bad
    c5_detail = (f"carry复算不符 {len(carry_bad)}/{n}"
                 + (f"（如 {', '.join(carry_bad[:3])}）" if carry_bad else "（全部符合同号同量级）"))
    if borrow_zero and n > 0:
        c5_detail += "；v2_cost_borrow_bps 全为 0 → 结论标注'未计借币'"
    checks["c5_carry_check"] = {"pass": c5, "hold": not c5, "detail": c5_detail}

    # 条件6：零路径副作用（shadow-only、埋点无异常；applied 路径与 §5 触发
    # 无法从 shadow 文件观测，由 collect/report 侧补人工核对项）
    non_shadow = [f"{r.get('coin', '?')}:{r.get('mode')}" for r in rows
                  if r.get("mode") != "shadow"]
    anomalies = []
    for r in rows:
        for why in _sv2_field_anomalies(r):
            anomalies.append(f"{r.get('coin', '?')}:{why}")
    c6 = n > 0 and not non_shadow and not anomalies
    c6_detail = (f"非shadow记录 {len(non_shadow)}，埋点异常 {len(anomalies)}"
                 + (f"（如 {', '.join(anomalies[:3])}）" if anomalies else ""))
    checks["c6_zero_side_effects"] = {
        "pass": c6, "hold": not c6,
        "detail": c6_detail + "；applied 仓位零变化/§5 回滚零触发须人工核对",
        "manual_required": [
            "applied 仓位零变化（v2 仍 false / cap 路径不变）",
            "§5 回滚条件零触发（5%漂移断言/regime detect失败/影子回撤扩大/ROE 损失）",
        ],
    }

    all_pass = all(c["pass"] for c in checks.values())
    if n == 0:
        gate = COLLECTING
        gate_reason = f"{window_h}h 窗口内 0 条 v2_cost 记录（CS-G 刚上线，继续采数）"
    elif all_pass:
        gate = PROMOTE
        gate_reason = "§8.1 六条件全部满足（仍需 §4 六条 + 人工晋升）"
    else:
        gate = COLLECTING
        fails = [k for k, c in checks.items() if not c["pass"]]
        gate_reason = f"§8.1 {len(fails)}/6 项未满足（{', '.join(fails)}）→ 延长观察，不调闸门"

    return {
        "window_h": window_h, "n": n, "n_long": n_long, "n_short": n_short,
        "cold_rate": round(cold_rate, 4), "cap_bind_rate": round(cap_rate, 4),
        "ratio_p50": round(p50, 4) if p50 is not None else None,
        "ratio_gt1_rate": round(gt1_rate, 4),
        "carry_mismatch": len(carry_bad), "borrow_all_zero": borrow_zero,
        "non_shadow_records": len(non_shadow), "field_anomalies": len(anomalies),
        "sides": sides, "checks": checks,
        "all_pass": all_pass, "gate": gate, "gate_reason": gate_reason,
    }


def _pullback_candidate(rec: dict):
    """pullback gate records a composite score; a positive score marks a
    candidate the gate flagged."""
    s = rec.get("composite_score")
    if not isinstance(s, (int, float)):
        return None
    return s > 0


def _hit_field(rec: dict, kind: str, arm: str = ""):
    """Return True/False if the record carries a hit decision, else None.

    Field names differ per writer; a couple of arms use bespoke conventions
    (ta_late_entry `blocked`, sizing_v2 v1/v2 notional delta, pullback score)."""
    if arm == "sizing_v2":
        h = _sizing_v2_changed(rec)
        if h is not None:
            return h
    if arm == "pullback":
        h = _pullback_candidate(rec)
        if h is not None:
            return h
    fields = {"block": _BLOCK_FIELDS, "change": _CHANGE_FIELDS,
              "signal": _SIGNAL_FIELDS}[kind]
    for f in fields:
        if f in rec and rec[f] is not None:
            return bool(rec[f])
    return None


def _record_ts_ms(rec: dict):
    """Extract a record timestamp as epoch millis. Handles both conventions:
    ISO string (`timestamp`) and millisecond epoch (`ts` / numeric `timestamp`)."""
    for key in ("ts", "timestamp", "bar_close_ms"):
        v = rec.get(key)
        if v is None:
            continue
        if isinstance(v, (int, float)):
            # Heuristic: >10^12 is already millis; otherwise seconds.
            return float(v) if v > 1e12 else float(v) * 1000.0
        if isinstance(v, str):
            s = v.strip().replace("Z", "+00:00")
            try:
                dt = datetime.fromisoformat(s)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.timestamp() * 1000.0
            except ValueError:
                continue
    return None


def _shadow_files(path: str) -> list[str]:
    """Active shadow file plus its daily/size-rotated siblings, oldest->newest.

    Audit 2026-09-08 (rotation-aware grading): shadow_log.append_jsonl rotates
    each local day (and on 10 MiB) through ``<path>.1``..``<path>.5`` (see
    hermes_trader/shadow_log.py). The active file therefore holds only the
    current day, but the grader's longest window is 168h. We merge the active
    file with the numeric rotated siblings so the trailing windows actually see
    history. ``.bak-*`` manual snapshots are skipped (non-numeric suffix). The
    grader is strictly INERT: it only ever READS these files, never writes.
    """
    import glob as _glob
    files = []
    nums = []
    for p in _glob.glob(f"{path}.*"):
        suf = p[len(path) + 1:]
        if suf.isdigit():
            nums.append((int(suf), p))
    # .5 is oldest, .1 is newest sibling; read oldest-first so the active file
    # (appended last) wins if a line ever appears twice.
    for _, p in sorted(nums, key=lambda x: -x[0]):
        files.append(p)
    if os.path.exists(path):
        files.append(path)
    return files


def _read_jsonl(path: str) -> list[dict]:
    """Read a shadow JSONL and its rotated siblings (best-effort).

    Merges the active file with numeric ``.1``..``.5`` rotations and de-dups by
    raw line so a record present in two files is counted once. Read-only: the
    grader never writes back (INERT)."""
    out = []
    if not path:
        return out
    seen_lines: set[str] = set()
    for fpath in _shadow_files(path):
        try:
            with open(fpath, "r", encoding="utf-8", errors="replace") as fh:
                for ln in fh:
                    ln = ln.strip()
                    if not ln or ln in seen_lines:
                        continue
                    seen_lines.add(ln)
                    try:
                        out.append(json.loads(ln))
                    except (json.JSONDecodeError, ValueError):
                        continue
        except OSError:
            continue
    return out


def _window_stats(records: list[dict], kind: str, window_h: int, now_ms: float,
                  arm: str = "") -> dict:
    """Aggregate hit/outcome stats for records within the trailing window."""
    cutoff = now_ms - window_h * 3600.0 * 1000.0
    total = 0
    hits = 0
    decisions = 0
    outcomes = []          # backfilled counterfactual outcomes: win/loss strings
    pnl_usd = []
    for rec in records:
        ts = _record_ts_ms(rec)
        if ts is not None and ts < cutoff:
            continue
        total += 1
        hit = _hit_field(rec, kind, arm)
        if hit is not None:
            decisions += 1
            if hit:
                hits += 1
        oc = rec.get("outcome")
        if oc in ("win", "loss"):
            outcomes.append(oc)
        p = rec.get("pnl_usd")
        if isinstance(p, (int, float)):
            pnl_usd.append(float(p))
    return {
        "window_h": window_h, "total": total, "hits": hits, "decisions": decisions,
        "hit_rate": round(hits / decisions, 4) if decisions else 0.0,
        "mature_outcomes": len(outcomes),
        "outcome_wins": outcomes.count("win"),
        "outcome_losses": outcomes.count("loss"),
        "pnl_usd_sum": round(sum(pnl_usd), 4) if pnl_usd else 0.0,
        "has_pnl": bool(pnl_usd),
    }


def grade_arm(arm: str, mode: str, path: str, windows: list[int],
              now_ms: float | None = None, records: list[dict] | None = None) -> dict:
    """Grade one arm purely from its records + mode. Pure function (no I/O when
    `records` is supplied) so it is unit-testable."""
    now_ms = now_ms if now_ms is not None else time.time() * 1000.0
    kind = ARM_KIND.get(arm, "block")
    if records is None:
        records = _read_jsonl(path)
    stats = [_window_stats(records, kind, w, now_ms, arm) for w in windows]
    w_long = max(windows)
    longest = next((s for s in stats if s["window_h"] == w_long), stats[-1])

    # DATA_GAP: configured to collect but the file yields nothing in the longest
    # window. Distinguish "never triggered / path blind" from "no opportunity".
    if mode in ("shadow", "enforce") and longest["total"] == 0:
        verdict = DATA_GAP
        reason = f"mode={mode} 但 {w_long}h 窗口内 0 条记录（未触发或路径写不进）"
    elif mode == "off":
        # Arm disabled → nothing is expected; report honestly instead of
        # implying data collection is underway (never pushed to Feishu).
        verdict = OFF
        reason = f"mode=off 未启用（{w_long}h {longest['total']} 条残留记录）"
    elif longest["total"] < MIN_SAMPLES_PROMOTE:
        verdict = INSUFFICIENT if mode == "shadow" else COLLECTING
        reason = (f"{w_long}h 仅 {longest['total']} 条 (<{MIN_SAMPLES_PROMOTE})，"
                  f"继续累积样本")
    elif longest["mature_outcomes"] >= MIN_MATURE_OUTCOMES:
        # Backfilled counterfactual results are available → judge effectiveness.
        wr = longest["outcome_wins"] / max(1, longest["mature_outcomes"])
        if longest["has_pnl"] and longest["pnl_usd_sum"] > 0 and longest["hits"] > 0:
            verdict = REVIEW
            reason = (f"拦截/调整命中 {longest['hits']} 次，但回填反事实合计 "
                      f"${longest['pnl_usd_sum']:.2f} 为正 —— 闸门可能误伤盈利交易")
        elif wr > MAX_FUTILE_BLOCK_RATE and kind in ("block", "change"):
            # Audit 2026-09-08 (CS-C change-arm gate fix): outcome "win" means
            # arm-HARMFUL for ALL arms. For a change arm wr is therefore the
            # arm-HARMFUL rate, not a block win-rate — the same futile/harmful
            # threshold must gate change arms too, or a harmful change arm
            # (e.g. confidence_decay / atr_regime_calib, which never write
            # pnl_usd and so cannot be caught by the sum branch above) would
            # be mislabelled PROMOTE_CANDIDATE.
            verdict = REVIEW
            if kind == "change":
                reason = (f"回填反事实 {longest['mature_outcomes']} 条中臂有害率 "
                          f"{wr:.0%} > {MAX_FUTILE_BLOCK_RATE:.0%} —— "
                          f"调整/跳过反而放弃盈利，疑似有害，勿升级")
            else:
                reason = (f"被拦信号 {longest['mature_outcomes']} 条中胜率 "
                          f"{wr:.0%} > {MAX_FUTILE_BLOCK_RATE:.0%} —— 拦太宽，疑似误伤")
        else:
            verdict = PROMOTE if mode == "shadow" else COLLECTING
            if kind == "change":
                # Audit 2026-09-08 (change-arm label fix): outcome "win" is
                # encoded for ALL arms as "counterfactual pnl > 0 => the arm
                # foregoes profit / hurts". For a change arm that means win =
                # arm-HARMFUL and loss = arm-BENEFICIAL, so the raw win-rate is
                # inverted vs the plain reading. Report the arm-beneficial rate
                # (1 - wr) instead to avoid misleading the rollout decision.
                reason = (f"{w_long}h {longest['total']} 条、命中率 "
                          f"{longest['hit_rate']:.1%}、反事实臂有益率 "
                          f"{1 - wr:.0%}（outcome 负={longest['outcome_losses']}/"
                          f"{longest['mature_outcomes']}），信号健康")
            else:
                reason = (f"{w_long}h {longest['total']} 条、命中率 "
                          f"{longest['hit_rate']:.1%}、回填胜率 {wr:.0%}，信号健康")
    else:
        # Enough records but no backfilled outcomes yet.
        rate = longest["hit_rate"]
        if longest["decisions"] > 0 and rate < MIN_BLOCK_RATE and kind in ("block", "change"):
            verdict = COLLECTING
            reason = (f"{w_long}h {longest['total']} 条但命中率仅 {rate:.2%} "
                      f"(<{MIN_BLOCK_RATE:.0%})，闸门几乎不触发，先继续观察")
        else:
            verdict = PROMOTE if mode == "shadow" else COLLECTING
            reason = (f"{w_long}h {longest['total']} 条、命中率 {rate:.1%}；"
                      f"尚无回填 outcome，建议跑 reconcile 后再定")
    out = {"arm": arm, "mode": mode, "kind": kind, "path": path,
           "verdict": verdict, "verdict_cn": _VERDICT_CN[verdict],
           "reason": reason, "windows": stats}
    # CS-G §8.1：sizing_v2 成本上限六条件（独立 168h 只读闸门，不改上面的
    # 臂级 verdict；仅供晋升时与 §4 闸门并列人工核对）。
    if arm == "sizing_v2":
        out["sv2_cost"] = grade_sizing_v2_cost(
            records, window_h=SV2_COST_WINDOW_H, now_ms=now_ms)
    return out


def collect_grades(windows: list[int]) -> dict:
    """Read config, grade every arm, attach real-trade baseline. Read-only."""
    cfg = {}
    try:
        from hermes_trader.agents.config_store import read_agent_config
        cfg = read_agent_config() or {}
    except Exception as e:  # pragma: no cover - depends on runtime env
        print(f"[warn] 读取 agent 配置失败（按 env/默认路径判断）：{e}",
              file=sys.stderr)

    now_ms = time.time() * 1000.0
    arms = []
    for label, blk_name, env_file, default_name, mode_key, path_key in sp.ARMS:
        mode = sp._arm_mode(cfg, blk_name, env_file, mode_key)
        path = sp._arm_path(cfg, blk_name, env_file, default_name, path_key)
        arms.append(grade_arm(label, mode, path, windows, now_ms=now_ms))

    baseline = {"real_closes": 0, "real_win_rate": None, "note": ""}
    try:
        from hermes_trader.agents.memory import memory
        ps = memory.get_payoff_stats(limit=500)
        baseline["real_closes"] = int(ps.get("n", 0))
        wr = memory.get_win_rate()
        if wr.get("total"):
            baseline["real_win_rate"] = round(wr.get("rate", 0.0), 4)
        if baseline["real_closes"] == 0:
            baseline["note"] = "SHADOW 模式无真实成交，forward ledger 为空 —— 评级仅基于 shadow 采数，缺真钱对照"
    except Exception as e:  # pragma: no cover
        baseline["note"] = f"真实账本读取失败：{e}"

    return {"generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "windows_h": windows, "arms": arms, "real_baseline": baseline}


def _fmt_report(d: dict) -> str:
    lines = ["=" * 100,
             f"SHADOW 风控臂夜间评级（只读·建议，不自动改配置）  {d['generated_at']}",
             "=" * 100,
             f"{'臂':20s} {'mode':8s} {'类型':6s} {'评级':28s} 说明",
             "-" * 100]
    order = {DATA_GAP: 0, REVIEW: 1, PROMOTE: 2, INSUFFICIENT: 3, COLLECTING: 4, OFF: 5}
    for a in sorted(d["arms"], key=lambda x: order.get(x["verdict"], 9)):
        lines.append(f"{a['arm']:20s} {a['mode']:8s} {a['kind']:6s} "
                     f"{a['verdict_cn']:28s} {a['reason']}")
        # Audit 2026-09-08 (change-arm label fix): outcome win/loss is encoded
        # for all arms as "win = counterfactual pnl>0 => the arm foregoes profit
        # (arm hurts)". For change arms a "win" therefore means arm-HARMFUL;
        # relabel so the night report doesn't read as the arm's own win-rate.
        if a.get("kind") == "change":
            oc_good, oc_bad = "臂有益", "臂有害"
        else:
            oc_good, oc_bad = "胜", "负"
        for s in a["windows"]:
            lines.append(f"{'':36s}{s['window_h']:>4d}h: {s['total']:>5d} 条  "
                         f"命中 {s['hits']:>4d}/{s['decisions']:<4d} "
                         f"({s['hit_rate']:.1%})  回填 outcome {s['mature_outcomes']} "
                         f"({oc_good}{s['outcome_losses']}/{oc_bad}{s['outcome_wins']})")
        cost = a.get("sv2_cost")
        if cost:
            _COST_CN = {
                "c1_sample_per_side": "条件1 分方向样本",
                "c2_source_maturity": "条件2 来源成熟度",
                "c3_cap_bind_rate": "条件3 上限绑定率",
                "c4_ratio_sanity": "条件4 比率合理性",
                "c5_carry_check": "条件5 carry校验",
                "c6_zero_side_effects": "条件6 零副作用",
            }
            tag = "六条件全过" if cost["all_pass"] else "未全过(延长观察)"
            lines.append(f"{'':36s}§8.1 成本上限 168h：n={cost['n']} "
                         f"(long {cost['n_long']}/short {cost['n_short']}) → {tag}")
            for ck in ("c1_sample_per_side", "c2_source_maturity", "c3_cap_bind_rate",
                       "c4_ratio_sanity", "c5_carry_check", "c6_zero_side_effects"):
                c = cost["checks"][ck]
                lines.append(f"{'':40s}[{'PASS' if c['pass'] else 'HOLD'}] "
                             f"{_COST_CN[ck]}：{c['detail']}")
    b = d["real_baseline"]
    lines.append("-" * 100)
    lines.append(f"真实成交基线：closes={b['real_closes']}  "
                 f"win_rate={b['real_win_rate']}  {b['note']}")
    n_gap = sum(1 for a in d["arms"] if a["verdict"] == DATA_GAP)
    n_prom = sum(1 for a in d["arms"] if a["verdict"] == PROMOTE)
    n_rev = sum(1 for a in d["arms"] if a["verdict"] == REVIEW)
    lines.append("-" * 100)
    lines.append(f"汇总：采数缺口 {n_gap} / 可考虑升级 {n_prom} / 建议复核 {n_rev}。"
                 f"所有 PROMOTE_CANDIDATE 均需人工 reconcile + config_store 权威写后才生效。")
    return "\n".join(lines)


def _push_feishu(d: dict) -> None:
    """Push a risk card summarising anything that needs human attention.
    Never raises / never blocks (notify.send_card already swallows)."""
    try:
        from hermes_trader import notify
    except Exception as e:  # pragma: no cover
        print(f"[warn] notify 不可用，跳过飞书推送：{e}", file=sys.stderr)
        return
    gaps = [a for a in d["arms"] if a["verdict"] == DATA_GAP]
    reviews = [a for a in d["arms"] if a["verdict"] == REVIEW]
    promos = [a for a in d["arms"] if a["verdict"] == PROMOTE]
    cost_blocks = [a for a in d["arms"]
                   if a.get("sv2_cost") and a["sv2_cost"]["n"] > 0
                   and not a["sv2_cost"]["all_pass"]]
    if not (gaps or reviews or promos or cost_blocks):
        return
    fields = {
        "采数缺口(门变盲)": f"{len(gaps)} 条：{', '.join(a['arm'] for a in gaps) or '无'}",
        "建议复核": f"{len(reviews)} 条：{', '.join(a['arm'] for a in reviews) or '无'}",
        "可考虑升级(待人工)": f"{len(promos)} 条：{', '.join(a['arm'] for a in promos) or '无'}",
        "真实成交数": str(d["real_baseline"]["real_closes"]),
    }
    md_lines = ["**评级器只读建议，不会自动改任何闸门/配置。**", ""]
    for a in gaps + reviews + promos:
        md_lines.append(f"- `{a['arm']}` ({a['mode']}) → **{a['verdict_cn']}**：{a['reason']}")
    # CS-G §8.1：已有 v2_cost 样本但六条件未全过 → 明确阻断晋升（零样本不告警）。
    for a in d["arms"]:
        cost = a.get("sv2_cost")
        if cost and cost["n"] > 0 and not cost["all_pass"]:
            n_hold = sum(1 for c in cost["checks"].values() if not c["pass"])
            md_lines.append(f"- `sizing_v2` §8.1 成本上限：{n_hold}/6 项未满足 → "
                            f"**阻断晋升，延长观察**（{cost['gate_reason']}）")
    notify.send_card(
        "SHADOW 风控臂夜间评级（需人工关注）",
        fields=fields,
        category="risk",
        level="danger" if gaps else "warning",
        markdown="\n".join(md_lines),
        dedup_key="shadow_grade_nightly",
    )


def _slim_snapshot(d: dict) -> dict:
    """Project a full grade report down to one slim, JSONL-friendly history
    line: per-arm verdict/mode + per-window counts the trend chart needs
    (total/hits/decisions/hit_rate/mature_outcomes). Audit 2026-09-08 (CS-C
    window-scoped history): all windows are kept under `windows` so the 24h/72h
    trend survives the nightly snapshot; the flat longest-window fields are
    retained for older readers / pre-CS-C history rows."""
    w_long = max(d.get("windows_h") or [168])
    _WIN_KEYS = ("window_h", "total", "hits", "decisions", "hit_rate",
                 "mature_outcomes")
    arms = []
    for a in d.get("arms", []):
        longest = next((s for s in a.get("windows", [])
                        if s.get("window_h") == w_long), {})
        windows = [{k: s.get(k, 0) for k in _WIN_KEYS}
                   for s in a.get("windows", [])]
        row = {
            "arm": a.get("arm"),
            "mode": a.get("mode"),
            "kind": a.get("kind"),
            "verdict": a.get("verdict"),
            "total": longest.get("total", 0),
            "hits": longest.get("hits", 0),
            "decisions": longest.get("decisions", 0),
            "hit_rate": longest.get("hit_rate", 0.0),
            "mature_outcomes": longest.get("mature_outcomes", 0),
            "windows": windows,
        }
        cost = a.get("sv2_cost")
        if cost:
            row["sv2_cost"] = {
                "n": cost["n"], "n_long": cost["n_long"], "n_short": cost["n_short"],
                "all_pass": cost["all_pass"], "gate": cost["gate"],
                "cap_bind_rate": cost["cap_bind_rate"], "ratio_p50": cost["ratio_p50"],
                "cold_rate": cost["cold_rate"], "carry_mismatch": cost["carry_mismatch"],
                "field_anomalies": cost["field_anomalies"],
            }
        arms.append(row)
    return {
        "ts": int(time.time() * 1000),
        "generated_at": d.get("generated_at"),
        "window_h": w_long,
        "real_closes": (d.get("real_baseline") or {}).get("real_closes", 0),
        "arms": arms,
    }


def append_history(d: dict, path: str | None = None) -> bool:
    """Best-effort: append one slim snapshot line to the nightly grade history.
    Returns True on success. Never raises — the grader must stay read-only
    with respect to trading and a history write failure must not affect the
    report or the cron exit code."""
    path = path or HISTORY_FILE
    try:
        rec = _slim_snapshot(d)
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        _trim_history(path)
        return True
    except Exception as e:  # pragma: no cover - best-effort side channel
        print(f"[warn] 评级历史落盘失败（不影响评级）：{e}", file=sys.stderr)
        return False


def _trim_history(path: str) -> None:
    """Keep at most HISTORY_MAX_LINES lines (one nightly snapshot each).
    Best-effort; any error leaves the file untouched."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
        if len(lines) <= HISTORY_MAX_LINES:
            return
        with open(path, "w", encoding="utf-8") as fh:
            fh.writelines(lines[-HISTORY_MAX_LINES:])
    except Exception:
        pass


def read_history(path: str | None = None, since_ms: float | None = None,
                 limit: int = 365) -> list[dict]:
    """Read nightly grade snapshots (oldest first), newest `limit` kept and an
    optional `since_ms` lower bound. Best-effort: missing/corrupt file → []."""
    path = path or HISTORY_FILE
    out: list[dict] = []
    if not os.path.exists(path):
        return out
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for ln in fh:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    rec = json.loads(ln)
                except (json.JSONDecodeError, ValueError):
                    continue
                if since_ms is not None and isinstance(rec.get("ts"), (int, float)) \
                        and rec["ts"] < since_ms:
                    continue
                out.append(rec)
    except OSError:
        return []
    if limit and len(out) > limit:
        out = out[-limit:]
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="SHADOW 风控臂夜间评级 / 建议器（INERT 只读）")
    ap.add_argument("--json", action="store_true", help="机读 JSON 输出")
    ap.add_argument("--push", action="store_true", help="推送飞书风险卡片（不抛异常）")
    ap.add_argument("--windows", type=int, nargs="+", default=[24, 72, 168],
                    help="统计窗口（小时），默认 24 72 168")
    args = ap.parse_args()

    d = collect_grades(args.windows)
    if args.push:
        _push_feishu(d)
    # Audit 2026-09-07 (M1): persist the nightly snapshot for the dashboard
    # trend view. Best-effort; only the cron/main path writes history.
    append_history(d)
    if args.json:
        print(json.dumps(d, ensure_ascii=False, indent=2))
    else:
        print(_fmt_report(d))
    # Exit non-zero only on the dangerous DATA_GAP (configured but blind) so a
    # nightly cron can surface it; ratings themselves never block anything.
    return 1 if any(a["verdict"] == DATA_GAP for a in d["arms"]) else 0


if __name__ == "__main__":
    sys.exit(main())
