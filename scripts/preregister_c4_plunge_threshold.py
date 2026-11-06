"""Pre-register C4's plunge threshold without reading forward returns."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math

from hermes_trader.agents.market_regime import recent_4h_plunge
from hermes_trader.models.types import Candle

BAR_MS = 4 * 60 * 60 * 1000


def _quantile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def _candle(ts: int, raw: list) -> Candle:
    return Candle(
        t=ts // 1000,
        o=float(raw[0]),
        h=float(raw[1]),
        l=float(raw[2]),
        c=float(raw[3]),
        v=float(raw[4]),
    )


def calibrate(candle_data: dict, shadow_book: dict) -> dict:
    entries = {}
    for fill in shadow_book.get("accounts", {}).get("taker", {}).get("fills", []):
        if fill.get("type") != "close" or fill.get("entry_regime") != "up":
            continue
        if fill.get("opened_at") and fill.get("position_id"):
            entries.setdefault(fill["position_id"], fill)

    values = []
    coins = set()
    days = set()
    for entry in entries.values():
        opened_at = int(entry["opened_at"])
        series = candle_data.get(f"{entry['coin']}|4h") or {}
        closed = [
            _candle(int(ts), raw)
            for ts, raw in sorted(series.items(), key=lambda item: int(item[0]))
            if int(ts) + BAR_MS <= opened_at
        ]
        plunge = recent_4h_plunge(closed)
        if not plunge["data_ok"]:
            continue
        values.append(float(plunge["plunge_pct"]))
        coins.add(entry["coin"])
        days.add(datetime.fromtimestamp(opened_at / 1000, timezone.utc).date().isoformat())

    if not values:
        raise ValueError("no eligible old-regime=up entries with closed 4h data")
    p90 = _quantile(values, 0.90)
    return {
        "population": "unique shadow-book entries with old entry_regime=up",
        "forward_returns_read": False,
        "metric": "max bearish body/close-to-close drop over last 3 closed 4h bars",
        "quantile": "linear interpolation; selected p90 rounded up to 0.01 percentage point",
        "eligible_entries": len(values),
        "unique_coins": len(coins),
        "unique_entry_days": len(days),
        "p50": _quantile(values, 0.50),
        "p75": _quantile(values, 0.75),
        "p90": p90,
        "p95": _quantile(values, 0.95),
        "selected_x_pct": math.ceil(p90 * 100.0 - 1e-12) / 100.0,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candles", default="/data/.historical-candles.json")
    parser.add_argument("--shadow-book", default="/data/.shadow-book.json")
    args = parser.parse_args()
    with open(args.candles) as handle:
        candle_data = json.load(handle)
    with open(args.shadow_book) as handle:
        shadow_book = json.load(handle)
    print(json.dumps(calibrate(candle_data, shadow_book), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
