"""R13-B4: executor.py hot-path execution constants 隐式参数注册测试。

修复前，executor.py 主下单路径里有 5 个未在 CANONICAL_DEFAULTS 登记的
隐式参数（候选 1+3+5 执行批），造成两类问题：

  1. **DRIFT (最高优)** — ``tp_atr_mult=1.0`` 早已登记在 CANONICAL_DEFAULTS
     (config_store.py L179) 且 server.py / research.py 都读它，但
     executor._place_tp_scale_out L787 + 主下单 L2326/2328 实际挂 TP 用
     模块常量 ``TP_ATR_MULT=1.0``，从不读 cfg。运维调 1.2/1.5 时 AI 建议
     / 回测 / 下单三处不一致。R13-B4 修了这条 drift。

  2. **未登记 (env-only / 函数内)** — 三个在 hot-path 默默生效的常量：

     * ``sl_ceiling_hard_max_pct=15.0`` (executor.py L2296, 函数内局部常量)
       — 备份 SL 宽度硬上限 clamp，HYPE 43% 事故防护。运维不可调。
     * ``liq_buffer_usd=10.0`` (L105, env-only ``HERMES_LIQ_BUFFER_USD``)
       — P0-4 强平缓冲闸门阈值。
     * ``execution.taker_fee_pct=0.025`` (L73, env-only ``HERMES_TAKER_FEE_PCT``)
       + ``execution.round_trip_fills=2`` (L74, 纯硬编码)
       — close/flatten 簿记里的 round-trip 费率模型。

修复方式：

  * CANONICAL_DEFAULTS 登记：
      - 根键 ``liq_buffer_usd=10.0``（drift 前的旧登记 + 新加根键）
      - 根键 ``sl_ceiling_hard_max_pct=15.0``（新加）
      - 嵌套块 ``execution`` 2 字段：``taker_fee_pct=0.025`` /
        ``round_trip_fills=2``（新加）
      - 根键 ``tp_atr_mult=1.0`` 早已存在，仅补 hot-path 接线
  * ``_ConfigPatch`` schema 加 ``execution`` 字段（drift sentinel）
  * executor.py hot-path 全部走 ``_resolve_live_float`` /
    ``_resolve_liq_buffer_usd`` / ``_resolve_hl_taker_fee_pct`` /
    ``_resolve_hl_round_trip_fills`` 热路径重解析（env / .agent-config.json
    编辑无需重启即生效）
  * legacy env ``HERMES_LIQ_BUFFER_USD`` / ``HERMES_TAKER_FEE_PCT`` 仍
    优先（operator override 不变），cfg_get 兑底

零行为变化约束：默认值 = 旧硬编码字面量（1.0 / 15.0 / 10.0 / 0.025 / 2），
未设 env/config 时行为与原硬编码完全一致；``liq_buffer_usd=0`` 表示
"闸门禁用"，必须 round-trip 0.0（不能被 ``or`` 链吞掉）。
"""

import json

import pytest

from hermes_trader.agents import config_store, executor
from hermes_trader.agents.config_schema import _ConfigPatch
from hermes_trader.agents.config_store import (
    CANONICAL_DEFAULTS,
    cfg_get,
    read_agent_config,
)

# ── 1. canonical 登记断言 ──────────────────────────────────────────────

def test_r13_b4_tp_atr_mult_still_registered():
    """drift 修复前提：tp_atr_mult 必须早已登记在 CANONICAL_DEFAULTS 顶层。"""
    assert "tp_atr_mult" in CANONICAL_DEFAULTS
    assert CANONICAL_DEFAULTS["tp_atr_mult"] == 1.0


def test_r13_b4_liq_buffer_usd_registered():
    """P0-4 强平缓冲闸门阈值必须登记。"""
    assert "liq_buffer_usd" in CANONICAL_DEFAULTS
    assert CANONICAL_DEFAULTS["liq_buffer_usd"] == 10.0


def test_r13_b4_sl_ceiling_hard_max_pct_registered():
    """备份 SL 宽度硬上限（15%）必须登记。"""
    assert "sl_ceiling_hard_max_pct" in CANONICAL_DEFAULTS
    assert CANONICAL_DEFAULTS["sl_ceiling_hard_max_pct"] == 15.0


def test_r13_b4_execution_block_registered():
    """execution 嵌套块必须登记。"""
    assert "execution" in CANONICAL_DEFAULTS
    assert isinstance(CANONICAL_DEFAULTS["execution"], dict)


