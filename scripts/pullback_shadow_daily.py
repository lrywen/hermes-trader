#!/usr/bin/env python3
"""Daily pullback-long SHADOW reconciliation + Feishu report.

Runs the same DSL two-phase stop simulation as reconcile_pullback_shadow.py
for all mature shadow signals, then pushes a compact summary card to Feishu
(category=report, routed to the non-trade webhook).

Designed to be called once per day from the container scheduler in
docker-compose.yml.  Safe to run repeatedly — outcomes already written to
the JSONL are not re-simulated (unless --force).

Usage:
    python3 scripts/pullback_shadow_daily.py
    python3 scripts/pullback_shadow_daily.py --window-hours 24 --push
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

os.environ.setdefault("HERMES_BACKTEST", "1")
_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

from hermes_trader.agents.config_store import cfg_get, read_agent_config
from hermes_trader.client.hl_client import fetch_hl_candles
from hermes_trader.models.types import Candle

SHADOW_FILE = os.environ.get(
    "HERMES_PULLBACK_SHADOW_FILE",
    "/data/pullback_shadow.jsonl",
)
ROUND_TRIP_FEE_BPS = 5.0


def _parse_iso(ts: str) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _find_entry_bar(candles: List[Candle], after_ts: datetime) -> int:
    for i, c in enumerate(candles):
        ct = getattr(c, "t", None)
        if ct is None:
            continue
        bar_dt = datetime.fromtimestamp(ct / 1000.0, tz=timezone.utc)
        if bar_dt >= after_ts:
            return i
    return -1


def _simulate_exit(entry_px: float, entry_idx: int,
                   candles: List[Candle], dsl_cfg: Dict[str, Any]
                   ) -> Tuple[float, str, int]:
    max_loss = float(cfg_get("dsl_exit.max_loss_pct", config=dsl_cfg))
    protect = float(cfg_get("dsl_exit.protect_pct", config=dsl_cfg))
    retrace = float(cfg_get("dsl_exit.retrace_threshold", config=dsl_cfg))
    hard_timeout = 180
    peak = entry_px
    for j in range(entry_idx + 1, min(entry_idx + 1 + hard_timeout, len(candles))):
        bar = candles[j]
        if j - entry_idx >= hard_timeout:
            return bar.c, "hard_timeout", j
        stop_px = entry_px * (1 - max_loss / 100)
        if bar.l <= stop_px:
            return min(stop_px, bar.o), f"max_loss {max_loss}%", j
        profit_pct = (peak - entry_px) / entry_px * 100
        if profit_pct >= protect:
            profit_range = peak - entry_px
            floor = entry_px + profit_range * (1 - retrace)
            if bar.l <= floor:
                return min(floor, bar.o), "trailing_stop", j
        if bar.h > peak:
            peak = bar.h
    last = candles[min(entry_idx + hard_timeout, len(candles) - 1)]
    return last.c, "window_end", min(entry_idx + hard_timeout, len(candles) - 1)


def reconcile(records: List[Dict], window_hours: int,
              force: bool = False) -> List[Dict]:
    """Simulate exits for mature signals without outcomes. Returns results."""
    now_ts = datetime.now(timezone.utc).timestamp()
    cutoff = now_ts - window_hours * 3600
    fee_pct = ROUND_TRIP_FEE_BPS / 10000.0

    try:
        live = read_agent_config()
        dsl_cfg = live.get("dsl_exit", {}) or {}
    except Exception:
        dsl_cfg = {}

    results = []
    for r in records:
        if r.get("outcome") and not force:
            continue
        dt = _parse_iso(r.get("timestamp", ""))
        if dt is None or dt.timestamp() > cutoff:
            continue
        coin = r.get("coin", "")
        entry_px = float(r.get("entry_px") or 0)
        if entry_px <= 0:
            r["outcome"] = "no_entry_px"
            continue
        try:
            candles = fetch_hl_candles(coin, "1h", 300)
        except Exception as e:
            print(f"  {coin}: fetch error: {e}", file=sys.stderr)
            continue
        idx = _find_entry_bar(candles, dt)
        if idx < 0 or idx >= len(candles) - 2:
            r["outcome"] = "no_future_bars"
            continue
        exit_px, reason, _ = _simulate_exit(entry_px, idx, candles, dsl_cfg)
        gross_pct = (exit_px - entry_px) / entry_px
        pnl_pct = (gross_pct - fee_pct) * 100
        r["exit_px"] = round(exit_px, 6)
        r["pnl_pct"] = round(pnl_pct, 4)
        r["exit_reason"] = reason
        r["outcome"] = "win" if pnl_pct > 0 else "loss"
        results.append(r)
    return results


def build_report(records: List[Dict], new_results: List[Dict],
                 window_hours: int) -> Tuple[str, Dict[str, Any], str]:
    """Return (title, fields, markdown) for the Feishu card."""
    all_reconciled = [r for r in records if r.get("outcome") in ("win", "loss")]
    pending = [r for r in records if r.get("outcome") is None]

    n = len(all_reconciled)
    wins = [r for r in all_reconciled if r["outcome"] == "win"]
    losses = [r for r in all_reconciled if r["outcome"] == "loss"]
    total_pnl = sum(r.get("pnl_pct", 0) for r in all_reconciled)
    avg_w = (sum(r["pnl_pct"] for r in wins) / len(wins)) if wins else 0.0
    avg_l = (sum(r["pnl_pct"] for r in losses) / len(losses)) if losses else 0.0
    payoff = abs(avg_w / avg_l) if avg_l else float("inf")
    expectancy = total_pnl / n if n else 0.0
    win_rate = len(wins) / n * 100 if n else 0.0

    by_reason: Dict[str, int] = {}
    for r in all_reconciled:
        reason = r.get("exit_reason", "?")
        by_reason[reason] = by_reason.get(reason, 0) + 1

    level = "success" if total_pnl > 0 else ("danger" if n > 0 else "info")
    title = f"回调做多旁路 Shadow 日报 — {datetime.now(timezone.utc).strftime('%Y-%m-%d')}"

    fields: Dict[str, Any] = {
        "已对账信号": str(n),
        "待成熟信号": str(len(pending)),
        "胜率": f"{win_rate:.1f}% ({len(wins)}/{n})" if n else "N/A",
        "累计PnL": f"{total_pnl:+.2f}%" if n else "N/A",
        "平均盈利": f"{avg_w:+.2f}%" if wins else "N/A",
        "平均亏损": f"{avg_l:+.2f}%" if losses else "N/A",
        "盈亏比": f"{payoff:.2f}" if n else "N/A",
        "期望值/笔": f"{expectancy:+.3f}%" if n else "N/A",
        "本次新增对账": str(len(new_results)),
    }

    lines = []
    if by_reason:
        lines.append("**退出原因分布：**")
        for reason, cnt in sorted(by_reason.items(), key=lambda x: -x[1]):
            lines.append(f"- {reason}: {cnt}")
    if all_reconciled:
        lines.append("\n**最近信号明细：**")
        for r in sorted(all_reconciled, key=lambda x: x.get("timestamp", ""))[-10:]:
            pnl = r.get("pnl_pct", 0)
            emoji = "🟢" if pnl > 0 else "🔴"
            lines.append(
                f"{emoji} `{r.get('timestamp','')[:16]}` {r.get('coin',''):8} "
                f"score={r.get('composite_score',0):.0f} "
                f"pnl={pnl:+.2f}% [{r.get('exit_reason','')}]"
            )
    if pending:
        lines.append(f"\n⏳ {len(pending)} 个信号未满 {window_hours}h，等待对账...")
    if not all_reconciled and not pending:
        lines.append("暂无旁路信号记录。48h 灰度窗口进行中...")

    return title, fields, "\n".join(lines), level


def push_feishu(title: str, fields: Dict, markdown: str,
                level: str = "info") -> bool:
    try:
        from hermes_trader import notify
        return notify.send_card(
            title=title,
            fields=fields,
            category="report",
            level=level,
            markdown=markdown,
        )
    except Exception as e:
        print(f"Feishu push failed: {e}", file=sys.stderr)
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file", default=SHADOW_FILE)
    ap.add_argument("--window-hours", type=int, default=24,
                    help="Mature signals older than this many hours")
    ap.add_argument("--push", action="store_true",
                    help="Push summary card to Feishu")
    ap.add_argument("--write", action="store_true", default=True,
                    help="Write outcomes back to JSONL (default on)")
    ap.add_argument("--force", action="store_true",
                    help="Re-simulate signals that already have outcomes")
    args = ap.parse_args()

    records: List[Dict] = []
    if os.path.exists(args.file):
        with open(args.file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass

    print(f"Loaded {len(records)} shadow records from {args.file}")
    new_results = reconcile(records, args.window_hours, force=args.force)
    print(f"Reconciled {len(new_results)} new mature signals")

    title, fields, markdown, level = build_report(
        records, new_results, args.window_hours
    )

    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")
    for k, v in fields.items():
        print(f"  {k:<14s}: {v}")
    if markdown:
        print(f"\n{markdown}")

    if args.write and new_results:
        with open(args.file, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        print(f"\nOutcomes written to {args.file}")

    if args.push:
        ok = push_feishu(title, fields, markdown, level)
        print(f"\nFeishu push: {'OK' if ok else 'FAILED'}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
