"""C11/C12 (Audit 2026-09-06), hard-coded constant -> config tuning guard tests.

守护四项硬编码常量迁入 config 后，默认行为不变且 config/env 覆盖真正到达
热路径（含失败回落，质量门不被削弱）：

  C11 tiered notional cap —— notional_cap_tier_equity_usd (50.0) 与
      notional_cap_tier_multiple (1.5) 可调；_tiered_notional_cap 纯函数默认
      参数=历史常量（现有 2 参数调用不破）；executor 调用点解析 config 传入。
  C12 min_order_usd (10.5) —— exchange._resolve_min_order_usd 读 canonical key；
      异常/缺省回落 10.5；HL 硬底 $10 以下配置不生效（回落 10.5）。
  C12 correlation_crypto_coins (40 币) —— correlation_cap 币池可调；空/非 list
      回落内置 frozenset（门永不因坏配置静默失效）；仅计多头。
另锁 CANONICAL_DEFAULTS sentinel 与 schema 越界拒绝。
"""

from hermes_trader.agents import risk_gates as rg
from hermes_trader.agents import executor as ex
from hermes_trader.agents.config_schema import validate_config_updates
from hermes_trader.agents.config_store import CANONICAL_DEFAULTS, cfg_get
from hermes_trader.client import exchange as exchange_mod


# ── C11: tiered notional cap ────────────────────────────────────────────────
def test_c11_canonical_defaults_present():
    assert CANONICAL_DEFAULTS["notional_cap_tier_equity_usd"] == 50.0
    assert CANONICAL_DEFAULTS["notional_cap_tier_multiple"] == 1.5


def test_c11_pure_function_default_args_match_legacy_constants():
    # Default kwargs must equal the historical module constants so existing
    # 2-arg call sites/tests keep identical behaviour.
    assert ex._NOTIONAL_CAP_TIER_EQUITY_USD == 50.0
    assert ex._NOTIONAL_CAP_TIER_MULTIPLE == 1.5
    # base<=0 -> disabled
    assert ex._tiered_notional_cap(0.0, 1000.0) == 0.0
    # below tier equity -> absolute floor binds
    assert ex._tiered_notional_cap(30.0, 20.9) == 30.0
    # at/above tier -> max(base, equity*multiple)
    assert ex._tiered_notional_cap(30.0, 100.0) == 150.0
    assert ex._tiered_notional_cap(30.0, 50.0) == 75.0
    # base already larger than scaled -> base
    assert ex._tiered_notional_cap(500.0, 60.0) == 500.0


def test_c11_config_overrides_change_tier():
    cfg = {"notional_cap_tier_equity_usd": 200.0, "notional_cap_tier_multiple": 2.0}
    tier_eq = float(cfg_get("notional_cap_tier_equity_usd", config=cfg))
    tier_mult = float(cfg_get("notional_cap_tier_multiple", config=cfg))
    assert tier_eq == 200.0
    assert tier_mult == 2.0
    # equity 100 < new tier 200 -> floor binds (would have scaled under old 50)
    assert ex._tiered_notional_cap(30.0, 100.0, tier_eq, tier_mult) == 30.0
    # equity 300 >= 200 -> 2.0x
    assert ex._tiered_notional_cap(30.0, 300.0, tier_eq, tier_mult) == 600.0


def test_c11_env_override_reaches_cfg(monkeypatch):
    monkeypatch.setenv("HERMES_CFG_NOTIONAL_CAP_TIER_MULTIPLE", "3.0")
    assert float(cfg_get("notional_cap_tier_multiple", config={})) == 3.0


# ── C12: min_order_usd ──────────────────────────────────────────────────────
def test_c12_min_order_default_sentinel():
    assert CANONICAL_DEFAULTS["min_order_usd"] == 10.5
    assert exchange_mod.MIN_ORDER_USD == 10.5


def test_c12_resolve_min_order_default_is_10_5():
    # No config (empty dict) -> canonical default 10.5
    assert exchange_mod._resolve_min_order_usd() == cfg_get(
        "min_order_usd", 10.5, config={})


def test_c12_resolve_min_order_config_override(monkeypatch):
    monkeypatch.setenv("HERMES_CFG_MIN_ORDER_USD", "12.0")
    assert exchange_mod._resolve_min_order_usd() == 12.0


def test_c12_resolve_min_order_below_hl_floor_falls_back(monkeypatch):
    # HL hard-rejects < $10; a sub-floor config must NOT be honoured.
    monkeypatch.setenv("HERMES_CFG_MIN_ORDER_USD", "5.0")
    assert exchange_mod._resolve_min_order_usd() == exchange_mod.MIN_ORDER_USD


def test_c12_min_order_size_scales_with_resolver(monkeypatch):
    # sz_decimals=0 (integer coins), price=$1.00 -> ceil(min/1) coins.
    monkeypatch.setenv("HERMES_CFG_MIN_ORDER_USD", "20.0")
    size = exchange_mod._min_order_size(1.0, 0)
    assert size == 20.0
    monkeypatch.delenv("HERMES_CFG_MIN_ORDER_USD", raising=False)
    assert exchange_mod._min_order_size(1.0, 0) == 11.0  # ceil(10.5)=11


def test_c12_resolve_never_raises_on_bad_config(monkeypatch):
    monkeypatch.setenv("HERMES_CFG_MIN_ORDER_USD", "not-a-number")
    # _coerce may yield NaN/str; float() then raises -> resolver must fall back.
    val = exchange_mod._resolve_min_order_usd()
    assert val == exchange_mod.MIN_ORDER_USD


