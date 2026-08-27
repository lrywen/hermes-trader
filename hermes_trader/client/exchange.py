"""Hyperliquid exchange client — order placement using official SDK.

This module uses the hyperliquid-python-sdk for all authenticated operations:
- ECDSA signing (handled internally by the SDK)
- Order placement, trigger orders, leverage updates, cancel orders
- ATR calculation on HL candles

The SDK handles:
- msgpack encoding of actions
- ECDSA secp256k1 signing with Agent typed-data domain
- Keccak256 hashing for connection IDs
- EIP-712 typed data for Exchange domain signing
"""

from __future__ import annotations

import logging
import math
import os
import threading
import time as _time
from typing import Any, Dict, Optional, Tuple

# Hyperliquid rejects any order below $10 notional. Target a small buffer above
# it so the IOC price offset and mark-vs-limit rounding can't dip under.
MIN_ORDER_USD = 10.5

from eth_account import Account
from hyperliquid.api import API
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils.signing import (
    OrderType,
    TriggerOrderType,
)
from hyperliquid.utils.types import Cloid
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ── SDK retry patching ────────────────────────────────────────────────────────
# The testnet API (api.hyperliquid-testnet.xyz) intermittently drops SSL
# connections (SSLEOFError) and returns 429 under burst load. The SDK's
# API base class creates a bare requests.Session with zero retries and
# calls self.session.post() directly. We fix this in two layers:
#
# 1. Patch API.__init__ to mount a urllib3 Retry adapter immediately after
#    session creation — before Info.__init__ fires meta()/spotMeta().
# 2. Wrap API.post to catch SSLEOFError / ConnectionError and retry with
#    exponential backoff, since urllib3 Retry does NOT classify SSLEOFError
#    as retryable by default.

_MAX_POST_RETRIES = 5
_POST_BACKOFF = 2.0  # seconds, doubles each attempt

# Per-request timeout for the official SDK's HTTP calls. The SDK defaults to
# timeout=None (infinite wait) — a silent TCP hang would block the trading
# loop forever and only be unblocked by the 600s watchdog re-exec. Bound it.
# 30s is generous for HL's typical <1s responses but bounds a hung socket
# before the watchdog fires. Read/connect share the same value (requests
# accepts a float for total timeout).
_SDK_TIMEOUT = float(os.environ.get("HERMES_HL_SDK_TIMEOUT_S", "30"))


def _mount_retry_adapter(session) -> None:
    retry = Retry(
        total=5,
        connect=5,
        read=5,
        backoff_factor=2.0,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)


_orig_api_init = API.__init__


def _patched_api_init(self, base_url=None, timeout=None):
    _orig_api_init(self, base_url, timeout)
    _mount_retry_adapter(self.session)


API.__init__ = _patched_api_init

_orig_api_post = API.post


def _patched_api_post(self, url_path, payload=None):
    payload = payload or {}
    last_err = None
    for attempt in range(_MAX_POST_RETRIES):
        try:
            return _orig_api_post(self, url_path, payload)
        except Exception as e:
            last_err = e
            err_name = type(e).__name__
            is_retriable = any(
                s in err_name
                for s in ("SSLEOFError", "SSLError", "ConnectionError",
                           "ConnectTimeout", "ReadTimeout", "ProxyError")
            )
            if not is_retriable or attempt == _MAX_POST_RETRIES - 1:
                raise
            delay = _POST_BACKOFF * (2 ** attempt)
            logging.getLogger(__name__).warning(
                "API.post %s failed (%s), retry %d/%d in %.1fs",
                url_path, err_name, attempt + 1, _MAX_POST_RETRIES - 1, delay,
            )
            _time.sleep(delay)
    raise last_err  # type: ignore[misc]


API.post = _patched_api_post

from hermes_trader.client.hl_client import (
    HL_API,
    _http_post,
    fetch_hl_candles,
    resolve_user_address,
)

logger = logging.getLogger(__name__)

# ── Environment ────────────────────────────────────────────────────────────────

HL_WALLET = os.environ.get("HYPERLIQUID_WALLET_ADDRESS", "")
HL_MASTER = os.environ.get("HYPERLIQUID_MASTER_ADDRESS", "")
PRIVATE_KEY_HEX = os.environ.get("HYPERLIQUID_PRIVATE_KEY", "")

# Unified account: MASTER holds funds, WALLET signs orders
IS_AGENT = bool(HL_MASTER and HL_WALLET and HL_MASTER.lower() != HL_WALLET.lower())
HL_ACCOUNT = HL_MASTER if IS_AGENT else HL_WALLET

# Default leverage used only as a fallback when .agent-config.json omits
# `leverage` (the executor/sizing always prefer the live config value,
# currently 12x in production). Env-overridable so a deploy can change the
# fallback without a code edit; kept at 5x for backward compatibility.
HL_LEVERAGE = int(os.environ.get("HERMES_DEFAULT_LEVERAGE", "5"))  # cross margin

# Slippage cap for IOC marketable-limit orders. The 1% headroom past the L2
# touch is meant to absorb in-flight price drift, NOT to accept a fill at any
# price. If the computed IOC limit deviates from mid by more than this, the
# book has moved too far (or L2 is stale/malformed) → reject rather than lift
# a wide offer. Closes (reduce_only) get a relaxed cap because an emergency
# flatten must be allowed to escape; set HERMES_MAX_SLIPPAGE_CLOSE_PCT=0 to
# disable the escape hatch. Both values are in percent.
_MAX_SLIPPAGE_PCT = float(os.environ.get("HERMES_MAX_SLIPPAGE_PCT", "1.5"))
_MAX_SLIPPAGE_CLOSE_PCT = float(os.environ.get("HERMES_MAX_SLIPPAGE_CLOSE_PCT", "5.0"))


_exchange_instance = None  # Singleton instance

