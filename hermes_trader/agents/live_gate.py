"""G3 live-trading gate (B-12 / B-13, 2026-09-20).

A config flip ``mode: SHADOW -> LIVE`` must never be sufficient to trade real
money. This module adds the BOOT-TIME acceptance gate on top of the existing
per-order P0-1 gate (``live_trading_authorized``) — it does not replace it:

* **B-12 — combination assertion.** ``HERMES_ENABLE_LIVE=true`` together with
  ``mode`` other than ``LIVE`` is a contradictory/footgun combo (the operator
  armed the env but the config is not actually live, or vice-versa). It is not
  fatal (SHADOW still can't place orders), but it must surface loudly so a
  single mistyped string can't silently change the risk posture.

* **B-13 — acceptance record.** When ``mode=LIVE`` the process must present a
  signed acceptance-gate record proving the strategy passed the §5.2 gate
  (outcome A:判据 1–5 pass AND a block-bootstrap CI strictly above zero).
  Without a valid record the trading loop refuses to start and lists what is
  missing. The record is a small JSON file (default under the data dir;
  override with ``HERMES_LIVE_GATE_FILE`` for tests).

Pure helpers here are side-effect light and fully unit-testable; the loop only
calls :func:`startup_live_gate_errors` before the first scan.
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Optional

RECORD_VERSION = 1
_REQUIRED_FIELDS = (
    "version", "decision", "arms", "block_bootstrap_ci",
    "config_src", "config_sha256", "operator", "utc_iso",
)


def default_gate_path() -> str:
    """Resolve the acceptance-record path (env override for tests)."""
    return os.environ.get(
        "HERMES_LIVE_GATE_FILE",
        os.path.join(os.environ.get("HERMES_DATA_DIR", "/data"), "live_acceptance_gate.json"),
    )


def config_sha256(cfg: dict[str, Any]) -> str:
    """Stable hash of the merged config the gate decision was made against."""
    blob = json.dumps(cfg, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def evaluate_live_combo(mode: str, env_authorized: bool) -> list[str]:
    """B-12: warnings for contradictory env×mode combinations (never fatal
    for non-LIVE trading — these are footguns, surfaced at boot)."""
    m = str(mode or "OFF").upper()
    warnings: list[str] = []
    if env_authorized and m != "LIVE":
        warnings.append(
            f"B-12: HERMES_ENABLE_LIVE=true but mode={m} — the live-money env "
            "grant is armed while the config is NOT live (unset the env or set "
            "mode=LIVE); no real entries can be placed in this state")
    if m == "LIVE" and not env_authorized:
        # Mirrors the per-order P0-1 gate; at boot it is still only a warning
        # (the hard refusal happens per-order). B-13 still requires the record.
        warnings.append(
            "B-12: mode=LIVE but HERMES_ENABLE_LIVE is not true — entries will "
            "be denied per-order by the P0-1 gate even if the acceptance record "
            "is present")
    return warnings


def validate_acceptance_record(
    record: dict[str, Any], *, expected_config_sha256: Optional[str] = None,
) -> list[str]:
    """Validate a parsed acceptance record. Returns human-readable errors
    (empty == valid). Enforces the §5.2 outcome-A shape; it does not itself
    re-run the bootstrap (the recorded CI is the evidence, cross-checked for
    internal consistency, not blindly trusted)."""
    errors: list[str] = []
    for k in _REQUIRED_FIELDS:
        if k not in record:
            errors.append(f"B-13: acceptance record missing field '{k}'")
    if errors:
        return errors  # downstream checks need the fields present

    if record.get("version") != RECORD_VERSION:
        errors.append(
            f"B-13: acceptance record version={record.get('version')!r} "
            f"!= supported {RECORD_VERSION}")
    if record.get("decision") != "outcome_a_go_live":
        errors.append(
            "B-13: acceptance record decision is not 'outcome_a_go_live' — only "
            "§5.2 outcome A (credible backtest AND positive block bootstrap) "
            f"may authorize LIVE; got {record.get('decision')!r}")
    ci = record.get("block_bootstrap_ci")
    if not isinstance(ci, (list, tuple)) or len(ci) != 2:
        errors.append("B-13: block_bootstrap_ci must be a [lo, hi] pair")
    else:
        try:
            lo, hi = float(ci[0]), float(ci[1])
        except (TypeError, ValueError):
            errors.append("B-13: block_bootstrap_ci bounds must be numeric")
        else:
            if lo >= hi:
                errors.append("B-13: block_bootstrap_ci lo must be < hi")
            if lo <= 0:
                errors.append(
                    f"B-13: block_bootstrap_ci lo={lo} is not strictly > 0 — "
                    "no positive statistically-significant result (this is "
                    "outcome B/C, which must remain in SHADOW)")
    if not str(record.get("config_src", "")).strip():
        errors.append("B-13: config_src must name the production config source")
    if not str(record.get("operator", "")).strip():
        errors.append("B-13: operator must identify the authorizing operator/run")
    if not str(record.get("utc_iso", "")).strip():
        errors.append("B-13: utc_iso timestamp is required")
    if (expected_config_sha256 and record.get("config_sha256")
            and record["config_sha256"] != expected_config_sha256):
        errors.append(
            "B-13: acceptance record config_sha256 does not match the running "
            "config — re-run the §5.2 gate against the current config")
    return errors


def load_acceptance_record(path: Optional[str] = None) -> tuple[
        Optional[dict[str, Any]], Optional[str]]:
    """Return (record, load_error). record None when unreadable/missing."""
    path = path or default_gate_path()
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh), None
    except FileNotFoundError:
        return None, f"acceptance record not found at {path}"
    except (OSError, json.JSONDecodeError) as e:
        return None, f"acceptance record unreadable at {path}: {e}"


def build_acceptance_record(
    cfg: dict[str, Any], *, arms: list[str], ci_lo: float, ci_hi: float,
    config_src: str, operator: str, utc_iso: str,
    extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Construct a §5.2 outcome-A acceptance record bound to *cfg*.

    Raises ValueError if the supplied bootstrap CI is not strictly positive
    (an outcome-B/C result must never be turned into a LIVE record).
    """
    if not (ci_lo < ci_hi) or ci_lo <= 0:
        raise ValueError(
            f"refusing to build LIVE record: block-bootstrap CI [{ci_lo},{ci_hi}] "
            "must be strictly positive (lo>0); outcome B/C stays in SHADOW")
    rec = {
        "version": RECORD_VERSION,
        "decision": "outcome_a_go_live",
        "arms": list(arms),
        "block_bootstrap_ci": [float(ci_lo), float(ci_hi)],
        "config_src": config_src,
        "config_sha256": config_sha256(cfg),
        "operator": operator,
        "utc_iso": utc_iso,
    }
    if extra:
        rec.update(extra)
    # Fail loudly if anything about the assembled record is itself invalid.
    errs = validate_acceptance_record(rec, expected_config_sha256=config_sha256(cfg))
    if errs:
        raise ValueError("built acceptance record failed validation: " + "; ".join(errs))
    return rec