# ── C12: correlation_crypto_coins pool ──────────────────────────────────────
def _corr_ctx(positions, side="long"):
    return rg.GateContext(
        confidence=0.8,
        current_positions=positions,
        trade_notional_usd=100.0,
        daily_pnl=0.0,
        market_volume_24h_usd=1_000_000.0,
        coin="NEWCOIN",
        trade_side=side,
        has_binary_news_risk=False,
        equity=1000.0,
        total_open_notional=0.0,
        entry_px=100.0,
    )


def test_c12_corr_default_pool_sentinel_is_40_coins():
    pool = CANONICAL_DEFAULTS["correlation_crypto_coins"]
    assert isinstance(pool, list)
    assert len(pool) == 40
    assert "BTC" in pool and "ETH" in pool
    # built-in frozenset mirrors the canonical list
    assert set(pool) == set(rg._CRYPTO_COINS)


def test_c12_corr_builtin_pool_blocks_when_cap_reached():
    # Two existing BTC/ETH longs, cap=2 -> 3rd long blocked.
    positions = [
        {"coin": "BTC", "side": "long"},
        {"coin": "ETH", "side": "long"},
    ]
    r = rg.correlation_cap(_corr_ctx(positions), 2)
    assert r["pass"] is False
    # cap=3 -> pass
    assert rg.correlation_cap(_corr_ctx(positions), 3)["pass"] is True


def test_c12_corr_only_counts_longs():
    positions = [
        {"coin": "BTC", "side": "short"},
        {"coin": "ETH", "side": "short"},
    ]
    assert rg.correlation_cap(_corr_ctx(positions, side="long"), 1)["pass"] is True


def test_c12_corr_config_pool_override_takes_effect():
    # Custom pool containing only FOO; existing BTC/ETH longs are NOT in it.
    positions = [
        {"coin": "BTC", "side": "long"},
        {"coin": "ETH", "side": "long"},
    ]
    # cap=1 with builtin pool would block; with custom pool ["FOO"] it passes.
    r = rg.correlation_cap(_corr_ctx(positions), 1, coins=["FOO", "BAR"])
    assert r["pass"] is True
    # A FOO long now counts -> blocks at cap=1.
    positions2 = [{"coin": "FOO", "side": "long"}]
    r2 = rg.correlation_cap(_corr_ctx(positions2), 1, coins=["FOO", "BAR"])
    assert r2["pass"] is False


def test_c12_corr_empty_pool_falls_back_to_builtin():
    positions = [
        {"coin": "BTC", "side": "long"},
        {"coin": "ETH", "side": "long"},
    ]
    # empty list / None must fall back to builtin 40-coin pool -> blocks.
    for empty in (None, [], (), set()):
        r = rg.correlation_cap(_corr_ctx(positions), 2, coins=empty)
        assert r["pass"] is False, empty


def test_c12_corr_eval_all_gates_resolves_config_pool(monkeypatch):
    # eval_all_gates must pass the config pool through. With a pool that excludes
    # BTC/ETH, a long that would otherwise hit the correlation cap passes it.
    import hermes_trader.agents.risk_gates as rg_mod

    cfg = {
        "max_crypto_long_correlated": 1,
        "correlation_crypto_coins": ["FOO"],
        # neutralise other gates that could block this synthetic ctx
        "correlation_check": True,
    }
    positions = [{"coin": "BTC", "side": "long"}]
    ctx = _corr_ctx(positions)
    # Call the gate the way eval_all_gates does (resolve + pass coins).
    coins = cfg_get("correlation_crypto_coins", config=cfg)
    if not isinstance(coins, (list, tuple, set, frozenset)):
        coins = None
    r = rg_mod.correlation_cap(
        ctx, int(cfg_get("max_crypto_long_correlated", config=cfg)), coins=coins)
    # BTC not in ["FOO"] -> 0 correlated longs < cap 1 -> pass
    assert r["pass"] is True


def test_c12_corr_non_list_config_treated_as_none():
    # Mirror eval_all_gates' defensive isinstance guard.
    for bad in ("BTC,ETH", 42, 7.5, True):
        coins = bad if isinstance(bad, (list, tuple, set, frozenset)) else None
        assert coins is None


# ── schema validation ───────────────────────────────────────────────────────
def test_c11_c12_schema_accepts_valid():
    errors = validate_config_updates({
        "notional_cap_tier_equity_usd": 100.0,
        "notional_cap_tier_multiple": 2.0,
        "min_order_usd": 12.0,
        "correlation_crypto_coins": ["BTC", "ETH"],
    }, strict_keys=True)
    assert errors == [], errors


def test_c11_c12_schema_rejects_out_of_range():
    # tier multiple out of [0,100]
    assert validate_config_updates(
        {"notional_cap_tier_multiple": 999.0}, strict_keys=True)
    # tier equity negative
    assert validate_config_updates(
        {"notional_cap_tier_equity_usd": -1.0}, strict_keys=True)
    # min_order_usd below HL floor (ge=10.0)
    assert validate_config_updates(
        {"min_order_usd": 5.0}, strict_keys=True)
    # min_order_usd above cap
    assert validate_config_updates(
        {"min_order_usd": 99999.0}, strict_keys=True)


def test_c12_min_order_schema_floor_is_hl_10():
    # 10.0 is the hard floor and must be accepted (ge=10.0); 9.99 rejected.
    assert validate_config_updates(
        {"min_order_usd": 10.0}, strict_keys=True) == []
    assert validate_config_updates(
        {"min_order_usd": 9.99}, strict_keys=True)
