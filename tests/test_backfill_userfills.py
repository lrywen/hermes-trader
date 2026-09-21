"""交易所 userFills 补录脚本与 dashboard 接入测试。

验证：
  - backfill_userfills.normalize 按 dir 把成交拆成 execute(开仓)/close(平仓)，
    净美元=closedPnl-开仓费估算，并产出成交口径的 realized_pnl_pct；
  - dashboard._read_backfill_lines 读取嵌套记录（含 size/mtime 缓存）；
  - 补录 close 行进入 trades 时间线并与开仓配对。
"""
from __future__ import annotations

import json

from hermes_trader import dashboard
from scripts import backfill_userfills as bf


def _fill(tid, time, coin, dir_, side, px, sz, closed_pnl=0.0, fee=0.005):
    return {
        "tid": tid, "time": time, "coin": coin, "dir": dir_, "side": side,
        "px": px, "sz": sz, "closedPnl": closed_pnl, "fee": fee, "oid": tid * 10,
    }


def test_normalize_splits_open_and_close():
    fills = [
        _fill(1, 1000, "MEGA", "Open Long", "B", 0.037, 283.0),
        _fill(2, 2000, "MEGA", "Close Long", "A", 0.0385, 283.0, closed_pnl=0.35),
        _fill(3, 3000, "XYZ", "Open Short", "A", 1.0, 10.0),
        _fill(4, 4000, "XYZ", "Close Short", "B", 0.99, 10.0, closed_pnl=0.08),
    ]
    recs = bf.normalize(fills)
    assert [r["event"] for r in recs] == ["execute", "close", "execute", "close"]
    open_long = recs[0]["payload"]
    assert open_long["side"] == "long" and open_long["executed"] is True
    close_long = recs[1]["payload"]
    # 净额 = closedPnl - 名义×2.5bps；名义=0.0385×283≈10.8955
    notional = round(0.0385 * 283.0, 6)
    entry_fee = round(notional * 0.00025, 6)
    assert close_long["realized_pnl_usd"] == round(0.35 - entry_fee, 6)
    assert close_long["realized_pnl_pct"] is not None
    assert close_long["close_source"] == "exchange_userfills_backfill"
    # 空头：开仓 dir=Open Short → side short
    assert recs[2]["payload"]["side"] == "short"
    assert recs[3]["payload"]["side"] == "short"


def test_dashboard_wires_backfill_into_trades(monkeypatch, tmp_path):
    path = tmp_path / "userfills-backfill.jsonl"
    fills = [
        _fill(1, 1_700_000_000_000, "AAA", "Open Long", "B", 1.0, 10.0),
        _fill(2, 1_700_000_600_000, "AAA", "Close Long", "A", 1.05, 10.0,
              closed_pnl=0.5),
    ]
    with path.open("w", encoding="utf-8") as f:
        for r in bf.normalize(fills):
            f.write(json.dumps(r) + "\n")

    # 隔离其它数据源：空 session-log、空 outcome，只读补录文件。
    monkeypatch.setattr(dashboard, "_read_trade_log_lines", lambda: [])
    monkeypatch.setattr(dashboard, "_read_outcome_lines", lambda: [])
    monkeypatch.setattr(dashboard, "_BACKFILL_FILE", path)
    dashboard._BACKFILL_CACHE.update(sig=None, lines=[])

    tr = dashboard._trades_payload(50)
    assert len(tr) == 2  # 1 开 1 平
    close = next(r for r in tr if r["kind"] == "close")
    assert close["source"] == "userfills_backfill"
    assert close["pair_id"] is not None  # 与开仓成功配对
    assert close["hold_minutes"] == 10.0  # 600s = 10min
    # 清掉本用例写入的全局缓存，避免 tmp 路径记录跨用例泄漏。
    dashboard._BACKFILL_CACHE.update(sig=None, lines=[])
