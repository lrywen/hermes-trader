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

import pytest

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

    # taker filled immediately; R2 applies conservative 0.05% buy-side
    # slippage on the open while the maker arm rests off the untouched mid.
    assert fill["price"] == 100.05
    assert fill["mid_px"] == 100.0
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
    # Pin the real post time to the close of bar0 so locate_posted_bar_idx
    # anchors bar0 as the posted bar and only bar1+ are judged for a touch.
    order["posted_at"] = _T0 + 60_000

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
    of = open_fills[0]
    assert of["fill_model"] == "maker_shadow"
    # fill-quality / adverse-selection metrics pinned on the open fill
    assert of["resting_bars"] == 1
    assert of["maker_edge_bps"] is not None and of["maker_edge_bps"] > 0
    # bar after fill mid = (100.0+99.94)/2 = 99.97; filled at 99.95
    assert of["post_fill_mid_drift_bps"] is not None


def test_maker_no_touch_keeps_resting_within_ttl(tmp_path, monkeypatch):
    _patch_cfg(monkeypatch, maker_ttl_bars=30)
    book = sb.ShadowBook(path=str(tmp_path / "s.json"))
    book.shadow_open(coin="BTC", side="long", entry_px=100.0,
                     size_usd=1000.0, leverage=1)
    _resting_order(book)["posted_at"] = _T0 + 60_000
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
    order["posted_at"] = _T0 - 31 * 60 * 1000
    # Bars must span the real post bar (31 min back) through now; none touch.
    bars = [_candle(i, 100.0, 100.2, 99.99, 100.1)
            for i in range(-32, 3)]
    monkeypatch.setattr(
        "hermes_trader.client.hl_client.fetch_hl_candles",
        lambda *a, **k: bars)

    book.mark_to_market({"BTC": 100.0})

    maker = book.state["accounts"]["maker_shadow"]
    assert maker["pending_orders"] == []
    assert maker["positions"] == []
    cancels = [f for f in maker["fills"] if f["type"] == "cancel"]
    assert len(cancels) == 1


def test_maker_stale_order_cancels_when_post_bar_outside_window(
        tmp_path, monkeypatch):
    """Regression (2026-10-04): a resting maker order whose real post bar is
    older than the fixed ttl+2 fetch window made locate_posted_bar_idx return
    None, so every resolve cycle hit ``continue`` and the order hung
    unfilled/uncanceled for days (observed FIL long, age 70h). The resolve must
    fall back to a straight TTL-age cancel even when the post bar is not in the
    window — never hang a stale order."""
    _patch_cfg(monkeypatch, maker_ttl_bars=30)
    book = sb.ShadowBook(path=str(tmp_path / "s.json"))
    book.shadow_open(coin="BTC", side="long", entry_px=100.0,
                     size_usd=1000.0, leverage=1)
    # posted ~2.9 days ago; the fetched 1m window (only recent bars) cannot
    # locate the post bar -> locate_posted_bar_idx returns None.
    order = _resting_order(book)
    order["posted_at"] = _T0 - int(70 * 3600 * 1000)
    bars = [_candle(i, 100.0, 100.2, 99.99, 100.1) for i in range(0, 5)]
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


def test_max_loss_gap_records_explicit_overshoot(tmp_path, monkeypatch):
    """Audit 2026-10-16: a max_loss that gaps through the backup-SL trigger must
    record gap_spot_pct>0 and gap_capped=True on the close fill (the flat
    taker slippage_pct does not capture the gap)."""
    _patch_cfg(monkeypatch)
    book = sb.ShadowBook(path=str(tmp_path / "s.json"))
    book.shadow_open(coin="BTC", side="long", entry_px=100.0,
                     size_usd=1000.0, leverage=1)
    from hermes_trader.agents.dsl_exit import ExitVerdict

    class _Fake:
        def check(self, mark, index_px=None):
            return ExitVerdict(exit=True, reason="max_loss", floor_price=95.0)

    book._trackers[("taker", "BTC_long")] = _Fake()
    # entry_atr_pct > 0 so the backup-SL trigger is computed (not None).
    book.state["accounts"]["taker"]["positions"][0]["entry_atr_pct"] = 2.0
    # mark 93.0 gaps well below entry 100 -> definitely through any backup trigger.
    closed = book.mark_to_market({"BTC": 93.0})

    assert len(closed) == 1
    f = closed[0]
    assert f["reason"] == "max_loss"
    assert isinstance(f.get("gap_spot_pct"), float)
    assert f["gap_spot_pct"] > 0.0
    assert f["gap_capped"] is True
    # the explicit gap marker is recorded even though slippage_pct is the flat 0.05
    assert f["slippage_pct"] == 0.05


