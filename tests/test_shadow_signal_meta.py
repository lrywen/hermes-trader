"""P0-A instrumentation: point-in-time trigger labels pinned on open fills.

Real shadow fills must carry the fired trigger names / composite / AI
confidence so future real fills can be sliced by *which trigger fired* ->
realized ROE. Historical fills predate this and carry none; missing/malformed
meta must never break bookkeeping.
"""
from __future__ import annotations

import time

from hermes_trader.agents import shadow_book as sb

_T0 = int(time.time() // 60 * 60) * 1000


def _patch_cfg(monkeypatch):
    cfg = {"enabled": True, "maker_shadow_enabled": True,
           "starting_balance": 10000.0, "taker_fee_pct": 0.025,
           "maker_fee_pct": 0.01, "maker_limit_offset_bps": 5.0,
           "maker_ttl_bars": 30}
    monkeypatch.setattr(sb, "_shadow_cfg", lambda: dict(cfg))


# --------------------------------------------------------------------- sanitize

def test_sanitize_keeps_clean_fields():
    out = sb._sanitize_signal_meta({
        "fired_names": ["breakout", "uptrendMomentum", "breakout", ""],
        "composite_score": 58.2, "ai_confidence": "0.66",
        "trigger_score": 55, "ignored_extra": {"x": 1}})
    assert out["fired_names"] == ["breakout", "uptrendMomentum"]  # dedup+sorted
    assert out["composite_score"] == 58.2
    assert out["ai_confidence"] == 0.66
    assert out["trigger_score"] == 55.0
    assert "ignored_extra" not in out


def test_sanitize_drops_garbage():
    assert sb._sanitize_signal_meta(None) == {}
    assert sb._sanitize_signal_meta("nope") == {}
    out = sb._sanitize_signal_meta({
        "fired_names": ["ok"], "composite_score": float("nan"),
        "ai_confidence": float("inf"), "trigger_score": "abc"})
    assert out["fired_names"] == ["ok"]
    assert "composite_score" not in out
    assert "ai_confidence" not in out
    assert "trigger_score" not in out


# ------------------------------------------------------- sanitize: fresh_src

def test_sanitize_keeps_fresh_src():
    # P1: runner-gate fresh-impulse provenance rides the open fill.
    for src in ("brk", "recent_brk", "vol_burst", "burst_score"):
        assert sb._sanitize_signal_meta({"fresh_src": src}) \
            == {"fresh_src": src}


def test_sanitize_drops_blank_or_nonstring_fresh_src():
    # A blank / non-str value must vanish rather than persist an empty key.
    for raw in ("", "   ", None, 7, ["brk"], {"x": 1}):
        assert "fresh_src" not in sb._sanitize_signal_meta({"fresh_src": raw})


def test_sanitize_caps_fresh_src_length():
    out = sb._sanitize_signal_meta({"fresh_src": "x" * 80})
    assert len(out["fresh_src"]) == sb._SIGNAL_META_STR_MAX


# ----------------------------------------------------------------- taker fill

def test_taker_open_carries_signal_meta(tmp_path, monkeypatch):
    _patch_cfg(monkeypatch)
    book = sb.ShadowBook(path=str(tmp_path / "s.json"))
    fill = book.shadow_open(
        coin="BTC", side="long", entry_px=100.0, size_usd=1000.0, leverage=1,
        analysis_id="a1",
        signal_meta={"fired_names": ["breakout"], "composite_score": 58.0,
                     "ai_confidence": 0.7, "fresh_src": "brk"})
    assert fill["fired_names"] == ["breakout"]
    assert fill["composite_score"] == 58.0
    assert fill["ai_confidence"] == 0.7
    assert fill["fresh_src"] == "brk"
    # persisted onto the open position too
    pos = book.state["accounts"]["taker"]["positions"][0]
    assert pos["fired_names"] == ["breakout"]
    assert pos["fresh_src"] == "brk"


def test_taker_open_without_meta_back_compat(tmp_path, monkeypatch):
    _patch_cfg(monkeypatch)
    book = sb.ShadowBook(path=str(tmp_path / "s.json"))
    fill = book.shadow_open(coin="BTC", side="long", entry_px=100.0,
                            size_usd=1000.0, leverage=1, analysis_id="a1")
    assert "fired_names" not in fill
    assert "composite_score" not in fill


# ----------------------------------------------------------------- maker chain

def test_maker_resting_order_and_fill_carry_meta(tmp_path, monkeypatch):
    _patch_cfg(monkeypatch)
    book = sb.ShadowBook(path=str(tmp_path / "s.json"))
    book.shadow_open(
        coin="ETH", side="long", entry_px=200.0, size_usd=1000.0, leverage=1,
        analysis_id="a9",
        signal_meta={"fired_names": ["momentumBurst", "uptrendMomentum"],
                     "composite_score": 61.0, "ai_confidence": 0.72,
                     "fresh_src": "burst_score"})
    order = book.state["accounts"]["maker_shadow"]["pending_orders"][0]
    # meta rides the resting order so the async fill inherits it
    assert order["signal_meta"]["fired_names"] == \
        ["momentumBurst", "uptrendMomentum"]
    assert order["signal_meta"]["fresh_src"] == "burst_score"
    fill = book._fill_maker_order(order, fill_px=199.9, filled_at=_T0)
    assert fill["fired_names"] == ["momentumBurst", "uptrendMomentum"]
    assert fill["composite_score"] == 61.0
    assert fill["ai_confidence"] == 0.72
    assert fill["fresh_src"] == "burst_score"
    assert fill["fill_model"] == "maker_shadow"
    pos = book.state["accounts"]["maker_shadow"]["positions"][0]
    assert pos["fired_names"] == ["momentumBurst", "uptrendMomentum"]
    assert pos["fresh_src"] == "burst_score"


def test_meta_persists_across_reload(tmp_path, monkeypatch):
    _patch_cfg(monkeypatch)
    p = str(tmp_path / "s.json")
    book = sb.ShadowBook(path=p)
    book.shadow_open(coin="SOL", side="short", entry_px=50.0, size_usd=500.0,
                     leverage=2, analysis_id="z",
                     signal_meta={"fired_names": ["downtrendMomentum"],
                                  "ai_confidence": 0.8})
    book2 = sb.ShadowBook(path=p)
    book2.reload_if_changed()
    fill = [f for f in book2.state["accounts"]["taker"]["fills"]
            if f["type"] == "open"][0]
    assert fill["fired_names"] == ["downtrendMomentum"]
    assert fill["ai_confidence"] == 0.8