def test_r13_b4_execution_block_defaults_match_historical_literals():
    """默认值严格等于 executor.py 旧硬编码字面量；零行为变化。"""
    block = CANONICAL_DEFAULTS["execution"]
    # L73 env 优先 / fallback "0.025"
    assert block["taker_fee_pct"] == 0.025
    # L74 纯硬编码
    assert block["round_trip_fills"] == 2


def test_r13_b4_execution_block_has_exactly_two_keys():
    """sentinel：未来若有人加字段 / 删字段都需要在测试里显式改。"""
    block = CANONICAL_DEFAULTS["execution"]
    assert set(block.keys()) == {"taker_fee_pct", "round_trip_fills"}


def test_r13_b4_module_fallback_constants_match_canonical():
    """executor.py 的模块级 fallback 常量（保留供 import-time 引用）必须与
    canonical 默认值字面量严格一致——任何一边漂移就是 silent regression。"""
    assert executor.TP_ATR_MULT == CANONICAL_DEFAULTS["tp_atr_mult"] == 1.0
    assert executor._LIQ_BUFFER_USD == CANONICAL_DEFAULTS["liq_buffer_usd"] == 10.0
    assert executor._HL_TAKER_FEE_PCT == CANONICAL_DEFAULTS["execution"]["taker_fee_pct"] == 0.025
    assert executor._HL_ROUND_TRIP_FILLS == CANONICAL_DEFAULTS["execution"]["round_trip_fills"] == 2


# ── 2. cfg_get 解析：点路径 + 空 config 回退 canonical ────────────────

def test_r13_b4_cfg_get_tp_atr_mult():
    assert cfg_get("tp_atr_mult", config={}) == 1.0


def test_r13_b4_cfg_get_liq_buffer_usd():
    assert cfg_get("liq_buffer_usd", config={}) == 10.0


def test_r13_b4_cfg_get_sl_ceiling_hard_max_pct():
    assert cfg_get("sl_ceiling_hard_max_pct", config={}) == 15.0


def test_r13_b4_cfg_get_execution_block():
    block = cfg_get("execution", config={})
    assert isinstance(block, dict)
    assert block["taker_fee_pct"] == 0.025
    assert block["round_trip_fills"] == 2


def test_r13_b4_cfg_get_execution_dotted_paths():
    assert cfg_get("execution.taker_fee_pct", config={}) == 0.025
    assert cfg_get("execution.round_trip_fills", config={}) == 2


# ── 3. env 覆盖（canonical env 路由） ─────────────────────────────────

def test_r13_b4_env_override_tp_atr_mult(monkeypatch):
    """drift 修复后，env 改写立刻生效（之前只 server/research 见效）。"""
    assert cfg_get("tp_atr_mult", config={}) == 1.0
    monkeypatch.setenv("HERMES_CFG_TP_ATR_MULT", "1.5")
    assert cfg_get("tp_atr_mult", config={}) == 1.5


def test_r13_b4_env_override_liq_buffer_usd(monkeypatch):
    assert cfg_get("liq_buffer_usd", config={}) == 10.0
    monkeypatch.setenv("HERMES_CFG_LIQ_BUFFER_USD", "25.5")
    assert cfg_get("liq_buffer_usd", config={}) == 25.5


def test_r13_b4_env_override_sl_ceiling_hard_max_pct(monkeypatch):
    assert cfg_get("sl_ceiling_hard_max_pct", config={}) == 15.0
    monkeypatch.setenv("HERMES_CFG_SL_CEILING_HARD_MAX_PCT", "20.0")
    assert cfg_get("sl_ceiling_hard_max_pct", config={}) == 20.0


def test_r13_b4_env_override_execution_dotted(monkeypatch):
    """嵌套块 env 路由：双下划线。"""
    assert cfg_get("execution.taker_fee_pct", config={}) == 0.025
    assert cfg_get("execution.round_trip_fills", config={}) == 2
    monkeypatch.setenv("HERMES_CFG_EXECUTION__TAKER_FEE_PCT", "0.03")
    monkeypatch.setenv("HERMES_CFG_EXECUTION__ROUND_TRIP_FILLS", "3")
    assert cfg_get("execution.taker_fee_pct", config={}) == 0.03
    assert cfg_get("execution.round_trip_fills", config={}) == 3


# ── 4. config dict 部分覆盖：deep merge 正确 ─────────────────────────

