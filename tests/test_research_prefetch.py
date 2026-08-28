"""P1-7: the parallel research prefetch bounds each source with its OWN outer
timeout instead of one flat 45s ceiling for every future. A hung funding/news
call must bail in seconds (and name the stuck source), not burn the whole
research window and trip the 600s daemon watchdog.
"""
from __future__ import annotations

import threading
import time

import pytest

from hermes_trader.agents import research


def test_parallel_prefetch_per_source_timeout(monkeypatch):
    """A hung funding future trips the per-source funding timeout (~1s) and
    raises RuntimeError naming ``funding`` — it must NOT wait for the 45s
    fallback ceiling."""
    # Fast, deterministic stand-ins for the sources we don't care about.
    monkeypatch.setattr(research, "fetch_hl_candles",
                        lambda coin, tf, n: [{"t": i, "c": 1.0} for i in range(3)])
    monkeypatch.setattr(research, "_signals_block",
                        lambda coin: "signals: ok")

    # Funding hangs (simulates a socket read that never returns). An Event lets
    # the test release the worker thread on teardown so the shared pool thread
    # is not occupied for the full sleep after the assertion.
    release = threading.Event()

    def _hung_funding(coin):
        release.wait(timeout=30.0)
        return "N/A"

    monkeypatch.setattr(research, "_fetch_funding_rate", _hung_funding)
    monkeypatch.setenv("HERMES_RESEARCH_FETCH_TIMEOUT_FUNDING", "1")

    t0 = time.monotonic()
    with pytest.raises(RuntimeError) as exc_info:
        research._parallel_prefetch("BTC", skip_news_flag=True)
    elapsed = time.monotonic() - t0
    release.set()  # let the stuck worker thread return

    # Bailed on the funding timeout, not the 45s fallback.
    assert elapsed < 10.0, f"per-source timeout did not fire fast enough ({elapsed:.1f}s)"
    assert "funding" in str(exc_info.value)


def test_parallel_prefetch_returns_all_sources_when_fast(monkeypatch):
    """When every source returns promptly, the per-source timeouts do not
    interfere — all six keys come back populated."""
    monkeypatch.setattr(research, "fetch_hl_candles",
                        lambda coin, tf, n: [{"t": i, "c": 1.0} for i in range(3)])
    monkeypatch.setattr(research, "_signals_block", lambda coin: "signals: ok")
    monkeypatch.setattr(research, "_fetch_funding_rate", lambda coin: "0.01%")

    out = research._parallel_prefetch("BTC", skip_news_flag=True)
    assert set(out.keys()) == {"c1h", "c4h", "c1d", "funding_raw",
                               "news", "signals_block"}
    assert out["funding_raw"] == "0.01%"
    # skip_news_flag=True → news is the local HTA-boundary placeholder.
    assert "HTA" in out["news"]
