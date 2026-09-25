"""Point-in-time bar adapter around the PRODUCTION DSLTracker.

This is the linchpin of P4: instead of re-implementing DSL exit semantics in
the backtest (the three research scripts each did, and drifted — see
tests/test_p4_dsl_parity.py D1-D6), the kernel drives the exact exit engine
live trading uses, with bars fed through the conservative intrabar rule
already proven in the parity tests (``run_production``):

1. Both clocks are frozen to the SAME virtual instant for the bar's two checks
   (wall clock -> timeouts/hold; monotonic -> confirm gates), then restored.
2. The ADVERSE extreme is checked first (its verdict can exit); then the
   FAVORABLE extreme is checked only to advance the peak, whose verdict is
   discarded. This keeps a stop decided on a bar from using that same bar's
   favorable high to raise the floor (the D1 intrabar look-ahead).
3. Stop/floor exits fill at the floor reference gap-filled against the bar
   open: ``min(floor, open)`` long / ``max(floor, open)`` short. Timeouts have
   no floor and fill at the bar close.

Entry semantics: the position opens at the OPEN of the FIRST bar handed to the
adapter (``bar_index`` 0) — that bar IS the entry bar, and its high/low are the
first prices the live stop would see (a gap-through on the entry bar must be
caught, scenario D3/s1). Candle ``t`` is the bar-OPEN ms; that bar's close is
one bar interval after entry, so the virtual clock stamps
``entry + (bar_index+1)*bar_ms``. With 5-minute bars a 15-minute hard timeout
therefore fires at ``bar_index`` 2 (that bar's close is exactly 15 minutes
after the entry open). The bar cadence is configurable via ``bar_ms`` so
non-5m backtests still measure timeouts in real minutes (the production DSL
cadence is fixed wall-clock time, not a bar count).

Clock injection note: production ``check()`` reads module-level ``time``; until
P1 adds an explicit clock seam we freeze ``time.time``/``time.monotonic``
process-wide ONLY inside the two synchronous ``check()`` calls and restore them
in ``finally``. Backtests run in a dedicated single-threaded process.
"""
from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass, replace
from typing import Optional

from hermes_trader.agents import dsl_exit as dx
from hermes_trader.agents.dsl_exit import DSLTracker, ExitPolicy

from .types import ExitEvent, Side, normalize_reason

#: Default bar cadence: 5-minute bars, the production DSL cadence.
BAR_MS_5M = 300_000


@contextlib.contextmanager
def _frozen_clock(wall: float, mono: float):
    """Temporarily pin time.time/monotonic; restored even if check() raises."""
    real_time, real_mono = time.time, time.monotonic
    time.time = lambda: wall
    time.monotonic = lambda: mono
    try:
        yield
    finally:
        time.time = real_time
        time.monotonic = real_mono


@contextlib.contextmanager
def _no_persistence():
    """Suppress dsl_exit's registry writes and shadow-log side effects.

    Direct-constructed trackers are never in ``_active_positions`` (a save
    would only rewrite an empty registry), but forcing no-ops still removes
    file-lock contention and the stop-tuning shadow's config read / jsonl
    append during a fast backtest. Best-effort: restore everything on exit.
    """
    real_save = dx._request_save
    real_shadow = dx._record_stop_tuning_shadow
    dx._request_save = lambda force=False: None
    dx._record_stop_tuning_shadow = lambda *a, **k: None
    try:
        yield
    finally:
        dx._request_save = real_save
        dx._record_stop_tuning_shadow = real_shadow


