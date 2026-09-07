"""Wave D (Audit 2026-09-06), Pathia-ported risk gates — guard tests.

三门均为 SHADOW 灰度先行（mode off/shadow/enforce），守护以下不变量：
  D1 trend_filter_200ma —— 仅过滤多头（空头永不拦）；off 零网络；日线抓取
     失败 block_unknown=false → fail-OPEN+WARNING，block_unknown=true →
     fail-CLOSED；新币日线不足仅 min_history_bars>0 时 fail-CLOSED；daily
     mover 旁路只在 24h 涨幅 [10%,30%] 区间生效，>30% parabolic 不旁路、
     涨幅未知不旁路；shadow 记录反事实但放行，enforce 才 block。
  D2 daily_extension_cap —— 仅多头；24h 涨幅超 cap(30%) 不追多；涨幅未知
     fail-OPEN；cap<=0 禁用；无 mode 块时默认 shadow（env 可切）。
  D3 reentry_cap —— 多空均计开仓；窗口内开仓 >= max_per_coin 才拦；
     memory 读失败 fail-OPEN；max_per_coin<=0 禁用；shadow 放行 enforce 拦。
另锁配置层（CANONICAL_DEFAULTS 两块 + 两标量、schema 三态/越界/未知叶）。
"""

import json
from pathlib import Path

from hermes_trader.agents import risk_gates as rg
from hermes_trader.agents.config_schema import validate_config_updates
from hermes_trader.agents.config_store import CANONICAL_DEFAULTS, cfg_get


# ── helpers ────────────────────────────────────────────────────────────────
def _ctx(coin="TESTCOIN", side="long", entry_px=100.0):
    return rg.GateContext(
        confidence=0.8,
        current_positions=[],
        trade_notional_usd=100.0,
        daily_pnl=0.0,
        market_volume_24h_usd=1_000_000.0,
        coin=coin,
        trade_side=side,
        has_binary_news_risk=False,
        equity=1000.0,
        total_open_notional=0.0,
        entry_px=entry_px,
    )


def _d_cfg(tmp_path, mode="shadow", **over):
    blk = {
        "mode": mode,
        "period": 5,                 # tiny period so 6 bars form an SMA
        "fetch_bars": 10,
        "block_unknown": False,
        "allow_daily_mover_long_bypass": True,
        "daily_mover_min_ext_pct": 10.0,
        "daily_mover_max_ext_pct": 30.0,
        "shadow_log_path": str(tmp_path / "trend.jsonl"),
    }
    blk.update(over)
    cfg = {"trend_filter_200ma": blk}
    return cfg


def _candles(closes):
    """D1 reads c["c"] (dict-style Candle)."""
    return [{"c": float(c)} for c in closes]


def _patch_fetch(monkeypatch, candles=None, raises=False):
    """Patch the lazily-imported fetch_hl_candles / _drop_forming_bar.

    The gate does `from hermes_trader.client.hl_client import fetch_hl_candles`
    inside the function, so patch the source module attribute."""
    import hermes_trader.agents.perception as perception
    import hermes_trader.client.hl_client as hl_client

    if raises:
        def _boom(*_a, **_k):
            raise RuntimeError("candle API down")
        monkeypatch.setattr(hl_client, "fetch_hl_candles", _boom)
    else:
        monkeypatch.setattr(hl_client, "fetch_hl_candles",
                            lambda *_a, **_k: list(candles or []))
    # _drop_forming_bar drops the last (forming) bar; emulate on dicts.
    monkeypatch.setattr(perception, "_drop_forming_bar",
                        lambda raw, _iv: (list(raw[:-1]), False))


def _patch_universe(monkeypatch, prev=None, mid=None):
    import hermes_trader.client.universe as universe

    mkt = {}
    if prev is not None:
        mkt["prevDayPx"] = prev
    if mid is not None:
        mkt["midPx"] = mid
    monkeypatch.setattr(universe, "get_market_by_coin",
                        lambda _c: dict(mkt) if mkt else None)


def _read_shadow(path):
    p = Path(path)
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines()]


