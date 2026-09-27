#!/usr/bin/env python3
"""近1年生成端闸门参数历史审计（K线可复现部分）。

目的：在尽快创造实盘条件前，用近1年历史数据逐一检验各入场闸门阈值是否
合理。只审计"判定为已收盘K线纯函数"的闸门，因为这些可以在长窗口无偏差
回放；依赖 AI verdict / 账户 / whale / news 等外生状态的闸门无法做近1年
回放，脚本在结尾明确列出并说明只能靠影子盘继续累积。

每个候选点 = 某币某根基准周期K线刚收盘；用 PIT 切片计算各闸门的关键
指标，再按固定 horizon 前向评级，统计"闸门触发/不触发"两组的后续收益。
判定阈值合理的标准：触发组的期望收益在方向上应与闸门意图一致（追高类
闸门触发=后续回落，即做多期望为负），且在 train/test 两段符号一致。

数据：Binance 现货 K线（HL 5m 仅保留~17天，近1年必须用此源）。现货 vs
永续存在基差/资金费偏差，结论用于阈值合理性判断而非精确盈亏。

用法：
    python3 scripts/audit_gates_1y.py --days 365 --coins BTC,ETH,SOL --write
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

os.environ.setdefault("HERMES_BACKTEST", "1")
_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

from hermes_trader.indicators.math import atr as _atr_fn
from hermes_trader.indicators.math import ema
from hermes_trader.indicators.math import rsi as _rsi_fn

_BINANCE = "https://data-api.binance.vision/api/v3/klines"
_CACHE_DIR = _REPO / "logs" / "binance_klines_cache"

# 生产实盘基准 —— 直接从 CANONICAL_DEFAULTS 派生，避免硬编码随调参过期
# （此前写死 85/15/8.0，生产已为 88/12/12.0；daily cap 也已结构化为
# {mode, fail_closed}）。canonical 即生产 read_agent_config 的 deep-merge 基底。
def _build_base() -> dict[str, Any]:
    from hermes_trader.agents.config_store import CANONICAL_DEFAULTS as _CD

    _tle = _CD.get("ta_late_entry") or {}
    _lc = _CD.get("late_chase") or {}
    _lc_rt = _lc.get("realtime") or {}
    _dec = _CD.get("daily_extension_cap") or {}
    return {
        "ta_late_entry": {
            "rsi_ob": _tle.get("rsi_ob", 75),
            "rsi_os": _tle.get("rsi_os", 25),
            "ext_ob": _tle.get("ext_ob", 2.5),
            "ext_os": _tle.get("ext_os", -2.5),
            "trend_relax_enabled": _tle.get("trend_relax_enabled", True),
            "adx_trend_threshold": _tle.get("adx_trend_threshold", 35),
            "rsi_ob_relaxed": _tle.get("rsi_ob_relaxed", 82),
            "rsi_os_relaxed": _tle.get("rsi_os_relaxed", 18),
            "ext_ob_relaxed": _tle.get("ext_ob_relaxed", 3.5),
            "ext_os_relaxed": _tle.get("ext_os_relaxed", -3.5),
        },
        "late_chase": {
            "rsi1h_ob": _lc.get("rsi1h_overbought", 88.0),
            "rsi1h_os": _lc.get("rsi1h_oversold", 12.0),
            "rt_rsi_ob": _lc_rt.get("rsi_overbought", 80.0),
            "rt_rsi_os": _lc_rt.get("rsi_oversold", 20.0),
            "rt_ext_atr": _lc_rt.get("max_extension_atr", 3.0),
            "fresh_band_pct": _lc.get("fresh_move_band_pct", 12.0),
        },
        # daily_extension_cap 是灰度臂而非裸数值：mode=off/shadow/enforce。
        # 仅 enforce 时该闸门才真正强制；数值顶来自 override_max_daily_extension_pct。
        "daily_ext_cap": {
            "mode": str(_dec.get("mode", "off")),
            "fail_closed": bool(_dec.get("fail_closed", True)),
            "cap_pct": float(_CD.get("override_max_daily_extension_pct", 30.0)),
        },
    }


BASE = _build_base()

ROUND_TRIP_FEE_BPS = 5.0
HORIZON_BARS = 12          # 5m基准：持有12根=1h（与早前VP/CVD验证同口径）
WARMUP_BARS = 120          # 指标预热
TEST_FRACTION = 0.25       # 最近25%时间作为test段
SAMPLE_EVERY = 12          # 每12根5m抽样1点（每小时1点），大幅降算


# ---------------------------------------------------------------------------
# Binance 现货 K线（分页 + 磁盘缓存）
# ---------------------------------------------------------------------------

def fetch_binance(coin: str, interval: str, start_ms: int, end_ms: int,
                  step_ms: int) -> list[list[float]]:
    symbol = f"{coin}USDT"
    cache = _CACHE_DIR / f"{symbol}_{interval}.json"
    data: dict[int, list] = {}
    if cache.is_file():
        try:
            for row in json.loads(cache.read_text()):
                data[int(row[0])] = row
        except Exception:
            data = {}
    grid0 = start_ms - start_ms % step_ms
    if not all(t in data for t in range(grid0, end_ms + 1, step_ms)):
        cur = grid0
        now_ms = int(time.time() * 1000)
        while cur <= end_ms:
            url = (f"{_BINANCE}?symbol={symbol}&interval={interval}"
                   f"&startTime={cur}&endTime={end_ms + step_ms - 1}&limit=1000")
            payload = None
            for _attempt in range(4):
                try:
                    with urllib.request.urlopen(url, timeout=30) as r:
                        payload = json.loads(r.read())
                    break
                except Exception:
                    if _attempt == 3:
                        raise
                    time.sleep(1.5 * (_attempt + 1))
            if not payload:
                break
            for k in payload:
                if int(k[6]) <= now_ms:
                    data[int(k[0])] = [int(k[0]), float(k[1]), float(k[2]),
                                       float(k[3]), float(k[4]), float(k[5])]
            nxt = int(payload[-1][0]) + step_ms
            if nxt <= cur:
                break
            cur = nxt
            time.sleep(0.04)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps([data[t] for t in sorted(data)]))
    return [data[t] for t in sorted(data) if start_ms <= t <= end_ms]


# ---------------------------------------------------------------------------
# 指标（输入为 [t,o,h,l,c,v] 列表）
# ---------------------------------------------------------------------------

class _B:
    __slots__ = ("t", "o", "h", "l", "c", "v")

    def __init__(self, row: list[float]):
        self.t, self.o, self.h, self.l, self.c, self.v = row

    def __getitem__(self, k: str) -> float:
        return getattr(self, k)


def _rsi(window: list[_B], period: int = 14) -> Optional[float]:
    if len(window) <= period:
        return None
    out = _rsi_fn(window, period)
    return float(out[-1]) if out else None


def _atr(window: list[_B], period: int = 14) -> Optional[float]:
    if len(window) <= period:
        return None
    out = _atr_fn(window, period)
    return float(out[-1]) if out else None


def _adx(window: list[_B], period: int = 14) -> Optional[float]:
    from hermes_trader.indicators.math import adx
    if len(window) <= period:
        return None
    out = adx(window, period)
    return float(out[-1]) if out else None


def _extension_atr(window: list[_B]) -> Optional[float]:
    """价格相对 EMA21 的偏离，单位 ATR14。正=高于EMA。"""
    if len(window) < 35:
        return None
    atr = _atr(window)
    ema21 = ema([b.c for b in window], 21)
    if not atr or atr <= 0 or not ema21:
        return None
    return (window[-1].c - float(ema21[-1])) / atr


def _chg24(series_1h: list[_B], t_close_ms: int, step1h: int) -> Optional[float]:
    cutoff = t_close_ms - step1h
    closes = {b.t: b.c for b in series_1h if b.t <= cutoff}
    c_now = closes.get(cutoff)
    c_prev = closes.get(cutoff - 24 * step1h)
    if c_now is None or c_prev is None or c_prev <= 0:
        return None
    return (c_now - c_prev) / c_prev * 100.0


# ---------------------------------------------------------------------------
# 闸门触发判定（返回触发原因集合）
# ---------------------------------------------------------------------------

def _trend_dir(window: list[_B]) -> str:
    if len(window) < 35:
        return "flat"
    e8 = ema([b.c for b in window], 8)
    e21 = ema([b.c for b in window], 21)
    if not e8 or not e21:
        return "flat"
    if e8[-1] > e21[-1]:
        return "bullish"
    if e8[-1] < e21[-1]:
        return "bearish"
    return "flat"


def evaluate_gates(window5m: list[_B], window1h: list[_B],
                   chg24: Optional[float]) -> dict[str, Any]:
    """在一个 5m 收盘点，计算各闸门是否会拦截做多/做空。"""
    rsi5m = _rsi(window5m)
    rsi1h = _rsi(window1h)
    ext5m = _extension_atr(window5m)
    adx5m = _adx(window5m)
    tdir = _trend_dir(window5m)

    p_te = BASE["ta_late_entry"]
    p_lc = BASE["late_chase"]

    # ta_late_entry（4h口径在生产；这里用5m+1h近似同结构阈值）
    relaxed = False
    if p_te["trend_relaxed"] if False else p_te["trend_relax_enabled"]:
        relaxed = bool(adx5m is not None and adx5m >= p_te["adx_trend_threshold"]
                       and tdir == "bullish")

    def _te_block_long() -> bool:
        rsi_lim = p_te["rsi_ob_relaxed"] if relaxed else p_te["rsi_ob"]
        ext_lim = p_te["ext_ob_relaxed"] if relaxed else p_te["ext_ob"]
        return bool((rsi5m is not None and rsi5m > rsi_lim)
                    or (ext5m is not None and ext5m > ext_lim))

    # late_chase Leg2: 1h RSI blowoff
    def _lc_rsi1h_long() -> bool:
        return bool(rsi1h is not None and rsi1h > p_lc["rsi1h_ob"])

    # late_chase Leg3: 5m realtime RSI / extension
    def _lc_rt_long() -> bool:
        return bool((rsi5m is not None and rsi5m > p_lc["rt_rsi_ob"])
                    or (ext5m is not None and ext5m > p_lc["rt_ext_atr"]))

    # daily_extension_cap：仅 enforce 时强制（off/shadow 不拦）。
    _dec_cfg = BASE["daily_ext_cap"]

    def _daily_cap_long() -> bool:
        if _dec_cfg["mode"] != "enforce":
            return False
        return bool(chg24 is not None and chg24 > _dec_cfg["cap_pct"])

    return {
        "rsi5m": rsi5m, "rsi1h": rsi1h, "ext5m": ext5m,
        "adx5m": adx5m, "chg24": chg24, "relaxed": relaxed,
        "ta_late_entry": _te_block_long(),
        "late_chase_rsi1h": _lc_rsi1h_long(),
        "late_chase_rt": _lc_rt_long(),
        "daily_ext_cap": _daily_cap_long(),
    }


# ---------------------------------------------------------------------------
# 前向评级
# ---------------------------------------------------------------------------

def forward(bars: list[_B], i: int, side: int,
            horizon: int) -> Optional[float]:
    exit_i = i + horizon
    if exit_i >= len(bars):
        return None
    entry = bars[i].c
    if entry <= 0:
        return None
    fee = ROUND_TRIP_FEE_BPS / 10000.0
    gross = side * (bars[exit_i].c - entry) / entry
    return gross - fee


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------

def _stats(vals: list[float]) -> dict[str, Any]:
    n = len(vals)
    if not n:
        return {"n": 0}
    wins = sum(1 for v in vals if v > 0)
    return {
        "n": n,
        "win_rate": round(wins / n, 4),
        "ev_pct": round(sum(vals) / n * 100, 4),
        "median_pct": round(sorted(vals)[n // 2] * 100, 4),
    }


def gate_table(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    fired = [r["ret"] for r in rows if r["gates"][key]]
    clear = [r["ret"] for r in rows if not r["gates"][key]]
    return {"fired": _stats(fired), "not_fired": _stats(clear)}


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

GATE_KEYS = ["ta_late_entry", "late_chase_rsi1h", "late_chase_rt",
             "daily_ext_cap"]


def run(*, days: int, coins: list[str], horizon: int,
        write: bool, out: str) -> dict[str, Any]:
    as_of = int(time.time() * 1000)
    step5m, step1h = 5 * 60_000, 60 * 60_000
    start5m = as_of - (days * 86_400_000) - WARMUP_BARS * step5m
    start1h = as_of - (days * 86_400_000) - 200 * step1h

    per_coin: dict[str, dict[str, Any]] = {}
    all_rows: list[dict[str, Any]] = []

    for ci, coin in enumerate(coins):
        t0 = time.time()
        raw5m = fetch_binance(coin, "5m", start5m, as_of, step5m)
        raw1h = fetch_binance(coin, "1h", start1h, as_of, step1h)
        bars5m = [_B(r) for r in raw5m]
        bars1h = [_B(r) for r in raw1h]
        if len(bars5m) < WARMUP_BARS + horizon + 10:
            print(f"[{ci+1}/{len(coins)}] {coin}: 5m不足，跳过")
            continue

        win_start = as_of - days * 86_400_000
        coin_rows: list[dict[str, Any]] = []
        # 抽样点：从 WARMUP_BARS 起每 SAMPLE_EVERY 根取1点
        first_i = WARMUP_BARS + ((-WARMUP_BARS) % SAMPLE_EVERY)
        for i in range(first_i, len(bars5m) - horizon, SAMPLE_EVERY):
            if bars5m[i].t < win_start:
                continue
            w5m = bars5m[max(0, i - 99):i + 1]
            # 1h 已收盘前缀
            cutoff = bars5m[i].t
            j1h = sum(1 for b in bars1h if b.t <= cutoff - step1h)
            w1h = bars1h[max(0, j1h - 99):j1h]
            chg = _chg24(bars1h[:j1h], bars5m[i].t + step1h, step1h)
            gates = evaluate_gates(w5m, w1h, chg)
            ret = forward(bars5m, i, 1, horizon)   # 做多方向收益
            if ret is None:
                continue
            coin_rows.append({"t": bars5m[i].t, "gates": gates, "ret": ret})

        per_coin[coin] = {"n_points": len(coin_rows)}
        all_rows.extend(coin_rows)
        print(f"[{ci+1}/{len(coins)}] {coin}: {len(coin_rows)}点 "
              f"({time.time()-t0:.0f}s)")

    if not all_rows:
        raise SystemExit("无有效数据点")

    # 时间切分 train/test
    tmin = min(r["t"] for r in all_rows)
    tmax = max(r["t"] for r in all_rows)
    test_start = tmax - (tmax - tmin) * TEST_FRACTION
    train = [r for r in all_rows if r["t"] < test_start]
    test = [r for r in all_rows if r["t"] >= test_start]

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "days": days, "coins": coins, "horizon_5m_bars": horizon,
        "data_source": "Binance spot (HL perp basis/funding deviation)",
        "baseline_params": BASE,
        "n_points": len(all_rows),
        "window": {
            "start": datetime.fromtimestamp(tmin/1000, timezone.utc).date().isoformat(),
            "end": datetime.fromtimestamp(tmax/1000, timezone.utc).date().isoformat(),
        },
        "gates": {
            "all": {k: gate_table(all_rows, k) for k in GATE_KEYS},
            "train": {k: gate_table(train, k) for k in GATE_KEYS},
            "test": {k: gate_table(test, k) for k in GATE_KEYS},
        },
    }

    if write:
        tmp = out + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        os.replace(tmp, out)
    report["_written"] = write
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--coins", default="BTC,ETH,SOL")
    ap.add_argument("--horizon", type=int, default=HORIZON_BARS)
    ap.add_argument("--out", default="/tmp/gate_audit_1y.json")
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()
    coins = [c.strip() for c in args.coins.split(",") if c.strip()]
    rep = run(days=args.days, coins=coins, horizon=args.horizon,
              write=args.write, out=args.out)

    print(f"\n=== 闸门审计 {args.days}d {coins} 共{rep['n_points']}点 ===")
    for seg in ("train", "test"):
        print(f"\n-- {seg} --")
        for g, t in rep["gates"][seg].items():
            f, nf = t["fired"], t["not_fired"]
            print(f"  {g:<18} 触发 n={f.get('n',0):>5} ev={f.get('ev_pct')}%"
                  f" wr={f.get('win_rate')} | 未触发 ev={nf.get('ev_pct')}%"
                  f" wr={nf.get('win_rate')}")
    print(f"\n报告{'已写入 ' + args.out if args.write else '（dry-run；--write保存）'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
