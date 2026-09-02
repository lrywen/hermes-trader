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
from concurrent.futures import ThreadPoolExecutor

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

# ── 持久化轮转日志（2026-09-02，替代 compose 的 `tee -a`）────────────────
# tee 追加的文件无任何轮转机制（14 天涨到 140MB）。这里用
# RotatingFileHandler 按 50MB × 5 代轮转；stdout/stderr 仍由 basicConfig 的
# StreamHandler 输出（docker logs，json-file 另有 50m×5 轮转）。
# 单进程写（trading_loop 只有一个 loop 进程），无跨进程竞争。
# 文件不可写等任何失败都回落到纯 stderr（handleError 不抛异常），观测链不断。
_loop_log_path = os.environ.get("HERMES_LOOP_LOG_FILE", "/data/trading-loop.log")
try:
    from logging.handlers import RotatingFileHandler
    _loop_fh = RotatingFileHandler(
        _loop_log_path, maxBytes=50 * 1024 * 1024, backupCount=5,
        encoding="utf-8",
    )
    _loop_fh.setFormatter(logging.Formatter(
        '%(asctime)s %(levelname)s:%(name)s:%(message)s'))
    logging.getLogger().addHandler(_loop_fh)
except Exception as _loop_log_err:
    logging.getLogger(__name__).warning(
        "loop file handler disabled (stderr only): %s", _loop_log_err)

from hermes_trader.agents.perception import scan_once, signal_fingerprint
from hermes_trader.agents.ta_filter import analyze_perception
from hermes_trader.agents.research import research
from hermes_trader.agents.executor import close_position_market, maybe_execute, monitor_exits, route_verdict, sync_exchange_sl, retry_pending_sl, maybe_roe_blowup_halt
from hermes_trader.agents.dsl_exit import active_position_coins, held_coins_missing_mids, rehydrate_from_exchange
from hermes_trader.agents.config import get_config
from hermes_trader.agents.config_store import read_agent_config, cfg_get
from hermes_trader.agents.memory import memory
from hermes_trader.client.exchange import get_all_hl_mids, prewarm_meta_cache, mid_feed_age_seconds, MID_FEED_MAX_STALE_S
from hermes_trader.client.universe import get_universe
from hermes_trader.client.hl_client import fetch_account_state, fetch_aggregate_contributions_since, resolve_user_address, start_ws_mids, stop_ws_mids, start_ws_user_fills, stop_ws_user_fills, drain_ws_user_fills, ws_feed_age_seconds, wait_for_ws_user_fills
from hermes_trader.positions_snapshot import write_snapshot
from hermes_trader.session_log import append as log_event
from hermes_trader.surge_postmortem import SurgeDetector, SurgeConfig
from hermes_trader.realtime_feed import dynamic_scan_interval, classify_feed_status, FeedStatusTracker

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


# ── Intra-cycle DSL exit checkpoint ─────────────────────────────────────────
# The main loop evaluates DSL exits once per cycle, at the TOP of the loop
# before the scan. A cold scan (~3 min) plus N serial LLM researches (a single
# GOOGL HTA call was observed at 77s; a full batch can run several minutes)
# therefore opens an inter-cycle blind window of up to the ~600s watchdog
# budget during which a breached floor is not acted on even though the WS
# real-time mid (Phase 3) already reflects the move.
#
# `_exit_checkpoint()` is a LIGHTWEIGHT re-run of the exit decision that we
# call between trigger coins during the research batch (and after the scan).
# It deliberately does NOT do the heavy per-cycle work — no
# rehydrate_from_exchange (that needs the fresh account snapshot + dexes and
# issues extra POSTs), no sync_exchange_sl / retry_pending_sl (those ratchet
# and POST per held coin) — it only re-checks the DSL floor against the
# freshest available price and market-closes anything breached. monitor_exits
# already prefers the WS real-time mid (Phase 3) and falls back to the passed
# REST snapshot, so passing the cycle's `mids` is safe and cheap.
#
# Concurrency: this runs on the SAME main thread, never from the WS callback.
# No new thread, no lock — it simply shortens the time between two otherwise
# identical main-thread exit evaluations.
_EXIT_CHECKPOINT_MIN_INTERVAL_S = float(
    os.environ.get("HERMES_EXIT_CHECKPOINT_MIN_INTERVAL_S", "5.0"))
_last_exit_checkpoint_ts = 0.0