def startup_live_gate_errors(
    cfg: dict[str, Any], *, env_authorized: Optional[bool] = None,
    gate_path: Optional[str] = None,
) -> tuple[list[str], list[str]]:
    """Boot-time gate used by the trading loop.

    Returns ``(fatal, warnings)``. A non-empty ``fatal`` list MUST stop startup
    (refuse to trade). Combines B-12 (combo warnings) and B-13 (record gate,
    fatal only when mode=LIVE). SHADOW/OFF never produce fatal errors here.
    """
    if env_authorized is None:
        from hermes_trader.agents.config_store import live_trading_authorized
        env_authorized = live_trading_authorized()

    mode = str(cfg.get("mode", "OFF")).upper()
    warnings = evaluate_live_combo(mode, bool(env_authorized))
    fatal: list[str] = []

    if mode == "LIVE":
        record, load_err = load_acceptance_record(gate_path)
        if record is None:
            fatal.append(
                f"B-13: mode=LIVE but {load_err}. Refusing to start. To authorize "
                "LIVE you must record a §5.2 outcome-A acceptance gate (判据1–5 "
                "pass + block-bootstrap 95% CI strictly > 0) at the gate path")
        else:
            errs = validate_acceptance_record(
                record, expected_config_sha256=config_sha256(cfg))
            if errs:
                fatal.append(
                    "B-13: mode=LIVE but the acceptance record is invalid; "
                    "refusing to start: " + "; ".join(errs))
    return fatal, warnings
