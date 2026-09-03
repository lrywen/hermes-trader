"""R13-B11: memory equity 质量门旋钮 canonical 登记测试。

修复前，equity 数据质量门控的十一个旋钮散落在两个模块里：

  * memory.py 写入门（track_daily_pnl partial-dex degraded-read 过滤）——
    不可信摆动阈值是裸局部字面量 ``_IMPLAUSIBLE_PCT = 0.25``、同读数过滤窗
    是 ``(now_s - prev_ts) < 180``、再确认次数是 ``streak < 2``；立即接受的
    崩盘阈值经 ``os.environ.get("HERMES_EQUITY_CRASH_DOWN_PCT", "0.40")``
    读取；
  * memory.py 读取门（avg_exit_slip_bps）——回看窗 / 最少样本是函数签名默认值
    ``days=30.0`` / ``min_samples=3``；非强制 flush 节流经模块级
    ``os.environ.get("HERMES_MEMORY_FLUSH_THROTTLE_S", "0.2")`` 读取；
  * dashboard.py 读侧门（_equity_curve_payload dip 打标 / _summary_payload
    心跳陈旧判定 / _closed_trades_payload 跨源去重）——dip 比率与 trailing
    窗经模块级 HERMES_EQUITY_DIP_RATIO(0.7) / HERMES_EQUITY_DIP_WINDOW(15)
    读取（且被 tests/test_cleanup.py monkeypatch.setattr 补丁），陈旧阈值是
    裸字面量 ``> 180``，去重窗经 HERMES_CLOSED_TRADES_DEDUP_MS(5000) 读取。

它们既不进 CANONICAL_DEFAULTS（dashboard dump 看不见、validate_config
不校验），也没有 agent-config / HERMES_CFG_ 通道。

修复后（零行为变化）：
  * 新增 canonical 块 ``memory_quality``（7 叶）与 ``dashboard_equity``
    （4 叶），默认值逐字镜像旧字面量；
  * memory.py 新增 ``_memory_quality_params()`` helper、dashboard.py 新增
    ``_dashboard_equity_params()`` / ``_dashboard_equity_defaults()``：逐叶
    解析链 = legacy env（HERMES_EQUITY_CRASH_DOWN_PCT /
    HERMES_MEMORY_FLUSH_THROTTLE_S / HERMES_EQUITY_DIP_RATIO /
    HERMES_EQUITY_DIP_WINDOW / HERMES_CLOSED_TRADES_DEDUP_MS，**最高优先**）
    → cfg_get("memory_quality|dashboard_equity.<leaf>")（HERMES_CFG_* env +
    agent-config + CANONICAL_DEFAULTS）→ 字面量；int/float coerce 且须过
    最小 guard；任何失败整块回退字面量独立拷贝，热路径绝不抛错；
  * dashboard 的 _EQUITY_DIP_RATIO / _EQUITY_DIP_WINDOW 模块属性保留
    （test_dashboard_config_api 钉死默认值；test_cleanup 用
    monkeypatch.setattr 改行为），helper 的字面量回退层在调用时经 globals()
    读活全局，setattr 补丁继续生效；
  * avg_exit_slip_bps 的 days / min_samples 形参改 Optional[None]，None 时
    从 helper 解析（executor 显式传 days=30.0 不受影响）。

明确排除：HERMES_AGENT_MEMORY_FILE / HERMES_EVENTS_FILE（文件路径，conftest
强制 setenv）、memory_limits 块（P2-3 已登记）、public.py 三个 HTTP 缓存
TTL（_SUMMARY/_EQUITY_CURVE/_CLOSED_TRADES TTL，纯 HTTP 缓存属 B12）、
equity-curve Query 边界（ge=60/le=2_592_000，属 API 形状）。
"""

import inspect
import json
import os

import pytest

import hermes_trader.agents.memory as memory_mod
import hermes_trader.dashboard as dashboard_mod
from hermes_trader.agents import config_store
from hermes_trader.agents.config_schema import _ConfigPatch, validate_config_updates
from hermes_trader.agents.config_store import CANONICAL_DEFAULTS, cfg_get, read_agent_config
from hermes_trader.agents.memory import (
    _MEMORY_QUALITY_DEFAULTS,
    _MEMORY_QUALITY_SPEC,
    _memory_quality_params,
)
from hermes_trader.dashboard import (
    _DASHBOARD_EQUITY_SPEC,
    _dashboard_equity_defaults,
    _dashboard_equity_params,
)

MQ_BLOCK = "memory_quality"
DE_BLOCK = "dashboard_equity"
MQ_LEAVES = (
    "implausible_pct", "crash_down_pct", "filter_window_sec",
    "reconfirm_streak", "slip_window_days", "slip_min_samples",
    "flush_throttle_s",
)
DE_LEAVES = ("dip_ratio", "dip_window", "stale_tick_age_s", "dedup_window_ms")

