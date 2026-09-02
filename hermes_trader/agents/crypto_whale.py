"""FREE crypto whale-flow signal — our own build of the Unusual-Whales / Whale
Alert "large trade" workflow, with NO paid feed and NO API key.

Truly keyless on-chain wallet tracking needs a paid/keyed explorer, so the
genuinely-free real-time whale read is ORDER-FLOW whales: Binance's public
aggTrades endpoint (no auth) streams every executed trade with side, so we can
isolate the LARGE aggressive prints and net their pressure.

    https://api.binance.com/api/v3/aggTrades?symbol=BTCUSDT&limit=1000

Each aggTrade has price, qty, timestamp, and `m` = isBuyerMaker:
  m=True  -> buyer was the maker -> the taker SOLD  -> aggressive SELL
  m=False -> buyer was the taker -> aggressive BUY

What it produces, for any crypto coin (skips xyz: equity perps):
  - whale buy/sell $ volume = sum of aggressive prints >= a USD threshold,
  - net flow + a bias: heavy net aggressive BUYING by size = bullish whale
    pressure (and vice versa) — the same read as "big market buyer stepping in".

PURE compute (testable) + thin cached fetch. Nothing here trades; it's the signal
product. Wiring into perception/override is a separate, gated step.
"""

from __future__ import annotations

import json
import logging
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional

from hermes_trader.agents.config_store import cfg_get

logger = logging.getLogger(__name__)

try:
    import certifi
    _SSL = ssl.create_default_context(cafile=certifi.where())
except Exception:                     # pragma: no cover
    # P1-15: never fall back to an unverified context (MITM risk). Use the
    # system trust store with full verification; certifi is a pinned dep.
    _SSL = ssl.create_default_context()

_AGG = "https://api.binance.com/api/v3/aggTrades"

# HL coin -> Binance spot symbol. Default = {COIN}USDT; the k-prefixed HL meme
# tickers (kPEPE, kSHIB, kBONK …) are 1000x-scaled on HL but plain on Binance.
_SYMBOL_OVERRIDE = {
    "kPEPE": "PEPEUSDT", "kSHIB": "SHIBUSDT", "kBONK": "BONKUSDT",
    "kFLOKI": "FLOKIUSDT", "kLUNC": "LUNCUSDT", "kDOGS": "DOGSUSDT",
}


def binance_symbol(coin: str) -> Optional[str]:
    """HL crypto coin -> Binance USDT symbol. Returns None for xyz: equities."""
    if ":" in (coin or ""):
        return None
    return _SYMBOL_OVERRIDE.get(coin, f"{coin.upper()}USDT")


@dataclass(frozen=True)
class Print:
    price: float
    qty: float
    ts: int          # ms
    is_buy: bool     # aggressive taker BUY

    @property
    def usd(self) -> float:
        return self.price * self.qty


@dataclass(frozen=True)
class WhaleReport:
    symbol: str
    window_n: int             # total aggTrades scanned
    whale_n: int              # prints >= threshold
    buy_usd: float            # aggressive whale buying
    sell_usd: float           # aggressive whale selling
    net_usd: float            # buy - sell
    bias: str                 # "whale_buying" | "whale_selling" | "balanced"
    min_usd: float
    window_minutes: float = 0.0
    note: str = ""


def parse_aggtrades(payload: list) -> list[Print]:
    out: list[Print] = []
    for t in payload or []:
        try:
            out.append(Print(
                price=float(t["p"]), qty=float(t["q"]), ts=int(t["T"]),
                is_buy=not bool(t["m"]),       # m=True => buyer is maker => taker SOLD
            ))
        except (KeyError, ValueError, TypeError):
            continue
    return out


# ── Config wiring (R13-B7) ───────────────────────────────────────────────────
# Literal fallback (== pre-R13-B7 behavior); the CANONICAL `crypto_whale` block
# carries the same values and is the single source of truth at runtime.
# _CACHE_TTL_S stays as a module symbol (external reference + TTL fallback).
# The legacy HERMES_WHALE_HTTP_TIMEOUT_S / HERMES_CRYPTO_WHALE_CACHE_MAX env
# reads were removed (no deployment referenced them); their defaults live on
# as canonical leaves and remain env-overridable via HERMES_CFG_CRYPTO_WHALE__*.
_CRYPTO_WHALE_DEFAULTS: dict[str, Any] = {
    "ttl_sec": 120.0,
    "http_timeout_s": 2.5,
    "cache_max": 1024,
    "window_minutes": 15.0,
    "min_usd": 100_000.0,
    "bias_threshold": 0.20,
    "max_pages": 6,
}


