#!/usr/bin/env python3
"""辩论影子 A/B 的事后 outcome 评估（只读离线分析，不交易）。

背景：``debate_shadow_ab`` 探针只记录「单模型 vs 多空辩论」的 verdict 分歧，
本身不含后续盈亏，因此无法回答「分歧样本里辩论侧是否事后更优」。本脚本把每条
探针与该币之后发生的真实平仓 outcome 做时间窗口 join，分别评估两侧 verdict
若被执行的假想结果，给出是否可把 ``debate_research.enabled`` 置 True（enforce）
的达标建议。

数据来源（全部只读）：
  - 探针：events.jsonl 的 ``debate_shadow_ab``（含轮转 .1/.2/..）
  - outcome：session log 及其 .gz 归档中的平仓事件。SHADOW 阶段为影子账户
    平仓 ``shadow_exit``（带 realized_pnl_pct / spot_pct）；enforce 后为真实
    平仓 ``dsl_exit`` / ``close_position`` / ``external_close_recorded``。
  字段以扁平行口径读取。

判定口径（每个探针在 horizon 内寻找该币下一笔真实平仓）：
  - directional verdict（LONG/SHORT）：
      * 存在同向且 pnl_pct>0 的平仓 → correct（命中盈利）
      * 存在同向但 pnl_pct<=0 的平仓 → wrong（本会亏损）
      * 无同向平仓 → unscored（该方向未被市场验证）
  - 非 directional verdict（PASS/CLOSE/None）：
      * 之后所有平仓 pnl_pct<=0 → avoided（正确规避）
      * 之后存在 pnl_pct>0 的平仓 → missed（错过盈利）
      * 无任何平仓 → unscored

enforce 达标阈值（可被命令行覆盖）：
  - 分歧样本中辩论侧 correct/avoided 占比 ≥ ADVANTAGE_MIN（默认 0.6）
  - 且可评分分歧样本数 ≥ MIN_SCORED（默认 30）
  - 同时要求辩论侧净优势（正确数−错误数）为正

用法：
  python scripts/debate_ab_outcome.py [--days 60] [--horizon-hours 72]
                                      [--min-scored 30] [--advantage-min 0.6]
                                      [--json]
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hermes_trader import event_log, session_log

_DIRECTIONAL = {"LONG", "SHORT"}
# SHADOW 阶段用影子账户平仓 shadow_exit；enforce 后为真实平仓事件
_CLOSE_EVENTS = {
    "shadow_exit",
    "dsl_exit",
    "close_position",
    "external_close_recorded",
}

# outcome 行里 pnl 百分比的候选键（杠杆口径优先，回退现货口径）
_PNL_PCT_KEYS = ("pnl_pct", "realized_pnl_pct", "spot_pct")
_TS_KEYS = ("ts", "closed_at", "close_ts")


def _parse_iso(ts: str) -> Optional[datetime]:
    return event_log.parse_iso(ts)


def _session_outcomes() -> list[dict[str, Any]]:
    """读取活跃 session log + 轮转 .gz 归档中的真实平仓事件（归一化为扁平行）。"""
    paths = [session_log.SESSION_LOG_FILE] + session_log._list_rotated()
    out: list[dict[str, Any]] = []
    for path in paths:
        try:
            if path.endswith(".gz"):
                fh = gzip.open(path, "rt", encoding="utf-8")
            else:
                fh = open(path, "r", encoding="utf-8")
        except OSError:
            continue
        with fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict) and rec.get("event") in _CLOSE_EVENTS:
                    out.append(rec)
    return out


def _row_ms(row: dict[str, Any]) -> Optional[int]:
    for k in _TS_KEYS:
        v = row.get(k)
        if isinstance(v, (int, float)):
            return int(v)
    return None


def _row_pnl_pct(row: dict[str, Any]) -> Optional[float]:
    for k in _PNL_PCT_KEYS:
        v = row.get(k)
        if isinstance(v, (int, float)):
            return float(v)
    return None


def _classify(verdict: Optional[str], side: Optional[str],
              later_closes: list[dict[str, Any]]) -> str:
    """按之后的平仓结果给单个 verdict 分类：correct/wrong/avoided/missed/unscored。"""
    if verdict in _DIRECTIONAL:
        same = [c for c in later_closes if c.get("side") == side]
        if not same:
            return "unscored"
        pnls = [p for p in (_row_pnl_pct(c) for c in same) if p is not None]
        if not pnls:
            return "unscored"
        return "correct" if any(p > 0 for p in pnls) else "wrong"
    # PASS / CLOSE / None
    if not later_closes:
        return "unscored"
    pnls = [p for p in (_row_pnl_pct(c) for c in later_closes) if p is not None]
    if not pnls:
        return "unscored"
    return "avoided" if all(p <= 0 for p in pnls) else "missed"


def evaluate(days: int, horizon_hours: int) -> dict[str, Any]:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00Z")
    probes = event_log.query_events(event_type="debate_shadow_ab", start=since)
    outcomes = _session_outcomes()

    # outcome 按币 → 毫秒时间排序，便于窗口检索
    by_coin: dict[str, list[dict[str, Any]]] = {}
    for c in outcomes:
        ms = _row_ms(c)
        if ms is None:
            continue
        by_coin.setdefault(str(c.get("coin")), []).append(c)
    for rows in by_coin.values():
        rows.sort(key=_row_ms)

    horizon_ms = horizon_hours * 3600 * 1000
    rows: list[dict[str, Any]] = []
    for rec in probes:
        p = rec.get("payload", {})
        coin = str(p.get("coin"))
        probe_ms = int(rec["_dt"].timestamp() * 1000)
        later = [
            c for c in by_coin.get(coin, [])
            if (_row_ms(c) or 0) >= probe_ms
            and (_row_ms(c) or 0) <= probe_ms + horizon_ms
        ]
        single, debate = p.get("single", {}), p.get("debate", {})
        s_cls = _classify(single.get("verdict"), single.get("side"), later)
        d_cls = _classify(debate.get("verdict"), debate.get("side"), later)
        rows.append({
            "ts": rec.get("timestamp"), "coin": coin,
            "composite": p.get("composite_score"), "agree": p.get("agree"),
            "single_verdict": single.get("verdict"), "single_side": single.get("side"),
            "debate_verdict": debate.get("verdict"), "debate_side": debate.get("side"),
            "single_class": s_cls, "debate_class": d_cls,
            "n_later_closes": len(later),
        })

    good = {"correct", "avoided"}
    bad = {"wrong", "missed"}
    disagreement = [r for r in rows if not r["agree"]]
    scored_dis = [r for r in disagreement
                  if r["debate_class"] != "unscored" or r["single_class"] != "unscored"]

    def _tally(cls_key: str) -> dict[str, int]:
        t: dict[str, int] = {}
        for r in scored_dis:
            t[r[cls_key]] = t.get(r[cls_key], 0) + 1
        return t

    d_tally, s_tally = _tally("debate_class"), _tally("single_class")
    d_good = sum(v for k, v in d_tally.items() if k in good)
    d_bad = sum(v for k, v in d_tally.items() if k in bad)
    s_good = sum(v for k, v in s_tally.items() if k in good)

    n_scored = len(scored_dis)
    advantage = (d_good / n_scored) if n_scored else 0.0
    return {
        "probes_total": len(rows),
        "disagreement_total": len(disagreement),
        "scored_disagreement": n_scored,
        "debate_tally": d_tally,
        "single_tally": s_tally,
        "debate_good": d_good, "debate_bad": d_bad, "single_good": s_good,
        "debate_advantage_rate": round(advantage, 4),
        "debate_net_advantage": d_good - d_bad,
        "rows": rows,
    }


def decide(res: dict[str, Any], min_scored: int, advantage_min: float) -> dict[str, Any]:
    ready = (
        res["scored_disagreement"] >= min_scored
        and res["debate_advantage_rate"] >= advantage_min
        and res["debate_net_advantage"] > 0
    )
    reasons = []
    if res["scored_disagreement"] < min_scored:
        reasons.append(f"可评分分歧样本 {res['scored_disagreement']} < {min_scored}")
    if res["debate_advantage_rate"] < advantage_min:
        reasons.append(f"辩论优势占比 {res['debate_advantage_rate']} < {advantage_min}")
    if res["debate_net_advantage"] <= 0:
        reasons.append("辩论净优势 <= 0")
    return {"enforce_ready": ready, "reasons": reasons}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--horizon-hours", type=int, default=72)
    ap.add_argument("--min-scored", type=int, default=30)
    ap.add_argument("--advantage-min", type=float, default=0.6)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    res = evaluate(args.days, args.horizon_hours)
    verdict = decide(res, args.min_scored, args.advantage_min)

    if args.json:
        print(json.dumps({"summary": {k: v for k, v in res.items() if k != "rows"},
                          "decision": verdict, "rows": res["rows"]},
                         ensure_ascii=False, indent=2))
        return 0

    print(f"探针总数 {res['probes_total']}，分歧 {res['disagreement_total']}，"
          f"可评分分歧 {res['scored_disagreement']}")
    print(f"辩论侧分类 {res['debate_tally']}")
    print(f"单模型分类 {res['single_tally']}")
    print(f"辩论优势占比 {res['debate_advantage_rate']}（阈值 {args.advantage_min}），"
          f"净优势 {res['debate_net_advantage']}")
    if verdict["enforce_ready"]:
        print("结论：达标，可把 debate_research.enabled 置 True（建议仍先小仓位）。")
    else:
        print("结论：暂不可 enforce。")
        for r in verdict["reasons"]:
            print(f"  - {r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
