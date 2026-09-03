#!/usr/bin/env python3
"""Retest harness for the Phase 3 WS real-time mid substitution path WITH
real native perp positions, plus the intra-cycle exit-checkpoint wiring.

Why this script exists
----------------------
The `[dsl:ws-mid] N/M positions used WS real-time mid` line in
`dsl_exit.check_all_positions` is only emitted on a PARTIAL substitution
(0 < ws_substituted < total): a full substitution (all-native perps with a
fresh WS) and a zero substitution (WS down / all HIP-3) are both silent by
design. The earlier end-to-end runs rehydrated 0 trackers (flat account), so
the substitution branch never fired on a real held coin.

This script proves the branch with REAL data, read-only:
  1. Starts the SAME persistent allMids WebSocket the trading loop uses and
     waits until it is connected AND fresh (data_age < 2s, the Phase 3 gate).
  2. Fetches the live account and lists every REAL native perp position.
     - If the account holds >= 1 native perp, those coins are used directly:
       the WS carries them, so they MUST be substituted with the WS mid.
     - A single in-memory dummy tracker for a coin the WS does NOT carry
       ("ZZFAKE-PERP-9Q") is injected to force the PARTIAL case, guaranteeing
       the `[dsl:ws-mid] N/M` line fires even when every real position is a
       native perp. The dummy is priced via the REST `mids` fallback, exactly
       like a HIP-3 coin would be.
  3. Runs `check_all_positions(rest_mids)` and asserts the substitution log
     appears with the expected N/M.

Safety: trackers are inserted DIRECTLY into `dsl_exit._active_positions` (no
`register_position`, hence no `_save_state()` / no state-file write) and the
registry is restored before exit. No orders are placed or closed — this only
reads prices and evaluates the exit predicate.

Usage:
    python3 scripts/retest_ws_mid_with_native_perp.py
Env (optional, else read from .env.local):
    HYPERLIQUID_MASTER_ADDRESS=0x...
"""

from __future__ import annotations

import logging
import os
import sys
import time

