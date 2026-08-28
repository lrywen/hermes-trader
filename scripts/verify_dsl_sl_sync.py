#!/usr/bin/env python3
"""End-to-end verification for the Phase-2 dynamic exchange-SL sync.

This is a SAFE, OFFLINE simulation — it never touches Hyperliquid. It:

  1. Loads the real DSLTracker / sync_exchange_sl code against a TEMP state
     file (so it cannot clobber production state in /data/.dsl-state.json).
  2. Registers a long position and seeds an initial exchange bracket
     (sl_oid / sl_px / sl_size), exactly as executor does after placing an SL.
  3. Monkeypatches `executor.modify_sl_trigger` with a fake cancel+replace that
     returns a NEW oid each call (mimicking HL's real batchModify behaviour).
  4. Drives a synthetic mark-price series through `check_all_positions` +
     `sync_exchange_sl` — exactly what trading_loop.py does every ~15s — and
     prints the floor / target SL / oid evolution, including:
       - Phase 1 (below protect_pct): NO move
       - Phase 2 trigger: first ratchet
       - Higher tiers: subsequent ratchets
       - The 30s per-coin throttle (driven by a fake clock)
       - The only-tightens guard (a price dip must NOT loosen the SL)
       - The min-move threshold
  5. Restart-recovery test: builds a v1-style tracker with NO oids and runs
     `backfill_brackets_from_exchange` against a fake openOrders payload, then
     asserts SL/TP oids are recovered and a plain reduce-only LIMIT order is
     NOT misclassified as a trigger.

Run:
    python3 scripts/verify_dsl_sl_sync.py
Exit code 0 = all assertions passed.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import types
from typing import Any, Dict, List, Optional

# ── 1. Isolate state file BEFORE importing dsl_exit ───────────────────────
_TMPDIR = tempfile.mkdtemp(prefix="dsl-sl-verify-")
os.environ["HERMES_DSL_STATE_FILE"] = os.path.join(_TMPDIR, ".dsl-state.json")
# Ensure no real keys are needed by the (stubbed) exchange path.
os.environ.setdefault("HYPERLIQUID_PRIVATE_KEY", "0x" + "0" * 64)

from hermes_trader.agents import dsl_exit  # noqa: E402
from hermes_trader.agents import executor    # noqa: E402
from hermes_trader.agents.dsl_exit import (  # noqa: E402
    ExitPolicy,
    register_position,
    check_all_positions,
    set_bracket,
    get_tracker,
    backfill_brackets_from_exchange,
    _active_positions,
)

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"
INFO = "\033[96mINFO\033[0m"

results: List[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    tag = PASS if cond else FAIL
    results.append(f"{tag}  {name}" + (f"  — {detail}" if detail else ""))
    print(f"  [{tag}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        raise AssertionError(name)


# ── 2. Fake exchange: batchModify that returns a fresh oid each call ──────
class FakeExchange:
    def __init__(self) -> None:
        self.next_oid = 900000
        self.calls: List[Dict[str, Any]] = []

    def modify_sl_trigger(
        self,
        is_long_position: bool,
        size: float,
        new_trigger_px: float,
        coin: str,
        oid: int,
    ) -> Dict[str, Any]:
        self.calls.append({
            "coin": coin, "old_oid": oid,
            "new_trigger_px": new_trigger_px, "size": size,
            "is_long": is_long_position,
        })
        self.next_oid += 1
        # Match the REAL parsed shape from _parse_order_result(accept_resting=True).
        return {"ok": True, "order_id": self.next_oid, "status": "resting"}


# ── 3. Controllable clock for the 30s throttle ────────────────────────────
class FakeClock:
    def __init__(self, start: float) -> None:
        self.t = start

    def time(self) -> float:
        return self.t

    def advance(self, sec: float) -> None:
        self.t += sec


def section(title: str) -> None:
    print(f"\n{'=' * 72}\n  {title}\n{'=' * 72}")


def main() -> int:
    # ── Seed a long position ─────────────────────────────────────────────
    section("SETUP — register long + seed initial exchange bracket")
    policy = ExitPolicy(
        max_loss_pct=2.5,
        protect_pct=1.5,
        retrace_threshold=0.30,
        phase2_tiers=[
            dsl_exit.RetraceTier(1.5, 0.30),
            dsl_exit.RetraceTier(5.0, 0.20),
            dsl_exit.RetraceTier(10.0, 0.10),
        ],
        # NOTE: hard_timeout_minutes MUST be >0 — check() treats `elapsed >= 0`
        # as an immediate hard-timeout exit when it's 0. Set a large value to
        # keep the scenario deterministic. stale_flat=0 means "off" (the check
        # is guarded by `> 0`), same for breakeven_trigger.
        hard_timeout_minutes=1_000_000.0,
        stale_flat_timeout_minutes=0.0,
        breakeven_trigger_pct=0.0,
        atr_stop_enabled=False,
        consecutive_breaches_required=1,
        noise_band_enabled=False,
    )
    ENTRY = 100.0
    COIN = "TESTETH"
    SIZE = 1.0
    # A real placement puts the static backup SL ~3% behind entry (ceiling).
    INITIAL_SL = ENTRY * 0.97

    tracker = register_position(COIN, "long", ENTRY, policy=policy, leverage=1)
    set_bracket(COIN, "long", sl_oid=12345, sl_px=INITIAL_SL, sl_size=SIZE)

    fake = FakeExchange()
    # sync_exchange_sl calls these as module-level names in executor.
    executor.modify_sl_trigger = fake.modify_sl_trigger  # type: ignore[assignment]

    clock = FakeClock(start=tracker.entry_time + 1.0)
    executor.time = clock  # type: ignore[assignment]

    print(f"  entry={ENTRY}  initial exchange SL oid=12345 px={INITIAL_SL:.4f}")
    print(f"  protect_pct={policy.protect_pct}  "
          f"throttle={executor._SL_MOVE_MIN_INTERVAL_SEC}s  "
          f"min_move={executor._SL_MOVE_MIN_BPS}bps  "
          f"buffer={executor._SL_BUFFER_BPS}bps")

    def tick(mark: float, label: str) -> Dict[str, Any]:
        """One scan cycle: check_all_positions then sync_exchange_sl."""
        mids = {COIN: mark}
        verdicts = check_all_positions(mids)
        before_calls = len(fake.calls)
        before_oid = tracker.sl_oid
        before_slpx = tracker.sl_px
        executor.sync_exchange_sl(mids)
        moved = len(fake.calls) > before_calls
        last = fake.calls[-1] if moved else None
        print(
            f"  {label:28s} mark={mark:8.4f}  phase_pct="
            f"{tracker._unrealized_pct(mark):+6.2f}%  "
            f"peak={tracker.peak_px:8.4f}  floor="
            f"{(tracker._last_floor or 0):8.4f}  sl_px="
            f"{(tracker.sl_px or 0):8.4f}  oid={tracker.sl_oid}"
            + (f"  -> MOVED old={before_oid} new={last['old_oid'] if last else '?'}->"
               f"{tracker.sl_oid} @ {last['new_trigger_px']:.4f}" if moved else "")
        )
        return {
            "moved": moved, "verdict_exit": any(v.exit for v in verdicts),
            "sl_px": tracker.sl_px, "oid": tracker.sl_oid,
        }

    # ── Phase 1: below protect — SL must NOT move ────────────────────────
    section("SCENARIO A — Phase 1 (price below protect_pct): SL must stay static")
    r = tick(100.5, "phase1, +0.5%")
    check("phase1 no SL move", not r["moved"], "SL must not move below protect_pct")
    check("phase1 oid unchanged", r["oid"] == 12345)

    r = tick(101.0, "phase1 edge, +1.0% (<1.5%)")
    check("phase1 edge no SL move", not r["moved"])

    # ── Phase 2 first trigger: just above protect ────────────────────────
    section("SCENARIO B — Phase 2 first trigger (>= protect_pct)")
    r = tick(102.0, "phase2 first trigger, +2.0%")
    check("phase2 first move happened", r["moved"],
          "floor ratchets, exchange SL pulled up behind it")
    check("new oid persisted after cancel+replace", r["oid"] != 12345 and r["oid"] is not None)
    check("SL moved UP (tightened) for a long", r["sl_px"] > INITIAL_SL)
    # SL trails floor by buffer on the adverse side: target = floor*(1-buffer/1e4).
    expected_target = tracker._last_floor * (1.0 - executor._SL_BUFFER_BPS / 10_000.0)
    check("SL target trails floor by buffer",
          abs(r["sl_px"] - expected_target) < 1e-9,
          f"sl_px={r['sl_px']:.6f} expected={expected_target:.6f}")

    # ── Throttle: immediate second tick within 30s must be skipped ───────
    section("SCENARIO C — 30s per-coin throttle")
    r_throttled = tick(102.5, "phase2, +2.5% but <30s elapsed")
    check("throttled within 30s", not r_throttled["moved"],
          "second batchModify must be blocked by throttle window")

    # ── Advance clock >30s, push into a higher tier ──────────────────────
    section("SCENARIO D — advance clock + push to higher tier (new ratchet)")
    clock.advance(31.0)
    r2 = tick(106.0, "phase2 tier2, +6.0% (after 31s)")
    check("ratchet after throttle window", r2["moved"])
    check("SL tightened further", r2["sl_px"] > (r["sl_px"] or 0))
    check("oid rotated again", r2["oid"] != r["oid"])
    oid_after_ratchet = r2["oid"]
    slpx_after_ratchet = r2["sl_px"]

    # ── Only-tightens: price dips but floor ratchet stays — SL must NOT ──
    # move down, even though the current tick's target could be lower.
    section("SCENARIO E — only-tightens guard (price dip must NOT loosen SL)")
    clock.advance(31.0)
    r_dip = tick(103.0, "pull-back to +3.0% (peak stays at 106)")
    # The floor is ratcheted to peak-based tier; even if target recomputes, the
    # guard rejects any move that isn't strictly tighter than current sl_px.
    if r_dip["moved"]:
        check("dip move is still upward", r_dip["sl_px"] >= slpx_after_ratchet)
    else:
        check("dip produced no loosening move", True,
              "SL held at ratcheted level (target not tighter)")
    check("SL never moved DOWN for a long",
          tracker.sl_px >= slpx_after_ratchet - 1e-9)

    # ── Min-move threshold: tiny peak increase past the same tier ────────
    section("SCENARIO F — min-move threshold (sub-threshold ratchet skipped)")
    clock.advance(31.0)
    # Tiny move that changes floor by < 15bps of entry — should be skipped.
    r_tiny = tick(106.05, "tiny +6.05% (floor change < 15bps)")
    check("sub-threshold move skipped", not r_tiny["moved"],
          f"move_bps must be < {executor._SL_MOVE_MIN_BPS}")

    # ── Summary of fake exchange calls ───────────────────────────────────
    section("EXCHANGE CALL LOG (fake batchModify)")
    for i, c in enumerate(fake.calls, 1):
        print(f"  #{i}  {c['coin']}  old_oid={c['old_oid']}  "
              f"new_px={c['new_trigger_px']:.4f}  size={c['size']}  long={c['is_long']}")
    check("at least 2 real moves (first + higher tier)", len(fake.calls) >= 2,
          f"got {len(fake.calls)} modify calls")

    # ── Persistence: state file must contain v2 bracket fields ───────────
    section("PERSISTENCE — state file carries bracket oids (v2)")
    with open(os.environ["HERMES_DSL_STATE_FILE"]) as f:
        state = json.load(f)
    check("state version is 2", state.get("version") == 2, f"got {state.get('version')}")
    pos = next(p for p in state["positions"] if p["coin"] == COIN)
    check("sl_oid persisted", pos.get("sl_oid") is not None)
    check("sl_px persisted", pos.get("sl_px") is not None)
    check("sl_size persisted", pos.get("sl_size") == SIZE)
    check("persisted sl_oid matches in-memory", pos.get("sl_oid") == tracker.sl_oid)

    # ── Restart recovery: backfill missing oids from openOrders ──────────
    section("RESTART RECOVERY — backfill_brackets_from_exchange")
    # Build a SECOND tracker that looks like a v1 reload (no oids).
    _active_positions.clear()
    rec = register_position("BACKFILL", "long", 200.0, policy=policy, leverage=1)
    check("tracker starts with no sl_oid", rec.sl_oid is None)
    check("tracker starts with no tp_oid", rec.tp_oid is None)

    entry = rec.entry_px
    # IMPORTANT: mirror REAL mainnet openOrders format. HL does NOT return
    # `triggerPx` or `orderType` for resting market-tpsl orders — only `limitPx`
    # (which carries the trigger price), `side`, `sz`, `reduceOnly`. The backfill
    # must classify SL vs TP by price-relative-to-entry, not by a trigger marker.
    fake_orders = [
        # reduce-only SL BELOW entry (sell-side for a long).
        {"coin": "BACKFILL", "oid": 555001, "side": "A", "sz": "1.0",
         "reduceOnly": True, "limitPx": f"{entry * 0.97}"},
        # reduce-only TP ABOVE entry.
        {"coin": "BACKFILL", "oid": 555002, "side": "B", "sz": "1.0",
         "reduceOnly": True, "limitPx": f"{entry * 1.02}"},
        # DECOY: a NON-reduce-only resting order (an entry/opening order) for
        # the same coin. It MUST be ignored — backfill only claims reduceOnly
        # brackets, never an unrelated resting entry.
        {"coin": "BACKFILL", "oid": 555003, "side": "B", "sz": "1.0",
         "reduceOnly": False, "limitPx": f"{entry * 1.05}"},
        # DECOY: a reduce-only order for a coin we do NOT hold — wrong coin.
        {"coin": "OTHER", "oid": 555004, "side": "A", "sz": "1.0",
         "reduceOnly": True, "limitPx": "1.0"},
    ]
    dsl_exit._fetch_open_orders = lambda user: fake_orders  # type: ignore[assignment]
    n = backfill_brackets_from_exchange("0xfakeuser")
    check("backfill reported 2 updates", n == 2, f"got {n}")
    check("SL oid recovered (real format, no triggerPx field)",
          rec.sl_oid == 555001, f"got {rec.sl_oid}")
    check("TP oid recovered (real format, no triggerPx field)",
          rec.tp_oid == 555002, f"got {rec.tp_oid}")
    check("SL px recovered", rec.sl_px is not None and abs(rec.sl_px - entry * 0.97) < 1e-9)
    check("non-reduceOnly entry decoy NOT used as bracket",
          rec.sl_oid != 555003 and rec.tp_oid != 555003)
    check("other-coin decoy NOT used",
          rec.sl_oid != 555004 and rec.tp_oid != 555004)

    # ── Idempotency: backfill must not overwrite an existing oid ─────────
    section("RECOVERY IDEMPOTENCY — never overwrite a known oid")
    dsl_exit._fetch_open_orders = lambda user: [  # type: ignore[assignment]
        {"coin": "BACKFILL", "oid": 999999, "side": "A", "sz": "1.0",
         "reduceOnly": True, "triggerPx": f"{entry * 0.95}",
         "orderType": {"trigger": {"isMarket": True, "tpsl": "sl"}}},
    ]
    n2 = backfill_brackets_from_exchange("0xfakeuser")
    check("backfill no-op when oid already known", n2 == 0, f"got {n2}")
    check("existing sl_oid preserved", rec.sl_oid == 555001)

    # ── Done ─────────────────────────────────────────────────────────────
    section("RESULT")
    failed = [line for line in results if line.startswith(FAIL)]
    if failed:
        print(f"\n{FAIL}  {len(failed)} check(s) failed:\n")
        for line in failed:
            print("   " + line)
        return 1
    print(f"\n{PASS}  All {len(results)} checks passed. "
          f"State file was isolated at: {os.environ['HERMES_DSL_STATE_FILE']}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as e:
        print(f"\n{FAIL}  Aborted on failed assertion: {e}")
        sys.exit(1)