def _resolve_perp_dexs() -> Optional[list]:
    """Discover HIP-3 perpDex names so the SDK can resolve colon-namespaced coins.

    Only invoked when HIP-3 is enabled via .agent-config.json `enable_hip3`.
    Returns None when disabled so the SDK uses its default (main perp dex only).

    CRITICAL: the SDK treats `perp_dexs` as *exclusive* — if you pass a list,
    it loads ONLY those dexes and drops the main perp universe. The empty
    string `""` is the sentinel for the main dex. So we must prepend `""` to
    keep BTC/ETH/etc. resolvable alongside the HIP-3 namespaced coins.
    """
    try:
        from hermes_trader.agents.config_store import read_agent_config
        if not read_agent_config().get("enable_hip3"):
            return None
        from hermes_trader.client.universe import list_hip3_dexes
        hip3 = list_hip3_dexes()
        return [""] + hip3 if hip3 else None
    except Exception as e:
        logger.warning(f"[_resolve_perp_dexs] HIP-3 dex discovery failed: {e}")
        return None


def _is_backtest_process() -> bool:
    """Detect whether the current process is a backtest run (P3-17).

    Two independent signals, either of which is sufficient:
      1. ``HERMES_BACKTEST=1`` env var, set explicitly by backtest launchers.
      2. ``sys.argv[0]`` basename contains "backtest" (covers scripts/backtest*.py).

    Testnet runs (``HYPERLIQUID_TESTNET`` set) are NOT treated as backtest.
    """
    if os.environ.get("HERMES_BACKTEST", "").strip().lower() in ("1", "true", "yes"):
        return True
    try:
        import sys
        argv0 = os.path.basename(sys.argv[0] or "").lower()
        if "backtest" in argv0:
            return True
    except Exception:
        pass
    return False


def _make_exchange() -> Exchange:
    """Create or reuse Exchange client singleton (avoids WebSocket connection limit)."""
    global _exchange_instance
    if _exchange_instance is not None:
        return _exchange_instance

    # P3-17: backtest processes must NEVER construct a signing client against
    # a live mainnet wallet. A backtest that imports this module must either
    # run against testnet or (preferably) not place orders at all. Failing
    # loudly here prevents a real private key from being loaded into a
    # simulation process where it could be misused or leaked.
    if PRIVATE_KEY_HEX and _is_backtest_process():
        testnet = os.environ.get("HYPERLIQUID_TESTNET", "").strip().lower() in ("1", "true", "yes")
        if not testnet:
            raise RuntimeError(
                "Refusing to load live HYPERLIQUID_PRIVATE_KEY in a backtest process "
                "(detected via HERMES_BACKTEST=1 or argv 'backtest'). "
                "Unset the private key, set HYPERLIQUID_TESTNET=1, or run outside backtest."
            )

    if not PRIVATE_KEY_HEX:
        raise RuntimeError("HYPERLIQUID_PRIVATE_KEY not set")

    key_hex = PRIVATE_KEY_HEX
    if key_hex.startswith("0x"):
        key_hex = key_hex[2:]

    # The SDK uses eth_account for signing
    acct = Account.from_key(key_hex)

    # For unified accounts with agent wallet:
    # - WALLET signs orders
    # - MASTER holds funds
    account_address = HL_WALLET if IS_AGENT else None

    # perp_dexs= teaches the SDK the HIP-3 dex list at init so name_to_asset
    # can resolve `xyz:NVDA` etc. when enable_hip3 is on. With it None the
    # SDK behaves exactly as before.
    last_err = None
    for attempt in range(5):
        try:
            _exchange_instance = Exchange(
                wallet=acct,
                base_url=HL_API,
                account_address=account_address,
                perp_dexs=_resolve_perp_dexs(),
                timeout=_SDK_TIMEOUT,
            )
            break
        except Exception as e:
            last_err = e
            if attempt < 4:
                _time.sleep(2.0 * (2 ** attempt))
    if _exchange_instance is None:
        raise last_err  # type: ignore[misc]
    return _exchange_instance


_info_instance: Optional[Info] = None


def _get_info() -> Info:
    """Get (or lazily create) the shared HTTP-only Info client.

    skip_ws=True: the callers only use REST methods (meta, all_mids, l2,
    fills, ...), so no WebSocket connection is opened.

    Retries construction up to 3 times because the SDK's Info() constructor
    fires meta() + spotMeta() immediately, and on testnet the SSL handshake
    intermittently fails with SSLEOFError.
    """
    global _info_instance
    if _info_instance is None:
        last_err = None
        for attempt in range(5):
            try:
                _info_instance = Info(skip_ws=True, base_url=HL_API,
                                     perp_dexs=_resolve_perp_dexs(),
                                     timeout=_SDK_TIMEOUT)
                break
            except Exception as e:
                last_err = e
                if attempt < 4:
                    # Exponential backoff: 2s, 4s, 8s, 16s (handles 429 + SSL flakiness)
                    _time.sleep(2.0 * (2 ** attempt))
        if _info_instance is None:
            raise last_err  # type: ignore[misc]
    return _info_instance


# Per-dex meta cache. szDecimals / pxDecimals / maxLeverage are static, but
# get_coin_index / get_max_leverage used to call info.meta(dex=...) on EVERY
# order + trigger + leverage-set. Under burst load (several executes in one
# cycle) those uncached HTTP calls 429'd, get_coin_index fell through to
# "Unknown coin: xyz:SMSN", and the HIP-3 backup stop-loss silently failed.
# Cache the meta universe per dex so a transient 429 can't break coin
# resolution and we stop hammering the API. TTL is long — meta rarely changes.
_META_CACHE: Dict[str, Tuple[float, list]] = {}
_META_TTL_S = float(os.environ.get("HERMES_META_TTL_S", "3600"))
# H11: the meta cache is process-local (lost on watchdog os.execv). On a cold
# restart the first scan fires N concurrent get_coin_index calls across the
# ThreadPoolExecutor; without a lock they all miss simultaneously and stampede
# info.meta(), provoking the restart-time 429 storm that breaks HIP-3 coin
# resolution. Serialize cache fills so only ONE in-flight meta() per dex and
# the rest wait for its result.
_META_CACHE_LOCK = threading.Lock()