# 两块的全部 legacy env（helper 默认值测试前需清空，防宿主环境污染）。
_LEGACY_ENVS = (
    "HERMES_EQUITY_CRASH_DOWN_PCT",
    "HERMES_MEMORY_FLUSH_THROTTLE_S",
    "HERMES_EQUITY_DIP_RATIO",
    "HERMES_EQUITY_DIP_WINDOW",
    "HERMES_CLOSED_TRADES_DEDUP_MS",
)


def _clear_b11_env(monkeypatch):
    """清空两块全部 legacy env + HERMES_CFG_MEMORY_QUALITY__* /
    HERMES_CFG_DASHBOARD_EQUITY__* canonical env。"""
    for v in _LEGACY_ENVS:
        monkeypatch.delenv(v, raising=False)
    for k in list(os.environ):
        if k.startswith("HERMES_CFG_MEMORY_QUALITY__") or \
           k.startswith("HERMES_CFG_DASHBOARD_EQUITY__"):
            monkeypatch.delenv(k, raising=False)


# ── canonical 登记：两块存在、叶数正确、与模块字面量表逐字一致 ────────────

def test_r13_b11_memory_quality_block_registered():
    assert MQ_BLOCK in CANONICAL_DEFAULTS
    assert isinstance(CANONICAL_DEFAULTS[MQ_BLOCK], dict)
    assert len(CANONICAL_DEFAULTS[MQ_BLOCK]) == 7
    assert set(CANONICAL_DEFAULTS[MQ_BLOCK]) == set(MQ_LEAVES)


def test_r13_b11_dashboard_equity_block_registered():
    assert DE_BLOCK in CANONICAL_DEFAULTS
    assert isinstance(CANONICAL_DEFAULTS[DE_BLOCK], dict)
    assert len(CANONICAL_DEFAULTS[DE_BLOCK]) == 4
    assert set(CANONICAL_DEFAULTS[DE_BLOCK]) == set(DE_LEAVES)


def test_r13_b11_canonical_mq_defaults_mirror_memory_literals():
    assert CANONICAL_DEFAULTS[MQ_BLOCK] == _MEMORY_QUALITY_DEFAULTS


def test_r13_b11_individual_mq_leaf_values_sentinel():
    """逐叶 sentinel：锁死 7 个 memory 质量门默认值（零行为变化基线）。"""
    b = CANONICAL_DEFAULTS[MQ_BLOCK]
    assert b["implausible_pct"] == 0.25
    assert b["crash_down_pct"] == 0.40
    assert b["filter_window_sec"] == 180
    assert b["reconfirm_streak"] == 2
    assert b["slip_window_days"] == 30.0
    assert b["slip_min_samples"] == 3
    assert b["flush_throttle_s"] == 0.2


def test_r13_b11_individual_de_leaf_values_sentinel():
    """逐叶 sentinel：锁死 4 个 dashboard 读侧门默认值。"""
    b = CANONICAL_DEFAULTS[DE_BLOCK]
    assert b["dip_ratio"] == 0.7
    assert b["dip_window"] == 15
    assert b["stale_tick_age_s"] == 180
    assert b["dedup_window_ms"] == 5000


def test_r13_b11_leaf_types_match_literals():
    """类型 sentinel：int 槽位保持 int、float 保持 float。"""
    mb = CANONICAL_DEFAULTS[MQ_BLOCK]
    for leaf in ("implausible_pct", "crash_down_pct",
                 "slip_window_days", "flush_throttle_s"):
        assert isinstance(mb[leaf], float), leaf
    for leaf in ("filter_window_sec", "reconfirm_streak", "slip_min_samples"):
        assert isinstance(mb[leaf], int), leaf
    db = CANONICAL_DEFAULTS[DE_BLOCK]
    assert isinstance(db["dip_ratio"], float)
    for leaf in ("dip_window", "stale_tick_age_s", "dedup_window_ms"):
        assert isinstance(db[leaf], int), leaf


# ── cfg_get 点路径 / 整块：空 config 回退 canonical ───────────────────────

def test_r13_b11_cfg_get_all_mq_leaves():
    for leaf in MQ_LEAVES:
        assert cfg_get(f"{MQ_BLOCK}.{leaf}", config={}) == _MEMORY_QUALITY_DEFAULTS[leaf]


def test_r13_b11_cfg_get_all_de_leaves():
    for leaf in DE_LEAVES:
        assert cfg_get(f"{DE_BLOCK}.{leaf}", config={}) == CANONICAL_DEFAULTS[DE_BLOCK][leaf]