def _process_exits(exits, *, source: str = "dsl") -> int:
    """Market-close every DSL exit verdict and emit telemetry.

    Shared by the per-cycle monitor pass and the intra-cycle checkpoint so a
    close is executed/logged identically regardless of where it fired. Returns
    the number of closes attempted. Never raises into the caller.
    """
    n = 0
    for ex in exits:
        coin = ex["coin"]
        lev = ex.get("leverage", 1)
        lpct = ex.get("leveraged_pct", ex["unrealized_pct"] * lev)
        _reg = ex.get("entry_regime") or "unknown"
        _hold = ex.get("hold_min") or 0.0
        _mfe = ex.get("mfe_pct") or 0.0
        _tag = "DSL" if source == "dsl" else "DSL-CHECKPOINT"
        logger.info(f"[{_tag}] Closing {coin} {ex.get('side','?')} ({lev}x): "
                    f"{ex['reason']} (margin {lpct:+.2f}% · spot {ex['unrealized_pct']:+.2f}%)")
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
            "close_source": source,
        }
        if res.get("realized_pnl_pct") is not None:
            evt["fill_px"] = res.get("fill_px")
            evt["entry_px"] = res.get("entry_px")
            evt["realized_spot_pct"] = res.get("spot_pct")
            evt["realized_pnl_pct"] = res.get("realized_pnl_pct")
            evt["fees_pct"] = res.get("fees_pct")
        log_event(evt)
        n += 1
    return n


def _drain_and_report_fills() -> int:
    """Drain queued WS userFills and surface close fills to the dashboard.

    Shared by the main loop (once per cycle) and the intra-cycle exit
    checkpoint, so an externally-triggered close (manual flat / another
    session / liquidation) lands in the dashboard Trades feed at checkpoint
    granularity instead of waiting out a whole multi-minute scan+research
    cycle. Non-blocking and never raises into the caller; returns the number
    of fills drained. The drain does NOT make exit decisions — it only
    reports fills; exits stay with monitor_exits / _exit_checkpoint.
    """
    try:
        _drained_fills = drain_ws_user_fills()
        for _f in _drained_fills:
            _f_dir = str(_f.get("dir", ""))
            _closed_pnl = str(_f.get("closedPnl", "0"))
            _is_close = (
                _closed_pnl not in ("0", "0.0", "", "0.00000")
                or "close" in _f_dir.lower()
                or "liquidation" in _f_dir.lower()
            )
            # Always log the drained fill (Phase 1 diagnostic parity).
            logger.info(
                "[ws:user-fills:drain] coin=%s side=%s dir=%s sz=%s "
                "px=%s closedPnl=%s tid=%s is_close=%s",
                _f.get("coin"), _f.get("side"), _f_dir,
                _f.get("sz"), _f.get("px"), _closed_pnl,
                _f.get("tid"), _is_close,
            )
            if _is_close:
                # SSE event — Trades.vue refreshes on ws_user_fill via
                # REFRESH_EVENTS. The Portal BFF sends the operator ticket so
                # the full feed is delivered; the public feed filter
                # (D-FCFG-3) drops it for anonymous clients because
                # 'ws_user_fill' is not in _PUBLIC_FEED_EVENTS.
                log_event({
                    "event": "ws_user_fill",
                    "kind": "close",
                    "coin": str(_f.get("coin", "")),
                    "side": str(_f.get("side", "")),
                    "dir": _f_dir,
                    "sz": str(_f.get("sz", "")),
                    "px": str(_f.get("px", "")),
                    "closedPnl": _closed_pnl,
                    "tid": str(_f.get("tid", "")),
                    "t": int(_f.get("t", 0) or 0),
                    "ts": int(time.time() * 1000),
                })
        return len(_drained_fills)
    except Exception as _drain_err:
        # Drain failures must never break the trading loop or a research batch.
        logger.warning(
            f"[ws:user-fills:drain] error (non-fatal): {_drain_err}"
        )
        return 0


def _refresh_positions_after_close(reason: str) -> None:
    """Re-fetch positions after a confirmed mid-cycle close and republish.

    The positions snapshot (read by the dashboard /api/dashboard/positions
    endpoint, which prefers the snapshot for 120s) and the position_update SSE
    event are otherwise only refreshed once per full cycle at the loop top.
    After a checkpoint or external close the snapshot would still list the
    closed coin for the rest of a multi-minute scan, so the Positions/Overview
    pages render a zombie position until the next cycle. This re-syncs ONLY
    when a close actually happened (never on the every-5s no-op path), and
    skips the write on a degraded read (equity<=0) so it can never publish an
    empty list over the real positions. Never raises.
    """
    try:
        _eq, _positions, _avail, _spot, _qdexes, _state = _sync_account_state()
        if _eq <= 0:
            logger.warning(
                f"[positions] post-close refresh skipped ({reason}): "
                f"degraded read equity=${_eq:.2f}, keeping previous snapshot")
            return
        write_snapshot(_positions)
        _pos_coins = [str((p.get("position") or {}).get("coin") or "")
                      for p in _positions]
        _pos_coins = [c for c in _pos_coins if c]
        log_event({
            "event": "position_update",
            "count": len(_positions),
            "coins": _pos_coins,
            "reason": reason,
            "ts": int(time.time() * 1000),
        })
        logger.info(f"[positions] post-close refresh ({reason}): "
                    f"{len(_positions)} open position(s)")
    except Exception as _refresh_err:
        logger.warning(
            f"[positions] post-close refresh failed (non-fatal): {_refresh_err}"
        )


