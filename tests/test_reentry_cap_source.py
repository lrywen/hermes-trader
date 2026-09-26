"""reentry_cap data-source fix (2026-09-26).

In SHADOW mode openings land in the shadow book (shadow_open never writes the
live memory trade log), so the gate must count shadow-book fills; in ENFORCE
mode it counts the live memory log. Earlier the gate always read memory, so in
shadow mode the count was permanently 0 and the gate could never trigger.
"""
from hermes_trader.agents import risk_gates


def _ctx(**kw):
    base = dict(confidence=0.9, current_positions=[], trade_notional_usd=50,
                daily_pnl=0, market_volume_24h_usd=1e8, coin="BTC",
                trade_side="long", has_binary_news_risk=False, equity=1000,
                total_open_notional=0)
    base.update(kw)
    return risk_gates.GateContext(**base)


def test_shadow_mode_counts_shadow_book(monkeypatch):
    captured = {}

    class FakeBook:
        def count_openings_since(self, coin, since_ms):
            captured["coin"] = coin
            captured["since"] = since_ms
            return 2  # >= cap → would block

    import hermes_trader.agents.shadow_book as sb
    monkeypatch.setattr(sb, "get_book", lambda: FakeBook())

    cfg = {"reentry_cap": {"mode": "shadow", "max_per_coin": 2,
                           "window_hours": 24}}
    res = risk_gates.reentry_cap_gate(_ctx(), cfg)
    assert captured["coin"] == "BTC"
    # shadow still passes but reports the would-block via tag
    assert res["pass"] is True
    assert res["via"] == "reentry_cap_shadow_block"


def test_shadow_mode_below_cap_passes(monkeypatch):
    class FakeBook:
        def count_openings_since(self, coin, since_ms):
            return 1

    import hermes_trader.agents.shadow_book as sb
    monkeypatch.setattr(sb, "get_book", lambda: FakeBook())

    cfg = {"reentry_cap": {"mode": "shadow", "max_per_coin": 2,
                           "window_hours": 24}}
    res = risk_gates.reentry_cap_gate(_ctx(), cfg)
    assert res["via"] == "reentry_cap_ok"
    assert res["openings"] == 1


def test_enforce_mode_counts_memory(monkeypatch):
    class FakeMemory:
        def count_openings_since(self, coin, since_ms):
            return 2

    import hermes_trader.agents.memory as mem
    monkeypatch.setattr(mem, "memory", FakeMemory())

    cfg = {"reentry_cap": {"mode": "enforce", "max_per_coin": 2,
                           "window_hours": 24}}
    res = risk_gates.reentry_cap_gate(_ctx(), cfg)
    assert res["pass"] is False
    assert res["via"] == "reentry_cap_block"


def test_shadow_book_count_method_counts_only_taker_opens():
    from hermes_trader.agents.shadow_book import ShadowBook
    b = ShadowBook.__new__(ShadowBook)

    class _Lock:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    b._lock = _Lock()
    b.reload_if_changed = lambda: False
    b.state = {"accounts": {"taker": {"fills": [
        {"type": "open", "coin": "AAA", "ts": 1000},
        {"type": "open", "coin": "AAA", "ts": 2000},
        {"type": "close", "coin": "AAA", "ts": 2500},
        {"type": "open", "coin": "BBB", "ts": 1500},
        {"type": "open", "coin": "AAA", "ts": 500},
    ]}, "maker": {"fills": []}}}

    def _account(name):
        return b.state["accounts"][name]

    b._account = _account
    assert b.count_openings_since("AAA", 900) == 2
    assert b.count_openings_since("AAA", 0) == 3
    assert b.count_openings_since("BBB", 0) == 1
    assert b.count_openings_since("XXX", 0) == 0
