"""Offline tests for the hermes-trader codebase.

Covers the pure/refactored logic — no network or Hyperliquid credentials
required. Run from the repo root: ``pytest`` (or ``python3 -m pytest``).

Network-dependent paths (live order placement, account-state fetches, the
OpenRouter research call, the full market scan) are not covered here — they
can only be exercised against the real exchange.
"""
import math
import pathlib

import pytest

from hermes_trader.models.types import Candle

ROOT = pathlib.Path(__file__).resolve().parents[1]
MCP_SCRIPT = str(ROOT / "scripts" / "hermes-mcp-server.py")


@pytest.fixture(autouse=True)
def _clear_dsl_trackers():
    """Isolate the DSL tracker registry between tests. The re-entry backstop in
    maybe_execute now reads dsl_exit._active_positions, so a tracker leaked by an
    earlier test would inject a phantom held-coin and block unrelated trades."""
    try:
        from hermes_trader.agents import dsl_exit
        dsl_exit._active_positions.clear()
    except Exception:
        pass
    yield
    try:
        from hermes_trader.agents import dsl_exit
        dsl_exit._active_positions.clear()
    except Exception:
        pass


def _candles(n=150):
    return [
        Candle(t=i, o=100 + i * 0.1, h=101 + i * 0.1, l=99 + i * 0.1,
               c=100 + i * 0.1 + math.sin(i) * 0.5, v=1000.0 + i)
        for i in range(n)
    ]


# ── models ──────────────────────────────────────────────────────────────


def test_parse_verdict_json_camelcase():
    from hermes_trader.agents.research import parse_verdict
    txt = ('reasoning\n{"verdict":"LONG","confidence":0.8,"side":"long",'
           '"entryPx":100,"stopPx":95,"tpPx":110,"reasoning":"x"}')
    v = parse_verdict(txt, "BTC", {"mid": 50})
    assert v["verdict"] == "LONG" and v["side"] == "long"
    assert v["entry_px"] == 100 and v["stop_px"] == 95 and v["tp_px"] == 110


def test_parse_verdict_empty_defaults_to_pass():
    from hermes_trader.agents.research import parse_verdict
    v = parse_verdict("", "BTC", {"mid": 42})
    assert v["verdict"] == "PASS" and v["entry_px"] == 42


def test_fetch_news_no_key_returns_no_news(monkeypatch):
    """Without BRAVE_API_KEY, news fetch degrades to 'no news' — never raises."""
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    from hermes_trader.agents.research import _fetch_news
    assert _fetch_news("BTC") == "no news"


def test_fetch_news_sends_freshness_window(monkeypatch):
    """The Brave request must carry a freshness range so year-old articles
    (the AIXBT 2025 hack) don't feed the gate. Regression guard."""
    from hermes_trader.agents import research
    monkeypatch.setenv("BRAVE_API_KEY", "k")
    captured = {}
    class _Resp:
        is_success = True
        def json(self):
            return {"results": [{"title": "fresh headline"}]}
    def fake_get(url, params=None, headers=None, timeout=None):
        captured["params"] = params
        return _Resp()
    # P1-9: outbound calls go through the shared httpx.Client (_http()),
    # not the module-level httpx.get function.
    class _FakeClient:
        def get(self, url, **kw):
            return fake_get(url, **kw)
    monkeypatch.setattr(research, "_http", lambda: _FakeClient())
    out = research._fetch_news("AIXBT")
    assert out == "fresh headline"
    fr = captured["params"]["freshness"]
    assert "to" in fr and len(fr.split("to")) == 2  # YYYY-MM-DDtoYYYY-MM-DD


def test_parse_verdict_extracts_news_risk():
    from hermes_trader.agents.research import parse_verdict
    v = parse_verdict('{"verdict":"LONG","confidence":0.7,"newsRisk":"positive"}',
                      "BTC", {"mid": 1})
    assert v["news_risk"] == "positive"
    # snake_case + invalid values fall back to "none"
    assert parse_verdict('{"verdict":"LONG","confidence":0.7,"news_risk":"negative"}',
                         "B", {"mid": 1})["news_risk"] == "negative"
    assert parse_verdict('{"verdict":"LONG","confidence":0.7,"newsRisk":"spicy"}',
                         "B", {"mid": 1})["news_risk"] == "none"
    # absent → defaults none
    assert parse_verdict('{"verdict":"PASS","confidence":0}', "B", {"mid": 1})["news_risk"] == "none"


def test_parse_verdict_short_derives_side_short():
    """A SHORT verdict with no/null side must yield side='short', NOT fall
    through to the executor's 'long' default (wrong-direction bug)."""
    from hermes_trader.agents.research import parse_verdict
    txt = '{"verdict":"SHORT","confidence":0.7}'   # no side field
    v = parse_verdict(txt, "BTC", {"mid": 100})
    assert v["verdict"] == "SHORT"
    assert v["side"] == "short"

    # explicit null side too
    v2 = parse_verdict('{"verdict":"SHORT","confidence":0.7,"side":null}', "BTC", {"mid": 100})
    assert v2["side"] == "short"


