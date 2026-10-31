#!/usr/bin/env python3
"""P0 — freeze an auditable baseline before any structural change.

Writes ONE timestamped directory (default ``/data/baselines/<UTC stamp>``)
containing everything a later change must be compared against:

  * ``manifest.json``       — git SHA/branch/dirty, runtime mode, config era_id
  * ``config.effective.json`` — the merged effective config (defaults + disk)
  * ``config.tracked.json``  — the exact subset hashed into ``era_id``
  * ``ledger.json``          — per-account wallet / fills / fee / PnL summary
  * ``data_inventory.json``  — top-level sizes + key state-file mtimes

Why the ledger is included: the 2026-10-31 "wallet does not deduct fees"
claim was wrong (``shadow_book._close_position`` books ``gross - fee``).
A baseline that freezes the wallet WITHOUT the fill-derived sums would
reproduce that mistake, so every account records both sides and the
``wallet_minus_sum_net`` offset.

Read-only: never writes into /data outside ``--out-dir``.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

UTC = timezone.utc
REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_DATA_DIR = os.environ.get("HERMES_DATA_DIR", "/data")
DEFAULT_OUT_DIR = os.path.join(DEFAULT_DATA_DIR, "baselines")
DEFAULT_SHADOW_BOOK = os.environ.get(
    "HERMES_SHADOW_BOOK_FILE",
    os.path.join(DEFAULT_DATA_DIR, ".shadow-book.json"),
)
DEFAULT_CONFIG_FILE = os.environ.get(
    "HERMES_AGENT_CONFIG_FILE",
    os.path.join(DEFAULT_DATA_DIR, ".agent-config.json"),
)

# Key runtime state files worth pinning (missing ones are recorded as null).
_KEY_FILES = (
    ".agent-memory.json",
    ".positions-snapshot.json",
    ".dsl-state.json",
    ".pending-sl-retries.json",
    "scheduler_state.json",
    "reconcile_status.json",
    ".historical-candles.json",
)


def _utc_stamp(now: Optional[datetime] = None) -> str:
    return (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")


def _git(*args: str) -> str:
    try:
        out = subprocess.run(
            ["git", *args], cwd=str(REPO_ROOT), capture_output=True,
            text=True, timeout=15, check=False,
        )
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def summarize_ledger(state: Any) -> dict[str, Any]:
    """Per-account wallet vs fill-derived sums from a shadow-book state dict.

    Pure. ``wallet_minus_sum_net`` is the baseline offset implied by resets:
    a healthy book keeps it constant across runs unless a manual reset or a
    booking bug happened.
    """
    accounts = state.get("accounts") if isinstance(state, dict) else None
    out: dict[str, Any] = {"accounts": {}, "version": None}
    if isinstance(state, dict):
        out["version"] = state.get("version")
    if not isinstance(accounts, dict):
        return out
    for name, acc in sorted(accounts.items()):
        if not isinstance(acc, dict):
            continue
        fills = acc.get("fills") or []
        fills = [f for f in fills if isinstance(f, dict)]
        closes = [f for f in fills if f.get("type") == "close"]
        opens = [f for f in fills if f.get("type") == "open"]

        def _fsum(rows: list[dict[str, Any]], key: str) -> float:
            total = 0.0
            for r in rows:
                try:
                    total += float(r.get(key) or 0.0)
                except (TypeError, ValueError):
                    continue
            return round(total, 6)

        wallet = round(float(acc.get("wallet_balance") or 0.0), 6)
        sum_net = _fsum(closes, "realized_pnl_usd")
        sum_gross = _fsum(closes, "gross_pnl_usd")
        sum_fee = _fsum(closes, "fee_usd")
        equity = acc.get("equity_curve") or []
        last_equity = equity[-1] if equity and isinstance(equity[-1], dict) else None
        out["accounts"][name] = {
            "wallet_balance": wallet,
            "closed_count": int(acc.get("closed_count") or len(closes)),
            "open_positions": len(acc.get("positions") or []),
            "pending_orders": len(acc.get("pending_orders") or []),
            "fills_retained": len(fills),
            "open_fills_retained": len(opens),
            "close_fills_retained": len(closes),
            "sum_gross_pnl_usd": sum_gross,
            "sum_fee_usd": sum_fee,
            "sum_net_pnl_usd": sum_net,
            # wallet - net == 0 iff the book started at 0; any non-zero value is
            # the accumulated reset offset, NOT a fee omission (fees are already
            # inside realized_pnl_usd).
            "wallet_minus_sum_net": round(wallet - sum_net, 6),
            "last_equity_point": (
                {"ts": last_equity.get("ts"), "equity": last_equity.get("equity")}
                if last_equity else None
            ),
        }
    return out


def inventory(path: str) -> dict[str, Any]:
    """Size/mtime inventory for a directory tree's direct children."""
    root = Path(path)
    entries: list[dict[str, Any]] = []
    if not root.is_dir():
        return {"path": path, "exists": False, "entries": []}
    for child in sorted(root.iterdir(), key=lambda p: p.name):
        try:
            st = child.stat()
        except OSError:
            continue
        item: dict[str, Any] = {
            "name": child.name,
            "kind": "dir" if child.is_dir() else "file",
            "mtime": datetime.fromtimestamp(st.st_mtime, UTC).isoformat(),
        }
        if child.is_dir():
            total = 0
            files = 0
            for dirpath, _dirnames, filenames in os.walk(child):
                for fn in filenames:
                    try:
                        total += os.stat(os.path.join(dirpath, fn)).st_size
                        files += 1
                    except OSError:
                        continue
            item["bytes"] = total
            item["files"] = files
        else:
            item["bytes"] = st.st_size
        entries.append(item)
    return {"path": str(root), "exists": True, "entries": entries}


