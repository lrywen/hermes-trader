"""Hyperliquid API client — HTTP + persistent Info() for REST.

Architecture:
1. Single Info() instance for meta/spot_meta (REST, not WS)
2. All candle/mids/account calls via direct HTTP POST
3. Optional websocket for future realtime features
4. Volume-based pre-filtering to stay under rate limits

Rate limit management:
- metaAndAssetCtxs (weight 20) returns ALL 230 perps with dayNtlVlm
- spotMetaAndAssetCtxs (weight 20) returns ALL 297 spot assets
- candleSnapshot per-coin costs weight 20 each
- Total capacity: 1,200 weight/minute
- Strategy: volume-filter to top N markets, then fetch candles only for those
- Candle cache with 15min TTL so repeated scans reuse data
"""

from __future__ import annotations

import logging
import math
import os
import random
import threading
import time
from typing import TYPE_CHECKING, Any, Optional

import requests

from hermes_trader.client.rate_limit import (
    _HL_CLIENT_IO,
    _HL_RATE_LIMIT,
)
from hermes_trader.client.rate_limit import (
    HL_LIMITER as _HL_LIMITER,
)
from hermes_trader.client.rate_limit import (
    endpoint_weight as _endpoint_weight,
)
from hermes_trader.client.rate_limit import (
    timed_per_endpoint_gate as _per_endpoint_gate,
)
from hermes_trader.models.types import Candle

if TYPE_CHECKING:
    from hyperliquid.info import Info

    from hermes_trader.client.ws_client import HyperliquidWebSocket

logger = logging.getLogger(__name__)


class CandleFetchError(RuntimeError):
    """Raised when a candle fetch fails due to a transient error (429/timeout).

    Distinct from a genuine empty-history result (which returns ``[]``): a
    ``CandleFetchError`` means the data is *unknown* and the caller should
    treat the coin as un-scannable this cycle rather than reading it as
    "no signal / insufficient history"."""

# ── Environment-driven API endpoint ─────────────────────────────────
# Default: Hyperliquid mainnet. Set HYPERLIQUID_TESTNET=true to switch all
# HTTP/WS traffic to the testnet (api.hyperliquid-testnet.xyz).
def _hl_api_base() -> str:
    is_testnet = os.environ.get("HYPERLIQUID_TESTNET", "").strip().lower() in ("1", "true", "yes", "on")
    return "https://api.hyperliquid-testnet.xyz" if is_testnet else "https://api.hyperliquid.xyz"


HL_API = _hl_api_base()
_MS_PER_CANDLE: dict[str, int] = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}

# ── Singleton Info client ─────────────────────────────────────────────
# We create Info() ONCE with pre-fetched meta (HTTP, fast, no WS blocking).

_info_instance: "Info | None" = None
_info_lock = threading.Lock()


def _fetch_meta_sync() -> tuple:
    """Fetch meta and spot_meta via HTTP (fast, no WS needed).

    Goes through `_http_post` so these two weight-20 calls draw from the
    shared rate bucket (a bare requests.post bypassed the limiter and could
    fire during a 429 backoff window).
    """
    try:
        perp_meta = _http_post("/info", {"type": "meta"}, timeout=10)
        spot_meta = _http_post("/info", {"type": "spotMeta"}, timeout=10)
        if not perp_meta:
            perp_meta = {}
        if not spot_meta:
            spot_meta = {}
        return perp_meta, spot_meta
    except Exception as e:
        logger.error(f"[hl] Meta fetch failed: {e}")
        return {}, {}


def init_info() -> None:
    """Initialize the shared Info client.

    Fast path: fetches meta via HTTP then creates Info with skip_ws=True.
    """
    global _info_instance

    with _info_lock:
        if _info_instance is not None:
            return

        logger.info("[hl] Initializing Info client...")
        perp_meta, spot_meta = _fetch_meta_sync()

        try:
            from hyperliquid.info import Info
            # skip_ws=True prevents blocking WS connect + meta fetch
            # We already have meta from HTTP above
            _info_instance = Info(skip_ws=True, base_url=HL_API, meta=perp_meta, spot_meta=spot_meta)
            logger.info("[hl] Info client initialized (HTTP-only)")
        except Exception as e:
            logger.warning(f"[hl] Failed to create Info: {e}")
            _info_instance = None


# ── HTTP helpers ──────────────────────────────────────────────────────

# Shared connection pool. Every _http_post reuses keep-alive connections
# instead of opening a fresh TCP+TLS handshake per call (~50-200ms each).
# At 60 markets × 2 candle fetches + 8 dex queries per scan, that handshake
# tax dominated. requests.Session is thread-safe for concurrent .post()
# when the adapter pool is sized for our fan-out (8 dex queries + headroom).
_session: "requests.Session | None" = None
_session_lock = threading.Lock()


def _get_session() -> "requests.Session":
    global _session
    if _session is None:
        with _session_lock:
            if _session is None:
                s = requests.Session()
                adapter = requests.adapters.HTTPAdapter(
                    pool_connections=16,
                    pool_maxsize=32,
                    max_retries=requests.adapters.Retry(
                        total=2, backoff_factor=0.3,
                        # C-M3: 408 is response-unknown; GET/POST info calls
                        # are read-side (and order POST carries Cloid), so a
                        # retry cannot double-submit. See exchange.py H6 notes.
                        status_forcelist=[408, 502, 503, 504],
                        allowed_methods=["POST"],
                    ),
                )
                s.mount("https://", adapter)
                _session = s
    return _session


