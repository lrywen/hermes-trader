"""O-2: signal content-level dedup (signal_fingerprint + bar_close_ms).

The trading loop itself is a top-level `while True:` script (not importable),
so the dedup GATE lives in the loop while its pure logic — the fingerprint —
lives in perception.py and is unit-tested here. The tests pin:

  * the fingerprint identity rules (same bar + same triggers => same setup;
    new bar or new trigger mix => different setup; inert without bar_close_ms);
  * the perception payload carrying a correct bar_close_ms (= open t of the
    last CLOSED bar + bar duration), which is the stable bar identity the
    fingerprint keys on (unlike fired_at / the random perception id).
"""

from hermes_trader.agents import perception
from hermes_trader.models.types import Candle

# ── fingerprint pure function ───────────────────────────────────────────────

def _percp(coin="BTC", bar_close_ms=1_000_000, triggers=None, fired_triggers=None):
    """Build a perception-shaped dict. `triggers` uses the raw hits shape;
    `fired_triggers` uses the normalized analysis shape."""
    p: dict = {"coin": coin, "bar_close_ms": bar_close_ms}
    if triggers is not None:
        p["triggers"] = triggers
    if fired_triggers is not None:
        p["fired_triggers"] = fired_triggers
    return p


def _hits(names):
    return [{"name": n, "fired": True} for n in names]


def test_fingerprint_same_bar_same_triggers_matches():
    # Different scan cycles (different random ids) but the SAME closed bar and
    # SAME fired triggers => the same setup, fingerprints must be equal.
    a = _percp(triggers=_hits(["breakout", "whaleAccumulation"]))
    a["id"] = "BTC-111-aaa"
    b = _percp(triggers=_hits(["breakout", "whaleAccumulation"]))
    b["id"] = "BTC-222-bbb"  # re-scanned later, fresh random id
    assert perception.signal_fingerprint(a) == perception.signal_fingerprint(b)


def test_fingerprint_trigger_order_independent():
    # extract_fired_triggers is order-preserving; the fingerprint SORTS, so two
    # scans that surface the same triggers in a different order still match.
    a = _percp(triggers=_hits(["zebra", "alpha"]))
    b = _percp(triggers=_hits(["alpha", "zebra"]))
    assert perception.signal_fingerprint(a) == perception.signal_fingerprint(b)


def test_fingerprint_new_bar_differs():
    # A new closed bar => new bar_close_ms => a genuinely new signal.
    a = _percp(bar_close_ms=1_000_000, triggers=_hits(["breakout"]))
    b = _percp(bar_close_ms=1_300_000, triggers=_hits(["breakout"]))  # +5m
    assert perception.signal_fingerprint(a) != perception.signal_fingerprint(b)


def test_fingerprint_new_trigger_mix_differs():
    # Same bar but a materially different trigger combination => new setup.
    a = _percp(triggers=_hits(["breakout"]))
    b = _percp(triggers=_hits(["breakout", "whaleAccumulation"]))
    assert perception.signal_fingerprint(a) != perception.signal_fingerprint(b)


def test_fingerprint_different_coin_differs():
    a = _percp(coin="BTC", triggers=_hits(["breakout"]))
    b = _percp(coin="ETH", triggers=_hits(["breakout"]))
    assert perception.signal_fingerprint(a) != perception.signal_fingerprint(b)


def test_fingerprint_none_without_bar_close_ms():
    # An older perception payload lacking bar_close_ms => None (dedup inert,
    # never crashes the loop).
    old = {"coin": "BTC", "triggers": _hits(["breakout"])}
    assert perception.signal_fingerprint(old) is None
    assert perception.signal_fingerprint(None) is None
    assert perception.signal_fingerprint("not-a-dict") is None


def test_fingerprint_tolerates_normalized_analysis_shape():
    # Executor-side analysis payloads carry fired_triggers (no raw triggers).
    p = _percp(fired_triggers=["breakout", "whaleAccumulation"])
    fp = perception.signal_fingerprint(p)
    assert fp is not None
    assert fp[2] == ("breakout", "whaleAccumulation")  # sorted tuple


# ── perception carries a correct bar_close_ms ───────────────────────────────

def _mk_candle(t, o, h, l, c, v):
    return Candle(t=t, o=o, h=h, l=l, c=c, v=v)


def _trend_candles(n, start=100.0, step=-0.12, vol=1000.0, bar_ms=300_000):
    """Steadily trending candles (fires momentum triggers). t is the bar OPEN
    time, spaced bar_ms apart; all bars are long-closed (t far in the past)."""
    out = []
    for i in range(n):
        c = start + i * step
        out.append(_mk_candle(i * bar_ms, c, c + 0.8, c - 0.2, c, vol + i))
    return out


def _patch_fetch(monkeypatch, candles_5m, candles_1h):
    monkeypatch.setattr(
        perception, "_fetch_candles_sync",
        lambda coin, interval, count, ttl, **kwargs:
        candles_5m if interval == "5m" else candles_1h)


def test_perception_carries_bar_close_ms(monkeypatch):
    """A surfaced perception must carry bar_close_ms = open-t of the last
    CLOSED bar + bar duration (300_000ms for 5m). All fixture candles are
    long-closed (t near epoch), so none is treated as a forming bar."""
    from hermes_trader.agents.config import get_config
    down_5m = _trend_candles(120)
    trend_1h = _trend_candles(48, start=100.0, step=-0.5, bar_ms=3_600_000)
    _patch_fetch(monkeypatch, down_5m, trend_1h)

    cfg = get_config()
    market = {"coin": "TRX", "type": "perp", "dex": None}
    gate = cfg["scan"]["minCompositeScore"]

    ok, res = perception._scan_single_market(
        market, 100.0, cfg, gate, None, False, trend_surface_enabled=True)
    assert ok and isinstance(res, dict), f"expected a surfaced perception, got {res}"

    last_closed_open_t = down_5m[-1].t
    assert res["bar_close_ms"] == last_closed_open_t + 300_000
    # And it must be a STABLE identity across re-scans of the same candles
    # (unlike fired_at / id which change every scan).
    ok2, res2 = perception._scan_single_market(
        market, 100.0, cfg, gate, None, False, trend_surface_enabled=True)
    assert ok2 and isinstance(res2, dict)
    assert res2["bar_close_ms"] == res["bar_close_ms"]
    assert res2["id"] != res["id"]  # random id differs — proving why id can't key dedup