def _cached_universe(dex: Optional[str] = None) -> list:
    """Return the meta `universe` for a dex (None = main), cached for _META_TTL_S.

    On a fetch failure we serve a stale cached copy if we have one, rather than
    raising — a transient API blip must not break coin resolution mid-execute.
    """
    key = dex or ""
    hit = _META_CACHE.get(key)
    now = _time.time()
    if hit and (now - hit[0]) < _META_TTL_S:
        return hit[1]
    # H11: serialize the cold fetch (see _META_CACHE_LOCK). Double-check the
    # cache under the lock so concurrent threads coalesce onto the first
    # thread's meta() result instead of stampeding the API.
    with _META_CACHE_LOCK:
        hit = _META_CACHE.get(key)
        now = _time.time()
        if hit and (now - hit[0]) < _META_TTL_S:
            return hit[1]
        info = _get_info()
        try:
            meta = info.meta(dex=dex) if dex else info.meta()
            universe = meta.get("universe", []) or []
            if universe:
                _META_CACHE[key] = (now, universe)
            return universe
        except Exception as e:
            if hit:  # serve stale rather than fail the lookup
                logger.warning(f"[_cached_universe] meta fetch failed for dex={dex!r}; serving stale: {e}")
                return hit[1]
            raise


def prewarm_meta_cache() -> int:
    """Populate the per-dex meta cache at startup, BEFORE the first scan/execute
    burst hammers the API. Without this the cache is cold on restart, so the
    restart-time 429 storm makes get_coin_index/get_max_leverage fall through to
    'Unknown coin' (killing the HIP-3 backup stop-loss) and candle fetches
    return empty. Best-effort: a dex that 429s now will be retried lazily later.
    Returns the number of dex universes successfully cached.
    """
    dexes: list = [None]  # main perp dex
    try:
        for d in (_resolve_perp_dexs() or []):
            if d and d not in dexes:
                dexes.append(d)
    except Exception:
        pass
    warmed = 0
    for d in dexes:
        try:
            if _cached_universe(dex=d):
                warmed += 1
        except Exception as e:
            logger.warning(f"[prewarm_meta_cache] dex={d!r} failed (will retry lazily): {e}")
    logger.info(f"[prewarm_meta_cache] warmed {warmed}/{len(dexes)} dex meta universes")
    return warmed


def get_coin_index(coin: str) -> Tuple[int, int, int]:
    """Resolve a coin name to (asset_index, sz_decimals, px_decimals) via the SDK meta endpoint.

    Searches the main perp universe first, then (for HIP-3 colon-namespaced
    coins like `xyz:NVDA`) the parent dex's meta. The returned index is the
    coin's position *within its own universe*; downstream callers (the SDK
    `order`/`update_leverage` etc.) translate name → asset ID internally using
    `perp_dexs`, so this helper is only used for sz/px decimals in our own
    rounding code.
    """
    for i, u in enumerate(_cached_universe()):
        if u["name"] == coin:
            return i, u.get("szDecimals", 5), u.get("pxDecimals", 4)
    if ":" in coin:
        dex = coin.split(":", 1)[0]
        try:
            for i, u in enumerate(_cached_universe(dex=dex)):
                if u["name"] == coin:
                    return i, u.get("szDecimals", 5), u.get("pxDecimals", 4)
        except Exception as e:
            logger.warning(f"[get_coin_index] HIP-3 meta lookup failed for dex={dex}: {e}")
    raise ValueError(f"Unknown coin: {coin}")


def get_max_leverage(coin: str) -> int:
    """The coin's maximum allowed leverage, from the SDK meta endpoint.

    Hyperliquid sets this per coin (e.g. BOME 3x, ONDO 10x, BTC 40x); an order
    that tries to exceed it is rejected, so callers cap their leverage here.
    HIP-3 namespaced coins (e.g. `xyz:NVDA`) are looked up in the parent dex's
    metadata when not found in the main perp universe.
    """
    # Main perp dex
    for u in _cached_universe():
        if u["name"] == coin:
            return int(u.get("maxLeverage", 1))
    # HIP-3: derive dex name from the namespace prefix and consult that dex's meta
    if ":" in coin:
        dex = coin.split(":", 1)[0]
        try:
            for u in _cached_universe(dex=dex):
                if u["name"] == coin:
                    return int(u.get("maxLeverage", 1))
        except Exception as e:
            logger.warning(f"[get_max_leverage] HIP-3 meta lookup failed for dex={dex}: {e}")
    raise ValueError(f"Unknown coin: {coin}")


# ── Market data ────────────────────────────────────────────────────────────────

def get_hl_price(coin: str = "BTC") -> float:
    """Get the current mid price for a coin.

    HIP-3 namespaced coins (e.g. `xyz:NVDA`) live on a separate perpDex and
    aren't returned by the bare `all_mids()` call — that endpoint only
    covers the main HL perp universe. For colon-namespaced names we derive
    the dex from the prefix and call `all_mids(dex=...)`. Without this fix
    the executor was silently aborting HIP-3 trades at
    `if mid_price <= 0: return invalid_price`.
    """
    info = _get_info()
    if ":" in coin:
        dex = coin.split(":", 1)[0]
        try:
            mids = info.all_mids(dex=dex)
            v = mids.get(coin)
            if v is not None:
                return float(v)
        except Exception as e:
            logger.warning(f"[get_hl_price] HIP-3 all_mids failed for dex={dex}: {e}")
        return 0.0
    mids = info.all_mids()
    return float(mids.get(coin, "0"))