def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    """Parse an HTTP Retry-After header (seconds or HTTP-date) into seconds.

    Returns None if absent/unparseable so the caller falls back to its own
    backoff. Date values are clamped to a sane upper bound.
    """
    if not value:
        return None
    value = value.strip()
    # Delta-seconds form.
    try:
        secs = float(value)
        return max(0.0, secs)
    except ValueError:
        pass
    # HTTP-date form.
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(value)
        if dt is not None:
            wait = (dt.timestamp() - time.time())
            return max(0.0, wait)
    except (TypeError, ValueError):
        pass
    return None


def _http_post(
    path: str,
    payload: dict[str, Any],
    timeout: int = 5,
    max_wait: Optional[float] = None,
) -> Any:
    """Direct HTTP POST over the shared keep-alive connection pool.

    Acquires a rate-limit token first (HL: ~1200 weight/min). On budget
    exhaustion, returns None so the caller's existing retry/backoff handles it
    rather than firing into a 429.

    `max_wait` caps how long we queue for budget. Trading-path callers leave it
    None and get the full HERMES_HL_RATE_MAX_WAIT_S (30s) — their data is
    required. Observability callers (e.g. surge postmortem candles) pass a small
    value to declare themselves opportunistic: they yield the budget to the
    trading path instead of parking on it for 30s, and a miss is logged at debug
    since losing that data is expected and harmless.
    """
    weight = _endpoint_weight(payload.get("type"))
    req_type = payload.get("type") or "unknown"
    opportunistic = max_wait is not None
    if max_wait is None:
        # R13-B13: canonical hl_rate_limit.rate_max_wait_s (import-time snapshot;
        # the legacy HERMES_HL_RATE_MAX_WAIT_S env channel still wins at boot).
        max_wait = float(_HL_RATE_LIMIT["rate_max_wait_s"])

    # R11-C1: serialize concurrent calls into the SAME endpoint so a
    # burst of workers doesn't all stampede the endpoint's per-route
    # limit and 429 as a herd. The gate is held across the full
    # acquire + HTTP + release cycle. Different endpoints lock
    # independently so cross-endpoint parallelism is preserved.
    _t0 = time.monotonic()
    with _per_endpoint_gate(req_type):
        _gate_wait_s = time.monotonic() - _t0
        return _hl_request_inner(
            payload=payload,
            path=path,
            weight=weight,
            req_type=req_type,
            opportunistic=opportunistic,
            max_wait=max_wait,
            timeout=timeout,
            _gate_wait_s=_gate_wait_s,
        )


def _hl_request_inner(
    *,
    payload,
    path: str,
    weight: int,
    req_type: str,
    opportunistic: bool,
    max_wait: float,
    timeout: float,
    _gate_wait_s: float,
):
    _rate_t0 = time.monotonic()
    if not _HL_LIMITER.acquire(weight, max_wait=max_wait):
        _wait_s = time.monotonic() - _rate_t0
        msg = (
            f"[hl] rate budget exhausted for {req_type} "
            f"(weight={weight}, gate={_gate_wait_s*1000:.1f}ms, "
            f"waited {_wait_s:.2f}s/{max_wait:g}s) — skipping request"
        )
        if opportunistic:
            logger.debug(f"{msg} [opportunistic]")
        else:
            logger.warning(msg)
        return None
    _rate_wait_s = time.monotonic() - _rate_t0

    _http_t0 = time.monotonic()
    # Bounded 429-aware retry. We already hold rate-limit tokens for this
    # request; on a server-side 429 we drain the bucket by Retry-After
    # (adaptive penalty so other callers queue instead of piling on) and
    # retry once after the server's instructed wait. urllib3's Retry is NOT
    # used for 429 because it can't honor Retry-After and would re-fire in
    # lockstep across the worker pool.
    # R13-B13: canonical hl_rate_limit.rate_429_retries (import-time snapshot;
    # legacy HERMES_HL_429_RETRIES env channel still wins at boot).
    max_429_retries = int(_HL_RATE_LIMIT["rate_429_retries"])
    for attempt in range(max_429_retries + 1):
        try:
            resp = _get_session().post(f"{HL_API}{path}", json=payload, timeout=timeout)
            if resp.status_code == 429:
                retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
                # Penalty: drain the bucket enough to cover the cooldown the
                # server asked for (refill rate * wait), plus one request
                # weight. Capped so a bad header can't zero the bucket for long.
                cooldown = min(retry_after, 15.0) if retry_after else 2.0
                penalty = weight + _HL_LIMITER.refill_per_sec * cooldown
                try:
                    _HL_LIMITER.penalize(penalty)
                except Exception:
                    pass
                if attempt < max_429_retries:
                    logger.warning(
                        f"[hl] {req_type} got 429 (attempt {attempt+1}) — "
                        f"draining {penalty:.0f} budget, backing off {cooldown:.1f}s"
                    )
                    time.sleep(cooldown + random.uniform(0, 0.25))
                    continue
                logger.warning(
                    f"[hl] {req_type} 429 persisted after {max_429_retries} retries — giving up"
                )
                return None
            resp.raise_for_status()
            _result = resp.json()
            _http_s = time.monotonic() - _http_t0
            if _rate_wait_s > 1.0 or _http_s > 3.0:
                logger.info(
                    f"[hl] {req_type} | rate_wait={_rate_wait_s:.2f}s http={_http_s:.2f}s "
                    f"total={time.monotonic()-_http_t0:.2f}s weight={weight}"
                )
            return _result
        except Exception as e:
            _http_s = time.monotonic() - _http_t0
            logger.error(
                f"[hl] HTTP POST {path} ({req_type}) failed after {_http_s:.2f}s: {e}"
            )
            return None


