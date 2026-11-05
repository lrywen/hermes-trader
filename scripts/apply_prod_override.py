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

    # reverse direction: backflow the live config into the tracked snapshot
    python scripts/apply_prod_override.py --capture \
        --config /home/ldy/hermes-deploy/.agent-config.json

The write goes through config_store.write_agent_config (same atomic + backup
path the app uses), and the snapshot is validated with the production strict
validator before anything is written.  ``--capture`` performs the opposite
(and routine) direction: production tuning evolves on the persistent volume,
so the tracked snapshot must be refreshed FROM live instead of only being
re-applied TO it.  It strips ``mode`` (deployment state, never versioned) and
keeps the snapshot's descriptive ``_comment``.
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


def _flatten(node, prefix: str = "") -> dict:
    out = {}
    if isinstance(node, dict) and node:
        for k, v in node.items():
            out.update(_flatten(v, f"{prefix}.{k}" if prefix else k))
    else:
        out[prefix] = node
    return out


def _unflatten(flat: dict) -> dict:
    root: dict = {}
    for path, value in flat.items():
        node = root
        parts = path.split(".")
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = value
    return root


_MISSING = object()


def _canonical_delta(live: dict) -> dict:
    """Leaves of *live* that must be stored in the override snapshot.

    The snapshot is an OVERRIDE, not a full config copy: storing leaves that
    merely echo today's canonical value would freeze them forever and silently
    undo future canonical fixes (e.g. the 2026-10-18 detox that removed
    runner_entry_gate.min_confidence).  Comparison is byte-strict so a genuine
    type change (int 4 vs float 4.0) is still captured as a delta.

    ``None`` is ALWAYS kept: ``_deep_merge`` treats it as a deletion marker, so
    dropping it would resurrect the canonical value instead of deleting the
    key (era-visible: ``dsl_exit.ignite_grace_minutes``).
    """
    live_flat = _flatten(live)
    canon_flat = _flatten(CANONICAL_DEFAULTS)
    keep = {}
    for path, value in live_flat.items():
        if value is not None:
            canon = canon_flat.get(path, _MISSING)
            if canon is not _MISSING and (
                    json.dumps(value, sort_keys=True) == json.dumps(canon, sort_keys=True)):
                continue
        keep[path] = value
    return _unflatten(keep)


def _effective_view(raw: dict) -> dict:
    """Mirror read_agent_config(): deep-merge, then re-materialize top-level
    canonical None stubs (nested nulls stay deleted, per _deep_merge)."""
    merged = config_store._deep_merge(CANONICAL_DEFAULTS, raw)
    for key, default in CANONICAL_DEFAULTS.items():
        if default is None and key not in merged:
            merged[key] = None
    return merged


def _tag(value) -> str:
    return "<ABSENT>" if value is _MISSING else json.dumps(value, sort_keys=True)


def _capture(snapshot_path: str, config_path: str) -> int:
    """Backflow the live config at *config_path* into the tracked snapshot."""
    with open(config_path, encoding="utf-8") as f:
        live = json.load(f)
    carried_mode = live.pop("mode", None)
    with open(snapshot_path, encoding="utf-8") as f:
        previous = json.load(f)
    if previous.get("_comment"):
        live["_comment"] = previous["_comment"]

    errs = config_store.validate_config_dict(live, strict_keys=True)
    delta = _canonical_delta(live)
    # Safety net: the snapshot's effective view must equal live's leaf-for-leaf.
    want = _flatten(_effective_view(live))
    got = _flatten(_effective_view(delta))
    bad = [p for p in set(want) | set(got)
           if _tag(want.get(p, _MISSING)) != _tag(got.get(p, _MISSING))]

    print(f"source   : {config_path}")
    print(f"snapshot : {snapshot_path}")
    print(f"mode     : {carried_mode!r} stripped (deployment state)")
    print(f"validation: {'OK' if not errs else str(len(errs)) + ' message(s)'}")
    for e in errs[:20]:
        print("   -", e)

    diffs = _diff_leaves(previous, delta)
    print(f"snapshot leaves stored (delta vs canonical): {len(_flatten(delta))}")
    print(f"leaves changed vs previous snapshot: {len(diffs)}")
    for p, c, l in sorted(diffs, key=lambda x: x[0]):
        cs = json.dumps(c, ensure_ascii=False)[:36]
        ls = json.dumps(l, ensure_ascii=False)[:36]
        print(f"   {p}: {cs} -> {ls}")

    if errs:
        print("\nREFUSING to capture: live config failed strict validation.",
              file=sys.stderr)
        return 2
    if bad:
        print(f"\nREFUSING to capture: delta view differs from live at {bad[:5]}",
              file=sys.stderr)
        return 2

    tmp = snapshot_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        # sort_keys matches the artifact's existing canonical form, so a capture
        # diff shows only leaves that actually changed, not a whole-file reorder.
        json.dump(delta, f, indent=2, ensure_ascii=False, sort_keys=True)
        f.write("\n")
    os.replace(tmp, snapshot_path)
    print(f"\ncaptured live config -> {snapshot_path}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--snapshot", default=DEFAULT_SNAPSHOT)
    ap.add_argument("--config", default=config_store.CONFIG_PATH,
                    help="target config path (default: config_store.CONFIG_PATH)")
    ap.add_argument("--apply", action="store_true",
                    help="write the snapshot (default is dry-run)")
    ap.add_argument("--capture", action="store_true",
                    help="reverse direction: read --config and write it to --snapshot "
                         "(strips 'mode', keeps the snapshot _comment)")
    ap.add_argument("--with-mode", action="store_true",
                    help="also carry the runtime 'mode' (OFF/SHADOW/LIVE) from the "
                         "snapshot. Default strips it so a rebuild stays on the safe "
                         "canonical OFF and never starts trading without an explicit choice.")
    args = ap.parse_args(argv)

    if args.capture:
        if args.apply or args.with_mode:
            ap.error("--capture is incompatible with --apply/--with-mode")
        return _capture(args.snapshot, args.config)

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
            config_store.write_agent_config(snapshot, via="apply_prod_override")
        finally:
            config_store.CONFIG_PATH = tmp
    else:
        config_store.write_agent_config(snapshot, via="apply_prod_override")
    print(f"\napplied snapshot -> {args.config} (previous file backed up).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