# ══════════════════════════════════════════════════════════════════════════
# 配置层
# ══════════════════════════════════════════════════════════════════════════
def test_d_config_defaults_registered():
    assert "trend_filter_200ma" in CANONICAL_DEFAULTS
    assert "reentry_cap" in CANONICAL_DEFAULTS
    t = CANONICAL_DEFAULTS["trend_filter_200ma"]
    assert t["mode"] == "off"                # 默认不武装（零网络直到翻转）
    assert t["period"] == 200
    assert t["block_unknown"] is False
    assert t["allow_daily_mover_long_bypass"] is True
    assert t["daily_mover_min_ext_pct"] == 10.0
    assert t["daily_mover_max_ext_pct"] == 30.0
    # D2: mode block defaults to SHADOW (Pathia ships the cap live; Hermes
    # probes would-blocks first), matching the gate's no-block fallback.
    d2 = CANONICAL_DEFAULTS["daily_extension_cap"]
    assert d2["mode"] == "shadow"
    assert "shadow_log_path" in d2
    r = CANONICAL_DEFAULTS["reentry_cap"]
    assert r["mode"] == "off"
    assert r["max_per_coin"] == 2
    assert r["window_hours"] == 24.0
    # D1/D2 root scalars
    assert cfg_get("min_history_bars", config={}) == 0
    assert cfg_get("override_max_daily_extension_pct", config={}) == 30.0


def test_d_schema_accepts_three_modes_and_rejects_bad():
    for blk_name in ("trend_filter_200ma", "daily_extension_cap", "reentry_cap"):
        for mode in ("off", "shadow", "enforce"):
            errors = validate_config_updates({blk_name: {"mode": mode}},
                                             strict_keys=True)
            assert errors == [], (blk_name, mode, errors)
        errors = validate_config_updates({blk_name: {"mode": "bogus"}},
                                         strict_keys=True)
        assert any("mode" in e for e in errors), errors
        errors = validate_config_updates({blk_name: {"not_a_leaf": 1}},
                                         strict_keys=True)
        assert any("unknown" in e for e in errors), errors


def test_d_schema_rejects_out_of_range():
    errors = validate_config_updates(
        {"trend_filter_200ma": {"period": 5}}, strict_keys=True)  # < 20
    assert errors, errors
    errors = validate_config_updates(
        {"reentry_cap": {"max_per_coin": 999}}, strict_keys=True)  # > 100
    assert errors, errors


# ══════════════════════════════════════════════════════════════════════════
# D1 trend_filter_200ma
# ══════════════════════════════════════════════════════════════════════════
def test_d1_off_never_fetches(monkeypatch, tmp_path):
    def _boom(*_a, **_k):
        raise AssertionError("off mode must not fetch daily candles")
    import hermes_trader.client.hl_client as hl_client
    monkeypatch.setattr(hl_client, "fetch_hl_candles", _boom)
    cfg = _d_cfg(tmp_path, mode="off")
    r = rg.trend_filter_200ma_gate(_ctx(), cfg)
    assert r["pass"] and r["via"] == "trend_filter_off"


def test_d1_short_never_gated(monkeypatch, tmp_path):
    _patch_fetch(monkeypatch, candles=_candles([200] * 10))  # price below
    cfg = _d_cfg(tmp_path, mode="enforce")
    r = rg.trend_filter_200ma_gate(_ctx(side="short", entry_px=100.0), cfg)
    assert r["pass"] and r["via"] == "trend_filter_short_skip"


def test_d1_above_sma_passes(monkeypatch, tmp_path):
    # SMA of ~100; price 150 above → pass
    _patch_fetch(monkeypatch, candles=_candles([100, 100, 100, 100, 100, 100]))
    cfg = _d_cfg(tmp_path, mode="enforce")
    r = rg.trend_filter_200ma_gate(_ctx(entry_px=150.0), cfg)
    assert r["pass"] and r["via"] == "trend_filter_above_sma"