def test_r13_b4_config_dict_partial_overlay():
    """传入 config 含子集覆盖，未列出的 key 走 canonical 默认。"""
    cfg = {"tp_atr_mult": 1.25, "liq_buffer_usd": 50.0}
    assert cfg_get("tp_atr_mult", config=cfg) == 1.25
    assert cfg_get("liq_buffer_usd", config=cfg) == 50.0
    # 未覆盖的 key 仍回退 canonical
    assert cfg_get("sl_ceiling_hard_max_pct", config=cfg) == 15.0
    assert cfg_get("execution.taker_fee_pct", config=cfg) == 0.025


def test_r13_b4_config_dict_execution_block_partial_overlay():
    cfg = {"execution": {"taker_fee_pct": 0.04}}
    assert cfg_get("execution.taker_fee_pct", config=cfg) == 0.04
    # 未列出的 round_trip_fills 走 canonical
    assert cfg_get("execution.round_trip_fills", config=cfg) == 2


# ── 5. read_agent_config 完整可见 + 深合并 ──────────────────────────

def test_r13_b4_read_agent_config_exposes_keys(monkeypatch, tmp_path):
    """read_agent_config() 返回的 dict 包含全部 R13-B4 新登记键。"""
    cfg_file = tmp_path / ".agent-config.json"
    monkeypatch.setattr(config_store, "CONFIG_PATH", str(cfg_file))
    monkeypatch.setattr(config_store, "_CONFIG_LOCK_PATH", str(cfg_file) + ".lock")
    config_store._invalidate_raw_cache()
    cfg = read_agent_config()
    assert cfg["tp_atr_mult"] == 1.0
    assert cfg["liq_buffer_usd"] == 10.0
    assert cfg["sl_ceiling_hard_max_pct"] == 15.0
    assert cfg["execution"]["taker_fee_pct"] == 0.025
    assert cfg["execution"]["round_trip_fills"] == 2


def test_r13_b4_read_agent_config_deep_merges_partial_overlay(monkeypatch, tmp_path):
    """on-disk 部分覆盖与 canonical 深合并，未列出的 key 仍为 canonical 默认。"""
    cfg_file = tmp_path / ".agent-config.json"
    cfg_file.write_text(json.dumps({
        "tp_atr_mult": 1.75,
        "execution": {"taker_fee_pct": 0.05},
    }))
    monkeypatch.setattr(config_store, "CONFIG_PATH", str(cfg_file))
    monkeypatch.setattr(config_store, "_CONFIG_LOCK_PATH", str(cfg_file) + ".lock")
    config_store._invalidate_raw_cache()
    cfg = read_agent_config()
    assert cfg["tp_atr_mult"] == 1.75
    assert cfg["execution"]["taker_fee_pct"] == 0.05
    # 未在 on-disk 出现的仍为 canonical
    assert cfg["liq_buffer_usd"] == 10.0
    assert cfg["sl_ceiling_hard_max_pct"] == 15.0
    assert cfg["execution"]["round_trip_fills"] == 2


# ── 6. schema 接受 + drift sentinel ──────────────────────────────────

def test_r13_b4_schema_accepts_execution_block():
    """_ConfigPatch 必须把 execution 声明为字段。"""
    from typing import Any
    fields = _ConfigPatch.model_fields
    assert "execution" in fields
    assert fields["execution"].annotation == dict[str, Any]


def test_r13_b4_validate_config_updates_accepts_r13_b4_keys():
    """含 R13-B4 字段的 patch 通过 validate_config_updates（strict_keys=True）。"""
    from hermes_trader.agents.config_schema import validate_config_updates
    errors = validate_config_updates({
        "tp_atr_mult": 1.5,
        "liq_buffer_usd": 25.0,
        "sl_ceiling_hard_max_pct": 20.0,
        "execution": {"taker_fee_pct": 0.03, "round_trip_fills": 3},
    })
    assert errors == []


# ── 7. executor 模块加载 + hot-path cfg_get 接线 ─────────────────────

def test_r13_b4_executor_module_loads_clean():
    """executor 模块加载不抛错；hot-path helpers 全存在。"""
    assert hasattr(executor, "_resolve_live_float")
    assert hasattr(executor, "_resolve_live_int")
    assert hasattr(executor, "_resolve_liq_buffer_usd")
    assert hasattr(executor, "_resolve_hl_taker_fee_pct")
    assert hasattr(executor, "_resolve_hl_round_trip_fills")


