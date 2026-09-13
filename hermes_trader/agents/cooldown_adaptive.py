"""Audit 2026-09-12 (#8 majors-missed-surge): research-cooldown volatility
adaptation helpers.

Kept in a tiny import-safe module (no network / no loop-at-import side effects)
so both scripts/trading_loop.py and the unit tests can use the pure classifier.

`_coin_is_hot(perception, blk)` decides whether a coin is in a "hot" σ-burst
state that justifies shortening the re-research cooldown window. It uses ONLY
data already present on the perception dict (no extra HTTP):

* a strong return/volume σ spike (structured `z` emitted by triggers.py), or
* a momentumBurst trigger fired.

Missing / disabled / garbled input → (False, {}); callers keep the long calm
cooldown (fail-safe — never shortens a risk window on a read/parse fault).
"""

from __future__ import annotations

from typing import Any


def coin_is_hot(perception: dict[str, Any], blk: dict[str, Any]):
    try:
        if not isinstance(blk, dict) or not blk.get("enabled"):
            return False, {}
        pct_min = float(blk.get("pct_sigma_min", 3.0))
        vol_min = float(blk.get("vol_sigma_min", 5.0))
        pct_z = vol_z = 0.0
        burst = False
        for t in perception.get("triggers", []) or []:
            name = t.get("name")
            if name == "pctMoveSpike":
                pct_z = float(t.get("z") or 0.0)
            elif name == "volumeSpike":
                vol_z = float(t.get("z") or 0.0)
            elif name == "momentumBurst" and t.get("fired"):
                burst = True
        hot = burst or pct_z >= pct_min or vol_z >= vol_min
        return hot, {"pct_z": round(pct_z, 2), "vol_z": round(vol_z, 2),
                     "burst": bool(burst)}
    except Exception:
        return False, {}
