"""Structural point-in-time (PIT) guards for the backtest kernel.

The kernel's no-look-ahead guarantee rests on a handful of invariants that are
easy to break silently when wiring new signal sources or replay adapters:

  * a signal decided at bar ``i``'s close can only fill at ``i+1``'s open, so
    signals on the last bar can never trade and negative indices are invalid;
  * two signals on the same decision bar are ambiguous — the driver silently
    keeps one — so they are rejected rather than ignored;
  * a trade's exit may never precede its entry (an exit INSIDE the entry bar is
    legal — ``exit_bar == entry_bar`` — since the entry bar is fed at rel=0);
  * the single-position kernel never holds overlapping trades: the next entry
    fills strictly after the prior exit bar;
  * the timestamps recorded on a trade must match the bars actually entered and
    exited (checked when ``bars`` are supplied).

These are structural assertions over kernel inputs/outputs — they do not
verify TA math, only that nothing could have peeked at future bars.
"""
from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path

from hermes_trader.models.types import Candle

from .types import Side, Signal, Trade

_SIDES: tuple[Side, ...] = ("long", "short")

# B-2：实盘峰值并发上限。生产 executor 同时在仓的头寸最多 2 个；旧回测路径
# 默认不约束（实测峰值 7、均值 1.29），把 filt 从 −5.20% 高估成 +13.86%
# （P3-4：差 19 个百分点）。所有回测路径必须按此上限调度，guard 硬拒绝 >2。
#
# A-2（2026-09-20 只读取证定性）：maxc=2 是**当前 10x/$30 小账户风险包的
# 有意设计，非临时值**。配置演值 20（大账户时代）→10→4→2，2 与 10x/$30 包
# 同步确立后历经月余、多次提交与全部容器备份从未变动；canonical 默认即 2
# （config_store），shadow_book 有 SHADOW/LIVE cap parity 硬约束。maxc 曲线
# **非单调**（P3-4：maxc=6 +21.21%、∞ 回落到 +19.43%、=2 为 −5.20%），无法
# 靠回测扫描安全重选——改它会改变资金暴露，只能在 mode=SHADOW 下另行评估。
# 故此处固化为受保护常量，而非待优化参数。
MAX_CONCURRENT_POSITIONS = 2


def assert_max_concurrent_allowed(n: int) -> None:
    """Reject any backtest path that would allow more than the live cap.

    The live executor holds at most :data:`MAX_CONCURRENT_POSITIONS` open
    positions. A backtest configured with a higher ceiling is structurally
    incomparable to production and overstates capacity (P3-4: the filter edge
    flips sign once the cap is applied), so it is a hard error rather than a
    warning.
    """
    if not isinstance(n, int) or isinstance(n, bool) or n <= 0:
        raise ValueError(f"max_concurrent must be a positive int, got {n!r}")
    if n > MAX_CONCURRENT_POSITIONS:
        raise ValueError(
            f"max_concurrent={n} exceeds the live cap "
            f"{MAX_CONCURRENT_POSITIONS} (P3-4: uncapped backtests overstate "
            f"capacity — filt +13.86% flips to -5.20% at maxc=2)"
        )


# B-guard-1：生产杠杆。live executor 以 leverage=10 运行（/data 权威配置，
# 2026-09-21 实测）。回测若用不同杠杆，其初始保证金、维持保证金与爆仓/强平
# 口径都与生产不可比——杠杆直接决定 ROE 缩放与 max_loss_roe 的触发距离。
# 与 MAX_CONCURRENT_POSITIONS 同性质：固化为受保护常量，而非待优化参数；改它
# 会改变资金暴露，只能在 mode=SHADOW 下另行评估。
LIVE_LEVERAGE = 10