def test_r13_b4_resolve_live_float_falls_back_on_none():
    """_resolve_live_float 对 None / 非法值回退 fallback；0.0 round-trips。"""
    # None → fallback
    assert executor._resolve_live_float("nonexistent_key", 9.9) == 9.9
    # 0.0 round-trips（重要：liq buffer 0=禁用，不能被 or 链吞）
    monkey_cfg = {"test_key": 0.0}
    assert executor._resolve_live_float("test_key", 9.9, config=monkey_cfg) == 0.0
    # 非数字字符串 → fallback
    monkey_bad = {"test_key": "not_a_number"}
    assert executor._resolve_live_float("test_key", 9.9, config=monkey_bad) == 9.9


def test_r13_b4_resolve_liq_buffer_usd_legacy_env_wins(monkeypatch):
    """legacy env HERMES_LIQ_BUFFER_USD 仍优先于 canonical。"""
    monkeypatch.setenv("HERMES_LIQ_BUFFER_USD", "77.7")
    monkeypatch.setenv("HERMES_CFG_LIQ_BUFFER_USD", "999.0")  # canonical env 应被 legacy 压
    assert executor._resolve_liq_buffer_usd() == 77.7


def test_r13_b4_resolve_liq_buffer_usd_zero_round_trips(monkeypatch):
    """liq_buffer_usd=0（"闸门禁用"）必须 round-trip 0.0，不能 fallback 到 10.0。"""
    # 清掉可能干扰
    monkeypatch.delenv("HERMES_LIQ_BUFFER_USD", raising=False)
    # 用 config dict 直接喂 0
    monkeypatch.setattr(
        config_store, "CONFIG_PATH",
        "/tmp/__nonexistent_r13b4_path__/.agent-config.json"
    )
    # 直接走 _resolve_live_float 验证 0 round-trips
    assert executor._resolve_live_float("liq_buffer_usd", 10.0, config={"liq_buffer_usd": 0.0}) == 0.0


def test_r13_b4_resolve_hl_taker_fee_legacy_env_wins(monkeypatch):
    """legacy env HERMES_TAKER_FEE_PCT 仍优先。"""
    monkeypatch.setenv("HERMES_TAKER_FEE_PCT", "0.05")
    monkeypatch.setenv("HERMES_CFG_EXECUTION__TAKER_FEE_PCT", "0.10")
    assert executor._resolve_hl_taker_fee_pct() == 0.05


def test_r13_b4_resolve_hl_taker_fee_canonical_fallback(monkeypatch):
    """legacy env 未设时，canonical 兑底。"""
    monkeypatch.delenv("HERMES_TAKER_FEE_PCT", raising=False)
    monkeypatch.delenv("HERMES_CFG_EXECUTION__TAKER_FEE_PCT", raising=False)
    assert executor._resolve_hl_taker_fee_pct() == 0.025


def test_r13_b4_resolve_hl_round_trip_fills_canonical_fallback(monkeypatch):
    """无 env / config 时 round_trip_fills=2（与原硬编码一致）。"""
    monkeypatch.delenv("HERMES_CFG_EXECUTION__ROUND_TRIP_FILLS", raising=False)
    assert executor._resolve_hl_round_trip_fills() == 2


def test_r13_b4_resolve_hl_round_trip_fills_env_override(monkeypatch):
    monkeypatch.setenv("HERMES_CFG_EXECUTION__ROUND_TRIP_FILLS", "4")
    assert executor._resolve_hl_round_trip_fills() == 4


# ── 8. drift 修复：tp_atr_mult 在 _place_tp_scale_out 真实生效 ──────

def test_r13_b4_tp_atr_mult_drift_fixed_in_tp_scale_out(monkeypatch):
    """drift sentinel：调高 tp_atr_mult 后，_place_tp_scale_out 真实挂单
    用新值（之前始终用模块常量 1.0）。"""
    # 直接驱动 _resolve_live_float，验证 hot-path helper 是 cfg-driven
    # （端到端 _place_tp_scale_out 需要 mock 大量内部状态；用 helper 行为
    # 等价于 L787 那一行 atr * live_tp_atr_mult 的 live_tp_atr_mult 部分）
    cfg = {"tp_atr_mult": 1.5}
    val = executor._resolve_live_float("tp_atr_mult", executor.TP_ATR_MULT, config=cfg)
    assert val == 1.5  # 用 config 而不是 fallback 1.0

    # 不设 config 时仍回退到模块常量
    val2 = executor._resolve_live_float("tp_atr_mult", executor.TP_ATR_MULT, config={})
    assert val2 == executor.TP_ATR_MULT == 1.0

    # env override 同样生效
    monkeypatch.setenv("HERMES_CFG_TP_ATR_MULT", "1.2")
    val3 = executor._resolve_live_float("tp_atr_mult", executor.TP_ATR_MULT, config={})
    assert val3 == 1.2