def test_r13_b11_cfg_get_full_blocks():
    mb = cfg_get(MQ_BLOCK, config={})
    assert isinstance(mb, dict) and len(mb) == 7
    assert mb["implausible_pct"] == 0.25 and mb["reconfirm_streak"] == 2
    db = cfg_get(DE_BLOCK, config={})
    assert isinstance(db, dict) and len(db) == 4
    assert db["dip_ratio"] == 0.7 and db["dedup_window_ms"] == 5000


# ── HERMES_CFG_ canonical env 通道（含 int/float coerce）──────────────────

def test_r13_b11_cfg_env_override_mq_int(monkeypatch):
    monkeypatch.setenv("HERMES_CFG_MEMORY_QUALITY__RECONFIRM_STREAK", "4")
    v = cfg_get(f"{MQ_BLOCK}.reconfirm_streak", config={})
    assert v == 4 and isinstance(v, int)


def test_r13_b11_cfg_env_override_mq_float(monkeypatch):
    monkeypatch.setenv("HERMES_CFG_MEMORY_QUALITY__IMPLAUSIBLE_PCT", "0.33")
    v = cfg_get(f"{MQ_BLOCK}.implausible_pct", config={})
    assert v == 0.33 and isinstance(v, float)


def test_r13_b11_cfg_env_override_de_int(monkeypatch):
    # stale_tick_age_s 没有 legacy env，canonical env 是唯一 env 通道
    monkeypatch.setenv("HERMES_CFG_DASHBOARD_EQUITY__STALE_TICK_AGE_S", "240")
    v = cfg_get(f"{DE_BLOCK}.stale_tick_age_s", config={})
    assert v == 240 and isinstance(v, int)


def test_r13_b11_cfg_env_override_de_float(monkeypatch):
    monkeypatch.setenv("HERMES_CFG_DASHBOARD_EQUITY__DIP_RATIO", "0.55")
    v = cfg_get(f"{DE_BLOCK}.dip_ratio", config={})
    assert v == 0.55 and isinstance(v, float)


# ── config dict 部分覆盖：未给叶保留 canonical ────────────────────────────

def test_r13_b11_config_dict_partial_overlay():
    cfg = {
        MQ_BLOCK: {"implausible_pct": 0.4, "filter_window_sec": 300},
        DE_BLOCK: {"dip_window": 30, "dedup_window_ms": 9000},
    }
    assert cfg_get(f"{MQ_BLOCK}.implausible_pct", config=cfg) == 0.4
    assert cfg_get(f"{MQ_BLOCK}.filter_window_sec", config=cfg) == 300
    assert cfg_get(f"{MQ_BLOCK}.crash_down_pct", config=cfg) == 0.40
    assert cfg_get(f"{MQ_BLOCK}.slip_min_samples", config=cfg) == 3
    assert cfg_get(f"{DE_BLOCK}.dip_window", config=cfg) == 30
    assert cfg_get(f"{DE_BLOCK}.dedup_window_ms", config=cfg) == 9000
    assert cfg_get(f"{DE_BLOCK}.dip_ratio", config=cfg) == 0.7
    assert cfg_get(f"{DE_BLOCK}.stale_tick_age_s", config=cfg) == 180


# ── read_agent_config 可见 + 深合并（dashboard dump 通道）─────────────────

def test_r13_b11_read_agent_config_exposes_blocks():
    cfg = read_agent_config()
    assert MQ_BLOCK in cfg and DE_BLOCK in cfg
    assert cfg[MQ_BLOCK]["implausible_pct"] == 0.25
    assert cfg[MQ_BLOCK]["flush_throttle_s"] == 0.2
    assert cfg[DE_BLOCK]["dip_ratio"] == 0.7
    assert cfg[DE_BLOCK]["dedup_window_ms"] == 5000


def test_r13_b11_read_agent_config_deep_merges_partial(tmp_path, monkeypatch):
    cfg_file = tmp_path / ".agent-config.json"
    cfg_file.write_text(json.dumps({
        MQ_BLOCK: {"reconfirm_streak": 5, "implausible_pct": 0.3},
        DE_BLOCK: {"stale_tick_age_s": 300},
    }))
    monkeypatch.setattr(config_store, "CONFIG_PATH", str(cfg_file))
    monkeypatch.setattr(config_store, "_CONFIG_LOCK_PATH", str(cfg_file) + ".lock")

    cfg = read_agent_config()
    assert cfg[MQ_BLOCK]["reconfirm_streak"] == 5
    assert cfg[MQ_BLOCK]["implausible_pct"] == 0.3
    assert cfg[MQ_BLOCK]["crash_down_pct"] == 0.40          # canonical 保留
    assert cfg[DE_BLOCK]["stale_tick_age_s"] == 300
    assert cfg[DE_BLOCK]["dip_window"] == 15                # canonical 保留


