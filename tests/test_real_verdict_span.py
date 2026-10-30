"""Regression: real_verdict_eval aligns records across a long backfill span.

Earlier the bridge fetched only earliest_ts .. earliest_ts+(horizon+5)bars, which
dropped/misaligned records spread over a multi-month window. Now it spans
min..max+horizon. Here we monkeypatch the candle fetcher and verify every record
aligns to its own timestamp and yields a distinct forward return.
"""
import importlib.util, os

_spec = importlib.util.spec_from_file_location(
    "rv",
    os.path.join(os.path.dirname(__file__), "..", "scripts", "real_verdict_eval.py"))
rv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rv)


class _C:
    def __init__(self, t, c):
        self.t = t; self.c = c


def test_forward_returns_spans_full_window(monkeypatch):
    BAR = 300000
    # two records 30 days apart
    import datetime as _dt
    base = _dt.datetime(2026, 9, 1, tzinfo=_dt.timezone.utc)
    def ts(d, b=0):
        return int((base + _dt.timedelta(days=d, minutes=5 * b)).timestamp() * 1000)
    recs = [
        {"timestamp": rv.datetime.fromtimestamp(ts(0) / 1000, tz=rv.timezone.utc)
                     .strftime("%Y-%m-%dT%H:%M:%SZ"),
         "would": "block", "rule": "mover_bypass_veto", "coin": "BTC",
         "detail": {"entry_px": 100.0}},
        {"timestamp": rv.datetime.fromtimestamp(ts(30) / 1000, tz=rv.timezone.utc)
                     .strftime("%Y-%m-%dT%H:%M:%SZ"),
         "would": "block", "rule": "mover_bypass_veto", "coin": "BTC",
         "detail": {"entry_px": 100.0}},
    ]

    def fake_range(coin, interval, t0, t1):
        # candles every 5m across the requested range, price rises linearly
        n = (t1 - t0) // BAR + 1
        return [_C(int(t0 + i * BAR), 100.0 + i) for i in range(int(n))]

    monkeypatch.setattr("hermes_trader.data.binance_candles.to_binance_symbol",
                        lambda c: "BTCUSDT")
    monkeypatch.setattr("hermes_trader.data.binance_candles.fetch_candle_range",
                        fake_range)
    rets = rv.forward_returns(recs, 48)
    # BOTH records align (no drop): pre-fix the later record was dropped because
    # the fetch window ended ~5h after the earliest timestamp.
    assert len(rets) == 2, rets
    # with constant entry=100 and rising candles, the later record aligns to a much
    # higher forward price; its gross move must reflect its own timestamp's index,
    # i.e. rets[1] >> rets[0] (pre-fix it would have been absent/misaligned).
    assert rets[1] > rets[0] > 0