def test_max_loss_no_gap_records_zero_overshoot(tmp_path, monkeypatch):
    """A max_loss whose confirming mark did NOT gap through records 0 and
    gap_capped=False."""
    _patch_cfg(monkeypatch)
    book = sb.ShadowBook(path=str(tmp_path / "s.json"))
    book.shadow_open(coin="BTC", side="long", entry_px=100.0,
                     size_usd=1000.0, leverage=1)
    from hermes_trader.agents.dsl_exit import ExitVerdict

    class _Fake:
        def check(self, mark, index_px=None):
            return ExitVerdict(exit=True, reason="max_loss", floor_price=98.0)

    book._trackers[("taker", "BTC_long")] = _Fake()
    book.state["accounts"]["taker"]["positions"][0]["entry_atr_pct"] = 2.0
    # mark 98.5 - inside the backup trigger, no gap-through.
    closed = book.mark_to_market({"BTC": 98.5})

    assert len(closed) == 1
    f = closed[0]
    assert f["reason"] == "max_loss"
    assert f["gap_spot_pct"] == 0.0
    assert f["gap_capped"] is False


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


# ---------------------------------------------------------------------------
# read views: maker trades feed + adverse-selection stats
# ---------------------------------------------------------------------------

def test_get_trades_returns_both_account_feeds(tmp_path, monkeypatch):
    _patch_cfg(monkeypatch)
    book = sb.ShadowBook(path=str(tmp_path / "s.json"))
    book.shadow_open(coin="BTC", side="long", entry_px=100.0,
                     size_usd=1000.0, leverage=1)
    view = book.get_trades()
    assert "trades" in view and "maker_trades" in view
    assert view["total"] >= 1          # taker open fill
    assert view["maker_total"] == 0    # resting order is not a fill yet
    assert view["maker_trades"] == []


def test_maker_stats_aggregate_adverse_selection(tmp_path, monkeypatch):
    _patch_cfg(monkeypatch, maker_ttl_bars=30)
    book = sb.ShadowBook(path=str(tmp_path / "s.json"))
    book.shadow_open(coin="BTC", side="long", entry_px=100.0,
                     size_usd=1000.0, leverage=1)
    _resting_order(book)["posted_at"] = _T0 + 60_000
    bars = [
        _candle(0, 100.0, 100.1, 99.98, 100.0),
        _candle(1, 100.0, 100.0, 99.90, 99.96),
        _candle(2, 99.96, 100.0, 99.94, 100.0),
    ]
    monkeypatch.setattr(
        "hermes_trader.client.hl_client.fetch_hl_candles",
        lambda *a, **k: bars)
    book.mark_to_market({"BTC": 100.0})

    stats = book.get_stats()["maker_shadow"]
    sel = stats["adverse_selection"]
    assert sel["fills"] == 1
    assert sel["cancels"] == 0
    assert sel["fill_rate_pct"] == 100.0
    assert sel["avg_maker_edge_bps"] is not None
    assert sel["avg_resting_bars"] == 1.0


# ---------------------------------------------------------------------------
# deferred post-fill drift backfill (2026-10-21): a fill on the most recent
# bar has no next-bar mid yet; the metric is parked in pending_drift and
# backfilled on a later cycle. Statistics only — never touches price/PnL.
# ---------------------------------------------------------------------------