def _key_files(data_dir: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name in _KEY_FILES:
        p = Path(data_dir) / name
        try:
            st = p.stat()
            out[name] = {
                "bytes": st.st_size,
                "mtime": datetime.fromtimestamp(st.st_mtime, UTC).isoformat(),
            }
        except OSError:
            out[name] = None
    return out


def _read_json(path: str) -> Any:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _config_era(config_file: str) -> dict[str, Any]:
    """era_id + tracked subset for the effective config at ``config_file``."""
    prev = os.environ.get("HERMES_AGENT_CONFIG_FILE")
    os.environ["HERMES_AGENT_CONFIG_FILE"] = config_file
    try:
        from hermes_trader.agents.config_store import (
            compute_config_era,
            read_agent_config,
        )
        effective = read_agent_config()
        era = compute_config_era(effective)
        return {"effective": effective, **era}
    finally:
        if prev is None:
            os.environ.pop("HERMES_AGENT_CONFIG_FILE", None)
        else:
            os.environ["HERMES_AGENT_CONFIG_FILE"] = prev


def _runtime_view(effective: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "mode", "max_trade_notional_usd", "max_total_notional_pct",
        "atr_risk_sizing", "regime_risk_overlay",
    )
    return {k: effective.get(k) for k in keys}


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    ap.add_argument("--out-dir", default=None)
    ap.add_argument("--shadow-book", default=None)
    ap.add_argument("--config-file", default=None)
    ap.add_argument("--skip-inventory", action="store_true")
    args = ap.parse_args(argv)

    data_dir = args.data_dir
    out_dir = Path(args.out_dir or os.path.join(data_dir, "baselines"))
    shadow_book = args.shadow_book or os.path.join(data_dir, ".shadow-book.json")
    config_file = args.config_file or os.path.join(data_dir, ".agent-config.json")
    if args.shadow_book is None and os.environ.get("HERMES_SHADOW_BOOK_FILE"):
        shadow_book = os.environ["HERMES_SHADOW_BOOK_FILE"]
    if args.config_file is None and os.environ.get("HERMES_AGENT_CONFIG_FILE"):
        config_file = os.environ["HERMES_AGENT_CONFIG_FILE"]

    stamp = _utc_stamp()
    target = out_dir / stamp
    target.mkdir(parents=True, exist_ok=True)

    era: dict[str, Any] = {"effective": {}, "era_id": None, "tracked": {}}
    if os.path.exists(config_file):
        era = _config_era(config_file)
    effective = era.get("effective") or {}

    ledger = summarize_ledger(_read_json(shadow_book) or {})
    inv = (
        {"skipped": True}
        if args.skip_inventory
        else {"data": inventory(data_dir), "key_files": _key_files(data_dir)}
    )

    manifest = {
        "schema": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "git": {
            "commit": _git("rev-parse", "HEAD"),
            "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
            "dirty": bool(_git("status", "--porcelain")),
        },
        "runtime": _runtime_view(effective),
        "era": {
            "era_id": era.get("era_id"),
            "tracked_keys": sorted((era.get("tracked") or {}).keys()),
        },
        "paths": {
            "data_dir": data_dir,
            "config_file": config_file,
            "shadow_book": shadow_book,
        },
        "files": {
            "config_effective": "config.effective.json",
            "config_tracked": "config.tracked.json",
            "ledger": "ledger.json",
            "data_inventory": None if args.skip_inventory else "data_inventory.json",
        },
    }

    def _dump(name: str, payload: Any) -> None:
        with open(target / name, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, sort_keys=True, default=str)
            f.write("\n")

    _dump("manifest.json", manifest)
    _dump("config.effective.json", effective)
    _dump("config.tracked.json", era.get("tracked") or {})
    _dump("ledger.json", ledger)
    if not args.skip_inventory:
        _dump("data_inventory.json", inv)

    print(json.dumps({
        "out_dir": str(target),
        "era_id": manifest["era"]["era_id"],
        "accounts": list(ledger.get("accounts", {})),
        "git_dirty": manifest["git"]["dirty"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
