"""Roadmap §3: market-level extreme-conditions circuit breaker (market_circuit).

覆盖四层：
  A. 配置登记——canonical 14 叶块、cfg_get/env 通道、深合并、strict schema
     （mode 三值接受 / 非法值与越界与未知叶拒绝）、_ConfigPatch drift sentinel；
  B. 纯判决——window_drawdown_pct 边界、decide 三触发器（index_crash /
     stop_cluster / funding_extreme）各自 trip/不 trip/禁用、data_ok fail-open；
  C. I/O 评估器 evaluate——off 不触网、shadow would_trip 不 arm halt 但写
     JSONL、enforce arm set_global_halt + notify + event、冷却去重、
     funding 默认不取、数据全失败 fail-open 永不 arm；
  D. trading_loop 接线——market_circuit_tick 薄包装（AST 提取，loop 模块
     不可导入）的 off/None、enforce 当 tick 全平守卫（equity>0、auto_flatten
     开关、空仓），以及源码接线断言。
"""

import ast
import json
import logging
from pathlib import Path
from types import SimpleNamespace

from hermes_trader.agents import config_store
from hermes_trader.agents import market_circuit as mc
from hermes_trader.agents.config_schema import _ConfigPatch, validate_config_updates
from hermes_trader.agents.config_store import (
    CANONICAL_DEFAULTS,
    cfg_get,
    read_agent_config,
)

BLOCK = "market_circuit"
LEAVES = (
    "mode",
    "index_crash_enabled",
    "index_crash_interval",
    "index_crash_pct",
    "index_crash_window_bars",
    "stop_cluster_enabled",
    "stop_cluster_window_s",
    "stop_cluster_min_coins",
    "funding_enabled",
    "funding_extreme_frac",
    "halt_minutes",
    "cooldown_minutes",
    "fetch_bars",
    "shadow_log_path",
)

REPO_ROOT = Path(__file__).resolve().parents[1]
TRADING_LOOP_SRC = (REPO_ROOT / "scripts" / "trading_loop.py").read_text(encoding="utf-8")


def _candle(h, c):
    """Minimal closed-bar stand-in (window_drawdown_pct only reads .h/.c)."""
    return SimpleNamespace(h=float(h), c=float(c))


def _crash_candles(peak=100.0, low=97.0):
    """A spike-then-fall leg: peak high then a close ~3% under it."""
    return [_candle(peak, peak), _candle(peak, peak - 1.0),
            _candle(low + 1.0, low)]


def _calm_candles(price=100.0):
    return [_candle(price, price), _candle(price + 0.1, price),
            _candle(price + 0.1, price - 0.1)]


def _passthrough_filter(raw, interval):
    return raw, False


class _FakeMem:
    def __init__(self, remaining_min=0.0):
        self.remaining_min = remaining_min
        self.armed_until = None
        self.arm_calls = 0

    def global_halt_remaining_min(self):
        return self.remaining_min

    def set_global_halt(self, until_ms):
        self.armed_until = until_ms
        self.arm_calls += 1


# ══════════════════════════════════════════════════════════════════════════
# A. 配置登记
# ══════════════════════════════════════════════════════════════════════════
def test_mc_block_registered_with_14_leaves():
    assert BLOCK in CANONICAL_DEFAULTS
    blk = CANONICAL_DEFAULTS[BLOCK]
    assert isinstance(blk, dict)
    assert len(blk) == 14
    assert set(blk) == set(LEAVES)


def test_mc_default_values_sentinel():
    """锁死默认值：mode 默认 off（roadmap 硬约束），funding 默认关。"""
    b = CANONICAL_DEFAULTS[BLOCK]
    assert b["mode"] == "off"                    # 必须显式武装
    assert b["index_crash_enabled"] is True
    assert b["index_crash_interval"] == "5m"
    assert b["index_crash_pct"] == 2.0
    assert b["index_crash_window_bars"] == 3
    assert b["stop_cluster_enabled"] is True
    assert b["stop_cluster_window_s"] == 180.0
    assert b["stop_cluster_min_coins"] == 3
    assert b["funding_enabled"] is False         # 资金费率触发器默认关
    assert b["funding_extreme_frac"] == 0.005
    assert b["halt_minutes"] == 60.0
    assert b["cooldown_minutes"] == 60.0
    assert b["fetch_bars"] == 20
    assert b["shadow_log_path"] == ""


