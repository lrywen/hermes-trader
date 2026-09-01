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


def test_parallel_prefetch_signals_timeout_fails_open(monkeypatch):
    """supplemental audit 2026-08-31: the positioning ``signals`` block is a
    free, non-essential hint. If it trips its outer per-source timeout (a cold
    hip3 ticker with a hung GEX/FINRA source), research must DEGRADE FAIL-OPEN
    to the "none flagged" placeholder and still return candles/funding/news —
    it must NOT raise and abort the whole coin's research."""
    monkeypatch.setattr(research, "fetch_hl_candles",
                        lambda coin, tf, n: [{"t": i, "c": 1.0} for i in range(3)])
    monkeypatch.setattr(research, "_fetch_funding_rate", lambda coin: "0.01%")

    release = threading.Event()

    def _hung_signals(coin):
        release.wait(timeout=30.0)
        return "signals: ok"

    monkeypatch.setattr(research, "_signals_block", _hung_signals)
    monkeypatch.setenv("HERMES_RESEARCH_FETCH_TIMEOUT_SIGNALS", "1")

    t0 = time.monotonic()
    out = research._parallel_prefetch("SHAZ:XYZ", skip_news_flag=True)
    elapsed = time.monotonic() - t0
    release.set()  # release the stuck worker thread

    # Bailed on the signals timeout without aborting research.
    assert elapsed < 10.0, f"signals fail-open did not return fast enough ({elapsed:.1f}s)"
    assert set(out.keys()) == {"c1h", "c4h", "c1d", "funding_raw",
                               "news", "signals_block"}
    # Required sources are intact; only the free hint degraded.
    assert out["funding_raw"] == "0.01%"
    assert "none flagged" in out["signals_block"]


def test_parallel_prefetch_signals_exception_fails_open(monkeypatch):
    """A signals block that RAISES (not just hangs) also degrades fail-open and
    does not abort research."""
    monkeypatch.setattr(research, "fetch_hl_candles",
                        lambda coin, tf, n: [{"t": i, "c": 1.0} for i in range(3)])
    monkeypatch.setattr(research, "_fetch_funding_rate", lambda coin: "0.01%")

    def _boom_signals(coin):
        raise RuntimeError("FINRA api exploded")

    monkeypatch.setattr(research, "_signals_block", _boom_signals)

    out = research._parallel_prefetch("SOXL:XYZ", skip_news_flag=True)
    assert out["funding_raw"] == "0.01%"
    assert "none flagged" in out["signals_block"]
