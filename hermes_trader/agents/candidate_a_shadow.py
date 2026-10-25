"""Record-only shadow arm for preregistered "Candidate A" (merge-plan §5.3).

Candidate A is the ONE acceleration path with offline evidence, but it FAILED
family-wise correction (FWER p=0.205), so it must stay a shadow arm and never
enter the live config. This module collects the forward (live) sample needed
to re-judge it after 250 trades. Preregistration, frozen:

    combo   higherLows1h AND pctMoveSpike AND volumeSpike
    regime  regime == "up" (coin's own 1h candles)
    hold    fixed 4h, no stop/take-off (raw drift)
    cost    9.47 bp round trip (applied at analysis time, not here)
    gate    mean net >= 3 bp/trade AND day-block bootstrap CI lower bound > 0
            after 250 trades
   纪律    no changing combo / regime / horizon; higherLows1h alone is -5.76bp.

The arm is strictly observational:

  * it never routes, places, or shadow-books an order and never flips
    allow_shorts (stays false); Candidate A is a LONG continuation setup;
  * it reads the triggers/regime the scan already computed (zero extra
    network) and appends one JSONL row per eligible closed bar;
  * every failure degrades to a no-op — the arm must never perturb trading.

Configuration (launch_capture.candidate_a_shadow):
  enabled bool (default False — the whole path is a strict no-op when off)
  require_regime_up bool (default True — the frozen regime filter)
  log_path: optional JSONL override (defaults to candidate_a_shadow.jsonl)
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

_COMBO = ("higherLows1h", "pctMoveSpike", "volumeSpike")

# Same closed-bar setup is recorded at most once per process. Keyed on the
# signal fingerprint (coin + bar_close_ms + fired triggers) the main flow uses.
_SEEN_KEYS: set[tuple[str, Optional[int]]] = set()
_MAX_SEEN = 20_000


def _resolve(config: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Return the validated sub-config when enabled, else None."""
    lc = (config or {}).get("launch_capture") or {}
    cfg = lc.get("candidate_a_shadow") or {}
    if not bool(cfg.get("enabled", False)):
        return None
    return cfg


def combo_fired(fired_names: list[str]) -> bool:
    """Pure predicate: all three frozen combo legs fired on this bar."""
    names = set(fired_names or [])
    return all(leg in names for leg in _COMBO)


def in_candidate_a(
    *, fired_names: list[str], regime: Optional[str], config: dict[str, Any],
) -> bool:
    """Pure eligibility predicate (no I/O). Long continuation setup only."""
    cfg = _resolve(config)
    if cfg is None:
        return False
    if not combo_fired(fired_names):
        return False
    if bool(cfg.get("require_regime_up", True)) and regime != "up":
        return False
    return True


def maybe_record(
    *, coin: str, perception: dict[str, Any], regime: Optional[str],
    config: dict[str, Any], data_dir: str,
) -> Optional[dict[str, Any]]:
    """Append one record-only row for an eligible Candidate A setup.

    Returns the written record (or None when skipped). Never raises. The 4h
    forward return is NOT computed here — offline analysis joins entry price
    (``mid``) to the 4h-future close; the row carries everything needed to do
    so reproducibly (bar close, mid, composite, fired legs).
    """
    try:
        fired = list(perception.get("fired_triggers") or [])
        if not in_candidate_a(
                fired_names=fired, regime=regime, config=config):
            return None
        bar_close = perception.get("bar_close_ms")
        dedup_key = (coin, bar_close if isinstance(bar_close, int) else None)
        if dedup_key in _SEEN_KEYS:
            return None
        if len(_SEEN_KEYS) >= _MAX_SEEN:
            _SEEN_KEYS.clear()
        _SEEN_KEYS.add(dedup_key)

        cfg = _resolve(config)
        assert cfg is not None
        rec = {
            "ts": int(time.time() * 1000),
            "coin": coin,
            "bar_close_ms": bar_close,
            "entry_mid": perception.get("mid"),
            "composite_score": perception.get("composite_score"),
            "regime": regime,
            "combo": list(_COMBO),
            "fired_triggers": fired,
            "hold_bars_1h": 4,            # frozen: fixed 4h, raw drift
            "round_trip_cost_bps": 9.47,  # frozen offline analysis cost
            "shadow": True,               # never a trade / routing decision
        }
        path = str(cfg.get("log_path") or "") or os.path.join(
            data_dir, "candidate_a_shadow.jsonl")
        from hermes_trader.shadow_log import append_jsonl
        append_jsonl(path, rec, stream="candidate_a_shadow")
        logger.info(
            "[candidate_a_shadow] %s bar=%s regime=%s combo fired "
            "(record-only, 4h hold, no trade)", coin, bar_close, regime)
        return rec
    except Exception as e:  # absolute guard: the shadow never breaks the loop
        logger.debug("[candidate_a_shadow] skipped: %s", e)
        return None


def reset_seen_for_tests() -> None:
    _SEEN_KEYS.clear()