def test_d1_below_sma_shadow_passes_but_records(monkeypatch, tmp_path):
    _patch_fetch(monkeypatch, candles=_candles([200, 200, 200, 200, 200, 200]))
    _patch_universe(monkeypatch, prev=200.0, mid=100.0)  # -50% day, no bypass
    cfg = _d_cfg(tmp_path, mode="shadow")
    r = rg.trend_filter_200ma_gate(_ctx(entry_px=100.0), cfg)
    assert r["pass"] and r["via"] == "trend_filter_shadow_block"
    recs = _read_shadow(cfg["trend_filter_200ma"]["shadow_log_path"])
    assert recs and recs[-1]["trend_would_block"] is True
    assert recs[-1]["above_sma"] is False


def test_d1_below_sma_enforce_blocks(monkeypatch, tmp_path):
    _patch_fetch(monkeypatch, candles=_candles([200] * 6))
    _patch_universe(monkeypatch, prev=200.0, mid=100.0)
    cfg = _d_cfg(tmp_path, mode="enforce")
    r = rg.trend_filter_200ma_gate(_ctx(entry_px=100.0), cfg)
    assert not r["pass"] and r["via"] == "trend_filter_below_sma"


def test_d1_fetch_failure_fail_open_when_block_unknown_false(monkeypatch, tmp_path):
    _patch_fetch(monkeypatch, raises=True)
    cfg = _d_cfg(tmp_path, mode="enforce", block_unknown=False)
    r = rg.trend_filter_200ma_gate(_ctx(), cfg)
    assert r["pass"] and r["via"] == "trend_filter_data_missing"


def test_d1_fetch_failure_fail_closed_when_block_unknown_true(monkeypatch, tmp_path):
    _patch_fetch(monkeypatch, raises=True)
    cfg = _d_cfg(tmp_path, mode="enforce", block_unknown=True)
    r = rg.trend_filter_200ma_gate(_ctx(), cfg)
    assert not r["pass"] and r["via"] == "trend_filter_unknown_block"


def test_d1_new_coin_fail_closed_only_with_floor(monkeypatch, tmp_path):
    # Only 2 closed bars (< period 5); SMA not established.
    _patch_fetch(monkeypatch, candles=_candles([100, 100]))
    # min_history_bars armed (>= period) → new coin fail-closed under enforce
    cfg = _d_cfg(tmp_path, mode="enforce")
    cfg["min_history_bars"] = 5
    r = rg.trend_filter_200ma_gate(_ctx(), cfg)
    assert not r["pass"] and r["via"] == "trend_filter_new_coin"
    # shadow → pass but records would-block
    cfg2 = _d_cfg(tmp_path, mode="shadow")
    cfg2["min_history_bars"] = 5
    r2 = rg.trend_filter_200ma_gate(_ctx(), cfg2)
    assert r2["pass"] and r2["via"] == "trend_filter_shadow_new_coin"
    # floor inactive (min_history_bars=0, Pathia default) → fail-open
    cfg3 = _d_cfg(tmp_path, mode="enforce")  # no min_history_bars → 0
    r3 = rg.trend_filter_200ma_gate(_ctx(), cfg3)
    assert r3["pass"] and r3["via"] in (
        "trend_filter_insufficient_history", "trend_filter_data_missing")


def test_d1_mover_bypass_window(monkeypatch, tmp_path):
    # price 100 below SMA 200; qualify via a 24h mover in [10%,30%].
    _patch_fetch(monkeypatch, candles=_candles([200] * 6))
    cfg = _d_cfg(tmp_path, mode="enforce")
    # +20% day → bypass granted
    _patch_universe(monkeypatch, prev=100.0, mid=120.0)
    r = rg.trend_filter_200ma_gate(_ctx(entry_px=120.0), cfg)
    assert r["pass"] and r["via"] == "trend_filter_mover_bypass"
    # +50% day → parabolic, NO bypass → block
    _patch_universe(monkeypatch, prev=100.0, mid=150.0)
    r2 = rg.trend_filter_200ma_gate(_ctx(entry_px=150.0), cfg)
    assert not r2["pass"] and r2["via"] == "trend_filter_below_sma"
    # +5% day → too soft, no bypass → block
    _patch_universe(monkeypatch, prev=100.0, mid=105.0)
    r3 = rg.trend_filter_200ma_gate(_ctx(entry_px=105.0), cfg)
    assert not r3["pass"] and r3["via"] == "trend_filter_below_sma"
    # unknown 24h change → bypass not granted → block
    _patch_universe(monkeypatch)  # no market
    r4 = rg.trend_filter_200ma_gate(_ctx(entry_px=100.0), cfg)
    assert not r4["pass"] and r4["via"] == "trend_filter_below_sma"


