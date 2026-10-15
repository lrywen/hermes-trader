"""Confidence-threshold single-source guard (audit 2026-10-15 P0-1).

The entry decision used to read two separately-tuned floors:

  * ``min_ai_confidence`` — the canonical AI conviction floor; also the value
    the structural-override floor rewrites confidence *up to* and the base the
    risk-gates ``confidence`` gate + aligned-regime discount use;
  * ``runner_entry_gate.min_confidence`` — the runner entry gate's own bar.

They drifted apart (0.60 canonical vs 0.70 in the live file), so the override
bumped a trade to 0.60 and the *risk-gates* confidence check cleared it while
the *runner* gate — which judges the model's pre-floor ``ai_confidence_raw`` —
silently cut 34.7% of candidates that every other floor considered good
enough. The effective bar was whichever copy happened to be highest, and no
single place named it.

Convergence rule:

  * ``runner_entry_gate.min_confidence`` is the **generic** runner bar and must
    equal the canonical ``min_ai_confidence``. The runner keeps its genuinely
    distinct *situation* bars — ``min_short_confidence`` (fighting the funding
    regime) and ``mover_min_confidence`` (top-of-board extension) — which are
    not duplicates and are free to sit above the generic bar.
  * When the nested generic key is absent the runner derives it from
    ``min_ai_confidence`` (single source).

:func:`confidence_threshold_warnings` returns human-readable drift findings
(never raises). It is surfaced as a **startup warning**, not a hard refusal:
config drift must be loud, but it must never stop a safety-critical boot or
block exit management.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

_EPS = 1e-9


def canonical_generic_confidence(config: Mapping[str, Any]) -> float | None:
    """The single generic AI confidence floor (``min_ai_confidence``)."""
    try:
        return float(config["min_ai_confidence"])
    except (KeyError, TypeError, ValueError):
        return None


def runner_generic_confidence(config: Mapping[str, Any]) -> float | None:
    """Runner gate's generic bar if explicitly set in the nested block."""
    gate = config.get("runner_entry_gate")
    if not isinstance(gate, Mapping) or "min_confidence" not in gate:
        return None
    try:
        return float(gate["min_confidence"])
    except (TypeError, ValueError):
        return None


def resolve_runner_generic_confidence(config: Mapping[str, Any]) -> float:
    """Effective generic runner bar: explicit nested key, else canonical.

    The nested key stays settable for emergency override, but when it is absent
    the runner no longer carries its own literal — it derives the bar from the
    single canonical ``min_ai_confidence``.
    """
    nested = runner_generic_confidence(config)
    if nested is not None:
        return nested
    canonical = canonical_generic_confidence(config)
    return canonical if canonical is not None else 0.70


def confidence_threshold_warnings(config: Mapping[str, Any]) -> list[str]:
    """Return drift findings between the two generic confidence floors.

    Pure and total: malformed/missing values are reported, never raised. The
    deliberate per-situation runner bars (short / mover) are checked only for
    ordering relative to the generic bar, not equality.
    """
    findings: list[str] = []
    canonical = canonical_generic_confidence(config)
    if canonical is None:
        findings.append(
            "min_ai_confidence missing or non-numeric; generic confidence "
            "floor falls back to built-in defaults")
        return findings

    nested = runner_generic_confidence(config)
    if nested is not None and abs(nested - canonical) > _EPS:
        findings.append(
            f"confidence floor drift: min_ai_confidence={canonical:.2f} but "
            f"runner_entry_gate.min_confidence={nested:.2f}. The higher of the "
            "two silently sets the real entry bar; align them (set the nested "
            "key equal to min_ai_confidence or remove it to derive)")

    gate = config.get("runner_entry_gate")
    if isinstance(gate, Mapping):
        for key in ("min_short_confidence", "mover_min_confidence"):
            try:
                v = float(gate[key])
            except (KeyError, TypeError, ValueError):
                continue
            if v < canonical - _EPS:
                findings.append(
                    f"runner_entry_gate.{key}={v:.2f} is below the generic "
                    f"min_ai_confidence={canonical:.2f}; the situation bar "
                    "should be >= the generic bar")
    return findings
