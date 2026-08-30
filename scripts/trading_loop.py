#!/usr/bin/env python3
"""Continuous trading loop for hermes-trader.

Per cycle: scan -> TA filter -> AI research -> execute. The TA filter
(`analyze_perception`, zero AI cost) gates the paid LLM call — only CONFIRMED
perceptions reach research. A perception whose `momentumBurst` trigger fired
bypasses the gate: a large fast move is always worth researching.

Every cycle and decision is appended to the session log (`session_log`), so
`status.py` and the hourly cron report show a live activity feed.

Flags (tolerant — unknown flags are ignored so legacy callers keep working):
  --env {prod,dev}  Currently informational; loaded from .env.local in CWD.
  --daemon          Currently informational; the loop already daemonizes via
                    `nohup ... &` / Hermes background. Kept for skill scripts.
"""
import argparse
import math
import os
import sys
import threading
import time
import logging

# Load .env.local (CWD-relative, matches skill restart command).
env_path = '.env.local'
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, _, val = line.partition('=')
                os.environ[key.strip()] = val.strip()

# Tolerant argparse — `--env prod --daemon` were silently dropped before.
# Now they're parsed (and ignored) instead of raising on stray flags some
# future callers might add.
_parser = argparse.ArgumentParser(add_help=False)
_parser.add_argument("--env", default="prod")
_parser.add_argument("--daemon", action="store_true")
_args, _unknown = _parser.parse_known_args()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s:%(name)s:%(message)s'
)

from hermes_trader.agents.perception import scan_once
from hermes_trader.agents.ta_filter import analyze_perception
from hermes_trader.agents.research import research
from hermes_trader.agents.executor import close_position_market, maybe_execute, monitor_exits, route_verdict, sync_exchange_sl, retry_pending_sl
from hermes_trader.agents.dsl_exit import active_position_coins, held_coins_missing_mids, rehydrate_from_exchange
from hermes_trader.agents.config import get_config
from hermes_trader.agents.config_store import read_agent_config, cfg_get
from hermes_trader.agents.memory import memory
from hermes_trader.client.exchange import get_all_hl_mids, prewarm_meta_cache, mid_feed_age_seconds, MID_FEED_MAX_STALE_S
from hermes_trader.client.universe import get_universe
from hermes_trader.client.hl_client import fetch_account_state, fetch_aggregate_contributions_since, resolve_user_address
from hermes_trader.positions_snapshot import write_snapshot
from hermes_trader.session_log import append as log_event
from hermes_trader.surge_postmortem import SurgeDetector, SurgeConfig

logger = logging.getLogger(__name__)

# Surge postmortem watcher (module-level singleton): detects a coin whose
# composite score explodes across cycles and auto-writes a full postmortem to
# the container log + /data/postmortems/. Never raises into the loop.
#
# The surge NOTIFY threshold is intentionally INDEPENDENT of the scan/trade
# gate (minCompositeScore, 54): we want to be alerted earlier (score>=40) even
# though a trade isn't actionable until 54. Override via HERMES_SURGE_MIN_SCORE.
_surge_min = float(os.environ.get("HERMES_SURGE_MIN_SCORE", "40"))
_surge_detector = SurgeDetector(SurgeConfig(min_score=_surge_min))


def _remaining_minutes(ms_remaining: float) -> int:
    """Human log label for a positive millisecond cooldown."""
    return max(1, int(math.ceil(max(0.0, ms_remaining) / 60_000)))

# ── Self-healing watchdog (armed FIRST, before any network I/O) ─────────────
# No external supervisor exists (restart.sh just launches). A local DNS/network
# outage froze the loop twice — once mid-scan, once during STARTUP (universe
# load / prewarm) where the watchdog wasn't armed yet, so it stayed hung ~58min.
# Arm it before any network call so BOTH a startup hang and a mid-scan hang
# self-heal via re-exec. `_last_progress_ts` is bumped after each completed scan
# cycle; if it goes stale > HERMES_WATCHDOG_TIMEOUT_S (default 600s, generous so
# a slow-but-progressing scan isn't killed) the process re-execs (startup
# rehydrates trackers from disk; the stacking backstop prevents a re-entry
# pyramid). A persistent DNS outage just re-execs every ~600s until it clears.
_last_progress_ts = time.time()
_watchdog_timeout_s = int(os.environ.get('HERMES_WATCHDOG_TIMEOUT_S', '600'))


def _pre_exec_flush(timeout_s: float = 3.0) -> None:
    """Best-effort persistence of in-memory state before a watchdog re-exec.

    The watchdog fires from its own thread while the MAIN thread is hung. If
    the main thread holds the DSL-state or memory file lock, calling the
    flush directly from here would block on flock() and defeat the self-heal.
    Run each flush in a short-lived daemon thread with a bounded join so a
    contended lock is abandoned rather than blocking the restart. A filled
    order whose DSL tracker wasn't yet flushed is reconciled on startup by
    rehydrate_from_exchange() anyway; this just narrows that window.
    """
    def _dsl() -> None:
        try:
            from hermes_trader.agents import dsl_exit
            dsl_exit._save_state()
        except Exception as e:
            logger.debug(f"[watchdog] pre-exec dsl flush failed: {e}")

    def _mem() -> None:
        try:
            memory.flush()
        except Exception as e:
            logger.debug(f"[watchdog] pre-exec memory flush failed: {e}")

    for target in (_dsl, _mem):
        t = threading.Thread(target=target, daemon=True)
        t.start()
        t.join(timeout_s)
        if t.is_alive():
            logger.warning("[watchdog] pre-exec flush timed out "
                           f"(lock held by hung main thread) — proceeding to re-exec")


def _watchdog() -> None:
    while True:
        time.sleep(60)
        if _watchdog_timeout_s <= 0:
            continue
        stalled = time.time() - _last_progress_ts
        if stalled >= _watchdog_timeout_s:
            logger.error(
                f"[watchdog] no progress for {stalled:.0f}s "
                f"(> {_watchdog_timeout_s}s) — HUNG (startup or scan); re-execing to self-heal")
            try:
                log_event({"event": "error", "scope": "watchdog",
                           "error": f"hung {stalled:.0f}s — re-exec"})
            except Exception:
                pass
            # Persist what we can before the image is replaced. Server-side
            # SL/TP brackets keep protecting positions through the restart;
            # startup rehydrate rebuilds trackers from the exchange.
            _pre_exec_flush()
            os.execv(sys.executable, [sys.executable] + sys.argv)


def _beat(stage: str) -> None:
    """Per-stage watchdog heartbeat.

    The loop only bumped `_last_progress_ts` once per FULL cycle (after all
    triggers had been researched + executed). A slow-but-progressing cycle
    (cold-start meta prewarm + ~3 min scan + N serial HTA researches) could
    legitimately exceed the 600s timeout and get killed mid-research, at
    which point the restart re-did the same expensive work — a death spiral
    observed 2026-08-18 (re-exec every ~10 min for hours). Bumping before
    each major stage means a STUCK stage still fires the watchdog, but a
    slow-moving one doesn't.
    """
    global _last_progress_ts
    _last_progress_ts = time.time()
    logger.debug(f"[watchdog] beat: {stage}")


threading.Thread(target=_watchdog, name="hermes-watchdog", daemon=True).start()
logger.info(f"[watchdog] armed pre-startup: re-exec if no progress for {_watchdog_timeout_s}s")

logger.info("=== HERMES TRADER - Starting Continuous Trading Loop ===")

config = get_config()
startup_agent_config = read_agent_config()
startup_mode = str(startup_agent_config.get("mode", "OFF")).upper()
logger.info(f"Mode: {startup_mode}  env={_args.env}  daemon={_args.daemon}")
# HIP-3 toggle: read once at startup so the prefetched universe includes
# tokenized-equity / commodity perps if enabled. The agent config is
# hot-reloaded per cycle inside the executor / perception layer for other
# fields; the universe itself is fetched once at startup, so flipping
# enable_hip3 mid-run requires a loop restart to pick up new markets.
try:
    _enable_hip3 = bool(startup_agent_config.get("enable_hip3", False))
except Exception:
    _enable_hip3 = False