def test_mc_leaf_types_sentinel():
    b = CANONICAL_DEFAULTS[BLOCK]
    for leaf in ("index_crash_window_bars", "stop_cluster_min_coins", "fetch_bars"):
        assert isinstance(b[leaf], int) and not isinstance(b[leaf], bool), leaf
    for leaf in ("index_crash_pct", "stop_cluster_window_s", "funding_extreme_frac",
                 "halt_minutes", "cooldown_minutes"):
        assert isinstance(b[leaf], float), leaf
    for leaf in ("index_crash_enabled", "stop_cluster_enabled", "funding_enabled"):
        assert isinstance(b[leaf], bool), leaf
    assert isinstance(b["mode"], str) and isinstance(b["index_crash_interval"], str)


def test_mc_cfg_get_falls_back_to_canonical():
    for leaf in LEAVES:
        assert cfg_get(f"{BLOCK}.{leaf}", config={}) == CANONICAL_DEFAULTS[BLOCK][leaf]


def test_mc_cfg_env_override(monkeypatch):
    monkeypatch.setenv("HERMES_CFG_MARKET_CIRCUIT__MODE", "shadow")
    monkeypatch.setenv("HERMES_CFG_MARKET_CIRCUIT__INDEX_CRASH_PCT", "3.5")
    assert cfg_get(f"{BLOCK}.mode", config={}) == "shadow"
    v = cfg_get(f"{BLOCK}.index_crash_pct", config={})
    assert v == 3.5 and isinstance(v, float)


def test_mc_read_agent_config_exposes_and_deep_merges(tmp_path, monkeypatch):
    cfg_file = tmp_path / ".agent-config.json"
    cfg_file.write_text(json.dumps(
        {BLOCK: {"mode": "enforce", "stop_cluster_min_coins": 4}}))
    monkeypatch.setattr(config_store, "CONFIG_PATH", str(cfg_file))
    monkeypatch.setattr(config_store, "_CONFIG_LOCK_PATH", str(cfg_file) + ".lock")

    cfg = read_agent_config()
    assert cfg[BLOCK]["mode"] == "enforce"               # 覆盖
    assert cfg[BLOCK]["stop_cluster_min_coins"] == 4     # 覆盖
    assert cfg[BLOCK]["index_crash_pct"] == 2.0          # canonical 保留
    assert cfg[BLOCK]["funding_enabled"] is False


def test_mc_schema_accepts_valid_updates():
    errors = validate_config_updates(
        {BLOCK: {"mode": "enforce", "index_crash_pct": 3.0,
                 "funding_enabled": True, "fetch_bars": 30}},
        strict_keys=True)
    assert errors == [], errors


def test_mc_schema_rejects_bad_mode():
    errors = validate_config_updates({BLOCK: {"mode": "bogus"}}, strict_keys=True)
    assert any("mode" in e for e in errors), errors


def test_mc_schema_rejects_out_of_range_and_unknown_leaf():
    errors = validate_config_updates(
        {BLOCK: {"index_crash_pct": 999.0}}, strict_keys=True)
    assert errors, errors
    errors = validate_config_updates(
        {BLOCK: {"not_a_leaf": True}}, strict_keys=True)
    assert any("unknown" in e for e in errors), errors


def test_mc_config_patch_drift_sentinel():
    field = _ConfigPatch.model_fields.get(BLOCK)
    assert field is not None
    blk = field.default_factory()
    assert len(blk) == 14
    assert blk["mode"] == "off"
    assert blk["index_crash_pct"] == 2.0