@dataclass
class DslBarExit:
    """Exit adapter: feed bars of a fixed cadence; get the first :class:`ExitEvent`.

    Parameters mirror DSLTracker. Confirmation gates are zeroed here (a bar is
    already an aggregated, confirmed candle — there is no sub-bar tick stream
    to wait on). ``bar_ms`` defaults to the production 5-minute cadence; pass
    the real interval for 15m/1h/4h/1d backtests so wall-clock timeouts fire on
    schedule.
    """

    side: Side
    entry_px: float
    entry_time_ms: int
    policy: ExitPolicy
    leverage: int = 1
    coin: str = "BACKTEST"
    entry_atr_pct: float = 0.0
    entry_regime: str = ""
    bar_ms: int = BAR_MS_5M
    # T-26：破位确认口径。
    #   "bar"  —— 旧口径：一根 bar 只驱动一次 check，consecutive_breaches 被
    #             解释为"连续 N 根 K 线"（默认，逐位不变，供粗粒度回测）。
    #   "tick" —— 实盘口径：bar 内以 tick_confirm_s 为步长子采样，多次驱动
    #             check，consecutive_breaches 表示"连续 N 次秒级轮询"。
    confirm_mode: str = "bar"
    #: 实盘出场检查点最小间隔（exit_checkpoint_min_interval_s 默认 5s）。
    tick_confirm_s: float = 5.0

    def __post_init__(self) -> None:
        # bar 口径：复制 policy 并把亚秒级确认闸门清零（一根 bar 已是聚合的、
        # 已确认的 K 线，没有子 bar tick 流可等）。复制（而非改调用方 policy）
        # 同时断开 tiers 列表，回测永不改共享的 live policy。
        # tick 口径（T-26）：保留 policy 原样（breach_confirm_sec / hard_stop
        # _confirm_sec 与 consecutive_breaches 都按秒级 tick 真实回放），只断开
        # tiers 列表。
        if self.confirm_mode not in ("bar", "tick"):
            raise ValueError(
                f"confirm_mode 必须是 'bar' 或 'tick'，得到 {self.confirm_mode!r}")
        if self.confirm_mode == "bar":
            policy = replace(self.policy,
                             phase2_tiers=list(self.policy.phase2_tiers),
                             breach_confirm_sec=0.0, hard_stop_confirm_sec=0.0)
        else:
            policy = replace(self.policy,
                             phase2_tiers=list(self.policy.phase2_tiers))
        self._tr = DSLTracker(
            self.coin, self.side, self.entry_px, self.entry_time_ms / 1000.0,
            policy=policy, leverage=self.leverage,
            entry_atr_pct=self.entry_atr_pct, entry_regime=self.entry_regime,
        )

    def on_bar(self, bar, bar_index: int) -> Optional[ExitEvent]:
        """Process one bar (0 = the entry bar itself); exit or None.

        The entry bar is fed first: an adverse gap/open there can stop the
        position out within its first bar. Decisions are stamped at the
        bar CLOSE, ``entry + (bar_index+1)*bar_ms`` after the entry open, which
        keeps hard/stale timeouts measured from the entry open (with 5m bars a
        15-minute timeout fires at ``bar_index`` 2).
        """
        if self.confirm_mode == "tick":
            return self._on_bar_tick(bar, bar_index)
        return self._on_bar_bar(bar, bar_index)

    def _on_bar_bar(self, bar, bar_index: int) -> Optional[ExitEvent]:
        """旧 bar 口径：bar 内一次 check（adverse 先、favorable 仅推进 peak）。"""
        is_long = self.side == "long"
        # Virtual instant: this bar's close, relative to entry open. The bar
        # cadence is real wall-clock time (production timeouts are in minutes),
        # so non-5m feeds must pass their own ``bar_ms``.
        bar_secs = self.bar_ms / 1000.0
        wall = self.entry_time_ms / 1000.0 + (bar_index + 1) * bar_secs
        mono = float(bar_index + 1) * bar_secs
        with _no_persistence(), _frozen_clock(wall, mono):
            adverse = bar.l if is_long else bar.h
            verdict = self._tr.check(adverse, index_px=None)
            if verdict.exit:
                if verdict.floor_price is not None:
                    ref = (min(verdict.floor_price, bar.o) if is_long
                           else max(verdict.floor_price, bar.o))
                else:
                    ref = bar.c
                return ExitEvent(bar_index, normalize_reason(verdict.reason), ref)
            favorable = bar.h if is_long else bar.l
            self._tr.check(favorable, index_px=None)
        return None

    def _on_bar_tick(self, bar, bar_index: int) -> Optional[ExitEvent]:
        """T-26 tick 口径：bar 内按 tick_confirm_s 子采样，秒级回放破位确认。

        一根 bar 只有 OHLC，子采样的价格路径取【保守】构造（不引入 bar 内不
        可知的信息）：每个 tick 先喂 adverse 极端（其破位可触发出场），再喂
        favorable 极端仅用于推进 peak（裁决丢弃）—— 与 bar 口径同一保守
        intrabar 规则，区别只是【时间粒度】：同一根 bar 内每隔 tick_confirm_s
        推进一次虚拟时钟并驱动一次 check，于是连续 N 次破位 = N 个秒级轮询，
        而非 N 根 K 线。时间闸门（breach_confirm_sec / hard_stop_confirm_sec）
        在 tick 口径下也按真实秒数生效。

        出场成交：floor 类仍按 floor 相对 bar 开盘 gap-fill（min/max），timeout
        类按 bar 收盘；成交模型与 bar 口径一致，仅【是否以及何时认定破位】的
        判定变为 tick 粒度。
        """
        is_long = self.side == "long"
        bar_secs = self.bar_ms / 1000.0
        step = max(1e-6, float(self.tick_confirm_s))
        n_ticks = max(1, int(bar_secs // step))
        adverse = bar.l if is_long else bar.h
        favorable = bar.h if is_long else bar.l

        # ── D1 隔离的关键 ──────────────────────────────────────────────
        # check() 在内部据【当前 peak】重算 floor，所以只钉 _last_floor 不够：
        # 必须把 peak 也钉在【进入本 bar 时】的 peak，adverse 判定在整根 bar 内
        # 才不会对照到由本 bar favorable high 算出的新 floor（bar 内前瞻）。
        # favorable 推进出的新 peak / 新 floor 在隔离状态上计算，bar 结束后才
        # 一并生效（供下一根 bar）。
        entry_floor = self._tr._last_floor
        entry_peak = self._tr.peak_px
        new_floor = entry_floor
        new_peak = entry_peak

        def _better(a, b):
            if b is None:
                return a
            if a is None:
                return b
            return max(a, b) if is_long else min(a, b)

        for ti in range(1, n_ticks + 1):
            frac = ti / n_ticks
            wall = (self.entry_time_ms / 1000.0
                    + bar_index * bar_secs + frac * bar_secs)
            mono = (bar_index + frac) * bar_secs
            with _no_persistence(), _frozen_clock(wall, mono):
                # 1) adverse 判定：peak/floor 都钉在入 bar 基线，隔离本 bar
                # favorable 的任何影响。
                self._tr.peak_px = entry_peak
                self._tr._last_floor = entry_floor
                verdict = self._tr.check(adverse, index_px=None)
                # 首根 bar 基线为 None：check 已据 hard-stop 算出，取为基线。
                if entry_floor is None:
                    entry_floor = self._tr._last_floor
                if verdict.exit:
                    if verdict.floor_price is not None:
                        ref = (min(verdict.floor_price, bar.o) if is_long
                               else max(verdict.floor_price, bar.o))
                    else:
                        ref = bar.c
                    return ExitEvent(bar_index, normalize_reason(verdict.reason), ref)

                # 2) favorable 在隔离状态上推进 peak / 计算新 floor；破位计数
                # 以 adverse 判定后的真实状态为准（不被 favorable 清零）。
                cb = self._tr.consecutive_breaches
                bts = self._tr._first_breach_ts
                self._tr.peak_px = new_peak
                self._tr._last_floor = new_floor
                self._tr.check(favorable, index_px=None)
                cand_floor = self._tr._last_floor
                cand_peak = self._tr.peak_px
                # 还原破位计数（favorable 不应改它）。
                self._tr.consecutive_breaches = cb
                self._tr._first_breach_ts = bts
                new_peak = _better(new_peak, cand_peak)
                new_floor = _better(new_floor, cand_floor)

        # bar 结束：favorable 算出的新 peak / floor 才在此刻生效（供下一根）。
        self._tr.peak_px = new_peak
        self._tr._last_floor = new_floor
        return None