def test_d1_env_mode_override(monkeypatch, tmp_path):
    _patch_fetch(monkeypatch, candles=_candles([200] * 6))
    _patch_universe(monkeypatch, prev=200.0, mid=100.0)
    monkeypatch.setenv("HERMES_TREND_FILTER_MODE", "enforce")
    cfg = _d_cfg(tmp_path, mode="shadow")  # config says shadow, env enforces
    r = rg.trend_filter_200ma_gate(_ctx(entry_px=100.0), cfg)
    assert not r["pass"] and r["via"] == "trend_filter_below_sma"


# ══════════════════════════════════════════════════════════════════════════
# D2 daily_extension_cap
# ══════════════════════════════════════════════════════════════════════════
def test_d2_disabled_when_cap_zero():
    r = rg.daily_extension_cap_gate(_ctx(), {"override_max_daily_extension_pct": 0.0})
    assert r["pass"] and r["via"] == "daily_ext_cap_disabled"


def test_d2_short_never_gated(monkeypatch):
    _patch_universe(monkeypatch, prev=100.0, mid=200.0)  # +100%
    r = rg.daily_extension_cap_gate(
        _ctx(side="short", entry_px=200.0),
        {"override_max_daily_extension_pct": 30.0,
         "daily_extension_cap": {"mode": "enforce"}})
    assert r["pass"] and r["via"] == "daily_ext_cap_short_skip"


def test_d2_over_cap_shadow_passes_enforce_blocks(monkeypatch, tmp_path):
    _patch_universe(monkeypatch, prev=100.0, mid=150.0)  # +50% > 30%
    base = {"override_max_daily_extension_pct": 30.0,
            "daily_extension_cap": {"mode": "shadow",
                                    "shadow_log_path": str(tmp_path / "d2.jsonl")}}
    rs = rg.daily_extension_cap_gate(_ctx(entry_px=150.0), base)
    assert rs["pass"] and rs["via"] == "daily_ext_cap_shadow_block"
    recs = _read_shadow(base["daily_extension_cap"]["shadow_log_path"])
    assert recs and recs[-1]["ext_would_block"] is True
    cfg_e = dict(base, daily_extension_cap={"mode": "enforce"})
    re = rg.daily_extension_cap_gate(_ctx(entry_px=150.0), cfg_e)
    assert not re["pass"] and re["via"] == "daily_ext_cap_block"


def test_d2_under_cap_passes(monkeypatch):
    _patch_universe(monkeypatch, prev=100.0, mid=110.0)  # +10% < 30%
    r = rg.daily_extension_cap_gate(
        _ctx(entry_px=110.0),
        {"override_max_daily_extension_pct": 30.0,
         "daily_extension_cap": {"mode": "enforce"}})
    assert r["pass"] and r["via"] == "daily_ext_cap_ok"


def test_d2_unknown_change_fail_open(monkeypatch):
    _patch_universe(monkeypatch)  # no market
    r = rg.daily_extension_cap_gate(
        _ctx(),
        {"override_max_daily_extension_pct": 30.0,
         "daily_extension_cap": {"mode": "enforce"}})
    assert r["pass"] and r["via"] == "daily_ext_cap_data_missing"


def test_d2_defaults_to_shadow_without_mode_block(monkeypatch):
    # Pathia ships a scalar only; gate must default to shadow (probe, not block).
    _patch_universe(monkeypatch, prev=100.0, mid=200.0)  # +100%
    r = rg.daily_extension_cap_gate(
        _ctx(entry_px=200.0), {"override_max_daily_extension_pct": 30.0})
    assert r["pass"] and r["via"] == "daily_ext_cap_shadow_block"


