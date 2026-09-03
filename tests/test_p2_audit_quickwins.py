"""Audit P2 quick-win remediation tests (docs/REVIEW_FINAL_GITHUB.md, 2026-09-03).

Seven low-risk bug fixes, one test section each:

P2-1  memory.py      — intraday peakDailyPnl / startOfDayEquity now survive a
                       flush→reload within the same UTC day (give-back breaker
                       no longer degrades to a zero high-water mark on restart).
P2-3  rate_limit.py  — userFillsByTime / openOrders are cheap (weight 2), not
                       the unknown-endpoint default of 20.
P2-4  universe.py    — get_market_by_coin("<dex>:<symbol>") fetches the
                       HIP-3-inclusive universe (namespaced coins only exist
                       there), mirroring get_day_ntl_vlm.
P2-5  config_preset  — `apply --account-size N` refuses up-front with guidance
                       instead of auto-picking a legacy risk preset and then
                       rejecting it; --allow-legacy-risk-preset still works.
P2-7  research.py    — _account_context tolerates JSON nulls (equity=None,
                       szi=None, non-numeric rows) instead of float(None) crash.
P2-9  backtest_logged— ATR primary-stop sizing reads dsl_exit gates via cfg_get
                       canonical defaults (0.4% / ROE 5.0%), not stale inline
                       2.0% / 40.0% fallbacks that diverged from the live gates.
P2-13 exchange.py    — _is_isolated_only goes through _cached_universe()
                       (TTL + stampede lock + stale fallback) instead of a
                       direct info.meta() POST on every leverage set.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
# backtest_logged.py imports the sibling scripts/ module `_memory_io`; make the
# scripts dir importable before the module is loaded below.
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


# ── P2-1: peakDailyPnl / startOfDayEquity persistence ──────────────────────

def _isolated_memory(monkeypatch, tmp_path):
    """Fresh AgentMemory pointed at tmp paths; resets the module singleton on
    teardown so other tests are unaffected (mirrors test_audit_bf2_bf6_bf7)."""
    from hermes_trader.agents import memory as memory_mod

    mem_path = str(tmp_path / ".agent-memory.json")
    monkeypatch.setattr(memory_mod, "MEMORY_FILE", mem_path)
    monkeypatch.setattr(memory_mod, "MEMORY_LOCK_FILE", mem_path + ".lock")
    monkeypatch.setattr(memory_mod, "_EVENTS_FILE", str(tmp_path / "events.jsonl"))

    memory_mod.AgentMemory._instance = None
    m = memory_mod.AgentMemory.get_instance()
    m.load()
    monkeypatch.setattr(memory_mod, "memory", m)
    return m, mem_path


def test_p2_1_peak_daily_pnl_survives_flush_reload(monkeypatch, tmp_path):
    m, mem_path = _isolated_memory(monkeypatch, tmp_path)
    # Simulate an intraday high-water mark (e.g. +$12.30 paper peak).
    m._peak_daily_pnl = 12.30
    m._start_of_day_equity = 100.0
    m._daily_pnl = 4.56
    m._equity = 104.56
    m._dirty = True
    m.flush(force=True)

    # Reload from disk through a brand-new instance.
    from hermes_trader.agents import memory as memory_mod
    memory_mod.AgentMemory._instance = None
    m2 = memory_mod.AgentMemory.get_instance()
    m2.load()
    assert m2.peak_daily_pnl() == 12.30
    assert m2._start_of_day_equity == 100.0
    assert m2._daily_pnl == 4.56
    assert m2._equity == 104.56


def test_p2_1_old_memory_file_without_peak_loads_as_zero(monkeypatch, tmp_path):
    """A file written before P2-1 (no peakDailyPnl key) hydrates to 0.0 rather
    than crashing — the first accepted equity tick re-baselines it."""
    import json

    from hermes_trader.agents import memory as memory_mod

    mem_path = tmp_path / ".agent-memory.json"
    mem_path.write_text(json.dumps({"equity": 50.0}))
    monkeypatch.setattr(memory_mod, "MEMORY_FILE", str(mem_path))
    monkeypatch.setattr(memory_mod, "MEMORY_LOCK_FILE", str(mem_path) + ".lock")
    monkeypatch.setattr(memory_mod, "_EVENTS_FILE", str(tmp_path / "events.jsonl"))

    memory_mod.AgentMemory._instance = None
    m = memory_mod.AgentMemory.get_instance()
    m.load()
    assert m.peak_daily_pnl() == 0.0
    assert m._start_of_day_equity == 0.0
    assert m._equity == 50.0


# ── P2-3: endpoint weights for cheap user queries ──────────────────────────

def test_p2_3_user_fills_by_time_is_cheap_weight():
    from hermes_trader.client import rate_limit
    assert rate_limit.endpoint_weight("userFillsByTime") == 2
    assert rate_limit.endpoint_weight("openOrders") == 2
    assert rate_limit.endpoint_weight("userFills") == 2
    # Unknown / None still conservatively cost the heavy bucket.
    assert rate_limit.endpoint_weight("someNewEndpoint") == 20
    assert rate_limit.endpoint_weight(None) == 20


# ── P2-4: HIP-3 namespaced market lookups include the HIP-3 universe ───────

def test_p2_4_namespaced_coin_fetches_hip3_universe(monkeypatch):
    from hermes_trader.client import universe

    calls: list[bool] = []

    def fake_get_universe(force_refresh=False, include_hip3=False):
        calls.append(include_hip3)
        if include_hip3:
            return [{"coin": "xyz:NVDA", "type": "perp", "dex": "xyz"}]
        return [{"coin": "BTC", "type": "perp", "dex": None}]

    monkeypatch.setattr(universe, "get_universe", fake_get_universe)

    assert universe.get_market_by_coin("xyz:NVDA") == {"coin": "xyz:NVDA", "type": "perp", "dex": "xyz"}
    assert calls == [True]  # namespaced lookup must request the HIP-3-inclusive universe


def test_p2_4_plain_coin_uses_main_universe(monkeypatch):
    from hermes_trader.client import universe

    calls: list[bool] = []

    def fake_get_universe(force_refresh=False, include_hip3=False):
        calls.append(include_hip3)
        return [{"coin": "BTC", "type": "perp", "dex": None}]

    monkeypatch.setattr(universe, "get_universe", fake_get_universe)
    assert universe.get_market_by_coin("BTC")["coin"] == "BTC"
    assert calls == [False]
    assert universe.get_market_by_coin("xyz:MISSING") is None


# ── P2-5: config_preset auto-pick refusal path ─────────────────────────────

def _load_config_preset():
    spec = importlib.util.spec_from_file_location(
        "config_preset", _SCRIPTS_DIR / "config_preset.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_p2_5_auto_pick_refused_by_default(monkeypatch, tmp_path, capsys):
    preset = _load_config_preset()
    cfg_file = tmp_path / ".agent-config.json"
    cfg_file.write_text("{}")
    monkeypatch.setattr(preset, "CONFIG_FILE", cfg_file)

    rc = preset.cmd_apply(None, account_size=250.0, yes=True,
                          allow_legacy_risk_preset=False)
    out = capsys.readouterr().out
    assert rc == 2
    assert "refused by default" in out
    assert "small_aggressive" in out  # names the preset auto-pick would have chosen
    assert "hip3_only" in out  # actionable alternative
    # Config must not have been touched.
    assert cfg_file.read_text() == "{}"


def test_p2_5_auto_pick_allowed_with_flag(monkeypatch, tmp_path):
    preset = _load_config_preset()
    cfg_file = tmp_path / ".agent-config.json"
    cfg_file.write_text("{}")
    monkeypatch.setattr(preset, "CONFIG_FILE", cfg_file)

    rc = preset.cmd_apply(None, account_size=250.0, yes=True,
                          allow_legacy_risk_preset=True)
    assert rc == 0
    import json
    saved = json.loads(cfg_file.read_text())
    assert saved["leverage"] == 40  # small_aggressive applied


# ── P2-7: _account_context null tolerance ──────────────────────────────────

def test_p2_7_equity_null_does_not_crash(monkeypatch):
    from hermes_trader.agents import research

    monkeypatch.setattr(research, "resolve_user_address", lambda: "0xabc")
    monkeypatch.setattr(
        research,
        "fetch_account_state",
        lambda *a, **k: {"equity": None, "dex_equity": None, "asset_positions": None},
    )
    equity, dex_equity, positions = research._account_context(None)
    assert equity == 0.0
    assert dex_equity == {}
    assert positions == []


def test_p2_7_bad_position_rows_are_skipped(monkeypatch):
    from hermes_trader.agents import research

    monkeypatch.setattr(research, "resolve_user_address", lambda: "0xabc")
    state = {
        "equity": "1000",
        "dex_equity": {"xyz": "50"},
        "asset_positions": [
            {"position": None},  # null position row — skip
            {"position": {"coin": "ETH", "szi": None, "positionValue": None, "entryPx": None}},
            {"position": {"coin": "JUNK", "szi": "not-a-number"}},  # bad float — skip
            {"position": {"coin": "ZERO", "szi": "0"}},  # flat — skip
            {"position": {"coin": "BTC", "szi": "0.5", "positionValue": "3000", "entryPx": "60000"}},
            {"position": {"coin": "DOGE", "szi": "-100", "positionValue": None, "entryPx": "0.1"}},
        ],
    }
    equity, _dex, positions = research._account_context(state)
    assert equity == 1000.0
    assert {(p["coin"], p["side"]) for p in positions} == {("BTC", "long"), ("DOGE", "short")}
    btc = next(p for p in positions if p["coin"] == "BTC")
    doge = next(p for p in positions if p["coin"] == "DOGE")
    assert btc["size_usd"] == 3000.0
    # positionValue null → falls back to |szi| * entryPx = 100 * 0.1 = 10
    assert doge["size_usd"] == 10.0


# ── P2-9: backtest ATR sizing uses canonical dsl_exit gates, not stale ones ─

def _load_backtest_logged():
    spec = importlib.util.spec_from_file_location(
        "backtest_logged", _SCRIPTS_DIR / "backtest_logged.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_p2_9_primary_stop_uses_canonical_gates(monkeypatch):
    bt = _load_backtest_logged()
    monkeypatch.setattr(bt, "max_leverage_for", lambda coin, fallback: fallback)

    cfg = {
        "max_trade_notional_usd": 0,
        "atr_risk_sizing": {"enabled": True, "risk_per_trade_pct": 0.01,
                            "sizing_basis": "primary_stop"},
    }
    # Empty dsl_cfg: keys missing → cfg_get falls through to CANONICAL_DEFAULTS
    # (max_loss_pct 0.4 / max_loss_roe_pct 5.0), not the old stale 2.0 / 40.0.
    notional, tag = bt.live_sized_notional(
        coin="BTC", entry_px=60000.0, entry_ms=0, equity=1000.0,
        equity_fraction=0.1, leverage=5, cfg=cfg, dsl_cfg={},
    )
    assert tag.startswith("primary_stop")
    # stop_frac = min(0.4, 5.0/5)/100 = 0.004 → notional = 0.01*1000/0.004 = 2500,
    # capped at equity*lev = 5000. Stale fallbacks would have given
    # min(2.0, 40/5)/100 = 0.02 → 500. Assert the canonical number.
    assert abs(notional - 2500.0) < 1e-6


def test_p2_9_no_stale_inline_fallbacks_in_source():
    """Guard against reintroducing the diverged `or 2.0` / `or 40.0` inline
    defaults on the primary-stop branch."""
    src = (_SCRIPTS_DIR / "backtest_logged.py").read_text()
    # Scope to the stop_frac computation statement itself (the surrounding
    # comment mentions the old values by design).
    stop_block = src.split("stop_frac = min(", 1)[1].split(") / 100.0", 1)[0]
    assert "or 2.0" not in stop_block
    assert "or 40.0" not in stop_block


# ── P2-13: _is_isolated_only via the TTL meta cache ────────────────────────

def test_p2_13_reads_through_cached_universe(monkeypatch):
    from hermes_trader.client import exchange

    seen_dex: list = []

    def fake_cached(dex=None):
        seen_dex.append(dex)
        if dex is None:
            return [{"name": "BTC", "onlyIsolated": False},
                    {"name": "ISOL", "onlyIsolated": True}]
        return [{"name": f"{dex}:NVDA", "onlyIsolated": True}]

    monkeypatch.setattr(exchange, "_cached_universe", fake_cached)
    # A boom guard: the direct info.meta() path must NOT be used.
    monkeypatch.setattr(exchange, "_get_info", lambda: (_ for _ in ()).throw(
        AssertionError("P2-13: _is_isolated_only must not call _get_info()")))

    assert exchange._is_isolated_only("BTC") is False
    assert exchange._is_isolated_only("ISOL") is True
    assert exchange._is_isolated_only("xyz:NVDA") is True
    assert seen_dex == [None, None, None, "xyz"]  # main dex probed first, then HIP-3 dex


def test_p2_13_lookup_failure_defaults_to_cross(monkeypatch):
    from hermes_trader.client import exchange

    def boom(*a, **k):
        raise RuntimeError("meta unavailable")

    monkeypatch.setattr(exchange, "_cached_universe", boom)
    assert exchange._is_isolated_only("BTC") is False