def assert_leverage_allowed(leverage: int) -> None:
    """Reject a backtest run whose leverage differs from the live value.

    Margin/liquidation math and the ROE-scaled stop distance all depend on
    leverage; a run at a different multiple is structurally incomparable to
    the live account, so divergence is a hard error (like the concurrent
    position cap), not a silent discrepancy.
    """
    if not isinstance(leverage, int) or isinstance(leverage, bool) or leverage <= 0:
        raise ValueError(f"leverage must be a positive int, got {leverage!r}")
    if leverage != LIVE_LEVERAGE:
        raise ValueError(
            f"backtest leverage={leverage} != live leverage={LIVE_LEVERAGE} — "
            "margin/liquidation and ROE stop distance would be incomparable; "
            "evaluate a leverage change under mode=SHADOW rather than silently "
            "replaying at a different multiple"
        )


# B-guard-2：逐币成本表齐全度。生产回测的单边滑点取自
# hermes_trader/data/per_coin_half_spread_bps.json 的 81 币半价差；币不在表中
# 时会静默回退到 flat/0.31bps（bt_ra_exch._slip_for），使该币成本被低估或
# 错配而不报错。任何回测路径在调度前必须确认其币池被成本表完整覆盖。
COST_TABLE_FALLBACK_BPS = 0.31


def check_cost_table_coverage(
    coins: Sequence[str], covered_coins: Sequence[str]
) -> list[str]:
    """Return the backtest coins missing from the per-coin cost table.

    Empty list means every coin has an explicit half-spread entry. A coin in
    the replay universe but absent from ``covered_coins`` (the keys of
    per_coin_half_spread_bps.json) would otherwise silently fall back to
    :data:`COST_TABLE_FALLBACK_BPS`, understating its cost.
    """
    covered = {str(c).upper() for c in covered_coins}
    return [str(c) for c in coins if str(c).upper() not in covered]


def assert_cost_table_complete(
    coins: Sequence[str], covered_coins: Sequence[str]
) -> None:
    """Raise ``ValueError`` listing any replay coin lacking a cost entry."""
    missing = check_cost_table_coverage(coins, covered_coins)
    if missing:
        preview = ", ".join(missing[:10]) + (f" …(+{len(missing)-10})"
                                             if len(missing) > 10 else "")
        raise ValueError(
            f"per-coin cost table missing {len(missing)} replay coin(s): {preview} "
            f"— they would silently fall back to {COST_TABLE_FALLBACK_BPS}bps; "
            "extend per_coin_half_spread_bps.json or shrink the backtest universe"
        )

# 生产状态卷根（容器内命名卷 hermes-deploy_hermes_data→/data）。研究回测的
# 落盘产物只能写仓库 logs/（gitignored）等研究路径，禁止写入生产卷，避免
# 研究 JSONL 与生产 events/session/state 混杂或写满卷（评估报告 R2）。
PRODUCTION_DATA_ROOTS: tuple[str, ...] = ("/data",)


def research_output_path_safe(path: str | os.PathLike[str]) -> bool:
    """Return True when a research output ``path`` is NOT under a production卷.

    The check resolves the path and tests whether it lives inside any
    :data:`PRODUCTION_DATA_ROOTS`. Reading the authoritative config from
    ``/data/.agent-config.json`` is fine — this guard is for OUTPUT paths only.
    """
    resolved = Path(path).expanduser().resolve()
    for root in PRODUCTION_DATA_ROOTS:
        root_p = Path(root).resolve()
        if resolved == root_p or root_p in resolved.parents:
            return False
    return True


def assert_research_output_safe(path: str | os.PathLike[str]) -> None:
    """Raise ``PermissionError`` when a research output targets the production卷."""
    if not research_output_path_safe(path):
        raise PermissionError(
            f"research output path {str(path)!r} resolves under a production "
            f"data root {PRODUCTION_DATA_ROOTS}; write research artifacts to a "
            f"research dir (e.g. the repo's gitignored logs/) instead"
        )


