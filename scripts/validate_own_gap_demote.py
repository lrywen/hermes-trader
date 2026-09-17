#!/usr/bin/env python3
"""validate_own_gap_demote.py — 坑1 own-gap demote 阶段2反事实验证（影子 JSONL 单源）

回答三个问题（规则阈值 = own_gap_demote_pct, 生产已上线 15%）：

  1. 触发验证 —— 部署后 aligned & gap≥阈值 的行, 真实 gate via 还是 "aligned" 吗?
     （post-deploy 应为 0; >0 说明规则未生效/被旁路 → WARNING, exit 2）
  2. 处置分布 —— 被降级的行, blocked（拦截） vs confidence/composite/trigger:*（过 bar 执行）各多少?
  3. 反事实评级 —— 若当时放行, 这批单子赢面多少? 复用
     reconcile_per_coin_regime_shadow.py 的评级机（真实平仓优先 + 1h DSL 兜底,
     净 -5bps, 与离线审计同口径）, 按 cohort 汇总 WR:
       cohort A = aligned & side-adjusted gap ≥ 阈值   (生产会被降级)
       cohort B = aligned & gap < 阈值                  (对照组, 不受影响)

判定建议（--wr-keep/--wr-drop 可调）:
  WR_A < 35%   → 规则有效, 保留
  WR_A > 45%   → 误拦偏多, 考虑提阈值或回滚（配置 own_gap_demote_pct=0 热重载即回滚）
  中间          → 不确定, 继续累计样本（已评级 < --min-graded 时强制"样本不足"）

用法（容器内）:
  python3 scripts/validate_own_gap_demote.py \
      [--file /data/per_coin_regime_shadow.jsonl] \
      [--since 2026-09-15T09:30:00Z | --since 1758000000000] \
      [--threshold 15.0] [--no-reconcile] [--window-hours 24] [--json]

退出码: 0 正常; 2 发现 post-deploy 仍 free-pass 的异常行。
零三方依赖, 沙箱/容器均可跑。
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_SHADOW_FILE = "/data/per_coin_regime_shadow.jsonl"
RECONCILE_SCRIPT = REPO / "scripts" / "reconcile_per_coin_regime_shadow.py"

# via 处置分类（对齐 risk_gates.market_regime_gate / _counter_trend_decision 语义）
_EXEC_VIAS = ("confidence", "composite")


def _parse_since(s: str | None) -> int:
    """--since 接受 epoch ms（全数字）或 ISO 时间串, 返回 epoch ms; None→0。"""
    if not s:
        return 0
    s = s.strip()
    if s.isdigit():
        return int(s)
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _ts_ms(v) -> int:
    """影子行 timestamp 兼容 epoch ms（数字）与 ISO 串（如 2026-09-15T00:07:16Z）。"""
    if v is None:
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip()
    if s.isdigit():
        return int(s)
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _side_adjusted_gap(row: dict) -> float:
    """影子 detail.own_gap_pct 是原始值（未方向调整）; 生产规则用 side-adjusted。"""
    gap = (row.get("detail") or {}).get("own_gap_pct")
    try:
        gap = float(gap or 0.0)
    except (TypeError, ValueError):
        gap = 0.0
    return -gap if (row.get("side") or "").lower() == "short" else gap


def _load_rows(path: Path, since_ms: int) -> tuple[list[dict], int]:
    rows: list[dict] = []
    total = 0
    if not path.exists():
        return rows, total
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        total += 1
        if since_ms and _ts_ms(row.get("timestamp")) < since_ms:
            continue
        if row.get("rule") != "per_coin_regime":
            continue
        if row.get("macro_aligned") is not True:
            continue  # 反事实只对 aligned 行有意义（非 aligned 本就走 counter 路径）
        rows.append(row)
    return rows, total


def _via_bucket(via) -> str:
    """实盘处置分类: freypass_anomaly / demoted_blocked / demoted_executed / unknown。"""
    v = (via or "").strip().lower()
    if not v:
        return "unknown"
    if v == "aligned":
        return "freypass_anomaly"   # 顺宏观放行 —— post-deploy 对 gap≥阈值行应为 0
    if v.startswith("blocked"):
        return "demoted_blocked"    # 降级后 counter bar 拦截
    if v in _EXEC_VIAS or v.startswith("trigger"):
        return "demoted_executed"   # 降级后过 bar 执行
    return "unknown"


def _cohort_stats(rows: list[dict]) -> dict:
    graded = [r for r in rows if r.get("pnl_pct") is not None]
    wins = [r for r in graded if float(r.get("pnl_pct") or 0) > 0]
    pnls = [float(r["pnl_pct"]) for r in graded]
    return {
        "n": len(rows),
        "graded": len(graded),
        "pending": len(rows) - len(graded),
        "wins": len(wins),
        "losses": len(graded) - len(wins),
        "win_rate": (len(wins) / len(graded)) if graded else None,
        "mean_pnl_pct": statistics.fmean(pnls) if pnls else None,
        "sum_pnl_pct": sum(pnls) if pnls else None,
        "avg_win_pct": statistics.fmean([p for p in pnls if p > 0]) if wins else None,
        "avg_loss_pct": statistics.fmean([p for p in pnls if p <= 0]) if (len(graded) - len(wins)) else None,
    }


def _fmt_pct(x, digits=1):
    return "n/a" if x is None else f"{x * 100:.{digits}f}%"


def _fmt_signed(x, digits=2):
    return "n/a" if x is None else f"{x:+.{digits}f}%"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", default=DEFAULT_SHADOW_FILE, help=f"影子 JSONL（默认 {DEFAULT_SHADOW_FILE}）")
    ap.add_argument("--since", default=None, help="只看该时间之后的行（epoch ms 或 ISO, 建议传部署时间）")
    ap.add_argument("--threshold", type=float, default=15.0, help="own-gap demote 阈值 %%（默认 15, 对齐生产）")
    ap.add_argument("--window-hours", type=float, default=24.0, help="传给 reconcile 的评级窗口（默认 24h）")
    ap.add_argument("--no-reconcile", action="store_true", help="跳过评级步骤（用文件里已有的 pnl_pct）")
    ap.add_argument("--min-graded", type=int, default=10, help="cohort A 最少已评级样本数（默认 10）")
    ap.add_argument("--wr-keep", type=float, default=0.35, help="低于此 WR 判规则有效（默认 0.35）")
    ap.add_argument("--wr-drop", type=float, default=0.45, help="高于此 WR 判误拦偏多（默认 0.45）")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    args = ap.parse_args()

    path = Path(args.file)
    since_ms = _parse_since(args.since)

    # ── 1) 评级（复用 reconcile 评级机, 与离线审计同口径）──
    if not args.no_reconcile:
        if not RECONCILE_SCRIPT.exists():
            print(f"[WARN] 未找到 {RECONCILE_SCRIPT}, 跳过评级", file=sys.stderr)
        else:
            cmd = [sys.executable, str(RECONCILE_SCRIPT), "--file", str(path),
                   "--write", "--window-hours", str(args.window_hours)]
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode != 0:
                print(f"[WARN] reconcile 退出码 {proc.returncode}: {proc.stderr.strip()[:400]}", file=sys.stderr)

    # ── 2) 读行 + cohort 重构 ──
    rows, total = _load_rows(path, since_ms)
    cohort_a = [r for r in rows if _side_adjusted_gap(r) >= args.threshold]
    cohort_b = [r for r in rows if _side_adjusted_gap(r) < args.threshold]

    # ── 3) cohort A 处置分布 + 触发验证 ──
    buckets: dict[str, list[dict]] = {}
    for r in cohort_a:
        buckets.setdefault(_via_bucket((r.get("detail") or {}).get("macro_via")), []).append(r)
    executed_vias = sorted({
        ((r.get("detail") or {}).get("macro_via") or "")
        for r in buckets.get("demoted_executed", [])
    })
    anomalies = buckets.get("freypass_anomaly", [])

    # ADX 确认子集（对应离线审计 tier_would 口径, WR 18.8%@n=15）
    adx_subset = [
        r for r in cohort_a
        if float((r.get("detail") or {}).get("own_adx_4h") or 0.0)
        >= float((r.get("detail") or {}).get("require_own_adx") or 20.0)
    ]

    stats_a = _cohort_stats(cohort_a)
    stats_b = _cohort_stats(cohort_b)
    stats_adx = _cohort_stats(adx_subset)

    # ── 4) 判定 ──
    if stats_a["graded"] < args.min_graded:
        verdict = f"样本不足（cohort A 已评级 {stats_a['graded']} < {args.min_graded}）。继续累计后再判。"
        verdict_code = "insufficient"
    elif (stats_a["win_rate"] or 0) < args.wr_keep:
        verdict = (f"cohort A WR {_fmt_pct(stats_a['win_rate'])} < {_fmt_pct(args.wr_keep, 0)} "
                   f"→ 被降级的单子放行也多为亏损, 规则有效, 保留。")
        verdict_code = "keep"
    elif (stats_a["win_rate"] or 0) > args.wr_drop:
        verdict = (f"cohort A WR {_fmt_pct(stats_a['win_rate'])} > {_fmt_pct(args.wr_drop, 0)} "
                   f"→ 误拦偏多, 考虑提阈值或回滚（配置 own_gap_demote_pct=0 热重载即回滚）。")
        verdict_code = "loosen_or_rollback"
    else:
        verdict = (f"cohort A WR {_fmt_pct(stats_a['win_rate'])} 落在 "
                   f"{_fmt_pct(args.wr_keep, 0)}~{_fmt_pct(args.wr_drop, 0)} 灰区, 不确定, 继续累计样本。")
        verdict_code = "inconclusive"

    report = {
        "file": str(path),
        "since": args.since,
        "threshold_pct": args.threshold,
        "rows_total": total,
        "rows_aligned_in_window": len(rows),
        "cohort_a": stats_a,
        "cohort_b": stats_b,
        "adx_confirmed_subset": {**stats_adx, "require_own_adx": 20.0},
        "disposition_counts": {k: len(v) for k, v in sorted(buckets.items())},
        "executed_via_detail": executed_vias,
        "anomaly_freypass_count": len(anomalies),
        "anomaly_examples": [
            {
                "timestamp": r.get("timestamp"),
                "coin": r.get("coin"),
                "side": r.get("side"),
                "own_gap_pct": (r.get("detail") or {}).get("own_gap_pct"),
                "macro_via": (r.get("detail") or {}).get("macro_via"),
                "trace_id": r.get("trace_id"),
            }
            for r in anomalies[:5]
        ],
        "verdict": verdict,
        "verdict_code": verdict_code,
    }

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        since_disp = args.since or "(文件起点)"
        print(f"════ own-gap demote 阶段2反事实验证 ════")
        print(f"文件: {path}（共 {total} 行, aligned & since {since_disp} 后 {len(rows)} 行）")
        print(f"阈值: side-adjusted gap ≥ {args.threshold}%   评级窗口: {args.window_hours}h")
        print()
        print(f"── cohort A: aligned & gap≥{args.threshold}%（生产会被降级, n={stats_a['n']}）──")
        print(f"已评级 {stats_a['graded']} / pending {stats_a['pending']}   "
              f"WR {_fmt_pct(stats_a['win_rate'])} ({stats_a['wins']}W/{stats_a['losses']}L)   "
              f"mean {_fmt_signed(stats_a['mean_pnl_pct'])}   合计 {_fmt_signed(stats_a['sum_pnl_pct'])}（等权净, -5bps）")
        if stats_a["graded"]:
            print(f"均盈 {_fmt_signed(stats_a['avg_win_pct'])} / 均亏 {_fmt_signed(stats_a['avg_loss_pct'])}")
        disp = report["disposition_counts"]
        print("实盘处置分布: " + (", ".join(f"{k}={v}" for k, v in disp.items()) if disp else "（无）"))
        if executed_vias:
            print(f"  降级后仍执行的 via: {', '.join(executed_vias)}")
        print(f"ADX≥20 确认子集（审计口径）: n={stats_adx['n']} 已评级 {stats_adx['graded']} "
              f"WR {_fmt_pct(stats_adx['win_rate'])}")
        print()
        print(f"── cohort B: aligned & gap<{args.threshold}%（对照组, n={stats_b['n']}）──")
        print(f"已评级 {stats_b['graded']} / pending {stats_b['pending']}   "
              f"WR {_fmt_pct(stats_b['win_rate'])}   mean {_fmt_signed(stats_b['mean_pnl_pct'])}   "
              f"合计 {_fmt_signed(stats_b['sum_pnl_pct'])}")
        print()
        print(f"── 判定 ──")
        print(verdict)
        if anomalies:
            print()
            print(f"⚠ WARNING: {len(anomalies)} 行 gap≥阈值却仍 via=aligned（post-deploy 应为 0）——"
                  f"规则未触发或被旁路, 检查配置 own_gap_demote_pct 与代码版本！")
            for ex in report["anomaly_examples"]:
                print(f"  {ex['timestamp']} {ex['coin']} {ex['side']} "
                      f"gap={ex['own_gap_pct']}% via={ex['macro_via']} trace={ex['trace_id']}")

    return 2 if anomalies else 0


if __name__ == "__main__":
    raise SystemExit(main())