# ── Public API ────────────────────────────────────────────────────────

def get_info() -> "Info | None":
    """Get the shared Info client instance."""
    global _info_instance
    if _info_instance is None:
        init_info()
    return _info_instance


def resolve_user_address() -> str:
    """Master address if set, else wallet address, else empty string."""
    return os.environ.get("HYPERLIQUID_MASTER_ADDRESS") or os.environ.get("HYPERLIQUID_WALLET_ADDRESS", "")


# Short-TTL candle cache. ta_filter and research() each fetched the SAME
# coin's 1h/4h/1d candles back-to-back per scan (6 network calls where 3 suffice),
# doubling the API pressure behind the recurring 429 storms that kill scans. A
# small per-(coin,interval) TTL collapses those duplicates within a cycle. TTL is
# well under a candle period so freshness is unaffected; env-tunable / 0 disables.
#
# Uses the shared LRU+TTL ``_Cache`` abstraction (client/cache.py) instead of a
# bespoke dict+sweep; this gives bounded size, atomic access and per-key
# invalidation for free.
# R13-B13: TTL/size resolve from canonical hl_client_io (import-time
# snapshot; legacy HERMES_CANDLE_CACHE_TTL_S / HERMES_CANDLE_CACHE_MAX env
# channels still win at boot). TTL <= 0 disables the cache as before.
_CANDLE_CACHE_TTL_S = float(_HL_CLIENT_IO["candle_cache_ttl_s"])
_CANDLE_CACHE_MAX = int(_HL_CLIENT_IO["candle_cache_max"])
_CANDLE_CACHE_DISABLED = _CANDLE_CACHE_TTL_S <= 0

if _CANDLE_CACHE_DISABLED:
    _CANDLE_CACHE = None
else:
    from hermes_trader.client.cache import _Cache as _SharedCache
    _CANDLE_CACHE = _SharedCache(max_size=_CANDLE_CACHE_MAX, default_ttl=_CANDLE_CACHE_TTL_S)

# Funding rate cache: funding updates hourly, so a 5-min TTL is safe and
# eliminates a weight-20 POST on every research cycle.
# R13-B13: canonical hl_client_io.funding_cache_ttl_s (import-time
# snapshot; legacy HERMES_FUNDING_CACHE_TTL_S env channel still wins).
_FUNDING_CACHE_TTL_S = float(_HL_CLIENT_IO["funding_cache_ttl_s"])
if _FUNDING_CACHE_TTL_S <= 0:
    _FUNDING_CACHE = None
else:
    from hermes_trader.client.cache import _Cache as _FundingCache
    _FUNDING_CACHE = _FundingCache(max_size=256, default_ttl=_FUNDING_CACHE_TTL_S)

# In-flight request coalescing: when N callers request the same cache_key
# concurrently (e.g. 3 candle fetches across research + ta_filter), only one
# HTTP call fires; the rest await the same result. Without this, a cold cache
# after restart issues 3 duplicate candleSnapshot calls (weight 60) instead of
# one (weight 20), tripling rate-budget pressure during exactly the window
# when the bucket is already drained.
_inflight: dict[str, "threading.Event"] = {}
_inflight_results: dict[str, Any] = {}
_inflight_lock = threading.Lock()


def _candle_cache_metric(interval: str, result: str) -> None:
    """Best-effort Prometheus counter for candle cache outcomes (hit/coalesced/miss).

    Phase 0 (deep audit R7): miss-rate per interval exposes how often the
    gate/screen pay a cold weight-20 HTTP call instead of reusing the 90s
    cache. Never raises — metrics must not touch the trading path.
    """
    try:
        from hermes_trader import metrics
        metrics.CANDLE_CACHE_LOOKUPS.labels(interval=interval, result=result).inc()
    except Exception:
        pass


def fetch_hl_candles(
    coin: str,
    interval: str = "5m",
    count: int = 100,
    opportunistic: bool = False,
) -> list[Candle]:
    """Fetch candles via HTTP (short-TTL cached per coin+interval+count).

    `opportunistic=True` marks the call as observability-only: it takes at most
    a short slice of rate budget, skips the retry ladder, and never warns. Used
    by the surge postmortem, whose candleSnapshot (weight 20) calls otherwise
    compete with the trading path's own fetches.
    """
    cache_key = f"{coin}|{interval}|{count}"
    if _CANDLE_CACHE is not None:
        hit = _CANDLE_CACHE.get(cache_key)
        if hit is not None:
            logger.debug(f"[candles] {coin} {interval}: cache HIT ({len(hit)} bars)")
            _candle_cache_metric(interval, "hit")
            return hit

    # Coalesce concurrent requests for the same key: if another thread is
    # already fetching this exact candle range, wait for its result instead
    # of issuing a duplicate weight-20 HTTP call.
    _coalesce_event: Optional[threading.Event] = None
    with _inflight_lock:
        existing = _inflight.get(cache_key)
        if existing is not None:
            _coalesce_event = existing
        elif not opportunistic:
            _inflight[cache_key] = threading.Event()

    if _coalesce_event is not None:
        _wait_t0 = time.monotonic()
        _coalesce_event.wait(timeout=30)
        with _inflight_lock:
            result = _inflight_results.get(cache_key)
        if result is not None:
            logger.debug(
                f"[candles] {coin} {interval}: coalesced "
                f"(waited {time.monotonic()-_wait_t0:.2f}s)"
            )
            _candle_cache_metric(interval, "coalesced")
            return result
        # If the leader failed/timed out, fall through and fetch ourselves.
        logger.debug(f"[candles] {coin} {interval}: coalesce leader failed, fetching directly")

    _fetch_t0 = time.monotonic()
    try:
        candles = _fetch_hl_candles_raw(coin, interval, count, cache_key, opportunistic)
        # Publish successful result to coalesced waiters before signalling.
        with _inflight_lock:
            _inflight_results[cache_key] = candles
    except Exception:
        with _inflight_lock:
            _inflight_results[cache_key] = None
        raise
    finally:
        with _inflight_lock:
            _evt = _inflight.pop(cache_key, None)
        if _evt is not None:
            _evt.set()

    _elapsed = time.monotonic() - _fetch_t0
    if _elapsed > 2.0:
        logger.info(
            f"[candles] {coin} {interval}: fetch {len(candles)} bars in {_elapsed:.2f}s"
        )
    _candle_cache_metric(interval, "miss")
    return candles


