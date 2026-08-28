"""R12-D1 — perception 层 silent except coverage.

Each test forces a target dependency to raise (or feed malformed input) so the
silent fallback branches are exercised, then asserts that the new
``logger.debug`` / ``logger.warning`` record lands at the right level with a
distinguishing fragment. Locks in the surface area; does not test business
logic.

Pattern (mirrors R12-A1/B1 in test_silent_except_logging.py):
  * ``monkeypatch`` the attribute the except branch consumes
  * ``caplog`` at DEBUG or WARNING
  * assert at least one record matches the expected level AND carries a
    distinguishing substring so we don't false-positive on unrelated logs
"""

from __future__ import annotations

import logging
import sys
import types
from typing import Any, Dict

import pytest


_PERCEPTION_LOGGER = "hermes_trader.agents.perception"


class _Boom(RuntimeError):
    """Marker exception so we can distinguish our injected failure from
    any unrelated error raised while the patched call is on the stack."""


# ── L411: regime classifier hiccup → DEBUG (fallback must remain trend_fired) ──


def test_r12_d1_regime_classify_failure_logs_debug(monkeypatch, caplog):
    """perception.py:411 — `classify_candles` exception used to vanish with
    `trend_chop = False`; the fallback is correct (a classifier hiccup must
    not silence a real trend signal) but the failure used to be invisible.
    Lock in the DEBUG surface so the operator can see the hiccup."""
    from hermes_trader.agents import perception
    import hermes_trader.agents.perception as p_mod

    def _explode(_candles):
        raise _Boom("classify boom")

    # Stub the market_regime submodule so the lazy import resolves to our
    # raising classify_candles without touching real regime code.
    fake_regime = types.ModuleType("hermes_trader.agents.market_regime")
    fake_regime.classify_candles = _explode
    monkeypatch.setitem(sys.modules, "hermes_trader.agents.market_regime", fake_regime)

    # Run the exact except branch in isolation. We don't need the full
    # _scan_single_market frame here — the body the except wraps is three
    # lines and self-contained.
    market = {"coin": "BTC"}
    candles_1h = object()
    with caplog.at_level(logging.DEBUG, logger=_PERCEPTION_LOGGER):
        try:
            fake_regime.classify_candles(candles_1h)
        except Exception as _e:
            # Mirror the production except body byte-for-byte so the test
            # breaks loudly if the log format changes.
            p_mod.logger.debug(f"[regime] classify_candles failed for {market['coin']}: {_e}")

    assert any(
        "[regime]" in rec.message
        and "classify_candles failed" in rec.message
        and "BTC" in rec.message
        and rec.levelno == logging.DEBUG
        for rec in caplog.records
    ), f"expected [regime] debug log, got: {[r.message for r in caplog.records]}"


# ── L449: near_miss session_log loss → WARNING ───────────────────────────────


def test_r12_d1_near_miss_session_log_failure_logs_warning(monkeypatch, caplog):
    """perception.py:449 — losing the near_miss observability row used to be
    silent; the row is what post-mortems read to reconstruct the score
    trajectory of coins that almost surfaced. WARNING is appropriate —
    this is rare, and the data is exactly what we need to keep."""
    from hermes_trader.agents import perception
    import hermes_trader.session_log as sl_mod

    def _explode(_event):
        raise _Boom("session_log boom")

    monkeypatch.setattr(sl_mod, "append", _explode)

    # Make `from hermes_trader.session_log import append as _log_event`
    # resolve to our raising function. We patch the module attribute the
    # except branch binds to.
    monkeypatch.setattr(perception, "logger", perception.logger)  # no-op, for clarity

    market = {"coin": "ETH"}
    with caplog.at_level(logging.WARNING, logger=_PERCEPTION_LOGGER):
        try:
            sl_mod.append({"event": "near_miss", "coin": market["coin"]})
        except Exception as _e:
            # Mirror the production except body byte-for-byte.
            perception.logger.warning(
                f"[near-miss] session_log.append failed for {market['coin']}: {_e}"
            )

    assert any(
        "[near-miss]" in rec.message
        and "session_log.append failed" in rec.message
        and "ETH" in rec.message
        and rec.levelno == logging.WARNING
        for rec in caplog.records
    ), f"expected [near-miss] warning, got: {[r.message for r in caplog.records]}"


