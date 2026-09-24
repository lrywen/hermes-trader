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
    CANONICAL_DEFAULTS,
    _deep_merge,
    read_agent_config,
)

DSR_ACCEPT_P = 0.95


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
            args.boot, args.seed, args.n_trials)
    # Default: the file validate_outcome writes.
    default = "/tmp/validate_outcome.json"
    if not os.path.exists(default):
        raise SystemExit(
            f"no validation result at {default}; run scripts/validate_outcome.py "
            "first, or pass --result PATH / --run-validate")
    with open(default, encoding="utf-8") as fh:
        return json.load(fh)


def _select_positive_arm(result: dict) -> dict:
    rows = result.get("arms", [])
    ok = [
        r for r in rows
        if "skip" not in r
        and r.get("bb_positive")
        and float(r.get("bb_ci", [0, 0])[0]) > 0
        and float(r.get("dsr_p", 0)) >= DSR_ACCEPT_P
    ]
    if not ok:
        verdict = result.get("verdict", "?")
        raise SystemExit(
            f"refusing to mint: verdict={verdict}; no arm has a strictly "
            "positive block-bootstrap CI (lo>0) with DSR P>= "
            f"{DSR_ACCEPT_P}. An outcome-B/C (negative-expectancy) result can "
            "never become a LIVE record.")
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
    ap.add_argument("--n-trials", type=int, default=16)
    ap.add_argument("--operator", default=os.environ.get("USER", "unknown"))
    ap.add_argument("--gate-path", default=live_gate.default_gate_path())
    ap.add_argument("--yes", action="store_true",
                    help="actually write; without it this is a dry run")
    args = ap.parse_args()

    result = _load_validation_result(args)
    arm = _select_positive_arm(result)
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
    )

    payload = json.dumps(record, ensure_ascii=False, indent=2)
    print(f"selected arm : {arm['arm']}  CI [{lo},{hi}]  DSR P={arm['dsr_p']}")
    print(f"gate path    : {args.gate_path}")
    print(f"config sha   : {record['config_sha256']}")
    if not args.yes:
        print("\n[dry run] no file written. Re-run with --yes to mint.")
        print(payload)
        return

    _atomic_write_0600(args.gate_path, payload)
    print(f"\n[minted] {args.gate_path} (mode 0600). The B-13 record is ready; "
          "flipping mode to LIVE is now permitted by the write guard.")


if __name__ == "__main__":
    sys.exit(main())