# ── schema：两块作为 object 被 strict_keys 接受；_ConfigPatch drift ───────

def test_r13_b11_schema_accepts_both_blocks():
    errors = validate_config_updates(
        {
            MQ_BLOCK: {"implausible_pct": 0.3, "reconfirm_streak": 3},
            DE_BLOCK: {"dip_window": 20, "dedup_window_ms": 7000},
        },
        strict_keys=True,
    )
    assert not any("unknown key" in e for e in errors), errors
    assert errors == [], errors


def test_r13_b11_config_patch_knows_both_blocks():
    """drift sentinel：CANONICAL_DEFAULTS 加块后 _ConfigPatch 必须同步声明。"""
    fields = _ConfigPatch.model_fields
    assert MQ_BLOCK in fields and DE_BLOCK in fields
    mb = fields[MQ_BLOCK].default_factory()
    assert len(mb) == 7 and mb["implausible_pct"] == 0.25 and mb["reconfirm_streak"] == 2
    db = fields[DE_BLOCK].default_factory()
    assert len(db) == 4 and db["dip_ratio"] == 0.7 and db["stale_tick_age_s"] == 180


# ── helper：默认叶集 == 字面量、SPEC 映射 / kind / guard 正确 ─────────────

def test_r13_b11_mq_helper_defaults_equal_literals(monkeypatch):
    _clear_b11_env(monkeypatch)
    p = _memory_quality_params()
    assert set(p) == set(MQ_LEAVES)
    assert p == _MEMORY_QUALITY_DEFAULTS


def test_r13_b11_de_helper_defaults_equal_literals(monkeypatch):
    _clear_b11_env(monkeypatch)
    p = _dashboard_equity_params(config={})
    assert set(p) == set(DE_LEAVES)
    assert p == CANONICAL_DEFAULTS[DE_BLOCK]


def test_r13_b11_spec_maps_legacy_envs():
    """memory_quality 2 叶、dashboard_equity 3 叶映射 legacy env；其余叶
    legacy=None（纯硬编码）。"""
    assert _MEMORY_QUALITY_SPEC["crash_down_pct"][0] == "HERMES_EQUITY_CRASH_DOWN_PCT"
    assert _MEMORY_QUALITY_SPEC["flush_throttle_s"][0] == "HERMES_MEMORY_FLUSH_THROTTLE_S"
    for leaf in ("implausible_pct", "filter_window_sec", "reconfirm_streak",
                 "slip_window_days", "slip_min_samples"):
        assert _MEMORY_QUALITY_SPEC[leaf][0] is None, leaf
    assert _DASHBOARD_EQUITY_SPEC["dip_ratio"][0] == "HERMES_EQUITY_DIP_RATIO"
    assert _DASHBOARD_EQUITY_SPEC["dip_window"][0] == "HERMES_EQUITY_DIP_WINDOW"
    assert _DASHBOARD_EQUITY_SPEC["dedup_window_ms"][0] == "HERMES_CLOSED_TRADES_DEDUP_MS"
    assert _DASHBOARD_EQUITY_SPEC["stale_tick_age_s"][0] is None


def test_r13_b11_spec_kinds_and_guards():
    """kind 列：窗口/次数/样本为 i；比率/天数/节流为 f。
    guard 列：reconfirm_streak / dip_window / slip_min_samples 下限 1；
    filter_window_sec / dedup_window_ms / 比率类下限 0。"""
    for leaf in ("filter_window_sec", "reconfirm_streak", "slip_min_samples"):
        assert _MEMORY_QUALITY_SPEC[leaf][1] == "i", leaf
    for leaf in ("implausible_pct", "crash_down_pct",
                 "slip_window_days", "flush_throttle_s"):
        assert _MEMORY_QUALITY_SPEC[leaf][1] == "f", leaf
    assert _MEMORY_QUALITY_SPEC["reconfirm_streak"][2] == 1
    assert _MEMORY_QUALITY_SPEC["slip_min_samples"][2] == 1
    assert _MEMORY_QUALITY_SPEC["filter_window_sec"][2] == 0
    assert _MEMORY_QUALITY_SPEC["implausible_pct"][2] == 0.0
    assert _DASHBOARD_EQUITY_SPEC["dip_ratio"][1] == "f"
    for leaf in ("dip_window", "stale_tick_age_s", "dedup_window_ms"):
        assert _DASHBOARD_EQUITY_SPEC[leaf][1] == "i", leaf
    assert _DASHBOARD_EQUITY_SPEC["dip_window"][2] == 1
    assert _DASHBOARD_EQUITY_SPEC["dedup_window_ms"][2] == 0
    assert _DASHBOARD_EQUITY_SPEC["stale_tick_age_s"][2] == 0