# ══════════════════════════════════════════════════════════════════════════
# D3 reentry_cap
# ══════════════════════════════════════════════════════════════════════════
class _FakeMemory:
    def __init__(self, count=0, raises=False):
        self._count = count
        self._raises = raises

    def count_openings_since(self, coin, since_ms):
        if self._raises:
            raise RuntimeError("memory down")
        return self._count


def _patch_memory(monkeypatch, fake):
    # The gate does `from hermes_trader.agents.memory import memory` inside the
    # function, so patch the singleton instance attribute on the source module.
    import hermes_trader.agents.memory as memory_mod
    monkeypatch.setattr(memory_mod, "memory", fake)


def _r_cfg(tmp_path, mode="shadow", **over):
    blk = {"mode": mode, "max_per_coin": 2, "window_hours": 24.0,
           "shadow_log_path": str(tmp_path / "reentry.jsonl")}
    blk.update(over)
    return {"reentry_cap": blk}


def test_d3_off_passes_without_memory(monkeypatch, tmp_path):
    _patch_memory(monkeypatch, _FakeMemory(raises=True))
    r = rg.reentry_cap_gate(_ctx(), _r_cfg(tmp_path, mode="off"))
    assert r["pass"] and r["via"] == "reentry_cap_off"


def test_d3_under_cap_passes(monkeypatch, tmp_path):
    _patch_memory(monkeypatch, _FakeMemory(count=1))
    r = rg.reentry_cap_gate(_ctx(), _r_cfg(tmp_path, mode="enforce"))
    assert r["pass"] and r["via"] == "reentry_cap_ok"


def test_d3_over_cap_shadow_passes_enforce_blocks(monkeypatch, tmp_path):
    _patch_memory(monkeypatch, _FakeMemory(count=2))  # >= 2
    cfg = _r_cfg(tmp_path, mode="shadow")
    rs = rg.reentry_cap_gate(_ctx(), cfg)
    assert rs["pass"] and rs["via"] == "reentry_cap_shadow_block"
    recs = _read_shadow(cfg["reentry_cap"]["shadow_log_path"])
    assert recs and recs[-1]["reentry_would_block"] is True
    re = rg.reentry_cap_gate(_ctx(), _r_cfg(tmp_path, mode="enforce"))
    assert not re["pass"] and re["via"] == "reentry_cap_block"


def test_d3_counts_both_sides(monkeypatch, tmp_path):
    # side-agnostic: a short is also gated when the coin hit its opening cap.
    _patch_memory(monkeypatch, _FakeMemory(count=2))
    r = rg.reentry_cap_gate(_ctx(side="short"), _r_cfg(tmp_path, mode="enforce"))
    assert not r["pass"] and r["via"] == "reentry_cap_block"


def test_d3_memory_failure_fail_open(monkeypatch, tmp_path):
    _patch_memory(monkeypatch, _FakeMemory(raises=True))
    r = rg.reentry_cap_gate(_ctx(), _r_cfg(tmp_path, mode="enforce"))
    assert r["pass"] and r["via"] == "reentry_cap_data_missing"


def test_d3_zero_cap_disables(monkeypatch, tmp_path):
    _patch_memory(monkeypatch, _FakeMemory(count=99))
    r = rg.reentry_cap_gate(_ctx(), _r_cfg(tmp_path, mode="enforce", max_per_coin=0))
    assert r["pass"] and r["via"] == "reentry_cap_disabled"


def test_d3_memory_count_openings_since_filters_coin_and_time():
    from hermes_trader.agents.memory import AgentMemory
    mem = AgentMemory.__new__(AgentMemory)  # bypass singleton/__init__ side effects
    import threading
    mem._lock = threading.RLock()
    now = 1_000_000
    # 2 BTC openings in-window, 1 BTC stale, 1 ETH in-window
    mem._trades = [
        {"coin": "BTC", "executed_at": now - 1000},
        {"coin": "BTC", "executed_at": now - 2000},
        {"coin": "BTC", "executed_at": now - 100_000_000},  # stale
        {"coin": "ETH", "executed_at": now - 1000},
    ]
    assert mem.count_openings_since("BTC", now - 10_000) == 2
    assert mem.count_openings_since("ETH", now - 10_000) == 1
    assert mem.count_openings_since("SOL", now - 10_000) == 0