def closed_candles_only(
    candles: "list[Candle]", interval: str
) -> "tuple[list[Candle], bool]":
    """H-8 (supplemental audit 2026-08-30): drop the still-forming final bar.

    candleSnapshot with ``endTime=now`` always includes the in-progress bar;
    its h/l/c are mid-bar values that WILL change before close. Indicators
    computed on it (ATR, EMA, RSI, ADX, trigger evaluation) see a partially
    filled range that shrinks ATR and biases signals — a look-ahead on data
    that does not yet exist at decision time. Return ``(series_to_score,
    dropped)`` where ``dropped`` reports whether a forming bar was removed.
    Client-layer counterpart of ``agents.perception._drop_forming_bar``
    (agents may import client, never the reverse).
    """
    if not candles:
        return candles, False
    last = candles[-1]
    bar_dur_ms = _MS_PER_CANDLE.get(interval, 300_000)
    if time.time() * 1000.0 >= float(getattr(last, "t")) + bar_dur_ms:
        return candles, False
    return candles[:-1], True


def _fetch_hl_candles_raw(
    coin: str,
    interval: str,
    count: int,
    cache_key: str,
    opportunistic: bool,
) -> list[Candle]:
    """Internal: actual HTTP fetch + parse for fetch_hl_candles."""
    ms = _MS_PER_CANDLE.get(interval, 300_000)
    end_time = int(time.time() * 1000)
    start_time = end_time - ms * count

    payload = {
        "type": "candleSnapshot",
        "req": {
            "coin": coin,
            "interval": interval,
            "startTime": start_time,
            "endTime": end_time,
        }
    }
    if opportunistic:
        # One shot, tiny budget wait, no retries: the caller's report just omits
        # the candle section rather than starving the trading path.
        opp_wait = float(_HL_RATE_LIMIT["rate_opportunistic_wait_s"])
        raw = _http_post("/info", payload, max_wait=opp_wait)
        if not isinstance(raw, list):
            logger.debug(
                f"[candles] {coin} {interval}: opportunistic fetch missed "
                f"(rate budget or transient error) — returning EMPTY")
            return []
    else:
        # A non-list result means a transient failure (429 / timeout), NOT "no
        # candles" — HL returns an empty LIST for a coin with no history. Those
        # must be distinguishable downstream: research must not treat a
        # 429-blanked fetch as "insufficient history" (which previously emitted
        # stop/tp = 0.0 and could be force-executed stopless by the whale
        # override). Retry transient failures with backoff; if they persist,
        # raise CandleFetchError so callers fail-closed instead of misreading
        # "unknown" as "no signal".
        raw = _http_post("/info", payload)
        attempts = 0
        while not isinstance(raw, list) and attempts < 3:
            attempts += 1
            # H14: exponential-ish backoff with jitter so a coordinated burst
            # of per-coin retries doesn't re-hit the rate limiter in lockstep.
            time.sleep(0.3 * attempts + random.uniform(0, 0.25))
            raw = _http_post("/info", payload)
        if not isinstance(raw, list):
            logger.warning(
                f"[candles] {coin} {interval}: fetch failed across {attempts} retries "
                f"(transient 429/timeout) — raising CandleFetchError (fail-closed)")
            raise CandleFetchError(
                f"candle fetch failed for {coin} {interval} after {attempts} retries"
            )

    # Genuine empty history: HL returns []. Surface as [] (distinct from failure).
    candles: list[Candle] = []
    prev_t: Optional[int] = None
    for c in raw:
        try:
            o = float(c["o"]); h = float(c["h"]); l = float(c["l"]); close = float(c["c"])
            v = float(c.get("v", "0")); t = int(c["t"])
        except (KeyError, TypeError, ValueError):
            logger.warning(f"[candles] {coin} {interval}: dropping malformed candle {c!r}")
            continue
        # Reject NaN/Inf OHLC — one bad bar poisons every indicator downstream.
        if not all(math.isfinite(x) for x in (o, h, l, close, v)):
            logger.warning(f"[candles] {coin} {interval}: dropping non-finite candle t={t}")
            continue
        # Drop duplicate / out-of-order timestamps (monotonic ascending required).
        if prev_t is not None and t <= prev_t:
            continue
        prev_t = t
        candles.append(Candle(t=t, o=o, h=h, l=l, c=close, v=v))

    # C-M2 (deep audit 2026-08-28): quality gate. HL's candleSnapshot has
    # silently returned truncated / gappy / stale series during 429 storms —
    # an ATR computed over those bars is distorted (too small → oversized
    # position + stop too tight) and nothing downstream noticed. Annotate the
    # series; the sizing path (get_hl_atr) treats a non-ok gate as 0.0 so the
    # executor's existing ``atr <= 0 → no_atr_no_stop`` rule fails CLOSED.
    if candles:
        quality = assess_candle_quality(candles, interval, count)
        if not quality["ok"]:
            logger.warning(
                f"[candles] {coin} {interval}: quality gate failed "
                f"({', '.join(quality['issues'])}; bars={len(candles)}, "
                f"gaps={quality['gaps']}, age_ms={quality['age_ms']}) — "
                f"ATR/sizing consumers must fail-closed")

    if _CANDLE_CACHE is not None and candles:
        _CANDLE_CACHE.set(cache_key, candles)
    return candles