def get_all_hl_mids(include_hip3: bool = False) -> Dict[str, float]:
    """Return {coin: mid_price} for every perp — one HTTP call for the whole universe.

    When `include_hip3=True`, also queries each registered HIP-3 perpDex
    (~8 extra POSTs, weight ~2 each) and merges colon-namespaced mids
    (`xyz:MU`, `vntl:NVDA`, ...) into the result. Without this, the DSL
    exit pass receives no mid for any HIP-3 position, every tracker's
    `advance()` short-circuits, peak/floor never update, and the
    dashboard shows "no DSL" indefinitely for those positions.
    """
    info = _get_info()
    raw = info.all_mids() or {}
    # Freshness assertion (Phase-0 hardening): an empty main all_mids() is a
    # degraded read, not an empty market — every perp has a mid. Silently
    # returning {} freezes every DSL tracker (advance() short-circuits on a
    # missing mid) with no trace. Flag it loudly.
    if not raw:
        logger.warning(
            "[get_all_hl_mids] FEED-FRESHNESS: main all_mids() returned EMPTY "
            "(degraded read) — DSL trackers will get no price this pass")
    out: Dict[str, float] = {}
    for k, v in raw.items():
        try:
            out[k] = float(v)
        except (TypeError, ValueError):
            continue
    if include_hip3:
        try:
            from hermes_trader.client.universe import list_hip3_dexes
            _hip3_dropped = 0
            for dex in list_hip3_dexes():
                try:
                    dex_mids = info.all_mids(dex=dex) or {}
                except Exception as e:
                    _hip3_dropped += 1
                    logger.warning(f"[get_all_hl_mids] HIP-3 all_mids failed for dex={dex}: {e}")
                    continue
                for k, v in dex_mids.items():
                    try:
                        out[k] = float(v)
                    except (TypeError, ValueError):
                        continue
            if _hip3_dropped:
                logger.warning(
                    f"[get_all_hl_mids] FEED-FRESHNESS: {_hip3_dropped} HIP-3 dex(es) "
                    f"dropped from mids this pass — their positions' DSL trackers get "
                    f"no price (peak/floor frozen until next good read)")
        except Exception as e:
            logger.warning(f"[get_all_hl_mids] HIP-3 dex enumeration failed: {e}")
    return out


# ── Order placement ────────────────────────────────────────────────────────────

def _is_isolated_only(coin: str) -> bool:
    """True if the market only supports isolated margin (no cross-margin).

    Most HIP-3 markets are isolated-only (85% of xyz, 100% of vntl, 87% of km,
    etc.) and a small subset of native HL perps too. Calling
    `update_leverage(is_cross=True)` on these silently fails on the exchange,
    after which the next `order()` is rejected with "Insufficient margin to
    place order" — even when the wallet has plenty of free equity, because no
    isolated-margin position was actually opened first.

    Reads the per-market meta (main dex or the namespace-prefix HIP-3 dex)
    and returns the `onlyIsolated` flag. Defaults to False (cross) if lookup
    fails so native crypto behavior is preserved.
    """
    info = _get_info()
    try:
        for m in info.meta().get("universe", []):
            if m["name"] == coin:
                return bool(m.get("onlyIsolated", False))
        if ":" in coin:
            dex = coin.split(":", 1)[0]
            for m in info.meta(dex=dex).get("universe", []):
                if m["name"] == coin:
                    return bool(m.get("onlyIsolated", False))
    except Exception as e:
        logger.warning(f"[_is_isolated_only] meta lookup failed for {coin}: {e}")
    return False


def set_leverage(coin: str, leverage: int) -> Dict[str, Any]:
    """Set leverage for a coin, choosing cross vs isolated based on the market.

    For markets flagged `onlyIsolated: true` (most HIP-3 + ~3% of native HL),
    we send `is_cross=False`. Without this branch the leverage call no-ops
    on isolated-only markets and the order that follows is rejected by HL
    with "Insufficient margin to place order" despite plenty of free margin.

    No-op when no private key is set.
    """
    if not PRIVATE_KEY_HEX:
        return {"ok": False, "error": "no private key"}

    is_cross = not _is_isolated_only(coin)
    try:
        exchange = _make_exchange()
        # SDK: update_leverage(leverage, coin, is_cross). is_cross=False for
        # markets where the dex rejects cross-margin (HIP-3 majority).
        result = exchange.update_leverage(leverage, coin, is_cross=is_cross)
        return {"ok": True, "result": result, "is_cross": is_cross}
    except Exception as e:
        logger.error(f"Failed to set leverage for {coin} (is_cross={is_cross}): {e}")
        return {"ok": False, "error": str(e), "is_cross": is_cross}


def _round_price_for_hl(price: float, sz_decimals: int, is_perp: bool = True,
                        is_buy: Optional[bool] = None) -> str:
    """Round a price to satisfy Hyperliquid's two constraints:

    1. Multiple of the tick size: tick = 10^(-(MAX_DECIMALS - sz_decimals))
       where MAX_DECIMALS = 6 for perps, 8 for spot.
    2. At most 5 significant figures total.

    `is_buy` controls rounding direction so an IOC limit always rounds in
    the direction of MORE aggression (preserves cross-the-book intent):
      * BUY → ROUND_CEILING (round UP; limit stays at/above ask)
      * SELL → ROUND_FLOOR (round DOWN; limit stays at/below bid)
    Without this, a SELL price like 0.00016533 (1% below bid 0.000167)
    half-up-rounds to 0.00017, which is ABOVE the bid → IOC can never
    match → "could not immediately match" rejection. Passing None falls
    back to ROUND_HALF_UP for non-order callsites.
    """
    from decimal import Decimal, ROUND_HALF_UP, ROUND_CEILING, ROUND_FLOOR, getcontext
    getcontext().prec = 28

    if price <= 0:
        return "0"

    MAX_DECIMALS = 6 if is_perp else 8
    px_decimals_by_tick = max(0, MAX_DECIMALS - int(sz_decimals))

    int_digits = max(0, int(math.floor(math.log10(price))) + 1)
    px_decimals_by_sigfig = max(0, 5 - int_digits)
    px_decimals = min(px_decimals_by_tick, px_decimals_by_sigfig)

    if is_buy is True:
        rounding_mode = ROUND_CEILING
    elif is_buy is False:
        rounding_mode = ROUND_FLOOR
    else:
        rounding_mode = ROUND_HALF_UP

    q = Decimal(10) ** -px_decimals if px_decimals > 0 else Decimal(1)
    rounded = (Decimal(str(price)) / q).quantize(Decimal('1'), rounding=rounding_mode) * q
    if px_decimals > 0:
        return f"{rounded:.{px_decimals}f}"
    return f"{rounded:.0f}"


