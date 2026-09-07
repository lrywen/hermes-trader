"""H-6 (supplemental audit 2026-08-30): cross-source price verification.

Entry pricing, DSL peak/floor anchoring and position sizing all derive from a
SINGLE price source: the Hyperliquid mid (WS allMids, with a same-origin REST
fallback). Binance was already queried for whale order-flow
(agents/crypto_whale.py) but never for price verification, so a stale,
manipulated or simply wrong HL mid could not be detected before an order went
out.

This module adds an independent second source — Binance's public, keyless
spot ticker (``/api/v3/ticker/price``) — and compares it against the HL mid:

  * small divergence            -> ok
  * warn threshold exceeded     -> ok=False with action="alert"  (logged/notified)
  * block threshold exceeded    -> ok=False with action="block"  (entry vetoed)

Design constraints
------------------
* Client layer: no imports from ``hermes_trader.agents`` (agents may import
  client, never the reverse). The HL-coin -> Binance-symbol map therefore
  lives here (a superset of the whale-flow map, with denomination scale).
* Fail-OPEN: Binance outage, rate limiting, an unsupported pair (xyz: equity
  perps, delisted tickers) or an unknown denomination scale return
  ``{"ok": True, "checked": False, ...}``. The secondary source is a safety
  net, never a hard dependency that could halt trading on its own outage.
* Exits are NEVER vetoed by callers — closing a position is safety-critical;
  the veto is only meaningful on the entry path.
* k-prefixed HL meme tickers (kPEPE, kSHIB, ...) are 1000x-denominated vs the
  plain Binance spot price, so the Binance price is scaled before compare.
"""
from __future__ import annotations

import json
import logging
import os
import ssl
import threading
import time
import urllib.parse
import urllib.request
from typing import Any, Optional

logger = logging.getLogger(__name__)

try:
    import certifi
    _SSL = ssl.create_default_context(cafile=certifi.where())
except Exception:  # pragma: no cover - certifi is a pinned dep
    # Never fall back to an unverified context (MITM risk); use the system
    # trust store with full verification.
    _SSL = ssl.create_default_context()

_TICKER = "https://api.binance.com/api/v3/ticker/price"

# HL crypto coin -> (Binance USDT spot symbol, price scale).
# Default for unlisted coins: ("{COIN}USDT", 1.0). k-prefixed HL meme perps
# trade at 1000x the plain spot price; xyz: equity perps have no Binance pair
# and are skipped by the caller (None symbol).
_SYMBOL_SCALE_OVERRIDE: dict[str, tuple[str, float]] = {
    "kPEPE": ("PEPEUSDT", 1000.0),
    "kSHIB": ("SHIBUSDT", 1000.0),
    "kBONK": ("BONKUSDT", 1000.0),
    "kFLOKI": ("FLOKIUSDT", 1000.0),
    "kLUNC": ("LUNCUSDT", 1000.0),
    "kDOGS": ("DOGSUSDT", 1000.0),
}

# Default thresholds (bps of the HL reference price). Spot-vs-perp basis and
# HL/Binance microstructure noise stay well inside 30 bps; a sustained >1%
# gap means one feed is wrong/stale and an entry priced off it is dangerous.
_DEFAULT_WARN_BPS = 30.0     # 0.30%
_DEFAULT_BLOCK_BPS = 100.0   # 1.00%
_DEFAULT_TTL_S = 10.0        # entries are rare; a short cache is plenty
_DEFAULT_HTTP_TIMEOUT_S = 2.5

_cache: dict[str, tuple[float, Optional[float]]] = {}
_cache_lock = threading.Lock()


# Audit 2026-09-06 (F6, engineering hygiene): the four knobs below used to be
# a hardcoded 2.5s timeout literal plus three env-only floats — invisible to
# the dashboard config dump, not validated by validate_config_updates, and not
# overridable via .agent-config.json. They now register in CANONICAL_DEFAULTS
# (block ``price_crosscheck``) and resolve through the same client-layer
# helper as hl_client_io: legacy env (top priority, deployment channel) ->
# cfg_get("price_crosscheck.*") -> the literals below. The on/off switch stays
# a plain env read (no canonical leaf), matching the historical semantics.
_PRICE_CROSSCHECK_DEFAULTS: dict[str, Any] = {
    "http_timeout_s": _DEFAULT_HTTP_TIMEOUT_S,
    "ttl_s": _DEFAULT_TTL_S,
    "warn_bps": _DEFAULT_WARN_BPS,
    "block_bps": _DEFAULT_BLOCK_BPS,
}

# leaf -> (legacy env or None, kind "f", min guard value).
_PRICE_CROSSCHECK_SPEC: dict[str, tuple[Optional[str], str, float]] = {
    "http_timeout_s": (None, "f", 0.0),
    "ttl_s": ("HERMES_PRICE_CROSSCHECK_TTL_S", "f", 0.0),
    "warn_bps": ("HERMES_PRICE_DIVERGENCE_WARN_BPS", "f", 0.0),
    "block_bps": ("HERMES_PRICE_DIVERGENCE_BLOCK_BPS", "f", 0.0),
}