def assess_candle_quality(
    candles: "list[Candle]",
    interval: str,
    expected_count: int,
    now_ms: Optional[float] = None,
) -> dict[str, Any]:
    """C-M2: sanity-check a candle series for continuity / freshness / coverage.

    Returns ``{"ok": bool, "issues": [str, ...], "gaps": int, "age_ms": int}``.

    Checks (all against HL's fixed-alignment grid, bar open time = ``t``):
    - gaps: adjacent closed bars whose open-time delta != exactly one interval
      (a missing bar anywhere silently compresses indicator history);
    - stale: the newest closed bar is more than ``2 * interval`` old (feed
      outage — a "fresh" series cannot lag by two+ bars);
    - low_coverage: fewer closed bars than ``80%`` of ``expected_count``
      (truncated snapshot produces unreliable, over-fit indicators);
    - thin: fewer than 2 closed bars (nothing to compute on).
    """
    issues: list[str] = []
    gaps = 0
    age_ms = -1
    ms = _MS_PER_CANDLE.get(interval, 300_000)
    cur_ms = now_ms if now_ms is not None else time.time() * 1000.0

    # Series is ascending by construction (out-of-order ts are dropped at
    # parse). The still-forming last bar has no successor to gap-check against
    # and its open time is always current, so excluding it leaves exactly the
    # closed bars — this also covers the cache-tail continuity comparison:
    # a refetched series whose closed bars don't chain at the cache boundary
    # shows up here as an internal delta != ms.
    closed = candles
    if candles and cur_ms < float(candles[-1].t) + ms:
        closed = candles[:-1]

    if len(closed) < 2:
        issues.append("thin")
        return {"ok": False, "issues": issues or ["thin"], "gaps": gaps,
                "age_ms": age_ms}

    for i in range(1, len(closed)):
        delta = int(closed[i].t) - int(closed[i - 1].t)
        if delta != ms:
            gaps += 1
    if gaps:
        issues.append("gaps")

    age_ms = int(cur_ms - float(closed[-1].t))
    if age_ms > 2 * ms:
        issues.append("stale")

    expected_closed = max(1, expected_count - 1)
    if len(closed) < int(expected_closed * 0.8):
        issues.append("low_coverage")

    return {"ok": not issues, "issues": issues, "gaps": gaps, "age_ms": age_ms}