def _parse_order_result(result: Any, accept_resting: bool = False) -> Dict[str, Any]:
    """Normalize a raw SDK order response into {ok, order_id?, avg_px?, total_sz?, error?}.

    For `filled` statuses we extract avgPx and totalSz too — downstream uses
    these to compute realized PnL from the actual fill price rather than the
    pre-trade mid (the two differ by spread + slippage, which compounds at
    leverage).

    NOTE: For trigger orders the exchange may accept the order at placement
    (returning `resting`) then ASYNCHRONOUSLY reject it when the trigger fires
    (e.g. "minTradeNtlRejected"). This parser only sees the placement-time
    response; post-trigger rejections must be caught by polling historicalOrders.
    An empty `statuses` array on an otherwise-ok response is treated as a
    failure here — it indicates the SDK did not confirm the order.
    """
    if not (isinstance(result, dict) and result.get("status") == "ok"):
        logger.error(f"[_parse_order_result] exchange returned non-ok: {result}")
        return {"ok": False, "error": str(result)}
    statuses = result.get("response", {}).get("data", {}).get("statuses", [])
    if not statuses:
        # Defensive: an ok envelope with no statuses means the SDK did not
        # return an order confirmation. Previously this silently returned
        # {"ok": True} which masked placement failures (e.g. BCH TP 2026-08-21
        # was accepted as resting then rejected on trigger — a separate
        # failure mode, but the empty-statuses path should never lie).
        logger.error(
            f"[_parse_order_result] ok envelope with EMPTY statuses — order "
            f"NOT confirmed by exchange. full_response={result}"
        )
        return {"ok": False, "error": "no order status in exchange response"}
    st = statuses[0]
    if accept_resting and st.get("resting"):
        oid = str(st["resting"]["oid"])
        logger.info(
            f"[_parse_order_result] order RESTING on exchange: oid={oid} "
            f"(trigger order accepted, awaiting price)"
        )
        return {"ok": True, "order_id": oid}
    if st.get("filled"):
        f = st["filled"]
        oid = str(f.get("oid", ""))
        out: Dict[str, Any] = {"ok": True, "order_id": oid}
        try:
            if "avgPx" in f:
                out["avg_px"] = float(f["avgPx"])
            if "totalSz" in f:
                out["total_sz"] = float(f["totalSz"])
            # (H-7) The SDK's filled payload carries the fill time in ms —
            # surface it so the DSL tracker can use the REAL entry time
            # (hard_timeout / stale_flat timers key off it) instead of the
            # signal time.
            if "time" in f:
                out["filled_at_ms"] = int(f["time"])
        except (TypeError, ValueError):
            pass
        logger.info(
            f"[_parse_order_result] order FILLED: oid={oid}, "
            f"avg_px={out.get('avg_px')}, total_sz={out.get('total_sz')}"
        )
        return out
    if st.get("error"):
        err = st["error"]
        logger.error(
            f"[_parse_order_result] exchange REJECTED order: error={err}, "
            f"full_status={st}"
        )
        return {"ok": False, "error": err}
    # Unknown status shape — don't claim success.
    logger.warning(
        f"[_parse_order_result] UNRECOGNIZED status shape — cannot confirm "
        f"order outcome: status={st}"
    )
    return {"ok": False, "error": f"unrecognized status: {st}"}


def _min_order_size(price: float, sz_decimals: int) -> float:
    """Smallest size at the coin's precision worth at least MIN_ORDER_USD.

    Rounded UP to the size tick (10^-sz_decimals): for integer-size coins
    (sz_decimals=0) a plain round-to-precision would drop a near-$10 size
    under HL's floor. e.g. MEGA at $0.084 needs ~125 coins, not 100.
    """
    tick = 10.0 ** (-sz_decimals)
    return math.ceil((MIN_ORDER_USD / price) / tick) * tick


def min_entry_notional_usd(coin: str, mid_price: float) -> float:
    """Minimum entry notional after HL size precision is applied.

    This is higher than MIN_ORDER_USD on integer-size markets because the
    smallest valid size may overshoot the dollar floor. Executors should compare
    their intended notional to this before gates so the order layer never
    silently up-sizes a trade after risk checks have passed.
    """
    if mid_price <= 0:
        return 0.0
    _, sz_dec, _ = get_coin_index(coin)
    return _min_order_size(mid_price, sz_dec) * mid_price


def entry_size_for_notional(coin: str, notional_usd: float, mid_price: float) -> float:
    """Coin size the entry order will submit for an intended dollar notional.

    Mirrors place_hl_order's size precision and minimum-order logic, without
    placing anything. Callers can feed this into risk gates and SL/TP sizing so
    their bookkeeping matches the order that will actually be sent.
    """
    if notional_usd <= 0 or mid_price <= 0:
        return 0.0
    _, sz_dec, _ = get_coin_index(coin)
    size = max(notional_usd / mid_price, _min_order_size(mid_price, sz_dec))
    return float(f"{size:.{sz_dec}f}")