def check_signals_pit(signals: Sequence[Signal], *, n_bars: int) -> list[str]:
    """Return a list of PIT violations in ``signals``; empty means clean."""
    errors: list[str] = []
    if n_bars <= 0:
        return errors

    seen: set[int] = set()
    for k, sig in enumerate(signals):
        idx = sig.bar_index
        if not isinstance(idx, int) or isinstance(idx, bool) or idx < 0:
            errors.append(f"signal[{k}] has invalid decision bar_index={idx!r}")
            continue
        if sig.side not in _SIDES:
            errors.append(f"signal[{k}] at bar {idx} has invalid side={sig.side!r}")
        if idx > n_bars - 2:
            # Decided on the last bar (or beyond): no later open can fill it.
            errors.append(
                f"signal[{k}] decides at bar {idx} but last tradeable "
                f"decision bar is {n_bars - 2} (n_bars={n_bars})"
            )
        if idx in seen:
            errors.append(f"duplicate signal at decision bar {idx}")
        seen.add(idx)
    return errors


def assert_signals_pit(signals: Sequence[Signal], *, n_bars: int) -> None:
    """Raise ``AssertionError`` listing every signal-side PIT violation."""
    errors = check_signals_pit(signals, n_bars=n_bars)
    if errors:
        raise AssertionError("PIT signal violations:\n  - " + "\n  - ".join(errors))


def check_trades_pit(
    trades: Sequence[Trade], *, bars: Sequence[Candle] | None = None
) -> list[str]:
    """Return a list of PIT violations in completed ``trades``."""
    errors: list[str] = []
    n = len(bars) if bars is not None else None

    for k, tr in enumerate(trades):
        if tr.entry_bar < 0 or tr.exit_bar < 0:
            errors.append(f"trade[{k}] {tr.coin} has negative bar index")
        if tr.exit_bar < tr.entry_bar:
            errors.append(
                f"trade[{k}] {tr.coin} exits at bar {tr.exit_bar} "
                f"before entry at bar {tr.entry_bar}"
            )
        if tr.exit_time_ms < tr.entry_time_ms:
            errors.append(
                f"trade[{k}] {tr.coin} exit_time {tr.exit_time_ms} "
                f"precedes entry_time {tr.entry_time_ms}"
            )
        if n is not None:
            if not 0 <= tr.entry_bar < n or not 0 <= tr.exit_bar < n:
                errors.append(
                    f"trade[{k}] {tr.coin} bar index out of range "
                    f"(entry={tr.entry_bar}, exit={tr.exit_bar}, n={n})"
                )
                continue
            if bars[tr.entry_bar].t != tr.entry_time_ms:
                errors.append(
                    f"trade[{k}] {tr.coin} entry_time {tr.entry_time_ms} != "
                    f"bar {tr.entry_bar} open time {bars[tr.entry_bar].t}"
                )
            if bars[tr.exit_bar].t != tr.exit_time_ms:
                errors.append(
                    f"trade[{k}] {tr.coin} exit_time {tr.exit_time_ms} != "
                    f"bar {tr.exit_bar} open time {bars[tr.exit_bar].t}"
                )

    # Single-position kernel: entries must be strictly ordered after exits.
    ordered = sorted(enumerate(trades), key=lambda kv: (kv[1].entry_bar, kv[0]))
    for (ki, prev), (kj, nxt) in zip(ordered, ordered[1:]):
        if nxt.entry_bar <= prev.exit_bar:
            errors.append(
                f"overlapping trades: trade[{ki}] entered bar {prev.entry_bar} "
                f"exits bar {prev.exit_bar}, but trade[{kj}] already enters "
                f"bar {nxt.entry_bar}"
            )
    return errors


def check_run_pit(
    bars: Sequence[Candle], signals: Sequence[Signal], trades: Sequence[Trade]
) -> list[str]:
    """Validate one complete kernel run end to end."""
    errors = check_signals_pit(signals, n_bars=len(bars))
    errors.extend(check_trades_pit(trades, bars=bars))
    return errors


def assert_run_pit(
    bars: Sequence[Candle], signals: Sequence[Signal], trades: Sequence[Trade]
) -> None:
    """Raise ``AssertionError`` listing every PIT violation in a kernel run."""
    errors = check_run_pit(bars, signals, trades)
    if errors:
        raise AssertionError("PIT run violations:\n  - " + "\n  - ".join(errors))
