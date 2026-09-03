"""Market-level extreme-conditions circuit breaker (roadmap §3, 2026-09-04).

The existing ``circuit_breaker`` block is a per-coin / daily-PnL dimension: it
halts entries after THIS bot loses money. It cannot see a market-wide tail
event (flash crash / exchange halt / correlated deleveraging) that has not yet
flowed through our own fills. This module adds a MARKET dimension with three
non-PnL triggers, any one of which trips a single account-wide action:

  1. ``index_crash``   — BTC and/or ETH drop more than a threshold over a
     short 5–15 min window (the whole crypto book correlates to BTC; a sharp
     index leg down is the systemic-crash signature).
  2. ``stop_cluster``  — N or more of OUR OWN DSL stops fire within a short
     window. The engine holds a small, curated book; several stops firing
     together means the market is moving against the whole book at once, not
     idiosyncratic single-coin noise.
  3. ``funding_extreme`` — the funding rate on BTC/ETH spikes to an extreme
     level (crowded positioning that typically precedes/coincides with a
     violent unwind). Off by default (``funding_enabled=False``) — funding is
     a slower positioning signal than the crash tape and stays opt-in until
     shadow evidence supports it.

Tripping reuses the EXISTING disposal channel — ``memory.set_global_halt()``
arms the time-windowed global halt, which the ``global_halt_gate`` already
enforces (blocks ALL new entries) and ``bm11_breaker_flatten`` already turns
into a hard flatten when ``auto_flatten_on_global_halt`` is on (default on).
No new halt mechanism is introduced.

Modes (``market_circuit.mode``), mirroring ta_late_entry's gray-release:

  * ``off``     — the check is absent (default; roadmap: must be explicitly
                  armed after shadow review).
  * ``shadow``  — verdicts are computed, metric'd and written to a dedicated
                  JSONL, but the halt is NEVER armed (would-trigger only).
  * ``enforce`` — a tripped trigger arms the global halt (+ alert/audit).

Design rules (same as the other risk layers):

  * Fail-OPEN on data problems: this is an ADDITIONAL veto on top of the
    exchange path, so a fetch/compute failure (or too little data) must never
    arm a halt — a false trip flattens a healthy book. Data failure is
    surfaced as the ``data_missing`` verdict.
  * Every side effect is best-effort and wrapped; nothing here raises into
    the trading loop.
  * The decision is pure (``decide()``) and separate from I/O
    (``evaluate()``) so thresholds/windows are unit-testable with no network.
"""
from __future__ import annotations

import logging
import os
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# Index proxies watched for the short-window crash leg. Everything in the
# crypto book correlates to BTC; ETH is the second-high-volume macro asset.
DEFAULT_INDEX_COINS = ("BTC", "ETH")

# Bounded verdict labels for the metric (triggers + outcomes).
_VERDICTS = ("trip", "no_trip", "data_missing")


# ──────────────────────────────────────────────────────────────────────────
# Pure decision helpers (no I/O; unit-testable with hand-built inputs)
# ──────────────────────────────────────────────────────────────────────────
def window_drawdown_pct(candles: list[Any], window_bars: int) -> Optional[float]:
    """Peak-to-last-close drop over the last ``window_bars`` CLOSED bars, in %.

    Returns a negative number (e.g. -2.3 for a 2.3% fall), ``None`` when there
    is not enough data or no positive reference price. Uses the highest HIGH
    over the window as the reference (a crash leg that spikes then falls is
    measured from the spike top), and the last bar's CLOSE as the endpoint.
    """
    n = int(window_bars)
    if n <= 0 or not candles or len(candles) < 2:
        return None
    window = candles[-n:] if len(candles) > n else candles
    try:
        peak = max(float(b.h) for b in window)
        last_close = float(window[-1].c)
    except (TypeError, ValueError, AttributeError):
        return None
    if peak <= 0 or last_close <= 0:
        return None
    return (last_close - peak) / peak * 100.0