# ══════════════════════════════════════════════════════════════════════════
# B. 纯判决
# ══════════════════════════════════════════════════════════════════════════
def test_window_drawdown_basic_and_edges():
    # spike to 101, last close 98 → -2.97%
    dd = mc.window_drawdown_pct(
        [_candle(100, 100), _candle(101, 100), _candle(100, 98)], 3)
    assert dd is not None and abs(dd - (98 - 101) / 101 * 100.0) < 1e-9
    # window slicing: only the last N bars count
    dd2 = mc.window_drawdown_pct(
        [_candle(200, 200), _candle(100, 100), _candle(100, 99)], 2)
    assert dd2 is not None and dd2 > -2.0           # 200 spike outside window
    # not enough data / bad args / bad prices
    assert mc.window_drawdown_pct([], 3) is None
    assert mc.window_drawdown_pct([_candle(100, 100)], 3) is None
    assert mc.window_drawdown_pct(_calm_candles(), 0) is None
    assert mc.window_drawdown_pct([_candle(0, 0), _candle(0, 0)], 2) is None


def test_decide_index_crash_trips_on_deep_leg():
    v = mc.decide({}, index_drawdowns={"BTC": -3.0, "ETH": -0.5})
    assert v["tripped"] and v["trigger"] == "index_crash"
    assert v["details"]["crash_coin"] == "BTC"
    # deepest coin wins
    v2 = mc.decide({}, index_drawdowns={"BTC": -0.5, "ETH": -2.5})
    assert v2["details"]["crash_coin"] == "ETH"


def test_decide_index_crash_threshold_and_disable():
    assert not mc.decide({}, index_drawdowns={"BTC": -1.0})["tripped"]
    # exactly at threshold trips (dd <= -threshold)
    assert mc.decide({}, index_drawdowns={"BTC": -2.0})["tripped"]
    v = mc.decide({"index_crash_enabled": False},
                  index_drawdowns={"BTC": -9.0})
    assert not v["tripped"]


def test_decide_stop_cluster_trips_on_three_distinct_coins():
    now = 1_000_000
    evts = [{"ts_ms": now - 1000, "coin": "BTC"},
            {"ts_ms": now - 2000, "coin": "ETH"},
            {"ts_ms": now - 3000, "coin": "SOL"}]
    v = mc.decide({}, index_drawdowns={}, stop_events=evts, now_ms=now)
    assert v["tripped"] and v["trigger"] == "stop_cluster"
    assert v["details"]["stop_cluster_count"] == 3


def test_decide_stop_cluster_dedup_window_and_disable():
    now = 1_000_000
    # same coin three times = one distinct coin
    dup = [{"ts_ms": now, "coin": "BTC"}] * 3
    assert not mc.decide({}, index_drawdowns={},
                         stop_events=dup, now_ms=now)["tripped"]
    # two coins only → below min 3
    two = [{"ts_ms": now, "coin": "BTC"}, {"ts_ms": now, "coin": "ETH"}]
    assert not mc.decide({}, index_drawdowns={},
                         stop_events=two, now_ms=now)["tripped"]
    # an event older than the 180s window is excluded
    old = two + [{"ts_ms": now - 200_000, "coin": "SOL"}]
    assert not mc.decide({}, index_drawdowns={},
                         stop_events=old, now_ms=now)["tripped"]
    # disabled → never trips
    assert not mc.decide({"stop_cluster_enabled": False}, index_drawdowns={},
                         stop_events=[{"ts_ms": now, "coin": c}
                                      for c in ("BTC", "ETH", "SOL")],
                         now_ms=now)["tripped"]


def test_decide_funding_extreme_opt_in():
    # default disabled → even extreme rates do not trip
    v_off = mc.decide({}, index_drawdowns={},
                      funding_rates={"BTC": 0.01, "ETH": None})
    assert not v_off["tripped"]
    v = mc.decide({"funding_enabled": True}, index_drawdowns={},
                  funding_rates={"BTC": 0.006, "ETH": 0.0001})
    assert v["tripped"] and v["trigger"] == "funding_extreme"
    v2 = mc.decide({"funding_enabled": True}, index_drawdowns={},
                   funding_rates={"BTC": 0.001, "ETH": -0.001})
    assert not v2["tripped"]


