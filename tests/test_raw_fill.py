"""P2 raw_fill 单测：tape VWAP / book 最优档 / 固定滑点兜底 / PIT 契约。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_trader.backtest.cost import CostModel
from hermes_trader.backtest.raw_fill import RawFillModel
from hermes_trader.backtest.types import ExitReason

DAY = "2026-10-30"
# 2026-10-30 00:00:00 UTC
T0 = 1793318400000


def _write_tape(root: Path, coin: str, rows: list[dict]) -> None:
    d = root / f"date={DAY}"
    d.mkdir(parents=True, exist_ok=True)
    with open(d / f"{coin}.jsonl", "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _write_book(root: Path, coin: str, rows: list[dict]) -> None:
    d = root / f"date={DAY}"
    d.mkdir(parents=True, exist_ok=True)
    with open(d / f"{coin}.jsonl", "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _tape_row(t, px, sz, spread_bps=2.0):
    return {"t": t, "coin": "BTC", "side": "B", "px": px, "sz": sz,
            "bid": px - 1, "ask": px + 1, "spread_bps": spread_bps}


def _book_row(t, bid, ask):
    return {"t": t, "b": [[str(bid), "1.0"]], "a": [[str(ask), "1.0"]]}


def _model(tmp_path, coin="BTC") -> RawFillModel:
    return RawFillModel(coin, tape_root=tmp_path / "tape",
                        book_root=tmp_path / "book")


# ── tape VWAP ──────────────────────────────────────────────────────
def test_tape_vwap_volume_weighted(tmp_path):
    # 两笔：100@1.0 + 200@3.0 → vwap = (100+600)/4 = 175
    _write_tape(tmp_path / "tape", "BTC", [
        _tape_row(T0 - 2000, 100.0, 1.0),
        _tape_row(T0 - 1000, 200.0, 3.0),
    ])
    m = _model(tmp_path)
    m.set_time(T0)
    # 买：vwap + 半 spread；spread_bps=2 → half = 175*2/2e4 = 0.0175
    px = m.fill_entry(999.0, "long")  # raw_px 被 tape 覆盖
    assert px == pytest.approx(175.0 + 175.0 * 2.0 / 2e4)
    assert m.stats["tape"] == 1


def test_tape_sell_subtracts_half_spread(tmp_path):
    _write_tape(tmp_path / "tape", "BTC", [_tape_row(T0 - 1000, 100.0, 2.0)])
    m = _model(tmp_path)
    m.set_time(T0)
    px = m.fill_exit(999.0, "long", ExitReason.END_OF_DATA)  # long 平仓=卖
    assert px == pytest.approx(100.0 - 100.0 * 2.0 / 2e4)


def test_tape_pit_excludes_future(tmp_path):
    # t_ms 之后的逐笔不得进入窗口
    _write_tape(tmp_path / "tape", "BTC", [
        _tape_row(T0 - 1000, 100.0, 1.0),
        _tape_row(T0 + 5000, 500.0, 1.0),  # 未来
    ])
    m = _model(tmp_path)
    m.set_time(T0)
    px = m.fill_entry(0.0, "long")
    # 只有 100 那笔（500 是未来，被排除）
    assert px == pytest.approx(100.0 + 100.0 * 2.0 / 2e4)


def test_tape_empty_window_falls_to_book(tmp_path):
    # tape 有数据但都在窗口外（很早），book 有该时刻帧 → 走 book
    _write_tape(tmp_path / "tape", "BTC", [_tape_row(T0 - 100000, 100.0, 1.0)])
    _write_book(tmp_path / "book", "BTC", [_book_row(T0 - 500, 99.0, 101.0)])
    m = _model(tmp_path)
    m.set_time(T0)
    px = m.fill_entry(0.0, "long")  # 买取 ask=101 + depth slip
    assert px == pytest.approx(101.0 + 101.0 * 8.0 / 1e4)
    assert m.stats["book"] == 1


# ── book 最优档 ────────────────────────────────────────────────────
def test_book_buy_takes_ask_sell_takes_bid(tmp_path):
    _write_book(tmp_path / "book", "BTC", [_book_row(T0 - 500, 99.0, 101.0)])
    m = _model(tmp_path)
    m.set_time(T0)
    buy = m.fill_entry(0.0, "long")
    assert buy == pytest.approx(101.0 + 101.0 * 8.0 / 1e4)
    m.set_time(T0)
    sell = m.fill_exit(0.0, "long", ExitReason.END_OF_DATA)
    assert sell == pytest.approx(99.0 - 99.0 * 8.0 / 1e4)


def test_book_stale_frame_falls_back(tmp_path):
    # book 帧距今 > BOOK_MAX_AGE_S(10s) → 回退固定滑点
    _write_book(tmp_path / "book", "BTC", [_book_row(T0 - 60000, 99.0, 101.0)])
    m = _model(tmp_path)
    m.set_time(T0)
    raw = 100.0
    px = m.fill_entry(raw, "long")
    expected = CostModel()._adjust(raw, True, CostModel().entry_slip_bps)
    assert px == pytest.approx(expected)
    assert m.stats["fallback"] == 1


# ── 固定滑点兜底 ───────────────────────────────────────────────────
def test_no_data_falls_back_to_cost_model(tmp_path):
    m = _model(tmp_path)  # 无任何 raw 文件
    m.set_time(T0)
    raw = 100.0
    px = m.fill_entry(raw, "long")
    expected = CostModel()._adjust(raw, True, CostModel().entry_slip_bps)
    assert px == pytest.approx(expected)
    assert m.stats["fallback"] == 1


def test_no_time_set_returns_raw(tmp_path):
    m = _model(tmp_path)
    # 未 set_time：返回 raw_px 不加滑点
    assert m.fill_entry(123.0, "long") == 123.0


# ── fee/pnl 口径不变 ───────────────────────────────────────────────
def test_fee_and_pnl_match_cost_model(tmp_path):
    m = _model(tmp_path)
    cm = CostModel()
    assert m.fee_usd(10000.0) == cm.fee_usd(10000.0)
    assert m.pnl_usd("long", 100.0, 110.0, 10000.0) == \
        cm.pnl_usd("long", 100.0, 110.0, 10000.0)


# ── driver 端到端：证明 set_time → tape fill 链路真实走通 ───────────
# 这是复核指出的缺口：RawFillModel 此前只在单测里直接调 fill_entry，
# 没有任何一笔真实交易经过 driver.run。这里构造合成 bar + signal + tape，
# 强制一笔 long 在 bar1 open 入场、bar2 end_of_data 平仓，断言两个 fill
# 价都来自 tape VWAP 而非 CostModel 固定滑点。
def test_driver_run_uses_tape_fill_end_to_end(tmp_path):
    from hermes_trader.agents.dsl_exit import ExitPolicy
    from hermes_trader.backtest import driver
    from hermes_trader.backtest.types import Signal
    from hermes_trader.models.types import Candle

    BAR_MS = 3_600_000  # 1h bars
    # bar0 = decision bar（signal），bar1 open = entry，bar2 close = exit。
    bars = [
        Candle(t=T0,              o=100.0, h=101.0, l=99.0, c=100.0, v=1.0),
        Candle(t=T0 + BAR_MS,     o=100.0, h=101.0, l=99.0, c=100.0, v=1.0),
        Candle(t=T0 + 2 * BAR_MS, o=100.0, h=101.0, l=99.0, c=105.0, v=1.0),
    ]
    # tape 在 entry 时刻（T0+BAR_MS）：真实成交 ~110（与 bar open 100 明显
    # 偏离，固定滑点不可能产生这个价）；exit 时刻 ~112。
    entry_t = T0 + BAR_MS
    exit_t = T0 + 2 * BAR_MS
    _write_tape(tmp_path / "tape", "BTC", [
        _tape_row(entry_t - 1000, 110.0, 1.0, spread_bps=0.0),
        _tape_row(exit_t - 1000, 112.0, 1.0, spread_bps=0.0),
    ])
    model = RawFillModel("BTC",
                         fallback=CostModel(round_trip_fee_bps=0.0,
                                            entry_slip_bps=5.0,
                                            exit_slip_bps=15.0,
                                            stop_delay_slip_bps=10.0),
                         tape_root=tmp_path / "tape",
                         book_root=tmp_path / "book")
    policy = ExitPolicy(
        max_loss_pct=50.0, max_loss_roe_pct=1e9, protect_pct=50.0,
        retrace_threshold=0.30, hard_timeout_minutes=1e9,
        breakeven_trigger_pct=0.0, breakeven_lock_pct=0.0,
        stale_flat_timeout_minutes=0.0,
        hard_stop_confirm_sec=0.0, breach_confirm_sec=0.0,
    )
    trades = driver.run(bars, [Signal(0, "long")], policy,
                        coin="BTC", notional_usd=10_000.0,
                        cost=model, bar_ms=BAR_MS)
    assert len(trades) == 1, "synthetic run must produce exactly one trade"
    t = trades[0]
    # 固定滑点入场价会是 100.05；tape 路径必须是 110（spread_bps=0）。
    assert t.entry_fill_px == pytest.approx(110.0, abs=1e-9)
    # end_of_data exit 在 bar2 close=105，但 tape 覆盖为 112。
    assert t.exit_fill_px == pytest.approx(112.0, abs=1e-9)
    # 两笔 fill 都命中 tape，零次兜底——证明 set_time 被 driver 真实调用。
    assert model.stats == {"tape": 2, "book": 0, "fallback": 0}
