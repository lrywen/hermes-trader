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
# M12：零回填诊断门槛。比 MIN_SAMPLES_PROMOTE 低——pullback 实测全周 42 条
# 且 outcome 恒为 0，若门槛挂在 60 上，这类「永远等不到判定」的臂会长期沉默，
# 审计明确点名其 reconcile 链路可能从未覆盖。30 条足够排除「刚上线」噪音。
ZERO_BACKFILL_MIN_SAMPLES = 30
MIN_BLOCK_RATE = 0.03        # 拦截率低于此值 = 几乎从不触发，晋升无意义
# Audit 2026-09-10 (M2/M2′)：拆分为两个语义独立的常量。
#   旧 MAX_FUTILE_BLOCK_RATE=0.5 同时被用于「有害率红线」与「拦太宽」两种完全
#   不同的判定，导致命中率 97% 的 enforce 臂没有任何宽度告警（M2），而命中率
#   80% 的 change 臂宽度信号又与有害率混为一谈。
MAX_HARMFUL_RATE = 0.5       # outcome win 率（=臂有害率）红线：超过即 REVIEW
MAX_HIT_RATE_TOO_WIDE = 0.5  # 命中率（命中/决策）>此值 = 拦/改太宽，必须提示复核
# Audit 2026-09-10 (M3)：PROMOTE 安全余量。旧逻辑有害率在 (0, 0.5] 整段都可
# PROMOTE（45.1% 有害率的 confidence_decay 即被放行）。晋升建议要求有害率不
# 高于 0.4，0.4~0.5 区间维持 COLLECTING 继续观察。
PROMOTE_MAX_HARMFUL_RATE = 0.4
# Audit 2026-09-10 (M4)：低回填率降级线。mature/total 低于此值时，即使有害率
# 越线，REVIEW 结论也只标注为「低置信」；PROMOTE 则直接降 COLLECTING。
MIN_OUTCOME_BACKFILL_RATE = 0.2
# Audit 2026-09-12 (评级口径修正)：纯胜率对「高频小赢、低频大亏」凸性分布会
# 系统性误判——sizing_v2 实测命中集 166 win / 27 loss（win 率 86%），但 win 笔
# 中位反事实仅 +$0.06（手续费/点差级噪声，名义本金中位约 $80、往返 5bps 成本
# 约 $0.04/边），少数大额 loss 使金额合计 −$30.85（v2 净优），旧 OR 分支只看
# 胜率仍把它打成 REVIEW。有金额维度（pnl_usd）的命中集改以「金额合计正负」为
# 主条款，win 率分母只统计 |反事实 pnl| ≥ 此阈值的实质性笔；atr_regime_calib /
# confidence_decay 等只写 pnl_pct 的臂无金额维度，保留原全样本胜率规则。
MIN_MATERIAL_PNL_USD = 0.5
# Audit 2026-09-10 (M13)：shadow/enforce 臂最长窗口 24h 子窗零记录即视为采数
# 停滞（典型：单事件后停采），不改变 verdict 但必须出告警。
STALE_WINDOW_H = 24
# Audit 2026-09-10 (M13 修正)：部分臂是「每 tick 评估、仅在极端/候选事件发生
# 时才往事件 JSONL 落一条」。对这类臂，事件流 24h 零写入是正常的（市场无极端
# 行情），不能据此误报采数停滞——只要它的独立心跳文件仍在新鲜更新，就说明评估
# 在持续运行。心跳年龄（秒）小于此阈值视为健康。
#   market_circuit：trading_loop 每个 scan tick 都重写 /data/.market-circuit.state
#   （clear/no_trip 也写），实测约每 12s 一次；给 30min 宽松阈值容忍重启/抖动。
#   pullback（M17）：仅在 runner gate 走到 pullback-long 旁路块（非结构化做多候选）
#   时重写 /data/.pullback-gate.state，记录最近一次评估的宏观/分数/慢燃等快照。
#   pullback 影子流极稀疏（全部合取成立才落一条），regime 翻 up 过渡期 24h 0 条是
#   常态；心跳新鲜即证明评估路径活着，不是写路径异常。研究节流下候选评估约每分钟级，
#   同样给 30min 阈值。
HEARTBEAT_FRESH_SEC = int(os.environ.get("HERMES_ARM_HEARTBEAT_FRESH_SEC", 1800))
ARM_HEARTBEAT_FILE = {
    "market_circuit": os.environ.get(
        "HERMES_MARKET_CIRCUIT_STATE_FILE", "/data/.market-circuit.state"),
    "pullback": os.environ.get(
        "HERMES_PULLBACK_GATE_STATE_FILE", "/data/.pullback-gate.state"),
    #   regime_overlay：仅在宏观姿态翻转（enter/exit derisk）时落影子事件，
    #   平稳行情可数天 0 条；心跳在每次成功宏观采样（默认 300s 限频）后重写。
    "regime_overlay": os.environ.get(
        "HERMES_REGIME_OVERLAY_STATE_FILE", "/data/.regime-overlay.state"),
}
# Audit 2026-09-10 (ta_late_entry 命中率口径修正)：该臂的 shadow 流混合了两层——
#   layer="prefilter"（TA 预筛）：仅在「拦截成立」时才写一条（放行候选不落盘），
#       是采样偏置的观察流，blocked 天然≈100%，不能作为闸门命中率分母；
#   layer="gate"（下单前 ta_late_entry_gate）：每次真实交易决策都写（拦/放行皆有），
#       这才是「闸门对真实交易拦多宽」的正确分母；8-29 前的旧记录无 layer 字段且
#       带 entry_px、blocked=False，属 gate 语义。
# 对这些臂，命中率(decisions/hits)只统计 gate 层；total/mature/pnl 仍用全量
# （prefilter 的反事实 outcome 是「假如拦掉会怎样」，仍有独立参考价值）。
GATE_LAYER_DECISION_ARMS = {"ta_late_entry"}
# Audit 2026-09-11 (M16)：仅做多、且配置 require_macro_uptrend=true 的信号臂，
# 在宏观 regime 非 "up" 时于结构上不可能产生候选（executor fail-closed 直接
# withhold，连 shadow 记录都不写）。这类臂近 24h 零写入是「宏观非多头期策略性
# 不采数」，不是停采/写路径异常。collect_grades 只读探测当前宏观 regime 传入；
# 非 up 期间把停滞告警降级为中性说明，regime=up 时仍按原逻辑报真停滞。
MACRO_LONG_ONLY_ARMS = {"pullback"}