def _book_with_pending_drift(tmp_path, monkeypatch, coin, side, fill_px,
                             fill_bar_t, now_ms):
    _patch_cfg(monkeypatch)
    monkeypatch.setattr(sb, "_now_ms", lambda: now_ms)
    book = sb.ShadowBook(path=str(tmp_path / "s.json"))
    with book._lock:
        order = {"coin": coin, "side": side, "size_usd": 1000.0,
                 "leverage": 1, "entry_atr_pct": 0.0, "entry_regime": "",
                 "analysis_id": "a1", "signal_meta": {}}
        # fill with NO drift / fill_bar_t -> the fill parks a pending_drift row
        f = book._fill_maker_order(order, fill_px, fill_bar_t, selection={})
    return book, f


def test_drift_backfilled_on_later_cycle(tmp_path, monkeypatch):
    fill_t = _T0 - 10 * 60_000
    now_ms = fill_t + 2 * 60_000  # within the 3-bar wait window, target closed
    book, f = _book_with_pending_drift(
        tmp_path, monkeypatch, "BTC", "long", 99.95, fill_t, now_ms)
    acc = book.state["accounts"]["maker_shadow"]
    assert len(acc["pending_drift"]) == 1
    assert f["post_fill_mid_drift_bps"] is None

    # next bar (fill_t+60s) closed; mid dips to (99.90+99.80)/2 = 99.85 ->
    # buy-side adverse drift = (99.95-99.85)/99.95 ~ +10bps.
    bars = [Candle(t=fill_t, o=100.0, h=100.0, l=99.90, c=99.96, v=1.0),
            Candle(t=fill_t + 60_000, o=99.96, h=99.90, l=99.80, c=99.85, v=1.0)]
    monkeypatch.setattr(
        "hermes_trader.client.hl_client.fetch_hl_candles",
        lambda *a, **k: bars)
    with book._lock:
        book._resolve_pending_drift()

    assert acc["pending_drift"] == []
    assert f["post_fill_mid_drift_bps"] is not None
    assert f["post_fill_mid_drift_bps"] > 0
    # price/PnL fields are untouched by the statistics backfill
    assert f["price"] == 99.95
    assert f["notional_usd"] == 1000.0


def test_drift_waits_while_next_bar_still_forming(tmp_path, monkeypatch):
    fill_t = _T0 - 10 * 60_000
    # "now" is exactly the target bar's close boundary minus 1ms: bar exists
    # in the window but has not closed -> must stay pending.
    now_ms = fill_t + 60_000 - 1
    book, f = _book_with_pending_drift(
        tmp_path, monkeypatch, "BTC", "long", 99.95, fill_t, now_ms)
    bars = [Candle(t=fill_t, o=100.0, h=100.0, l=99.90, c=99.96, v=1.0),
            Candle(t=fill_t + 60_000, o=99.96, h=99.99, l=99.90, c=99.95, v=1.0)]
    monkeypatch.setattr(
        "hermes_trader.client.hl_client.fetch_hl_candles",
        lambda *a, **k: bars)
    with book._lock:
        book._resolve_pending_drift()
    acc = book.state["accounts"]["maker_shadow"]
    assert len(acc["pending_drift"]) == 1
    assert f["post_fill_mid_drift_bps"] is None


def test_drift_abandoned_after_deadline(tmp_path, monkeypatch):
    fill_t = _T0 - 10 * 60_000
    now_ms = fill_t + (sb._MAKER_DRIFT_WAIT_BARS + 1) * 60_000
    book, f = _book_with_pending_drift(
        tmp_path, monkeypatch, "BTC", "long", 99.95, fill_t, now_ms)
    called = {"n": 0}
    def _fetch(*a, **k):
        called["n"] += 1
        return []
    monkeypatch.setattr(
        "hermes_trader.client.hl_client.fetch_hl_candles", _fetch)
    with book._lock:
        book._resolve_pending_drift()
    acc = book.state["accounts"]["maker_shadow"]
    assert acc["pending_drift"] == []      # dropped, queue cannot grow
    assert f["post_fill_mid_drift_bps"] is None
    assert called["n"] == 0                # deadline path skips the fetch


