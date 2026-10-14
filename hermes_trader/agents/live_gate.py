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
import hmac
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

RECORD_VERSION = 2
_REQUIRED_FIELDS = (
    "version", "decision", "arms", "block_bootstrap_ci",
    "config_src", "config_sha256", "operator", "utc_iso",
    "not_after", "sig_algo", "sig",
)

# P4-2: a LIVE mandate is time-boxed. Default horizon for a freshly minted
# record; after not_after the record stops authorizing NEW entries (existing
# positions keep running under their DSL/stop management).
DEFAULT_TTL_DAYS = 30

_OPERATOR_TOKEN_ENV = "HERMES_OPERATOR_TOKEN"
SIG_ALGO = "hmac-sha256"

# Timestamp format for not_after / utc_iso parsing ("...Z" or explicit offset).
_ISO_FORMATS = ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z")


def _parse_utc(ts: str) -> Optional[datetime]:
    """Parse an ISO-8601 UTC timestamp; None when malformed."""
    s = str(ts or "").strip()
    if not s:
        return None
    for fmt in _ISO_FORMATS:
        try:
            dt = datetime.strptime(s, fmt)
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    return None


def _signing_payload(record: dict[str, Any]) -> bytes:
    """Canonical bytes covered by the signature: every field except the
    signature itself (``sig``), sorted, so key order on disk can't matter."""
    body = {k: v for k, v in record.items() if k != "sig"}
    return json.dumps(
        body, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, default=str,
    ).encode("utf-8")


def _operator_token() -> str:
    return os.environ.get(_OPERATOR_TOKEN_ENV, "")


def sign_acceptance_record(record: dict[str, Any], *, token: Optional[str] = None) -> dict[str, Any]:
    """Attach an HMAC-SHA256 signature over the record body.

    Uses HERMES_OPERATOR_TOKEN (or an explicit *token*). This does NOT defend
    against the trading process signing its own records (it can read the env
    token); it proves the record was minted by a holder of the operator token
    and has not been hand-edited / corrupted afterwards. Raises ValueError if
    no token is configured — a record must never be written unsigned.
    """
    secret = token if token is not None else _operator_token()
    if not secret:
        raise ValueError(
            f"refusing to sign: {_OPERATOR_TOKEN_ENV} is not set; a LIVE mandate "
            "must carry an operator-token HMAC signature")
    rec = dict(record)
    rec["sig_algo"] = SIG_ALGO
    rec.pop("sig", None)
    rec["sig"] = hmac.new(
        secret.encode("utf-8"), _signing_payload(rec), hashlib.sha256).hexdigest()
    return rec


