#!/usr/bin/env python3
"""24-hour gray-mode observer for the dynamic RSI entry gate.

Runs side-by-side evaluation of the CURRENT production gate (strict RSI 75/25)
versus the PROPOSED dynamic RSI gate (ADX-scaled thresholds + multi-timeframe
resonance exception) on live 1h candles, every hour for 24 hours.

For every coin in the production universe, on each hourly tick:
  1. Fetch fresh 1h + 4h candles.
  2. Evaluate entry with both rsi_variant="strict" and rsi_variant="dynamic".
  3. If DYNAMIC would admit a signal that STRICT would block ("newly allowed"),
     open a PAPER position tracked with the production DSL exit model.
  4. Advance every open paper position on each subsequent tick using the DSL.
  5. Append a JSONL record per (tick, event) to OBS_LOG.

This is strictly read-only — it never places real orders, never touches the
production loop, and never writes to the production session log.

Usage:
    python3 scripts/observe_dynamic_rsi_gray.py --hours 24
    python3 scripts/observe_dynamic_rsi_gray.py --hours 24 --coins 20 --once
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_REPO = Path(__file__).resolve().parents[1]
_env = _REPO / ".env.local"
if _env.is_file():
    for _line in _env.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            if _k.strip() == "HYPERLIQUID_PRIVATE_KEY":
                continue
            os.environ.setdefault(_k.strip(), _v.strip())
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "scripts"))

# Reuse the exact same entry evaluator + DSL exit model as the backtest, so the
# gray-mode numbers are directly comparable to A/B/C results.
from backtest_ab_compare import (  # noqa: E402
    DSL,
    _evaluate_entry,
    _resample_4h,
)
from hermes_trader.agents.config import get_config  # noqa: E402
from hermes_trader.client.hl_client import fetch_hl_candles  # noqa: E402
from hermes_trader.client.universe import get_universe  # noqa: E402

OBS_LOG = Path("/tmp/hermes_dynamic_rsi_gray.jsonl")
SUMMARY_LOG = Path("/tmp/hermes_dynamic_rsi_summary.jsonl")

ROUND_TRIP_FEE_BPS = 5.0


@dataclass
class PaperPos:
    coin: str
    side: str
    entry_ts: int
    entry_px: float
    rsi_at_entry: float
    adx_at_entry: float
    ext_atr_at_entry: float
    size_mult: float
    notional: float
    entry_bar: int
    rule_path: str  # "dynamic-threshold" | "resonance-exception"
    dsl: DSL
    exit_ts: int = 0
    exit_px: float = 0.0
    pnl_usd: float = 0.0
    exit_reason: str = ""
    closed: bool = False


def _now_ms() -> int:
    return int(time.time() * 1000)


def _iso(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _write_jsonl(path: Path, record: Dict[str, Any]) -> None:
    with path.open("a") as fh:
        fh.write(json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n")


def _classify_path(
    strict_block: str,
    dyn_block: str,
    size_mult: float,
    rsi: float,
    adx: float,
) -> str:
    """When STRICT blocked but DYNAMIC admitted, which rule path admitted it?"""
    if size_mult < 1.0:
        return "resonance-exception"
    # It was admitted because the ADX-scaled threshold moved above RSI.
    if adx >= 45:
        return "dynamic-threshold-adx45"
    if adx >= 30:
        return "dynamic-threshold-adx30"
    return "dynamic-threshold-base"


def evaluate_universe(
    coins: List[Dict[str, Any]],
    cfg: Dict[str, Any],
    equity: float,
    equity_fraction: float,
    lev_ceiling: int,
    max_loss_pct: float,
    protect_pct: float,
    retrace_threshold: float,
    open_positions: Dict[str, PaperPos],
    tick_idx: int,
) -> Dict[str, Any]:
    """Run one evaluation pass. Returns the tick summary."""
    newly_allowed: List[Dict[str, Any]] = []
    still_blocked: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []

    total_bars = 24 * 21  # 21 days of 1h, matches backtest warmup.

    for m in coins:
        coin = m["coin"]
        max_lev = int(m.get("maxLeverage", 5))
        try:
            candles_1h = fetch_hl_candles(coin, "1h", total_bars)
            if len(candles_1h) < 200:
                continue
            candles_4h = _resample_4h(candles_1h)
            window_1h = candles_1h[:-1]   # evaluate on the last CLOSED bar
            window_4h = [c for c in candles_4h if c.t <= window_1h[-1].t]
            next_bar = candles_1h[-1]     # next/current forming bar (entry proxy)

            s_verdict, s_rsi, s_ext, s_adx, s_block, s_mult = _evaluate_entry(
                window_1h, window_4h, cfg,
                use_new_rules=True, rsi_variant="strict",
            )
            d_verdict, d_rsi, d_ext, d_adx, d_block, d_mult = _evaluate_entry(
                window_1h, window_4h, cfg,
                use_new_rules=True, rsi_variant="dynamic",
            )

            # Case: STRICT blocks but DYNAMIC allows → newly allowed signal.
            if s_verdict is None and d_verdict is not None and coin not in open_positions:
                side = "long" if d_verdict == "LONG" else "short"
                lev = min(lev_ceiling, max_lev)
                notional = equity * equity_fraction * lev * d_mult
                rule_path = _classify_path(s_block, d_block, d_mult, d_rsi, d_adx or 0)
                pos = PaperPos(
                    coin=coin,
                    side=side,
                    entry_ts=next_bar.t,
                    entry_px=next_bar.o,
                    rsi_at_entry=d_rsi,
                    adx_at_entry=d_adx or 0,
                    ext_atr_at_entry=d_ext,
                    size_mult=d_mult,
                    notional=notional,
                    entry_bar=len(candles_1h) - 1,
                    rule_path=rule_path,
                    dsl=DSL(
                        side=side, entry_px=next_bar.o, entry_bar=len(candles_1h) - 1,
                        peak_px=next_bar.o, max_loss_pct=max_loss_pct,
                        protect_pct=protect_pct, retrace_threshold=retrace_threshold,
                    ),
                )
                open_positions[coin] = pos
                rec = {
                    "ts": _now_ms(),
                    "event": "newly_allowed",
                    "tick": tick_idx,
                    "coin": coin,
                    "side": side,
                    "entry_px": next_bar.o,
                    "rsi": round(d_rsi, 1),
                    "adx": round(d_adx or 0, 1),
                    "ext_atr": round(d_ext, 2),
                    "size_mult": d_mult,
                    "notional": round(notional, 2),
                    "strict_reason": s_block,
                    "dynamic_reason": d_block,
                    "rule_path": rule_path,
                }
                newly_allowed.append(rec)
                _write_jsonl(OBS_LOG, rec)

            # Case: STRICT still blocks (and DYNAMIC also blocks) for telemetry.
            elif s_verdict is None and d_verdict is None:
                still_blocked.append({
                    "coin": coin,
                    "rsi": round(s_rsi, 1),
                    "adx": round(s_adx or 0, 1),
                    "strict_reason": s_block,
                    "dynamic_reason": d_block,
                })

        except Exception as exc:  # noqa: BLE001
            errors.append({"coin": coin, "error": str(exc)[:200]})

    return {
        "newly_allowed": newly_allowed,
        "still_blocked_count": len(still_blocked),
        "errors": errors,
    }


def advance_positions(
    coins_candles: Dict[str, List],
    open_positions: Dict[str, PaperPos],
) -> List[PaperPos]:
    """Mark-to-market / exit open paper positions using the latest 1h bar."""
    closed: List[PaperPos] = []
    fee_pct = ROUND_TRIP_FEE_BPS / 10000.0
    for coin, pos in list(open_positions.items()):
        candles = coins_candles.get(coin)
        if not candles:
            continue
        bar = candles[-1]  # current forming/latest bar
        done, exit_px, reason = pos.dsl.check_bar(len(candles) - 1, bar)
        if done:
            gross_pct = (
                (exit_px - pos.entry_px) / pos.entry_px
                if pos.side == "long"
                else (pos.entry_px - exit_px) / pos.entry_px
            )
            pos.exit_px = exit_px
            pos.exit_reason = reason
            pos.exit_ts = bar.t
            pos.pnl_usd = pos.notional * (gross_pct - fee_pct)
            pos.closed = True
            closed.append(pos)
            del open_positions[coin]
            _write_jsonl(OBS_LOG, {
                "ts": _now_ms(),
                "event": "paper_close",
                "coin": coin,
                "side": pos.side,
                "entry_px": pos.entry_px,
                "exit_px": exit_px,
                "pnl_usd": round(pos.pnl_usd, 4),
                "exit_reason": reason,
                "rsi_at_entry": round(pos.rsi_at_entry, 1),
                "adx_at_entry": round(pos.adx_at_entry, 1),
                "size_mult": pos.size_mult,
                "rule_path": pos.rule_path,
                "bars_held": len(candles) - 1 - pos.entry_bar,
            })
    return closed


def write_summary(
    tick_idx: int,
    started_at: float,
    open_positions: Dict[str, PaperPos],
    closed_positions: List[PaperPos],
    tick_info: Dict[str, Any],
) -> None:
    total_new = len(tick_info["newly_allowed"])
    total_closed = len([p for p in closed_positions if p.closed])
    total_pnl = sum(p.pnl_usd for p in closed_positions)
    wins = [p for p in closed_positions if p.pnl_usd > 0]
    losses = [p for p in closed_positions if p.pnl_usd < 0]
    open_pnl = 0.0
    for pos in open_positions.values():
        # unrealised using last close
        pass  # recorded separately via "open" event each tick
    by_path: Dict[str, Dict[str, float]] = {}
    for p in closed_positions:
        b = by_path.setdefault(p.rule_path, {"n": 0, "pnl": 0.0, "wins": 0})
        b["n"] += 1
        b["pnl"] += p.pnl_usd
        if p.pnl_usd > 0:
            b["wins"] += 1

    summary = {
        "ts": _now_ms(),
        "event": "tick_summary",
        "tick": tick_idx,
        "elapsed_hours": round((time.time() - started_at) / 3600, 2),
        "newly_allowed_this_tick": total_new,
        "open_positions": len(open_positions),
        "closed_total": total_closed,
        "closed_pnl_usd": round(total_pnl, 4),
        "closed_win_rate": round(len(wins) / total_closed * 100, 1) if total_closed else 0.0,
        "closed_avg_win": round(sum(p.pnl_usd for p in wins) / len(wins), 4) if wins else 0.0,
        "closed_avg_loss": round(sum(p.pnl_usd for p in losses) / len(losses), 4) if losses else 0.0,
        "by_rule_path": by_path,
        "still_blocked_count": tick_info["still_blocked_count"],
        "errors_this_tick": len(tick_info["errors"]),
    }
    _write_jsonl(SUMMARY_LOG, summary)
    print(f"[{_iso(_now_ms())}] tick={tick_idx:>2} "
          f"new={total_new} open={len(open_positions)} "
          f"closed={total_closed} closed_pnl=${total_pnl:+.2f} "
          f"wr={summary['closed_win_rate']}%")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--coins", type=int, default=20)
    ap.add_argument("--equity", type=float, default=200.0)
    ap.add_argument("--max-loss", type=float, default=2.5)
    ap.add_argument("--interval", type=int, default=3600,
                    help="Seconds between evaluation ticks (default 3600 = 1h).")
    ap.add_argument("--once", action="store_true",
                    help="Run a single evaluation tick and exit (smoke test).")
    args = ap.parse_args()

    from hermes_trader.agents.config_store import read_agent_config, cfg_get
    live = read_agent_config()
    equity_fraction = float(live.get("equity_fraction_per_trade", 0.10))
    lev_ceiling = int(cfg_get("leverage", config=live))
    dsl = live.get("dsl_exit", {}) or {}
    max_loss = float(dsl.get("max_loss_pct", args.max_loss))
    protect = float(cfg_get("dsl_exit.protect_pct", config=dsl))
    retrace = float(cfg_get("dsl_exit.retrace_threshold", config=dsl))

    cfg = get_config()
    universe = get_universe()
    perps = [m for m in universe if m["type"] == "perp" and not m["coin"].startswith("@")]
    coins = sorted(perps, key=lambda m: m.get("dayNtlVlm", 0), reverse=True)[: args.coins]

    # Reset logs for this run.
    OBS_LOG.write_text("")
    SUMMARY_LOG.write_text("")

    open_positions: Dict[str, PaperPos] = {}
    closed_positions: List[PaperPos] = []
    started_at = time.time()

    print(f"=== Dynamic RSI gray observer ===")
    print(f"Duration: {args.hours}h | Coins: {args.coins} | Tick every: {args.interval}s")
    print(f"Equity: ${args.equity:.0f} | Fraction: {equity_fraction:.0%} | "
          f"Lev: {lev_ceiling}x | DSL: {max_loss}%/{protect}%/{retrace}")
    print(f"Logs: {OBS_LOG} | {SUMMARY_LOG}")
    print()

    for tick_idx in range(args.hours if not args.once else 1):
        # Fetch candles once per coin this tick (advance + evaluate).
        coins_candles: Dict[str, List] = {}
        for m in coins:
            try:
                coins_candles[m["coin"]] = fetch_hl_candles(m["coin"], "1h", 24 * 21)
            except Exception:
                pass

        # 1. Mark-to-market / exit existing paper positions.
        closed = advance_positions(coins_candles, open_positions)
        closed_positions.extend(closed)

        # 2. Evaluate fresh entries (strict vs dynamic).
        tick_info = evaluate_universe(
            coins, cfg, args.equity, equity_fraction, lev_ceiling,
            max_loss, protect, retrace, open_positions, tick_idx,
        )

        # 3. Write + print tick summary.
        write_summary(tick_idx, started_at, open_positions, closed_positions, tick_info)

        if args.once:
            break

        # Sleep until next tick, but be responsive to Ctrl-C.
        for _ in range(args.interval):
            time.sleep(1)

    # Final report.
    total_new = sum(
        1 for line in OBS_LOG.read_text().splitlines()
        if '"event":"newly_allowed"' in line
    )
    total_closed = len(closed_positions)
    total_pnl = sum(p.pnl_usd for p in closed_positions)
    open_count = len(open_positions)
    print("\n" + "=" * 72)
    print(f"  GRAY RUN COMPLETE — {args.hours}h")
    print("=" * 72)
    print(f"  Newly allowed signals (DYN vs STRICT) : {total_new}")
    print(f"  Closed paper positions                 : {total_closed}")
    print(f"  Open paper positions at end            : {open_count}")
    print(f"  Realised PnL on closed                 : ${total_pnl:+.2f}")
    if total_closed:
        wins = [p for p in closed_positions if p.pnl_usd > 0]
        print(f"  Win rate                               : "
              f"{len(wins)/total_closed*100:.1f}%")
    print(f"\n  Event log : {OBS_LOG}")
    print(f"  Summary   : {SUMMARY_LOG}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
