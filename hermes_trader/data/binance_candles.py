"""Offline-only Binance public-klines data source for deep-history replay.

WHY: HyperLiquid's candleSnapshot only retains ~5,011 5m bars (~17 days) per
symbol (hard exchange retention). Binance public spot klines serve the same 5m
interval back for months/years, so M1.6 uses it as a SECOND, offline-only data
source to reach the 40-60 independent trading days the promotion gate needs.

SCOPE / GUARDRAILS
  * Read-only public market data (api.binance.com, api/v3/klines). No keys.
  * NEVER imported by the live decision/execution path. Used solely by the
    offline replay scripts; the running bot keeps using HyperLiquid.
  * Symbols not on Binance USDT spot are reported uncovered, never guessed.
  * Binance kline[5] is BASE-asset volume; kline[7] is QUOTE(USDT) notional.
    The runner gate's volume triggers are self-relative (recent vs the series'
    own baseline), so base volume works; daily_mover's absolute USD floor uses
    kline[7]. We carry both: Candle.v = base volume; *_quote_usd via cache map.

Candle open-time grid is identical (5m epoch-aligned), so the same PIT/closed
bar contract as historical_candles applies.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Optional

from hermes_trader.models.types import Candle

INTERVAL_MS = {"5m": 300_000, "15m": 900_000,
               "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}

BASE_HOSTS = [
    os.environ.get("HERMES_BINANCE_HOST", "https://api.binance.com"),
    "https://data-api.binance.vision",  # public market-data mirror
]

# HyperLiquid/Hermes symbol -> Binance base asset. Identity for the large-cap
# basket that trades on both venues. HIP-3 "k"-prefixed tokens are NOT 1:1 with
# Binance symbols (e.g. HL kPEPE = 1000x PEPE and Binance's symbol is PEPEUSDT),
# so they are intentionally left unmapped and reported uncovered rather than
# mix a 1000x price scale into absolute-price/outcome math.
_SYMBOL_OVERRIDES: dict[str, str] = {}

DEFAULT_CACHE_FILE = os.environ.get(
    "HERMES_BINANCE_CANDLE_CACHE", "/data/.binance-candles.json")

_BAR_CACHE: dict[tuple[str, str], dict[int, tuple[float, float]]] = {}
# (coin, interval, t) -> (Candle, quote_usd); store base vol on Candle and
# keep a parallel quote map for the USD daily-mover floor.
_QUOTE: dict[tuple[str, str], dict[int, float]] = {}
_LOCK = threading.RLock()
_DISK_FILE: Optional[str] = None
_LOADED = False
_DIRTY = False


def interval_ms(interval: str) -> int:
    if interval not in INTERVAL_MS:
        raise ValueError(f"unsupported binance interval: {interval!r}")
    return INTERVAL_MS[interval]


def to_binance_symbol(coin: str) -> Optional[str]:
    """Map a Hermes coin to Binance SYMBOLUSDT, or None if not tradeable."""
    if coin in _SYMBOL_OVERRIDES:
        return _SYMBOL_OVERRIDES[coin].upper() + "USDT"
    # HIP-3 / "k" prefixed or colon tokens have no clean Binance 1:1 symbol.
    if ":" in coin or coin.startswith("k"):
        return None
    return coin.upper() + "USDT"


def _http_get_json(url: str, timeout: int = 20,
                     retries: int = 3, backoff_s: float = 1.5) -> list:
    last = None
    for attempt in range(max(1, retries)):
        for host in BASE_HOSTS:
            full = host + url
            try:
                req = urllib.request.Request(
                    full, headers={"User-Agent": "hermes-offline-replay/1.0"})
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return json.loads(r.read())
            except urllib.error.HTTPError as e:
                last = e
                # 429/418 weight ban -> back off before retrying any host
                if e.code in (418, 429):
                    time.sleep(backoff_s * (attempt + 1))
                continue
            except (urllib.error.URLError, TimeoutError,
                    json.JSONDecodeError) as e:
                last = e
                continue
        if attempt + 1 < retries:
            time.sleep(backoff_s * (attempt + 1))
    raise RuntimeError(f"binance klines fetch failed: {last}")


def _request_klines(symbol: str, interval: str,
                    start_ms: int, end_ms: int,
                    limit: int = 1000) -> list[list]:
    iv = interval_ms(interval)
    url = (f"/api/v3/klines?symbol={symbol}&interval={interval}"
           f"&startTime={int(start_ms)}&endTime={int(end_ms) + iv - 1}"
           f"&limit={int(limit)}")
    return _http_get_json(url)


def _parse(rows: list[list]) -> tuple[dict[int, Candle], dict[int, float]]:
    bars: dict[int, Candle] = {}
    quote: dict[int, float] = {}
    for row in rows:
        try:
            t = int(row[0])
            o, h, l, c = (float(row[1]), float(row[2]),
                          float(row[3]), float(row[4]))
            v = float(row[5])
            qv = float(row[7])
        except (TypeError, ValueError, IndexError, KeyError):
            continue
        if not all(abs(x) < float("inf") for x in (o, h, l, c, v, qv)):
            continue
        if h < l or c <= 0:
            continue
        bars[t] = Candle(t=t, o=o, h=h, l=l, c=c, v=v)
        quote[t] = qv
    return bars, quote


def _load_disk() -> None:
    global _LOADED, _DISK_FILE
    with _LOCK:
        if _LOADED:
            return
        _DISK_FILE = DEFAULT_CACHE_FILE
        try:
            if os.path.exists(_DISK_FILE):
                d = json.load(open(_DISK_FILE))
                for key, bucket in (d.get("bars") or {}).items():
                    coin, iv = key.rsplit("|", 1)
                    bk = _BAR_CACHE.setdefault((coin, iv), {})
                    for t, v in bucket.items():
                        o, h, l, c, vol = v
                        bk[int(t)] = Candle(t=int(t), o=o, h=h, l=l, c=c, v=vol)
                for key, bucket in (d.get("quote") or {}).items():
                    coin, iv = key.rsplit("|", 1)
                    qk = _QUOTE.setdefault((coin, iv), {})
                    for t, qv in bucket.items():
                        qk[int(t)] = float(qv)
        except Exception:
            pass
        _LOADED = True


def flush_disk() -> bool:
    with _LOCK:
        if not _DIRTY or not _DISK_FILE:
            return False
        try:
            bars_out, quote_out = {}, {}
            for (coin, iv), bk in _BAR_CACHE.items():
                bars_out[f"{coin}|{iv}"] = {
                    str(t): [b.o, b.h, b.l, b.c, b.v] for t, b in bk.items()}
            for (coin, iv), qk in _QUOTE.items():
                quote_out[f"{coin}|{iv}"] = {str(t): q for t, q in qk.items()}
            tmp = _DISK_FILE + ".tmp"
            json.dump({"bars": bars_out, "quote": quote_out},
                      open(tmp, "w"))
            os.replace(tmp, _DISK_FILE)
            return True
        except Exception:
            return False


# Binance returns up to 1000 rows per call; page forward by startTime.
PAGE_BARS = 1000
MAX_TOTAL_BARS = 60_000


def fetch_candle_range(coin: str, interval: str,
                       start_ms: int, end_ms: int, *,
                       use_cache: bool = True,
                       sleep_s: float = 0.0) -> list[Candle]:
    """Ascending closed-ish klines with open-time in [start,end], paged."""
    global _DIRTY
    iv = interval_ms(interval)
    symbol = to_binance_symbol(coin)
    g0 = start_ms - (start_ms % iv)
    g1 = end_ms - (end_ms % iv)
    if (g1 - g0) // iv + 1 > MAX_TOTAL_BARS:
        raise ValueError("binance range exceeds 60k bars; narrow it")
    if use_cache:
        _load_disk()
    with _LOCK:
        bk = _BAR_CACHE.setdefault((coin, interval), {})
        qk = _QUOTE.setdefault((coin, interval), {})
        cursor = g0
        while cursor <= g1:
            rows = _request_klines(symbol, interval, cursor, g1, PAGE_BARS)
            bars, quote = _parse(rows)
            if not bars:
                break  # symbol not listed / no history here
            added = 0
            for t, cdl in bars.items():
                if t not in bk:
                    added += 1
                bk[t] = cdl
                qk[t] = quote[t]
            if added:
                _DIRTY = True
            newest = max(bars)
            nxt = newest + iv
            if nxt <= cursor or newest >= g1:
                break
            cursor = nxt
            # gentle default pacing to stay under Binance IP weight limits
            time.sleep(sleep_s or 0.05)
        return [bk[t] for t in sorted(bk) if g0 <= t <= g1]


def quote_volume(coin: str, interval: str, t_ms: int) -> Optional[float]:
    """Quote-asset (USDT) notional for the bar opening at t_ms, if cached."""
    with _LOCK:
        return _QUOTE.get((coin, interval), {}).get(int(t_ms))


def reset_cache() -> None:
    with _LOCK:
        _BAR_CACHE.clear()
        _QUOTE.clear()