def crypto_whale_params(*, config: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Resolve the Binance aggTrades whale-flow knobs (live `crypto_whale`
    canonical block, env ``HERMES_CFG_CRYPTO_WHALE__*`` overrides included) with
    the module literals as fallback. Per-leaf lookups keep each key
    independently env-overridable; TTL/timeout/threshold/window/min_usd must be
    non-negative and the bias threshold in (0, 1), cache_max/max_pages positive
    ints. Any read/coerce failure returns a fresh copy of the literals (signal
    path must never break on a bad config)."""
    p = dict(_CRYPTO_WHALE_DEFAULTS)
    try:
        for key in ("ttl_sec", "http_timeout_s", "window_minutes", "min_usd"):
            v = cfg_get(f"crypto_whale.{key}", config=config)
            if v is not None:
                fv = float(v)
                p[key] = fv if fv >= 0 else p[key]
        v = cfg_get("crypto_whale.bias_threshold", config=config)
        if v is not None:
            fv = float(v)
            p["bias_threshold"] = fv if 0.0 < fv < 1.0 else p["bias_threshold"]
        for key in ("cache_max", "max_pages"):
            v = cfg_get(f"crypto_whale.{key}", config=config)
            if v is not None:
                iv = int(v)
                p[key] = iv if iv > 0 else p[key]
    except Exception as e:  # never let config break the signal path
        logger.debug(f"[whale] crypto_whale params config read failed: {e}")
        return dict(_CRYPTO_WHALE_DEFAULTS)
    return p


def compute_whale_flow(prints: list[Print], min_usd: float = 100_000.0,
                       symbol: str = "",
                       bias_threshold: Optional[float] = None) -> WhaleReport:
    """Net aggressive whale flow from large prints (>= min_usd)."""
    if bias_threshold is None:
        bias_threshold = crypto_whale_params()["bias_threshold"]
    buy = sell = 0.0
    whales = 0
    for p in prints:
        if p.usd < min_usd:
            continue
        whales += 1
        if p.is_buy:
            buy += p.usd
        else:
            sell += p.usd
    net = buy - sell
    total = buy + sell
    # bias needs a meaningful imbalance (>threshold share of whale $ on one side)
    if total > 0 and abs(net) / total >= bias_threshold:
        bias = "whale_buying" if net > 0 else "whale_selling"
    else:
        bias = "balanced"
    return WhaleReport(
        symbol=symbol, window_n=len(prints), whale_n=whales,
        buy_usd=round(buy, 2), sell_usd=round(sell, 2), net_usd=round(net, 2),
        bias=bias, min_usd=min_usd,
        note=("large aggressive buyers stepping in" if bias == "whale_buying"
              else "large aggressive sellers hitting bids" if bias == "whale_selling" else ""),
    )


# ── thin cached fetch ────────────────────────────────────────────────────────
_CACHE_TTL_S = 120.0
_CACHE_MAX = 1024
_cache: dict[str, tuple] = {}
_lock = threading.Lock()

# 2026-09-02：Binance 对未上市/已下架的机械拼造 symbol（{COIN}USDT，如
# CASHCATUSDT）稳定返回 HTTP 400 + body {"code":-1121,"msg":"Invalid symbol"}。
# 此前每次扫描都重试并刷 warning（"GET failed ... HTTP Error 400"）。对这类
# 确定性错误做 60 分钟负缓存：命中即跳过请求、日志降为 DEBUG。60 分钟后重试，
# 以便新币上市 / 币种恢复后能自动恢复（无人工干预）。
_INVALID_SYMBOL_TTL_S = 3600.0
_invalid_symbols: dict[str, float] = {}


def _is_invalid_symbol(symbol: str) -> bool:
    return time.time() < _invalid_symbols.get(symbol, 0.0)


def _mark_invalid_symbol(symbol: str, code: int) -> None:
    _invalid_symbols[symbol] = time.time() + _INVALID_SYMBOL_TTL_S
    logger.info(f"[whale] Binance reports {symbol} invalid (HTTP {code}, code=-1121); "
                f"skipping for {int(_INVALID_SYMBOL_TTL_S / 60)} min")

# Single-flight coalescing for cold misses.
_inflight: dict[str, threading.Event] = {}
_inflight_results: dict[str, object] = {}
_inflight_lock = threading.Lock()

# Binance is normally <1s, but a stalled edge can hold the default 10s for up
# to 6 sequential pages = 60s. Bound each page to 2.5s so a degraded Binance
# can't dominate the parallel-fetch window in research(). The live value comes
# from crypto_whale_params(); _HTTP_TIMEOUT_S remains as a module fallback.
_HTTP_TIMEOUT_S = 2.5


def _cache_sweep(now: float, ttl: float, max_keep: int) -> None:
    """Caller must hold _lock."""
    expired = [k for k, (ts, _v) in _cache.items() if (now - ts) >= ttl]
    for k in expired:
        _cache.pop(k, None)
    if len(_cache) > max_keep:
        overflow = len(_cache) - max_keep
        for k in sorted(_cache, key=lambda k: _cache[k][0])[:overflow]:
            _cache.pop(k, None)


# M-9 (supplemental audit 2026-08-30): only plain http/https URLs may be
# fetched. Without an explicit scheme allowlist a manipulated URL could use
# file:// (local file read / LFI) or ftp:// etc. through urllib.
def _is_safe_web_url(url: str) -> bool:
    try:
        return urllib.parse.urlsplit(url).scheme in ("http", "https")
    except Exception:
        return False


def _get_json(url: str, timeout: Optional[float] = None,
              symbol: Optional[str] = None) -> Optional[dict[str, Any]]:
    if timeout is None:
        timeout = crypto_whale_params()["http_timeout_s"]
    if not _is_safe_web_url(url):  # M-9: reject file:// and other non-web schemes
        logger.warning(f"[whale] refusing non-http(s) URL: {url[:80]!r}")
        return None
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    _t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as r:  # nosec B310 (supplemental audit 2026-08-30): scheme allowlisted by _is_safe_web_url above
            data = json.loads(r.read().decode("utf-8", "replace"))
            _elapsed = time.monotonic() - _t0
            if _elapsed > 1.5:
                logger.info(f"[whale] GET {url[:80]}... in {_elapsed:.2f}s")
            return data
    except urllib.error.HTTPError as _he:
        # 2026-09-02：HTTP 400 + Binance -1121 = 该 symbol 不存在（确定性错误，
        # 重试无意义）。读 body 判定后登记负缓存，日志降为 DEBUG（只打一次 INFO，
        # 见 _mark_invalid_symbol）。其他 HTTP 错误仍按原级别 warning。
        if symbol and _he.code == 400:
            try:
                _body = _he.read().decode("utf-8", "replace")
            except Exception:
                _body = ""
            if '"code":-1121' in _body.replace(" ", ""):
                _mark_invalid_symbol(symbol, _he.code)
                logger.debug(f"[whale] {symbol} invalid-symbol response (HTTP 400, -1121); "
                             f"negatively cached for {int(_INVALID_SYMBOL_TTL_S / 60)} min")
                return None
        _elapsed = time.monotonic() - _t0
        logger.warning(f"[whale] GET failed after {_elapsed:.2f}s: HTTPError: {_he}")
        return None
    except Exception as _e:
        _elapsed = time.monotonic() - _t0
        logger.warning(f"[whale] GET failed after {_elapsed:.2f}s: {type(_e).__name__}: {_e}")
        return None


def fetch_aggtrades_window(symbol: str, window_minutes: Optional[float] = None,
                           max_pages: Optional[int] = None,
                           page_limit: int = 1000) -> list[Print]:
    """Pull ALL aggTrades over the last `window_minutes` by forward-paginating from
    startTime (fromId), so the read covers real minutes — not the ~seconds that a
    single latest-1000 batch spans on a liquid pair. Bounded by max_pages."""
    p = crypto_whale_params()
    if window_minutes is None:
        window_minutes = p["window_minutes"]
    if max_pages is None:
        max_pages = int(p["max_pages"])
    sym = urllib.parse.quote(symbol)
    start_ms = int((time.time() - window_minutes * 60) * 1000)
    payload = _get_json(f"{_AGG}?symbol={sym}&startTime={start_ms}&limit={page_limit}",
                        symbol=symbol)
    if not isinstance(payload, list):
        return []
    prints = parse_aggtrades(payload)
    pages = 1
    while payload and len(payload) >= page_limit and pages < max_pages:
        last_id = payload[-1].get("a")
        if last_id is None:
            break
        payload = _get_json(f"{_AGG}?symbol={sym}&fromId={int(last_id) + 1}&limit={page_limit}",
                            symbol=symbol)
        if not isinstance(payload, list):
            break
        prints.extend(parse_aggtrades(payload))
        pages += 1
    return prints


def crypto_whale_signal(coin: str, min_usd: Optional[float] = None,
                        window_minutes: Optional[float] = None,
                        max_pages: Optional[int] = None,
                        ttl: Optional[float] = None,
                        allow_fetch: bool = True) -> Optional[WhaleReport]:
    """Free whale-flow report for a crypto coin via Binance public aggTrades over a
    rolling time WINDOW. Returns None for xyz: equities or on fetch failure.

    allow_fetch=False = CACHE-ONLY (return last cached value or None, no network)."""
    p = crypto_whale_params()
    if ttl is None:
        ttl = p["ttl_sec"]
    if min_usd is None:
        min_usd = p["min_usd"]
    if window_minutes is None:
        window_minutes = p["window_minutes"]
    if max_pages is None:
        max_pages = int(p["max_pages"])
    cache_max = int(p["cache_max"])
    http_timeout_s = p["http_timeout_s"]
    sym = binance_symbol(coin)
    if not sym:
        return None
    # 2026-09-02：已知无效 symbol（Binance -1121，如未上市的 CASHCATUSDT）
    # 在 60min 负缓存窗口内直接跳过，不发请求、不刷 warning。
    if allow_fetch and _is_invalid_symbol(sym):
        logger.debug(f"[whale] crypto_whale_signal({coin}) skipped: {sym} in invalid-symbol cache")
        return None
    now = time.time()
    key = f"{sym}::{int(min_usd)}::{window_minutes}"
    with _lock:
        hit = _cache.get(key)
        if hit and (now - hit[0]) < ttl:
            return hit[1]
    if not allow_fetch:
        return hit[1] if hit else None

    # Single-flight coalescing for cold cache misses.
    waiter: Optional[threading.Event] = None
    with _inflight_lock:
        existing = _inflight.get(key)
        if existing is not None:
            waiter = existing
        else:
            evt = threading.Event()
            _inflight[key] = evt
    if waiter is not None:
        _wt0 = time.monotonic()
        waiter.wait(timeout=(http_timeout_s * max_pages) + 2.0)
        with _inflight_lock:
            result = _inflight_results.get(key)
        logger.debug(f"[whale] crypto_whale_signal({coin}) coalesced in "
                     f"{time.monotonic() - _wt0:.2f}s")
        return result if isinstance(result, WhaleReport) else None

    _fetch_t0 = time.monotonic()
    try:
        prints = fetch_aggtrades_window(sym, window_minutes=window_minutes, max_pages=max_pages)
        rep = compute_whale_flow(prints, min_usd=min_usd, symbol=sym) if prints else None
        if rep is not None:
            rep = WhaleReport(**{**rep.__dict__, "window_minutes": window_minutes})
        with _lock:
            _cache[key] = (now, rep)
            if len(_cache) > cache_max:
                _cache_sweep(now, ttl, cache_max)
        with _inflight_lock:
            _inflight_results[key] = rep
        _elapsed = time.monotonic() - _fetch_t0
        if _elapsed > 1.5:
            logger.info(f"[whale] crypto_whale_signal({coin}) in {_elapsed:.2f}s "
                        f"({len(prints)} prints)")
        return rep
    finally:
        with _inflight_lock:
            _evt = _inflight.pop(key, None)
            _inflight_results.pop(key, None)
        if _evt is not None:
            _evt.set()