# 评级档位
PROMOTE = "PROMOTE_CANDIDATE"
COLLECTING = "COLLECTING"
INSUFFICIENT = "INSUFFICIENT_DATA"
DATA_GAP = "DATA_GAP"
REVIEW = "REVIEW"
OFF = "OFF"
# Audit 2026-09-10 (M1)：已在 enforce 的臂没有「晋升」语义，复用 COLLECTING
# 会给出「继续采数、信号健康」的晋升导向文案。新增两档只服务 enforce 臂：
#   MAINTAIN  — 已生产、证据健康，建议维持现状
#   DEGRADED_REVIEW — 已生产但出现宽度/有害性告警，建议复核是否应降级
MAINTAIN = "ENFORCE_MAINTAIN"
DEGRADED_REVIEW = "ENFORCE_DEGRADED_REVIEW"

_VERDICT_CN = {
    PROMOTE: "可考虑升enforce(待人工拍板)",
    COLLECTING: "继续采数",
    INSUFFICIENT: "样本不足",
    DATA_GAP: "采数缺口(该采没采!)",
    REVIEW: "建议复核(疑似无效/有害)",
    OFF: "未启用(mode=off)",
    MAINTAIN: "enforce·维持",
    DEGRADED_REVIEW: "enforce·建议复核降级",
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


def _heartbeat_age_sec(arm: str, now_ms: float | None = None) -> float | None:
    """Return age (seconds) of an arm's independent heartbeat state file, or None
    when the arm has no heartbeat / it is unreadable/missing a ts.

    M13 修正专用：事件型闸门（如 market_circuit）每个评估 tick 都重写心跳，
    但只在真正 trip 时才往事件 JSONL 落一条。用事件流判活会在平稳行情下永久误报
    停滞；心跳年龄才是「评估是否在跑」的可靠信号。只读，绝不写。"""
    hb_path = ARM_HEARTBEAT_FILE.get(arm)
    if not hb_path or not os.path.exists(hb_path):
        return None
    try:
        with open(hb_path, "r", encoding="utf-8", errors="replace") as fh:
            d = json.load(fh)
        ts = float(d.get("ts")) if isinstance(d, dict) else None
    except (OSError, ValueError, TypeError):
        return None
    if ts is None or ts <= 0:
        return None
    now_ms = now_ms if now_ms is not None else time.time() * 1000.0
    # 心跳文件 ts 是 epoch 秒（time.time()），now_ms 是毫秒。
    return max(0.0, now_ms / 1000.0 - ts)


def _current_macro_regime() -> str | None:
    """只读探测当前 BTC 宏观 regime（up/down/neutral/chop），供 M16 判定仅做多臂
    在宏观非多头期的「策略性不采数」。独立 CLI/cron 进程内为进程内缓存未命中，
    会触发一次带 TTL 缓存的 K 线拉取。任何异常都回退 None —— 调用方据此
    fail-open（不抑制停滞告警），绝不因探测失败而掩盖真实停采。绝不写配置/状态。"""
    try:
        from hermes_trader.agents.market_regime import detect_regime
        regime = detect_regime("BTC")
        return str(regime) if regime else None
    except Exception as e:  # pragma: no cover - 网络/环境相关
        print(f"[warn] 宏观 regime 只读探测失败（pullback 停滞按原逻辑判定）：{e}",
              file=sys.stderr)
        return None


def _is_gate_layer_record(rec: dict) -> bool:
    """ta_late_entry：该记录是否属于真实下单闸门（gate）层。

    layer="gate" 显式为 gate；无 layer 字段的 8-29 前旧记录带真实 entry_px 且
    blocked=False，亦为 gate 语义。layer="prefilter" 被排除（只记拦截的观察流）。"""
    layer = rec.get("layer")
    if layer is not None:
        return layer == "gate"
    return True


def _window_stats(records: list[dict], kind: str, window_h: int, now_ms: float,
                  arm: str = "") -> dict:
    """Aggregate hit/outcome stats for records within the trailing window."""
    cutoff = now_ms - window_h * 3600.0 * 1000.0
    total = 0
    hits = 0
    decisions = 0
    outcomes = []          # backfilled counterfactual outcomes: win/loss strings
    pnl_usd = []
    # Audit 2026-09-12 (回填分母修正)：not_material 是 reconcile 对「该记录根本
    # 不会触发本臂动作（无可评估反事实）」的终态标记（如 sizing_v2 名义额变动
    # ≤1%、atr_calib would_change=False），不是待回填缺口。计入回填分母会把
    # atr_regime_calib 实测 24/25 可评估记录稀释成 25/325=7.7% 的假低回填率。
    # 注意 no_coin/no_future_bars 仍是回填链路缺口，保留在分母内。
    not_material = 0
    # ta_late_entry：命中率只统计真实下单闸门层（gate），排除 prefilter 采样偏置。
    gate_only_decisions = arm in GATE_LAYER_DECISION_ARMS
    for rec in records:
        ts = _record_ts_ms(rec)
        if ts is not None and ts < cutoff:
            continue
        total += 1
        if rec.get("outcome") == "not_material":
            not_material += 1
        if (not gate_only_decisions) or _is_gate_layer_record(rec):
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
    mature = len(outcomes)
    return {
        "window_h": window_h, "total": total, "hits": hits, "decisions": decisions,
        "hit_rate": round(hits / decisions, 4) if decisions else 0.0,
        "mature_outcomes": mature,
        "outcome_wins": outcomes.count("win"),
        "outcome_losses": outcomes.count("loss"),
        "pnl_usd_sum": round(sum(pnl_usd), 4) if pnl_usd else 0.0,
        "has_pnl": bool(pnl_usd),
        # ta_late_entry：决策命中率分母只含 gate 层；total 仍为全量（含 prefilter）。
        "decision_scope": "gate_layer" if gate_only_decisions else "all_records",
        # M6/M10：outcome 由 reconcile 按原始记录 ts 事后回填，短窗口成熟数结构
        # 性偏低（不是样本少，是回填滞后+归窗口径）。短窗 mature=0 而长窗有
        # 成熟样本时打标，供面板/报告标注「短窗 outcome 不可用，只采信最长窗」。
        "outcomes_pending": bool(
            window_h < 168 and mature == 0 and total > 0),
        "not_material_outcomes": not_material,
        # 回填分母：剔除终态不可评估记录后的可评估记录数。
        "eligible_total": total - not_material,
    }


def _backfill_rate(s: dict) -> float:
    """mature outcome 回填率 = mature/可评估记录（M4：低回填率臂结论置信弱）。

    Audit 2026-09-12：not_material 终态记录无反事实可评估（本臂不会动作），
    不属于回填缺口，须从分母剔除；否则事件量大但触发稀疏的臂回填率被系统性
    稀释（atr_regime_calib 实测 25/325=7.7% 的假象，实际 24/25≈96%）。"""
    eligible = s.get("eligible_total", s["total"])
    return s["mature_outcomes"] / eligible if eligible else 0.0


def _independent_outcomes(records: list[dict], window_ms: float,
                          now_ms: float) -> int:
    """M4：按 (coin, 自然日UTC) 去重后的成熟 outcome 近似独立场景数。

    实测 atr_regime_calib 25 条 mature 中 18 条为同一币种同日的连续序列
    （同 cf 场景的重复评估），直接按行数算 win 率会高估置信度。这里只做
    保守的「有效样本数」提示，不改变 outcome_wins 的原始计数（判定仍以原始
    mature 为准，避免静默改判）。"""
    cut = now_ms - window_ms
    scenes = set()
    for r in records:
        ts = _record_ts_ms(r)
        if ts is None or ts < cut or r.get("outcome") not in ("win", "loss"):
            continue
        day = int(ts // 86_400_000)
        scenes.add((str(r.get("coin") or "-"), day))
    return len(scenes)


def grade_arm(arm: str, mode: str, path: str, windows: list[int],
              now_ms: float | None = None,
              records: list[dict] | None = None,
              heartbeat_age_sec: float | None = None,
              macro_regime: str | None = None) -> dict:
    """Grade one arm purely from its records + mode. Pure function (no I/O when
    ``records`` is supplied) so it is unit-testable.

    Audit 2026-09-10 additions (warnings never change the INERT posture):
      * M1  enforce 臂独立健康档 MAINTAIN / DEGRADED_REVIEW；
      * M2  命中率 > MAX_HIT_RATE_TOO_WIDE = 拦/改太宽 → REVIEW/降级复核；
      * M3  PROMOTE 要求有害率 ≤ 0.4 安全余量（0.4~0.5 降 COLLECTING）；
      * M4  低回填率标注低置信（附 (币,日) 去重独立场景数）；
      * M8  signal 臂有害率人工通道字段；M12 outcome 零回填诊断；
      * M13 近 24h 零写入的采数停滞告警——但事件型闸门（仅 trip 才落事件流、
            每 tick 写独立心跳）若心跳新鲜，则不算停滞（见 heartbeat_age_sec）。
    """
    now_ms = now_ms if now_ms is not None else time.time() * 1000.0
    kind = ARM_KIND.get(arm, "block")
    if records is None:
        records = _read_jsonl(path)
    stats = [_window_stats(records, kind, w, now_ms, arm) for w in windows]
    w_long = max(windows)
    longest = next((s for s in stats if s["window_h"] == w_long), stats[-1])
    warnings: list[str] = []

    backfill = _backfill_rate(longest)
    # M13：采数停滞（最长窗有记录但近 STALE_WINDOW_H 子窗为 0）。
    # M13 修正：事件型闸门（market_circuit 等）只在真正触发时才写事件流，平稳
    # 行情下 24h 零事件是正常的；若其独立心跳仍在新鲜更新（评估在持续运行），
    # 不判停滞，只给中性说明。心跳缺失/陈旧时才维持停滞告警。
    stale = None
    heartbeat_ok = (heartbeat_age_sec is not None
                    and heartbeat_age_sec >= 0
                    and heartbeat_age_sec <= HEARTBEAT_FRESH_SEC)
    # M16：仅做多且要求宏观多头的臂，regime 非 up 时结构性不触发（executor
    # fail-closed withhold，不写 shadow）。None 表示无法判定（探测失败/未提供），
    # 不退化为抑制，保持原停滞告警以免掩盖真故障。
    macro_long_only = arm in MACRO_LONG_ONLY_ARMS
    macro_blocks = (macro_long_only and macro_regime is not None
                    and macro_regime != "up")
    if mode in ("shadow", "enforce") and longest["total"] > 0:
        short = next((s for s in stats if s["window_h"] == STALE_WINDOW_H), None)
        if short is not None and short["total"] == 0:
            ts_all = [t for t in (_record_ts_ms(r) for r in records) if t]
            newest = max(ts_all) if ts_all else None
            age_h = (now_ms - newest) / 3_600_000 if newest is not None else None
            if macro_blocks:
                # M16：宏观非多头期，仅做多臂策略性不采数，属正常而非故障。
                warnings.append(
                    f"宏观 regime={macro_regime or 'unknown'}（非 up），本臂仅做多且"
                    "要求宏观多头，此期间 executor fail-closed 不产生候选也不写影子"
                    f"记录，近 {STALE_WINDOW_H}h 0 条属策略性不采数，非停采/写路径异常"
                    + (f"；最新记录距今 {age_h:.0f}h" if age_h is not None else ""))
            elif heartbeat_ok:
                # M17：宏观已不阻挡，但事件型信号极稀疏（全部合取成立才落一条），
                # 心跳新鲜说明 gate 仍在持续评估、只是没有合格候选 —— 非写路径异常。
                if macro_long_only and macro_regime is not None:
                    warnings.append(
                        f"宏观 regime={macro_regime}，gate 评估心跳正常"
                        f"（{heartbeat_age_sec:.0f}s 前仍在评估），近 {STALE_WINDOW_H}h "
                        "无合格候选落盘（须同时满足 4h 上行+宏观多头+慢燃≥2+评分≥30+"
                        "非新爆发行情+RSI/伸展过滤），属等待触发，非停采/写路径异常"
                        + (f"；最新记录距今 {age_h:.0f}h" if age_h is not None else ""))
                else:
                    warnings.append(
                        f"事件型闸门心跳正常（{heartbeat_age_sec:.0f}s 前仍在评估），"
                        f"近 {STALE_WINDOW_H}h 无事件落盘属正常（无触发条件），"
                        "非采数停滞")
            else:
                stale = {"stale_hours": round(age_h, 1) if age_h is not None else None,
                         "window_h": STALE_WINDOW_H}
                warnings.append(
                    f"采数停滞：近 {STALE_WINDOW_H}h 0 条"
                    + (f"，最新记录距今 {age_h:.0f}h" if age_h is not None else "")
                    + "，疑似事件驱动型闸门停采或写路径异常")

    # M12：样本够但 outcome 回填恒为 0 → 永远无法进入有效性判定。
    if mode in ("shadow", "enforce") and longest["total"] >= ZERO_BACKFILL_MIN_SAMPLES \
            and longest["mature_outcomes"] == 0:
        eligible = longest.get("eligible_total", longest["total"])
        nm = longest.get("not_material_outcomes", 0)
        if eligible == 0:
            warnings.append(
                f"{longest['total']} 条记录全部为 not_material（本臂不会动作、"
                "无反事实可评估）：无有效性样本，非回填链路故障")
        else:
            warnings.append(
                f"outcome 回填为 0/{eligible}（另有 {nm} 条 not_material 不可评估）："
                "reconcile 链路可能未覆盖本臂，样本再多也无法验证有效性，请排查回填")

    # M8：signal 臂无自动有害率 REVIEW 通道，成熟 outcome 出现时必须把人工
    # 判定所需的有害率显式带出（不改判，仅提示）。
    signal_note = None
    if kind == "signal" and longest["mature_outcomes"] > 0:
        # signal 臂的 win=做多信号反事实为正=信号有效，与 block/change 臂
        # 「win=臂有害」语义相反，故用中性「成熟胜率」表述，不再称有害率。
        swr = longest["outcome_wins"] / longest["mature_outcomes"]
        n_w, n_m = longest["outcome_wins"], longest["mature_outcomes"]
        tail = "初步有效，待更多样本确认" if swr >= 0.5 else "尚未验证有效，继续累积样本"
        signal_note = f"信号成熟胜率 {swr:.0%}（{n_w}/{n_m}），{tail}"
        warnings.append(signal_note)

    # DATA_GAP: configured to collect but the file yields nothing in the longest
    # window. Distinguish "never triggered / path blind" from "no opportunity".
    # M13 扩展：事件型闸门（market_circuit 等）只在真正 trip 时才往事件 JSONL
    # 落一条，平稳行情下最长窗 0 事件是正常的——只要其独立心跳仍在新鲜更新
    # （评估每 tick 在跑），就不是「闸门盲跑」，降级为样本不足而非 DATA_GAP。
    # 心跳缺失/陈旧（评估真的停了/写路径真坏）时仍判 DATA_GAP。
    if (mode in ("shadow", "enforce") and longest["total"] == 0 and heartbeat_ok
            and arm in ARM_HEARTBEAT_FILE):
        verdict = INSUFFICIENT if mode == "shadow" else COLLECTING
        reason = (f"{w_long}h 窗口内 0 条事件，但事件型闸门心跳正常"
                  f"（{heartbeat_age_sec:.0f}s 前仍在评估）：无极端行情触发，"
                  "非采数缺口，继续等待触发累积样本")
        warnings.append(
            f"事件型闸门心跳正常（{heartbeat_age_sec:.0f}s 前仍在评估），"
            f"{w_long}h 0 事件属正常（无触发条件），非闸门盲跑/写路径异常")
    elif mode in ("shadow", "enforce") and longest["total"] == 0:
        verdict = DATA_GAP
        reason = (f"mode={mode} 但 {w_long}h 窗口内 0 条记录"
                  "（未触发或路径写不进，闸门变盲）")
    elif mode == "off":
        # Arm disabled → nothing is expected; report honestly instead of
        # implying data collection is underway (never pushed to Feishu).
        verdict = OFF
        reason = f"mode=off 未启用（{w_long}h {longest['total']} 条残留记录）"
    elif longest["total"] < MIN_SAMPLES_PROMOTE:
        verdict = INSUFFICIENT if mode == "shadow" else COLLECTING
        reason = (f"{w_long}h 仅 {longest['total']} 条 (<{MIN_SAMPLES_PROMOTE})，"
                  f"继续累积样本；命中率 {longest['hit_rate']:.1%}")
    elif mode == "enforce":
        verdict, reason = _enforce_verdict(
            arm, kind, longest, w_long, records, now_ms, backfill, warnings)
    else:
        verdict, reason = _shadow_verdict(
            arm, kind, longest, w_long, records, now_ms, backfill, warnings)

    out = {"arm": arm, "mode": mode, "kind": kind, "path": path,
           "verdict": verdict, "verdict_cn": _VERDICT_CN[verdict],
           "reason": reason, "windows": stats,
           "backfill_rate": round(backfill, 4)}
    # 命中集有害率（分母修正）：面板与 API 可直接展示，不依赖 reason 文案。
    if longest["mature_outcomes"] >= MIN_MATURE_OUTCOMES:
        _w: list[str] = []
        eh = _effective_harm(
            arm, kind, longest, records, w_long, now_ms, _w)
        out["hit_set_mature"] = eh["hit_mature"]
        # 金额维度臂 eff_wr 为实质性笔胜率（可能为 None：无任何实质性笔）；
        # pct-only 臂为全样本胜率。
        out["hit_set_harmful_rate"] = (
            round(eh["eff_wr"], 4) if eh["eff_wr"] is not None else None)
        out["harmful_rate_basis"] = eh["basis"]
        out["harmful_rate_money_basis"] = eh["has_money"]
        if eh["hit_pnl_sum"] is not None:
            out["hit_set_pnl_sum"] = eh["hit_pnl_sum"]
        if eh["material_pnl_sum"] is not None:
            out["hit_set_material_pnl_sum"] = eh["material_pnl_sum"]
            out["hit_set_material_n"] = eh["material_n"]
        if verdict in (REVIEW, DEGRADED_REVIEW, PROMOTE, COLLECTING, MAINTAIN):
            # 合并：预览调用产生的命中集口径告警 + grade_arm 主链路告警。
            for _line in _w:
                if _line not in warnings:
                    warnings.append(_line)
    if warnings:
        out["warnings"] = warnings
    if stale:
        out["collection_stalled"] = stale
    if heartbeat_age_sec is not None:
        out["heartbeat_age_sec"] = round(heartbeat_age_sec, 1)
        out["heartbeat_ok"] = heartbeat_ok
    if macro_long_only and macro_regime is not None:
        out["macro_regime"] = macro_regime
        out["macro_blocks_collection"] = bool(macro_blocks)
    if signal_note:
        out["signal_harmful_rate_note"] = signal_note
    # CS-G §8.1：sizing_v2 成本上限六条件（独立 168h 只读闸门，不改上面的
    # 臂级 verdict；仅供晋升时与 §4 闸门并列人工核对）。
    if arm == "sizing_v2":
        out["sv2_cost"] = grade_sizing_v2_cost(
            records, window_h=SV2_COST_WINDOW_H, now_ms=now_ms)
    return out


def _harmful_rate(s: dict) -> float:
    """outcome win 占比 = 臂有害率（win=反事实为正=该臂拦掉/改掉了本可盈利
    的交易）。block/change/signal 语义一致。"""
    return s["outcome_wins"] / s["mature_outcomes"] if s["mature_outcomes"] else 0.0


def _low_backfill_warning(s: dict, longest_h: int, records: list[dict],
                          now_ms: float, backfill: float) -> str:
    indep = _independent_outcomes(records, longest_h * 3_600_000, now_ms)
    return (f"outcome 回填率仅 {backfill:.1%}"
            f"（<{MIN_OUTCOME_BACKFILL_RATE:.0%}），(币,日) 去重后约 {indep} "
            "个独立场景，有害率结论为低置信")


def _effective_harm(arm: str, kind: str, s: dict, records: list[dict],
                    w_long: int, now_ms: float,
                    warnings: list[str]) -> dict:
    """Audit 2026-09-10 (分母修正)：有害率/反事实 pnl 的正确分母是「命中
    （拦/改发生）的成熟样本」，不是全部记录——未命中的记录本就不受闸门影响，
    计入会系统性稀释有害率（confidence_decay 实测全记录 8.8% vs 命中集
    45.1%，差约 5 倍）。

    Audit 2026-09-12 (口径修正)：纯胜率对「高频小赢、低频大亏」分布系统性
    误判（sizing_v2 实测 win 率 86% 但 win 笔中位仅 +$0.06、金额合计 −$30.85）。
    故：
      * 命中集有金额维度（存在 pnl_usd 字段）——以实质性笔（|pnl| ≥
        MIN_MATERIAL_PNL_USD）金额合计正负为主条款，win 率分母也只含实质性笔；
        被滤掉的小额噪声笔数进 denom_note / 告警。
      * 无金额维度（atr/confidence 等 pct-only 臂）——保留原全样本 win 率规则。
    命中集成熟样本 ≥ MIN_MATURE_OUTCOMES 时用命中集口径；否则回退全记录口径
    并追加「可能低估真实误伤」告警。signal 臂不在有害率自动判定范围内（M8），
    其调用方不使用 harmful_signal。"""
    mature = s["mature_outcomes"]
    cut = now_ms - w_long * 3_600_000

    def _empty_bucket() -> dict:
        return {"n": 0, "wins": 0, "pnl_sum": 0.0, "has_pnl": False,
                "mat_n": 0, "mat_wins": 0, "mat_pnl_sum": 0.0}

    def _tally(bucket: dict, r: dict) -> None:
        bucket["n"] += 1
        if r["outcome"] == "win":
            bucket["wins"] += 1
        p = r.get("pnl_usd")
        if isinstance(p, (int, float)):
            pf = float(p)
            bucket["has_pnl"] = True
            bucket["pnl_sum"] += pf
            if abs(pf) >= MIN_MATERIAL_PNL_USD:
                bucket["mat_n"] += 1
                bucket["mat_pnl_sum"] += pf
                if r["outcome"] == "win":
                    bucket["mat_wins"] += 1

    hit, allr = _empty_bucket(), _empty_bucket()
    for r in records:
        ts = _record_ts_ms(r)
        if ts is None or ts < cut or r.get("outcome") not in ("win", "loss"):
            continue
        _tally(allr, r)
        if _hit_field(r, kind, arm) is True:
            _tally(hit, r)

    if hit["n"] >= MIN_MATURE_OUTCOMES:
        b, basis = hit, "hit_set"
        denom_prefix = "命中集"
    else:
        b, basis = allr, "all_records"
        denom_prefix = "全记录"
    has_money = b["has_pnl"]
    if has_money:
        eff_wr = (b["mat_wins"] / b["mat_n"]) if b["mat_n"] else None
        dust = b["n"] - b["mat_n"]
        denom_note = (
            f"{denom_prefix}实质性 {b['mat_wins']}/{b['mat_n']}"
            f"（|pnl|≥${MIN_MATERIAL_PNL_USD:.2f}；合计 ${b['mat_pnl_sum']:.2f}）")
        if dust:
            denom_note += f"，{dust} 笔小额噪声不计胜率"
        # 金额维度：实质性笔金额合计为主条款；实质性 win 率红线为辅。
        harmful_signal = (
            (b["mat_pnl_sum"] > 0 and b["mat_wins"] > 0)
            or (eff_wr is not None and eff_wr > MAX_HARMFUL_RATE
                and kind in ("block", "change")))
    else:
        eff_wr = b["wins"] / b["n"] if b["n"] else 0.0
        denom_note = f"{denom_prefix} {b['wins']}/{b['n']}"
        harmful_signal = (
            eff_wr > MAX_HARMFUL_RATE and kind in ("block", "change"))

    if hit["n"] < MIN_MATURE_OUTCOMES and mature >= MIN_MATURE_OUTCOMES:
        warnings.append(
            f"命中集成熟样本仅 {hit['n']}（<{MIN_MATURE_OUTCOMES}），"
            "有害率按全记录口径计算，可能低估真实误伤")
    if has_money and hit["n"] >= MIN_MATURE_OUTCOMES and basis == "hit_set":
        dust = hit["n"] - hit["mat_n"]
        if dust >= 10 and dust / hit["n"] >= 0.5:
            warnings.append(
                f"命中集 {dust}/{hit['n']} 笔成熟命中 |反事实 pnl|"
                f"<${MIN_MATERIAL_PNL_USD:.2f}（手续费/点差级噪声），"
                "已从小额过滤后的金额/胜率口径评级，原始笔数胜率不具参考性")
    return {
        "eff_wr": eff_wr, "denom_note": denom_note,
        "harmful_signal": harmful_signal, "hit_mature": hit["n"],
        "basis": basis, "has_money": has_money,
        "hit_pnl_sum": round(hit["pnl_sum"], 4) if hit["has_pnl"] else None,
        "material_n": hit["mat_n"] if basis == "hit_set" else b["mat_n"],
        "material_pnl_sum": (round(b["mat_pnl_sum"], 4)
                             if has_money else None),
        "material_wins": b["mat_wins"] if has_money else None,
    }


def _harm_reason_fragment(eh: dict) -> str:
    """有害已坐实时的原因片段（enforce 降级 / shadow REVIEW 共用）。

    金额维度：主因是实质性笔金额合计为正（v1 净占便宜=采纳臂净亏钱），辅以
    实质性 win 率；pct-only：原始 win 率超红线。"""
    note = eh["denom_note"]
    if eh["has_money"]:
        mat_sum = eh["material_pnl_sum"]
        wr = eh["eff_wr"]
        if mat_sum is not None and mat_sum > 0 and (eh["material_wins"] or 0) > 0:
            lead = (f"实质性反事实合计 ${mat_sum:.2f} 为正（{note}）"
                    "——采纳该臂净亏钱")
        else:
            lead = (f"实质性臂有害率 {wr:.0%}（{note}）"
                    f"超红线 {MAX_HARMFUL_RATE:.0%}")
        return lead
    return (f"臂有害率 {eh['eff_wr']:.0%}（{note}）"
            f"超红线 {MAX_HARMFUL_RATE:.0%}")


def _harm_health_fragment(eh: dict) -> str:
    """未坐实有害时的健康描述片段（MAINTAIN / 宽度复核文案共用）。"""
    if eh["has_money"]:
        wr = eh["eff_wr"]
        mat_sum = eh["material_pnl_sum"]
        sum_txt = f"、实质性合计 ${mat_sum:.2f}" if mat_sum is not None else ""
        if wr is None:
            return f"无实质性笔（{eh['denom_note']}）{sum_txt}"
        return f"实质性臂有害率 {wr:.0%}（{eh['denom_note']}）{sum_txt}"
    return f"臂有害率 {eh['eff_wr']:.0%}（{eh['denom_note']}）"


def _enforce_verdict(arm: str, kind: str, s: dict, w_long: int,
                     records: list[dict], now_ms: float, backfill: float,
                     warnings: list[str]) -> tuple[str, str]:
    """M1：enforce 臂独立健康档。已生产的臂不需要「继续采数/晋升」导向文案，
    只输出「维持」或「建议复核降级」。命中宽度与有害率任一越线即降级复核。"""
    mature = s["mature_outcomes"]
    too_wide = (s["decisions"] > 0 and s["hit_rate"] > MAX_HIT_RATE_TOO_WIDE
                and kind in ("block", "change"))
    low_conf = mature >= MIN_MATURE_OUTCOMES and backfill < MIN_OUTCOME_BACKFILL_RATE
    if low_conf:
        warnings.append(_low_backfill_warning(s, w_long, records, now_ms, backfill))
    eh = _effective_harm(
        arm, kind, s, records, w_long, now_ms, warnings)
    harmful = mature >= MIN_MATURE_OUTCOMES and eh["harmful_signal"]
    # ta_late_entry 命中率只数真实下单闸门（gate）层；total 含仅记拦截的
    # prefilter 观察流，文案需显式区分，避免把 26k 观察记录误读成交易决策。
    scope_note = "（仅下单闸门层；prefilter 观察流不计宽度）" \
        if s.get("decision_scope") == "gate_layer" else ""
    if harmful or too_wide:
        parts = []
        if too_wide:
            parts.append(f"命中率 {s['hit_rate']:.1%}"
                         f"（>{MAX_HIT_RATE_TOO_WIDE:.0%}）=拦/改太宽{scope_note}")
        if harmful:
            parts.append(_harm_reason_fragment(eh))
        if low_conf:
            parts.append("回填不足，结论低置信")
        return DEGRADED_REVIEW, (
            f"已在 enforce 但出现健康告警：{'；'.join(parts)}。"
            "建议复核闸门配置/考虑降级 shadow（仅建议，不自动执行）")
    tail = (f"、{_harm_health_fragment(eh)}未越线" if mature
            else "（反事实 outcome 回填中）")
    return MAINTAIN, (
        f"已在 enforce：{w_long}h {s['total']} 条观察、下单闸门命中 {s['hits']}"
        f"/{s['decisions']}（{s['hit_rate']:.1%}）{scope_note}、回填 {mature} 条{tail}，"
        "运行正常建议维持")


def _shadow_verdict(arm: str, kind: str, s: dict, w_long: int,
                    records: list[dict], now_ms: float, backfill: float,
                    warnings: list[str]) -> tuple[str, str]:
    """shadow 臂判定（total≥60）。M2/M3/M4 修订点见函数内注释。"""
    mature = s["mature_outcomes"]
    too_wide = (s["decisions"] > 0 and s["hit_rate"] > MAX_HIT_RATE_TOO_WIDE
                and kind in ("block", "change"))
    low_conf = mature >= MIN_MATURE_OUTCOMES and backfill < MIN_OUTCOME_BACKFILL_RATE
    if low_conf:
        warnings.append(_low_backfill_warning(s, w_long, records, now_ms, backfill))

    if mature >= MIN_MATURE_OUTCOMES:
        # 分母修正（见 _effective_harm）：以命中集成熟样本为有效有害率。
        eh = _effective_harm(
            arm, kind, s, records, w_long, now_ms, warnings)
        eff_wr, denom_note = eh["eff_wr"], eh["denom_note"]
        harmful = eh["harmful_signal"]
        if harmful:
            conf = "（低置信，回填不足）" if low_conf else ""
            if kind == "change":
                # change 臂：win = v1 优于 v2 = 采纳变更反而少赚（臂有害）。
                return REVIEW, (
                    f"{_harm_reason_fragment(eh)}"
                    f" —— 调整/跳过反而放弃盈利，疑似有害，勿升级{conf}")
            return REVIEW, (
                f"被拦命中样本{_harm_reason_fragment(eh)}"
                f" —— 拦太宽，疑似误伤{conf}")
        # M2：有害率没越线但命中率太宽（几乎每条都动作），同样不能晋升。
        if too_wide:
            return REVIEW, (
                f"{w_long}h 命中率 {s['hit_rate']:.1%}"
                f"（>{MAX_HIT_RATE_TOO_WIDE:.0%}）=拦/改太宽（几乎每条都动作），"
                f"先复核宽度再谈晋升；{_harm_health_fragment(eh)}")
        # 金额维度但无任何实质性笔（全部是手续费级噪声）：无法证伪，继续采数。
        if eh["has_money"] and eff_wr is None:
            return COLLECTING, (
                f"{w_long}h {s['total']} 条、命中率 {s['hit_rate']:.1%}、"
                f"命中集成熟 {eh['hit_mature']} 笔但 |反事实 pnl| 全部 "
                f"<${MIN_MATERIAL_PNL_USD:.2f}（噪声级），无实质性盈亏证据，"
                "维持 shadow 继续采数，暂不建议晋升")
        # M3：健康臂也要求有害率安全余量（≤0.4）才能 PROMOTE。
        # M4：低回填率时即使数字健康也继续采数。
        if kind == "change":
            healthy_txt = (f"反事实臂有益率 {1 - eff_wr:.0%}"
                           f"（{denom_note}）")
        else:
            healthy_txt = f"命中集误伤率 {eff_wr:.0%}（{denom_note}）"
        if eff_wr > PROMOTE_MAX_HARMFUL_RATE:
            return COLLECTING, (
                f"{w_long}h {s['total']} 条、命中率 {s['hit_rate']:.1%}、"
                f"{healthy_txt}；但臂有害率 {eff_wr:.0%} 高于晋升安全余量 "
                f"{PROMOTE_MAX_HARMFUL_RATE:.0%}（40%~50% 灰区），"
                "维持 shadow 继续采数，暂不建议晋升")
        if low_conf:
            return COLLECTING, (
                f"{w_long}h {s['total']} 条、{healthy_txt} 看似健康，"
                f"但回填率仅 {backfill:.1%}（<{MIN_OUTCOME_BACKFILL_RATE:.0%}），"
                "证据覆盖不足，继续采数后再议晋升")
        return PROMOTE, (
            f"{w_long}h {s['total']} 条、命中率 {s['hit_rate']:.1%}、"
            f"{healthy_txt}（臂有害率 {eff_wr:.0%} ≤ "
            f"{PROMOTE_MAX_HARMFUL_RATE:.0%} 安全余量），信号健康，"
            "可考虑升 enforce（待人工拍板）")

    # Enough records but no backfilled outcomes yet.
    rate = s["hit_rate"]
    if s["decisions"] > 0 and rate < MIN_BLOCK_RATE and kind in ("block", "change"):
        return COLLECTING, (
            f"{w_long}h {s['total']} 条但命中率仅 {rate:.2%}"
            f"(<{MIN_BLOCK_RATE:.0%})，闸门几乎不触发，晋升无意义，先继续观察")
    if too_wide:
        return REVIEW, (
            f"{w_long}h {s['total']} 条、命中率 {rate:.1%}"
            f"（>{MAX_HIT_RATE_TOO_WIDE:.0%}）=拦/改太宽，"
            f"且回填 outcome 仅 {mature} 条尚无法证伪，先复核宽度，暂不晋升")
    return PROMOTE, (
        f"{w_long}h {s['total']} 条、命中率 {rate:.1%}；"
        f"尚无回填 outcome（{mature}/{MIN_MATURE_OUTCOMES}），"
        "建议跑 reconcile 后再定")


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
    # M16：只读探测一次当前 BTC 宏观 regime，供仅做多且 require_macro_uptrend
    # 的臂（pullback）判定「宏观非多头期策略性不采数」。独立 CLI 进程内会触发
    # 一次带缓存的 K 线拉取；任何失败都回退 None（不抑制停滞告警，fail-open）。
    macro_regime = _current_macro_regime()
    arms = []
    for label, blk_name, env_file, default_name, mode_key, path_key in sp.ARMS:
        mode = sp._arm_mode(cfg, blk_name, env_file, mode_key)
        path = sp._arm_path(cfg, blk_name, env_file, default_name, path_key)
        # M13 修正：事件型闸门用心跳判活（心跳新鲜则 24h 无事件不判停滞）。
        hb_age = _heartbeat_age_sec(label, now_ms) if label in ARM_HEARTBEAT_FILE else None
        arm_macro = macro_regime if label in MACRO_LONG_ONLY_ARMS else None
        arms.append(grade_arm(label, mode, path, windows, now_ms=now_ms,
                              heartbeat_age_sec=hb_age, macro_regime=arm_macro))

    baseline = {"real_closes": 0, "real_win_rate": None, "note": ""}
    try:
        from hermes_trader.agents.memory import memory
        # Audit 2026-09-10 (M5)：独立 CLI/cron 进程里 memory 单例启动时不会自动
        # hydrate（只有 server/trading_loop 主流程显式调 load()），不显式 load
        # 会让夜间快照的 real_closes 恒为 0（与运行中进程、events.jsonl 真相
        # 不符）。load() 幂等（_initialized 后为 no-op），对服务进程安全。
        memory.load()
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
    order = {DATA_GAP: 0, DEGRADED_REVIEW: 1, REVIEW: 2, PROMOTE: 3,
             INSUFFICIENT: 4, COLLECTING: 5, MAINTAIN: 6, OFF: 7}
    for a in sorted(d["arms"], key=lambda x: order.get(x["verdict"], 9)):
        lines.append(f"{a['arm']:20s} {a['mode']:8s} {a['kind']:6s} "
                     f"{a['verdict_cn']:28s} {a['reason']}")
        # M2/M4/M8/M12/M13：warnings（宽度/低回填/停滞/零回填/signal 人工通道）
        # 紧随该臂主行输出，避免只看 verdict 漏掉侧信号。
        for w in a.get("warnings", []):
            lines.append(f"{'':36s}⚠ {w}")
        # Audit 2026-09-08 (change-arm label fix): outcome win/loss is encoded
        # for all arms as "win = counterfactual pnl>0 => the arm foregoes profit
        # (arm hurts)". For change arms a "win" therefore means arm-HARMFUL;
        # relabel so the night report doesn't read as the arm's own win-rate.
        if a.get("kind") == "change":
            oc_good, oc_bad = "臂有益", "臂有害"
        else:
            oc_good, oc_bad = "胜", "负"
        for s in a["windows"]:
            pend = "  [短窗 outcome 回填滞后，结论只采信最长窗]" if s.get("outcomes_pending") else ""
            lines.append(f"{'':36s}{s['window_h']:>4d}h: {s['total']:>5d} 条  "
                         f"命中 {s['hits']:>4d}/{s['decisions']:<4d} "
                         f"({s['hit_rate']:.1%})  回填 outcome {s['mature_outcomes']} "
                         f"({oc_good}{s['outcome_losses']}/{oc_bad}{s['outcome_wins']}){pend}")
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
    n_deg = sum(1 for a in d["arms"] if a["verdict"] == DEGRADED_REVIEW)
    n_maintain = sum(1 for a in d["arms"] if a["verdict"] == MAINTAIN)
    n_stall = sum(1 for a in d["arms"] if a.get("collection_stalled"))
    lines.append("-" * 100)
    lines.append(f"汇总：采数缺口 {n_gap} / 建议复核 {n_rev + n_deg}"
                 f"（含 enforce 降级复核 {n_deg}）/ 可考虑升级 {n_prom}"
                 f" / enforce 维持 {n_maintain} / 采数停滞 {n_stall}。"
                 "所有 PROMOTE_CANDIDATE 均需人工 reconcile + config_store 权威写后才生效。")
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
    reviews = [a for a in d["arms"] if a["verdict"] in (REVIEW, DEGRADED_REVIEW)]
    promos = [a for a in d["arms"] if a["verdict"] == PROMOTE]
    # M13：采数停滞（近24h零写入）也必须上卡，旧逻辑里这类臂只显示样本不足。
    stalled = [a for a in d["arms"] if a.get("collection_stalled")]
    cost_blocks = [a for a in d["arms"]
                   if a.get("sv2_cost") and a["sv2_cost"]["n"] > 0
                   and not a["sv2_cost"]["all_pass"]]
    if not (gaps or reviews or promos or stalled or cost_blocks):
        return
    fields = {
        "采数缺口(门变盲)": f"{len(gaps)} 条：{', '.join(a['arm'] for a in gaps) or '无'}",
        "建议复核": f"{len(reviews)} 条：{', '.join(a['arm'] for a in reviews) or '无'}",
        "可考虑升级(待人工)": f"{len(promos)} 条：{', '.join(a['arm'] for a in promos) or '无'}",
        "采数停滞": f"{len(stalled)} 条：{', '.join(a['arm'] for a in stalled) or '无'}",
        "真实成交数": str(d["real_baseline"]["real_closes"]),
    }
    md_lines = ["**评级器只读建议，不会自动改任何闸门/配置。**", ""]
    for a in gaps + reviews + promos:
        md_lines.append(f"- `{a['arm']}` ({a['mode']}) → **{a['verdict_cn']}**：{a['reason']}")
        for w in a.get("warnings", []):
            md_lines.append(f"    - ⚠ {w}")
    for a in stalled:
        if a["verdict"] in (DATA_GAP, REVIEW, DEGRADED_REVIEW, PROMOTE):
            continue
        md_lines.append(f"- `{a['arm']}` ({a['mode']}) → **采数停滞**：{a['reason']}")
        for w in a.get("warnings", []):
            md_lines.append(f"    - ⚠ {w}")
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


def _slim_snapshot(d: dict, source: str = "cron") -> dict:
    """Project a full grade report down to one slim, JSONL-friendly history
    line: per-arm verdict/mode + per-window counts the trend chart needs
    (total/hits/decisions/hit_rate/mature_outcomes). Audit 2026-09-08 (CS-C
    window-scoped history): all windows are kept under `windows` so the 24h/72h
    trend survives the nightly snapshot; the flat longest-window fields are
    retained for older readers / pre-CS-C history rows.

    Audit 2026-09-10 (M9/M14):
      * ``source`` = "cron" | "manual" so trend views can exclude hand-run CLI
        snapshots (实测 16 条历史中 13 条是手工 CLI 副产物)；
      * 每臂补 outcome_wins / outcome_losses / backfill_rate / 有害率所需字段，
        否则历史趋势无法回溯有害率，只能看到 verdict 翻转。"""
    w_long = max(d.get("windows_h") or [168])
    _WIN_KEYS = ("window_h", "total", "hits", "decisions", "hit_rate",
                 "mature_outcomes", "outcome_wins", "outcome_losses",
                 "pnl_usd_sum", "outcomes_pending")
    arms = []
    for a in d.get("arms", []):
        longest = next((s for s in a.get("windows", [])
                        if s.get("window_h") == w_long), {})
        windows = [{k: s.get(k, 0) for k in _WIN_KEYS}
                   for s in a.get("windows", [])]
        mature = longest.get("mature_outcomes", 0) or 0
        row = {
            "arm": a.get("arm"),
            "mode": a.get("mode"),
            "kind": a.get("kind"),
            "verdict": a.get("verdict"),
            "total": longest.get("total", 0),
            "hits": longest.get("hits", 0),
            "decisions": longest.get("decisions", 0),
            "hit_rate": longest.get("hit_rate", 0.0),
            "mature_outcomes": mature,
            "outcome_wins": longest.get("outcome_wins", 0),
            "outcome_losses": longest.get("outcome_losses", 0),
            "pnl_usd_sum": longest.get("pnl_usd_sum", 0.0),
            "backfill_rate": a.get("backfill_rate",
                                   (mature / longest["total"]
                                    if longest.get("total") else 0.0)),
            "harmful_rate": round(longest.get("outcome_wins", 0) / mature, 4)
                             if mature else None,
            "warnings": a.get("warnings", []),
            "collection_stalled": a.get("collection_stalled"),
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
        "source": d.get("_history_source", source),
        "real_closes": (d.get("real_baseline") or {}).get("real_closes", 0),
        "real_win_rate": (d.get("real_baseline") or {}).get("real_win_rate"),
        "arms": arms,
    }


def append_history(d: dict, path: str | None = None,
                   source: str = "cron") -> bool:
    """Best-effort: append one slim snapshot line to the nightly grade history.
    Returns True on success. Never raises — the grader must stay read-only
    with respect to trading and a history write failure must not affect the
    report or the cron exit code.

    Audit 2026-09-10 (M9)：``source`` 区分 cron 夜间快照与手工 CLI 运行；
    趋势结论默认只采信 source="cron"。历史无此字段的旧行按 cron 对待。"""
    path = path or HISTORY_FILE
    try:
        rec = _slim_snapshot(d, source=source)
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        # M9 幂等补强：cron 每自然日（UTC）只允许一条快照。cron 重跑/容器重建后
        # 补跑/并发触发若在同一日再写，用新快照「替换」当日旧 cron 行而非追加，
        # 从根上杜绝趋势图同日多条堆叠（09/08 曾出现 9 条、09/10 同刻 2 条）。
        # manual 行不参与替换，保留手工重评的独立痕迹（趋势默认也不读它）。
        if source == "cron":
            rec = _upsert_history_line(path, rec)
        else:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        _trim_history(path)
        return True
    except Exception as e:  # pragma: no cover - best-effort side channel
        print(f"[warn] 评级历史落盘失败（不影响评级）：{e}", file=sys.stderr)
        return False


def _same_day_ms(a: float, b: float) -> bool:
    """两个 epoch-ms 是否落在同一 UTC 自然日。"""
    return (int(a) // 86_400_000) == (int(b) // 86_400_000)


def _upsert_history_line(path: str, rec: dict) -> dict:
    """写入 cron 快照并替换当日已存在的 cron 行（按 UTC 自然日去重）。

    返回最终落盘的 rec（若命中当日已有行，保留旧 ts 以维持时间序，仅更新
    载荷）。文件缺失或损坏时退化为纯追加。"""
    new_line = json.dumps(rec, ensure_ascii=False)
    try:
        existing: list[str] = []
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                existing = fh.readlines()
    except OSError:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(new_line + "\n")
        return rec

    replaced = False
    kept: list[str] = []
    for ln in existing:
        s = ln.strip()
        if not s:
            continue
        try:
            old = json.loads(s)
        except (json.JSONDecodeError, ValueError):
            kept.append(s)
            continue
        is_cron = old.get("source", "cron") == "cron"
        old_ts = old.get("ts")
        if (not replaced and is_cron and isinstance(old_ts, (int, float))
                and _same_day_ms(float(old_ts), float(rec["ts"]))):
            # 命中当日 cron 行：沿用旧 ts（顺序稳定），载荷整体替换为最新评级。
            rec["ts"] = old_ts
            kept.append(json.dumps(rec, ensure_ascii=False))
            replaced = True
        else:
            kept.append(s)
    if not replaced:
        kept.append(new_line)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(kept) + "\n")
    return rec


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
                 limit: int = 365, source: str | None = None) -> list[dict]:
    """Read nightly grade snapshots (oldest first), newest `limit` kept and an
    optional `since_ms` lower bound. Best-effort: missing/corrupt file → [].

    Audit 2026-09-10 (M9)：``source="cron"`` 时只返回夜间 cron 快照（手工 CLI
    快照 source="manual" 被排除）；旧行无 source 字段时按 cron 对待。"""
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
                if source is not None and rec.get("source", "cron") != source:
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
                    help="统计窗口（小时），默认 24 72 168（空格分隔）")
    # Audit 2026-09-10 (M9)：CLI 手工运行默认标记 manual，避免污染夜间趋势；
    # cron 包装脚本（cron_shadow_grade.sh）显式传 --history-source=cron。
    ap.add_argument("--history-source", choices=("cron", "manual"),
                    default=os.environ.get("HERMES_SHADOW_GRADE_SOURCE", "manual"),
                    help="历史快照来源标记（手工 CLI 默认 manual；cron 用 cron）")
    ap.add_argument("--no-history", action="store_true",
                    help="只输出评级，不向 history 追加快照（零污染复核用）")
    args = ap.parse_args()

    d = collect_grades(args.windows)
    if args.push:
        _push_feishu(d)
    # Audit 2026-09-07 (M1): persist the nightly snapshot for the dashboard
    # trend view. Best-effort; only the cron/main path writes history.
    # Audit 2026-09-10 (M9): source-tagged so hand runs can't fake nightly data.
    if not args.no_history:
        append_history(d, source=args.history_source)
    if args.json:
        print(json.dumps(d, ensure_ascii=False, indent=2))
    else:
        print(_fmt_report(d))
    # Exit non-zero only on the dangerous DATA_GAP (configured but blind) so a
    # nightly cron can surface it; ratings themselves never block anything.
    return 1 if any(a["verdict"] == DATA_GAP for a in d["arms"]) else 0


if __name__ == "__main__":
    sys.exit(main())