universe = get_universe(include_hip3=_enable_hip3)
logger.info(
    f"Universe loaded: {len(universe)} markets"
    + (f" (HIP-3 enabled — {sum(1 for m in universe if m.get('dex'))} tokenized markets)" if _enable_hip3 else "")
)
# Warm the per-dex meta cache BEFORE the first scan/execute so the restart-time
# 429 storm can't make coin resolution fall through to "Unknown coin" (which
# kills the HIP-3 backup stop-loss) or blank candle fetches. Bound it: the SDK
# meta call has hung during startup, which left the bot neither scanning nor
# monitoring exits until an external restart.
def _prewarm_meta_cache_bounded(timeout_s: float) -> None:
    state = {"done": False, "error": None}

    def _run() -> None:
        try:
            prewarm_meta_cache()
        except Exception as e:
            state["error"] = e
        finally:
            state["done"] = True

    t = threading.Thread(target=_run, name="hermes-meta-prewarm", daemon=True)
    t.start()
    t.join(timeout_s)
    if t.is_alive():
        logger.warning(
            f"[startup] meta prewarm exceeded {timeout_s:.0f}s — continuing; "
            "coin metadata will warm lazily")
    elif state["error"] is not None:
        logger.warning(f"[startup] meta prewarm failed (will warm lazily): {state['error']}")


_prewarm_meta_cache_bounded(float(os.environ.get('HERMES_META_PREWARM_TIMEOUT_S', '3')))
# The universe carries prevDayPx / dayNtlVlm / funding which DRIFT over the
# day; fetched once here they'd freeze at loop-start for the whole process,
# so mover-selection + volume-ranking would rank stale 24h windows (a coin
# ripping now would never enter the movers slot). Re-fetch on a TTL so those
# fields track the live market. metaAndAssetCtxs is ~20 weight (+~8 POSTs for
# HIP-3) — trivial against HL's 1200 weight/min. Env-overridable; 0 disables.
universe_refresh_s = int(os.environ.get('HERMES_UNIVERSE_REFRESH_S', '1800'))
_last_universe_refresh = time.time()
memory.load()  # hydrate from .agent-memory.json so cache + flush work.

# Startup grace: the prewarm burst above + the cold-cache first scan (every
# coin's candles fetched fresh) + any tail from the just-killed process all hit
# the SAME per-IP HL budget at once → the restart 429-storm (observed 2026-06-15:
# ~30% scan data-gaps for ~2min, loop stalled). Pause so the rate-limiter bucket
# refills before the first scan fires its full candle burst. Env-overridable;
# 0 disables. Cheap one-time cost; steady-state scans are unaffected.
_startup_grace_s = float(os.environ.get('HERMES_STARTUP_GRACE_S', '12'))
if _startup_grace_s > 0:
    logger.info(f"[startup] grace delay {_startup_grace_s:.0f}s — letting HL rate budget refill before the first cold scan")
    time.sleep(_startup_grace_s)

# Scan cadence: env-overridable, default 15s.
# Post-HYPE postmortem (2026-08-21): shortened from 60s to 15s to shrink the
# DSL exit polling blind window during fast crashes. The 5m candle cache TTL
# (50s) is keyed by candle timestamp so repeated reads within a TTL still see
# the same closed candle; intra-candle price is fetched live via midpoint.
scan_interval = int(os.environ.get('HERMES_SCAN_INTERVAL', '15'))
min_score = config['scan']['minCompositeScore']

logger.info(f"Scan interval: {scan_interval}s, Min score: {min_score}")
log_event({
    "event": "loop_start",
    "scan_interval": scan_interval,
    "min_score": min_score,
    # Full config snapshot at startup so the feed shows exactly what the bot
    # is configured to do — useful for postmortems ("what was the cap when
    # this trade happened?") and for the operator UI to surface drift.
    "config": startup_agent_config,
})


def _burst_fired(perception):
    """True if the perception's momentumBurst trigger fired (a large fast move)."""
    return any(t.get("name") == "momentumBurst" and t.get("fired")
               for t in perception.get("triggers", []))


# B-M11 (deep audit 2026-08-28): optional HARD flatten on breakers.
# Extracted as a pure helper so the breaker→flatten mapping is unit-testable
# without driving the whole loop.
def bm11_breaker_flatten(equity, positions, cfg, mem,
                         flattener=close_position_market,
                         event_log=log_event):
    """Flatten open positions when a breaker is armed AND its opt-in switch on.

    Mirrors the daily-loss kill-switch guards: ``equity > 0`` (a degraded read
    returns equity=0 and can never trigger a flatten) and non-empty positions.
    Both switches DEFAULT OFF (config_store) — flattening on a halt is a
    deliberate operator choice.

      * auto_flatten_on_global_halt: global halt armed → close EVERY coin.
      * auto_flatten_on_coin_circuit: coin circuit armed → close that coin
        (skipped if the global pass already closed it this tick).

    State reads never raise; a failing read is treated as "not armed".
    Returns the set of coins actually flattened.
    """
    flattened: set = set()
    if not (equity > 0 and positions):
        return flattened
    pos_coins = [(_p.get("position") or {}).get("coin") for _p in positions]
    pos_coins = [c for c in pos_coins if c]
    if not pos_coins:
        return flattened
    if bool(cfg_get("auto_flatten_on_global_halt", config=cfg)):
        try:
            _grem = float(mem.global_halt_remaining_min() or 0.0)
        except Exception as _ge:
            logger.error(f"[killswitch] B-M11 global-halt state read failed: {_ge}")
            _grem = 0.0
        if _grem > 0:
            logger.warning(
                f"[killswitch] B-M11 global halt armed ({int(_grem)}min) and "
                f"auto_flatten_on_global_halt=ON — flattening {len(pos_coins)} "
                f"open position(s)")
            for _coin in pos_coins:
                try:
                    _res = flattener(_coin)
                    logger.warning(f"[killswitch] halt-flatten {_coin}: ok={_res.get('ok')}")
                    flattened.add(_coin)
                except Exception as _e:
                    logger.error(f"[killswitch] halt-flatten failed for {_coin}: {_e}")
            event_log({"event": "global_halt_auto_flatten",
                       "remaining_min": round(_grem, 1),
                       "flattened": len(flattened)})
    if bool(cfg_get("auto_flatten_on_coin_circuit", config=cfg)):
        for _coin in pos_coins:
            if _coin in flattened:
                continue
            try:
                _crem = float(mem.coin_circuit_remaining_min(_coin) or 0.0)
            except Exception as _ce:
                logger.error(f"[killswitch] B-M11 coin-circuit state read failed for {_coin}: {_ce}")
                continue
            if _crem <= 0:
                continue
            logger.warning(
                f"[killswitch] B-M11 coin circuit armed on {_coin} ({int(_crem)}min) "
                f"and auto_flatten_on_coin_circuit=ON — flattening")
            try:
                _res = flattener(_coin)
                logger.warning(f"[killswitch] circuit-flatten {_coin}: ok={_res.get('ok')}")
                flattened.add(_coin)
            except Exception as _e:
                logger.error(f"[killswitch] circuit-flatten failed for {_coin}: {_e}")
            event_log({"event": "coin_circuit_auto_flatten", "coin": _coin,
                       "remaining_min": round(_crem, 1)})
    return flattened


def af14_feed_decision(mids, stale_age, missing_mids,
                       max_stale_s=MID_FEED_MAX_STALE_S):
    """Pure A-F14/C-M1 feed-health decision for one loop tick.

    Returns (halt_reason, skip_exits):
      * halt_reason None = feed healthy (entries may run); otherwise entries
        are paused with the returned reason.
      * skip_exits True only for the A-F14 STALE case — DSL market-close
        decisions are skipped as well as entries (closing on a stale mid is
        exactly the wick exit A-F5 hardened against; exchange-side backup SLs
        remain live server-side). Empty/blind snapshots still run
        monitor_exits (entries paused only): an empty snapshot has no tracker
        whose floor could fire, and exchange SLs backstop the rest.
    """
    if not mids:
        return ("empty mids snapshot (all_mids feed failed)", False)
    if stale_age is not None and stale_age > max_stale_s:
        return (f"mids feed stale ({stale_age:.0f}s > {max_stale_s:.0f}s budget)",
                True)
    if missing_mids:
        _blind = sorted(missing_mids)
        reason = (f"no usable mid for held coin(s): {', '.join(_blind[:5])}"
                  + (f" +{len(_blind) - 5} more" if len(_blind) > 5 else ""))
        return (reason, False)
    return (None, False)


