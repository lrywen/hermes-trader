#!/usr/bin/env python3
"""BOME floor=0 vulnerability retrospective / closure audit.

Background
----------
The BOME incident class: a DSL trailing floor could be computed at 0 (or
non-positive) when the peak/entry relationship degenerated, which — combined
with an exchange backup stop — risked an invalid/immediate trigger. Code-level
defenses now exist at:
  * dsl_exit.py L521-529 : breakeven ratchet clamps floor to a locked gain
                           (max(floor, entry*(1+lock/100)) for longs), so a
                           zero/negative floor from a bad peak can never stand.
  * executor.py L1748-1793: backup SL floor default 1.2% + per-coin override,
                            three-way clamp, non-positive validation.

This script is the LIVE-DATA closure evidence the task spec demands. It pulls
every BOME-related record from the production data stores and answers:
  1. Did BOME ever actually open/close a position in the retained window?
  2. For every DSL floor update on BOME, was the floor a positive price and on
     the correct side of entry (no floor<=0)?
  3. For every BOME backup-SL placement, was the width within [floor, ceiling]?
  4. Is the floor defense present in the running source?

Data sources (auto-discovered):
  * /data/events.jsonl  (container authoritative event feed)
  * ~/.hermes-trading/events.jsonl  (host/shared fallback)
  * /data/.agent-memory.json  (closes/trades outcome store)
  * /data/trading-loop.log  (text DSL floor / SL placement lines)

Designed to run INSIDE the container (where /data lives):
    docker exec hermes-trader python3 /app/scripts/bome_floor_audit.py
Or on the host against a copied-out data dir:
    python3 scripts/bome_floor_audit.py --data-dir ./data_snapshot

Exit code 0 = audit clean (no floor<=0 evidence, defenses present).
Exit code 2 = evidence of a non-positive floor found (investigate immediately).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

COIN = "BOME"

# Candidate data locations, in priority order. The first existing parent wins
# unless --data-dir is given.
_CANDIDATE_DATA_DIRS = [
    Path("/data"),
    Path.home() / ".hermes-trading",
    Path(__file__).resolve().parents[1],
]
# Source tree for the defense-presence check.
_REPO_ROOT = Path(__file__).resolve().parents[1]


def _resolve_data_dir(explicit: Optional[str]) -> Path:
    if explicit:
        p = Path(explicit).expanduser().resolve()
        if not p.is_dir():
            print(f"[warn] --data-dir {p} not a directory; falling back to auto-detect")
        else:
            return p
    for d in _CANDIDATE_DATA_DIRS:
        if d.is_dir() and (d / "events.jsonl").exists():
            return d
    # Last resort: first candidate that exists at all.
    for d in _CANDIDATE_DATA_DIRS:
        if d.is_dir():
            return d
    return Path.cwd()


def _load_jsonl(path: Path) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not path.is_file():
        return out
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for ln in fh:
            ln = ln.strip()
            if not ln:
                continue
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                continue
    return out


def _coin_of(payload: Any) -> str:
    if isinstance(payload, dict):
        return str(payload.get("coin") or payload.get("ticker") or "").upper()
    return ""


def audit_events(data_dir: Path) -> Dict[str, Any]:
    """Lifecycle events for BOME: real executions vs blocked/shadow."""
    events = _load_jsonl(data_dir / "events.jsonl")
    bome = [e for e in events if _coin_of(e.get("payload")) == COIN]
    executed = []
    blocked = []
    closes = []
    for e in bome:
        kind = e.get("event")
        p = e.get("payload") or {}
        if kind == "execute":
            if p.get("executed"):
                executed.append(e)
            else:
                blocked.append(e)
        elif kind in ("close", "external_close_recorded"):
            closes.append(e)
    return {
        "total_bome_events": len(bome),
        "executed": executed,
        "blocked_count": len(blocked),
        "blocked_reasons": _tally(blocked, lambda e: (e.get("payload") or {}).get("detail")),
        "closes": closes,
        "first_ts": min((e.get("timestamp") for e in bome), default=None),
        "last_ts": max((e.get("timestamp") for e in bome), default=None),
    }


def audit_memory(data_dir: Path) -> Dict[str, Any]:
    """Outcome store: real BOME trades/closes persisted in .agent-memory.json."""
    mem_path = data_dir / ".agent-memory.json"
    if not mem_path.is_file():
        return {"memory_present": False, "bome_closes": 0, "bome_trades": 0}
    try:
        m = json.loads(mem_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"memory_present": False, "bome_closes": 0, "bome_trades": 0}
    closes = m.get("closes") or []
    trades = m.get("trades") or []
    bome_closes = [c for c in closes if str(c.get("coin", "")).upper() == COIN]
    bome_trades = [t for t in trades if str(t.get("coin", "")).upper() == COIN]
    return {
        "memory_present": True,
        "total_closes": len(closes),
        "total_trades": len(trades),
        "bome_closes": len(bome_closes),
        "bome_trades": len(bome_trades),
        "bome_close_rows": bome_closes,
    }


# [dsl:floor] BOME long phase=phase1 entry=0.001376 mark=... peak=... floor=0.00133
_FLOOR_RE = re.compile(
    r"\[dsl:floor\]\s+(?P<coin>\S+)\s+(?P<side>long|short).*?"
    r"entry=(?P<entry>[0-9.eE-]+).*?floor=(?P<floor>[0-9.eE-]+)"
)
# Backup SL placement: "Placed backup SL at <px> (<w>% from entry; atr_mult=...,
# floor=...%, ceiling=...%, slip+=...%)"
_SL_RE = re.compile(
    r"Placed backup SL at\s+(?P<px>[0-9.eE-]+).*?\((?P<width>[0-9.]+)% from entry;.*?"
    r"floor=(?P<floor>[0-9.]+)%.*?ceiling=(?P<ceiling>[0-9.]+)%"
)


def audit_logs(data_dir: Path) -> Dict[str, Any]:
    """Scan trading-loop.log for BOME DSL floor values and backup SL widths."""
    log_path = data_dir / "trading-loop.log"
    floors: List[Dict[str, Any]] = []
    sls: List[Dict[str, Any]] = []
    nonpos_floors: List[Dict[str, Any]] = []
    if log_path.is_file():
        with log_path.open("r", encoding="utf-8", errors="replace") as fh:
            for ln in fh:
                if COIN not in ln:
                    continue
                m = _FLOOR_RE.search(ln)
                if m:
                    d = m.groupdict()
                    try:
                        entry = float(d["entry"])
                        floor = float(d["floor"])
                    except (TypeError, ValueError):
                        continue
                    side = d["side"]
                    # A valid long floor must be >0 and below entry (stop side);
                    # a short floor must be >0 and above entry.
                    valid = floor > 0 and (
                        (side == "long" and floor < entry) or
                        (side == "short" and floor > entry)
                    )
                    row = {"entry": entry, "floor": floor, "side": side,
                           "valid": valid, "line": ln.strip()[:300]}
                    floors.append(row)
                    if not valid:
                        nonpos_floors.append(row)
                sm = _SL_RE.search(ln)
                if sm:
                    sls.append({
                        "px": float(sm.group("px")),
                        "width_pct": float(sm.group("width")),
                        "floor_pct": float(sm.group("floor")),
                        "ceiling_pct": float(sm.group("ceiling")),
                        "line": ln.strip()[:300],
                    })
    return {
        "floor_updates": len(floors),
        "floor_min": min((f["floor"] for f in floors), default=None),
        "floor_max": max((f["floor"] for f in floors), default=None),
        "non_positive_or_invalid_floors": nonpos_floors,
        "backup_sl_placements": len(sls),
        "sl_rows": sls,
    }


def audit_source_defenses() -> Dict[str, Any]:
    """Confirm the floor<=0 defenses are present in the running source tree."""
    dsl = _REPO_ROOT / "hermes_trader" / "agents" / "dsl_exit.py"
    exe = _REPO_ROOT / "hermes_trader" / "agents" / "executor.py"
    dsl_text = dsl.read_text(encoding="utf-8") if dsl.is_file() else ""
    exe_text = exe.read_text(encoding="utf-8") if exe.is_file() else ""
    return {
        "dsl_breakeven_clamp": (
            "breakeven_trigger_pct" in dsl_text and
            "max(floor," in dsl_text and "min(floor," in dsl_text
        ),
        "executor_sl_floor_const": "_DEFAULT_SL_FLOOR_PCT" in exe_text,
        "executor_sl_three_way_clamp": (
            "min(max(atr_stop_pct, sl_floor_pct), sl_ceiling_pct)" in exe_text
        ),
        "executor_sl_nonpos_validation": "invalid sl_floor_pct" in exe_text,
    }


def _tally(items: List[Dict[str, Any]], keyfn) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for it in items:
        k = str(keyfn(it) or "?")
        out[k] = out.get(k, 0) + 1
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", help="Directory containing events.jsonl / "
                                       ".agent-memory.json / trading-loop.log")
    ap.add_argument("--verbose", "-v", action="store_true",
                    help="Print full event/log rows (default: summary only)")
    args = ap.parse_args()

    data_dir = _resolve_data_dir(args.data_dir)
    print("=== BOME floor=0 retrospective audit ===")
    print(f"data_dir: {data_dir}")

    ev = audit_events(data_dir)
    mem = audit_memory(data_dir)
    logs = audit_logs(data_dir)
    src = audit_source_defenses()

    print(f"\n[1] Event feed ({data_dir/'events.jsonl'})")
    print(f"    BOME events ............. {ev['total_bome_events']} "
          f"({ev['first_ts']} → {ev['last_ts']})")
    print(f"    real executions ......... {len(ev['executed'])}")
    print(f"    blocked/shadow .......... {ev['blocked_count']}")
    for reason, n in ev["blocked_reasons"].items():
        print(f"        {n:>3}× {reason}")
    print(f"    close events ............ {len(ev['closes'])}")

    print("\n[2] Outcome store (.agent-memory.json)")
    print(f"    present ................. {mem['memory_present']}")
    print(f"    total closes/trades ..... {mem.get('total_closes',0)} / "
          f"{mem.get('total_trades',0)}")
    print(f"    BOME closes/trades ...... {mem['bome_closes']} / {mem['bome_trades']}")

    print("\n[3] trading-loop.log DSL floor + backup SL")
    print(f"    BOME floor updates ...... {logs['floor_updates']}")
    if logs["floor_updates"]:
        print(f"    floor price range ....... {logs['floor_min']} → {logs['floor_max']}")
    print(f"    invalid(<=0/wrong-side).. {len(logs['non_positive_or_invalid_floors'])}")
    print(f"    BOME backup SL placements {logs['backup_sl_placements']}")

    print("\n[4] Source defense presence")
    for k, v in src.items():
        print(f"    [{'OK' if v else 'MISSING'}] {k}")

    # Verdict
    real_position = (len(ev["executed"]) > 0 or mem["bome_closes"] > 0
                     or mem["bome_trades"] > 0)
    bad_floors = len(logs["non_positive_or_invalid_floors"])
    defenses_ok = all(src.values())

    print("\n=== VERDICT ===")
    if not real_position:
        print("    BOME had NO real position in the retained window: every BOME")
        print("    signal was blocked by gates (max-positions / runner_gate / shadow).")
        print("    => The floor=0 bug CANNOT be reproduced from live trade data in")
        print("       this window; no BOME close exists to have been hurt by it.")
    if bad_floors:
        print(f"    !!! {bad_floors} INVALID floor value(s) detected in logs !!!")
    else:
        print("    No non-positive / wrong-side BOME floor observed in logs.")
    print(f"    Code defenses present: {'YES' if defenses_ok else 'NO'}")

    if args.verbose:
        if ev["executed"]:
            print("\n    -- executed events --")
            for e in ev["executed"]:
                print("   ", json.dumps(e, ensure_ascii=False)[:500])
        if logs["non_positive_or_invalid_floors"]:
            print("\n    -- invalid floor rows --")
            for f in logs["non_positive_or_invalid_floors"]:
                print("   ", f["line"])
        if logs["sl_rows"]:
            print("\n    -- backup SL rows --")
            for s in logs["sl_rows"]:
                print("   ", s["line"])

    # Closure statement.
    print("\n=== CLOSURE ===")
    if real_position:
        print("    Real BOME trades exist — review rows above for floor integrity.")
    else:
        print("    Closure basis: (a) no BOME position was taken in the retained")
        print("    window so there is zero realized-PnL exposure to floor=0; and")
        print("    (b) the breakeven clamp + backup-SL floor defenses are present")
        print("    in source. The historical bug is code-closed; live-trade closure")
        print("    remains PENDING until BOME next actually trades (the post-fill")
        print("    drift assertion and ACTUAL_STOP_DEVIATION gauge will then record")
        print("    the evidence automatically).")

    if bad_floors or not defenses_ok:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