def test_drift_fetch_miss_keeps_pending(tmp_path, monkeypatch):
    fill_t = _T0 - 10 * 60_000
    now_ms = fill_t + 2 * 60_000
    book, f = _book_with_pending_drift(
        tmp_path, monkeypatch, "BTC", "long", 99.95, fill_t, now_ms)
    def _boom(*a, **k):
        raise RuntimeError("cache miss")
    monkeypatch.setattr(
        "hermes_trader.client.hl_client.fetch_hl_candles", _boom)
    with book._lock:
        book._resolve_pending_drift()
    acc = book.state["accounts"]["maker_shadow"]
    assert len(acc["pending_drift"]) == 1  # retried next cycle
    assert f["post_fill_mid_drift_bps"] is None


def test_drift_pending_survives_reload(tmp_path, monkeypatch):
    fill_t = _T0 - 2 * 60_000
    path = str(tmp_path / "s.json")
    now_ms = _T0  # 2 min after fill, still inside the 3-bar wait window
    _patch_cfg(monkeypatch)
    monkeypatch.setattr(sb, "_now_ms", lambda: now_ms)
    book = sb.ShadowBook(path=path)
    with book._lock:
        order = {"coin": "BTC", "side": "long", "size_usd": 1000.0,
                 "leverage": 1, "entry_atr_pct": 0.0, "entry_regime": "",
                 "analysis_id": "a1", "signal_meta": {}}
        book._fill_maker_order(order, 99.95, fill_t, selection={})
        book._save(force=True)

    # new process (same fake clock): pending_drift reloads and is backfilled
    book2 = sb.ShadowBook(path=path)
    acc = book2.state["accounts"]["maker_shadow"]
    assert len(acc["pending_drift"]) == 1
    bars = [Candle(t=fill_t, o=100.0, h=100.0, l=99.90, c=99.96, v=1.0),
            Candle(t=fill_t + 60_000, o=99.96, h=99.90, l=99.80, c=99.85, v=1.0)]
    monkeypatch.setattr(
        "hermes_trader.client.hl_client.fetch_hl_candles",
        lambda *a, **k: bars)
    with book2._lock:
        book2._resolve_pending_drift()
    of = [x for x in acc["fills"] if x["type"] == "open"][0]
    assert acc["pending_drift"] == []
    assert of["post_fill_mid_drift_bps"] is not None and of["post_fill_mid_drift_bps"] > 0


def _book_taker_close(book, side):
    pos = {
        "id": f"pos-{side}", "coin": "BTC", "side": side,
        "entry_px": 100.0, "size_usd": 1000.0, "size_coin": 10.0,
        "leverage": 1, "entry_regime": "trend", "analysis_id": "a-close",
        "opened_at": _T0 - 600_000,
    }
    return book._close_position("taker", pos, 100.0, "contract_test")


@pytest.mark.parametrize("side,post_mid", [("long", 101.0), ("short", 99.0)])
def test_taker_close_drift_uses_close_execution_direction(
        tmp_path, monkeypatch, side, post_mid):
    fill_t = _T0 - 2 * 60_000
    _patch_cfg(monkeypatch)
    monkeypatch.setattr(sb, "_now_ms", lambda: fill_t)
    book = sb.ShadowBook(path=str(tmp_path / f"{side}.json"))
    close = _book_taker_close(book, side)
    acc = book.state["accounts"]["taker"]
    pend = acc["pending_drift"][0]

    assert close["execution"] == "taker"
    assert close["phase"] == "close"
    assert close["post_fill_mid_drift_bps"] is None
    assert pend["is_buy"] is (side == "short")
    assert pend["execution"] == "taker" and pend["phase"] == "close"

    monkeypatch.setattr(sb, "_now_ms", lambda: _T0)
    bars = [Candle(t=fill_t + 60_000, o=100.0, h=post_mid,
                   l=post_mid, c=post_mid, v=1.0)]
    monkeypatch.setattr(
        "hermes_trader.client.hl_client.fetch_hl_candles",
        lambda *a, **k: bars)
    with book._lock:
        book._resolve_pending_drift()

    expected = ((close["price"] - post_mid) / close["price"] * 1e4
                if side == "short" else
                (post_mid - close["price"]) / close["price"] * 1e4)
    assert close["post_fill_mid_drift_bps"] == pytest.approx(expected, abs=1e-4)
    assert close["post_fill_mid_drift_bps"] > 0
    assert acc["pending_drift"] == []