def test_parse_verdict_long_derives_side_long():
    from hermes_trader.agents.research import parse_verdict
    v = parse_verdict('{"verdict":"LONG","confidence":0.6}', "ETH", {"mid": 50})
    assert v["side"] == "long"


def test_parse_verdict_coerces_string_confidence():
    """LLM sometimes returns confidence as a string — must coerce to float
    so the gate comparison doesn't TypeError on a live trade."""
    from hermes_trader.agents.research import parse_verdict
    v = parse_verdict('{"verdict":"LONG","confidence":"0.82","side":"long"}', "BTC", {"mid": 1})
    assert isinstance(v["confidence"], float)
    assert abs(v["confidence"] - 0.82) < 1e-9


def test_parse_verdict_clamps_confidence_range():
    from hermes_trader.agents.research import parse_verdict
    hi = parse_verdict('{"verdict":"LONG","confidence":1.8,"side":"long"}', "B", {"mid": 1})
    assert hi["confidence"] == 1.0
    lo = parse_verdict('{"verdict":"LONG","confidence":-0.5,"side":"long"}', "B", {"mid": 1})
    assert lo["confidence"] == 0.0
    junk = parse_verdict('{"verdict":"LONG","confidence":"high","side":"long"}', "B", {"mid": 1})
    assert junk["confidence"] == 0.0


def test_parse_verdict_unknown_verdict_defaults_pass():
    """HOLD or any non-LONG/SHORT/CLOSE verdict → PASS (no accidental trade)."""
    from hermes_trader.agents.research import parse_verdict
    for raw in ("HOLD", "WAIT", "MAYBE", ""):
        v = parse_verdict(f'{{"verdict":"{raw}","confidence":0.9}}', "BTC", {"mid": 1})
        assert v["verdict"] == "PASS", raw


def test_route_verdict_long_calls_execute():
    from hermes_trader.agents.executor import route_verdict
    calls = {}
    def exec_fn(a): calls["exec"] = a; return {"executed": True, "order_id": "1"}
    def close_fn(c): calls["close"] = c; return {"ok": True}
    r = route_verdict({"verdict": "LONG", "coin": "BTC", "side": "long"},
                      execute_fn=exec_fn, close_fn=close_fn)
    assert r["action"] == "execute"
    assert "exec" in calls and "close" not in calls


def test_route_verdict_short_calls_execute():
    from hermes_trader.agents.executor import route_verdict
    seen = {}
    r = route_verdict({"verdict": "SHORT", "coin": "ETH", "side": "short"},
                      execute_fn=lambda a: seen.setdefault("e", a) or {"executed": True},
                      close_fn=lambda c: seen.setdefault("c", c))
    assert r["action"] == "execute" and "e" in seen and "c" not in seen


def test_route_verdict_close_calls_close():
    """The bug that started the shakedown: CLOSE must call close_fn, not be dropped."""
    from hermes_trader.agents.executor import route_verdict
    calls = {}
    r = route_verdict({"verdict": "CLOSE", "coin": "DOGE"},
                      execute_fn=lambda a: calls.setdefault("e", a),
                      close_fn=lambda c: calls.setdefault("c", c) or {"ok": True})
    assert r["action"] == "close"
    assert calls.get("c") == "DOGE"
    assert "e" not in calls            # never executes a trade on CLOSE


def test_route_verdict_pass_is_noop():
    from hermes_trader.agents.executor import route_verdict
    calls = {}
    r = route_verdict({"verdict": "PASS", "coin": "BTC"},
                      execute_fn=lambda a: calls.setdefault("e", 1),
                      close_fn=lambda c: calls.setdefault("c", 1))
    assert r["action"] == "none"
    assert not calls                   # nothing called


def test_route_verdict_pass_with_whale_signal_routes_to_executor(monkeypatch):
    """A hedging AI PASS that carries a whale_signal must reach the executor so
    the force-execute-on-PASS override can fire — otherwise the whale path is
    dead (router dropped PASS before maybe_execute ever saw it)."""
    from hermes_trader.agents import executor
    monkeypatch.setattr(executor, "read_agent_config",
                        lambda: {"whale_force_execute": True})
    calls = {}
    r = executor.route_verdict(
        {"verdict": "PASS", "coin": "TRX",
         "whale_signal": {"signal": "oi_funding_anomaly"}},
        execute_fn=lambda a: calls.setdefault("e", a) or {"executed": True},
        close_fn=lambda c: calls.setdefault("c", c),
    )
    assert r["action"] == "execute"
    assert calls.get("e", {}).get("coin") == "TRX"
    assert "c" not in calls


def test_route_verdict_pass_with_slow_burn_hint_routes_to_executor(monkeypatch):
    from hermes_trader.agents import executor
    monkeypatch.setattr(executor, "read_agent_config", lambda: {
        "composite_force_execute": True,
        "force_execute_composite": 40,
        "force_execute_slow_burn_count": 2,
    })
    calls = {}
    r = executor.route_verdict(
        {"verdict": "PASS", "coin": "SOL",
         "composite_score": 45.0, "slow_burn_count": 2},
        execute_fn=lambda a: calls.setdefault("e", 1) or {"executed": True},
        close_fn=lambda c: calls.setdefault("c", 1),
    )
    assert r["action"] == "execute"
    assert calls.get("e") == 1