def fetch_account_state(user: str, include_hip3: bool = False) -> dict[str, Any]:
    """Fetch perp + spot account state, optionally aggregating HIP-3 dexes.

    When `include_hip3=True`, queries each HIP-3 perpDex's clearinghouse
    (one POST per dex), sums equity + total_ntl, concatenates asset_positions
    (HIP-3 coins normalized to `<dex>:<coin>`), and returns `dex_equity` +
    `queried_dexes` (the dexes that actually responded — used by the DSL
    rehydrator to avoid dropping trackers when a dex query times out).

    `available` stays main-dex only because HIP-3 free margin only backs
    trades on its own dex; the executor sizes against this for main trades.
    """
    perp = _http_post("/info", {"type": "clearinghouseState", "user": user}, timeout=15)
    spot = _http_post("/info", {"type": "spotClearinghouseState", "user": user}, timeout=15)

    if not perp:
        perp = {}
    if not spot:
        spot = {}

    margin_summary = perp.get("marginSummary", {})
    perp_equity = float(margin_summary.get("accountValue", "0"))
    total_ntl = float(margin_summary.get("totalNtlPos", "0"))
    total_margin_used = float(margin_summary.get("totalMarginUsed", "0"))

    raw_balances = spot.get("balances", []) or []
    spot_balances = [b for b in raw_balances if b.get("coin", "") in ("USDC", "USDT", "USD")]
    spot_usdc = sum(float(b.get("total", "0") or 0) for b in spot_balances)

    raw_positions = perp.get("assetPositions", []) or []
    asset_positions = [
        p for p in raw_positions
        if float(p.get("position", {}).get("szi", "0")) != 0
    ]

    equity = perp_equity
    # Free initial margin = what HL's UI shows as "Available to Trade" and
    # what HL checks before accepting new orders. `withdrawable` is a
    # different (much tighter) number — the spot-bridgeable amount — and
    # using it gated the executor at ~5% of equity even when ~50% was
    # actually free for new positions.
    available = max(0.0, equity - total_margin_used)

    dex_equity: dict[str, float] = {"": perp_equity}
    dex_available: dict[str, float] = {"": available}
    queried_dexes: set = {""}
    available_aggregated = available  # starts as main; HIP-3 adds in

    if include_hip3:
        from concurrent.futures import ThreadPoolExecutor

        from hermes_trader.client.universe import list_hip3_dexes
        try:
            dexes = list_hip3_dexes()
        except Exception as e:
            logger.warning(f"[hl] list_hip3_dexes failed during account aggregation: {e}")
            dexes = []

        # HIP-3 dex mute (mirrors the scanner): only aggregate balances for the
        # dexes we actually trade, so unfunded test/misc venues don't add 200+
        # clearinghouse POSTs to every dashboard poll. `hip3_dex_allowlist`
        # (e.g. ["xyz"]) = aggregate ONLY those dexes; `hip3_dex_blocklist` =
        # aggregate all but those.
        try:
            from hermes_trader.agents.config_store import read_agent_config
            _cfg = read_agent_config()
        except Exception:
            _cfg = {}
        allow = {d for d in (_cfg.get("hip3_dex_allowlist") or []) if d}
        block = {d for d in (_cfg.get("hip3_dex_blocklist") or []) if d}
        if allow:
            dexes = [d for d in dexes if d in allow]
        if block:
            dexes = [d for d in dexes if d not in block]
        if not dexes:
            logger.info("[hl] HIP-3 aggregation skipped — no dexes after allow/block filter")

        # Fan out the per-dex clearinghouse queries in parallel — serial loop
        # was 8 sequential POSTs × ~150ms each = 1.2s. Parallel finishes in
        # the slowest single call (~200ms), 4-6× speedup on the dashboard.
        def _fetch_dex(dex_name: str) -> tuple[str, Optional[dict[str, Any]]]:
            try:
                return (dex_name, _http_post("/info", {
                    "type": "clearinghouseState", "user": user, "dex": dex_name,
                }))
            except Exception as e:
                logger.warning(f"[hl] HIP-3 clearinghouseState failed for {dex_name}: {e}")
                return (dex_name, None)

        if dexes:
            with ThreadPoolExecutor(max_workers=min(8, len(dexes)),
                                    thread_name_prefix="hl-dex") as pool:
                for dex, dex_state in pool.map(_fetch_dex, dexes):
                    if dex_state is None:
                        continue
                    queried_dexes.add(dex)
                    dex_ms = dex_state.get("marginSummary", {}) or {}
                    dex_value = float(dex_ms.get("accountValue", 0) or 0)
                    dex_ntl = float(dex_ms.get("totalNtlPos", 0) or 0)
                    dex_margin_used = float(dex_ms.get("totalMarginUsed", 0) or 0)
                    dex_free = max(0.0, dex_value - dex_margin_used)
                    dex_equity[dex] = dex_value
                    dex_available[dex] = dex_free
                    equity += dex_value
                    total_ntl += dex_ntl
                    available_aggregated += dex_free
                    for p in (dex_state.get("assetPositions") or []):
                        pos = p.get("position", {}) or {}
                        if float(pos.get("szi", "0") or 0) == 0:
                            continue
                        coin = pos.get("coin", "") or ""
                        # HL HIP-3 endpoints return bare or namespaced — normalize.
                        if coin and ":" not in coin:
                            pos["coin"] = f"{dex}:{coin}"
                        asset_positions.append(p)

    return {
        "equity": equity,
        "available": available,                       # main-only — for executor sizing
        "available_aggregated": available_aggregated, # total across all dexes — for display
        "spot_usdc": spot_usdc,
        "total_usdc": equity + spot_usdc,
        "total_ntl": total_ntl,
        "spot_balances": spot_balances,
        "asset_positions": asset_positions,
        "dex_equity": dex_equity,
        "dex_available": dex_available,
        # JSON consumers (FastAPI JSONResponse) can't serialize a set; sort
        # for stable output. Internal callers wrap with set() or use `in`,
        # both of which work on a list.
        "queried_dexes": sorted(queried_dexes),
        # P0-4: per-coin liquidation price snapshot used by the executor's
        # pre-place gate. Each entry: {"liquidationPx": float, "szi": float,
        # "side": "long"|"short"}. `szi` is the signed size in coin units
        # (positive = long, negative = short). `liquidationPx` may be None
        # or "0" for very small / fully-margined positions; we filter those
        # out so the executor only sees positions that actually have a real
        # liquidation price. Format chosen to match HL clearinghouseState's
        # assetPositions[].position.liquidationPx field.
        "liquidation_px_by_coin": {
            p.get("position", {}).get("coin"): {
                "liquidationPx": _parse_liquidation_px(
                    p.get("position", {}).get("liquidationPx")
                ),
                "szi": float(p.get("position", {}).get("szi", "0") or 0),
            }
            for p in asset_positions
            if _parse_liquidation_px(p.get("position", {}).get("liquidationPx")) is not None
        },
    }


