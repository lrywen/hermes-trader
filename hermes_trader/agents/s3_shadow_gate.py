"""S3 shadow-shorts gate: regime-conditional short admission in SHADOW only.

This module is a pure decision layer. It does NOT place orders or mutate the
frozen production switches (scan_long_only / allow_shorts /
min_short_volume_usd). The executor calls ``shadow_short_allowed`` at the
existing short-circuit point; when it returns True the short proceeds through
the normal shadow-book path (zero exchange exposure). In LIVE the gate is
always closed regardless of config.

Design:
  * ``enabled`` defaults False → zero behaviour change until an operator flips
    it explicitly.
  * Regime is resolved once per decision via the existing
    ``resolve_decision_regime`` cache (no extra network).
  * The impact-measurement floor never blocks; it only tags each candidate
    with the alternative-floor outcome for offline analysis.
"""
from __future__ import annotations

from typing import Any, Mapping, Optional


def s3_config(config: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    """Extract and normalise the s3_shadow_shorts block; safe defaults."""
    blk = (config or {}).get("s3_shadow_shorts")
    if not isinstance(blk, dict):
        return {"enabled": False, "allow_regimes": ["down"],
                "adaptive_scan_long_only": True,
                "impact_measure_floor_usd": 5_000_000.0}
    regimes = blk.get("allow_regimes")
    if not isinstance(regimes, list) or not regimes:
        regimes = ["down"]
    regimes = [str(r).lower() for r in regimes]
    floor = blk.get("impact_measure_floor_usd", 5_000_000.0)
    try:
        floor = float(floor)
    except (TypeError, ValueError):
        floor = 5_000_000.0
    return {
        "enabled": bool(blk.get("enabled", False)),
        "allow_regimes": regimes,
        "adaptive_scan_long_only": bool(blk.get("adaptive_scan_long_only", True)),
        "impact_measure_floor_usd": max(0.0, floor),
    }


def shadow_short_allowed(
    *,
    mode: str,
    side: str,
    regime: Optional[str],
    config: Optional[Mapping[str, Any]],
) -> bool:
    """Pure predicate: may this short enter the SHADOW book under S3?

    Returns False for any long, any LIVE/OFF mode, when disabled, or when the
    resolved regime is not in the allow-list. Unknown/empty regime is rejected
    (fail-closed) so a regime lookup failure can never silently open shorts.
    """
    if str(side).lower() != "short":
        return False
    if str(mode).upper() != "SHADOW":
        return False
    cfg = s3_config(config)
    if not cfg["enabled"]:
        return False
    if not regime:
        return False
    return str(regime).lower() in cfg["allow_regimes"]


def adaptive_long_only_skip(
    *,
    mode: str,
    coin: str,
    ta: Mapping[str, Any],
    held_coins: Collection,
    scan_long_only: bool,
    regime: Optional[str],
    config: Optional[Mapping[str, Any]],
) -> bool:
    """Regime-adaptive replacement for ``long_only_should_skip`` in SHADOW.

    When the S3 channel is enabled with ``adaptive_scan_long_only``, a pure
    short-intent candidate in an allowed regime is NOT skipped (it reaches the
    AI debate and then the shadow gate). Every other case delegates to the
    original long-only semantics.
    """
    from hermes_trader.agents.ta_filter import long_only_should_skip

    if not scan_long_only:
        return False
    # Held coins always kept (close-check); preserve original behaviour.
    if coin in set(held_coins or ()):
        return False
    cfg = s3_config(config)
    if (cfg["enabled"] and cfg["adaptive_scan_long_only"]
            and str(mode).upper() == "SHADOW"
            and bool(ta.get("intend_short")) and not bool(ta.get("intend_long"))
            and regime and str(regime).lower() in cfg["allow_regimes"]):
        return False
    return long_only_should_skip(coin, ta, held_coins, scan_long_only)


def adaptive_long_only_decision(
    *,
    mode: str,
    coin: str,
    ta: Mapping[str, Any],
    held_coins: Collection,
    scan_long_only: bool,
    config: Optional[Mapping[str, Any]],
) -> dict[str, Any]:
    """Resolve the scan-time S3 decision and its observation metadata.

    Regime detection is paid for only when a pure short can actually use the
    enabled SHADOW channel. The probe never emits a coin-breakout audit row;
    the executor emits that row later with the real analysis id.
    """
    cfg = s3_config(config)
    regime = None
    if (scan_long_only and coin not in set(held_coins or ())
            and cfg["enabled"] and cfg["adaptive_scan_long_only"]
            and str(mode).upper() == "SHADOW"
            and bool(ta.get("intend_short")) and not bool(ta.get("intend_long"))):
        from hermes_trader.agents.executor import resolve_decision_regime

        probe = dict(ta)
        probe.update({"coin": coin, "side": "short"})
        regime = resolve_decision_regime(probe, config, record_bypass=False)
    skip = adaptive_long_only_skip(
        mode=mode, coin=coin, ta=ta, held_coins=held_coins,
        scan_long_only=scan_long_only, regime=regime, config=config)
    bypass = False
    if not skip and scan_long_only:
        from hermes_trader.agents.ta_filter import long_only_should_skip

        bypass = long_only_should_skip(coin, ta, held_coins, scan_long_only)
    return {"skip": skip, "regime": regime, "s3_scan_bypass": bypass}


def impact_floor_tag(
    *,
    side: str,
    volume_24h_usd: Optional[float],
    config: Optional[Mapping[str, Any]],
) -> dict[str, Any]:
    """Tag a short candidate with the alternative-floor impact outcome.

    Observation only — never blocks. Returns a dict suitable for merging into
    the runner-gate shadow record.
    """
    cfg = s3_config(config)
    alt_floor = cfg["impact_measure_floor_usd"]
    if str(side).lower() != "short":
        return {"s3_alt_floor_usd": None, "s3_alt_floor_would_block": None}
    if volume_24h_usd is None:
        return {"s3_alt_floor_usd": alt_floor, "s3_alt_floor_would_block": None}
    try:
        vol = float(volume_24h_usd)
    except (TypeError, ValueError):
        return {"s3_alt_floor_usd": alt_floor, "s3_alt_floor_would_block": None}
    return {
        "s3_alt_floor_usd": alt_floor,
        "s3_alt_floor_would_block": bool(alt_floor > 0 and vol < alt_floor),
    }


# Collection is only imported for typing; avoid a hard module-level dependency
# cycle by aliasing inside the function signature lazily.
try:
    from typing import Collection  # noqa: F401
except Exception:  # pragma: no cover
    Collection = list  # type: ignore