# C1 (ARCHITECTURE.md "equity-spike bug"): event types in events.jsonl that
# prove a REAL money-losing close happened recently. Every one of them is in
# event_log._FORKABLE_EVENTS, so query_events actually sees them. If a crash-
# sized equity drop is NOT explained by any of these within the lookback, it is
# a phantom degraded read, not a real loss.
_PHANTOM_CRASH_EXIT_EVENTS = frozenset({
    "close",                    # memory.record_close → event_log.append("close")
    "dsl_exit",                 # DSL market close (executed or backfilled mirror)
    "ai_close",                 # AI-decided close
    "external_close_recorded",  # exchange-side stop fill detected after the fact
    "hard_killswitch",          # this loop's own HARD flatten from a prior tick
})


def phantom_crash_unconfirmed(equity, prev_eq, prev_ts, *,
                              crash_pct=None, recency_s=180.0,
                              lookback_s=60.0, query_fn=None,
                              time_module=time, now=None):
    """True when a crash-sized single-tick equity drop has NO real-close event.

    The heartbeat already guards equity<=0 (empty API response) and partial-DEX
    reads (a held dex missing from the aggregate). The residual hole is a
    NON-zero phantom: the aggregate returns a *real-looking* value that is far
    too low (a degraded dex silently under-reports instead of dropping out).
    Left untouched it poisons daily PnL and FALSE-TRIPS the HARD kill-switch,
    flattening the whole book on fiction (the documented >50%-tick phantom).

    Returns True only when ALL hold:
      * a previous accepted reading exists and is recent (< ``recency_s``) and
        this tick is a crash-sized drop (``>= crash_pct``) against it;
      * no real-close event (``_PHANTOM_CRASH_EXIT_EVENTS``) was logged in the
        last ``lookback_s`` — a genuine liquidation/close that explains the drop.

    On True the caller preserves last-known-good (degraded return). A genuine
    crash is delayed at most one tick: the closes it triggers land in the log,
    and the still-crushed next tick then sees them and is accepted (fail-CLOSED
    for entries/exits in between — we would not want to trade on it anyway).
    Fail-OPEN on any evidence/threshold ambiguity: when ``query_fn`` is absent
    or the previous reading is missing/stale/older, returns False (accept).
    """
    if not (equity > 0 and prev_eq > 0):
        return False
    if crash_pct is None or crash_pct <= 0:
        return False
    _now = now if now is not None else time_module.time()
    if prev_ts <= 0 or (_now - prev_ts) > recency_s:
        return False
    move_frac = (equity - prev_eq) / prev_eq
    if move_frac > -crash_pct:
        return False
    # Crash-sized drop. Only call the log query on this (rare) path.
    if query_fn is None:
        return False
    from datetime import datetime, timezone, timedelta
    start_iso = (datetime.fromtimestamp(_now, tz=timezone.utc)
                 - timedelta(seconds=lookback_s)
                 ).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        recent = query_fn(start=start_iso) or []
    except Exception as e:
        # Event storage must never block the safety path; a read failure means
        # we cannot PROVE the drop is fake → accept it (fail-open on real risk).
        logger.warning(f"[heartbeat] phantom-crash event query failed ({e}); "
                       f"treating equity reading as real")
        return False
    explained = any(
        (e.get("event") in _PHANTOM_CRASH_EXIT_EVENTS)
        # forked records nest fields under "payload"; accept both shapes.
        or (e.get("payload", {}) or {}).get("event") in _PHANTOM_CRASH_EXIT_EVENTS
        for e in recent
    )
    return not explained


def _sync_account_state():
    """Pull live aggregated equity + positions from HL, persist to memory.

    Returns (equity, positions, available, spot_usdc, queried_dexes, state).
    `state` is the full dict so callers can grab per-dex breakdowns
    (`dex_equity`, `dex_available`) without re-fetching.
    """
    user = resolve_user_address()
    if not user:
        # No user → no authoritative position view. Return an EMPTY queried-dexes
        # set (not {""}) so the DSL reconcile preserves existing trackers instead
        # of dropping them as "stale".
        return 0.0, [], 0.0, 0.0, set(), {}
    try:
        # Respect the enable_hip3 config flag — when HIP-3 is disabled (e.g. the
        # 10-USDC mainnet config), querying every perpDex's clearinghouse burns
        # rate budget and time on every heartbeat for no benefit.
        _hip3_on = bool(read_agent_config().get("enable_hip3", False))
        state = fetch_account_state(user, include_hip3=_hip3_on)
    except Exception as e:
        # Fetch FAILED (e.g. API timeout storm). We did NOT successfully query any
        # dex, so report queried_dexes=set() — NOT {""}. Reporting the main dex as
        # "queried" while holding no position data caused live main-dex trackers
        # (e.g. NIL) to be falsely dropped and then re-synthesized with a looser
        # default stop. Empty set => rehydrate preserves every tracker this tick.
        logger.warning(f"[heartbeat] HL fetch_account_state failed: {e}")
        return 0.0, [], 0.0, 0.0, set(), {}

    equity = float(state.get("equity", 0) or 0)
    if equity <= 0:
        # A 'successful' fetch returning $0 equity while positions are open is a
        # degraded/empty API response (timeout-storm), not reality. Don't poison
        # memory — writing it would record a false equity=0 and dailyPnl=-SOD (which
        # also drags the daily-loss kill toward a false trip). Preserve last-known-good
        # by skipping the memory update this tick; queried_dexes=set() keeps DSL
        # trackers intact, and maybe_execute already refuses to size on equity<=0.
        logger.warning("[heartbeat] fetch returned equity<=0 (degraded API) — skipping memory update, preserving last-known-good")
        return 0.0, [], 0.0, 0.0, set(), {}
    # Heartbeat shows total-across-dexes free margin (what the operator
    # actually has trade-ready) — not the main-only number used internally
    # by the executor for native-crypto sizing.
    available = float(state.get("available_aggregated", state.get("available", 0)) or 0)
    spot_usdc = float(state.get("spot_usdc", 0) or 0)
    positions = state.get("asset_positions", []) or []
    queried_dexes = state.get("queried_dexes") or {""}

    # PARTIAL-DEX degraded-read guard: a 'successful' fetch where equity>0 (main
    # dex fine) but a HIP-3 dex we HOLD a position on failed to respond drops that
    # dex's equity from the aggregate — e.g. on 2026-06-03 a missing xyz dex made
    # equity read $56.65 instead of $187.42 (a phantom -$128/-69%). The equity<=0
    # guard above can't catch it (main was funded). Left unguarded it poisons
    # memory equity/dailyPnl AND can FALSE-TRIP the daily-loss kill switch.
    # Detect it: if any dex backing an open DSL tracker isn't in queried_dexes,
    # the aggregate is incomplete → preserve last-known-good (skip memory update,
    # queried_dexes=set() keeps trackers), same as the equity<=0 path.
    held_dexes = {(c.split(":", 1)[0] if ":" in c else "") for c in active_position_coins()}
    missing_dexes = held_dexes - set(queried_dexes)
    if missing_dexes:
        logger.warning(
            f"[heartbeat] partial-dex degraded read: held dex(es) {missing_dexes} "
            f"missing from queried {set(queried_dexes)} (equity read ${equity:.2f} is "
            f"incomplete) — skipping memory update, preserving last-known-good")
        return 0.0, [], 0.0, 0.0, set(), {}

    # PHANTOM-CRASH sanity check (C1): equity>0 and every held dex answered, yet
    # the aggregate can still return a real-looking but far-too-low value (a
    # degraded dex under-reports instead of dropping out). A crash-sized drop vs
    # the last ACCEPTED reading that NO recent real-close event explains is a
    # phantom — accepting it would poison daily PnL and false-trip the HARD kill.
    # Preserve last-known-good exactly like the guards above. Threshold reuses
    # memory's crash_down_pct (same single-tick crash definition); on a genuine
    # crash the closes land in events.jsonl and the next tick is accepted.
    prev_eq, prev_ts = memory.last_equity_reading()
    try:
        from hermes_trader.agents.memory import _memory_quality_params
        _crash_pct = float(_memory_quality_params().get("crash_down_pct", 0.0) or 0.0)
    except Exception as _qp_e:
        logger.warning(f"[heartbeat] crash-threshold read failed ({_qp_e}); "
                       f"skipping phantom-crash check this tick")
        _crash_pct = 0.0
    if _crash_pct > 0:
        try:
            from hermes_trader.event_log import query_events
        except Exception as _imp_e:
            logger.warning(f"[heartbeat] event_log unavailable ({_imp_e}); "
                           f"skipping phantom-crash check this tick")
            query_events = None
        if phantom_crash_unconfirmed(
                equity, prev_eq, prev_ts, crash_pct=_crash_pct,
                query_fn=query_events):
            logger.warning(
                f"[heartbeat] phantom-crash reading ${prev_eq:.2f} -> ${equity:.2f} "
                f"({(equity - prev_eq) / prev_eq * 100:.1f}%) with NO real-close "
                f"event in the last 60s — degraded/phantom read; skipping memory "
                f"update, preserving last-known-good (a sustained move re-asserts "
                f"next tick)")
            return 0.0, [], 0.0, 0.0, set(), {}

    # Subtract net USDC contributions so transfers/deposits don't show
    # up as trading PnL in the equity-diff calculation.
    sod_ts_ms = memory.get_day_start_ts() * 1000
    contributions = 0.0
    if sod_ts_ms > 0:
        try:
            contributions = fetch_aggregate_contributions_since(user, sod_ts_ms)
        except Exception as e:
            logger.warning(f"[heartbeat] contribution fetch failed: {e}")

    memory.track_daily_pnl(equity, contributions)
    memory.update_open_positions(positions)
    memory.flush()
    return equity, positions, available, spot_usdc, queried_dexes, state


