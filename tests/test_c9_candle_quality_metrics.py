"""C9 — fetch-side candle quality observability.

The fetch-side quality gate (``_fetch_hl_candles_raw``) previously only logged
when a candleSnapshot came back gappy / stale / truncated or when individual
bars were dropped while parsing. C9 adds best-effort Prometheus metrics so a
sustained feed degradation (429 storm, upstream outage) is alertable:

* ``hermes_candle_quality_issues_total{interval,issue}`` — one increment per
  bounded quality-gate issue found on a cold HTTP fetch;
* ``hermes_candle_parse_dropped_total{interval,cause}`` — raw bars dropped
  while parsing (malformed / non_finite / out_of_order);
* ``hermes_candle_closed_bar_age_seconds{interval}`` — newest closed-bar age
  (feed-lag early warning).

Labels are bounded enums (no coin, no free text). Metrics never raise into the
fetch path.
"""

from __future__ import annotations

import math
import time

from prometheus_client import REGISTRY


_MS_4H = 14_400_000


def _raw_bar(t_ms: int, price: float = 100.0) -> dict:
    return {
        "t": int(t_ms),
        "o": str(price),
        "h": str(price + 1.0),
        "l": str(price - 1.0),
        "c": str(price + 0.5),
        "v": "1000",
    }


def _gappy_raw_payload(n: int = 40, *, gap_at: int = 12) -> list[dict]:
    """n bars on the 4h grid ending in the currently-forming slot, with one
    bar removed to produce a gap, plus one malformed and one non-finite bar."""
    now = time.time() * 1000
    last_open = int(now // _MS_4H) * _MS_4H
    ts = [last_open - (n - 1 - i) * _MS_4H for i in range(n)]
    ts = [t for j, t in enumerate(ts) if j != gap_at]
    bars = [_raw_bar(t) for t in ts]
    # malformed bar: missing the "o" field → dropped as malformed
    bars.append({"t": last_open + _MS_4H, "h": "1", "l": "1", "c": "1", "v": "1"})
    # non-finite bar: NaN close → dropped as non_finite
    bad = _raw_bar(last_open + 2 * _MS_4H)
    bad["c"] = "NaN"
    bars.append(bad)
    return bars


def _sample(name: str, **labels) -> float:
    return float(REGISTRY.get_sample_value(name, labels) or 0.0)


def test_cold_fetch_emits_quality_and_parse_metrics(monkeypatch):
    from hermes_trader.client import hl_client

    payload = _gappy_raw_payload()
    monkeypatch.setattr(hl_client, "_http_post",
                        lambda *a, **k: list(payload))
    # Keep the cache out of the way (the gate-failed series is not cached
    # anyway; this also isolates the test from any shared cache state).
    monkeypatch.setattr(hl_client, "_CANDLE_CACHE", None)

    issues_before = _sample(
        "hermes_candle_quality_issues_total", interval="4h", issue="gaps")
    malformed_before = _sample(
        "hermes_candle_parse_dropped_total", interval="4h", cause="malformed")
    nonfinite_before = _sample(
        "hermes_candle_parse_dropped_total", interval="4h", cause="non_finite")

    candles = hl_client._fetch_hl_candles_raw(
        "BTC", "4h", 40, "c9-test-key", opportunistic=False)

    # The gappy series is still returned (callers fail-closed on the gate via
    # assess_candle_quality / get_hl_atr → 0.0), malformed/NaN bars dropped.
    assert candles, "valid bars should still be returned to the caller"
    assert all(math.isfinite(c.c) for c in candles)

    assert _sample("hermes_candle_quality_issues_total",
                   interval="4h", issue="gaps") == issues_before + 1
    assert _sample("hermes_candle_parse_dropped_total",
                   interval="4h", cause="malformed") == malformed_before + 1
    assert _sample("hermes_candle_parse_dropped_total",
                   interval="4h", cause="non_finite") == nonfinite_before + 1

    age = _sample("hermes_candle_closed_bar_age_seconds", interval="4h")
    assert age >= 0.0
    # newest closed bar opened at most ~one 4h slot ago (the forming bar is
    # excluded), so the age gauge must stay well under the 2-bar stale limit.
    assert age < 2 * _MS_4H / 1000.0


def test_quality_metric_helper_never_raises(monkeypatch):
    """A metrics backend failure must never propagate to the fetch path."""
    from hermes_trader.client import hl_client
    from hermes_trader import metrics

    def _boom(*_a, **_kw):
        raise RuntimeError("metrics backend down")

    monkeypatch.setattr(metrics.CANDLE_QUALITY_ISSUES, "labels", _boom)
    # Must return silently even though the counter raises.
    hl_client._candle_quality_metric(
        "4h", {"issues": ["gaps"], "age_ms": 1000},
        {"malformed": 1, "non_finite": 0, "out_of_order": 0})


def test_unknown_issue_normalises_to_other(monkeypatch):
    """Free-text / future issue names must not become unbounded labels."""
    from hermes_trader.client import hl_client

    before = _sample("hermes_candle_quality_issues_total",
                     interval="4h", issue="other")
    hl_client._candle_quality_metric(
        "4h", {"issues": ["some_future_issue"], "age_ms": -1},
        {"malformed": 0, "non_finite": 0, "out_of_order": 0})
    assert _sample("hermes_candle_quality_issues_total",
                   interval="4h", issue="other") == before + 1