def _exit_checkpoint(mids, *, tag: str = "intra-cycle") -> int:
    """Lightweight intra-cycle DSL exit re-evaluation.

    Re-runs monitor_exits on the freshest price (WS real-time mid preferred,
    REST `mids` as fallback) and closes any newly-breached position, clamping
    the worst-case inter-cycle blind window to ~HERMES_EXIT_CHECKPOINT_MIN_INTERVAL_S
    instead of the full scan+research duration.

    Also drains queued WS userFills on the same throttle so an EXTERNAL close
    (manual flat / another session) is reported to the dashboard mid-cycle
    instead of only at the next loop top, and refreshes the positions snapshot
    whenever a close (checkpoint or external fill) actually happened so the
    Positions/Overview pages drop the closed coin immediately.

    Safety gates, kept identical in spirit to the per-cycle pass:
      * Throttled to at most once per _EXIT_CHECKPOINT_MIN_INTERVAL_S so a fast
        research batch can't hammer the exit path.
      * A-F14 STALE feed → skip exit DECISIONS here too (closing on a stale mid
        is the wick exit A-F5 hardened against; exchange backup SLs stay live).
        The fill drain still runs (a queued external fill must not wait on the
        feed gate) and a post-close snapshot refresh still runs (it reads
        positions, not mids).
      * Wrapped so a checkpoint failure can never abort the research batch.
    Returns the number of closes performed (0 most of the time).
    """
    global _last_exit_checkpoint_ts
    now = time.time()
    if (now - _last_exit_checkpoint_ts) < _EXIT_CHECKPOINT_MIN_INTERVAL_S:
        return 0
    _last_exit_checkpoint_ts = now
    try:
        # Surface externally-triggered fills (manual flat / another session)
        # without waiting for the next full cycle. Independent of the stale
        # feed gate: it reports what already happened on the exchange.
        _n_fills = _drain_and_report_fills()
        _age = mid_feed_age_seconds()
        if _age is not None and _age > MID_FEED_MAX_STALE_S:
            # A-F14: don't make market-close decisions on a stale REST feed.
            # The WS mid inside monitor_exits may still be fresh, but the gate
            # here mirrors the per-cycle policy (fail closed); the next normal
            # cycle pass re-evaluates with a full snapshot.
            logger.debug(f"[dsl:checkpoint] {tag}: skip exit scan — feed stale ({_age:.0f}s)")
        else:
            exits = monitor_exits(mids)
            if exits:
                n = _process_exits(exits, source="checkpoint")
                logger.info(f"[dsl:checkpoint] {tag}: {n} position(s) closed "
                            f"mid-research to cap the exit blind window")
                _beat(f"exit_checkpoint:{tag}")
                _refresh_positions_after_close(f"checkpoint:{tag}")
                return n
        # An external close fill may have flattened a position even though the
        # exit scan was skipped (stale feed) or found nothing — refresh so it
        # vanishes from the dashboard Positions table immediately.
        if _n_fills:
            _refresh_positions_after_close(f"external_fill:{tag}")
    except Exception as e:
        logger.error(f"[dsl:checkpoint] {tag} failed (non-fatal): {e}")
    return 0


def _effective_scan_interval() -> int:
    """Effective inter-cycle sleep seconds (P0-1).

    Fixed ``scan_interval`` unless HERMES_SCAN_DYNAMIC is enabled, in which
    case a fresh allMids WS feed yields the fast cadence and a stopped/stale
    WS (REST fallback) yields the slow cadence. Never raises — on any
    diagnostic failure the fixed cadence is used.
    """
    if not _scan_dynamic_on:
        return scan_interval
    try:
        return dynamic_scan_interval(
            scan_interval,
            ws_age_s=ws_feed_age_seconds(),
            dynamic_on=_scan_dynamic_on,
            fresh_s=_scan_dynamic_fresh_s,
            fast_s=_scan_interval_fast,
            slow_s=_scan_interval_slow,
        )
    except Exception as _dyn_e:
        logger.warning(f"[phase4] dynamic interval failed (using fixed): {_dyn_e}")
        return scan_interval