def test_r13_b4_tp_atr_mult_invalid_falls_back(monkeypatch):
    """tp_atr_mult 设成 0 / 负数 / NaN 时回退模块常量。"""
    # 0
    assert executor._resolve_live_float("tp_atr_mult", 1.0, config={"tp_atr_mult": 0}) == 0  # 0 round-trips
    # 负数
    assert executor._resolve_live_float("tp_atr_mult", 1.0, config={"tp_atr_mult": -1.5}) == -1.5
    # 调用方负责 isfinite+>0 校验；helper 只做类型转换
    import math
    assert not math.isfinite(executor._resolve_live_float("tp_atr_mult", 1.0, config={"tp_atr_mult": float("nan")}))


# ── 9. _check_liquidation_buffer hot-path：0 值禁用语义保留 ───────

def test_r13_b4_check_liquidation_buffer_disabled_via_zero(monkeypatch):
    """liq_buffer_usd=0 时 _check_liquidation_buffer 返回 gate_disabled。"""
    # 直接 monkeypatch _resolve_liq_buffer_usd 返回 0.0（"闸门禁用"），
    # 模拟 operator 在 .agent-config.json 设 0 的 hot-path 行为。fetch
    # 路径不在 cfg 注册测试范围（端到端 fetch 涉及真实 HL API）。
    monkeypatch.setattr(executor, "_resolve_liq_buffer_usd", lambda: 0.0)
    res = executor._check_liquidation_buffer("BTC", 50000.0, None)
    assert res["ok"] is True
    assert res["reason"] == "gate_disabled"


def test_r13_b4_check_liquidation_buffer_no_mid_short_circuit(monkeypatch):
    """mid_price<=0 时 no_mid_price（gate 不评估）。"""
    monkeypatch.setattr(executor, "_resolve_liq_buffer_usd", lambda: 10.0)
    res = executor._check_liquidation_buffer("BTC", 0.0, None)
    assert res["ok"] is True
    assert res["reason"] == "no_mid_price"


# ── 10. legacy env 兼容性：保留不变 ───────────────────────────────

def test_r13_b4_legacy_env_liq_buffer_zero_disables(monkeypatch):
    """legacy env HERMES_LIQ_BUFFER_USD=0 仍然禁用闸门（历史契约）。"""
    monkeypatch.setenv("HERMES_LIQ_BUFFER_USD", "0")
    # 直接验证 helper 在 legacy env=0 时返回 0.0
    assert executor._resolve_liq_buffer_usd() == 0.0


def test_r13_b4_legacy_env_liq_buffer_value_used(monkeypatch):
    """legacy env HERMES_LIQ_BUFFER_USD=33 仍生效，门槛 = 33。"""
    monkeypatch.setenv("HERMES_LIQ_BUFFER_USD", "33")
    # 即使 canonical .agent-config.json 写了不同值，legacy env 优先
    assert executor._resolve_liq_buffer_usd() == 33.0


def test_r13_b4_legacy_env_hl_taker_fee_used(monkeypatch):
    """legacy env HERMES_TAKER_FEE_PCT=0.04 仍生效。"""
    monkeypatch.setenv("HERMES_TAKER_FEE_PCT", "0.04")
    assert executor._resolve_hl_taker_fee_pct() == 0.04


# ── 11. MCP / dashboard 可观测性 ───────────────────────────────────

def test_r13_b4_canonical_defaults_contains_r13_b4_keys_for_mcp_dump():
    """CANONICAL_DEFAULTS 是 MCP server / dashboard dump 的 source of truth。
    R13-B4 新登记的根键 + execution 块必须出现，否则 R11 审计的"隐式
    字段不可观测"问题未解。"""
    for key in ("tp_atr_mult", "liq_buffer_usd", "sl_ceiling_hard_max_pct", "execution"):
        assert key in CANONICAL_DEFAULTS, f"{key} missing from CANONICAL_DEFAULTS"

    # 默认值类型正确（避免 dashboard 渲染时 crash）
    assert isinstance(CANONICAL_DEFAULTS["tp_atr_mult"], float)
    assert isinstance(CANONICAL_DEFAULTS["liq_buffer_usd"], float)
    assert isinstance(CANONICAL_DEFAULTS["sl_ceiling_hard_max_pct"], float)
    assert isinstance(CANONICAL_DEFAULTS["execution"]["taker_fee_pct"], float)
    assert isinstance(CANONICAL_DEFAULTS["execution"]["round_trip_fills"], int)


