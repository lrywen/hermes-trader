"""Risk gates — every gate is a pure function returning {pass, reason?}.

All gates are evaluated; results are collected for telemetry (no short-circuit).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from hermes_trader.agents.config_store import cfg_get
from hermes_trader.shared_config import load_shared_config

logger = logging.getLogger(__name__)

GateResult = dict[str, Any]  # {pass: bool, reason?: str}


@dataclass
class GateContext:
    """Context passed to all risk gates.

    P2-4: now a dataclass. ``__post_init__`` coerces numerics/bools/strings
    to their declared types (upstream values arrive straight from LLM JSON /
    config / memory and may be ints, None, or strings), so individual gates
    no longer each repeat ``float(...)`` / ``bool(...)`` / ``... or ""``.
    Coercion is best-effort and fails safe: an unparseable numeric becomes
    0.0 (which fails closed for the positive-floor gates), a non-list
    positions value becomes ``[]``.
    """
    confidence: float
    current_positions: list[dict[str, Any]]
    trade_notional_usd: float
    daily_pnl: float
    market_volume_24h_usd: float
    coin: str
    trade_side: str  # 'long' or 'short'
    has_binary_news_risk: bool
    equity: float
    total_open_notional: float
    composite_score: float = 0.0
    momentum_burst_fired: bool = False
    slow_burn_fired: bool = False
    whale_signal_fired: bool = False
    binary_news_match: str = ""
    peak_daily_pnl: float = 0.0
    # (supplemental audit 2026-09-02) Today's REALIZED (locked-in, closed-trade)
    # PnL and its intraday high-water mark — exclude unrealized float so the
    # give-back gate arms only on profit actually banked. Default 0.0 keeps
    # callers that lack the ledger (manual path before its fix) fail-closed: no
    # realized peak => the give-back gate simply does not arm.
    daily_realized_pnl: float = 0.0
    peak_daily_realized_pnl: float = 0.0
    # H4 (deep audit 2026-08-29): liquidation-price pre-check inputs. Zero
    # values mean "not applicable" (manual-order path / caller without the
    # data) and liquidation_buffer_gate passes open. entry_px is the planned
    # fill price, leverage the cross-margin leverage about to be set, and
    # stop_distance_pct the planned worst-case stop distance in SPOT percent
    # (backup-SL width incl. slippage widen), so the gate can enforce
    # liq_distance > stop + sl_buffer.
    entry_px: float = 0.0
    leverage: float = 0.0
    stop_distance_pct: float = 0.0

    def __post_init__(self) -> None:
        def _num(v: Any) -> float:
            # bool is a subclass of int — keep it numeric-compatible here;
            # None / unparseable fail safe to 0.0.
            try:
                f = float(v)
                return f if f == f else 0.0  # NaN guard
            except (TypeError, ValueError):
                return 0.0

        self.confidence = _num(self.confidence)
        self.trade_notional_usd = _num(self.trade_notional_usd)
        self.daily_pnl = _num(self.daily_pnl)
        self.peak_daily_pnl = _num(self.peak_daily_pnl)
        # (supplemental audit 2026-09-02) coerce the realized-PnL inputs.
        self.daily_realized_pnl = _num(self.daily_realized_pnl)
        self.peak_daily_realized_pnl = _num(self.peak_daily_realized_pnl)
        self.market_volume_24h_usd = _num(self.market_volume_24h_usd)
        self.equity = _num(self.equity)
        self.total_open_notional = _num(self.total_open_notional)
        self.composite_score = _num(self.composite_score)
        # H4: pre-trade liquidation-check inputs (0.0 = not supplied → gate
        # passes open).
        self.entry_px = _num(self.entry_px)
        self.leverage = _num(self.leverage)
        self.stop_distance_pct = _num(self.stop_distance_pct)
        self.momentum_burst_fired = bool(self.momentum_burst_fired)
        # True iff any 1h slow-burn trigger fired (volumeBuildup1h /
        # trendFlip1h / higherLows1h). Used as a counter-regime bypass: a
        # clean 1h accumulation pattern overrides the slow BTC proxy.
        self.slow_burn_fired = bool(self.slow_burn_fired)
        # True iff whale_index oi_funding_anomaly flagged this coin
        # (negative funding + flat price + high OI = whale accumulation).
        # Same gate-bypass role as slow_burn_fired; orthogonal signal.
        self.whale_signal_fired = bool(self.whale_signal_fired)
        self.has_binary_news_risk = bool(self.has_binary_news_risk)
        # The headline + matched term that tripped the binary-news gate, for
        # log visibility ("which article blocked this?").
        self.binary_news_match = str(self.binary_news_match or "")
        self.coin = str(self.coin or "")
        self.trade_side = str(self.trade_side or "long")
        if not isinstance(self.current_positions, list):
            self.current_positions = []


def confidence_gate(ctx: GateContext, min_confidence: float) -> GateResult:
    if ctx.confidence >= min_confidence:
        return {"pass": True}
    return {"pass": False, "reason": f"confidence {ctx.confidence:.2f} < {min_confidence}"}


def max_concurrent_positions_gate(ctx: GateContext, max_concurrent: int) -> GateResult:
    if len(ctx.current_positions) < max_concurrent:
        return {"pass": True}
    return {"pass": False, "reason": f"max positions reached ({len(ctx.current_positions)}/{max_concurrent})"}


def per_trade_notional_cap_gate(ctx: GateContext, cap_usd: float) -> GateResult:
    cap = float(cap_usd or 0)
    if cap <= 0:
        return {"pass": True}
    # The executor normalizes the target notional into an exchange-valid coin
    # size before gates. Coin precision can create a few cents/dollars of cap
    # dust, e.g. target $650.00 -> valid size worth $650.05. Treat that as
    # still capped; larger overshoots remain blocked.
    precision_tolerance = max(0.25, cap * 0.005)
    if ctx.trade_notional_usd <= cap + precision_tolerance:
        return {"pass": True}
    return {"pass": False, "reason": f"trade notional ${ctx.trade_notional_usd:.2f} exceeds cap ${cap:.2f}"}


def daily_loss_kill_switch(ctx: GateContext, max_daily_loss: float) -> GateResult:
    if ctx.daily_pnl > max_daily_loss:
        return {"pass": True}
    return {"pass": False, "reason": f"daily loss killswitch triggered (PnL ${ctx.daily_pnl:.0f} <= ${max_daily_loss})"}


def daily_giveback_gate(ctx: GateContext, halt_pct: float, min_peak_usd: float) -> GateResult:
    """Lock in a green day: once REALIZED daily PnL has peaked at >=
    `min_peak_usd`, block NEW positions if it then retraces more than
    `halt_pct` from that peak. Existing positions keep riding their own stops;
    this only stops opening fresh risk so a won day can't fully round-trip.
    Disabled when halt_pct<=0. Resets at the UTC day roll.

    (supplemental audit 2026-09-02) Arming and retracement are measured off
    REALIZED (locked-in closed-trade) PnL — `peak_daily_realized_pnl` /
    `daily_realized_pnl` — NOT the mark-to-market `peak_daily_pnl` /
    `daily_pnl`, which include unrealized float. The old MTM peak latched the
    gate the moment an open position's paper PnL spiked past `min_peak_usd`;
    when that float later evaporated (price mean-reverted or the trade closed
    near breakeven) the gate read it as a >halt_pct give-back and blocked every
    fresh entry until the UTC roll — even though no profit had ever been banked
    to give back. Realized PnL only rises on an actual close, so the gate now
    arms exclusively on profit truly taken. Callers that don't supply realized
    values pass 0.0, which never arms (fail-closed: no locked-in profit => no
    give-back to protect, and no false block)."""
    if halt_pct <= 0:
        return {"pass": True}
    peak_realized = ctx.peak_daily_realized_pnl
    realized = ctx.daily_realized_pnl
    if peak_realized < min_peak_usd:
        return {"pass": True}
    floor = peak_realized * (1.0 - halt_pct)
    if realized <= floor:
        return {"pass": False,
                "reason": (f"daily give-back halt: realized PnL ${realized:.2f} retraced "
                           f">{halt_pct*100:.0f}% from realized peak ${peak_realized:.2f} "
                           f"(floor ${floor:.2f}) — no new entries until UTC roll")}
    return {"pass": True}


def market_liquidity_floor(
    ctx: GateContext,
    min_volume: float,
    min_volume_hip3: Optional[float] = None,
) -> GateResult:
    """Block trades on markets with insufficient 24h notional volume.

    HIP-3 tokenized-equity / commodity perps live on separate dexs and
    naturally carry less volume than BTC/ETH-style native markets (most
    `xyz:*` markets sit in the $1M–$50M range vs $1B+ for BTC). Applying
    the same 5M crypto floor incorrectly blocks adequately-liquid HIP-3
    markets like xyz:CRCL ($4.7M) and km:USTECH ($1.06M). When the coin
    is HIP-3 (colon-namespaced) and a separate `min_volume_hip3` is set,
    use that floor instead.
    """
    is_hip3 = ":" in (ctx.coin or "")
    floor = (min_volume_hip3 if (is_hip3 and min_volume_hip3 is not None) else min_volume)
    if ctx.market_volume_24h_usd >= floor:
        return {"pass": True}
    return {"pass": False, "reason": f"market 24h volume ${ctx.market_volume_24h_usd/1e6:.2f}M below floor ${floor/1e6:.2f}M"}


def short_liquidity_floor(ctx: GateContext, min_short_volume: float) -> GateResult:
    """SHORTS need materially more liquidity than longs — thin markets squeeze.

    Data (72h short segmentation): short BLEEDERS had a median 24h volume of
    ~$13M (XPL 0%/5 win, xyz:LITE -6.7%/10, PUMP, xyz:EWZ) while short WINNERS
    (XMR/TON/DOGE/BTC/ETH + commodities) had ~$223M — a 17x gap. Low-liquidity
    shorts ran to max_loss (the entire short bleed was 14 stopped shorts). Longs
    can tolerate a thin pump; a thin short gets squeezed. Applies ONLY to shorts;
    0/None disables (opt-in, reversible)."""
    if ctx.trade_side != "short" or not min_short_volume:
        return {"pass": True}
    if ctx.market_volume_24h_usd >= min_short_volume:
        return {"pass": True}
    return {"pass": False,
            "reason": (f"short on thin market: 24h vol ${ctx.market_volume_24h_usd/1e6:.1f}M "
                       f"< short floor ${min_short_volume/1e6:.0f}M (squeeze risk)")}


def coin_allowlist_gate(ctx: GateContext, allowlist: list[str], blocklist: list[str]) -> GateResult:
    if blocklist and ctx.coin in blocklist:
        return {"pass": False, "reason": f"{ctx.coin} is on the coin blocklist"}
    if allowlist and ctx.coin not in allowlist:
        return {"pass": False, "reason": f"{ctx.coin} not on the allowlist"}
    return {"pass": True}


def cooldown_gate(ctx: GateContext, last_trade_time: Optional[int], cooldown_min: float) -> GateResult:
    if last_trade_time is None:
        return {"pass": True}
    elapsed = (int(time.time() * 1000) - last_trade_time) / 60_000
    if elapsed >= cooldown_min:
        return {"pass": True}
    return {"pass": False, "reason": f"cooldown active ({int(cooldown_min - elapsed)}min remaining)"}


def coin_circuit_breaker_gate(ctx: GateContext) -> GateResult:
    """Block re-entry on a single coin while its per-trade loss breaker is armed.

    Reads the memory state set by the close chokepoint (a realized spot loss
    beyond circuit_breaker.single_coin_loss_pct halts that coin for
    single_coin_halt_min). Sits ON TOP of the legacy loss cooldown — it uses
    a larger threshold (3%) and a shorter, sharper window (60min) specifically
    to stop immediate re-buying after a stop-out (PURR #6 / BOME class).
    Memory is imported lazily to avoid a circular import at module load.
    """
    try:
        from hermes_trader.agents.memory import memory
        remaining = float(memory.coin_circuit_remaining_min(ctx.coin) or 0.0)
        if remaining > 0:
            return {"pass": False,
                    "reason": f"coin circuit breaker active on {ctx.coin} ({int(remaining)}min remaining)"}
    except Exception as e:  # noqa: BLE001 — a state-read failure must NOT block trading
        logger.debug(f"[risk] coin-circuit gate state read failed for {ctx.coin}: {e}")
    return {"pass": True}


def global_halt_gate(ctx: GateContext) -> GateResult:
    """Block ALL new entries while the daily cumulative-loss halt is armed.

    Reads the memory state set by the close chokepoint (daily realized +
    unrealized loss beyond circuit_breaker.daily_loss_pct of start-of-day
    equity halts the whole book for daily_halt_min). This is an equity-based
    hard stop complementing the USD-denominated daily_loss_kill_switch.
    """
    try:
        from hermes_trader.agents.memory import memory
        remaining = float(memory.global_halt_remaining_min() or 0.0)
        if remaining > 0:
            return {"pass": False,
                    "reason": f"global daily-loss halt active ({int(remaining)}min remaining)"}
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[risk] global-halt gate state read failed: {e}")
    return {"pass": True}


def consecutive_loss_gate(ctx: GateContext, limit: int) -> GateResult:
    """B-F2 (deep audit 2026-08-28): block re-entry on a coin after `limit`
    CONSECUTIVE losing closes.

    The close chokepoint already records the streak
    (memory.record_loss_outcome → _consecutive_losses[coin]); it resets on any
    winning close and at UTC day roll. Before this gate the counter was
    recorded but NOTHING read it, so the "consecutive-loss halt" of the 16
    defined risk controls was dead. A state-read failure fails OPEN (the
    existing cooldown/circuit breakers still apply); the streak itself is a
    pure non-negative int. Disabled when limit <= 0.
    """
    if limit <= 0:
        return {"pass": True}
    try:
        from hermes_trader.agents.memory import memory
        streak = int(memory.consecutive_losses(ctx.coin) or 0)
        if streak >= limit:
            return {"pass": False,
                    "reason": f"consecutive-loss halt: {ctx.coin} has {streak} "
                              f"losing closes in a row (>= {limit}) — no entries "
                              f"until a winning close or UTC roll"}
    except Exception as e:  # noqa: BLE001 — same fail-open-on-read-error convention as the breakers
        logger.debug(f"[risk] consecutive-loss gate state read failed for {ctx.coin}: {e}")
    return {"pass": True}


def per_coin_daily_loss_gate(ctx: GateContext, max_loss_pct: float) -> GateResult:
    """B-F6: block a coin whose CUMULATIVE realized loss today has reached
    `max_loss_pct` (%, of start-of-day equity).

    memory.coin_daily_realized_pnl_pct() sums today's closed-Trade PnL for the
    coin (a NEGATIVE number when net down). This catches the many-small-losses
    accumulation that the per-trade single_coin_loss_pct breaker misses (no
    single stop hits 3%, but ten -0.4% stops on the same name add up).
    Disabled when max_loss_pct <= 0 or there is no usable baseline equity yet.
    Read failure fails open (coin_circuit remains the independent backstop).
    """
    if max_loss_pct <= 0:
        return {"pass": True}
    try:
        from hermes_trader.agents.memory import memory
        sod_equity = float(memory.get_start_of_day_equity() or 0.0)
        if sod_equity <= 0:
            return {"pass": True}  # no baseline yet (pre-first-tick) → nothing to measure
        pnl_pct = float(memory.coin_daily_realized_pnl_pct(ctx.coin, sod_equity) or 0.0)
        if pnl_pct <= -max_loss_pct:
            return {"pass": False,
                    "reason": f"per-coin daily loss halt: {ctx.coin} realized "
                              f"{pnl_pct:.2f}% today (<= -{max_loss_pct:.1f}% of "
                              f"SOD equity) — no more entries on this coin today"}
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[risk] per-coin daily-loss gate state read failed for {ctx.coin}: {e}")
    return {"pass": True}


def drawdown_gate(ctx: GateContext, max_drawdown_pct: float) -> GateResult:
    """B-F7 (fixed 2026-09-03): block ALL new entries when account equity has
    fallen more than `max_drawdown_pct` (%) below its high-water mark.

    Recovery fix — the gate originally measured against the ALL-TIME peak,
    which only ever rises. After a permanent equity drop (realized loss,
    withdrawal, balance rebase) the drawdown stayed above threshold forever
    with NO recovery path: observed peak $50.9 vs equity $20.9 (−58.9%) latched
    the gate from 2026-09-01 04:19 onward, blocking 100% of entries (111
    consecutive blocks). The gate now:

      * measures against a ROLLING peak over ``drawdown_peak_window_days``
        (default 14d) instead of the all-time high — old peaks age out, so a
        permanently lower balance stops looking like a fresh drawdown;
      * RE-BASELINES after ``drawdown_cooldown_hours`` (default 24h) of a
        continuous freeze — entries resume once even if the rolling window
        hasn't aged the peak out yet (bounded recovery, no permanent lock);
      * on the FIRST gate evaluation after the upgrade (empty equity trail —
        legacy memory), an over-threshold drawdown is re-baselined once
        immediately, releasing the pre-existing latched deadlock.

    Disabled when max_drawdown_pct <= 0 or no reference peak exists yet (fail
    open on missing reference; fail closed once a real peak exists). Read
    failure fails open.
    """
    if max_drawdown_pct <= 0:
        return {"pass": True}
    try:
        from hermes_trader.agents.memory import memory
        window_days = float(cfg_get(
            "circuit_breaker.drawdown_peak_window_days", 14.0) or 0.0)
        cooldown_hours = float(cfg_get(
            "circuit_breaker.drawdown_cooldown_hours", 24.0) or 0.0)
        equity = ctx.equity
        if equity <= 0:
            return {"pass": True}
        # Legacy-memory one-shot: no rolling trail yet (pre-upgrade store).
        # If the OLD all-time peak is already latched beyond threshold,
        # re-baseline to current equity once so the deadlock lifts immediately
        # on deploy instead of waiting a full cooldown / window.
        legacy_peak = memory.peak_equity()
        trail_len = len(memory._equity_trail)  # noqa: SLF001 (same package; read-only)
        if trail_len == 0 and legacy_peak > 0:
            legacy_dd = (legacy_peak - equity) / legacy_peak * 100.0
            if legacy_dd >= max_drawdown_pct:
                # rebase_drawdown_peak() clears and re-seeds the rolling trail
                # at the new baseline, so the subsequent
                # rolling_peak_equity() reads the rebased peak instead of the
                # latched all-time high.
                memory.rebase_drawdown_peak(
                    equity,
                    reason=(f"legacy all-time peak ${legacy_peak:.2f} latched "
                            f"({legacy_dd:.1f}% drawdown) — one-shot rebase on "
                            f"upgraded rolling-peak gate"))
        peak = float(memory.rolling_peak_equity(window_days) or 0.0)
        if peak <= 0:
            return {"pass": True}  # no reference peak yet
        dd_pct = (peak - equity) / peak * 100.0
        if dd_pct >= max_drawdown_pct:
            since_ms = memory.mark_drawdown_frozen()
            frozen_min = max(0.0, (int(time.time() * 1000) - since_ms) / 60_000)
            # Cooldown recovery: a continuous freeze lasting cooldown_hours
            # re-baselines the peak to current equity and lets entries resume.
            if cooldown_hours > 0 and frozen_min >= cooldown_hours * 60.0:
                memory.rebase_drawdown_peak(
                    equity,
                    reason=(f"drawdown freeze held for {frozen_min / 60:.1f}h "
                            f">= cooldown {cooldown_hours:.0f}h"))
                logger.warning(
                    "[risk] drawdown gate cooldown recovery after %.1fh freeze "
                    "— entries re-armed at equity $%.2f", frozen_min / 60.0, equity)
                return {"pass": True}
            remain_min = max(0.0, cooldown_hours * 60.0 - frozen_min)
            return {"pass": False,
                    "reason": f"account drawdown halt: equity ${equity:.0f} is "
                              f"{dd_pct:.1f}% below {window_days:.0f}d peak ${peak:.0f} "
                              f"(>= {max_drawdown_pct:.1f}%) — new entries frozen "
                              f"for {frozen_min / 60:.1f}h (re-arm in {remain_min / 60:.1f}h)"}
        # Below threshold: ensure a stale freeze episode clears on recovery.
        memory.clear_drawdown_freeze()
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[risk] drawdown gate state read failed: {e}")
    return {"pass": True}


def liquidation_buffer_gate(
    ctx: GateContext,
    maint_margin_rate_pct: float,
    extra_buffer_pct: float,
) -> GateResult:
    """H4 (deep audit 2026-08-29): refuse an entry whose estimated liquidation
    price sits INSIDE the planned stop-loss bracket.

    On a thin-margin account (10U equity) a single order at 10x with a 3%
    backup stop is structurally guaranteed to be liquidated before the stop
    can fire (liq ~10% away, stop at 3%+slippage). This gate estimates the
    liquidation move from (entry_px, leverage, maintenance-margin rate) and
    enforces the audit contract::

        liq_distance_pct > stop_distance_pct + extra_buffer_pct

    The isolated-margin estimate ``(1/lev - mmr)`` is deliberately
    CONSERVATIVE for cross margin (real cross liq is further away because
    account-wide collateral backs the position), so it only rejects orders
    that would be fatal even in isolation. Disabled when maint_margin_rate_pct
    <= 0 or when the caller did not provide entry/leverage/stop data
    (zero-field contexts — e.g. the manual-order path — pass open).
    """
    if maint_margin_rate_pct <= 0:
        return {"pass": True}
    entry = float(ctx.entry_px or 0.0)
    lev = float(ctx.leverage or 0.0)
    stop_pct = float(ctx.stop_distance_pct or 0.0)
    if entry <= 0 or lev <= 0 or stop_pct <= 0:
        return {"pass": True}  # no pre-trade data supplied → nothing to check
    liq_distance_pct = (1.0 / lev) * 100.0 - float(maint_margin_rate_pct)
    if liq_distance_pct <= 0:
        return {"pass": False,
                "reason": f"liquidation_buffer: leverage {lev:g}x leaves no "
                          f"liq cushion at all (maint margin "
                          f"{maint_margin_rate_pct:.2f}%) — entry refused"}
    required_pct = stop_pct + float(extra_buffer_pct or 0.0)
    if liq_distance_pct <= required_pct:
        return {"pass": False,
                "reason": f"liquidation_buffer: {ctx.coin} {ctx.trade_side} at "
                          f"{lev:g}x would be liquidated ~{liq_distance_pct:.2f}% "
                          f"from entry, inside the planned stop bracket "
                          f"({stop_pct:.2f}% + {float(extra_buffer_pct or 0.0):.2f}% "
                          f"buffer = {required_pct:.2f}%) — entry refused (reduce "
                          f"leverage so 1/lev - maint > stop + buffer)"}
    return {"pass": True}


def opposite_direction_guard(ctx: GateContext) -> GateResult:
    """Block ANY re-entry on a coin we already hold. A held position is managed
    solely by the DSL engine + the periodic AI close-check (CLOSE / HOLD); it is
    never flipped (opposite side = no auto-flip) NOR added to (same side =
    uncontrolled pyramid). The held-coin close-check sometimes returns a fresh
    LONG/SHORT on a strong held name; without this it would try to pyramid in
    (previously only the exchange margin check stopped it)."""
    # P1-3: .get() defensively — a malformed position record (missing coin/side)
    # must not raise KeyError and abort the whole gate evaluation.
    # B-M4 (deep audit 2026-08-28): a held record missing 'side' is a corrupted
    # / tampered position state — we cannot tell whether the intended trade is a
    # flip or a pyramid, so BOTH are dangerous. Fail CLOSED (block) instead of
    # failing open; production position records always carry 'side' (built from
    # asset_positions szi sign in executor, backfilled by active_position_coins),
    # so this only trips on real corruption.
    existing = next((p for p in ctx.current_positions if p.get("coin") == ctx.coin), None)
    if not existing:
        return {"pass": True}
    held_side = existing.get("side")
    if not held_side:
        logger.error(f"[risk] opposite_direction_guard: held position on {ctx.coin} "
                     f"has malformed record (missing 'side' key) — fail-CLOSED: "
                     f"entry blocked to prevent auto-flip/pyramid on unknown state")
        return {"pass": False,
                "reason": f"malformed_position: held {ctx.coin} record missing 'side' "
                          f"— fail-closed (no auto-flip/pyramid on unknown state)"}
    if held_side != ctx.trade_side:
        return {"pass": False, "reason": f"opposite position exists ({ctx.coin} {held_side}) — no auto-flip"}
    return {"pass": False, "reason": f"already holding {ctx.coin} {held_side} — no pyramid/re-entry"}


# Major crypto coins for correlation cap
_CRYPTO_COINS = frozenset([
    "BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "MATIC", "LINK",
    "DOT", "UNI", "ATOM", "NEAR", "FTM", "APT", "ARB", "OP", "INJ", "TIA",
    "SUI", "SEI", "WIF", "PEPE", "BONK", "FLOKI", "TRX", "LTC", "BCH", "ETC",
    "XLM", "ALGO", "AAVE", "MKR", "SNX", "CRV", "COMP", "YFI", "SUSHI", "1INCH",
])


def correlation_cap(ctx: GateContext, max_crypto_correlated: int) -> GateResult:
    # Only cap long correlation
    if ctx.trade_side != "long":
        return {"pass": True}
    existing_crypto_long = sum(
        1 for p in ctx.current_positions
        if p.get("coin") in _CRYPTO_COINS and p.get("side") == "long"
    )
    if existing_crypto_long < max_crypto_correlated:
        return {"pass": True}
    return {"pass": False, "reason": f"crypto long correlation cap reached ({existing_crypto_long}/{max_crypto_correlated})"}


def equity_risk_cap(ctx: GateContext, max_total_notional_pct: float) -> GateResult:
    max_notional = ctx.equity * max_total_notional_pct
    projected_notional = ctx.total_open_notional + ctx.trade_notional_usd
    if projected_notional <= max_notional:
        return {"pass": True}
    return {
        "pass": False,
        "reason": f"total notional ${projected_notional:.0f} would exceed {max_total_notional_pct*100:.0f}% of equity (${max_notional:.0f})",
    }


def _funding_regime_for(coin: str) -> str:
    """Funding regime for this coin's asset class, fail-open to NEUTRAL.

    PER-CLASS LOOKUP: the gate uses the funding regime of THIS coin's
    asset class (crypto / equity / commodity), not a global crypto-only
    signal. Without this, a SHORT_CROWDED crypto regime would gate longs
    on oil (xyz:CL) and semis (xyz:ARM) — those have their own funding
    markets and shouldn't be evaluated by the crypto crowd.
    """
    try:
        from hermes_trader.agents.hyperfeed import market_get_funding_regime
        from hermes_trader.agents.market_regime import classify_asset
        funding_data = market_get_funding_regime()
        coin_class = classify_asset(coin)
        by_class = funding_data.get("regimes_by_class") or {}
        return by_class.get(coin_class) or funding_data.get("regime", "NEUTRAL")
    except Exception as err:
        # H4: a funding-data outage used to fail-open the crowding gate
        # invisibly — crowded/contra trades slipped through with no trace.
        # Log at WARNING (rate-limited by the natural scan cadence) so
        # operators can see the gate is running blind, while still degrading
        # to NEUTRAL to avoid hard-blocking all entries on a transient blip.
        logger.warning(
            f"[risk_gates] funding regime unavailable for {coin} "
            f"(class lookup failed: {type(err).__name__}: {err}) — "
            f"degrading gate to NEUTRAL (fail-open, visibility restored)"
        )
        return "NEUTRAL"


def _chop_decision(ctx: GateContext, base: dict[str, Any],
                   counter_regime_min_conf: float,
                   block_counter_trend_bypass: bool) -> GateResult:
    """Gate call for a chop tape (ADX<20, EMA-neutral).

    Unlike 'neutral' (which free-passes), chop RAISES the bar — both long
    and short are technically counter-trend in a range, and fakeout
    breakouts are the dominant loss mode here. Require real conviction
    (conf/score) OR a momentum burst (a genuine impulse out of the range);
    a lone slow_burn / whale ping does NOT clear it (those fire constantly
    in chop).
    """
    chop_min_conf = max(counter_regime_min_conf,
                        float(cfg_get("chop_min_conf")))
    chop_min_score = float(cfg_get("chop_min_score"))
    if ctx.confidence >= chop_min_conf or ctx.composite_score >= chop_min_score:
        return {"pass": True, "via": "chop_conviction",
                **{**base, "chop": True}}
    # P1-4: a lone momentum burst in chop is a classic wick-fakeout; require
    # a minimum composite score as confirmation (config: chop_burst_min_score,
    # default 20) so the bypass only fires for a genuine impulse out of range.
    chop_burst_min_score = float(cfg_get("chop_burst_min_score"))
    if (ctx.momentum_burst_fired and not block_counter_trend_bypass
            and ctx.composite_score >= chop_burst_min_score):
        return {"pass": True, "via": "trigger:momentum_burst",
                **{**base, "chop": True}}
    return {"pass": False, "via": "chop_blocked",
            **{**base, "chop": True, "counter_trend": True},
            "reason": (f"chop regime (ADX<20) — {ctx.trade_side} needs "
                       f"conf >= {chop_min_conf:.2f} or score >= {chop_min_score:.0f}"
                       f" or momentum burst, have conf {ctx.confidence:.2f}, "
                       f"score {ctx.composite_score:.0f}")}


def _counter_trend_decision(ctx: GateContext, base: dict[str, Any],
                            regime: str, funding_regime: str,
                            aligned: bool, weak_aligned: bool,
                            effective_min_conf: float,
                            effective_min_score: float,
                            block_counter_trend_bypass: bool) -> GateResult:
    """Counter-trend and/or against-funding bar.

    The trade must clear the (possibly elevated) bar via conviction or
    own-signal. `weak_aligned` trades are EMA-aligned but failed the
    continuous score; they face the same bar as counter-trend (no free
    pass on a weak cross).
    """
    base["counter_trend"] = (not aligned) or weak_aligned
    if weak_aligned:
        base["weak_trend_score"] = True
    if ctx.confidence >= effective_min_conf:
        return {"pass": True, "via": "confidence", **base}
    if ctx.composite_score >= effective_min_score:
        return {"pass": True, "via": "composite", **base}
    # Binary-trigger bypass: a strong own-coin signal (momentum_burst /
    # slow_burn / whale) normally overrides the slow macro-regime call.
    # `block_counter_trend_bypass` (config, default False, reversible)
    # DISABLES this bypass here — i.e. for trades that are already
    # counter-trend and/or against the funding crowd. Data (journal
    # P166-P177, ~-7% drawdown) showed low-conviction LONGS forced through
    # via `trigger:slow_burn` against a DOWN tape (SP500/MU/ORCL longs) and
    # bleeding. With the flag on, a counter-regime trade must clear REAL
    # conviction (conf/score); a lone momentum trigger no longer pushes it
    # through against the regime. Aligned and neutral-regime trades returned
    # earlier and are UNAFFECTED, so this does NOT blanket-weaken the bypass
    # — only where it fights a strong directional regime.
    if (ctx.momentum_burst_fired or ctx.slow_burn_fired or ctx.whale_signal_fired) \
            and not block_counter_trend_bypass:
        trig = ("momentum_burst" if ctx.momentum_burst_fired
                else "slow_burn" if ctx.slow_burn_fired else "whale")
        return {"pass": True, "via": f"trigger:{trig}", **base}

    blocked_via = "blocked_bypass" if block_counter_trend_bypass else "blocked"
    return {
        "pass": False,
        "via": blocked_via,
        **base,
        "reason": (f"counter-regime {ctx.trade_side} vs {regime} trend "
                   f"(funding={funding_regime}) — need conf >= {effective_min_conf:.2f} "
                   f"or score >= {effective_min_score:.0f}"
                   f"{'' if block_counter_trend_bypass else ' or own-coin signal'}, "
                   f"have conf {ctx.confidence:.2f}, score {ctx.composite_score:.0f}"),
    }


def market_regime_gate(ctx: GateContext, counter_regime_min_conf: float = 0.7,
                       block_counter_trend_bypass: bool = False,
                       crowded_with_min_conf: float = 0.0,
                       min_trend_score: float = 0.0) -> GateResult:
    """Block counter-regime trades unless conviction OR own-coin signal clears the bar.

      - aligned with regime → pass, BUT if min_trend_score>0 the 5-component
        continuous strength score must also be >= min_trend_score (calibrated
        0.55). This cuts the ~36% false-trend rate where EMA20/50 crosses on
        weak/noise bars that the backtest score correctly rates NEUTRAL/CHOP.
      - regime neutral      → pass (subject to funding-regime override below)
      - counter-trend trade → pass if any of:
          * confidence >= counter_regime_min_conf
          * composite_score >= 50
          * momentumBurst fired (large fast move on 5m)
          * slow_burn_fired (1h vol surge or EMA cross — accumulation breakout)
        else block.

    The own-signal bypasses exist because the regime proxy (BTC for crypto,
    SP500 for equity) is slow; a strong individual signal should override
    a stale macro call.

    Funding-regime overlay (added 2026): SYMMETRIC enforcement — when the
    market-wide funding regime is crowded, any trade going AGAINST the crowd
    direction must clear a higher bar. This is direction-agnostic and will
    apply the same way when the regime flips:

      * SHORT_CROWDED + long  → counter-regime, elevated bar
      * LONG_CROWDED  + short → counter-regime, elevated bar
      * SHORT_CROWDED + short → aligned, normal bar (no bias added)
      * LONG_CROWDED  + long  → aligned, normal bar (no bias added)

    Elevated bar = confidence >= max(counter_regime_min_conf, 0.85)
                   OR composite_score >= 60
                   OR any binary trigger (momentumBurst / slow_burn / whale_signal)

    The bypass triggers are preserved on both sides — those are explicit
    "the regime proxy is stale" signals and we never want to hard-block on
    a clear individual setup, just enforce regime discipline by default.
    """
    from hermes_trader.agents.market_regime import detect_regime_with_score
    regime, trend_score = detect_regime_with_score(ctx.coin)

    # Funding regime (cached) used as a symmetric overlay on the trend-regime
    # gate. Both directions are treated identically: anything going against
    # the crowded side faces the elevated bar. Extracted to _funding_regime_for
    # (P2-1): per-class lookup, fail-open to NEUTRAL with WARNING visibility.
    funding_regime = _funding_regime_for(ctx.coin)

    # Symmetric counter-funding-regime detection.
    against_funding = (
        (funding_regime == "SHORT_CROWDED" and ctx.trade_side == "long") or
        (funding_regime == "LONG_CROWDED"  and ctx.trade_side == "short")
    )
    # WITH-crowd (squeeze-prone): trading the SAME side the crowd is already on
    # (short into SHORT_CROWDED / long into LONG_CROWDED). These are trend-aligned
    # but are exactly what gets squeezed on a reversal — they round-tripped the
    # 2026-06-06 day. Require elevated conviction so only strong setups join a
    # crowded book. Gated by crowded_with_min_conf (0 = off).
    with_crowd = (
        (funding_regime == "SHORT_CROWDED" and ctx.trade_side == "short") or
        (funding_regime == "LONG_CROWDED"  and ctx.trade_side == "long")
    )

    # Effective thresholds: only elevated when against the funding regime.
    # When aligned with funding regime, use the normal counter_regime_min_conf
    # so we never *raise* the bar for regime-aligned trades.
    effective_min_conf = counter_regime_min_conf
    # R13-B3: the normal counter-trend composite_score bar was a hardcoded
    # 50.0 literal at this site; register it under
    # analyst_scoring.counter_trend_min_score so an operator can retune
    # the bar without redeploying. The canonical default = 50.0, so the
    # runtime bar is unchanged. Hot-path cfg_get picks up env overrides
    # (HERMES_CFG_ANALYST_SCORING__COUNTER_TREND_MIN_SCORE) and
    # .agent-config.json edits on the next gate call.
    effective_min_score = float(
        cfg_get("analyst_scoring.counter_trend_min_score", config={})
    )
    if against_funding:
        # P3 defense: an empty-string env override (HERMES_CFG_...='') makes
        # cfg_get return '', and float('') raises ValueError. `or default`
        # coerces ''/None to the elevated-bar defaults (0.85 conf / 60 score).
        try:
            _af_min_conf = float(cfg_get("against_funding_min_conf") or 0.85)
        except (TypeError, ValueError):
            _af_min_conf = 0.85
        try:
            _af_min_score = float(cfg_get("against_funding_min_score") or 60.0)
        except (TypeError, ValueError):
            _af_min_score = 60.0
        effective_min_conf = max(counter_regime_min_conf, _af_min_conf)
        effective_min_score = _af_min_score

    # Context attached to every result so the log reads "why" without
    # re-deriving regime state after the fact.
    base = {"regime": regime, "trend_score": round(trend_score, 3),
            "funding": funding_regime,
            "against_funding": against_funding, "counter_trend": False}

    # Aligned with trend regime AND not against funding regime → easy pass,
    # UNLESS it's a with-crowd (squeeze-prone) entry that fails the elevated
    # conviction bar — those are the crowded shorts/longs that round-trip on a
    # squeeze, so a weak one is blocked here.
    aligned = (regime == "up" and ctx.trade_side == "long") or \
              (regime == "down" and ctx.trade_side == "short")
    # Continuous strength overlay: the 4-state EMA20/50 cross fires on
    # weak/noise bars (audit: 36% false-trend rate). Demote an "aligned" entry
    # whose 5-component score is below min_trend_score to the counter-trend bar
    # below, so a high-conviction own-coin signal (conf/score/momentum burst)
    # can still clear it but a weak free-pass no longer does. min_trend_score=0
    # disables the overlay (reverts to the original always-pass-aligned behavior).
    weak_aligned = aligned and min_trend_score > 0 and trend_score < min_trend_score
    if aligned and not against_funding and not weak_aligned:
        if with_crowd and crowded_with_min_conf > 0 and ctx.confidence < crowded_with_min_conf:
            return {"pass": False, "via": "crowded_squeeze",
                    **{**base, "with_crowd": True},
                    "reason": (f"with-crowd {ctx.trade_side} into {funding_regime} "
                               f"(squeeze risk) — need conf >= {crowded_with_min_conf:.2f}, "
                               f"have {ctx.confidence:.2f}")}
        return {"pass": True, "via": "aligned", **{**base, "with_crowd": with_crowd}}

    # Trend-regime neutral and not against funding regime → pass.
    if regime == "neutral" and not against_funding:
        return {"pass": True, "via": "neutral", **base}

    # Chop regime (ADX<20, EMA-neutral): a directionless whipsaw tape. Unlike
    # 'neutral' (which free-passes), chop RAISES the bar — both long and short
    # are technically counter-trend in a range, and fakeout breakouts are the
    # dominant loss mode here. Require real conviction (conf/score) OR a
    # momentum burst (a genuine impulse out of the range); a lone slow_burn /
    # whale ping does NOT clear it (those fire constantly in chop).
    if regime == "chop" and not against_funding:
        return _chop_decision(ctx, base, counter_regime_min_conf,
                              block_counter_trend_bypass)

    # Past here the trade is counter-trend and/or against the funding crowd —
    # the (possibly elevated) conviction/own-signal bar lives in the helper.
    return _counter_trend_decision(
        ctx, base, regime, funding_regime, aligned, weak_aligned,
        effective_min_conf, effective_min_score, block_counter_trend_bypass)


def news_blackout_gate(ctx: GateContext) -> GateResult:
    if not ctx.has_binary_news_risk:
        return {"pass": True}
    detail = f" — {ctx.binary_news_match}" if ctx.binary_news_match else ""
    return {"pass": False,
            "reason": f"binary news risk (Fed/earnings/hack in recent news){detail} — standing down"}


_debate_defaults_warned = False


def _warn_debate_defaults() -> None:
    """One-time warning: debate_gate is running with no explicit config
    section (fail-closed default-on, P0-A)."""
    global _debate_defaults_warned
    if not _debate_defaults_warned:
        _debate_defaults_warned = True
        logger.warning(
            "[risk_gates] debate_gate enabled with IMPLICIT defaults "
            "(no debate_gate section in config) — min_agreement=0.6, "
            "min_agree_count=3, analyst3_default=False. Set "
            "debate_gate.enabled=false explicitly to disable."
        )


def debate_gate(
    ctx: GateContext,
    config: dict[str, Any],
) -> GateResult:
    """Multi-agent debate risk gate (B4).

    Simulates a lightweight multi-analyst debate by checking the trade's
    signal strength across multiple dimensions:
      - confidence consensus: high confidence + composite score alignment
      - trigger diversity: how many independent trigger types fired
      - whale/regime corroboration: independent validators vote too

    Fail-closed: when debate_gate is missing from config the gate ENABLES
    itself with defaults (a missing config must not silently disable a risk
    check). Set ``debate_gate.enabled: false`` explicitly to turn it off.
    The gate is intentionally lightweight — a full multi-LLM debate is
    delegated to the native in-process research debate; this is a fast
    pre-filter that catches obvious lone-analyst overrides.

    The gate evaluates five independent analysts:
      1. Trigger diversity: at least one trigger type fired
      2. Confidence/score consensus: confidence aligns with composite_score
      3. Regime alignment: NOT re-detected here (the market_regime gate owns
         that dimension) — this vote is configurable via
         ``analyst3_default`` and DEFAULTS TO False (no rubber-stamp vote;
         P0-D fix 2026-08-26). Set it true to restore the legacy behavior.
      4. News risk: blocks when a binary-news risk is present
      5. Whale boost: a whale signal or very high confidence

    Scoring:
      - Each analyst votes 0 or 1
      - agree_count / 5 = agreement_ratio
      - Block unless ratio >= min_agreement AND count >= min_agree_count
    """
    debate_cfg = config.get("debate_gate", {})
    if not debate_cfg.get("enabled", True):
        return {"pass": True, "via": "debate_disabled"}
    if not debate_cfg:
        # Config section entirely absent — the gate is running on implicit
        # defaults. Log once so the fail-closed default is never silent.
        _warn_debate_defaults()
    analyst3_default = bool(debate_cfg.get("analyst3_default", False))

    min_agreement = float(debate_cfg.get("min_agreement", 0.6))
    min_agree_count = int(debate_cfg.get("min_agree_count", 3))

    # --- Analyst 1: Trigger diversity ---
    # Count how many independent trigger types are firing
    trigger_types = [
        ctx.momentum_burst_fired,
        ctx.slow_burn_fired,
        ctx.whale_signal_fired,
    ]
    active_triggers = sum(1 for t in trigger_types if t)
    analyst1_agree = active_triggers >= 1  # At least one trigger type active

    # --- Analyst 2: Confidence vs Composite alignment ---
    # High confidence + high composite = strong consensus
    # Low confidence + high composite (or vice versa) = mixed signal
    # R13-B3: the three threshold pairs were hardcoded literals
    # (0.7/40, 0.5/60, 0.8/20). They're now in CANONICAL_DEFAULTS under
    # analyst_scoring.* so an operator can retune them without
    # redeploying. The canonical defaults match the old literals
    # verbatim, so existing vote behaviour is unchanged. Hot-path
    # cfg_get picks up env overrides
    # (HERMES_CFG_ANALYST_SCORING__ANALYST2_*)
    # and .agent-config.json edits on the next gate call.
    a2_high_conf = float(cfg_get("analyst_scoring.analyst2_high_conf", config={}))
    a2_high_score = float(cfg_get("analyst_scoring.analyst2_high_score", config={}))
    a2_mid_conf = float(cfg_get("analyst_scoring.analyst2_mid_conf", config={}))
    a2_mid_score = float(cfg_get("analyst_scoring.analyst2_mid_score", config={}))
    a2_vhigh_conf = float(cfg_get("analyst_scoring.analyst2_very_high_conf", config={}))
    a2_vhigh_score = float(cfg_get("analyst_scoring.analyst2_very_high_score", config={}))
    if ctx.confidence >= a2_high_conf and ctx.composite_score >= a2_high_score:
        analyst2_agree = True
    elif ctx.confidence >= a2_mid_conf and ctx.composite_score >= a2_mid_score:
        analyst2_agree = True
    elif ctx.confidence >= a2_vhigh_conf and ctx.composite_score >= a2_vhigh_score:
        analyst2_agree = True
    else:
        analyst2_agree = False

    # --- Analyst 3: Regime alignment ---
    # Regime is owned by the market_regime gate; this lightweight pre-filter
    # must not re-detect it. Historically this vote was hard-coded True, which
    # handed every trade a free vote out of five (P0-D). It now defaults to
    # False — trades must earn consensus from the real signal votes — and can
    # be flipped back via debate_gate.analyst3_default for legacy behavior.
    analyst3_agree = analyst3_default

    # --- Analyst 4: News risk check ---
    analyst4_agree = not ctx.has_binary_news_risk

    # --- Analyst 5: Whale signal boost ---
    # Whale signals are a strong independent validator
    # R13-B3: the 0.75 confidence floor was a hardcoded literal; it now
    # resolves through cfg_get(analyst_scoring.analyst5_whale_or_conf).
    # The canonical default is 0.75, so the existing vote behaviour is
    # preserved; env / config edits take effect on the next gate call.
    a5_conf_floor = float(cfg_get("analyst_scoring.analyst5_whale_or_conf", config={}))
    analyst5_agree = ctx.whale_signal_fired or ctx.confidence >= a5_conf_floor

    # --- Consensus ---
    analyst_votes = [analyst1_agree, analyst2_agree, analyst3_agree,
                     analyst4_agree, analyst5_agree]
    agree_count = sum(1 for v in analyst_votes if v)
    agreement_ratio = agree_count / len(analyst_votes)

    if agreement_ratio >= min_agreement and agree_count >= min_agree_count:
        return {
            "pass": True,
            "via": "debate_consensus",
            "agree_count": agree_count,
            "total_analysts": len(analyst_votes),
            "agreement_ratio": agreement_ratio,
        }

    return {
        "pass": False,
        "reason": (
            f"multi-agent debate blocked: {agree_count}/{len(analyst_votes)} analysts agree "
            f"(ratio {agreement_ratio:.2f} < {min_agreement}, need ≥{min_agree_count}). "
            f"Triggers={active_triggers}, conf={ctx.confidence:.2f}, "
            f"score={ctx.composite_score:.0f}, news_risk={ctx.has_binary_news_risk}"
        ),
        "agree_count": agree_count,
        "total_analysts": len(analyst_votes),
        "agreement_ratio": agreement_ratio,
    }


# ── ta_late_entry_gate (deep audit 高危项, 2026-08-30) ───────────────────
# The old late-entry veto lived only in analyze_perception() — a pre-filter
# for the PAID LLM debate that manual / MCP / API / CLI orders bypassed, and
# whose minutes-stale TA was never re-checked at order time. This gate runs
# the SAME late_entry_check() pure function as the pre-filter and the
# backtest engine (one source of truth, zero rule drift), re-fetching 4h
# (+15m) candles immediately before order placement.
#
# Activation: a ``ta_late_entry`` config block must be present (production
# configs always have it via CANONICAL_DEFAULTS; plain-dict test configs
# without the block take the zero-fetch disabled path). mode:
#   off     → gate disabled
#   shadow → verdict + metrics + JSONL recorded, order NEVER blocked
#             (gray-release; run 3–7 days, reconcile would_block vs the
#              subsequent move before flipping)
#   enforce → late entries are blocked (hard gate)
# Fail-OPEN: any fetch / data / compute failure (or too little 4h data)
# passes the order — a risk gate must not stall the exchange path — and is
# surfaced via the data_missing verdict label.
def late_entry_shadow_path(le_cfg: dict[str, Any]) -> str:
    """Resolve the ta_late_entry shadow JSONL path (config → env → default).

    Shared by the gate and the pre-filter screen so BOTH layers write the same
    file and are indistinguishable to downstream reconciliation apart from the
    record's ``layer`` field.
    """
    return str(le_cfg.get("shadow_log_path") or "").strip() or os.environ.get(
        "HERMES_TA_LATE_ENTRY_SHADOW_FILE",
        os.path.expanduser("~/.hermes-trading/ta_late_entry_shadow.jsonl"),
    )


def _record_late_entry_shadow(rec: dict[str, Any], path: str) -> None:
    """Best-effort append a late-entry shadow verdict to the audit JSONL."""
    import json
    import os
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError as e:  # noqa: BLE001
        logger.warning(f"[risk][gates] late-entry shadow write failed: {e}")


def ta_late_entry_gate(
    ctx: GateContext,
    config: dict[str, Any],
) -> GateResult:
    t0 = time.perf_counter()

    def _done(outcome: str, result: GateResult) -> GateResult:
        dt = time.perf_counter() - t0
        try:
            from hermes_trader import metrics
            metrics.RISK_GATE_DURATION.labels(gate="ta_late_entry", outcome=outcome).observe(dt)
        except Exception:  # noqa: BLE001
            pass
        if dt > 0.025:
            logger.warning(
                "[risk][gates] ta_late_entry slow: %.1fms outcome=%s coin=%s",
                dt * 1000.0, outcome, ctx.coin,
            )
        return result

    le_cfg = config.get("ta_late_entry")
    # No block at all → gate inactive (keeps plain-dict test configs off the
    # network entirely).
    if not isinstance(le_cfg, dict):
        return {"pass": True, "via": "ta_late_entry_disabled"}
    mode = str(le_cfg.get("mode", "shadow") or "shadow").lower()
    side = ctx.trade_side if ctx.trade_side in ("long", "short") else "long"
    if mode == "off":
        return _done("disabled", {"pass": True, "via": "ta_late_entry_off"})

    try:
        from concurrent.futures import ThreadPoolExecutor
        from hermes_trader.agents.ta_filter import (
            late_entry_check, forming_readings_4h,
        )
        from hermes_trader.agents.perception import _drop_forming_bar
        from hermes_trader.client.hl_client import fetch_hl_candles
        n = int(le_cfg.get("fetch_bars", 100) or 100)
        mtf_enabled = bool(le_cfg.get("mtf_enabled", False))
        # Phase 0 (audit R3): 15m is fetched ONLY when MTF is enabled — the
        # screen never warms that cache key, so the gate's 15m call was a cold
        # weight-20 HTTP on every order. With mtf disabled the gate now does a
        # single (usually cache-hot) 4h fetch.
        if mtf_enabled:
            with ThreadPoolExecutor(max_workers=2) as pool:
                f4 = pool.submit(fetch_hl_candles, ctx.coin, "4h", n)
                f15 = pool.submit(fetch_hl_candles, ctx.coin, "15m", n)
                candles_4h = f4.result()
                candles_15m = f15.result()
        else:
            candles_4h = fetch_hl_candles(ctx.coin, "4h", n)
            candles_15m = None
        # Phase 0 (audit R1): score the last CLOSED bar only. fetch_hl_candles'
        # snapshot ends on the still-forming bar; the backtest engine only ever
        # sees closed bars, so scoring the forming bar made live diverge from
        # backtest. The forming bar's readings are logged for shadow analysis
        # but never participate in the verdict.
        forming = forming_readings_4h(candles_4h)
        candles_4h, _ = _drop_forming_bar(candles_4h, "4h")
        if candles_15m is not None:
            candles_15m, _ = _drop_forming_bar(candles_15m, "15m")
        verdict = late_entry_check(candles_4h, candles_15m, side, le_cfg)
    except Exception as e:  # noqa: BLE001 — fail OPEN, never stall orders
        logger.warning(
            "[risk][gates] ta_late_entry fetch/compute failed for %s: %s",
            ctx.coin, e,
        )
        try:
            from hermes_trader import metrics
            metrics.TA_LATE_ENTRY_VERDICTS.labels(
                mode=mode, side=side, verdict="data_missing").inc()
        except Exception:  # noqa: BLE001
            pass
        return _done("data_missing", {
            "pass": True,
            "via": "ta_late_entry_data_missing",
            "reason": f"late-entry gate unavailable ({type(e).__name__})",
        })

    if not verdict.get("data_ok"):
        try:
            from hermes_trader import metrics
            metrics.TA_LATE_ENTRY_VERDICTS.labels(
                mode=mode, side=side, verdict="data_missing").inc()
        except Exception:  # noqa: BLE001
            pass
        return _done("data_missing", {
            "pass": True,
            "via": "ta_late_entry_data_missing",
            "reason": verdict.get("reason") or "insufficient 4h data",
        })

    blocked = bool(verdict.get("block"))
    rec = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "coin": ctx.coin,
        "side": side,
        "mode": mode,
        # P0-4: distinguish the two decision layers writing this JSONL.
        # "gate" = order-time hard gate; "prefilter" = pre-AI ta_filter veto.
        "layer": "gate",
        "blocked": blocked,
        "reason": verdict.get("reason", ""),
        "rsi4h": verdict.get("rsi4h"),
        "adx4h": verdict.get("adx4h"),
        "extension": verdict.get("extension"),
        "rsi15m": verdict.get("rsi15m"),
        "relaxed_by_trend": verdict.get("relaxed_by_trend"),
        "mtf_passed": verdict.get("mtf_passed"),
        "trend_direction": verdict.get("trend_direction"),
        # P0-2 (audit R1): readings of the still-forming 4h bar that was
        # DROPPED before scoring. Observability only — the verdict uses the
        # last closed bar, matching the backtest engine.
        "forming_rsi4h": forming.get("forming_rsi4h"),
        "forming_extension": forming.get("forming_extension"),
        "forming_bar_dropped": forming.get("forming_bar_dropped"),
        "entry_px": ctx.entry_px or None,
        "trade_notional_usd": ctx.trade_notional_usd or None,
        "confidence": ctx.confidence,
        "outcome": None,  # filled by post-run reconciliation
        "exit_px": None,
        "pnl_usd": None,
    }
    path = late_entry_shadow_path(le_cfg)
    _record_late_entry_shadow(rec, path)

    if not blocked:
        try:
            from hermes_trader import metrics
            metrics.TA_LATE_ENTRY_VERDICTS.labels(
                mode=mode, side=side, verdict="pass").inc()
        except Exception:  # noqa: BLE001
            pass
        return _done("pass", {
            "pass": True,
            "via": "ta_late_entry_pass",
            "data_ok": True,
            "rsi4h": verdict.get("rsi4h"),
            "adx4h": verdict.get("adx4h"),
            "extension": verdict.get("extension"),
            "rsi15m": verdict.get("rsi15m"),
            "relaxed_by_trend": verdict.get("relaxed_by_trend"),
            "mtf_passed": verdict.get("mtf_passed"),
        })

    reason = f"late-entry gate: {verdict.get('reason', '')}".rstrip()
    if mode == "shadow":
        # Gray release: record the would-block verdict but do NOT stop the
        # order. Warning level so it survives into container logs.
        logger.warning(
            "[risk][gates] ta_late_entry SHADOW would-block coin=%s side=%s: %s",
            ctx.coin, side, reason,
        )
        try:
            from hermes_trader import metrics
            metrics.TA_LATE_ENTRY_VERDICTS.labels(
                mode=mode, side=side, verdict="would_block").inc()
        except Exception:  # noqa: BLE001
            pass
        return _done("shadow_block", {
            "pass": True,  # shadow never blocks
            "via": "ta_late_entry_shadow",
            "would_block": True,
            "data_ok": True,
            "reason": reason,
            "rsi4h": verdict.get("rsi4h"),
            "adx4h": verdict.get("adx4h"),
            "extension": verdict.get("extension"),
            "rsi15m": verdict.get("rsi15m"),
            "mtf_passed": verdict.get("mtf_passed"),
        })

    # enforce: hard block.
    logger.info(
        "[risk][gates] ta_late_entry BLOCK coin=%s side=%s: %s",
        ctx.coin, side, reason,
    )
    try:
        from hermes_trader import metrics
        metrics.TA_LATE_ENTRY_VERDICTS.labels(
            mode=mode, side=side, verdict="block").inc()
    except Exception:  # noqa: BLE001
        pass
    return _done("enforce_block", {
        "pass": False,
        "via": "ta_late_entry_block",
        "reason": reason,
    })


# Load cross-component shared config (~/.hermes-trading/config.yaml).
# Canonical implementation lives in hermes_trader.shared_config.
_load_shared_config = load_shared_config


def _cfg(config: dict[str, Any], key: str, default: Any) -> Any:
    """Read a config value tolerating snake_case or camelCase keys."""
    if key in config:
        return config[key]
    parts = key.split("_")
    camel = parts[0] + "".join(p.capitalize() for p in parts[1:])
    return config[camel] if camel in config else default


def eval_all_gates(
    ctx: GateContext,
    config: dict[str, Any],
    last_trade_time: Optional[int] = None,
    *,
    analysis: Optional[dict[str, Any]] = None,
    trace_id: str = "",
) -> dict[str, Any]:
    """Evaluate all risk gates and collect results."""
    results = {}
    # ── DEBUG: eval_all_gates entry — full trade proposal context ────────
    logger.debug(
        "[risk][gates] eval_begin coin=%s side=%s confidence=%.3f "
        "notional_usd=%.2f equity=%.2f total_open=%.2f daily_pnl=%.2f "
        "peak_pnl=%.2f volume_24h=%.0f positions=%d news_risk=%s "
        "score=%.2f burst=%s slow=%s whale=%s trace_id=%s",
        ctx.coin, ctx.trade_side, ctx.confidence,
        ctx.trade_notional_usd or 0.0, ctx.equity or 0.0,
        ctx.total_open_notional or 0.0, ctx.daily_pnl or 0.0,
        ctx.peak_daily_pnl or 0.0, ctx.market_volume_24h_usd or 0.0,
        len(ctx.current_positions or []), ctx.has_binary_news_risk,
        ctx.composite_score or 0.0, ctx.momentum_burst_fired,
        ctx.slow_burn_fired, ctx.whale_signal_fired, trace_id,
    )
    # Regime-aware confidence floor: a WITH-TREND (aligned) trade — long in an up
    # regime, SHORT in a DOWN regime — gets a lower bar (`aligned_min_conf`) than
    # the default `min_ai_confidence`. The 0.78 default was calibrated on the
    # LONG-side 0.70-0.80 leak; applying it to aligned shorts made us sit out
    # selloffs (e.g. SOL SHORT 0.72 / -6.3% / $399M blocked). Demand full
    # conviction only to fight the trend (neutral/counter-trend keep the default).
    min_conf = float(cfg_get("min_ai_confidence", config=config))
    aligned_min_conf = config.get("aligned_min_conf")
    if aligned_min_conf is not None:
        try:
            from hermes_trader.agents.market_regime import detect_regime_with_score
            _rg, _score = detect_regime_with_score(ctx.coin)  # cached; market_regime_gate reuses
            _aligned = (_rg == "up" and ctx.trade_side == "long") or \
                       (_rg == "down" and ctx.trade_side == "short")
            # A weak EMA cross (score < min_trend_score) is demoted: it doesn't
            # get the lower aligned-min-confidence discount either.
            _min_ts = float(cfg_get("min_trend_score", config=config) or 0.0)
            if _aligned and not (_min_ts > 0 and _score < _min_ts):
                min_conf = min(min_conf, float(aligned_min_conf))
        except Exception:
            pass
    logger.debug(
        "[risk][gates] confidence_floor coin=%s side=%s "
        "min_conf=%.3f (aligned_min_conf=%s)",
        ctx.coin, ctx.trade_side, min_conf, aligned_min_conf,
    )
    results["confidence"] = confidence_gate(ctx, min_conf)
    results["max_concurrent"] = max_concurrent_positions_gate(
        ctx, int(cfg_get("max_concurrent", config=config)))
    results["notional_cap"] = per_trade_notional_cap_gate(
        ctx, float(cfg_get("max_trade_notional_usd", config=config)))
    results["daily_loss"] = daily_loss_kill_switch(
        ctx, float(cfg_get("max_daily_loss_usd", config=config)))
    results["daily_giveback"] = daily_giveback_gate(
        ctx,
        float(cfg_get("daily_giveback_halt_pct", config=config)),
        float(cfg_get("daily_giveback_min_peak_usd", config=config)),
    )
    results["liquidity"] = market_liquidity_floor(
        ctx,
        float(cfg_get("min_market_volume_usd", config=config)),
        float(cfg_get("min_hip3_volume_usd", config=config)),
    )
    results["short_liquidity"] = short_liquidity_floor(
        ctx, float(cfg_get("min_short_volume_usd", config=config)) or 0)
    results["coin_filter"] = coin_allowlist_gate(
        ctx,
        cfg_get("coin_allowlist", config=config) or [],
        cfg_get("coin_blocklist", config=config) or [],
    )
    results["cooldown"] = cooldown_gate(
        ctx, last_trade_time, float(cfg_get("cooldown_min", config=config)))
    # Tiered circuit breakers (sizing/risk-overhaul 2026-08-26): armed at the
    # close chokepoint, enforced here so a halted coin/book cannot re-enter.
    results["coin_circuit"] = coin_circuit_breaker_gate(ctx)
    results["global_halt"] = global_halt_gate(ctx)
    # B-F2/B-F6/B-F7 (deep audit 2026-08-28): the book-keeping existed but no
    # gate read it. Threshold <= 0 disables each gate.
    results["consecutive_loss"] = consecutive_loss_gate(
        ctx, int(cfg_get("circuit_breaker.consecutive_loss_limit", config=config) or 0))
    results["coin_daily_loss"] = per_coin_daily_loss_gate(
        ctx, float(cfg_get("circuit_breaker.coin_daily_loss_pct", config=config) or 0.0))
    results["drawdown"] = drawdown_gate(
        ctx, float(cfg_get("circuit_breaker.max_drawdown_pct", config=config) or 0.0))
    # H4 (deep audit 2026-08-29): refuse entries whose estimated liquidation
    # price falls inside the planned stop bracket (10U thin-margin blast
    # protection). maint_margin_rate_pct <= 0 disables; the extra buffer is
    # the canonical sl_buffer_bps (basis points → percent).
    results["liquidation_buffer"] = liquidation_buffer_gate(
        ctx,
        float(cfg_get("liquidation_maint_margin_pct", config=config) or 0.0),
        float(cfg_get("sl_buffer_bps", config=config) or 0.0) / 100.0,
    )
    results["opposite_guard"] = opposite_direction_guard(ctx)
    results["correlation"] = correlation_cap(
        ctx, int(cfg_get("max_crypto_long_correlated", config=config)))
    results["equity_risk"] = equity_risk_cap(
        ctx, float(cfg_get("max_total_notional_pct", config=config)))
    results["market_regime"] = market_regime_gate(
        ctx, float(cfg_get("counter_regime_min_conf", config=config)),
        bool(cfg_get("block_counter_trend_bypass", config=config)),
        float(cfg_get("crowded_with_min_conf", config=config) or 0.0),
        float(cfg_get("min_trend_score", config=config) or 0.0),
    )
    results["news"] = news_blackout_gate(ctx)
    results["debate"] = debate_gate(ctx, config)
    # ta_late_entry (deep audit 高危项, 2026-08-30): hard late-entry veto
    # re-checked at order time with FRESH candles (the pre-filter TA is
    # minutes stale). Runs the same late_entry_check() pure function as the
    # ta_filter pre-filter and the backtest; mode off/shadow/enforce.
    results["ta_late_entry"] = ta_late_entry_gate(ctx, config)

    block_reasons = []
    blocked = False
    # P3-1: count gate blocks (keys are the fixed gate names below; anything
    # unexpected is normalised to "other" to keep the label bounded).
    _GATE_KEYS = {
        "confidence", "max_concurrent", "notional_cap", "daily_loss",
        "daily_giveback", "liquidity", "short_liquidity", "coin_filter",
        "cooldown", "coin_circuit", "global_halt", "consecutive_loss",
        "coin_daily_loss", "drawdown", "liquidation_buffer",
        "opposite_guard",
        "correlation", "equity_risk", "market_regime", "news", "debate",
        "ta_late_entry",
    }
    for key, result in results.items():
        if not result.get("pass"):
            blocked = True
            block_reasons.append(result.get("reason", key))
            try:
                from hermes_trader import metrics
                metrics.RISK_GATE_BLOCKS.labels(
                    gate=key if key in _GATE_KEYS else "other").inc()
            except Exception:  # noqa: BLE001
                pass
    # P3-1: market-regime verdict code (trigger:* variants collapse to
    # "trigger"; anything outside the known via codes falls to "other").
    try:
        from hermes_trader import metrics
        _via = str(results.get("market_regime", {}).get("via", "") or "")
        if _via.startswith("trigger:"):
            _via = "trigger"
        _REGIME_VIAS = {
            "aligned", "neutral", "chop_conviction", "chop_blocked",
            "confidence", "composite", "crowded_squeeze", "blocked",
            "blocked_bypass", "trigger",
        }
        metrics.RISK_GATE_REGIME_VERDICTS.labels(
            via=_via if _via in _REGIME_VIAS else "other").inc()
    except Exception:  # noqa: BLE001
        pass

    # ── DEBUG: per-gate pass/fail summary + final verdict ────────────────
    gate_summary = ",".join(
        f"{k}={'1' if v.get('pass') else '0'}" for k, v in results.items()
    )
    logger.debug(
        "[risk][gates] eval_end coin=%s side=%s blocked=%s gates=[%s] block_reasons=%r",
        ctx.coin, ctx.trade_side, blocked,
        gate_summary, block_reasons,
    )

    return {"results": results, "blocked": blocked, "block_reasons": block_reasons}