# ── legacy env 兼容（硬约束）：仍生效且优先于 canonical 通道 ──────────────

def test_r13_b11_legacy_crash_down_pct_flows(monkeypatch):
    _clear_b11_env(monkeypatch)
    monkeypatch.setenv("HERMES_EQUITY_CRASH_DOWN_PCT", "0.55")
    p = _memory_quality_params()
    assert p["crash_down_pct"] == 0.55


def test_r13_b11_legacy_flush_throttle_flows(monkeypatch):
    _clear_b11_env(monkeypatch)
    monkeypatch.setenv("HERMES_MEMORY_FLUSH_THROTTLE_S", "1.5")
    p = _memory_quality_params()
    assert p["flush_throttle_s"] == 1.5


def test_r13_b11_legacy_dip_ratio_and_window_flow(monkeypatch):
    _clear_b11_env(monkeypatch)
    monkeypatch.setenv("HERMES_EQUITY_DIP_RATIO", "0.6")
    monkeypatch.setenv("HERMES_EQUITY_DIP_WINDOW", "25")
    p = _dashboard_equity_params(config={})
    assert p["dip_ratio"] == 0.6
    assert p["dip_window"] == 25


def test_r13_b11_legacy_dedup_window_flows(monkeypatch):
    _clear_b11_env(monkeypatch)
    monkeypatch.setenv("HERMES_CLOSED_TRADES_DEDUP_MS", "8000")
    p = _dashboard_equity_params(config={})
    assert p["dedup_window_ms"] == 8000


def test_r13_b11_legacy_env_beats_canonical_env(monkeypatch):
    """legacy env 优先级最高：同时设置时压过 HERMES_CFG_*。"""
    _clear_b11_env(monkeypatch)
    monkeypatch.setenv("HERMES_EQUITY_CRASH_DOWN_PCT", "0.5")
    monkeypatch.setenv("HERMES_CFG_MEMORY_QUALITY__CRASH_DOWN_PCT", "0.2")
    monkeypatch.setenv("HERMES_EQUITY_DIP_WINDOW", "40")
    monkeypatch.setenv("HERMES_CFG_DASHBOARD_EQUITY__DIP_WINDOW", "9")
    mp = _memory_quality_params()
    dp = _dashboard_equity_params(config={})
    assert mp["crash_down_pct"] == 0.5
    assert dp["dip_window"] == 40


def test_r13_b11_legacy_env_empty_string_falls_through(monkeypatch):
    """空串视为未设：落到 canonical 通道而非崩溃 / 透传空串。"""
    _clear_b11_env(monkeypatch)
    monkeypatch.setenv("HERMES_EQUITY_CRASH_DOWN_PCT", "")
    monkeypatch.setenv("HERMES_CFG_MEMORY_QUALITY__FILTER_WINDOW_SEC", "240")
    monkeypatch.setenv("HERMES_CLOSED_TRADES_DEDUP_MS", "")
    monkeypatch.setenv("HERMES_CFG_DASHBOARD_EQUITY__DEDUP_WINDOW_MS", "6000")
    mp = _memory_quality_params()
    dp = _dashboard_equity_params(config={})
    assert mp["crash_down_pct"] == 0.40
    assert mp["filter_window_sec"] == 240
    assert dp["dedup_window_ms"] == 6000


# ── canonical env / config dict 经 helper 流向消费方 ──────────────────────

def test_r13_b11_helper_canonical_env_flows(monkeypatch):
    """无 legacy env 干扰时，HERMES_CFG_* 必须流向 helper。"""
    _clear_b11_env(monkeypatch)
    monkeypatch.setenv("HERMES_CFG_MEMORY_QUALITY__IMPLAUSIBLE_PCT", "0.15")
    monkeypatch.setenv("HERMES_CFG_DASHBOARD_EQUITY__STALE_TICK_AGE_S", "600")
    mp = _memory_quality_params()
    dp = _dashboard_equity_params(config={})
    assert mp["implausible_pct"] == 0.15
    assert dp["stale_tick_age_s"] == 600