def _params() -> dict[str, Any]:
    """Resolved ``price_crosscheck`` block. Never raises — on any error a
    fresh copy of the literals is returned so the crosscheck path degrades to
    the old behaviour (and the crosscheck itself is fail-open anyway)."""
    try:
        from hermes_trader.client.rate_limit import _resolve_hl_block

        return _resolve_hl_block(
            "price_crosscheck", _PRICE_CROSSCHECK_DEFAULTS, _PRICE_CROSSCHECK_SPEC
        )
    except Exception:
        return dict(_PRICE_CROSSCHECK_DEFAULTS)


def binance_spot(coin: str) -> Optional[tuple[str, float]]:
    """HL crypto coin -> (Binance USDT symbol, price scale). None for coins
    with no comparable Binance spot pair (xyz: equity perps).

    (supplemental audit 2026-08-30) OPERATIONAL CONSTRAINT: a coin with no
    Binance pair returns None, so crosscheck_price fail-OPENs on the single HL
    source with no second-source veto. Equity / HIP-3 perps (``xyz:``-style
    tickers) therefore must NOT be enabled for live trading until an
    independent second price source is wired in here. Crypto perps are covered."""
    if not coin or ":" in coin:
        return None
    return _SYMBOL_SCALE_OVERRIDE.get(coin, (f"{coin.upper()}USDT", 1.0))


def _fetch_binance_price(symbol: str) -> Optional[float]:
    url = f"{_TICKER}?symbol={urllib.parse.quote(symbol)}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        # Audit 2026-09-06 (F6): timeout resolves from canonical
        # price_crosscheck.http_timeout_s (was a hardcoded 2.5 literal).
        with urllib.request.urlopen(
            req, timeout=float(_params()["http_timeout_s"]), context=_SSL
        ) as r:
            payload = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        logger.warning(f"[price-xcheck] Binance ticker GET failed for {symbol}: {e!r}")
        return None
    # Invalid/delisted symbol -> {"code": -1121, "msg": "Invalid symbol"}.
    if not isinstance(payload, dict) or "price" not in payload:
        return None
    try:
        return float(payload["price"])
    except (TypeError, ValueError):
        return None


def _binance_price_scaled(coin: str) -> tuple[Optional[float], str]:
    """Return (price in HL denomination, reason). price is None when the
    secondary source is unavailable / not comparable (fail-open)."""
    spot = binance_spot(coin)
    if spot is None:
        return None, "no_binance_pair"
    symbol, scale = spot
    now = time.time()
    # Audit 2026-09-06 (F6): TTL resolves from canonical price_crosscheck.ttl_s
    # (legacy HERMES_PRICE_CROSSCHECK_TTL_S stays top priority inside _params).
    ttl = float(_params()["ttl_s"])
    with _cache_lock:
        hit = _cache.get(symbol)
        if hit is not None and (now - hit[0]) < ttl:
            raw = hit[1]
        else:
            raw = None
    if hit is None:
        raw = _fetch_binance_price(symbol)
        with _cache_lock:
            _cache[symbol] = (now, raw)
    if raw is None:
        return None, "binance_unavailable"
    return raw * scale, ""


def crosscheck_price(coin: str, hl_price: float) -> dict[str, Any]:
    """Compare the Hyperliquid mid against Binance spot for ``coin``.

    Returns a dict:
      * {"ok": True, "checked": False, "reason": ...}  — skipped / secondary
        unavailable (caller proceeds; fail-open).
      * {"ok": True, "checked": True, "divergence_bps": ..., ...}  — within
        tolerance.
      * {"ok": False, "checked": True, "action": "alert"|"block",
         "divergence_bps": ..., ...} — warn/block thresholds exceeded.

    ``hl_price`` <= 0 (no HL mid yet) is treated as "not checked".
    """
    if os.environ.get("HERMES_PRICE_CROSSCHECK_ENABLED", "1").strip() in ("0", "false", "False"):
        return {"ok": True, "checked": False, "reason": "disabled"}
    try:
        hl = float(hl_price or 0.0)
    except (TypeError, ValueError):
        hl = 0.0
    if hl <= 0:
        return {"ok": True, "checked": False, "reason": "no_hl_price"}

    ref, reason = _binance_price_scaled(coin)
    if ref is None or ref <= 0:
        return {"ok": True, "checked": False, "reason": reason or "unavailable"}

    div_bps = abs(hl - ref) / hl * 10_000.0
    # Audit 2026-09-06 (F6): thresholds resolve from canonical
    # price_crosscheck.warn_bps / block_bps (legacy HERMES_PRICE_DIVERGENCE_*
    # env vars stay top priority inside _params).
    p = _params()
    block_bps = float(p["block_bps"])
    warn_bps = float(p["warn_bps"])
    result = {
        "ok": True,
        "checked": True,
        "coin": coin,
        "hl_price": hl,
        "ref_price": ref,
        "ref_source": "binance_spot",
        "divergence_bps": round(div_bps, 2),
        "warn_bps": warn_bps,
        "block_bps": block_bps,
    }
    if div_bps >= block_bps:
        result["ok"] = False
        result["action"] = "block"
        result["reason"] = (
            f"price divergence {div_bps:.1f} bps >= block {block_bps:.1f} bps "
            f"(HL mid {hl:g} vs Binance {ref:g})"
        )
    elif div_bps >= warn_bps:
        result["ok"] = False
        result["action"] = "alert"
        result["reason"] = (
            f"price divergence {div_bps:.1f} bps >= warn {warn_bps:.1f} bps "
            f"(HL mid {hl:g} vs Binance {ref:g})"
        )
    return result