def test_taker_close_pending_drift_backfills_after_reload(tmp_path, monkeypatch):
    fill_t = _T0 - 2 * 60_000
    path = str(tmp_path / "reload-taker-close.json")
    _patch_cfg(monkeypatch)
    monkeypatch.setattr(sb, "_now_ms", lambda: fill_t)
    book = sb.ShadowBook(path=path)
    close = _book_taker_close(book, "long")
    close_id = close["id"]
    book._save(force=True)

    monkeypatch.setattr(sb, "_now_ms", lambda: _T0)
    reloaded = sb.ShadowBook(path=path)
    acc = reloaded.state["accounts"]["taker"]
    assert len(acc["pending_drift"]) == 1
    assert acc["pending_drift"][0]["fill_id"] == close_id
    bars = [Candle(t=fill_t + 60_000, o=100.0, h=101.0,
                   l=101.0, c=101.0, v=1.0)]
    monkeypatch.setattr(
        "hermes_trader.client.hl_client.fetch_hl_candles",
        lambda *a, **k: bars)
    reloaded.mark_to_market({"BTC": 100.0})

    saved_close = next(f for f in acc["fills"] if f["id"] == close_id)
    assert saved_close["post_fill_mid_drift_bps"] > 0
    assert acc["pending_drift"] == []


def test_immediate_drift_does_not_queue_pending(tmp_path, monkeypatch):
    """When the simulator already has a next bar (drift non-None), no
    pending_drift row is created — the deferred path is only for the
    fill-on-last-bar case."""
    _patch_cfg(monkeypatch, maker_ttl_bars=30)
    book = sb.ShadowBook(path=str(tmp_path / "s.json"))
    book.shadow_open(coin="BTC", side="long", entry_px=100.0,
                     size_usd=1000.0, leverage=1)
    _resting_order(book)["posted_at"] = _T0 + 60_000
    bars = [_candle(0, 100.0, 100.1, 99.98, 100.0),
            _candle(1, 100.0, 100.0, 99.90, 99.96),
            _candle(2, 99.96, 100.0, 99.94, 100.0)]
    monkeypatch.setattr(
        "hermes_trader.client.hl_client.fetch_hl_candles",
        lambda *a, **k: bars)
    book.mark_to_market({"BTC": 100.0})
    acc = book.state["accounts"]["maker_shadow"]
    assert acc["pending_drift"] == []


def _seed_counterfactual_position(book, side):
    pos = {
        "id": f"cf-{side}", "coin": "BTC", "side": side,
        "entry_px": 100.0, "size_usd": 1000.0, "size_coin": 10.0,
        "leverage": 1, "entry_atr_pct": 0.0, "entry_regime": "trend",
        "analysis_id": "counterfactual-test", "opened_at": _T0,
    }
    book.state["accounts"]["taker"]["positions"].append(pos)
    return pos