def get_orderbook_spread(coin: str) -> Dict[str, Any]:
    """Fetch L2 order book and return bid/ask spread + depth info.

    Returns dict with:
      - best_bid, best_ask: top-of-book prices
      - spread_pct: (ask - bid) / mid * 100
      - bid_depth_1pct, ask_depth_1pct: cumulative notional within 1% of mid
      - ok: True if data was fetched successfully
    """
    try:
        levels = _get_info().l2_snapshot(coin).get("levels", [])
        bids_raw = levels[0] if len(levels) > 0 else []
        asks_raw = levels[1] if len(levels) > 1 else []
        if not bids_raw or not asks_raw:
            return {"ok": False, "error": "empty_orderbook"}

        best_bid = float(bids_raw[0]["px"])
        best_ask = float(asks_raw[0]["px"])
        mid = (best_bid + best_ask) / 2.0
        spread_pct = (best_ask - best_bid) / mid * 100.0 if mid > 0 else 999.0

        # Cumulative depth within 1% of mid
        bid_depth = sum(
            float(b["px"]) * float(b["sz"])
            for b in bids_raw
            if float(b["px"]) >= mid * 0.99
        )
        ask_depth = sum(
            float(a["px"]) * float(a["sz"])
            for a in asks_raw
            if float(a["px"]) <= mid * 1.01
        )

        return {
            "ok": True,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "mid": mid,
            "spread_pct": round(spread_pct, 4),
            "bid_depth_1pct_usd": round(bid_depth, 2),
            "ask_depth_1pct_usd": round(ask_depth, 2),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _ioc_cross_price(coin: str, is_buy: bool, mid_price: float) -> float:
    """Limit price for an IOC order that reliably crosses the live book.

    Anchors to the current best bid/ask from a *fresh* L2 fetch — the mid
    passed into place_hl_order is stale by the time the order is built
    (set_leverage + get_hl_atr run in between) — and steps 1% past the
    touch. An IOC fills at the resting price, so that 1% is headroom that
    absorbs price moves before the order lands, not slippage. The old
    fixed 0.1%-from-mid offset missed on moving coins ("could not
    immediately match"). Falls back to mid +/- 1% if the L2 fetch fails.
    """
    try:
        levels = _get_info().l2_snapshot(coin).get("levels", [])
        bids, asks = levels[0], levels[1]
        if is_buy and asks:
            return float(asks[0]["px"]) * 1.01
        if not is_buy and bids:
            return float(bids[0]["px"]) * 0.99
    except Exception as e:
        # Best-effort: fall back to mid ±1%. Log so a persistent l2 outage
        # is visible even though execution still succeeds.
        logger.warning(f"[aggressive_limit_px] l2_snapshot failed for {coin}: {e}")
    return mid_price * (1.01 if is_buy else 0.99)


def place_hl_order(
    is_buy: bool,
    size: float,
    mid_price: float,
    coin: str = "BTC",
    reduce_only: bool = False,
    cloid: Optional[Cloid] = None,
) -> Dict[str, Any]:
    """Place an IOC (immediate-or-cancel) limit order on Hyperliquid.

    reduce_only=True for CLOSES: the size floor below bumps small orders to HL's
    $10 minimum, so closing a sub-$10 position (e.g. an illiquid residual) would
    OVERSHOOT and flip the position to the opposite side without this flag. With
    reduce_only, HL fills only up to the live position size and rejects the rest
    → clean flatten, never a flip."""
    if not PRIVATE_KEY_HEX:
        return {"ok": False, "error": "HYPERLIQUID_PRIVATE_KEY not set"}
    if mid_price <= 0:
        return {"ok": False, "error": f"invalid price for {coin}"}

    try:
        # Always get sz_dec from get_coin_index() (px_dec is ignored — we compute it correctly)
        _, sz_dec, _ = get_coin_index(coin)

        # Price the IOC to cross the live book (best bid/ask + 1% headroom).
        price = _ioc_cross_price(coin, is_buy, mid_price)

        # H15: cap the accepted slippage vs mid. The 1% L2 headroom is drift
        # absorption, not a license to lift any offer. A wide/stale book (e.g.
        # during a flash crash or L2 outage) would otherwise produce an IOC
        # limit far from mid and fill at a predatory price. Closes get a
        # relaxed cap because an emergency flatten must still escape.
        if mid_price > 0:
            slip_pct = abs(price - mid_price) / mid_price * 100.0
            cap = _MAX_SLIPPAGE_CLOSE_PCT if reduce_only else _MAX_SLIPPAGE_PCT
            if cap > 0 and slip_pct > cap:
                logger.error(
                    f"[place_hl_order] REJECT {coin} {'BUY' if is_buy else 'SELL'} "
                    f"reduce_only={reduce_only} — IOC price {price:.6g} deviates "
                    f"{slip_pct:.2f}% from mid {mid_price:.6g} > cap {cap:.2f}%. "
                    f"Not submitting (book moved too far / L2 stale)."
                )
                return {
                    "ok": False,
                    "error": (
                        f"slippage {slip_pct:.2f}% > cap {cap:.2f}% "
                        f"(reduce_only={reduce_only})"
                    ),
                    "error_code": "slippage_exceeded",
                    "price": price,
                    "mid": mid_price,
                    "slippage_pct": round(slip_pct, 4),
                }

        # Round price honoring HL's tick + 5-sigfig rules. is_buy tells the
        # rounder which direction preserves IOC-cross aggression.
        price_str = _round_price_for_hl(price, sz_dec, is_perp=True, is_buy=is_buy)

        # Never place below HL's $10 minimum. _min_order_size rounds the floor
        # UP to the coin's size tick, so rounding to sz_dec can't drop it under.
        size = max(size, _min_order_size(mid_price, sz_dec))
        size_str = f"{size:.{sz_dec}f}"
        
        logger.info(f"[place_hl_order] price_str={price_str}, size_str={size_str}, mid={mid_price}, sz_dec={sz_dec}")
        
        exchange = _make_exchange()
        order_type = OrderType(limit={"tif": "Ioc"})
        
        logger.info(f"[place_hl_order] Calling exchange.order({coin}, {is_buy}, {float(size_str)}, {float(price_str)}, {order_type})")
        
        # SDK expects float for both size and price (signature: limit_px: float).
        # price_str was already rounded to HL's tick + sigfig rules, so float() is safe.
        # cloid is an exchange-side idempotency key: a retried order with the same
        # cloid is rejected by HL as a duplicate rather than filled twice.
        order_kwargs = {"reduce_only": reduce_only}
        if cloid is not None:
            order_kwargs["cloid"] = cloid
        result = exchange.order(
            coin,
            is_buy,
            float(size_str),
            float(price_str),
            order_type,
            **order_kwargs,
        )

        parsed = _parse_order_result(result)
        if not parsed.get("ok"):
            # Surface the HL error + the price/size we sent so future
            # "could not immediately match" failures are diagnosable.
            logger.warning(
                f"[place_hl_order] {coin} {'BUY' if is_buy else 'SELL'} "
                f"size={size_str} px={price_str} REJECTED: {parsed.get('error')}"
            )
        return parsed
    except Exception as e:
        logger.error(f"Failed to place order for {coin}: {e}")
        return {"ok": False, "error": str(e)}


def place_hl_trigger_order(
    is_long_position: bool,
    size: float,
    trigger_px: float,
    kind: str,  # 'sl' or 'tp'
    coin: str = "BTC",
) -> Dict[str, Any]:
    """Place a reduce-only trigger order (stop-loss or take-profit).

    Triggers a market order in the position-closing direction once the
    trigger price is crossed.
    """
    if not PRIVATE_KEY_HEX:
        return {"ok": False, "error": "HYPERLIQUID_PRIVATE_KEY not set"}
    if size <= 0 or trigger_px <= 0:
        return {"ok": False, "error": "invalid size/price"}

    try:
        _, sz_dec, _ = get_coin_index(coin)

        exchange = _make_exchange()

        # Trigger order closes the position: opposite direction, reduce-only.
        # For a long position: sell trigger (is_buy=False).
        # For a short position: buy trigger (is_buy=True).
        is_buy = not is_long_position

        trigger_str = _round_price_for_hl(trigger_px, sz_dec, is_perp=True)
        trigger_f = float(trigger_str)
        size_str = f"{size:.{sz_dec}f}"
        order_notional = float(size_str) * trigger_f

        # H1: a resting trigger below the HL minimum is accepted at submit time
        # but ASYNCHRONOUSLY REJECTED ("minTradeNtlRejected") the moment it
        # fires — leaving the position with no stop/tp and no signal to us.
        # Refuse to submit it; callers (executor TP/SL sizing) already upsize
        # or skip, so this is defense-in-depth against any path that slips a
        # sub-min order through. Set HERMES_ENFORCE_TRIGGER_MIN=0 to revert to
        # the old log-and-submit behavior.
        if order_notional < MIN_ORDER_USD and os.environ.get(
            "HERMES_ENFORCE_TRIGGER_MIN", "1"
        ) not in ("0", "false", "False"):
            logger.error(
                f"[place_hl_trigger_order] REJECT {coin} kind={kind} "
                f"size={size_str} trigger={trigger_str} notional=${order_notional:.2f} "
                f"< HL min ${MIN_ORDER_USD:.2f} — not submitting (would be "
                f"asynchronously rejected on trigger, leaving position unprotected)"
            )
            return {
                "ok": False,
                "error": f"trigger_notional_below_min: ${order_notional:.2f} < ${MIN_ORDER_USD:.2f}",
                "error_code": "trigger_notional_below_min",
            }

        order_type = OrderType(
            trigger=TriggerOrderType(
                triggerPx=trigger_f,
                isMarket=True,
                tpsl="sl" if kind == "sl" else "tp",
            )
        )

        logger.info(
            f"[place_hl_trigger_order] SUBMIT {coin} kind={kind} "
            f"side={'buy' if is_buy else 'sell'} size={size_str} "
            f"trigger={trigger_str} notional=${order_notional:.2f} "
            f"reduce_only=true (hl_min=${MIN_ORDER_USD:.2f}, "
            f"{'ABOVE min' if order_notional >= MIN_ORDER_USD else 'BELOW min — will be rejected on trigger!'})"
        )

        # isMarket=True fills at market on trigger; limit_px is a reference.
        result = exchange.order(
            coin,
            is_buy,
            float(size_str),
            trigger_f,
            order_type,
            reduce_only=True,
        )

        parsed = _parse_order_result(result, accept_resting=True)
        if parsed.get("ok"):
            logger.info(
                f"[place_hl_trigger_order] CONFIRMED {coin} kind={kind} "
                f"oid={parsed.get('order_id')} size={size_str} trigger={trigger_str}"
            )
        else:
            logger.error(
                f"[place_hl_trigger_order] FAILED {coin} kind={kind} "
                f"size={size_str} trigger={trigger_str} "
                f"error={parsed.get('error')}"
            )
        return parsed
    except Exception as e:
        logger.error(f"[place_hl_trigger_order] EXCEPTION {coin} kind={kind}: {e}")
        return {"ok": False, "error": str(e)}


def modify_sl_trigger(
    is_long_position: bool,
    size: float,
    new_trigger_px: float,
    coin: str,
    oid: int,
) -> Dict[str, Any]:
    """Modify an existing reduce-only SL trigger order's trigger price via batchModify.

    Hyperliquid implements `modify_order` as an atomic cancel+replace: the old
    `oid` is cancelled and a NEW oid is returned under `statuses[0].resting.oid`.
    Callers MUST persist the new oid — the old one is immediately invalid.

    Mainnet-verified constraints (2026-08, ETH perp):
      * `limit_px` MUST equal the new triggerPx (the SDK rejects mismatches).
      * The price MUST be rounded to the coin's tick size via _round_price_for_hl;
        naive round() to 2 dp produces "Invalid TP/SL price" rejections.
      * Response is `{"statuses":[{"resting":{"oid": <NEW_OID>}}]}` — same shape
        as a fresh trigger placement, so _parse_order_result(accept_resting=True)
        parses it directly.
    """
    if not PRIVATE_KEY_HEX:
        return {"ok": False, "error": "HYPERLIQUID_PRIVATE_KEY not set"}
    if size <= 0 or new_trigger_px <= 0 or not oid:
        return {"ok": False, "error": "invalid size/price/oid"}

    try:
        _, sz_dec, _ = get_coin_index(coin)
        exchange = _make_exchange()

        # SL closes the position: opposite side, reduce-only.
        is_buy = not is_long_position

        trigger_str = _round_price_for_hl(new_trigger_px, sz_dec, is_perp=True)
        trigger_f = float(trigger_str)
        size_str = f"{size:.{sz_dec}f}"

        order_type = OrderType(
            trigger=TriggerOrderType(
                triggerPx=trigger_f,
                isMarket=True,
                tpsl="sl",
            )
        )

        logger.info(
            f"[modify_sl_trigger] SUBMIT {coin} oid={oid} -> new_trigger={trigger_str} "
            f"side={'buy' if is_buy else 'sell'} size={size_str} reduce_only=true"
        )

        # limit_px MUST equal triggerPx for trigger SL modifications.
        result = exchange.modify_order(
            oid=int(oid),
            name=coin,
            is_buy=is_buy,
            sz=float(size_str),
            limit_px=trigger_f,
            order_type=order_type,
            reduce_only=True,
        )

        # accept_resting=True: batchModify returns resting{oid:NEW_OID}.
        parsed = _parse_order_result(result, accept_resting=True)
        if parsed.get("ok"):
            logger.info(
                f"[modify_sl_trigger] CONFIRMED {coin} old_oid={oid} "
                f"new_oid={parsed.get('order_id')} trigger={trigger_str}"
            )
        else:
            logger.error(
                f"[modify_sl_trigger] FAILED {coin} old_oid={oid} "
                f"target_trigger={trigger_str} error={parsed.get('error')}"
            )
        return parsed
    except Exception as e:
        logger.error(f"[modify_sl_trigger] EXCEPTION {coin} oid={oid}: {e}")
        return {"ok": False, "error": str(e)}


def cancel_open_orders_for_coin(coin: str) -> int:
    """Best-effort: cancel all resting orders for `coin` — e.g. the reduce-only
    SL/TP trigger bracket left stranded after a market close. Without this,
    stale triggers accumulate and a later reduce-only order on the same coin is
    rejected ('reduce only order would increase position'). Returns the count
    cancelled. Never raises."""
    try:
        user = resolve_user_address()
        if not user:
            return 0
        orders = _http_post("/info", {"type": "openOrders", "user": user}) or []
        n = 0
        for o in orders:
            if o.get("coin") == coin and o.get("oid") is not None:
                if cancel_orders(int(o["oid"]), coin).get("ok"):
                    n += 1
        if n:
            logger.info(f"[cancel_open_orders_for_coin] cancelled {n} stranded order(s) for {coin}")
        return n
    except Exception as e:
        logger.warning(f"[cancel_open_orders_for_coin] {coin} failed: {e}")
        return 0


def cancel_orders(oid: int, coin: Optional[str] = None, asset_idx: Optional[int] = None) -> Dict[str, Any]:
    """Cancel an order by order ID."""
    if not PRIVATE_KEY_HEX:
        return {"ok": False, "error": "PRIVATE_KEY not set"}
    
    try:
        # Need coin name for cancel - use asset_idx to look it up
        coin_name = coin
        if not coin_name and asset_idx is not None:
            info = _get_info()
            meta = info.meta()
            for u in meta.get("universe", []):
                if u.get("index") == asset_idx:
                    coin_name = u["name"]
                    break
            if not coin_name:
                return {"ok": False, "error": f"unknown asset index {asset_idx}"}
        
        if not coin_name:
            return {"ok": False, "error": "coin name required"}
        
        exchange = _make_exchange()
        result = exchange.cancel(coin_name, oid)
        
        if isinstance(result, dict) and result.get("status") == "ok":
            return {"ok": True}
        return {"ok": False, "error": str(result)}
    except Exception as e:
        logger.error(f"Failed to cancel order: {e}")
        return {"ok": False, "error": str(e)}


# ── ATR ────────────────────────────────────────────────────────────────────────

# ATR is computed from 4h candles (24 candles/day); it changes slowly so a
# short TTL safely eliminates redundant candle fetches within one execute()
# call and across nearby calls. 0.0 results are also cached so a coin with
# insufficient history doesn't trigger repeated network fetches.
_ATR_CACHE: Dict[str, Tuple[float, float]] = {}
_ATR_CACHE_LOCK = threading.Lock()
_ATR_TTL_S = float(os.environ.get("HERMES_ATR_TTL_S", "60"))


def get_hl_atr(
    interval: str = "4h",
    period: int = 14,
    coin: str = "BTC",
) -> float:
    """Compute ATR(period) on a given HL interval (defaults to 4h).

    Results are cached for _ATR_TTL_S seconds keyed by (interval, period, coin).
    ATR on 4h candles is stable over sub-minute windows, and the executor may
    call this multiple times with identical arguments within one execution;
    caching avoids redundant candle API round-trips.
    """
    cache_key = f"{interval}|{period}|{coin}"
    now = _time.time()
    with _ATR_CACHE_LOCK:
        hit = _ATR_CACHE.get(cache_key)
        if hit and (now - hit[0]) < _ATR_TTL_S:
            return hit[1]

    candles = fetch_hl_candles(coin, interval, period + 10)
    if len(candles) < period + 1:
        result = 0.0
    else:
        tr = []
        for i in range(1, len(candles)):
            cur, pc = candles[i], candles[i - 1]
            tr.append(max(
                cur.h - cur.l,
                abs(cur.h - pc.c),
                abs(cur.l - pc.c),
            ))

        if len(tr) < period:
            result = 0.0
        else:
            atr = sum(tr[:period]) / period
            for i in range(period, len(tr)):
                atr = (atr * (period - 1) + tr[i]) / period
            result = atr

    with _ATR_CACHE_LOCK:
        _ATR_CACHE[cache_key] = (now, result)
    return result