def decide(cfg: dict[str, Any], *,
           index_drawdowns: dict[str, Optional[float]],
           stop_events: Optional[list[dict[str, Any]]] = None,
           funding_rates: Optional[dict[str, Optional[float]]] = None,
           now_ms: Optional[int] = None) -> dict[str, Any]:
    """Pure market-circuit verdict for one loop tick.

    Inputs (all pre-fetched by the caller so this stays network-free):

      * ``index_drawdowns`` — {coin: window_drawdown_pct or None} for the
        index coins. ``None`` / missing coin = data unknown for that coin.
      * ``stop_events``     — recent DSL-stop events as ``{"ts_ms", "coin"}``
        dicts (already window-filtered by the caller or raw; filtered here).
      * ``funding_rates``   — {coin: funding rate as a FRACTION or None}.

    Returns a dict: ``{"tripped": bool, "trigger": str|None, "reasons": [...],
    "details": {...}, "data_ok": bool}``. Any trigger firing sets
    ``tripped``; ``trigger`` names the first firing trigger (index_crash /
    stop_cluster / funding_extreme). A total data failure (no index data at
    all AND no stop data) returns ``data_ok=False`` so the caller fails open.
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    reasons: list[str] = []
    details: dict[str, Any] = {}
    tripped = False
    trigger: Optional[str] = None

    def _f(key: str, default: float) -> float:
        try:
            return float(cfg.get(key, default))
        except (TypeError, ValueError):
            return float(default)

    def _arm(name: str) -> None:
        nonlocal tripped, trigger
        tripped = True
        if trigger is None:
            trigger = name

    # Data-availability bookkeeping for the fail-open verdict.
    index_have_data = False

    # ── Trigger 1: index short-window crash ────────────────────────────────
    if bool(cfg.get("index_crash_enabled", True)):
        threshold = abs(_f("index_crash_pct", 2.0))
        window_bars = int(_f("index_crash_window_bars", 3))
        worst_coin = None
        worst_dd: Optional[float] = None
        for coin in DEFAULT_INDEX_COINS:
            dd = index_drawdowns.get(coin) if isinstance(index_drawdowns, dict) else None
            if dd is None:
                continue
            index_have_data = True
            details.setdefault("index_drawdowns", {})[coin] = round(dd, 3)
            # dd is negative; a drop AS DEEP AS or deeper than -threshold trips.
            if dd <= -threshold and (worst_dd is None or dd < worst_dd):
                worst_dd = dd
                worst_coin = coin
        if worst_coin is not None:
            _arm("index_crash")
            reasons.append(
                f"index crash: {worst_coin} {worst_dd:.2f}% over last "
                f"{window_bars} bars (threshold -{threshold:.2f}%)")
            details["crash_coin"] = worst_coin
            details["crash_drawdown_pct"] = round(worst_dd, 3)

    # ── Trigger 2: correlated DSL-stop cluster ─────────────────────────────
    cluster_have_data = stop_events is not None
    if bool(cfg.get("stop_cluster_enabled", True)) and stop_events is not None:
        window_ms = int(_f("stop_cluster_window_s", 180) * 1000)
        min_coins = int(_f("stop_cluster_min_coins", 3))
        cutoff = now_ms - window_ms
        recent = [e for e in stop_events
                  if isinstance(e, dict) and int(e.get("ts_ms", 0)) >= cutoff]
        coins = sorted({str(e.get("coin")) for e in recent if e.get("coin")})
        details["stop_cluster_coins"] = coins
        details["stop_cluster_count"] = len(coins)
        if len(coins) >= min_coins:
            _arm("stop_cluster")
            reasons.append(
                f"stop cluster: {len(coins)} coins stopped within "
                f"{int(window_ms / 1000)}s ({', '.join(coins)}) "
                f"[threshold {min_coins}]")

    # ── Trigger 3: extreme funding on index coins ──────────────────────────
    if bool(cfg.get("funding_enabled", False)) and isinstance(funding_rates, dict):
        thresh = abs(_f("funding_extreme_frac", 0.005))
        extreme = {c: round(float(r), 5) for c, r in funding_rates.items()
                   if r is not None and abs(float(r)) >= thresh}
        details["funding_extreme"] = extreme
        if extreme:
            _arm("funding_extreme")
            who = ", ".join(f"{c}={r:+.4f}" for c, r in extreme.items())
            reasons.append(f"funding extreme: {who} (|rate|>={thresh:.4f})")

    data_ok = index_have_data or cluster_have_data or bool(funding_rates)
    return {
        "tripped": tripped,
        "trigger": trigger,
        "reasons": reasons,
        "details": details,
        "data_ok": data_ok,
    }


# ──────────────────────────────────────────────────────────────────────────
# Shadow JSONL (gray-release data pipeline; separate from event_log)
# ──────────────────────────────────────────────────────────────────────────
def shadow_log_path(cfg: dict[str, Any]) -> str:
    """Resolve the market_circuit shadow JSONL path (config → env → default)."""
    return str(cfg.get("shadow_log_path") or "").strip() or os.environ.get(
        "HERMES_MARKET_CIRCUIT_SHADOW_FILE",
        os.path.expanduser("~/.hermes-trading/market_circuit_shadow.jsonl"),
    )


def _record_shadow(rec: dict[str, Any], path: str) -> None:
    """Best-effort append a market-circuit shadow verdict to the JSONL."""
    import json
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError as e:
        logger.warning("[market_circuit] shadow write failed: %s", e)


# ──────────────────────────────────────────────────────────────────────────
# I/O evaluator (called once per loop tick; never raises into the loop)
# ──────────────────────────────────────────────────────────────────────────
class _StopClusterWindow:
    """Process-lifetime sliding window of recent DSL-stop events (ms ts)."""

    def __init__(self, maxlen: int = 256) -> None:
        self._events: deque = deque(maxlen=maxlen)

    def add(self, coin: str, ts_ms: Optional[int] = None) -> None:
        ts_ms = int(ts_ms if ts_ms is not None else time.time() * 1000)
        self._events.append({"ts_ms": ts_ms, "coin": str(coin)})

    def events(self) -> list[dict[str, Any]]:
        return list(self._events)


# Module-level window so stops accumulate across loop ticks (restart clears
# it — acceptable; a crash cluster is a sub-minute phenomenon).
_stop_window = _StopClusterWindow()


def record_stop(coin: str, ts_ms: Optional[int] = None) -> None:
    """Feed a DSL stop into the cluster window (called from the loop per exit)."""
    try:
        _stop_window.add(coin, ts_ms)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("[market_circuit] record_stop failed: %s", e)


def _gather_inputs(cfg: dict[str, Any], *,
                   candle_fetcher: Optional[Callable[..., list]] = None,
                   funding_fetcher: Optional[Callable[[str], Optional[float]]] = None,
                   closed_filter: Optional[Callable] = None) -> dict[str, Any]:
    """Fetch candles (closed-only) and funding for the watched index coins.

    Returns ``{"index_drawdowns": {...}, "funding_rates": {...}}``. Every fetch
    is wrapped per-coin: a single failure degrades that coin to ``None`` (fail
    open) rather than aborting the whole check.
    """
    if candle_fetcher is None:
        from hermes_trader.client.hl_client import (
            closed_candles_only,
            fetch_hl_candles,
        )
        candle_fetcher = fetch_hl_candles
        closed_filter = closed_candles_only
    interval = str(cfg.get("index_crash_interval", "5m") or "5m")
    window_bars = int(float(cfg.get("index_crash_window_bars", 3)))
    fetch_bars = max(window_bars + 3, int(float(cfg.get("fetch_bars", 20))))

    index_drawdowns: dict[str, Optional[float]] = {}
    for coin in DEFAULT_INDEX_COINS:
        try:
            raw = candle_fetcher(coin, interval, fetch_bars)
            closed, _ = (closed_filter(raw, interval)
                         if closed_filter is not None else (raw, False))
            index_drawdowns[coin] = window_drawdown_pct(closed, window_bars)
        except Exception as e:
            logger.warning("[market_circuit] candle fetch failed for %s: %s", coin, e)
            index_drawdowns[coin] = None

    funding_rates: dict[str, Optional[float]] = {}
    if bool(cfg.get("funding_enabled", False)) and funding_fetcher is not None:
        for coin in DEFAULT_INDEX_COINS:
            try:
                funding_rates[coin] = funding_fetcher(coin)
            except Exception as e:
                logger.warning("[market_circuit] funding fetch failed for %s: %s", coin, e)
                funding_rates[coin] = None
    return {"index_drawdowns": index_drawdowns, "funding_rates": funding_rates}


def _metric(mode: str, verdict: str) -> None:
    try:
        from hermes_trader import metrics
        metrics.MARKET_CIRCUIT_VERDICTS.labels(mode=mode, verdict=verdict).inc()
    except Exception:
        pass


def evaluate(cfg: dict[str, Any], *,
             mem: Optional[Any] = None,
             halt_minutes: Optional[float] = None,
             cooldown_minutes: Optional[float] = None,
             candle_fetcher: Optional[Callable[..., list]] = None,
             funding_fetcher: Optional[Callable[[str], Optional[float]]] = None,
             closed_filter: Optional[Callable] = None,
             notifier: Optional[Callable[..., None]] = None,
             event_log: Optional[Callable[..., None]] = None,
             stop_events: Optional[list[dict[str, Any]]] = None) -> dict[str, Any]:
    """Run one market-circuit evaluation for a loop tick.

    Reads ``mode`` from ``cfg``; ``off`` returns immediately without touching
    the network. On a trip in ``enforce`` mode, arms the global halt via
    ``mem.set_global_halt`` (the existing gate/flatten channel), fires a risk
    alert and writes an audit event. ``shadow`` mode never arms — it records a
    would-trigger shadow JSONL line and a warning log. All paths are wrapped;
    this function never raises.

    Returns the (always-present) verdict dict; on any failure a safe
    ``{"tripped": False, "data_ok": False, ...}`` is returned.
    """
    safe = {"tripped": False, "trigger": None, "reasons": [],
            "details": {}, "data_ok": False, "mode": "off", "action": "error"}
    try:
        if not isinstance(cfg, dict):
            return safe
        mode = str(cfg.get("mode", "off") or "off").lower()
        if mode not in ("off", "shadow", "enforce"):
            mode = "off"
        if mode == "off":
            return {"tripped": False, "trigger": None, "reasons": [],
                    "details": {}, "data_ok": True, "mode": "off", "action": "off"}

        halt_min = float(cfg.get("halt_minutes", 60.0) if halt_minutes is None
                         else halt_minutes)
        cooldown_min = float(cfg.get("cooldown_minutes", 60.0) if cooldown_minutes is None
                             else cooldown_minutes)

        # Cooldown: if a halt we armed is still in force, don't re-arm/re-alert
        # every tick. The global halt may also have been armed by other layers;
        # reading remaining-min is network-free and dedups both cases.
        remaining = 0.0
        if mem is not None:
            try:
                remaining = float(mem.global_halt_remaining_min() or 0.0)
            except Exception:
                remaining = 0.0

        gathered = _gather_inputs(
            cfg, candle_fetcher=candle_fetcher,
            funding_fetcher=funding_fetcher, closed_filter=closed_filter)
        verdict = decide(
            cfg,
            index_drawdowns=gathered["index_drawdowns"],
            stop_events=stop_events if stop_events is not None else _stop_window.events(),
            funding_rates=gathered["funding_rates"],
        )
        verdict["mode"] = mode

        if not verdict.get("data_ok"):
            logger.warning("[market_circuit] no usable market data — failing open")
            _metric(mode, "data_missing")
            verdict["action"] = "data_missing"
            return verdict

        if not verdict.get("tripped"):
            _metric(mode, "no_trip")
            verdict["action"] = "clear"
            return verdict

        # A trigger fired.
        trigger = verdict.get("trigger") or "market"
        reasons = "; ".join(verdict.get("reasons") or [])
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        rec = {
            "timestamp": ts,
            "mode": mode,
            "trigger": trigger,
            "tripped": True,
            "reasons": verdict.get("reasons", []),
            "details": verdict.get("details", {}),
            "halt_remaining_min": round(remaining, 1),
        }

        if remaining >= min(cooldown_min, halt_min):
            # A halt is already armed and within the cooldown window — record
            # but don't re-arm / re-alert (avoids alert spam on a sustained
            # crash leg).
            _metric(mode, "trip")
            verdict["action"] = "halt_already_armed"
            _record_shadow(rec, shadow_log_path(cfg))
            return verdict

        if mode == "shadow":
            logger.warning("[market_circuit] SHADOW would-trip (%s): %s", trigger, reasons)
            _metric(mode, "trip")
            verdict["action"] = "would_trip"
            _record_shadow(rec, shadow_log_path(cfg))
            return verdict

        # enforce: arm the existing global-halt channel.
        until_ms = int(time.time() * 1000) + int(halt_min * 60_000)
        armed = False
        if mem is not None:
            try:
                mem.set_global_halt(until_ms)
                armed = True
            except Exception as e:
                logger.error("[market_circuit] set_global_halt failed: %s", e)
        logger.critical(
            "[market_circuit] ENFORCE trip (%s) — global halt armed for %.0f min: %s",
            trigger, halt_min, reasons)
        _metric(mode, "trip")
        rec["action"] = "halt_armed"
        rec["halt_minutes"] = halt_min
        rec["armed"] = armed
        _record_shadow(rec, shadow_log_path(cfg))

        if notifier is not None:
            try:
                notifier(trigger=trigger, reasons=reasons, halt_min=halt_min)
            except Exception as e:
                logger.warning("[market_circuit] notify failed: %s", e)
        if event_log is not None:
            try:
                event_log({"event": "market_circuit_halt",
                           "trigger": trigger,
                           "reasons": verdict.get("reasons", []),
                           "details": verdict.get("details", {}),
                           "halt_minutes": halt_min,
                           "armed": armed})
            except Exception as e:
                logger.warning("[market_circuit] event log failed: %s", e)
        verdict["action"] = "halt_armed"
        verdict["armed"] = armed
        return verdict
    except Exception as e:
        logger.error("[market_circuit] evaluate failed (failing open): %s", e)
        _metric(str(cfg.get("mode", "off") if isinstance(cfg, dict) else "off"),
                "data_missing")
        return safe
