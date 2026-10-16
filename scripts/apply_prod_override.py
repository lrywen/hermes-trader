#!/usr/bin/env python3
"""Apply the versioned production override snapshot to the live config path.

Why this exists
---------------
``CANONICAL_DEFAULTS`` (config_defaults.py) is the audited, sentinel-guarded
baseline (it must byte-match the code-module literals; ~40 no-drift tests
enforce that). Production tuning must NOT be baked into canonical — it lives on
the persistent volume at ``/data/.agent-config.json`` and deep-merges over the
canonical defaults at read time (config_store.read_agent_config).

``deploy/prod_override.json`` is the versioned snapshot of that production
override (captured from 192.168.124.65). Re-applying it after a volume reset /
container rebuild reproduces the live tuning without touching canonical.

Usage
-----
    # dry-run: show validation + which leaves differ from canonical
    python scripts/apply_prod_override.py

    # actually write (backs up the current file first via config_store)
    python scripts/apply_prod_override.py --apply

    # use a different snapshot / target config path
    python scripts/apply_prod_override.py --snapshot deploy/prod_override.json \
        --config /data/.agent-config.json

The write goes through config_store.write_agent_config (same atomic + backup
path the app uses), and the snapshot is validated with the production strict
validator before anything is written.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hermes_trader.agents import config_store  # noqa: E402
from hermes_trader.agents.config_defaults import CANONICAL_DEFAULTS  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_SNAPSHOT = os.path.join(REPO_ROOT, "deploy", "prod_override.json")


def _norm(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        try:
            return float(v)
        except (TypeError, ValueError):
            return v
    return v


def _diff_leaves(base: dict, over: dict, path: str = ""):
    out = []
    if isinstance(base, dict) and isinstance(over, dict):
        for k in set(base) | set(over):
            np = f"{path}.{k}" if path else k
            if k in base and k in over:
                out.extend(_diff_leaves(base[k], over[k], np))
            elif k in over:
                out.append((np, "<absent>", over[k]))
    elif isinstance(base, list) and isinstance(over, list):
        for i, (x, y) in enumerate(zip(base, over)):
            out.extend(_diff_leaves(x, y, f"{path}[{i}]"))
        if len(base) != len(over):
            out.append((path, base, over))
    elif _norm(base) != _norm(over):
        out.append((path, base, over))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--snapshot", default=DEFAULT_SNAPSHOT)
    ap.add_argument("--config", default=config_store.CONFIG_PATH,
                    help="target config path (default: config_store.CONFIG_PATH)")
    ap.add_argument("--apply", action="store_true",
                    help="write the snapshot (default is dry-run)")
    ap.add_argument("--with-mode", action="store_true",
                    help="also carry the runtime 'mode' (OFF/SHADOW/LIVE) from the "
                         "snapshot. Default strips it so a rebuild stays on the safe "
                         "canonical OFF and never starts trading without an explicit choice.")
    args = ap.parse_args(argv)

    with open(args.snapshot) as f:
        snapshot = json.load(f)

    # Runtime trading mode is deployment STATE, not strategy tuning. The canonical
    # snapshot must not carry it: strip by default so an --apply can never silently
    # flip a rebuilt/another host into SHADOW/LIVE. Require an explicit opt-in.
    carried_mode = snapshot.get("mode")
    if "mode" in snapshot:
        if args.with_mode:
            print(f"WARNING: --with-mode set; carrying runtime mode={carried_mode!r}.")
        else:
            snapshot.pop("mode")
            print(f"NOTE: snapshot 'mode'={carried_mode!r} stripped (add --with-mode to keep); "
                  "target will use the safe canonical default OFF.")

    errs = config_store.validate_config_dict(snapshot, strict_keys=True)
    print(f"snapshot : {args.snapshot} ({len(snapshot)} keys)")
    print(f"target   : {args.config}")
    print(f"mode     : {'APPLY' if args.apply else 'DRY-RUN'}")
    print(f"validation: {'OK' if not errs else str(len(errs)) + ' message(s)'}")
    for e in errs[:20]:
        print("   -", e)

    diffs = _diff_leaves(CANONICAL_DEFAULTS, snapshot)
    print(f"leaves differing from CANONICAL_DEFAULTS: {len(diffs)}")
    for p, c, l in sorted(diffs, key=lambda x: x[0]):
        cs = json.dumps(c, ensure_ascii=False)[:36]
        ls = json.dumps(l, ensure_ascii=False)[:36]
        print(f"   {p}: {cs} -> {ls}")

    if not args.apply:
        print("\ndry-run only — re-run with --apply to write.")
        return 0

    if errs:
        print("\nREFUSING to apply: snapshot failed strict validation.", file=sys.stderr)
        return 2

    if os.path.abspath(args.config) != os.path.abspath(config_store.CONFIG_PATH):
        os.makedirs(os.path.dirname(args.config) or ".", exist_ok=True)
        tmp = config_store.CONFIG_PATH
        config_store.CONFIG_PATH = args.config
        try:
            config_store.write_agent_config(snapshot)
        finally:
            config_store.CONFIG_PATH = tmp
    else:
        config_store.write_agent_config(snapshot)
    print(f"\napplied snapshot -> {args.config} (previous file backed up).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