def test_r13_b11_helper_config_dict_override():
    # 签名差异：memory helper 无 config 形参（热路径直读全局 config），
    # dashboard helper 带 config 形参（测试/调用方可注入）。
    assert "config" not in inspect.signature(_memory_quality_params).parameters
    assert "config" in inspect.signature(_dashboard_equity_params).parameters
    # dashboard config dict 注入：dip_ratio/dip_window 显式覆盖（与 canonical
    # 字面量不同才生效），其余叶保留字面量：
    dp = _dashboard_equity_params(config={DE_BLOCK: {"dip_ratio": 0.6, "dip_window": 40}})
    assert dp["dip_ratio"] == 0.6
    assert dp["dip_window"] == 40
    assert dp["stale_tick_age_s"] == 180
    assert dp["dedup_window_ms"] == 5000
    # config dict 值恰好等于 canonical 字面量时，对 dip_* 而言视为"未配置"，
    # 回退活全局（此处活全局 == 字面量，故结果不变）：
    dp2 = _dashboard_equity_params(config={DE_BLOCK: {"dip_ratio": 0.7, "dip_window": 15}})
    assert dp2["dip_ratio"] == 0.7
    assert dp2["dip_window"] == 15


# ── guard：坏值 / 越界回退字面量，热路径不崩 ──────────────────────────────

def test_r13_b11_guard_reconfirm_streak_zero_falls_back(monkeypatch):
    _clear_b11_env(monkeypatch)
    monkeypatch.setenv("HERMES_CFG_MEMORY_QUALITY__RECONFIRM_STREAK", "0")
    p = _memory_quality_params()
    assert p["reconfirm_streak"] == 2                      # 0 会让首 tick 即接受


def test_r13_b11_guard_dip_window_zero_falls_back(monkeypatch):
    _clear_b11_env(monkeypatch)
    monkeypatch.setenv("HERMES_CFG_DASHBOARD_EQUITY__DIP_WINDOW", "0")
    p = _dashboard_equity_params(config={})
    assert p["dip_window"] == 15


def test_r13_b11_guard_garbage_returns_full_literals(monkeypatch):
    """coerce 失败：helper 整块回退字面量（不抛异常、不留半坏 dict）。"""
    _clear_b11_env(monkeypatch)
    monkeypatch.setenv("HERMES_MEMORY_FLUSH_THROTTLE_S", "garbage")
    mp = _memory_quality_params()
    assert mp == _MEMORY_QUALITY_DEFAULTS
    monkeypatch.setenv("HERMES_EQUITY_DIP_RATIO", "not-a-float")
    dp = _dashboard_equity_params(config={})
    assert dp == CANONICAL_DEFAULTS[DE_BLOCK]


def test_r13_b11_guard_zero_window_and_ratio_are_legal(monkeypatch):
    """filter_window_sec / dedup_window_ms 下限 0、比率类下限 0.0：0 是合法的
    "关闭窗口 / 全部打标"值。"""
    _clear_b11_env(monkeypatch)
    monkeypatch.setenv("HERMES_CFG_MEMORY_QUALITY__FILTER_WINDOW_SEC", "0")
    monkeypatch.setenv("HERMES_CFG_MEMORY_QUALITY__IMPLAUSIBLE_PCT", "0")
    mp = _memory_quality_params()
    assert mp["filter_window_sec"] == 0
    assert mp["implausible_pct"] == 0.0
    monkeypatch.setenv("HERMES_CFG_DASHBOARD_EQUITY__DEDUP_WINDOW_MS", "0")
    monkeypatch.setenv("HERMES_CFG_DASHBOARD_EQUITY__DIP_RATIO", "0")
    dp = _dashboard_equity_params(config={})
    assert dp["dedup_window_ms"] == 0
    assert dp["dip_ratio"] == 0.0


def test_r13_b11_negative_ratio_falls_back(monkeypatch):
    _clear_b11_env(monkeypatch)
    monkeypatch.setenv("HERMES_CFG_MEMORY_QUALITY__CRASH_DOWN_PCT", "-0.2")
    p = _memory_quality_params()
    assert p["crash_down_pct"] == 0.40


def test_r13_b11_helper_returns_independent_copy(monkeypatch):
    """每次返回独立 dict，调用方 mutate 不污染字面量表 / 彼此隔离。"""
    _clear_b11_env(monkeypatch)
    a = _memory_quality_params()
    a["implausible_pct"] = 9.9
    a["reconfirm_streak"] = 99
    b = _memory_quality_params()
    assert b["implausible_pct"] == 0.25
    assert b["reconfirm_streak"] == 2
    assert _MEMORY_QUALITY_DEFAULTS["implausible_pct"] == 0.25
    da = _dashboard_equity_params(config={})
    da["dip_ratio"] = 9.9
    db = _dashboard_equity_params(config={})
    assert db["dip_ratio"] == 0.7


# ── dashboard 活全局：模块属性保留 + setattr 补丁经回退层继续生效 ─────────