def _cycle_sleep(seconds: int) -> None:
    """Sleep between cycles, interruptible by a WS userFill (P0-2).

    With HERMES_WS_FILL_WAKE off (default) this is a plain ``time.sleep`` —
    identical to Phase 1. With it on, the wait returns the instant the WS
    callback thread enqueues a fill; the drain + SSE reporting then happen on
    THIS main thread (the callback thread still never touches the session
    log — single-writer rule preserved), and a close fill triggers an
    immediate positions refresh so Positions/Overview drop the closed coin
    without waiting out the rest of the inter-cycle sleep. Never raises.
    """
    if not _ws_fill_wake_on or seconds <= 0:
        time.sleep(max(0, int(seconds)))
        return
    try:
        _woken = wait_for_ws_user_fills(float(seconds))
        if _woken:
            _n = _drain_and_report_fills()
            logger.info(f"[phase4] fill-wake: drained {_n} fill(s) mid-sleep")
            # A close fill flattens a position — refresh the snapshot on the
            # main thread immediately (mirrors _exit_checkpoint's behaviour).
            _refresh_positions_after_close("fill_wake")
    except Exception as _wake_e:
        # Any failure must not break the loop's cadence: fall back to the
        # remainder of the wait as a plain sleep is unnecessary — the wrapper
        # itself slept the full timeout on failure — just log.
        logger.warning(f"[phase4] fill-wake wait failed (non-fatal): {_wake_e}")


def _maybe_emit_ws_status() -> None:
    """Classify combined feed liveness once per cycle; emit ws_status on edges.

    P0-3. Edge-triggered (with downgrade hysteresis via FeedStatusTracker) so
    the SSE stream carries an event only when ok→degraded→down and back. The
    event is operator-only (NOT added to _PUBLIC_FEED_EVENTS). This NEVER
    affects trading: entries/exits are still gated independently by af14_feed_decision
    (fail-closed). Disabled unless HERMES_WS_STATUS_EVENT is on. Never raises.
    """
    if not _ws_status_event_on:
        return
    try:
        _ws_age = ws_feed_age_seconds()
        _rest_age = mid_feed_age_seconds()
        _status = classify_feed_status(
            ws_age_s=_ws_age,
            rest_age_s=_rest_age,
            ws_fresh_s=_ws_status_fresh_s,
            rest_fresh_s=MID_FEED_MAX_STALE_S,
        )
        _transition = _feed_status_tracker.evaluate(_status)
        if _transition is not None:
            log_event({
                "event": "ws_status",
                "status": str(_transition.get("status", _status)),
                "previous": str(_transition.get("previous", "")),
                "reason": str(_transition.get("reason", "")),
                "ws_age_s": round(_ws_age, 1) if isinstance(_ws_age, (int, float)) else None,
                "rest_age_s": round(_rest_age, 1) if isinstance(_rest_age, (int, float)) else None,
                "ts": int(time.time() * 1000),
            })
            logger.warning(
                f"[phase4] ws_status {_transition.get('previous')} → "
                f"{_transition.get('status')} (ws_age={_ws_age}, rest_age={_rest_age})"
            )
    except Exception as _se:
        logger.warning(f"[phase4] ws_status emit failed (non-fatal): {_se}")


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

# Start the persistent allMids WebSocket so the loop reads sub-second
# top-of-book instead of paying a REST POST per scan. Falls back to REST
# automatically if WS fails to start or goes stale (see ws_client.py).
try:
    start_ws_mids()
except Exception as _ws_err:
    logger.warning(f"[ws] start_ws_mids failed (will fall back to REST): {_ws_err}")

# Phase 1 (WS user-fills feasibility): subscribe to the wallet's fill
# stream on the SAME WS connection as allMids. Callback is LOG-ONLY in
# Phase 1 — no exit decisions are driven off it yet. Failures are
# non-fatal: the trading loop's REST-based monitor_exits keeps working.
# ``resolve_user_address()`` returns the master or wallet address (or
# empty string when neither env is set — in which case we skip the
# subscription entirely; the empty-user guard in ws_client.subscribe_user_fills
# would log a warning and return False anyway, but doing the check
# here keeps the startup log clean).
try:
    _ws_user_addr = resolve_user_address()
    if _ws_user_addr:
        start_ws_user_fills(_ws_user_addr)
    else:
        logger.info("[ws:user-fills] skipped — no user address configured")
except Exception as _uf_err:
    logger.warning(f"[ws:user-fills] start_ws_user_fills failed (non-fatal): {_uf_err}")

