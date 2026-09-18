"""P1-1 step ③ — characterization for _dispatch_entry_shadow_probes (S2).

The S2 extraction must actually DISPATCH both entry shadow probes; it must not
no-op or recurse into itself (a regression the default-off test config would
otherwise hide, because neither probe runs unless enabled). These tests drive
the helper with shadow_signals enabled and assert the dispatch callables are
invoked through the function-local import seam.
"""
from __future__ import annotations

import sys
import types

from hermes_trader.agents import executor


def test_dispatch_invokes_shadow_signals_when_enabled(monkeypatch):
    calls = []

    def _run_shadow_async(coin, side, cfg, *, config=None):
        calls.append(("shadow_signals", coin, side, cfg is not None, config is not None))

    fake = types.SimpleNamespace(run_shadow_async=_run_shadow_async)
    monkeypatch.setitem(sys.modules,
                        "hermes_trader.agents.shadow_signals", fake)
    # xs_reversal module must still import (or be neutralized); neutralize to
    # keep the test offline and isolated.
    monkeypatch.setitem(sys.modules, "hermes_trader.agents.xs_reversal",
                        types.SimpleNamespace(run_xs_reversal_async=lambda *a, **k: None))

    analysis = {"coin": "ETH", "side": "long"}
    config = {"shadow_signals": {"enabled": True, "news": False}}
    executor._dispatch_entry_shadow_probes(analysis, config)

    assert calls == [("shadow_signals", "ETH", "long", True, True)]


def test_dispatch_skips_shadow_signals_when_disabled(monkeypatch):
    calls = []

    def _boom(*a, **k):
        calls.append(("should-not-run",))

    fake = types.SimpleNamespace(run_shadow_async=_boom)
    monkeypatch.setitem(sys.modules,
                        "hermes_trader.agents.shadow_signals", fake)
    monkeypatch.setitem(sys.modules, "hermes_trader.agents.xs_reversal",
                        types.SimpleNamespace(run_xs_reversal_async=lambda *a, **k: None))

    executor._dispatch_entry_shadow_probes(
        {"coin": "ETH", "side": "long"}, {"shadow_signals": {"enabled": False}})
    assert calls == []


def test_dispatch_invokes_xs_reversal(monkeypatch):
    calls = []
    monkeypatch.setitem(sys.modules, "hermes_trader.agents.shadow_signals",
                        types.SimpleNamespace(run_shadow_async=lambda *a, **k: None))
    monkeypatch.setitem(sys.modules, "hermes_trader.agents.xs_reversal",
                        types.SimpleNamespace(
                            run_xs_reversal_async=lambda coin, side, *, config=None:
                                calls.append((coin, side, config is not None))))

    executor._dispatch_entry_shadow_probes(
        {"coin": "BTC", "side": "short"}, {})
    # xs_reversal self-gates internally; here the stub is always invoked once.
    assert calls == [("BTC", "short", True)]


def test_dispatch_never_raises_on_probe_error(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("probe exploded")

    monkeypatch.setitem(sys.modules, "hermes_trader.agents.shadow_signals",
                        types.SimpleNamespace(run_shadow_async=_boom))
    monkeypatch.setitem(sys.modules, "hermes_trader.agents.xs_reversal",
                        types.SimpleNamespace(run_xs_reversal_async=_boom))
    # Must swallow both failures (non-fatal) and return None.
    assert executor._dispatch_entry_shadow_probes(
        {"coin": "ETH", "side": "long"},
        {"shadow_signals": {"enabled": True}}) is None


def test_dispatch_is_not_self_recursive(monkeypatch):
    # Guard against the helper accidentally calling itself: bound a recursion
    # budget by patching the real probe targets to recorders; if the helper
    # recursed, Python would overflow / call count would be > 1 per probe.
    shadow_calls = []
    xs_calls = []
    monkeypatch.setitem(sys.modules, "hermes_trader.agents.shadow_signals",
                        types.SimpleNamespace(
                            run_shadow_async=lambda *a, **k: shadow_calls.append(1)))
    monkeypatch.setitem(sys.modules, "hermes_trader.agents.xs_reversal",
                        types.SimpleNamespace(
                            run_xs_reversal_async=lambda *a, **k: xs_calls.append(1)))
    executor._dispatch_entry_shadow_probes(
        {"coin": "ETH", "side": "long"},
        {"shadow_signals": {"enabled": True}})
    assert len(shadow_calls) == 1
    assert len(xs_calls) == 1