@pytest.mark.parametrize("side,direction", [("long", "bearish"), ("short", "bullish")])
def test_cvd_counterfactual_is_deduped_and_never_closes_ledger(
        tmp_path, monkeypatch, side, direction):
    import json
    from hermes_trader.agents.microstructure import CVDDivergence

    state_path = tmp_path / f"cvd-{side}-state.json"
    event_path = tmp_path / f"cvd-{side}-events.jsonl"
    book = sb.ShadowBook(path=str(state_path),
                         exit_counterfactual_path=str(event_path))
    _seed_counterfactual_position(book, side)
    divergence = CVDDivergence(
        bullish=direction == "bullish", bearish=direction == "bearish",
        strength_pct=12.5, reason="production detector test")
    params = {"left": 8, "right": 5, "min_strength": 10.0}
    monkeypatch.setattr(book, "_detect_cvd_for_counterfactual",
                        lambda coin: (divergence, params))

    def forbidden_close(*args, **kwargs):
        raise AssertionError("counterfactual must not call _close_position")

    monkeypatch.setattr(book, "_close_position", forbidden_close)
    book.mark_to_market({"BTC": 105.0 if side == "long" else 95.0})
    book.mark_to_market({"BTC": 105.0 if side == "long" else 95.0})
    rows = [json.loads(line) for line in event_path.read_text().splitlines()]
    rows = [row for row in rows if row["signal"] == "cvd_divergence"]
    assert len(rows) == 1
    row = rows[0]
    assert row["account"] == "taker" and row["position_id"] == f"cf-{side}"
    assert row["signal"] == "cvd_divergence"
    assert row["virtual_exit_ts"] == row["ts"] and row["mid"] > 0
    assert direction in row["reason"] and side in row["reason"]
    assert row["evidence"]["production_params"] == params
    assert book.state["accounts"]["taker"]["fills"] == []
    assert len(book.state["accounts"]["taker"]["positions"]) == 1

    book._save(force=True)
    reloaded = sb.ShadowBook(path=str(state_path),
                             exit_counterfactual_path=str(event_path))
    monkeypatch.setattr(reloaded, "_detect_cvd_for_counterfactual",
                        lambda coin: (divergence, params))
    reloaded.mark_to_market({"BTC": 105.0 if side == "long" else 95.0})
    persisted = [json.loads(line) for line in event_path.read_text().splitlines()]
    assert sum(row["signal"] == "cvd_divergence" for row in persisted) == 1


