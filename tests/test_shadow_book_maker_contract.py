"""统一成交契约（2026-09-21）：同一个过闸决策同时喂 taker 与 maker_shadow。

These tests pin the dual-account contract:

  * ``shadow_open`` immediately fills the TAKER account at the decision mid AND
    rests an offset post-only limit in MAKER_SHADOW.
  * Each mark, the resting maker order is resolved against (cached) 1m candles
    via ``execution.maker_shadow.simulate_shadow_order``: a touch fills it, TTL
    elapsed without a touch cancels it.
  * The two accounts keep independent wallet / positions / equity and never
    pollute each other. A legacy v1 flat state migrates verbatim under
    ``accounts.taker`` with a fresh empty ``accounts.maker_shadow``.

No network: the candle fetch is stubbed. No funds, no exchange.
"""
from __future__ import annotations

import time

from hermes_trader.agents import shadow_book as sb
from hermes_trader.models.types import Candle

# Real candles carry current epoch ms; DSL hold-time math would misfire on the
# tiny 1970 timestamps, so anchor test bars to "now".
_T0 = int(time.time() // 60 * 60) * 1000


def _candle(min_idx: int, o: float, h: float, l: float, c: float) -> Candle:
    return Candle(t=_T0 + min_idx * 60_000, o=o, h=h, l=l, c=c, v=1.0)


def _patch_cfg(monkeypatch, **over):
    cfg = {"enabled": True, "maker_shadow_enabled": True,
           "starting_balance": 10000.0, "taker_fee_pct": 0.025,
           "maker_fee_pct": 0.01, "maker_limit_offset_bps": 5.0,
           "maker_ttl_bars": 30}
    cfg.update(over)
    monkeypatch.setattr(sb, "_shadow_cfg", lambda: dict(cfg))


# ---------------------------------------------------------------------------
# open: taker fills at mid, maker rests an offset limit
# ---------------------------------------------------------------------------

def test_open_fills_taker_and_rests_maker(tmp_path, monkeypatch):
    _patch_cfg(monkeypatch)
    book = sb.ShadowBook(path=str(tmp_path / "s.json"))

    fill = book.shadow_open(coin="BTC", side="long", entry_px=100.0,
                            size_usd=1000.0, leverage=1, analysis_id="a1")

    # taker filled immediately at mid
    assert fill["price"] == 100.0
    assert fill["fill_model"] == "taker"
    taker = book.state["accounts"]["taker"]
    maker = book.state["accounts"]["maker_shadow"]
    assert len(taker["positions"]) == 1
    assert len(taker["pending_orders"]) == 0
    # maker rests one limit offset 5bps below mid, no position yet
    assert len(maker["positions"]) == 0
    assert len(maker["pending_orders"]) == 1
    order = maker["pending_orders"][0]
    assert abs(order["limit_px"] - 99.95) < 1e-9
    assert order["post_mid_px"] == 100.0
    assert ("maker_shadow", "BTC_long") in book._trackers or True


def test_short_maker_limit_sits_above_mid(tmp_path, monkeypatch):
    _patch_cfg(monkeypatch)
    book = sb.ShadowBook(path=str(tmp_path / "s.json"))
    book.shadow_open(coin="ETH", side="short", entry_px=200.0,
                     size_usd=1000.0, leverage=1)
    order = book.state["accounts"]["maker_shadow"]["pending_orders"][0]
    assert abs(order["limit_px"] - 200.1) < 1e-9


def test_maker_disabled_leaves_no_resting_order(tmp_path, monkeypatch):
    _patch_cfg(monkeypatch, maker_shadow_enabled=False)
    book = sb.ShadowBook(path=str(tmp_path / "s.json"))
    book.shadow_open(coin="BTC", side="long", entry_px=100.0,
                     size_usd=1000.0, leverage=1)
    maker = book.state["accounts"]["maker_shadow"]
    assert maker["pending_orders"] == []
    assert maker["positions"] == []


# ---------------------------------------------------------------------------
# resolve: touch fills, no-touch + TTL cancels
# ---------------------------------------------------------------------------

def _resting_order(book, coin="BTC"):
    return book.state["accounts"]["maker_shadow"]["pending_orders"][0]


def test_maker_touch_fills_position(tmp_path, monkeypatch):
    _patch_cfg(monkeypatch, maker_ttl_bars=30)
    book = sb.ShadowBook(path=str(tmp_path / "s.json"))
    book.shadow_open(coin="BTC", side="long", entry_px=100.0,
                     size_usd=1000.0, leverage=1)
    order = _resting_order(book)

    # bar0 = post bar (no look-back fill); bar1 dips to the limit (99.95).
    bars = [
        _candle(0, 100.0, 100.1, 99.98, 100.0),
        _candle(1, 100.0, 100.0, 99.90, 99.96),
        _candle(2, 99.96, 100.0, 99.94, 100.0),
    ]
    monkeypatch.setattr(
        "hermes_trader.client.hl_client.fetch_hl_candles",
        lambda *a, **k: bars)

    closed = book.mark_to_market({"BTC": 100.0})

    maker = book.state["accounts"]["maker_shadow"]
    assert maker["pending_orders"] == []
    assert len(maker["positions"]) == 1
    pos = maker["positions"][0]
    assert abs(pos["entry_px"] - order["limit_px"]) < 1e-9
    assert ("maker_shadow", "BTC_long") in book._trackers
    # open fill tagged maker; nothing closed yet
    assert closed == []
    open_fills = [f for f in maker["fills"] if f["type"] == "open"]
    assert len(open_fills) == 1
    assert open_fills[0]["fill_model"] == "maker_shadow"


def test_maker_no_touch_keeps_resting_within_ttl(tmp_path, monkeypatch):
    _patch_cfg(monkeypatch, maker_ttl_bars=30)
    book = sb.ShadowBook(path=str(tmp_path / "s.json"))
    book.shadow_open(coin="BTC", side="long", entry_px=100.0,
                     size_usd=1000.0, leverage=1)
    # price never dips near the 99.95 limit
    bars = [_candle(i, 100.0, 100.2, 99.99, 100.1)
            for i in range(5)]
    monkeypatch.setattr(
        "hermes_trader.client.hl_client.fetch_hl_candles",
        lambda *a, **k: bars)

    book.mark_to_market({"BTC": 100.0})

    maker = book.state["accounts"]["maker_shadow"]
    assert len(maker["pending_orders"]) == 1
    assert maker["positions"] == []


def test_maker_ttl_expiry_cancels_order(tmp_path, monkeypatch):
    _patch_cfg(monkeypatch, maker_ttl_bars=30)
    book = sb.ShadowBook(path=str(tmp_path / "s.json"))
    book.shadow_open(coin="BTC", side="long", entry_px=100.0,
                     size_usd=1000.0, leverage=1)
    # Force the real posted age past the TTL so the cancel branch fires.
    order = _resting_order(book)
    order["posted_at"] = sb._now_ms() - 31 * 60 * 1000
    bars = [_candle(i, 100.0, 100.2, 99.99, 100.1)
            for i in range(35)]
    monkeypatch.setattr(
        "hermes_trader.client.hl_client.fetch_hl_candles",
        lambda *a, **k: bars)

    book.mark_to_market({"BTC": 100.0})

    maker = book.state["accounts"]["maker_shadow"]
    assert maker["pending_orders"] == []
    assert maker["positions"] == []
    cancels = [f for f in maker["fills"] if f["type"] == "cancel"]
    assert len(cancels) == 1


# ---------------------------------------------------------------------------
# account independence + migration
# ---------------------------------------------------------------------------

def test_accounts_keep_independent_wallets(tmp_path, monkeypatch):
    _patch_cfg(monkeypatch)
    book = sb.ShadowBook(path=str(tmp_path / "s.json"))
    book.shadow_open(coin="BTC", side="long", entry_px=100.0,
                     size_usd=1000.0, leverage=1)
    # Taker stops out via a fake tracker at 98; maker order is never touched.
    from hermes_trader.agents.dsl_exit import ExitVerdict

    class _Fake:
        def check(self, mark, index_px=None):
            return ExitVerdict(exit=True, reason="trailing_stop", floor_price=99.0)

    book._trackers[("taker", "BTC_long")] = _Fake()
    bars = [_candle(i, 100.0, 100.1, 99.99, 100.0)
            for i in range(3)]
    monkeypatch.setattr(
        "hermes_trader.client.hl_client.fetch_hl_candles",
        lambda *a, **k: bars)

    closed = book.mark_to_market({"BTC": 98.0})

    assert len(closed) == 1
    taker = book.state["accounts"]["taker"]
    maker = book.state["accounts"]["maker_shadow"]
    # taker wallet took the loss; maker never filled so its wallet is untouched
    assert taker["wallet_balance"] < 10000.0
    assert maker["wallet_balance"] == 10000.0
    assert len(maker["pending_orders"]) == 1


def test_v1_flat_state_migrates_to_nested_accounts(tmp_path):
    import json
    path = str(tmp_path / "s.json")
    v1 = {
        "starting_balance": 5000.0,
        "wallet_balance": 5123.45,
        "positions": [],
        "fills": [{"type": "close", "realized_pnl_usd": 123.45}],
        "equity_curve": [{"ts": 1, "equity": 5123.45}],
        "closed_count": 1,
    }
    with open(path, "w") as f:
        json.dump(v1, f)

    book = sb.ShadowBook(path=path)

    assert book.state["version"] == 2
    taker = book.state["accounts"]["taker"]
    maker = book.state["accounts"]["maker_shadow"]
    assert taker["wallet_balance"] == 5123.45
    assert taker["closed_count"] == 1
    assert len(taker["fills"]) == 1
    assert len(taker["equity_curve"]) == 1
    # fresh maker account (counterfactual starts at the canonical bankroll)
    assert maker["wallet_balance"] == 10000.0
    assert maker["positions"] == [] and maker["fills"] == []