def _parse_liquidation_px(raw) -> Optional[float]:
    """P0-4: defensively parse HL's liquidationPx field.

    HL returns it as a string ("0" for fully-margined/very small positions,
    or the actual price for cross-margin positions). Returns None when the
    value is missing, "0", empty, or unparseable — the executor must never
    build a gate on a missing or zero liquidation price (that would either
    be a no-op or a false-positive blow-up).
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or s == "0" or s.lower() in ("none", "null", "nan", ""):
        return None
    try:
        v = float(s)
    except (ValueError, TypeError):
        return None
    if v <= 0 or v != v:  # NaN check (v != v is the canonical NaN test)
        return None
    return v


def fetch_aggregate_contributions_since(user: str, start_ms: int) -> float:
    """Net USDC flowing INTO main + HIP-3 dex clearinghouses since `start_ms`.

    Used by daily-PnL tracking so transfers don't masquerade as trading
    gains: `daily_pnl = equity_now - equity_sod - contributions`. Counts
    deposits/withdrawals + transfers crossing the pool boundary (spot↔perp,
    spot↔HIP-3); treats intra-pool transfers (main↔xyz, xyz↔vntl) as neutral.

    Returns 0.0 on lookup failure to avoid distorting PnL on transient outages.
    """
    if not user or start_ms <= 0:
        return 0.0
    try:
        events = _http_post("/info", {
            "type": "userNonFundingLedgerUpdates",
            "user": user,
            "startTime": int(start_ms),
        }) or []
    except Exception as e:
        logger.warning(f"[hl] ledger fetch failed for contributions: {e}")
        return 0.0
    if not isinstance(events, list):
        return 0.0

    # Pool members: main HL is keyed as "" in HL's `send` schema; HIP-3
    # dexes are keyed by name.
    in_pool = {""}
    try:
        from hermes_trader.client.universe import list_hip3_dexes
        in_pool.update(list_hip3_dexes())
    except Exception:
        pass

    user_lc = (user or "").lower()
    net = 0.0
    for e in events:
        d = e.get("delta") or {}
        t = d.get("type")
        amt = float(d.get("usdcValue", d.get("amount", 0)) or 0)
        if amt == 0:
            continue
        if t == "send":
            src = d.get("sourceDex", "") or ""
            dst = d.get("destinationDex", "") or ""
            sender = (d.get("user") or "").lower()
            receiver = (d.get("destination") or "").lower()
            src_in, dst_in = src in in_pool, dst in in_pool
            if sender == receiver == user_lc:
                if dst_in and not src_in:
                    net += amt
                elif src_in and not dst_in:
                    net -= amt
            elif sender == user_lc and src_in:
                net -= amt
            elif receiver == user_lc and dst_in:
                net += amt
        elif t in ("deposit", "vaultWithdraw"):
            net += amt
        elif t in ("withdraw", "vaultDeposit"):
            net -= amt
        elif t in ("internalTransfer", "subAccountTransfer"):
            sender = (d.get("user") or "").lower()
            net += -amt if sender == user_lc else amt
    return net


# ── WebSocket mids (real-time, low latency) ───────────────────────────
# The persistent WebSocket connection gives sub-second latency for all 500+
# market prices. It's used by the autonomous scanning loop for real-time data.

_ws_mids_instance: "HyperliquidWebSocket | None" = None
_ws_mids_lock = threading.Lock()


def _get_ws_mids_instance() -> "HyperliquidWebSocket | None":
    """Return the active WebSocket mids instance, or None if not started."""
    with _ws_mids_lock:
        return _ws_mids_instance


def _try_ws_mids() -> dict[str, str] | None:
    """Try to get mids from the persistent WebSocket (non-blocking).

    Returns None immediately if WS isn't running. Caller decides whether
    to fall back to HTTP.
    """
    ws = _get_ws_mids_instance()
    if ws is None:
        return None
    mids = ws.get_all_mids()
    if mids:
        return {k: str(v) for k, v in mids.items()}
    return None


def fetch_all_mids(include_hip3: bool = False) -> dict[str, str]:
    """Get all mid prices.

    For one-shot commands: uses HTTP POST (reliable, fast).
    For the autonomous loop: use start_ws_mids() to keep a persistent
    WebSocket running, then call ws.get_all_mids() for sub-second data.

    Args:
        include_hip3: when True, also fetches `allMids` for each HIP-3 perpDex
            (one HTTP POST per dex, sequential) and merges results in. Adds
            ~8 small POSTs per call — only enable from contexts that need
            tokenized-equity / commodity prices.
    """
    ws_result = _try_ws_mids()
    if ws_result and not include_hip3:
        # WebSocket only carries the native HL perp mids; if HIP-3 is needed
        # we fall through to per-dex HTTP fetches below.
        return ws_result

    # Native perp + spot mids (one HTTP POST).
    raw = _http_post("/info", {"type": "allMids"})
    out: dict[str, str] = {}
    if raw and isinstance(raw, dict):
        out = {k: str(v) for k, v in raw.items()}
    elif ws_result:
        out = dict(ws_result)

    if include_hip3:
        # HIP-3 mids live behind a `dex` parameter. Walk the registered dex list
        # and merge — one POST per dex (~8 total, weight ~2 each).
        from hermes_trader.client.universe import list_hip3_dexes
        for dex in list_hip3_dexes():
            r = _http_post("/info", {"type": "allMids", "dex": dex})
            if r and isinstance(r, dict):
                for k, v in r.items():
                    out[k] = str(v)
    return out


def start_ws_mids() -> "HyperliquidWebSocket | None":
    """Start the persistent WebSocket for real-time mids (call once at startup)."""
    global _ws_mids_instance
    with _ws_mids_lock:
        if _ws_mids_instance is None:
            try:
                from hermes_trader.client.ws_client import HyperliquidWebSocket
                ws = HyperliquidWebSocket()
                ws.start()
                _ws_mids_instance = ws
                logger.info("[hl] WebSocket started for real-time mids")
            except Exception as e:
                logger.warning(f"[hl] WebSocket init failed: {e}")
                return None
        return _ws_mids_instance


def stop_ws_mids() -> None:
    """Stop the persistent WebSocket. Call when exiting the scanning loop."""
    global _ws_mids_instance
    with _ws_mids_lock:
        if _ws_mids_instance is not None:
            _ws_mids_instance.stop(timeout=2.0)
            _ws_mids_instance = None
            logger.info("[hl] WebSocket stopped")


# Phase 1 (WS user-fills feasibility): the ``userFills`` channel pushes a
# frame every time the wallet's orders match on the exchange. Phase 1 is
# LOG-ONLY — the callback in ws_client.py records a one-liner per fill
# and bumps a counter; no exit decisions are driven off it. The trading
# loop calls ``start_ws_user_fills(resolve_user_address())`` right after
# ``start_ws_mids()`` so both subscriptions share the same WS connection.
# If the subscription fails (SDK auth change, 429 on meta, etc.) the
# trading loop is unaffected — it falls back to the existing REST-based
# exit polling in monitor_exits.
def start_ws_user_fills(user: str) -> bool:
    """Start the ``userFills`` subscription on the persistent WebSocket.

    Must be called AFTER ``start_ws_mids()`` — it reuses the same WS
    connection (one socket, two subscriptions: allMids + userFills).
    Returns True on success, False on failure (non-fatal: trading loop
    continues on REST-based exit polling).

    Phase 1 boundary: the callback is LOG-ONLY. Phase 2 will route
    fills into a queue drained by the main loop.
    """
    if not user:
        logger.warning("[hl] start_ws_user_fills skipped — empty user")
        return False
    with _ws_mids_lock:
        if _ws_mids_instance is None:
            # start_ws_mids() wasn't called or failed — user-fills can't
            # ride a connection that doesn't exist. Non-fatal: the
            # trading loop's REST exit polling still backs us up.
            logger.warning(
                "[hl] start_ws_user_fills skipped — no WS mids instance "
                "(start_ws_mids failed or not called)"
            )
            return False
        try:
            return _ws_mids_instance.subscribe_user_fills(user)
        except Exception as e:
            logger.warning(
                f"[hl] start_ws_user_fills failed (non-fatal): {e}"
            )
            return False


def stop_ws_user_fills() -> None:
    """Clear the ``userFills`` subscription state.

    Called by the trading loop on shutdown BEFORE ``stop_ws_mids()``
    so the diagnostic log line ("clearing subscription state") is
    visible while the connection is still alive. The SDK's own
    subscription registry is torn down by ``stop_ws_mids``.
    """
    with _ws_mids_lock:
        if _ws_mids_instance is None:
            return
        try:
            _ws_mids_instance.unsubscribe_user_fills()
        except Exception as e:
            logger.warning(f"[hl] stop_ws_user_fills failed (non-fatal): {e}")


def drain_ws_user_fills() -> list[dict[str, Any]]:
    """Drain queued ``userFills`` events (Phase 2).

    Returns a list of normalized fill dicts (see
    ``HyperliquidWebSocket.drain_user_fills`` for the shape) in FIFO
    order. Empty list when no fills queued or WS not running.

    Called from the main trading loop (once per cycle) and from the
    intra-cycle ``_exit_checkpoint`` so a CLOSE fill is reported to the
    dashboard at batch granularity. The drain does NOT trigger an exit
    scan — exit re-evaluation stays with ``monitor_exits`` and the
    ``_exit_checkpoint``. Non-blocking so the loop never stalls.
    """
    with _ws_mids_lock:
        if _ws_mids_instance is None:
            return []
        try:
            return _ws_mids_instance.drain_user_fills()
        except Exception as e:
            logger.warning(
                f"[hl] drain_ws_user_fills failed (non-fatal): {e}"
            )
            return []


def ws_feed_diag() -> dict[str, Any] | None:
    """Return the persistent WS diagnostic snapshot, or None if not started.

    Thin read-only accessor for the trading loop / metrics / ws_status path.
    Never raises — a torn-down WS mid-flight reads as ``None`` (no feed).
    """
    ws = _get_ws_mids_instance()
    if ws is None:
        return None
    try:
        return ws.get_diag()
    except Exception as e:
        logger.warning(f"[hl] ws_feed_diag failed (non-fatal): {e}")
        return None


def ws_feed_age_seconds() -> float | None:
    """Age of the newest allMids frame, or None when the WS is not running."""
    diag = ws_feed_diag()
    if diag is None:
        return None
    try:
        return float(diag.get("data_age_s", 0.0))
    except (TypeError, ValueError):
        return None


def wait_for_ws_user_fills(timeout: float) -> bool:
    """Block up to ``timeout`` seconds until a userFill is enqueued.

    Wakes the trading loop's between-cycle / between-batch sleep the moment a
    fill frame arrives on the WS thread, while the fill itself is still
    drained and reported on the MAIN thread (single-writer rule: the WS
    callback thread never touches the session log / SSE). Returns True when
    woken early, False on timeout or when the WS is not running.
    """
    ws = _get_ws_mids_instance()
    if ws is None:
        time.sleep(max(0.0, timeout))
        return False
    try:
        return bool(ws.wait_for_fills(timeout))
    except Exception as e:
        logger.warning(f"[hl] wait_for_ws_user_fills failed (non-fatal): {e}")
        time.sleep(max(0.0, timeout))
        return False


def fetch_funding_history(
    coin: str, start_time: int, end_time: Optional[int] = None,
) -> list[dict[str, Any]]:
    """Fetch funding rate history.

    Funding rates update hourly on HL, so the latest rate is cacheable for a
    few minutes without staleness risk. Caching avoids a duplicate weight-20
    POST per research cycle when ta_filter and research both request it.
    """
    if end_time is None:
        end_time = int(time.time() * 1000)

    # Short-TTL cache for the most recent funding rate (5 min = 1/12 of the
    # hourly funding interval — more than fresh enough for prompt context).
    _fkey = f"funding|{coin}"
    if _FUNDING_CACHE is not None:
        hit = _FUNDING_CACHE.get(_fkey)
        if hit is not None:
            return hit

    payload = {"type": "fundingHistory", "coin": coin, "startTime": start_time, "endTime": end_time}
    raw = _http_post("/info", payload)
    result = raw if isinstance(raw, list) else []
    if result and _FUNDING_CACHE is not None:
        _FUNDING_CACHE.set(_fkey, result)
    return result