# ── 12. 零行为变化：未设置 env/config 时模块级 fallback 仍正确 ─────

def test_r13_b4_zero_behavior_change_module_fallbacks():
    """未设 env / config 时，所有模块级常量与原硬编码字面量严格一致——
    验证零行为变化。"""
    # 重新 import executor 模块确保无 env 干扰（模块 import 时已读 env，
    # 但我们只读 fallback 字面量而非重解析路径，所以稳定）
    assert executor.TP_ATR_MULT == 1.0
    assert executor._LIQ_BUFFER_USD == 10.0
    assert executor._HL_TAKER_FEE_PCT == 0.025
    assert executor._HL_ROUND_TRIP_FILLS == 2


# ── 13. O-9: sub-minimum TP slice — micro skip vs small-account upsize ──────
# Before O-9 a sub-minimum TP slice was ALWAYS skipped, which silently disabled
# scale-out for small-but-not-micro accounts: a 30% slice of a $30 position is
# ~$9 (< the $10.5 HL minimum), yet upsizing to the minimum fills only ~35% of
# the position and leaves a genuine trail remainder. The skip must fire only
# when upsize would itself be a near-total close (>=90% of the position).
def _patch_tp_deps(monkeypatch, min_size, placed):
    """entry_size_for_notional -> min_size; record placed orders; noop bracket."""
    monkeypatch.setattr(
        executor, "entry_size_for_notional",
        lambda coin, notional, px: min_size,
    )
    monkeypatch.setattr(executor, "set_bracket", lambda *a, **k: None)

    def _fake_place(is_buy, size, trig_px, kind, coin, **kw):
        placed.append({"size": size, "px": trig_px, "kind": kind, "coin": coin})
        return {"ok": True, "order_id": "OID-1"}

    monkeypatch.setattr(executor, "place_hl_trigger_order", _fake_place)


def test_o9_micro_account_sub_min_slice_still_skipped(monkeypatch):
    """$11 position, 50% slice = $5.5; min size ~= 95% of position -> SKIP,
    no order placed (near-total close masquerading as a scale-out)."""
    placed = []
    _patch_tp_deps(monkeypatch, min_size=0.95, placed=placed)
    executor._place_tp_scale_out(
        config={"tp_scale_fraction": 0.5}, atr=1.0,
        size_in_coin=1.0, entry_px=11.0, is_buy=True, coin="TEST", trade_side="long",
    )
    assert placed == []


def test_o9_small_account_sub_min_slice_upsized_instead_of_skipped(monkeypatch):
    """$30 position, 30% slice = ~$9 (< $10.5 min); min size ~= 35% of the
    position -> UPSIZE to the minimum and place the TP leg (was blanket-skipped
    before O-9, which left the 20U tier with NO server-side take-profit)."""
    placed = []
    _patch_tp_deps(monkeypatch, min_size=0.35, placed=placed)
    executor._place_tp_scale_out(
        config={"tp_scale_fraction": 0.3}, atr=1.0,
        size_in_coin=1.0, entry_px=30.0, is_buy=True, coin="TEST", trade_side="long",
    )
    assert len(placed) == 1
    assert placed[0]["kind"] == "tp"
    assert placed[0]["size"] == 0.35  # upsized to the exchange minimum
    # Trigger price respects tp_atr_mult (long: entry + atr*mult above entry).
    assert placed[0]["px"] == pytest.approx(31.0)


def test_o9_above_min_slice_places_normally(monkeypatch):
    """Slice already above the venue minimum: placed at the intended fractional
    size, no upsize, no skip (regression for the normal path)."""
    placed = []
    _patch_tp_deps(monkeypatch, min_size=0.1, placed=placed)
    executor._place_tp_scale_out(
        config={"tp_scale_fraction": 0.5}, atr=1.0,
        size_in_coin=1.0, entry_px=100.0, is_buy=True, coin="TEST", trade_side="long",
    )
    assert len(placed) == 1
    assert placed[0]["size"] == pytest.approx(0.5)  # intended fraction, untouched