def test_r13_b11_dashboard_module_attributes_kept():
    """test_dashboard_config_api 钉死：模块属性必须保留且默认值不变。"""
    assert dashboard_mod._EQUITY_DIP_RATIO == 0.7
    assert dashboard_mod._EQUITY_DIP_WINDOW == 15
    # FLUSH_THROTTLE_S 模块常量保留为 fallback 符号
    assert memory_mod.FLUSH_THROTTLE_S == 0.2


def test_r13_b11_dashboard_defaults_read_live_globals(monkeypatch):
    """字面量回退层在调用时读活全局：monkeypatch.setattr 改模块属性后，
    helper 回退值跟随（test_cleanup dip 测试硬约束）。"""
    _clear_b11_env(monkeypatch)
    monkeypatch.setattr(dashboard_mod, "_EQUITY_DIP_RATIO", 0.5)
    monkeypatch.setattr(dashboard_mod, "_EQUITY_DIP_WINDOW", 7)
    d = _dashboard_equity_defaults()
    assert d["dip_ratio"] == 0.5
    assert d["dip_window"] == 7
    p = _dashboard_equity_params(config={})
    assert p["dip_ratio"] == 0.5
    assert p["dip_window"] == 7
    # 其余叶不受 setattr 影响
    assert p["stale_tick_age_s"] == 180
    assert p["dedup_window_ms"] == 5000


def test_r13_b11_dashboard_legacy_env_beats_setattr(monkeypatch):
    """legacy env 优先级高于活全局回退（setattr 0.5 但 env 0.6 → 0.6）。"""
    _clear_b11_env(monkeypatch)
    monkeypatch.setattr(dashboard_mod, "_EQUITY_DIP_RATIO", 0.5)
    monkeypatch.setenv("HERMES_EQUITY_DIP_RATIO", "0.6")
    p = _dashboard_equity_params(config={})
    assert p["dip_ratio"] == 0.6


# ── 热路径接线：源码断言（helper 注入 + 消费点 + 旧内联 env/字面量清除）────

def test_r13_b11_modules_expose_helpers():
    assert hasattr(memory_mod, "_memory_quality_params")
    assert hasattr(dashboard_mod, "_dashboard_equity_params")
    assert hasattr(dashboard_mod, "_dashboard_equity_defaults")
    assert memory_mod._memory_quality_params is _memory_quality_params
    assert dashboard_mod._dashboard_equity_params is _dashboard_equity_params


def test_r13_b11_track_daily_pnl_resolves_gate_via_helper():
    src = inspect.getsource(memory_mod.AgentMemory.track_daily_pnl)
    assert "_q = _memory_quality_params()" in src
    assert '_IMPLAUSIBLE_PCT = _q["implausible_pct"]' in src
    assert '_CRASH_DOWN_PCT = _q["crash_down_pct"]' in src
    assert '_FILTER_WINDOW_S = _q["filter_window_sec"]' in src
    assert '_RECONFIRM_STREAK = _q["reconfirm_streak"]' in src
    assert "(now_s - prev_ts) < _FILTER_WINDOW_S" in src
    assert "streak < _RECONFIRM_STREAK" in src
    # 旧的裸字面量 / 内联 env 读取不得残留在方法体内
    assert "_IMPLAUSIBLE_PCT = 0.25" not in src
    assert 'os.environ.get("HERMES_EQUITY_CRASH_DOWN_PCT"' not in src
    assert "(now_s - prev_ts) < 180" not in src
    assert "streak < 2" not in src


def test_r13_b11_flush_resolves_throttle_via_helper():
    src = inspect.getsource(memory_mod.AgentMemory.flush)
    assert '_memory_quality_params()["flush_throttle_s"]' in src
    assert "< FLUSH_THROTTLE_S" not in src


def test_r13_b11_avg_exit_slip_resolves_defaults_via_helper():
    src = inspect.getsource(memory_mod.AgentMemory.avg_exit_slip_bps)
    assert "_q = _memory_quality_params()" in src
    assert 'days = _q["slip_window_days"]' in src
    assert 'min_samples = _q["slip_min_samples"]' in src
    assert "days: Optional[float] = None" in src
    assert "min_samples: Optional[int] = None" in src
    # 旧的签名硬编码默认值不得残留
    assert "days: float = 30.0" not in src
    assert "min_samples: int = 3" not in src


def test_r13_b11_equity_curve_payload_resolves_dip_via_helper():
    src = inspect.getsource(dashboard_mod._equity_curve_payload)
    assert "_q = _dashboard_equity_params()" in src
    assert 'dip_ratio = _q["dip_ratio"]' in src
    assert 'dip_window = _q["dip_window"]' in src
    assert "eq < dip_ratio * ref" in src
    assert "len(window) > dip_window" in src
    # 旧的直接模块全局消费不得残留（属性本身保留）
    assert "eq < _EQUITY_DIP_RATIO * ref" not in src
    assert "len(window) > _EQUITY_DIP_WINDOW" not in src