# When we last paid for AI research on each coin (this process). Throttles the
# AI close-check on coins we already hold so we don't research a "hold" every
# scan. Resets on restart (a fresh close-check on startup is harmless/useful).
_last_research_by_coin: dict = {}
# Composite score at the time of that paid research, per coin. The re-research
# throttle below uses it to exempt a coin whose setup materially strengthened
# mid-throttle: measured 2026-08-19/20, 86/160 surfaced slots (54%) were dropped
# by RESEARCH_THROTTLE alone, including movers that ran +20%+ after the skip.
# Waiting out a fixed window on a score that jumped is the expensive failure.
_last_research_score_by_coin: dict = {}


while True:
    try:
        # ── Heartbeat: refresh equity / positions before scanning ──────────
        equity, positions, available, spot_usdc, queried_dexes, state = _sync_account_state()
        daily_pnl = memory.get_daily_pnl()
        if equity <= 0 and spot_usdc > 0:
            logger.warning(
                f"[heartbeat] perp equity $0 but ${spot_usdc:.2f} USDC idle in "
                f"spot — transfer spot->perp to enable trading.")
        # Compact config snapshot for the heartbeat line — surfaces what the
        # bot is currently tuned to do without forcing the watcher to pop
        # open `.agent-config.json`. Read fresh each tick so a hot-reloaded
        # config is reflected in the next heartbeat.
        _cfg = read_agent_config()
        # Per-dex breakdown so the dashboard can show where USDC + free
        # margin actually sits (main vs xyz vs km, etc).
        dex_equity = {k: round(float(v), 2) for k, v in (state.get("dex_equity") or {}).items()}
        dex_available = {k: round(float(v), 2) for k, v in (state.get("dex_available") or {}).items()}
        log_event({
            "event": "loop_heartbeat",
            "equity": round(equity, 4),
            "available": round(available, 4),
            "dex_equity": dex_equity,
            "dex_available": dex_available,
            "spot_usdc": round(spot_usdc, 4),
            "daily_pnl": round(daily_pnl, 4),
            "open_positions": len(positions),
            "config": {
                "mode": _cfg.get("mode"),
                "frac": _cfg.get("equity_fraction_per_trade"),
                "lev": _cfg.get("leverage"),
                "max_conc": _cfg.get("max_concurrent"),
                "notional_cap": _cfg.get("max_total_notional_pct"),
                "cool_min": _cfg.get("cooldown_min"),
                "min_conf": _cfg.get("min_ai_confidence"),
                "kill": _cfg.get("max_daily_loss_usd"),
                "crypto": bool(_cfg.get("enable_crypto", True)),
                "hip3": bool(_cfg.get("enable_hip3", False)),
            },
        })
        # Publish the position list so the dashboard can render the table
        # without its own fetch_account_state call (which, sharing this IP,
        # was doubling HL load and tripping per-IP rate limits).
        write_snapshot(positions)
        _beat("account_sync")

        # ── HARD daily-loss kill-switch ─────────────────────────────────────
        # The daily_loss GATE (risk_gates) only blocks NEW entries — it can't
        # close what's already open, so a losing book OVERSHOOTS the limit as
        # positions keep bleeding to their DSL stops (2026-06-09: hit -$35 vs a
        # -$30 cap). Make the floor HARD: once the day's loss breaches the limit,
        # FLATTEN every open position so the loss can't run further. The gate then
        # keeps re-entry blocked until the UTC roll. Guarded by equity>0: every
        # degraded/partial-read path in _sync_account_state returns equity=0 (and
        # preserves last-known-good daily_pnl), so a bad read can NEVER trigger a
        # flatten. Idempotent: after flattening, the next tick's positions are
        # empty so it won't re-fire.
        _max_daily_loss = float(cfg_get("max_daily_loss_usd", config=_cfg))
        if equity > 0 and positions and daily_pnl <= _max_daily_loss:
            logger.warning(
                f"[killswitch] HARD daily-loss floor breached: PnL ${daily_pnl:.2f} "
                f"<= ${_max_daily_loss:.0f} — flattening {len(positions)} open "
                f"position(s) to cap the loss")
            for _p in positions:
                _coin = (_p.get("position") or {}).get("coin")
                if not _coin:
                    continue
                try:
                    _res = close_position_market(_coin)
                    logger.warning(f"[killswitch] flattened {_coin}: ok={_res.get('ok')}")
                except Exception as _e:
                    logger.error(f"[killswitch] failed to flatten {_coin}: {_e}")
            log_event({"event": "hard_killswitch", "daily_pnl": round(daily_pnl, 2),
                       "limit": _max_daily_loss, "flattened": len(positions)})

        # ── B-M11: optional HARD flatten on circuit breakers ───────────────
        # global_halt_gate / coin_circuit_breaker_gate only block NEW entries
        # (risk_gates) — a position already open when the breaker trips keeps
        # running to its DSL stop through the whole halt window. With these
        # opt-in switches armed (default OFF — config_store defaults), the
        # breaker becomes a hard flatten: global halt closes EVERY open
        # position; a coin circuit closes that coin's position. The flatten
        # is idempotent (close_position_market re-fetches live state and
        # no-ops an already-flat coin; after closing, the coin vanishes from
        # the next tick's `positions`), and the same equity>0 guard as the
        # daily-loss kill-switch applies so a degraded read can't trigger it.
        bm11_breaker_flatten(equity, positions, _cfg, memory)

        # ── DSL exit pass ───────────────────────────────────────────────────
        # Reconcile trackers with live exchange positions (handles restarts,
        # manual closes, externally-filled SLs), then market-close anything
        # whose dynamic floor was breached.
        # Resolve user address here so rehydrate can look up actual fill times
        # for synthesized trackers (the `user` local inside _sync_account_state
        # is not visible in this scope).
        user = resolve_user_address()
        # C-M1 (deep audit 2026-08-28): feed-health gate for NEW entries.
        # Set inside the DSL pass below when the price snapshot is unusable;
        # consumed after the OFF-mode check to skip scan/research/execution.
        # Exit monitoring always runs regardless (exchange SLs backstop).
        _feed_halt_reason = None
        try:
            dropped = rehydrate_from_exchange(positions,
                                    default_leverage=int(cfg_get("leverage", config=_cfg)),
                                    queried_dexes=queried_dexes,
                                    user=user)
            # Backfill close/outcome records for positions that vanished outside
            # the DSL market-close path (exchange-side SL/TP trigger, manual
            # close, liquidation). Without this, rehydrate silently dropped the
            # tracker and memory.closes never recorded the realized PnL — PURR
            # 2026-08-22 hit its server-side SL and the trade existed in
            # trades[] but had no matching closes[] row. Best-effort only: a
            # bookkeeping failure must never block exit monitoring.
            if dropped and user:
                try:
                    from hermes_trader.agents.dsl_exit import resolve_close_fill
                    for _tr in dropped:
                        try:
                            _fill = resolve_close_fill(
                                user, _tr.coin, _tr.side,
                                since_ts=_tr.entry_time - 1.0)
                            if not _fill:
                                logger.warning(
                                    f"[outcome-store] {_tr.coin} {_tr.side} "
                                    f"tracker dropped externally but no "
                                    f"reducing fill found — close NOT recorded")
                                log_event({"event": "external_close_unattributed",
                                           "coin": _tr.coin, "side": _tr.side,
                                           "entry_px": _tr.entry_px})
                                continue
                            _exit_px = float(_fill.get("px") or 0.0)
                            _sz = abs(float(_fill.get("sz") or 0.0))
                            _fee = float(_fill.get("fee") or 0.0)
                            _closed_pnl = float(_fill.get("closedPnl") or 0.0)
                            _closed_at = int(_fill.get("time") or
                                             int(time.time() * 1000))
                            if _exit_px <= 0 or _sz <= 0:
                                continue
                            _lev = max(1, int(_tr.leverage or 1))
                            _notional = _sz * _tr.entry_px
                            if _tr.side == "long":
                                _spot_pct = ((_exit_px - _tr.entry_px)
                                             / _tr.entry_px * 100.0)
                            else:
                                _spot_pct = ((_tr.entry_px - _exit_px)
                                             / _tr.entry_px * 100.0)
                            # closedPnl from the exchange is net of the closing
                            # fee but NOT the opening fee; subtract the entry
                            # fee estimate so the stored net matches a normal
                            # DSL close's realized_pnl_usd.
                            _entry_fee = _notional * 0.00025
                            _net_usd = round(_closed_pnl - _entry_fee, 4)
                            _hold_min = round((_closed_at / 1000.0
                                               - _tr.entry_time) / 60.0, 1)
                            memory.record_close({
                                "coin": _tr.coin, "side": _tr.side,
                                "entry_px": _tr.entry_px, "exit_px": _exit_px,
                                "size_coin": _sz,
                                "notional_usd": round(_notional, 4),
                                "spot_pct": round(_spot_pct, 4),
                                "realized_pnl_pct": round(
                                    _net_usd / _notional * 100.0 * _lev, 4)
                                if _notional > 0 else 0.0,
                                "realized_pnl_usd": _net_usd,
                                "gross_pnl_usd": round(_closed_pnl + _fee, 4),
                                "fee_usd": round(_fee + _entry_fee, 4),
                                "leverage": _lev,
                                "closed_at": _closed_at,
                                "entry_time": int(_tr.entry_time * 1000),
                                "hold_minutes": _hold_min,
                                "signals_at_entry": {},
                                "enforcement_at_entry": {},
                                "forced_override": None,
                                "entry_slip_bps": None,
                                "exit_slip_bps": None,
                                "regime_at_entry": _tr.entry_regime or "",
                                "is_hip3": ":" in _tr.coin,
                                "funding_cost_usd": None,
                                "close_source": "exchange_trigger",
                                "close_oid": _fill.get("oid"),
                            })
                            logger.info(
                                f"[outcome-store] backfilled external close "
                                f"{_tr.coin} {_tr.side} @ {_exit_px} "
                                f"({_spot_pct:+.2f}% spot, pnl=${_net_usd}) "
                                f"oid={_fill.get('oid')}")
                            # Emit a dsl_exit session-log event so the web
                            # dashboard's closed-trades panel (which only reads
                            # session-log, not memory.closes/events.jsonl) can
                            # see backfilled external closes. close_source is
                            # preserved on the memory record; this event is the
                            # dashboard-visible mirror.
                            _fees_pct = (0.00025 * 2 * _lev)
                            _gross_pct = _spot_pct * _lev
                            log_event({
                                "event": "dsl_exit",
                                "coin": _tr.coin,
                                "side": _tr.side,
                                "leverage": _lev,
                                "reason": "external_close_backfill",
                                "exit_reason": "exchange_trigger",
                                "entry_regime": _tr.entry_regime or "",
                                "hold_min": _hold_min,
                                "unrealized_pct": round(_spot_pct, 4),
                                "leveraged_pct": round(_gross_pct, 4),
                                "executed": True,
                                "detail": f"backfill oid={_fill.get('oid')}",
                                "fill_px": _exit_px,
                                "entry_px": _tr.entry_px,
                                "realized_spot_pct": round(_spot_pct, 4),
                                "realized_pnl_pct": round(
                                    _net_usd / _notional * 100.0 * _lev, 4)
                                if _notional > 0 else 0.0,
                                "fees_pct": _fees_pct,
                                "close_source": "exchange_trigger",
                            })
                            log_event({"event": "external_close_recorded",
                                       "coin": _tr.coin, "side": _tr.side,
                                       "entry_px": _tr.entry_px,
                                       "exit_px": _exit_px,
                                       "spot_pct": round(_spot_pct, 4),
                                       "realized_pnl_usd": _net_usd,
                                       "leverage": _lev,
                                       "oid": _fill.get("oid")})
                            # Arm the loss cooldown on a losing external fill
                            # so an exchange-side stop also enforces the
                            # anti-revenge re-entry block (normally done by
                            # close_position_market).
                            if _net_usd < 0:
                                try:
                                    lc_min = float(
                                        cfg_get("loss_cooldown_min",
                                                config=read_agent_config()))
                                    if lc_min > 0:
                                        until = int(time.time() * 1000
                                                    + lc_min * 60_000)
                                        memory.set_loss_cooldown(_tr.coin, until)
                                        logger.info(
                                            f"[executor] loss cooldown armed "
                                            f"on {_tr.coin}: {lc_min:.0f}min "
                                            f"(external close ${_net_usd})")
                                except Exception as _lc_e:
                                    logger.warning(
                                        f"[executor] loss-cooldown arm failed "
                                        f"for {_tr.coin}: {_lc_e}")
                        except Exception as _dc_e:
                            logger.warning(
                                f"[outcome-store] drop-backfill failed for "
                                f"{_tr.coin}: {_dc_e}")
                except Exception as _bf_e:
                    logger.warning(
                        f"[outcome-store] external-close backfill setup "
                        f"failed (non-fatal): {_bf_e}")

            # include_hip3=True so xyz:MU / vntl:* etc. get fresh mids each
            # cycle — without them, monitor_exits has no price for HIP-3
            # trackers and their peak/floor never advance (dashboard shows
            # "no DSL" indefinitely and DSL stop never fires on HIP-3).
            mids = get_all_hl_mids(include_hip3=True)
            # C-M1: fail CLOSED on feed failure. A completely empty snapshot
            # means all_mids() failed/returned nothing; held coins missing from
            # a non-empty snapshot mean the feed is blind to positions we hold
            # (their DSL exits cannot evaluate either — check_all_positions
            # screams per coin). In both states new entries this cycle would be
            # sized off stale/missing prices, so they are paused.
            #
            # A-F14 (deep audit 2026-08-28): also fail closed on a STALE feed.
            # A non-empty snapshot can still be an OLD snapshot — during a
            # network partition the SDK can hand back data buffered before the
            # drop, or a poll cycle can be delayed long enough that the mids no
            # longer reflect the market. mid_feed_age_seconds() stamps every
            # successful MAIN-book fetch; beyond MID_FEED_MAX_STALE_S (30s,
            # matching ws_client's ws_max_stale_s) the feed is treated as dead
            # for BOTH directions: new entries stay paused (below) and DSL
            # market-close exits are SKIPPED this cycle — closing on a stale
            # mid fires exactly the wick exits A-F5 hardened against, and on a
            # stale price in the WRONG direction there is no defensible exit
            # either. The exchange-side backup SL orders remain live
            # server-side and cover real disaster while decisions are paused.
            _stale_age = mid_feed_age_seconds()
            _blind = held_coins_missing_mids(mids) if mids else []
            _feed_halt_reason, _stale_skip_exits = af14_feed_decision(
                mids, _stale_age, _blind)
            if _stale_skip_exits:
                # A-F14: stale feed → skip DSL exit DECISIONS as well as entries.
                logger.error(
                    f"[FEED-FRESHNESS] {_feed_halt_reason} — pausing entries AND "
                    f"DSL market-close exits this cycle (exchange-side backup SLs "
                    f"remain live server-side).")
                exits = []
            else:
                exits = monitor_exits(mids)
            for ex in exits:
                coin = ex["coin"]
                lev = ex.get("leverage", 1)
                lpct = ex.get("leveraged_pct", ex["unrealized_pct"] * lev)
                _reg = ex.get("entry_regime") or "unknown"
                _hold = ex.get("hold_min") or 0.0
                _mfe = ex.get("mfe_pct") or 0.0
                logger.info(f"[dsl] Closing {coin} {ex.get('side','?')} ({lev}x): "
                            f"{ex['reason']} (margin {lpct:+.2f}% · spot {ex['unrealized_pct']:+.2f}%)")
                # Structured per-exit telemetry — machine-parseable for the
                # trailing-stop conservatism audit (avg realized profit / hold
                # time / MFE grouped by entry_regime). reason is canonicalized
                # so floor_breach* -> trailing_stop for grouping.
                _r = ex["reason"]
                _canon = "trailing_stop" if _r.startswith("floor_breach") else (
                    "max_loss" if _r.startswith("max_loss") else (
                    "hard_timeout" if _r.startswith("hard_timeout") else (
                    "stale_flat_timeout" if _r.startswith("stale_flat_timeout") else _r.split(" ")[0])))
                logger.info(f"[dsl:exit_stats] coin={coin} side={ex.get('side','?')} "
                            f"lev={lev} regime={_reg} reason={_canon} "
                            f"hold_min={_hold:.1f} mfe_spot_pct={_mfe:+.2f} "
                            f"exit_spot_pct={ex['unrealized_pct']:+.2f} "
                            f"exit_margin_pct={lpct:+.2f}")
                res = close_position_market(coin)
                # The close response carries authoritative realized PnL when
                # the order filled with a parseable avgPx — prefer it over the
                # tick-time estimate, which is gross of fees and off by the
                # fill slippage.
                evt = {
                    "event": "dsl_exit",
                    "coin": coin,
                    "side": ex.get("side"),
                    "leverage": lev,
                    "reason": ex["reason"],
                    "exit_reason": _canon,
                    "entry_regime": _reg,
                    "hold_min": round(_hold, 2),
                    "mfe_spot_pct": round(_mfe, 4),
                    "unrealized_pct": round(ex["unrealized_pct"], 4),
                    "leveraged_pct": round(lpct, 4),
                    "executed": bool(res.get("ok")),
                    "detail": res.get("order_id") or res.get("noop") or res.get("error"),
                }
                if res.get("realized_pnl_pct") is not None:
                    evt["fill_px"] = res.get("fill_px")
                    evt["entry_px"] = res.get("entry_px")
                    evt["realized_spot_pct"] = res.get("spot_pct")
                    evt["realized_pnl_pct"] = res.get("realized_pnl_pct")
                    evt["fees_pct"] = res.get("fees_pct")
                log_event(evt)

            # ── Dynamic exchange-SL coordination ──────────────────────────
            # After processing DSL exits, pull each Phase-2 position's static
            # exchange backup SL up behind the ratcheted DSL floor (throttled,
            # best-effort). This keeps the server-side safety net overlapping
            # locked-in profit instead of sitting at the initial 3% ceiling.
            try:
                sync_exchange_sl(mids)
            except Exception as _sl_e:
                logger.error(f"[dsl] sync_exchange_sl failed (non-fatal): {_sl_e}")
            # Retry placing backup SLs for positions whose initial placement
            # failed twice. This used to be dead code (defined but never
            # called) — a naked position had no server-side stop between DSL
            # polls or across a crash. Wired here, right after sync, so every
            # DSL monitor pass re-arms the exchange-side safety net.
            try:
                retry_pending_sl()
            except Exception as _rsl_e:
                logger.error(f"[dsl] retry_pending_sl failed (non-fatal): {_rsl_e}")
        except Exception as e:
            logger.error(f"[dsl] monitor pass failed: {e}")
            log_event({"event": "error", "scope": "dsl_monitor", "error": str(e)})

        _beat("dsl_exit")

        if str(_cfg.get("mode", "OFF")).upper() == "OFF":
            logger.info("[mode] OFF — skipping scan/research/execution; exits still monitored")
            _last_progress_ts = time.time()
            logger.info(f"Sleeping {scan_interval}s until next scan...")
            time.sleep(scan_interval)
            continue

        # C-M1: price feed is dead or blind to a held coin — skip ALL new-entry
        # decisions this cycle (scan/research/execution). Positions are still
        # exit-monitored above and backstopped by exchange-side SLs; trading on
        # missing/stale prices is the fail-open path the audit flagged.
        if _feed_halt_reason:
            logger.error(f"[feed] FEED-FRESHNESS halt — skipping entries this cycle: {_feed_halt_reason}")
            log_event({"event": "feed_halt", "reason": _feed_halt_reason})
            _last_progress_ts = time.time()
            logger.info(f"Sleeping {scan_interval}s until next scan...")
            time.sleep(scan_interval)
            continue

        # Hot-toggle `enable_hip3`: the universe is a startup snapshot (see the
        # comment above line "HIP-3 toggle: read once at startup"), so flipping
        # the flag mid-run previously required a restart to add/drop HIP-3
        # tokenized markets. Detect the change each cycle and rebuild immediately.
        # Read fresh here (do not rely on the perception-layer `include_hip3`,
        # which only gates filtering of the already-prefetched list).
        try:
            _hip3_now = bool(read_agent_config().get("enable_hip3", False))
        except Exception:
            _hip3_now = _enable_hip3
        if _hip3_now != _enable_hip3:
            try:
                universe = get_universe(force_refresh=True, include_hip3=_hip3_now)
                _enable_hip3 = _hip3_now
                _last_universe_refresh = time.time()
                _n_hip3 = sum(1 for m in universe if m.get("dex"))
                logger.info(
                    f"[universe] enable_hip3 flipped to {_hip3_now} — rebuilt: "
                    f"{len(universe)} markets ({_n_hip3} HIP-3)"
                )
            except Exception as e:
                logger.warning(f"[universe] enable_hip3 flip rebuild failed, keeping prior snapshot: {e}")

        # Refresh the universe on a TTL so prevDayPx / dayNtlVlm / funding track
        # the live market instead of freezing at loop-start (stale fields make
        # the scanner rank yesterday's movers — see HERMES_UNIVERSE_REFRESH_S).
        elif universe_refresh_s > 0 and (time.time() - _last_universe_refresh) >= universe_refresh_s:
            try:
                universe = get_universe(force_refresh=True, include_hip3=_enable_hip3)
                _last_universe_refresh = time.time()
                logger.info(f"Universe refreshed: {len(universe)} markets")
            except Exception as e:
                logger.warning(f"[universe] periodic refresh failed, keeping prior snapshot: {e}")

        logger.info("Scanning markets...")
        _beat("scan_start")
        results = scan_once(universe=universe, min_score=min_score, config=config)
        _beat("scan_done")
        logger.info(f"Scan found {len(results)} triggers")
        # Per-cycle heartbeat — proof of life even when nothing triggers.
        # `coin_scores` carries the composite score for each trigger so the
        # feed can show *why* a coin was picked, not just that it was.
        log_event({"event": "scan", "triggers": len(results),
                   "coins": [p['coin'] for p in results],
                   "coin_scores": [{"coin": p['coin'],
                                    "score": round(p.get('composite_score', 0), 1),
                                    "triggers": [t['name'] for t in p.get('triggers', []) if t.get('fired')]}
                                   for p in results]})

        # Surge detection: feed every trigger result (score >= gate) to the
        # postmortem watcher. It compares against the previous cycle and fires
        # a postmortem when a coin crosses the gate with a large score jump
        # driven by a momentum trigger. Failures are swallowed internally.
        for _p in results:
            _surge_detector.observe(
                _p.get("coin", "?"),
                float(_p.get("composite_score", 0) or 0),
                _p.get("triggers", []),
                perception=_p,
            )

        # Pre-research dedupe cache: coin → last research timestamp this run.
        # Prevents burning AI tokens on a setup that's still in cooldown from a
        # prior cycle. The execute-time `cooldown_gate` is still in place as the
        # authoritative backstop; this just stops the paid LLM call early.
        _cfg_cd = read_agent_config()
        cooldown_min = float(_cfg_cd.get("cooldown_min", 30))
        cooldown_ms = cooldown_min * 60_000
        # How often a HELD coin is re-researched for a possible AI CLOSE. We
        # don't pay for a "hold" PASS every scan — the DSL engine handles fast
        # exits in real time; the AI close-check is the slower structural-flip
        # judgment and only needs an occasional refresh.
        held_research_ms = float(_cfg_cd.get("held_research_interval_min", 10)) * 60_000
        # Re-research throttle for NON-held, non-traded coins: a coin that keeps
        # triggering but keeps PASSing (or whose trade gets gate/margin-rejected)
        # used to be researched EVERY scan — burning LLM tokens/credits on a setup
        # that won't meaningfully change in 60s (e.g. XLM PASS'd every cycle). Skip
        # re-research for this window regardless of verdict. The scan still re-detects
        # it; we just don't re-pay the LLM until the cooldown lapses.
        research_cooldown_ms = float(_cfg_cd.get("research_cooldown_min", 15)) * 60_000
        # Newest trade timestamp per coin (NOT oldest — see the method docstring;
        # the prior inline `setdefault` kept the oldest, so a coin traded twice
        # in the window paid for redundant LLM research every cycle).
        recent_trades_by_coin = memory.latest_trade_ts_by_coin(20)
        held_coins = memory.open_position_coins()
        # Blocklisted coins can never execute (coin_filter gate blocks them), so
        # we skip the paid LLM research for any we don't hold — see the else
        # branch below. Held blocklisted coins are exempt (AI must keep the
        # ability to CLOSE). Read once per scan from the hot-reloaded config.
        _blocklist = set(_cfg_cd.get("coin_blocklist", []) or [])
        now_ms = int(time.time() * 1000)

        # Per-cycle outcome tracker for the end-of-cycle summary log.
        _cycle_outcomes = []  # list of (coin, action, executed, detail)

        for perception in results:
            coin = perception['coin']
            score = perception.get('composite_score', 0)

            # Persist perceptions so memory/dashboard track real signal volume.
            try:
                memory.record_perception(perception)
            except Exception:
                pass

            if coin in held_coins:
                # Held position: research only every held_research_interval_min
                # so the AI can still issue a CLOSE without paying for a "hold"
                # PASS on every scan. (A re-entry is gate-blocked anyway.)
                last_research = _last_research_by_coin.get(coin, 0)
                if (now_ms - last_research) < held_research_ms:
                    remaining_min = _remaining_minutes(held_research_ms - (now_ms - last_research))
                    logger.info(f"{coin}: held — next AI close-check in {remaining_min}min — skip")
                    log_event({"event": "ta_skip", "coin": coin,
                               "signal": "HELD_THROTTLE",
                               "score": round(float(score), 1),
                               "trigger_score": round(float(score), 1)})
                    _cycle_outcomes.append((coin, "skip", False, "held throttle"))
                    continue
                # Infancy hold: skip the AI close-check while the position is
                # younger than min_ai_close_hold_min (0=off). Measured churn
                # 2026-06-11/12: the FIRST 10-min close-check reversed the AI's
                # own fresh entry 3x (TON 2x, ZEC 1x, each ~-1% ROE incl. fees) —
                # flip-flopping on entry noise. DSL stop + backup SL still
                # protect an infant position; only the AI's second-guess waits.
                min_hold_min = float(cfg_get("min_ai_close_hold_min", config=_cfg_cd))
                if min_hold_min > 0:
                    from hermes_trader.agents import dsl_exit as _dsl
                    _tr = (_dsl._active_positions.get(f"{coin}_long")
                           or _dsl._active_positions.get(f"{coin}_short"))
                    if _tr is not None:
                        age_min = (time.time() - _tr.entry_time) / 60
                        if age_min < min_hold_min:
                            logger.info(f"{coin}: held {age_min:.0f}min < min_hold "
                                        f"{min_hold_min:.0f}min — infancy, skip close-check")
                            _cycle_outcomes.append((coin, "skip", False, f"infancy hold {age_min:.0f}min"))
                            continue
            else:
                # Blocklisted + not held → coin_filter will reject any entry, so
                # skip the paid LLM research entirely (this coin keeps triggering
                # every scan otherwise). Held blocklisted coins took the held
                # branch above and still get their AI close-check.
                if coin in _blocklist:
                    logger.info(f"{coin}: on coin blocklist — skip research")
                    log_event({"event": "ta_skip", "coin": coin,
                               "signal": "BLOCKLISTED",
                               "score": round(float(score), 1),
                               "trigger_score": round(float(score), 1)})
                    _cycle_outcomes.append((coin, "skip", False, "blocklisted"))
                    continue
                # Not held but executed within cooldown_min → re-entry would be
                # gate-blocked, so skip the paid AI call.
                last_ms = recent_trades_by_coin.get(coin)
                if last_ms and (now_ms - last_ms) < cooldown_ms:
                    remaining_min = _remaining_minutes(cooldown_ms - (now_ms - last_ms))
                    logger.info(f"{coin}: pre-research cooldown ({remaining_min}min remaining) — skip")
                    log_event({"event": "ta_skip", "coin": coin,
                               "signal": "COOLDOWN",
                               "score": round(float(score), 1),
                               "trigger_score": round(float(score), 1)})
                    _cycle_outcomes.append((coin, "skip", False, f"cooldown {remaining_min}min"))
                    continue
                # Re-research throttle: already researched recently (any verdict) →
                # don't re-pay the LLM until research_cooldown_min lapses.
                #
                # Exemption: if the composite score jumped by at least
                # research_rescore_delta since that research, the setup is
                # materially different from the one we already judged, so the
                # cached verdict is stale and worth re-paying for. Fires at most
                # once per throttle window because we overwrite the stored score
                # on every paid research.
                last_research = _last_research_by_coin.get(coin, 0)
                if (now_ms - last_research) < research_cooldown_ms:
                    _rescore_delta = float(_cfg_cd.get("research_rescore_delta", 0) or 0)
                    _prev_score = _last_research_score_by_coin.get(coin)
                    _jumped = (
                        _rescore_delta > 0
                        and _prev_score is not None
                        and (float(score) - _prev_score) >= _rescore_delta
                    )
                    if _jumped:
                        logger.info(
                            f"{coin}: re-research throttle BYPASSED — composite "
                            f"{_prev_score:.1f} -> {float(score):.1f} "
                            f"(+{float(score) - _prev_score:.1f} >= {_rescore_delta:.1f})")
                    else:
                        remaining_min = _remaining_minutes(research_cooldown_ms - (now_ms - last_research))
                        logger.info(f"{coin}: re-research throttle ({remaining_min}min remaining) — skip")
                        log_event({"event": "ta_skip", "coin": coin,
                                   "signal": "RESEARCH_THROTTLE",
                                   "score": round(float(score), 1),
                                   "trigger_score": round(float(score), 1)})
                        _cycle_outcomes.append((coin, "skip", False, f"research throttle {remaining_min}min"))
                        continue

            # TA filter — cheap statistical gate before the paid AI call.
            # A momentum burst may bypass a WEAK TA score (the burst itself is
            # the impulse) but NEVER a REJECTED one — REJECTED now includes the
            # late-entry veto (RSI extreme / over-extension), so an over-extended
            # burst must not reach the paid AI.
            ta = analyze_perception(perception)
            _burst_bypass = _burst_fired(perception) and ta['signal'] != 'REJECTED'
            if ta['signal'] != 'CONFIRMED' and not _burst_bypass:
                logger.info(f"{coin}: TA {ta['signal']} (score {ta['score']:.0f}) — skip AI research")
                log_event({"event": "ta_skip", "coin": coin,
                           "signal": ta['signal'],
                           "score": round(float(ta.get('score', 0)), 1),
                           "reason": ta.get("reason"),
                           "trigger_score": round(float(score), 1)})
                _cycle_outcomes.append((coin, "skip", False, f"TA {ta['signal']}"))
                continue
            gate = 'CONFIRMED' if ta['signal'] == 'CONFIRMED' else f"{ta['signal']}+burst"
            logger.info(f"Researching {coin} (trigger {score:.1f}, TA {gate})...")
            # Per-coin heartbeat: a 3-trigger cycle with slow HTA research can
            # take several minutes (observed 77s on a single GOOGL call). A
            # beat before each paid research prevents the watchdog from
            # re-exec'ing mid-batch while coins are still making progress.
            _beat(f"research_start:{coin}")
            # Record the paid-research time so the held-coin throttle above can
            # pace the next AI close-check on this position.
            _last_research_by_coin[coin] = now_ms
            # Snapshot the score this verdict was formed on, so the throttle
            # above can detect a material re-score during the window.
            _last_research_score_by_coin[coin] = float(score)

            try:
                # Pass the cycle-level account state snapshot so research()
                # doesn't re-fetch it (saves N × (2+M) HL POSTs per cycle).
                analysis = research(coin, perception, account_snapshot=state)
                logger.info(f"Verdict: {analysis['verdict']}, Confidence: {analysis['confidence']}")
                # Store the full LLM reasoning verbatim — no character cap.
                # The feed shows the complete rationale.
                _r = (analysis.get('reasoning') or '').strip()
                log_event({"event": "research", "coin": coin,
                           "verdict": analysis['verdict'],
                           "confidence": round(float(analysis['confidence']), 2),
                           "reasoning": _r,
                           "news_risk": analysis.get('news_risk'),
                           "entry_px": analysis.get('entry_px'),
                           "stop_px": analysis.get('stop_px'),
                           "tp_px": analysis.get('tp_px')})

                # All verdict→action routing lives in executor.route_verdict
                # (unit-tested) so no verdict can be silently dropped again.
                routed = route_verdict(analysis)
                action = routed["action"]
                result = routed["result"] or {}
                if action == "execute":
                    executed = bool(result.get("executed"))
                    if executed:
                        _oid = result.get("order_id", "")
                        _sz = result.get("size_usd", 0)
                        logger.info(f"✅ {coin} ORDER PLACED — side={analysis['side']} "
                                    f"size=${_sz:.0f} order_id={_oid}")
                    else:
                        _blocks = result.get("blocked_by") or []
                        _reason = result.get("reason") or "; ".join(_blocks) or "unknown"
                        logger.info(f"🚫 {coin} BLOCKED — {_reason}")
                    # Surface the regime decision so the log answers "why did a
                    # counter-regime trade fire?" — via is one of aligned /
                    # neutral / confidence / composite / trigger:<name> / blocked.
                    _all_gates = result.get("gate_results") or {}
                    mr = _all_gates.get("market_regime") or {}
                    # Persist a compact pass/fail summary of ALL gates so the
                    # event log can reconstruct which gates blocked a trade
                    # (previously only market_regime's 4 fields were saved).
                    _gates_summary = {
                        gk: bool(gv.get("pass")) for gk, gv in _all_gates.items()
                        if isinstance(gv, dict)
                    }
                    log_event({"event": "execute", "coin": coin,
                               "side": analysis['side'],
                               "executed": executed,
                               "detail": result.get("order_id")
                               or result.get("reason")
                               or result.get("blocked_by"),
                               "blocked_by": result.get("blocked_by") if not executed else None,
                               "size_usd": result.get("size_usd"),
                               "entry_px": result.get("entry_px"),
                               "stop_px": result.get("stop_px"),
                               "tp_px": result.get("tp_px"),
                               "regime": mr.get("regime"),
                               "funding_regime": mr.get("funding"),
                               "regime_via": mr.get("via"),
                               "counter_regime": mr.get("counter_trend") or mr.get("against_funding"),
                               "gates": _gates_summary,
                               "gate_results": _all_gates})
                    _cycle_outcomes.append((coin, "execute", executed,
                                           result.get("order_id") or
                                           "; ".join(result.get("blocked_by") or []) or
                                           result.get("reason") or ""))
                elif action == "close":
                    logger.info(f"Closed {coin} per AI CLOSE verdict: {result}")
                    log_event({"event": "ai_close", "coin": coin,
                               "executed": bool(result.get("ok")),
                               "detail": result.get("order_id")
                               or result.get("noop")
                               or result.get("error"),
                               "reasoning": (analysis.get("reasoning") or "")})
                    _cycle_outcomes.append((coin, "close", bool(result.get("ok")),
                                           result.get("order_id") or result.get("noop") or ""))
                elif action == "unknown":
                    log_event({"event": "error", "coin": coin,
                               "error": f"unhandled verdict {routed['verdict']!r}"})
                    _cycle_outcomes.append((coin, "unknown", False, routed.get("verdict", "")))
                else:
                    # PASS / HOLD / no-action verdict — coin was researched but
                    # the AI chose not to act.
                    _cycle_outcomes.append((coin, action or "pass", False,
                                           f"verdict={analysis.get('verdict', '?')}"))
            except Exception as e:
                # repr(e) not str(e): a bare exception (e.g. some httpx errors)
                # stringifies to "" and produced blank "Error processing X:" lines.
                detail = repr(e) if str(e) == "" else str(e)
                logger.error(f"Error processing {coin}: {type(e).__name__}: {detail}")
                log_event({"event": "error", "coin": coin,
                           "error": f"{type(e).__name__}: {detail}"})
                _cycle_outcomes.append((coin, "error", False, f"{type(e).__name__}: {detail}"))
            # Post-coin beat — covers both research+execute path and exception
            # path, so one slow coin doesn't eat the whole cycle's budget.
            _beat(f"research_done:{coin}")

        _last_progress_ts = time.time()  # watchdog: a full cycle completed
        # End-of-cycle summary: one line per trigger coin so you can see at a
        # glance which coins passed, which were blocked, and why.
        if _cycle_outcomes:
            logger.info("=" * 60)
            logger.info(f"Cycle summary — {len(results)} trigger(s):")
            for _c, _act, _ok, _detail in _cycle_outcomes:
                if _act == "execute" and _ok:
                    _tag = "✅ EXECUTED"
                elif _act == "execute":
                    _tag = "🚫 BLOCKED"
                elif _act == "close" and _ok:
                    _tag = "✅ CLOSED"
                elif _act == "close":
                    _tag = "⚠️  CLOSE-FAIL"
                elif _act == "skip":
                    _tag = "⏭️  SKIP"
                elif _act == "error":
                    _tag = "❌ ERROR"
                else:
                    _tag = f"⏸  {_act.upper()}"
                logger.info(f"  {_tag:16s} {_c:14s} {_detail}")
            logger.info("=" * 60)
        else:
            logger.info("Cycle summary — no triggers this scan")
        logger.info(f"Sleeping {scan_interval}s until next scan...")
        time.sleep(scan_interval)

    except KeyboardInterrupt:
        logger.info("Trading loop stopped by user")
        log_event({"event": "loop_stop"})
        break
    except Exception as e:
        logger.error(f"Trading loop error: {e}")
        log_event({"event": "error", "error": str(e)})
        logger.info(f"Sleeping {scan_interval}s before retry...")
        time.sleep(scan_interval)