# ── Make the package importable when run from the repo root ──────────────
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_master_address() -> str | None:
    addr = os.environ.get("HYPERLIQUID_MASTER_ADDRESS")
    if addr:
        return addr.strip()
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            ".env.local")
    try:
        with open(env_path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("HYPERLIQUID_MASTER_ADDRESS="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return None


class _LogCapture(logging.Handler):
    """Collects formatted log records so we can assert on the ws-mid line."""

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.records: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.records.append(record.getMessage())
        except Exception:
            pass


def _wait_ws_fresh(ws, timeout_s: float = 45.0) -> bool:
    """Block until the WS is connected and has data younger than 2s."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            if ws.is_connected() and ws.get_data_age_seconds() < 2.0:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    cap = _LogCapture()
    logging.getLogger().addHandler(cap)
    log = logging.getLogger("retest")

    from hermes_trader.agents import dsl_exit
    from hermes_trader.agents.dsl_exit import DSLTracker, check_all_positions
    from hermes_trader.client.exchange import get_all_hl_mids
    from hermes_trader.client.hl_client import fetch_account_state, start_ws_mids, stop_ws_mids

    failures: list[str] = []

    # ── 1. Start the persistent allMids WS (same instance as the loop) ────
    log.info("Starting persistent WS allMids feed ...")
    ws = start_ws_mids()
    if ws is None:
        log.error("FAIL: start_ws_mids() returned None — WS unavailable")
        return 2
    try:
        if not _wait_ws_fresh(ws):
            log.error("FAIL: WS did not become connected+fresh within timeout "
                      f"(connected={ws.is_connected()}, "
                      f"age={ws.get_data_age_seconds():.1f}s)")
            return 2
        log.info(f"OK: WS connected, data_age={ws.get_data_age_seconds():.2f}s")
        for probe in ("BTC", "ETH"):
            px = ws.get_price(probe)
            log.info(f"    WS mid {probe} = {px}")
            if not (px > 0.0):
                failures.append(f"WS price for {probe} missing ({px})")

        # ── 2. Fetch REAL native perp positions ───────────────────────────
        user = _load_master_address()
        real_coins: list[tuple[str, str, float]] = []  # (coin, side, entry)
        if user:
            try:
                state = fetch_account_state(user, include_hip3=False)
                for p in state.get("asset_positions", []) or []:
                    pos = p.get("position", {})
                    coin = pos.get("coin")
                    szi = float(pos.get("szi", "0") or 0)
                    entry = float(pos.get("entryPx") or 0)
                    if coin and abs(szi) > 1e-12 and entry > 0:
                        side = "long" if szi > 0 else "short"
                        real_coins.append((coin, side, entry))
                log.info(f"Live native perp positions: "
                         f"{[(c, s) for c, s, _ in real_coins] or 'NONE (flat)'}")
            except Exception as e:
                log.warning(f"fetch_account_state failed ({e}); continuing with "
                            f"synthetic native probe only")
        else:
            log.warning("HYPERLIQUID_MASTER_ADDRESS not set; cannot list real "
                        "positions — using BTC/ETH as native probes")

        # If the account is flat, still prove substitution on coins the WS
        # definitely carries (BTC/ETH) by treating them as synthetic natives.
        native_probes: list[tuple[str, str, float]] = list(real_coins)
        if not native_probes:
            for probe in ("BTC", "ETH"):
                px = ws.get_price(probe)
                if px > 0:
                    # enter slightly BELOW mark for a long → open in profit,
                    # so the exit predicate cannot fire and close anything.
                    native_probes.append((probe, "long", px * 0.995))

        # ── 3. REST snapshot (fallback price source) ──────────────────────
        rest_mids = get_all_hl_mids(include_hip3=False) or {}
        log.info(f"REST main-book mids fetched: {len(rest_mids)} coins")

        # ── 4. Inject in-memory trackers (NO state-file write) ────────────
        saved_registry = dict(dsl_exit._active_positions)
        dsl_exit._active_positions.clear()
        dummy_coin = "ZZFAKE-PERP-9Q"  # not on the native allMids channel
        try:
            n_native = 0
            for coin, side, _entry in native_probes:
                mark = ws.get_price(coin) or rest_mids.get(coin) or 0.0
                if not (mark > 0):
                    continue
                # fresh, in-profit long → no exit verdict possible
                t = DSLTracker(coin, "long", float(mark) * 0.995, time.time(),
                               None, leverage=1)
                dsl_exit._active_positions[f"{coin}_long"] = t
                n_native += 1

            # Dummy coin: absent from WS → MUST fall back to REST mids. Price
            # it via the REST dict (as a HIP-3 coin would be) and enter in
            # profit so it never exits.
            ref = float(ws.get_price("BTC") or rest_mids.get("BTC") or 1.0)
            rest_mids[dummy_coin] = ref
            dsl_exit._active_positions[f"{dummy_coin}_long"] = DSLTracker(
                dummy_coin, "long", ref * 0.995, time.time(), None, leverage=1)

            total = n_native + 1
            log.info(f"Injected {n_native} WS-carried native tracker(s) + 1 "
                     f"non-WS dummy → total={total}; expect ws-mid {n_native}/{total}")

            # ── 5. Run the REAL exit predicate inside a FRESH window ───────
            # The Phase 3 gate requires WS data_age < 2.0s AT CHECK TIME. The
            # REST snapshot fetch above takes several seconds and the allMids
            # feed only refreshes on push, so we re-run the predicate in a
            # tight retry: every iteration that sees a stale WS simply falls
            # back (silent, no substitution) and we retry on the next fresh
            # tick until the partial-substitution log is observed. No order
            # is ever at risk: every tracker is opened in profit → 0 verdicts.
            exits: list = []
            ws_mid_lines: list[str] = []
            for attempt in range(1, 31):
                age = ws.get_data_age_seconds()
                if age >= 2.0:
                    time.sleep(0.2)
                    continue
                cap.records.clear()
                exits = check_all_positions(rest_mids)
                ws_mid_lines = [r for r in cap.records if "[dsl:ws-mid]" in r]
                log.info(f"attempt {attempt}: WS data_age={age:.2f}s fresh, "
                         f"exits={len(exits)}, ws_mid_lines={len(ws_mid_lines)}")
                if ws_mid_lines:
                    break
                time.sleep(0.2)
            log.info(f"check_all_positions returned {len(exits)} exit verdict(s) "
                     f"(expected 0 — all trackers opened in profit)")
            if exits:
                failures.append(
                    f"unexpected exit verdicts: {[e.coin for e in exits]}")

            log.info("── captured [dsl:ws-mid] lines ──")
            for line in ws_mid_lines:
                log.info(line)
            if not ws_mid_lines:
                failures.append(
                    "NO [dsl:ws-mid] line emitted — substitution branch did "
                    "not fire (WS not fresh, or partial-substitution condition "
                    "not reached)")
            else:
                expected = f"{n_native}/{total}"
                if expected not in ws_mid_lines[-1]:
                    failures.append(
                        f"ws-mid line does not show expected {expected}: "
                        f"{ws_mid_lines[-1]}")
                else:
                    log.info(f"OK: partial substitution confirmed ({expected} "
                             f"native perps used WS real-time mid; dummy fell "
                             f"back to REST mids like a HIP-3 coin)")
        finally:
            # Restore the real registry; nothing was persisted.
            dsl_exit._active_positions.clear()
            dsl_exit._active_positions.update(saved_registry)

        # ── 6. Verify the intra-cycle checkpoint wiring (static) ───────────
        loop_path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "scripts", "trading_loop.py")
        perc_path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "hermes_trader", "agents", "perception.py")
        loop_src = open(loop_path, "r", encoding="utf-8").read()
        perc_src = open(perc_path, "r", encoding="utf-8").read()
        for label, needle in [
            ("research-loop checkpoint", '_exit_checkpoint(mids, tag=f"research:{coin}")'),
            ("cold-scan checkpoint hook", 'on_batch_complete=lambda'),
            ("checkpoint helper defined", "def _exit_checkpoint("),
            ("shared close path", "def _process_exits("),
        ]:
            if needle not in loop_src:
                failures.append(f"trading_loop.py missing wiring: {label} ({needle!r})")
        for label, needle in [
            ("scan_once on_batch_complete param", "on_batch_complete"),
            ("batch hook invocation", "on_batch_complete(completed, total)"),
        ]:
            if needle not in perc_src:
                failures.append(f"perception.py missing wiring: {label}")
        if not failures:
            log.info("OK: intra-cycle checkpoint wiring present in trading_loop "
                     "(research + cold-scan) and perception.scan_once hook")

    finally:
        stop_ws_mids()

    log.info("=" * 60)
    if failures:
        log.error("RETEST RESULT: FAIL")
        for f in failures:
            log.error(f"  - {f}")
        return 1
    log.info("RETEST RESULT: PASS — WS real-time mid substitution verified on "
             "native perp positions and checkpoint wiring confirmed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