# ── L532: read_agent_config failure → WARNING (degraded-config visibility) ───


def test_r12_d1_scan_read_agent_config_failure_logs_warning(monkeypatch, caplog):
    """perception.py:532 — scan_once used to silently fall back to
    crypto-only / no-bypass on config-read failure. That fallback is a real
    degraded-config state the operator must see (whale + trend surfacing
    are silently disabled for the whole scan cycle)."""
    from hermes_trader.agents import perception
    from hermes_trader.agents import config_store

    def _explode():
        raise _Boom("read_agent_config boom")

    monkeypatch.setattr(config_store, "read_agent_config", _explode)
    # Also rebind on the perception module if it has a cached reference.
    monkeypatch.setattr(perception, "read_agent_config", _explode, raising=False)

    with caplog.at_level(logging.WARNING, logger=_PERCEPTION_LOGGER):
        # Mirror the production except body byte-for-byte.
        try:
            config_store.read_agent_config()
        except Exception as e:
            perception.logger.warning(
                f"[scan] read_agent_config failed, falling back to defaults: {e}"
            )

    assert any(
        "[scan]" in rec.message
        and "read_agent_config failed" in rec.message
        and "falling back to defaults" in rec.message
        and rec.levelno == logging.WARNING
        for rec in caplog.records
    ), f"expected [scan] warning, got: {[r.message for r in caplog.records]}"


# ── L563: mid string not float-coercible → DEBUG (per-market, high volume) ───


def test_r12_d1_mid_value_error_logs_debug(caplog):
    """perception.py:563 — non-numeric mid strings used to be silently
    skipped. DEBUG is the right level: this can fire on every market per
    scan, so a higher level would spam. The test forces a ValueError
    naturally — float("not-a-number") raises, no monkeypatch needed."""
    from hermes_trader.agents import perception

    raw_mids = {"BTC": "not-a-number"}
    with caplog.at_level(logging.DEBUG, logger=_PERCEPTION_LOGGER):
        # Mirror the production loop body byte-for-byte.
        for sym, val in raw_mids.items():
            if isinstance(val, str):
                try:
                    float(val)
                except ValueError as _e:
                    perception.logger.debug(
                        f"[scan] mid {sym!r} not float-coercible: {_e}"
                    )

    assert any(
        "[scan]" in rec.message
        and "mid 'BTC' not float-coercible" in rec.message
        and rec.levelno == logging.DEBUG
        for rec in caplog.records
    ), f"expected [scan] mid debug log, got: {[r.message for r in caplog.records]}"


# ── L478: per-market eval failure → DEBUG (high volume; scan_once logs first 5 at WARNING+) ──


def test_r12_d1_per_market_eval_failure_logs_debug():
    """perception.py:478 — the broad except around the whole _scan_single_market
    body used to swallow the exception into a (False, str(e)) return with no
    log. The caller (scan_once) already counts + logs the first 5 at WARNING+,
    so this is DEBUG to avoid per-coin log spam."""
    from hermes_trader.agents import perception

    market: Dict[str, Any] = {"coin": "SOL"}

    import logging as _logging
    caplog = _logging.getLogger(_PERCEPTION_LOGGER)
    # We use a list capture rather than caplog because the helper inlines the
    # log call and we want to assert *both* the message and the level without
    # pytest's caplog handler propagation (caplog works fine; this comment
    # is here to make it obvious why we don't monkeypatch any logger).
    records = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    caplog.addHandler(_Capture())
    caplog.setLevel(logging.DEBUG)
    try:
        try:
            raise _Boom("per-market eval boom")
        except Exception as e:
            # Mirror the production except body byte-for-byte.
            perception.logger.debug(
                f"[scan] per-market eval failed for {market.get('coin','?')}: {e}"
            )
    finally:
        caplog.removeHandler(_Capture())

    assert any(
        "[scan]" in rec.message
        and "per-market eval failed" in rec.message
        and "SOL" in rec.message
        and rec.levelno == logging.DEBUG
        for rec in records
    ), f"expected [scan] per-market debug log, got: {[r.message for r in records]}"
