#!/usr/bin/env python3
"""BTC macro-regime hourly watcher (read-only, no LLM, no orders).

Computes the SAME classifier the live entry path uses for every crypto perp
(``market_regime.detect_regime`` -> BTC proxy on 1h candles):

  * regime = up   when EMA20 > EMA30 AND EMA20 8-bar slope > +0.2%
  * regime = down when EMA20 < EMA30 AND slope < -0.2%
  * regime = chop when neutral-trend but ADX(14) < 20
  * otherwise neutral

Pushes a compact Feishu card (category=report). Designed to be invoked hourly
from the container scheduler. The regime line and the up/down transition
banner let the operator spot when the pullback-long shadow arm re-arms
(require_macro_uptrend needs regime == "up").

State (last pushed regime) is kept in /data/.macro-regime-watch.json so the
"turned up/down" banner only fires on a real transition; the full card is
still pushed every run for transparency.

Usage:
    python3 scripts/macro_regime_watch.py            # print only
    python3 scripts/macro_regime_watch.py --push     # print + Feishu
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hermes_trader.agents.market_regime import (
    CRYPTO_PROXY,
    closed_candles_only,
    detect_regime_with_score,
)
from hermes_trader.client.hl_client import fetch_hl_candles
from hermes_trader.indicators.math import adx, ema

STATE_FILE = Path(os.environ.get(
    "MACRO_REGIME_WATCH_STATE", "/data/.macro-regime-watch.json"))

# Mirror _classifier_params defaults / live config thresholds.
SLOPE_BARS = 8
SLOPE_UP_PCT = 0.2
ADX_CHOP = 20.0

REGIME_CN = {
    "up": "上涨 up",
    "down": "下跌 down",
    "neutral": "中性 neutral",
    "chop": "震荡 chop",
}


def _metrics(coin: str) -> dict:
    """Return authoritative regime + the raw components behind it."""
    regime, score = detect_regime_with_score(coin, force=True)
    out = {
        "regime": regime,
        "score": score,
        "ema_fast": None,
        "ema_slow": None,
        "slope_pct": None,
        "adx": None,
        "price": None,
        "error": None,
    }
    try:
        raw = fetch_hl_candles(coin, interval="1h", count=100)
        candles, _ = closed_candles_only(raw, "1h")
        if candles:
            closes = [float(c.c) for c in candles]
            fast = ema(closes, 20)
            slow = ema(closes, 30)
            f_now = fast[-1]
            slope = (f_now - fast[-1 - SLOPE_BARS]) / abs(fast[-1 - SLOPE_BARS]) * 100
            adx_arr = adx(candles, 14)
            last_adx = next(
                (v for v in reversed(adx_arr) if v == v and v != float("inf")),
                None,
            )
            out.update({
                "ema_fast": f_now,
                "ema_slow": slow[-1],
                "slope_pct": slope,
                "adx": last_adx,
                "price": closes[-1],
            })
    except Exception as e:  # never crash the watcher on a fetch hiccup
        out["error"] = str(e)
    return out


def _load_prev() -> str:
    try:
        return str(json.loads(STATE_FILE.read_text()).get("regime", ""))
    except Exception:
        return ""


def _save(regime: str) -> None:
    try:
        STATE_FILE.write_text(json.dumps({
            "regime": regime,
            "ts": datetime.now(timezone.utc).isoformat(),
        }))
    except Exception as e:
        print(f"state write failed (non-fatal): {e}", file=sys.stderr)


def _fmt(v, nd=2, suffix="") -> str:
    return "n/a" if v is None else f"{v:.{nd}f}{suffix}"


def build_card(m: dict, prev: str) -> tuple[str, dict, str, str]:
    regime = m["regime"]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    banner = ""
    if prev and prev != regime:
        banner = (f"**宏观切换：{REGIME_CN.get(prev, prev)} → "
                  f"{REGIME_CN.get(regime, regime)}**\n")
    title = f"BTC 宏观行情监控 · {REGIME_CN.get(regime, regime)}"
    fields = {
        "BTC 价格": _fmt(m["price"], 1),
        "EMA20": _fmt(m["ema_fast"], 1),
        "EMA30": _fmt(m["ema_slow"], 1),
        f"EMA20 {SLOPE_BARS}h斜率": _fmt(m["slope_pct"], 3, "%"),
        "ADX(14)": _fmt(m["adx"], 1),
        "强度分": _fmt(m["score"], 3),
    }
    if regime == "up":
        level = "good"
    elif regime == "down":
        level = "warning"
    else:
        level = "info"
    md_lines = [
        banner.rstrip(),
        f"判定：EMA20{'>' if (m['ema_fast'] or 0) > (m['ema_slow'] or 0) else '≤'}EMA30，"
        f"斜率需 > +{SLOPE_UP_PCT}% 判 up / < −{SLOPE_UP_PCT}% 判 down；"
        f"ADX < {ADX_CHOP:.0f} 为震荡。",
    ]
    if regime == "up":
        md_lines.append("宏观 up：pullback-long 回调低吸旁路已满足"
                        " require_macro_uptrend 条件（仍需其余闸门通过）。")
    else:
        md_lines.append(f"当前非 up（{regime}）：pullback-long 旁路不采数，"
                        "系统维持只做高质量 fresh-impulse 做多、禁空。")
    if m.get("error"):
        md_lines.append(f"⚠️ 指标抓取异常（regime 仍取自缓存/分类器）：{m['error']}")
    md_lines.append(f"巡检时间 {now}")
    return title, fields, level, "\n".join(x for x in md_lines if x)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--push", action="store_true", help="push card to Feishu")
    ap.add_argument("--coin", default=CRYPTO_PROXY)
    args = ap.parse_args()

    m = _metrics(args.coin)
    prev = _load_prev()
    title, fields, level, markdown = build_card(m, prev)

    print(title)
    for k, v in fields.items():
        print(f"  {k}: {v}")
    print(markdown)

    if args.push:
        try:
            from hermes_trader import notify
            ok = notify.send_card(
                title=title, fields=fields, category="report",
                level=level, markdown=markdown,
            )
            print("Feishu push:", "OK" if ok else "skipped/failed")
        except Exception as e:
            print(f"Feishu push failed: {e}", file=sys.stderr)

    _save(m["regime"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