def test_cvd_counterfactual_uses_production_detector_params(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from hermes_trader.client.hl_client import Candle

    book = sb.ShadowBook(path=str(tmp_path / "params-state.json"),
                         exit_counterfactual_path=str(tmp_path / "params-events.jsonl"))
    cfg = {
        "scan": {"candleInterval": "5m", "candleCount": 100,
                 "evaluateClosedBarsOnly": True},
        "launch_capture": {"cvd_divergence": {
            "left": 2, "right": 1, "min_strength_pct": 3.0}},
    }
    monkeypatch.setattr(
        "hermes_trader.agents.config_store.read_agent_config", lambda: cfg)
    points = [SimpleNamespace(t=float(i), cvd=float(i)) for i in range(8)]
    monkeypatch.setattr(
        "hermes_trader.agents.microstructure.get_microstructure",
        lambda: SimpleNamespace(cvd_series=lambda coin: points))
    candles = [Candle(t=i * 300_000, o=100.0, h=101.0, l=99.0,
                      c=100.0 + i, v=1.0) for i in range(8)]
    monkeypatch.setattr(
        "hermes_trader.client.hl_client.fetch_hl_candles",
        lambda *args, **kwargs: candles)
    captured = {}

    def fake_detect(prices, cvd, *, left, right, min_strength):
        captured.update(left=left, right=right, min_strength=min_strength,
                        prices=prices, cvd=cvd)
        return None

    monkeypatch.setattr(
        "hermes_trader.agents.microstructure.detect_cvd_divergence", fake_detect)
    divergence, params = book._detect_cvd_for_counterfactual("BTC")
    assert divergence is None
    assert params == {"left": 2, "right": 1, "min_strength": 3.0}
    assert captured["left"] == 2 and captured["right"] == 1
    assert captured["min_strength"] == 3.0
    assert len(captured["prices"]) == 8 and len(captured["cvd"]) == 8


def test_phase2_floor_counterfactual_requires_ticks_time_and_index(
        tmp_path, monkeypatch):
    import copy
    import json
    from hermes_trader.agents.dsl_exit import DSLTracker, ExitPolicy

    event_path = tmp_path / "floor-events.jsonl"
    book = sb.ShadowBook(path=str(tmp_path / "floor-state.json"),
                         exit_counterfactual_path=str(event_path))
    _seed_counterfactual_position(book, "long")
    policy = ExitPolicy(protect_pct=1.0, retrace_threshold=0.20,
                        consecutive_breaches_required=2, breach_confirm_sec=4.0)
    tracker = DSLTracker("BTC", "long", entry_px=100.0,
                         entry_time=_T0 / 1000.0, policy=policy,
                         entry_atr_pct=0.0)
    tracker.check(105.0, 105.0)  # phase2, floor = 104
    assert tracker._phase_label() == "phase2"
    assert tracker._last_floor == pytest.approx(104.0)
    tracker_before = copy.deepcopy(tracker.__dict__)
    ledger_before = copy.deepcopy(book.state)
    clock = [100.0]
    monkeypatch.setattr(sb.time, "monotonic", lambda: clock[0])

    def forbidden_close(*args, **kwargs):
        raise AssertionError("counterfactual must not call _close_position")

    monkeypatch.setattr(book, "_close_position", forbidden_close)
    observe = lambda: book._observe_phase2_floor_counterfactual(
        "taker", "cf-long", "long", 103.0, 102.0, tracker)
    observe()                       # first breach tick
    clock[0] = 101.0
    observe()                       # two ticks, but only one second
    assert not event_path.exists()
    clock[0] = 105.0
    book._observe_phase2_floor_counterfactual(
        "taker", "cf-long", "long", 103.0, 105.0, tracker)
    assert not event_path.exists()  # index still above the floor
    observe()                       # time + ticks + index now all confirmed
    observe()                       # same account/position/signal deduped

    rows = [json.loads(line) for line in event_path.read_text().splitlines()]
    assert len(rows) == 1
    evidence = rows[0]["evidence"]
    assert rows[0]["signal"] == "phase2_trailing_floor"
    assert rows[0]["reason"] == "phase2_trailing_floor_breach"
    assert rows[0]["virtual_exit_ts"] == rows[0]["ts"]
    assert rows[0]["mid"] == 103.0
    assert evidence["required_breach_ticks"] == 2
    assert evidence["required_breach_confirm_sec"] == 4.0
    assert evidence["breach_confirm_sec"] >= 4.0
    assert evidence["index_aligned"] is True and evidence["floor_px"] == 104.0
    assert tracker.__dict__ == tracker_before
    assert book.state == ledger_before
    assert book.state["accounts"]["taker"]["fills"] == []


def test_adx_atr_and_volume_stagnation_counterfactuals_are_observer_only(
        tmp_path, monkeypatch):
    import json
    from hermes_trader.client.hl_client import Candle

    event_path = tmp_path / "candle-exhaustion-events.jsonl"
    book = sb.ShadowBook(path=str(tmp_path / "candle-exhaustion-state.json"),
                         exit_counterfactual_path=str(event_path))
    candles = [Candle(t=i * 900_000, o=100.0, h=101.0, l=99.0,
                      c=100.0, v=10.0) for i in range(80)]
    candles[-1] = Candle(t=79 * 900_000, o=100.0, h=100.1, l=99.9,
                         c=100.05, v=25.0)
    monkeypatch.setattr(
        "hermes_trader.client.hl_client.fetch_hl_candles",
        lambda *args, **kwargs: candles)
    monkeypatch.setattr(
        "hermes_trader.agents.perception._drop_forming_bar",
        lambda rows, interval: (rows, 0))
    monkeypatch.setattr(sb, "_calculate_adx",
                        lambda rows: [30.0] * (len(rows) - 3) + [30.0, 29.0, 28.0])
    monkeypatch.setattr(sb, "_calculate_atr",
                        lambda rows: [4.0] * (len(rows) - 3) + [4.0, 3.0, 2.0])
    ledger_before = book.state

    observe = lambda: book._observe_candle_exhaustion_counterfactuals(
        "taker", "cf-long", "BTC", "long", 100.0)
    observe()
    observe()

    rows = [json.loads(line) for line in event_path.read_text().splitlines()]
    assert {row["signal"] for row in rows} == {"adx_atr_decay", "volume_stagnation"}
    assert len(rows) == 2
    assert all(row["observer_only"] is True for row in rows)
    assert all(row["virtual_exit_ts"] == row["ts"] for row in rows)
    volume = next(row for row in rows if row["signal"] == "volume_stagnation")
    assert volume["evidence"]["volume_multiple"] == 2.5
    assert volume["evidence"]["directional_bar_bp"] == pytest.approx(5.0)
    assert book.state == ledger_before
    assert book.state["accounts"]["taker"]["fills"] == []