def test_decide_data_ok_fail_open():
    # no index data, no stop data, no funding → data_ok False (caller fails open)
    v = mc.decide({}, index_drawdowns={"BTC": None, "ETH": None},
                  stop_events=None, funding_rates={})
    assert not v["tripped"] and v["data_ok"] is False
    # an empty stop window is valid "no stops" data → data_ok True
    v2 = mc.decide({}, index_drawdowns={}, stop_events=[], funding_rates={})
    assert v2["data_ok"] is True


def test_record_stop_feeds_module_window():
    mc.record_stop("TESTCOIN-MC")
    assert any(e["coin"] == "TESTCOIN-MC" for e in mc._stop_window.events())


# ══════════════════════════════════════════════════════════════════════════
# C. evaluate —— off/shadow/enforce、冷却、fail-open、shadow JSONL
# ══════════════════════════════════════════════════════════════════════════
def _eval_cfg(tmp_path, **over):
    cfg = {"mode": "shadow", "halt_minutes": 30.0, "cooldown_minutes": 60.0,
           "shadow_log_path": str(tmp_path / "mc_shadow.jsonl")}
    cfg.update(over)
    return cfg


def _crash_fetcher(*_a, **_k):
    return _crash_candles()


def _calm_fetcher(*_a, **_k):
    return _calm_candles()


def _failing_fetcher(*_a, **_k):
    raise RuntimeError("candle API down")


def _read_shadow(path):
    p = Path(path)
    assert p.exists()
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines()]


def test_evaluate_off_never_touches_network():
    def _boom(*_a, **_k):
        raise AssertionError("off mode must not fetch")

    v = mc.evaluate({"mode": "off"}, mem=_FakeMem(),
                    candle_fetcher=_boom, closed_filter=_passthrough_filter,
                    stop_events=[])
    assert v["action"] == "off"


def test_evaluate_invalid_mode_falls_back_to_off():
    v = mc.evaluate({"mode": "bogus"}, mem=_FakeMem(),
                    candle_fetcher=_crash_fetcher,
                    closed_filter=_passthrough_filter, stop_events=[])
    assert v["action"] == "off"


def test_evaluate_shadow_trip_records_but_never_arms(tmp_path):
    mem = _FakeMem()
    notified = []
    v = mc.evaluate(
        _eval_cfg(tmp_path, mode="shadow"), mem=mem,
        candle_fetcher=_crash_fetcher, closed_filter=_passthrough_filter,
        notifier=lambda **k: notified.append(k), event_log=lambda _e: None,
        stop_events=[])
    assert v["action"] == "would_trip" and v["tripped"]
    assert mem.arm_calls == 0                          # shadow 绝不 arm halt
    assert notified == []                              # shadow 不告警
    recs = _read_shadow(_eval_cfg(tmp_path)["shadow_log_path"])
    assert recs and recs[-1]["tripped"] and recs[-1]["mode"] == "shadow"
    assert all(r.get("action") != "halt_armed" for r in recs)


def test_evaluate_enforce_trip_arms_halt_alerts_and_logs(tmp_path):
    cfg = _eval_cfg(tmp_path, mode="enforce")
    mem = _FakeMem(remaining_min=0.0)
    notified = []
    events = []
    v = mc.evaluate(
        cfg, mem=mem,
        candle_fetcher=_crash_fetcher, closed_filter=_passthrough_filter,
        notifier=lambda **k: notified.append(k),
        event_log=lambda e: events.append(e), stop_events=[])
    assert v["action"] == "halt_armed" and v.get("armed") is True
    assert mem.arm_calls == 1
    # halt window ≈ 30 min from now
    import time as _t
    assert abs(mem.armed_until - int(_t.time() * 1000) - 30 * 60_000) < 60_000
    assert notified and notified[0]["trigger"] == "index_crash"
    assert any(e.get("event") == "market_circuit_halt" for e in events)
    recs = _read_shadow(cfg["shadow_log_path"])
    assert recs[-1]["action"] == "halt_armed" and recs[-1]["armed"] is True