def test_r13_b11_summary_payload_resolves_stale_via_helper():
    src = inspect.getsource(dashboard_mod._summary_payload)
    assert '_dashboard_equity_params()["stale_tick_age_s"]' in src
    assert "last_tick_age_s > stale_after_s" in src
    assert "last_tick_age_s > 180" not in src


def test_r13_b11_closed_trades_resolves_dedup_via_helper():
    src = inspect.getsource(dashboard_mod._closed_trades_payload)
    assert '_dashboard_equity_params()["dedup_window_ms"]' in src
    assert 'os.environ.get("HERMES_CLOSED_TRADES_DEDUP_MS"' not in src


# ── 行为 sentinel：质量门语义零变化（25% / 180s / streak=2 / min_samples）─

def test_r13_b11_degraded_read_filter_semantics_unchanged(monkeypatch):
    """复刻 test_atr_stop 硬约束：100→99 正常；100→59.7（-40.3% 秒级内）
    首次忽略；200s 后同值接受。接线后语义必须逐字不变。"""
    m = memory_mod.AgentMemory()
    monkeypatch.setattr(m, "flush", lambda: None)
    m._initialized = True
    m.track_daily_pnl(100.0)
    m.track_daily_pnl(99.0)
    assert round(m.get_daily_pnl(), 2) == -1.0
    m.track_daily_pnl(59.7)                      # phantom -40% in seconds
    assert round(m.get_daily_pnl(), 2) == -1.0   # ignored
    m._last_eq_reading_ts -= 200                 # pretend 200s passed
    m.track_daily_pnl(59.7)                      # sustained -> accepted
    assert round(m.get_daily_pnl(), 2) == -40.3


def test_r13_b11_reconfirm_streak_semantics_unchanged(monkeypatch):
    """首次不可信读数拒绝、连续第 2 次再确认后接受（streak < 2 语义）。"""
    m = memory_mod.AgentMemory()
    monkeypatch.setattr(m, "flush", lambda: None)
    m._initialized = True
    m.track_daily_pnl(100.0)
    m.track_daily_pnl(130.0)                     # +30% tick 1: implausible -> rejected
    assert round(m.get_daily_pnl(), 2) == 0.0
    m.track_daily_pnl(130.0)                     # tick 2: re-confirmed -> accepted
    assert round(m.get_daily_pnl(), 2) == 30.0


def test_r13_b11_crash_down_accepted_immediately(monkeypatch):
    """跌幅 ≥ crash 阈值（40%）立即接受（fail-open），不等再确认。"""
    m = memory_mod.AgentMemory()
    monkeypatch.setattr(m, "flush", lambda: None)
    m._initialized = True
    m.track_daily_pnl(100.0)
    m.track_daily_pnl(55.0)                      # -45% crash -> accepted at once
    assert round(m.get_daily_pnl(), 2) == -45.0


def test_r13_b11_avg_exit_slip_min_samples_semantics_unchanged(monkeypatch):
    """复刻 test_memory_caps.test_avg_exit_slip_requires_min_samples 硬约束：
    <3 条 adverse close → 0.0；≥3 条出均值；favorable close 不进 adverse deque。
    默认 days/min_samples 从 memory_quality 块解析（30.0 / 3）。"""
    import time as _time

    from hermes_trader import event_log
    monkeypatch.setattr(event_log, "append", lambda *a, **k: True)
    m = memory_mod.AgentMemory()
    monkeypatch.setattr(m, "flush", lambda *a, **k: None)
    m._initialized = True
    base_ms = int(_time.time() * 1000)

    def _close(bps, at_ms):
        return {"coin": "SOL", "side": "LONG", "realized_pnl_usd": 0.1,
                "exit_slip_bps": bps, "closed_at": at_ms}

    m.record_close(_close(12.0, base_ms))
    m.record_close(_close(18.0, base_ms + 60_000))
    assert m.avg_exit_slip_bps("SOL") == 0.0     # 2 samples < min_samples=3
    m.record_close(_close(30.0, base_ms + 120_000))
    assert m.avg_exit_slip_bps("SOL") == pytest.approx(20.0)   # (12+18+30)/3
    m.record_close(_close(-5.0, base_ms + 180_000))
    assert m.avg_exit_slip_bps("SOL") == pytest.approx(20.0)   # favorable 不计入
    # 显式 days 传参仍走调用方值（executor.py L818/L1606 用法不受影响）
    assert m.avg_exit_slip_bps("SOL", days=30.0, min_samples=3) == pytest.approx(20.0)