def test_route_verdict_pass_with_ta_sidestep_hint_routes_to_executor(monkeypatch):
    from hermes_trader.agents import executor
    monkeypatch.setattr(executor, "read_agent_config", lambda: {
        "ta_sidestep_force_execute": True,
        "ta_sidestep_min_slow_burn_count": 1,
        "force_execute_composite": 20,
    })
    calls = {}
    r = executor.route_verdict(
        {"verdict": "PASS", "coin": "PURR", "slow_burn_count": 1,
         "momentum_burst_fired": True, "composite_score": 0.0},
        execute_fn=lambda a: calls.setdefault("e", a) or {"executed": True},
        close_fn=lambda c: calls.setdefault("c", c),
    )
    assert r["action"] == "execute"
    assert calls.get("e", {}).get("coin") == "PURR"
    assert "c" not in calls


def test_route_verdict_ta_sidestep_respects_min_slow_burn(monkeypatch):
    from hermes_trader.agents import executor
    monkeypatch.setattr(executor, "read_agent_config", lambda: {
        "ta_sidestep_force_execute": True,
        "ta_sidestep_min_slow_burn_count": 2,
        "force_execute_composite": 20,
    })
    calls = {}
    r = executor.route_verdict(
        {"verdict": "PASS", "coin": "PURR", "slow_burn_count": 1,
         "composite_score": 0.0},
        execute_fn=lambda a: calls.setdefault("e", a) or {"executed": True},
        close_fn=lambda c: calls.setdefault("c", c),
    )
    assert r["action"] == "none"
    assert not calls


def test_route_verdict_ta_sidestep_min_slow_burn_not_bypassed_by_burst(monkeypatch):
    """momentum_burst 单条不得绕过 ta_sidestep_min_slow_burn_count。

    三个条件曾用 or 连接，一条 momentum_burst_fired 就满足整个子句，
    slow-burn 门槛成了死配置（实盘 2026-08-20 五次 override 全是 slow=1 对 min=2）。
    """
    from hermes_trader.agents import executor
    monkeypatch.setattr(executor, "read_agent_config", lambda: {
        "ta_sidestep_force_execute": True,
        "ta_sidestep_min_slow_burn_count": 2,
        "force_execute_composite": 30,
    })
    calls = {}
    r = executor.route_verdict(
        {"verdict": "PASS", "coin": "CASHCAT", "slow_burn_count": 1,
         "momentum_burst_fired": True, "composite_score": 44.1},
        execute_fn=lambda a: calls.setdefault("e", a) or {"executed": True},
        close_fn=lambda c: calls.setdefault("c", c),
    )
    assert r["action"] == "none"
    assert not calls


def test_route_verdict_plain_pass_still_noop():
    """A PASS with NO override hint (no whale, weak composite) stays a no-op —
    we don't want every hedged PASS hitting the executor."""
    from hermes_trader.agents.executor import route_verdict
    calls = {}
    r = route_verdict({"verdict": "PASS", "coin": "BTC",
                       "composite_score": 20.0, "slow_burn_count": 0},
                      execute_fn=lambda a: calls.setdefault("e", 1),
                      close_fn=lambda c: calls.setdefault("c", 1))
    assert r["action"] == "none"
    assert not calls


def test_route_verdict_unknown_is_flagged_not_dropped():
    """A novel/garbage verdict must surface as 'unknown', never silently no-op
    like a PASS — that's how the next dropped-verdict bug gets caught."""
    from hermes_trader.agents.executor import route_verdict
    calls = {}
    r = route_verdict({"verdict": "YOLO", "coin": "BTC"},
                      execute_fn=lambda a: calls.setdefault("e", 1),
                      close_fn=lambda c: calls.setdefault("c", 1))
    assert r["action"] == "unknown"
    assert r["verdict"] == "YOLO"
    assert not calls


def test_route_verdict_lowercase_verdict_normalized():
    from hermes_trader.agents.executor import route_verdict
    r = route_verdict({"verdict": "close", "coin": "X"},
                      execute_fn=lambda a: None, close_fn=lambda c: {"ok": True})
    assert r["action"] == "close"


def test_parse_verdict_regex_fallback_midtext():
    """JSON not on the last line is recovered by the regex fallback."""
    from hermes_trader.agents.research import parse_verdict
    txt = ('reasoning here\n{"verdict":"LONG","confidence":0.7,"side":"long"}\n'
           'some trailing commentary')
    v = parse_verdict(txt, "BTC", {"mid": 50})
    assert v["verdict"] == "LONG" and v["side"] == "long"


def test_parse_verdict_malformed_json_uses_first_line_keyword():
    """Unparseable JSON falls back to a keyword scan of the first line."""
    from hermes_trader.agents.research import parse_verdict
    txt = 'SHORT setup forming\n{"verdict": broken json,,}'
    v = parse_verdict(txt, "ETH", {"mid": 10})
    assert v["verdict"] == "SHORT" and v["side"] == "short"
