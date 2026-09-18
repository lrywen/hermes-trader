"""Core data types shared across the agent.

P1-3 central type layer: boundary DTOs that cross module seams (indicators,
client, risk gates) live here so the layers do not define their own copies.
Currently:

  * Candle       — OHLCV candle (indicators / client boundary)
  * TriggerHit   — one trigger-check result (indicators.triggers → perception)
  * GateContext  — context handed to every risk gate (risk_gates boundary)

This is deliberately NOT a whole-codebase typing campaign; consumers keep
importing these from their current modules too (risk_gates re-exports
GateContext for compatibility).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypedDict

from pydantic import BaseModel


class Candle(BaseModel):
    """OHLCV candle."""
    t: int  # timestamp (ms)
    o: float  # open
    h: float  # high
    l: float  # low
    c: float  # close
    v: float  # volume

    def __getitem__(self, key: str) -> float:
        """Allow dict-style access: candle['c'], candle['t'], etc."""
        return getattr(self, key)


class TriggerHit(TypedDict):
    """Result of a single trigger check from `indicators.triggers`, consumed
    by the perception scan + composite scoring."""
    name: str
    score: float
    reason: str
    fired: bool


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
    # S3 (RCA FARTCOIN 2026-08-26, observation 2): provenance of the research
    # verdict — True only when the native multi-LLM debate (bull/bear/synth)
    # produced it; False for a single-LLM fallback verdict and for manual
    # orders (which have no research verdict). debate_gate reads this so a
    # fallback verdict is never mislabelled "debate_consensus" in the gate
    # result / execute event. Observability only — it never changes pass/fail.
    debate_used: bool = False
    # per_coin_regime 坑1 (shadow-audited 2026-09-15): the coin's OWN 4h
    # close-vs-EMA21 extension, side-adjusted so >0 means stretched IN the
    # trade direction (long: close above ema; short: close below). The
    # market_regime gate demotes an aligned free-pass whose own gap exceeds
    # own_gap_demote_pct to the counter-trend bar. 0.0 = no data / no demote
    # (fail open; manual path and stale analysis land here).
    own_gap_pct: float = 0.0

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
        # S3: coerce research-verdict provenance to a strict bool.
        self.debate_used = bool(self.debate_used)
        # 坑1: 0.0 = no reading / not applicable → own-gap demote stays inert.
        self.own_gap_pct = _num(self.own_gap_pct)
        # The headline + matched term that tripped the binary-news gate, for
        # log visibility ("which article blocked this?").
        self.binary_news_match = str(self.binary_news_match or "")
        self.coin = str(self.coin or "")
        self.trade_side = str(self.trade_side or "long")
        if not isinstance(self.current_positions, list):
            self.current_positions = []