def test_evaluate_clear_writes_nothing(tmp_path):
    v = mc.evaluate(
        _eval_cfg(tmp_path, mode="shadow"), mem=_FakeMem(),
        candle_fetcher=_calm_fetcher, closed_filter=_passthrough_filter,
        stop_events=[])
    assert v["action"] == "clear" and not v["tripped"]
    assert not Path(_eval_cfg(tmp_path)["shadow_log_path"]).exists()


def test_evaluate_cooldown_dedups_sustained_crash(tmp_path):
    mem = _FakeMem(remaining_min=600.0)              # a halt is already armed
    notified = []
    v = mc.evaluate(
        _eval_cfg(tmp_path, mode="enforce"), mem=mem,
        candle_fetcher=_crash_fetcher, closed_filter=_passthrough_filter,
        notifier=lambda **k: notified.append(k), event_log=lambda _e: None,
        stop_events=[])
    assert v["action"] == "halt_already_armed"
    assert mem.arm_calls == 0 and notified == []     # 不重复 arm / 不刷屏告警


def test_evaluate_total_data_failure_fails_open(tmp_path):
    mem = _FakeMem()
    v = mc.evaluate(
        _eval_cfg(tmp_path, mode="enforce"), mem=mem,
        candle_fetcher=_failing_fetcher,
        closed_filter=_passthrough_filter,
        notifier=lambda **k: (_ for _ in ()).throw(AssertionError("no alert")),
        event_log=lambda _e: (_ for _ in ()).throw(AssertionError("no event")),
        stop_events=[])
    # 所有指数 candle 失败 → 无 trip；额外否决层永不 arm halt
    assert not v["tripped"] and v["action"] in ("clear", "data_missing")
    assert mem.arm_calls == 0


def test_evaluate_funding_disabled_by_default_skips_fetcher(tmp_path):
    def _funding_boom(_coin):
        raise AssertionError("funding must not be fetched when disabled")

    v = mc.evaluate(
        _eval_cfg(tmp_path, mode="shadow"), mem=_FakeMem(),
        candle_fetcher=_calm_fetcher, closed_filter=_passthrough_filter,
        funding_fetcher=_funding_boom, stop_events=[])
    assert v["action"] == "clear"


def test_evaluate_funding_enabled_trips(tmp_path):
    mem = _FakeMem()
    v = mc.evaluate(
        _eval_cfg(tmp_path, mode="enforce", funding_enabled=True), mem=mem,
        candle_fetcher=_calm_fetcher, closed_filter=_passthrough_filter,
        funding_fetcher=lambda _c: 0.01, stop_events=[])
    assert v["tripped"] and v["trigger"] == "funding_extreme"
    assert v["action"] == "halt_armed" and mem.arm_calls == 1


def test_evaluate_never_raises_on_broken_callbacks(tmp_path):
    def _notifier_boom(**_k):
        raise RuntimeError("notify down")

    def _event_boom(_e):
        raise RuntimeError("event down")

    v = mc.evaluate(
        _eval_cfg(tmp_path, mode="enforce"), mem=_FakeMem(),
        candle_fetcher=_crash_fetcher, closed_filter=_passthrough_filter,
        notifier=_notifier_boom, event_log=_event_boom, stop_events=[])
    assert v["action"] == "halt_armed"               # halt 仍 arm，告警失败不炸


def test_shadow_log_path_resolution(tmp_path, monkeypatch):
    custom = tmp_path / "custom.jsonl"
    assert mc.shadow_log_path({"shadow_log_path": str(custom)}) == str(custom)
    monkeypatch.setenv("HERMES_MARKET_CIRCUIT_SHADOW_FILE",
                       str(tmp_path / "env.jsonl"))
    assert mc.shadow_log_path({}) == str(tmp_path / "env.jsonl")
    monkeypatch.delenv("HERMES_MARKET_CIRCUIT_SHADOW_FILE")
    assert mc.shadow_log_path({}).endswith("market_circuit_shadow.jsonl")