def _signature_ok(record: dict[str, Any], *, token: Optional[str] = None) -> bool:
    """Constant-time verification of the record HMAC. Missing token/record
    signature -> False (fail closed)."""
    secret = token if token is not None else _operator_token()
    sig = record.get("sig")
    if not secret or not isinstance(sig, str) or record.get("sig_algo") != SIG_ALGO:
        return False
    expected = hmac.new(
        secret.encode("utf-8"), _signing_payload(record), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)


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
    now: Optional[datetime] = None, token: Optional[str] = None,
) -> list[str]:
    """Validate a parsed acceptance record. Returns human-readable errors
    (empty == valid). Enforces the §5.2 outcome-A shape; it does not itself
    re-run the bootstrap (the recorded CI is the evidence, cross-checked for
    internal consistency, not blindly trusted).

    P4-2 additionally enforces: an operator-token HMAC signature (tamper /
    hand-edit protection) and a ``not_after`` expiry — an expired mandate is
    invalid for NEW entries.
    """
    errors: list[str] = []
    for k in _REQUIRED_FIELDS:
        if k not in record:
            errors.append(f"B-13: acceptance record missing field '{k}'")
    if errors:
        return errors  # downstream checks need the fields present

    if record.get("version") != RECORD_VERSION:
        errors.append(
            f"B-13: acceptance record version={record.get('version')!r} "
            f"!= supported {RECORD_VERSION} (re-mint with the current tool)")
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
    issued = _parse_utc(record.get("utc_iso", ""))
    if issued is None:
        errors.append("B-13: utc_iso must be an ISO-8601 UTC timestamp")
    not_after = _parse_utc(record.get("not_after", ""))
    if record.get("not_after") and not_after is None:
        errors.append("B-13: not_after must be an ISO-8601 UTC timestamp")
    elif not_after is not None:
        now_dt = now or datetime.now(timezone.utc)
        if now_dt >= not_after:
            errors.append(
                f"B-13: acceptance record expired at not_after={record['not_after']} "
                "— the LIVE mandate no longer authorizes NEW entries (existing "
                "positions keep running under their stops); re-mint to continue")
        if issued is not None and not_after <= issued:
            errors.append("B-13: not_after must be later than utc_iso")
    if not _signature_ok(record, token=token):
        errors.append(
            "B-13: acceptance record signature is missing/invalid, or "
            f"{_OPERATOR_TOKEN_ENV} is unset — records must be minted with a "
            "valid operator-token HMAC (anti-tamper); re-mint")
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
    ttl_days: int = DEFAULT_TTL_DAYS, token: Optional[str] = None,
    extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Construct a §5.2 outcome-A acceptance record bound to *cfg*.

    The mandate is time-boxed: ``not_after = utc_iso + ttl_days`` (default
    30d). The record is signed with the operator token and self-validated
    before return. Raises ValueError if the supplied bootstrap CI is not
    strictly positive (an outcome-B/C result must never be turned into a LIVE
    record), the timestamp is malformed, ttl_days is non-positive, or no
    operator token is available to sign with.
    """
    if not (ci_lo < ci_hi) or ci_lo <= 0:
        raise ValueError(
            f"refusing to build LIVE record: block-bootstrap CI [{ci_lo},{ci_hi}] "
            "must be strictly positive (lo>0); outcome B/C stays in SHADOW")
    if int(ttl_days) <= 0:
        raise ValueError(f"ttl_days must be positive, got {ttl_days!r}")
    issued = _parse_utc(utc_iso)
    if issued is None:
        raise ValueError(f"utc_iso must be an ISO-8601 UTC timestamp, got {utc_iso!r}")
    not_after = issued + timedelta(days=int(ttl_days))
    rec = {
        "version": RECORD_VERSION,
        "decision": "outcome_a_go_live",
        "arms": list(arms),
        "block_bootstrap_ci": [float(ci_lo), float(ci_hi)],
        "config_src": config_src,
        "config_sha256": config_sha256(cfg),
        "operator": operator,
        "utc_iso": utc_iso,
        "not_after": not_after.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if extra:
        rec.update(extra)
    secret = token if token is not None else _operator_token()
    rec = sign_acceptance_record(rec, token=secret)
    # Fail loudly if anything about the assembled record is itself invalid.
    errs = validate_acceptance_record(
        rec, expected_config_sha256=config_sha256(cfg), now=issued, token=secret)
    if errs:
        raise ValueError("built acceptance record failed validation: " + "; ".join(errs))
    return rec


def startup_live_gate_errors(
    cfg: dict[str, Any], *, env_authorized: Optional[bool] = None,
    gate_path: Optional[str] = None, now: Optional[datetime] = None,
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
                record, expected_config_sha256=config_sha256(cfg), now=now)
            if errs:
                fatal.append(
                    "B-13: mode=LIVE but the acceptance record is invalid; "
                    "refusing to start: " + "; ".join(errs))
    return fatal, warnings


def live_entry_runtime_error(
    cfg: dict[str, Any], *, gate_path: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Optional[str]:
    """Runtime B-13 guard leaf (T-01/T-02).

    Pure decision: return ``None`` when a LIVE entry is permitted, or a
    human-readable reason when it must be blocked. Mirrors the boot gate's
    ``mode == "LIVE"`` branch but is safe to call on the hot path:

    * non-LIVE modes return ``None`` (this guard concerns only real entries;
      SHADOW/OFF are blocked earlier by the mode branch itself)
    * a missing/invalid/expired acceptance record (bound to *cfg* by sha256)
      returns ``"live_gate_record_invalid"``

    Callers MUST perform no order placement when a reason is returned.
    """
    if str(cfg.get("mode", "OFF")).upper() != "LIVE":
        return None
    record, _load_err = load_acceptance_record(gate_path)
    if record is None:
        return "live_gate_record_invalid"
    errs = validate_acceptance_record(
        record, expected_config_sha256=config_sha256(cfg), now=now)
    if errs:
        return "live_gate_record_invalid"
    return None
