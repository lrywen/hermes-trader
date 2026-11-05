#!/usr/bin/env python3
"""Mint a B-13 live-acceptance record from a validated §5.2 outcome (N-4).

This is the *supported* producer for ``live_acceptance_gate.json``. It closes
the gap where ``build_acceptance_record`` had only test callers: before this
script an outcome-A result could not be turned into a record without hand
writing JSON.

Pipeline (no trading side effects; this script only writes the gate file):

  1. load a ``validate_outcome`` result JSON (scripts/validate_outcome.py
     writes /tmp/validate_outcome.json), OR re-run validate_outcome when
     --run-validate is given with --input/--arms
  2. select the arm whose block-bootstrap 95% CI is strictly positive
     (lo > 0) AND whose DSR P passes; refuse otherwise — an outcome-B/C
     (negative expectancy) result can NEVER be minted into a LIVE record
  3. bind the record to the *current effective LIVE config* by sha256
  4. require explicit --yes (a dry run prints what would be written)
  5. atomic write to the gate path with mode 0600

Example:
  python scripts/validate_outcome.py --input logs/b3_filt.jsonl --arms filt
  python scripts/mint_live_gate_record.py --operator alice --yes

Safety: the current config need not already be LIVE — the record is built
against the effective view with mode pinned to LIVE, so it is ready for the
subsequent mode flip (which is itself B-13 gated).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

from hermes_trader.agents import config_store, live_gate
from hermes_trader.agents.config_store import (
    read_agent_config,
)
from hermes_trader.validation.trial_ledger import (
    TrialLedger,
    TrialLedgerError,
    deflated_sharpe_from_ledger,
)

DSR_ACCEPT_P = 0.95


def _refuse(code: str, message: str, **details) -> None:
    error = {"code": code, "message": message, "details": details}
    raise SystemExit("refusing to mint: " + json.dumps(
        {"error": error}, ensure_ascii=False, sort_keys=True))


def _load_validation_result(args: argparse.Namespace) -> dict:
    if args.result:
        with open(args.result, encoding="utf-8") as fh:
            return json.load(fh)
    if args.run_validate:
        import importlib.util
        here = os.path.dirname(os.path.abspath(__file__))
        spec = importlib.util.spec_from_file_location(
            "validate_outcome", os.path.join(here, "validate_outcome.py"))
        mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        return mod.evaluate(
            args.input, tuple(args.arms.split(",")),
            args.boot, args.seed, as_of=args.as_of,
            ledger_path=args.trial_ledger)
    # Default: the file validate_outcome writes.
    default = "/tmp/validate_outcome.json"
    if not os.path.exists(default):
        raise SystemExit(
            f"no validation result at {default}; run scripts/validate_outcome.py "
            "first, or pass --result PATH / --run-validate")
    with open(default, encoding="utf-8") as fh:
        return json.load(fh)


def _select_positive_arm(
        result: dict,
        *,
        as_of=None,
        ledger_path=None,
        ledger: TrialLedger | None = None) -> dict:
    """Recompute every candidate's DSR from current ledger evidence.

    A validation JSON is data, not authorization: stale/default trial counts,
    incomplete family coverage and legacy-only families are all rejected.
    """
    as_of = as_of or result.get("as_of")
    if result.get("verdict") == "fail_closed" or result.get("dsr_errors"):
        _refuse("validation_fail_closed", "validation result has DSR errors",
                as_of=as_of, dsr_errors=result.get("dsr_errors", []))
    rows = result.get("arms", [])
    ok = []
    coverage_errors = []
    legacy_statistical_passes = []
    for source in rows:
        if "skip" in source:
            continue
        arm_name = str(source.get("arm", ""))
        try:
            evidence = deflated_sharpe_from_ledger(
                observed_sharpe=float(source["sharpe"]),
                n_obs=int(source["days"]),
                arm=arm_name,
                paradigm="daily_outcome",
                as_of=as_of,
                ledger_path=ledger_path,
                ledger=ledger,
            )
            reported_dsr = float(source["dsr_p"])
            if abs(reported_dsr - evidence["dsr_p"]) > 0.000051:
                raise TrialLedgerError(
                    "validation_evidence_mismatch",
                    "validation DSR does not match current ledger evidence",
                    path=evidence["ledger_path"],
                    details={"arm": arm_name, "reported": reported_dsr,
                             "recomputed": evidence["dsr_p"]},
                )
        except TrialLedgerError as exc:
            error = exc.as_dict()
            error["arm"] = arm_name
            coverage_errors.append(error)
            continue
        except (KeyError, TypeError, ValueError) as exc:
            coverage_errors.append({
                "code": "invalid_validation_row", "arm": arm_name,
                "message": str(exc),
            })
            continue
        row = dict(source)
        row["n_trials"] = evidence["n_trials"]
        row["trial_evidence"] = evidence
        statistically_positive = (
            row.get("bb_positive")
            and float(row.get("bb_ci", [0, 0])[0]) > 0
            and evidence["dsr_p"] >= DSR_ACCEPT_P)
        if statistically_positive and not evidence["promotion_eligible"]:
            legacy_statistical_passes.append({
                "arm": arm_name, "block": evidence["promotion_block"]})
        if statistically_positive and evidence["promotion_eligible"]:
            ok.append(row)
    if coverage_errors:
        _refuse("dsr_fail_closed", "ledger coverage or evidence is incomplete",
                as_of=as_of, errors=coverage_errors)
    if not ok:
        code = ("legacy_import_only" if legacy_statistical_passes
                else "no_eligible_positive_arm")
        _refuse(
            code,
            "no preregistered arm has positive CI and passing DSR",
            verdict=result.get("verdict", "?"),
            as_of=as_of,
            legacy_statistical_passes=legacy_statistical_passes,
        )
    return ok[0]


def _live_effective_config() -> dict:
    """Effective config the gate binds, with mode pinned to LIVE."""
    raw = read_agent_config()
    # read_agent_config returns the merged effective view; flip mode only.
    cfg = dict(raw)
    cfg["mode"] = "LIVE"
    return cfg


def _atomic_write_0600(path: str, payload: str) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".gate-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    # mkstemp creates 0600 already, but enforce after replace as well.
    os.chmod(path, 0o600)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--result", help="validate_outcome JSON (default /tmp file)")
    ap.add_argument("--run-validate", action="store_true",
                    help="re-run validate_outcome instead of loading a file")
    ap.add_argument("--input", default="logs/b3_81coin_filt_ra.jsonl")
    ap.add_argument("--arms", default="filt")
    ap.add_argument("--boot", type=int, default=5000)
    ap.add_argument("--seed", type=int, default=20260920)
    ap.add_argument("--as-of", default=None,
                    help="must match the validation result as-of when both are set")
    ap.add_argument("--trial-ledger", default=None,
                    help="DSR trial ledger used to revalidate the signed mandate")
    ap.add_argument("--operator", default=os.environ.get("USER", "unknown"))
    ap.add_argument("--ttl-days", type=int, default=live_gate.DEFAULT_TTL_DAYS,
                    help="mandate horizon; the record stops authorizing NEW "
                         "entries after utc_iso+ttl (default 30)")
    ap.add_argument("--gate-path", default=live_gate.default_gate_path())
    ap.add_argument("--yes", action="store_true",
                    help="actually write; without it this is a dry run")
    args = ap.parse_args()

    if not os.environ.get("HERMES_OPERATOR_TOKEN", "").strip():
        raise SystemExit(
            "refusing to mint: HERMES_OPERATOR_TOKEN is not set. A LIVE mandate "
            "must be HMAC-signed with the operator token (same token the trading "
            "process uses to verify). Export it and re-run.")

    result = _load_validation_result(args)
    result_as_of = result.get("as_of")
    if args.as_of and result_as_of and args.as_of != result_as_of:
        _refuse("as_of_mismatch", "CLI and validation result as-of differ",
                cli_as_of=args.as_of, result_as_of=result_as_of)
    arm = _select_positive_arm(
        result, as_of=args.as_of or result_as_of,
        ledger_path=args.trial_ledger)
    lo, hi = arm["bb_ci"]

    live_cfg = _live_effective_config()
    utc_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    record = live_gate.build_acceptance_record(
        live_cfg,
        arms=[str(arm["arm"])],
        ci_lo=float(lo), ci_hi=float(hi),
        config_src=getattr(config_store, "CONFIG_PATH", "/data/.agent-config.json"),
        operator=str(args.operator),
        utc_iso=utc_iso,
        ttl_days=int(args.ttl_days),
        extra={"dsr_trial_evidence": arm["trial_evidence"]},
    )

    payload = json.dumps(record, ensure_ascii=False, indent=2)
    print(f"selected arm : {arm['arm']}  CI [{lo},{hi}]  DSR P={arm['dsr_p']}")
    print(f"gate path    : {args.gate_path}")
    print(f"config sha   : {record['config_sha256']}")
    print(f"valid until  : {record['not_after']}  (ttl {args.ttl_days}d)")
    if not args.yes:
        print("\n[dry run] no file written. Re-run with --yes to mint.")
        print(payload)
        return

    _atomic_write_0600(args.gate_path, payload)
    print(f"\n[minted] {args.gate_path} (mode 0600). The B-13 record is ready; "
          "flipping mode to LIVE is now permitted by the write guard.")


if __name__ == "__main__":
    sys.exit(main())