# ══════════════════════════════════════════════════════════════════════════
# D. trading_loop 接线（market_circuit_tick 薄包装 + 源码断言）
# ══════════════════════════════════════════════════════════════════════════
def _load_tick():
    """AST-extract market_circuit_tick (importing trading_loop runs its
    module-level while-True loop; same pattern as test_audit_p1 _load_loop_fn)."""
    tree = ast.parse(TRADING_LOOP_SRC)
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
              and n.name == "market_circuit_tick")
    ns = {
        "cfg_get": cfg_get,
        "logger": logging.getLogger("test.mc_tick"),
        "close_position_market": lambda *_a, **_k: {"ok": True},
        "log_event": lambda *_a, **_k: None,
        "market_circuit_evaluate": lambda *_a, **_k: {"action": "off"},
        "_market_circuit_funding": lambda *_a, **_k: None,
    }
    module = ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[]))
    exec(compile(module, "<trading_loop extracted>", "exec"), ns)
    return ns["market_circuit_tick"]


def test_tick_absent_block_or_off_returns_none():
    tick = _load_tick()
    assert tick({}, _FakeMem(), 1000.0, []) is None
    assert tick({"market_circuit": {"mode": "off"}}, _FakeMem(), 1000.0, []) is None


def test_tick_enforce_halt_flattens_same_tick():
    tick = _load_tick()
    flattened = []
    verdict = {"action": "halt_armed"}
    fn = lambda *a, **k: verdict
    out = tick(
        {"market_circuit": {"mode": "enforce"},
         "auto_flatten_on_global_halt": True},
        _FakeMem(), 1000.0,
        [{"position": {"coin": "BTC"}}, {"position": {"coin": "ETH"}}],
        evaluator=fn,
        flattener=lambda c: flattened.append(c) or {"ok": True})
    assert out is verdict
    assert flattened == ["BTC", "ETH"]


def test_tick_flatten_guards():
    tick = _load_tick()
    verdict = {"action": "halt_armed"}
    ev = lambda *a, **k: verdict
    # equity <= 0 (degraded read) → never flatten
    flat = []
    tick({"market_circuit": {"mode": "enforce"},
          "auto_flatten_on_global_halt": True},
         _FakeMem(), 0.0, [{"position": {"coin": "BTC"}}],
         evaluator=ev, flattener=lambda c: flat.append(c) or {"ok": True})
    assert flat == []
    # auto_flatten explicitly off → never flatten
    tick({"market_circuit": {"mode": "enforce"},
          "auto_flatten_on_global_halt": False},
         _FakeMem(), 1000.0, [{"position": {"coin": "BTC"}}],
         evaluator=ev, flattener=lambda c: flat.append(c) or {"ok": True})
    assert flat == []
    # no open positions → nothing to flatten
    tick({"market_circuit": {"mode": "enforce"},
          "auto_flatten_on_global_halt": True},
         _FakeMem(), 1000.0, [],
         evaluator=ev, flattener=lambda c: flat.append(c) or {"ok": True})
    assert flat == []
    # non-halt verdict → no flatten
    tick({"market_circuit": {"mode": "shadow"},
          "auto_flatten_on_global_halt": True},
         _FakeMem(), 1000.0, [{"position": {"coin": "BTC"}}],
         evaluator=lambda *a, **k: {"action": "would_trip"},
         flattener=lambda c: flat.append(c) or {"ok": True})
    assert flat == []


def test_trading_loop_source_wiring():
    assert ("from hermes_trader.agents.market_circuit "
            "import evaluate as market_circuit_evaluate") in TRADING_LOOP_SRC
    assert ("from hermes_trader.agents.market_circuit "
            "import record_stop as market_circuit_record_stop") in TRADING_LOOP_SRC
    # DSL close path feeds the stop-cluster window
    assert "market_circuit_record_stop(coin)" in TRADING_LOOP_SRC
    # main loop runs one tick per cycle after the exit pass
    assert "market_circuit_tick(_cfg, memory, equity, positions)" in TRADING_LOOP_SRC