# Scan cadence: env-overridable, default 15s.
# Post-HYPE postmortem (2026-08-21): shortened from 60s to 15s to shrink the
# DSL exit polling blind window during fast crashes. The 5m candle cache TTL
# (50s) is keyed by candle timestamp so repeated reads within a TTL still see
# the same closed candle; intra-candle price is fetched live via midpoint.
scan_interval = int(os.environ.get('HERMES_SCAN_INTERVAL', '15'))
min_score = config['scan']['minCompositeScore']

# ── Phase 4 realtime optimisations (all default OFF → Phase-1 behaviour) ──────
# P0-1 dynamic cadence: when the allMids WS feed is fresh, scan faster to
# shrink position/equity/close latency; when the WS is down (REST fallback),
# scan slower to spare REST rate budget. Off by default → fixed scan_interval.
_scan_dynamic_on = os.environ.get('HERMES_SCAN_DYNAMIC', '0').lower() in ('1', 'true', 'yes', 'on')
_scan_dynamic_fresh_s = float(os.environ.get('HERMES_SCAN_FRESH_S', '10'))
_scan_interval_fast = int(os.environ.get('HERMES_SCAN_INTERVAL_FAST', '8'))
_scan_interval_slow = int(os.environ.get('HERMES_SCAN_INTERVAL_SLOW', '20'))
# P0-2 fill wake: between cycles, sleep on a threading.Event that the WS
# callback thread sets the instant a userFill is enqueued, so an external
# close is drained + SSE-reported immediately instead of after the full
# inter-cycle sleep. The drain/report still runs on the MAIN thread (the
# callback thread never touches the session log). Off by default → time.sleep.
_ws_fill_wake_on = os.environ.get('HERMES_WS_FILL_WAKE', '0').lower() in ('1', 'true', 'yes', 'on')
# P0-3 ws_status: edge-triggered SSE event (with downgrade hysteresis) so the
# Portal can show feed green/yellow/red and dispatch Feishu/voice alerts. Off
# by default → no ws_status events are emitted.
_ws_status_event_on = os.environ.get('HERMES_WS_STATUS_EVENT', '0').lower() in ('1', 'true', 'yes', 'on')
_ws_status_hold_s = float(os.environ.get('HERMES_WS_STATUS_HOLD_S', '30'))
_ws_status_fresh_s = float(os.environ.get('HERMES_WS_STATUS_FRESH_S', '10'))
# P0-4 cross-coin research parallelism: the per-cycle research loop is 100%
# serial across triggered coins (for perception in results), and each research()
# blocks tens of seconds on the LLM — with a slow provider and trend_surface
# fanning out many triggers, the costs ADD LINEARLY and blow the tick to
# hundreds of seconds. When on, the paid research() calls for a cycle run
# concurrently on a bounded pool. ONLY the read-only research() is parallelised:
# all gating (held/cooldown/throttle/TA/dedup), verdict routing and order
# placement stay on the main thread in trigger order, so execution semantics
# (account state, position gates, session log) are unchanged. Off by default
# → the original serial loop.
_research_parallel_on = os.environ.get('HERMES_RESEARCH_PARALLEL', '0').lower() in ('1', 'true', 'yes', 'on')
_research_parallel_workers = int(os.environ.get('HERMES_RESEARCH_PARALLEL_WORKERS', '4'))

if _scan_dynamic_on or _ws_fill_wake_on or _ws_status_event_on:
    logger.info(
        f"[phase4] realtime opts enabled: dynamic_scan={_scan_dynamic_on} "
        f"(fast={_scan_interval_fast}s/slow={_scan_interval_slow}s, "
        f"fresh<{_scan_dynamic_fresh_s}s), fill_wake={_ws_fill_wake_on}, "
        f"ws_status={_ws_status_event_on} (hold={_ws_status_hold_s}s)"
    )

# P0-3 edge/hysteresis tracker: seeded silently on first classify, so a normal
# startup WS connect window never fires a false degraded/down alarm.
_feed_status_tracker = FeedStatusTracker(hold_seconds=_ws_status_hold_s)

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
# O-2 content-level signal dedup: fingerprints (coin, closed-bar, fired
# triggers) of setups we already paid AI research on THIS run. The time-based
# research throttle is keyed on wall-clock and re-pays the LLM once its window
# lapses even while the SAME closed bar keeps re-surfacing; this set keys on
# the actual bar identity, so a given setup is researched at most once.
# Held coins are exempt (their AI close-check must not be suppressed). Entries
# keyed on a new bar_close_ms naturally fall out of scope; the set stays small.
_researched_signal_fps: set = set()


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
        # Emit a position_update event so the dashboard SSE feed can drive
        # event-based refresh on the Positions/Trades pages instead of relying
        # on 10-30s setInterval polling. The SSE _tail_log_sse picks this up
        # via the operator ticket; the public feed filter (D-FCFG-3) drops it
        # automatically because "position_update" is not in _PUBLIC_FEED_EVENTS.
        try:
            _pos_coins = [str((p.get("position") or {}).get("coin") or "") for p in positions]
            _pos_coins = [c for c in _pos_coins if c]
            log_event({
                "event": "position_update",
                "count": len(positions),
                "coins": _pos_coins,
                "ts": int(time.time() * 1000),
            })
        except Exception:
            pass
        _beat("account_sync")

        # ── Phase 2: drain WS userFills and emit SSE events ──────────────
        # The trading loop still drives EXIT DECISIONS via monitor_exits
        # (below) on its regular cadence; Phase 2's job is to surface
        # exchange fills to the dashboard IMMEDIATELY so the operator
        # sees a close in the Trades table without waiting for the next
        # scan's dsl_exit/ai_close event (which can lag by up to
        # scan_interval). Drain is non-blocking; if WS isn't running
        # (REST fallback) the drain returns [] and we skip cleanly.
        # We classify a fill as "close" when closedPnl != "0" OR dir
        # contains "Close" / "Liquidation" — those are the fills the
        # dashboard cares about; open fills are still logged in the WS
        # callback for diagnostics but don't fire a separate SSE event
        # here (the next position_update SSE already covers them).
        # Shared with the intra-cycle checkpoint (_exit_checkpoint) so an
        # external close mid-scan is surfaced at batch granularity too.
        _drain_and_report_fills()

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
                                # O-8: fee_usd here = REAL exchange closing
                                # fee (userFills.fee) + the 2.5bps entry-fee
                                # estimate on real entry notional — the closest
                                # row to an actual round-trip fee. Flag it so
                                # memory.avg_round_trip_fee_bps calibrates the
                                # backtest fee constant on measured (not
                                # modeled) cost. In-process DSL closes model
                                # fee as 2.5bpsx2 and are deliberately unflagged.
                                "fee_actual": True,
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
                            # C3 (HYPE RCA item 5): blow-up self-halt also
                            # covers exchange-triggered closes (server-side SL
                            # fill / liquidation) — the executor chokepoint is
                            # bypassed on these, so arm the same ROE check here
                            # on the backfilled realized ROE.
                            try:
                                maybe_roe_blowup_halt(
                                    _tr.coin,
                                    (_net_usd / _notional * 100.0 * _lev)
                                    if _notional > 0 else None,
                                    source="exchange_trigger",
                                    event_log=log_event)
                            except Exception as _rh_e:
                                logger.warning(
                                    f"[outcome-store] roe blow-up halt check "
                                    f"failed for {_tr.coin}: {_rh_e}")
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
            # Execute the per-cycle exits via the shared close path (also used
            # by the intra-cycle _exit_checkpoint so behaviour is identical).
            _process_exits(exits, source="dsl")
            # Reset the checkpoint throttle: the cycle just did a full eval, so
            # the next intra-research checkpoint gets a fresh interval budget.
            _last_exit_checkpoint_ts = time.time()

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

        # P0-3: classify the combined WS/REST feed and emit a ws_status SSE
        # event on edge transitions (no-op unless HERMES_WS_STATUS_EVENT=1).
        # Purely observational: the fail-closed entry/exit gates above are the
        # only things that ever change trading behaviour.
        _maybe_emit_ws_status()

        _sleep_s = _effective_scan_interval()
        if str(_cfg.get("mode", "OFF")).upper() == "OFF":
            logger.info("[mode] OFF — skipping scan/research/execution; exits still monitored")
            _last_progress_ts = time.time()
            logger.info(f"Sleeping {_sleep_s}s until next scan...")
            _cycle_sleep(_sleep_s)
            continue

        # C-M1: price feed is dead or blind to a held coin — skip ALL new-entry
        # decisions this cycle (scan/research/execution). Positions are still
        # exit-monitored above and backstopped by exchange-side SLs; trading on
        # missing/stale prices is the fail-open path the audit flagged.
        if _feed_halt_reason:
            logger.error(f"[feed] FEED-FRESHNESS halt — skipping entries this cycle: {_feed_halt_reason}")
            log_event({"event": "feed_halt", "reason": _feed_halt_reason})
            _last_progress_ts = time.time()
            logger.info(f"Sleeping {_sleep_s}s until next scan...")
            _cycle_sleep(_sleep_s)
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
        # Cold scan sweeps hundreds of markets in batches and blocks this thread
        # for minutes. The batch-complete hook re-runs the lightweight exit
        # checkpoint between batches so the blind window during a cold scan is
        # capped to ~one batch + throttle, not the whole sweep.
        results = scan_once(
            universe=universe, min_score=min_score, config=config,
            on_batch_complete=lambda _done, _total: _exit_checkpoint(
                mids, tag="cold-scan"),
        )
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
        # P0-4: coins that pass every cheap gate and need the paid LLM research.
        # Phase 1 (this loop, main thread) runs all gates serially and only
        # collects jobs; phase 2 runs the read-only research() concurrently;
        # phase 3 routes verdicts (order side-effects) back on the main thread.
        _research_jobs = []  # list of (coin, perception, score, gate)

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
            # O-2 content-level dedup: TA passed on a setup (coin + closed bar
            # + fired triggers) we already paid AI research for this run →
            # skip the paid LLM re-call. The time throttle re-pays once its
            # window lapses even while the SAME closed bar keeps re-surfacing;
            # this keys on the bar identity itself. Held coins are exempt (the
            # AI close-check must not be suppressed). A None fingerprint (older
            # payload without bar_close_ms) leaves the gate inert.
            if coin not in held_coins:
                _sig_fp = signal_fingerprint(perception)
                if _sig_fp is not None and _sig_fp in _researched_signal_fps:
                    logger.info(f"{coin}: same setup (bar_close {perception.get('bar_close_ms')}, "
                                f"same triggers) already researched — content dedup skip")
                    log_event({"event": "ta_skip", "coin": coin,
                               "signal": "SIGNAL_DEDUP",
                               "score": round(float(score), 1),
                               "trigger_score": round(float(score), 1)})
                    _cycle_outcomes.append((coin, "skip", False, "signal content dedup"))
                    continue
            logger.info(f"Researching {coin} (trigger {score:.1f}, TA {gate})...")
            # Stamp the paid-research time / score / setup fingerprint BEFORE
            # dispatching so parallel workers can't race on these maps, and so
            # the held/research throttles above see the same "researched" mark
            # as the old serial code (they're written before research() runs).
            _last_research_by_coin[coin] = now_ms
            _last_research_score_by_coin[coin] = float(score)
            _sig_fp = signal_fingerprint(perception)
            if _sig_fp is not None:
                _researched_signal_fps.add(_sig_fp)
            _research_jobs.append((coin, perception, float(score), gate))

        # ---- Phase 2: paid research (read-only), parallel if enabled ----
        def _run_research(_j_coin, _j_perception, _j_score, _j_gate):
            # Runs on a worker thread when HERMES_RESEARCH_PARALLEL is on. It MUST
            # stay side-effect free: research() only performs LLM calls + read-only
            # data prefetch — it never places orders or mutates trading state.
            # account_snapshot=state is the per-cycle heartbeat snapshot already
            # taken above. All verdict routing/logging stays on the main thread.
            try:
                # Pass the cycle-level account state snapshot so research()
                # doesn't re-fetch it (saves N × (2+M) HL POSTs per cycle).
                _analysis = research(_j_coin, _j_perception, account_snapshot=state)
                return (_j_coin, _j_score, _j_gate, _analysis, None)
            except Exception as _je:
                # repr(e) not str(e): a bare exception (e.g. some httpx errors)
                # stringifies to "" and produced blank "Error processing X:" lines.
                _detail = repr(_je) if str(_je) == "" else str(_je)
                return (_j_coin, _j_score, _j_gate, None,
                        f"{type(_je).__name__}: {_detail}")

        _research_results = []
        if _research_jobs:
            if _research_parallel_on and len(_research_jobs) > 1:
                _workers = max(1, min(_research_parallel_workers, len(_research_jobs)))
                logger.info(f"[p0-4] parallel research: {len(_research_jobs)} coin(s) "
                            f"on {_workers} worker(s)")
                _beat("research_batch_start")
                # DSL exit checkpoint before we block on the slowest coin, so a
                # stop/floor breach during the parallel batch is still actioned.
                _exit_checkpoint(mids, tag="research:batch-pre")
                with ThreadPoolExecutor(max_workers=_workers,
                                        thread_name_prefix="research-coin") as _pool:
                    _futures = [_pool.submit(_run_research, *_job) for _job in _research_jobs]
                    # Futures are appended in trigger order, so iterating them
                    # re-serializes results deterministically for phase 3.
                    for _fut in _futures:
                        _research_results.append(_fut.result())
                _beat("research_batch_done")
            else:
                # Serial path — identical ordering/behavior to the original loop.
                for _job in _research_jobs:
                    _j_coin = _job[0]
                    _beat(f"research_start:{_j_coin}")
                    _research_results.append(_run_research(*_job))

        # ---- Phase 3: route verdicts on the MAIN thread (order side-effects) ----
        # executor.route_verdict may place/close orders, so it never runs in a
        # worker. Results are iterated in the original trigger order.
        for _r_coin, _r_score, _r_gate, analysis, _r_err in _research_results:
            if _r_err is not None:
                logger.error(f"Error processing {_r_coin}: {_r_err}")
                log_event({"event": "error", "coin": _r_coin, "error": _r_err})
                _cycle_outcomes.append((_r_coin, "error", False, _r_err))
            else:
                try:
                    logger.info(f"Verdict: {analysis['verdict']}, Confidence: {analysis['confidence']}")
                    # Store the full LLM reasoning verbatim — no character cap.
                    # The feed shows the complete rationale.
                    _r = (analysis.get('reasoning') or '').strip()
                    log_event({"event": "research", "coin": _r_coin,
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
                            logger.info(f"✅ {_r_coin} ORDER PLACED — side={analysis['side']} "
                                        f"size=${_sz:.0f} order_id={_oid}")
                        else:
                            _blocks = result.get("blocked_by") or []
                            _reason = result.get("reason") or "; ".join(_blocks) or "unknown"
                            logger.info(f"🚫 {_r_coin} BLOCKED — {_reason}")
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
                        log_event({"event": "execute", "coin": _r_coin,
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
                        _cycle_outcomes.append((_r_coin, "execute", executed,
                                               result.get("order_id") or
                                               "; ".join(result.get("blocked_by") or []) or
                                               result.get("reason") or ""))
                    elif action == "close":
                        logger.info(f"Closed {_r_coin} per AI CLOSE verdict: {result}")
                        log_event({"event": "ai_close", "coin": _r_coin,
                                   "executed": bool(result.get("ok")),
                                   "detail": result.get("order_id")
                                   or result.get("noop")
                                   or result.get("error"),
                                   "reasoning": (analysis.get("reasoning") or "")})
                        _cycle_outcomes.append((_r_coin, "close", bool(result.get("ok")),
                                               result.get("order_id") or result.get("noop") or ""))
                    elif action == "unknown":
                        log_event({"event": "error", "coin": _r_coin,
                                   "error": f"unhandled verdict {routed['verdict']!r}"})
                        _cycle_outcomes.append((_r_coin, "unknown", False, routed.get("verdict", "")))
                    else:
                        # PASS / HOLD / no-action verdict — coin was researched but
                        # the AI chose not to act.
                        _cycle_outcomes.append((_r_coin, action or "pass", False,
                                               f"verdict={analysis.get('verdict', '?')}"))
                except Exception as e:
                    # repr(e) not str(e): a bare exception (e.g. some httpx errors)
                    # stringifies to "" and produced blank "Error processing X:" lines.
                    detail = repr(e) if str(e) == "" else str(e)
                    logger.error(f"Error processing {_r_coin}: {type(e).__name__}: {detail}")
                    log_event({"event": "error", "coin": _r_coin,
                               "error": f"{type(e).__name__}: {detail}"})
                    _cycle_outcomes.append((_r_coin, "error", False, f"{type(e).__name__}: {detail}"))
            # Intra-cycle exit checkpoint: research (and, on the parallel path,
            # the whole batch) can take tens of seconds. Re-evaluate DSL exits
            # between coins so a stop/floor breach is actioned within
            # ~HERMES_EXIT_CHECKPOINT_MIN_INTERVAL_S. Throttled + never raises.
            _exit_checkpoint(mids, tag=f"research:{_r_coin}")
            # Post-coin heartbeat — placed after routing so it covers both the
            # parallel and serial paths (and route latency) the same way the
            # original serial loop did. Keeps the watchdog fed.
            _beat(f"research_done:{_r_coin}")

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
        logger.info(f"Sleeping {_sleep_s}s until next scan...")
        _cycle_sleep(_sleep_s)

    except KeyboardInterrupt:
        logger.info("Trading loop stopped by user")
        log_event({"event": "loop_stop"})
        # Phase 1: clear user-fills subscription BEFORE tearing down the
        # WS connection so the "clearing subscription state" log line is
        # visible while the socket is still alive. Non-fatal on failure
        # — stop_ws_mids() below tears down the SDK subscription
        # registry regardless.
        try:
            stop_ws_user_fills()
        except Exception:
            pass
        try:
            stop_ws_mids()
        except Exception:
            pass
        break
    except Exception as e:
        logger.error(f"Trading loop error: {e}")
        log_event({"event": "error", "error": str(e)})
        _retry_s = _effective_scan_interval()
        logger.info(f"Sleeping {_retry_s}s before retry...")
        _cycle_sleep(_retry_s)
