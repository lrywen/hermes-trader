# -*- coding: utf-8 -*-
"""E1 (Q2) regime_risk_overlay —— 震荡市自动降险总开关守护测试。

覆盖三层契约：
  1. 滞回防抖状态机纯函数（step_hysteresis）：连续 N 次非趋势观测才降险、
     连续 N 次趋势观测才恢复；单根抖动 / 中途反复都不翻转；查询失败保持
     当前态；阈值边界精确。
  2. knob 裁决纯函数（apply_overlay_knobs）：overlay 只做"收紧"方向的覆盖
     （max_concurrent 取 min、allow_shorts 取 AND、equity_fraction_mult
     与 pullback_long_enabled 按 profile 收敛），绝不放宽用户基线。
  3. evaluate_risk_overlay 有状态入口：disabled 恒不收紧；shadow 模式姿态
     翻转但 applied 恒为 False（反事实只落 JSONL，永不参与 live 判定）；
     enforce 模式 applied=True；每次进入/退出降险态都落一条结构化 JSONL。

设计要点（对应 docs/remediation-plan-2026-09-06.md E1）：
  - regime 观测来自 detect_regime_with_score 的缓存代理（BTC），"bars" 以
    连续观测次数计（代理缓存 TTL 天然限频）。
  - 非趋势 = chop / neutral（震荡 / 无方向，都属"不可激进"态）；
    趋势 = up / down（下跌趋势恢复 allow_shorts 用于顺势做空）。
  - fail-safe：regime 查询异常时保持当前姿态（不自动降险也不自动恢复），
    状态机等待下一次有效观测。
"""
from __future__ import annotations

import json

import pytest

from hermes_trader.agents import regime_overlay as ro
from hermes_trader.agents.config_store import CANONICAL_DEFAULTS


# ── helpers ────────────────────────────────────────────────────────────────


def _block(**over) -> dict:
    """A canonical-shaped regime_risk_overlay block with test overrides."""
    blk = {
        "enabled": True,
        "shadow_mode": True,
        "hysteresis_bars": 3,
        # Tests drive the state machine deterministically: one step per call.
        # Live wiring defaults this to the regime cache TTL (one step per
        # fresh macro sample ~= one macro "bar").
        "sample_interval_s": 0.0,
        "chop": {
            "max_concurrent": 1,
            "allow_shorts": False,
            "equity_fraction_mult": 0.5,
            "pullback_long_enabled": False,
        },
        "trend": {
            "max_concurrent": 4,
            "allow_shorts": True,
            "equity_fraction_mult": 1.0,
        },
        "shadow_log_path": "",
    }
    blk.update(over)
    return blk


def _cfg(**over) -> dict:
    return {"regime_risk_overlay": _block(**over)}


def _set_series(monkeypatch, regimes):
    """Force detect_regime_with_score to return regimes[i] on the i-th call."""
    seq = list(regimes)
    state = {"i": 0}

    def _fake(_coin, **_kw):
        r = seq[min(state["i"], len(seq) - 1)]
        state["i"] += 1
        if isinstance(r, Exception):
            raise r
        return (r, 0.7)

    monkeypatch.setattr(ro, "detect_regime_with_score", _fake)


def _read_jsonl(path):
    try:
        with open(path, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError:
        return []


def _read_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None


@pytest.fixture(autouse=True)
def _reset_state():
    ro.reset_overlay_state()
    yield
    ro.reset_overlay_state()


# ── 1. 滞回状态机纯函数 ─────────────────────────────────────────────────────


def test_hysteresis_enters_derisk_after_n_nontrend():
    # 2 次 chop 不翻转，第 3 次连续非趋势才降险。
    st = ro.HystState()
    ro.step_hysteresis(st, "chop", bars=3)
    ro.step_hysteresis(st, "chop", bars=3)
    assert st.derisked is False
    ro.step_hysteresis(st, "chop", bars=3)
    assert st.derisked is True
    assert st.run_count == 3


def test_hysteresis_single_jitter_does_not_flip():
    # 趋势态中夹一根 chop 不触发降险（防 ADX 在 20 上下抖动翻转）。
    st = ro.HystState()
    st.derisked = False
    for r in ("up", "chop", "up"):
        ro.step_hysteresis(st, r, bars=3)
    assert st.derisked is False
    assert st.run_count == 1  # 最后落在 up，连续趋势计数为 1


def test_hysteresis_neutral_counts_as_nontrend():
    # neutral（无方向）与 chop 一样计入降险观测。
    st = ro.HystState()
    for r in ("chop", "neutral", "neutral"):
        ro.step_hysteresis(st, r, bars=3)
    assert st.derisked is True


def test_hysteresis_restores_after_n_trend_from_derisk():
    st = ro.HystState(derisked=True)
    ro.step_hysteresis(st, "up", bars=3)
    ro.step_hysteresis(st, "down", bars=3)  # down 也是趋势
    assert st.derisked is True
    ro.step_hysteresis(st, "up", bars=3)
    assert st.derisked is False


def test_hysteresis_bars1_flips_immediately():
    st = ro.HystState()
    ro.step_hysteresis(st, "chop", bars=1)
    assert st.derisked is True
    ro.step_hysteresis(st, "up", bars=1)
    assert st.derisked is False


def test_hysteresis_interrupted_run_resets_counter():
    # chop,chop,up,chop,chop 中 up 打断了连续计数 → 不翻转。
    st = ro.HystState()
    for r in ("chop", "chop", "up", "chop", "chop"):
        ro.step_hysteresis(st, r, bars=3)
    assert st.derisked is False


def test_hysteresis_lookup_error_holds_state():
    # 查询失败：保持当前姿态与计数（fail-safe，不因异常翻转）。
    st = ro.HystState()
    ro.step_hysteresis(st, "chop", bars=3)
    ro.step_hysteresis(st, "chop", bars=3)
    before = (st.derisked, st.run_count)
    ro.step_hysteresis(st, None, bars=3)  # None 表示查询失败
    assert (st.derisked, st.run_count) == before


# ── 2. knob 裁决纯函数 ──────────────────────────────────────────────────────


def _base():
    return {
        "max_concurrent": 2,
        "allow_shorts": True,
        "equity_fraction_mult": 1.0,
        "pullback_long_enabled": True,
    }


def test_knobs_chop_profile_tightens_all():
    blk = _block()
    out = ro.apply_overlay_knobs(_base(), blk, derisked=True)
    assert out["max_concurrent"] == 1            # min(2, 1)
    assert out["allow_shorts"] is False          # AND → false
    assert out["equity_fraction_mult"] == 0.5
    assert out["pullback_long_enabled"] is False


def test_knobs_trend_profile_never_loosens_beyond_base():
    # base max_concurrent=2 / allow_shorts=False / pb=False；trend profile
    # 的 4 / true 不得放宽用户基线（overlay 是单向降险器）。
    base = {
        "max_concurrent": 2,
        "allow_shorts": False,
        "equity_fraction_mult": 1.0,
        "pullback_long_enabled": False,
    }
    out = ro.apply_overlay_knobs(base, _block(), derisked=False)
    assert out["max_concurrent"] == 2            # min(2, 4)
    assert out["allow_shorts"] is False          # AND false
    assert out["equity_fraction_mult"] == 1.0
    assert out["pullback_long_enabled"] is False


def test_knobs_derisked_false_keeps_base_when_base_tighter():
    out = ro.apply_overlay_knobs(_base(), _block(), derisked=False)
    # base max_concurrent=2 < trend 4 → 保持 2；其余按 AND/min 不变。
    assert out["max_concurrent"] == 2
    assert out["allow_shorts"] is True
    assert out["equity_fraction_mult"] == 1.0
    assert out["pullback_long_enabled"] is True


# ── 3. evaluate_risk_overlay 有状态入口 ─────────────────────────────────────


def test_evaluate_disabled_never_derisks():
    ro.evaluate_risk_overlay(_cfg(enabled=False), coin="BTC")
    st = ro.get_overlay_state()
    assert st.derisked is False


def test_evaluate_shadow_does_not_apply_but_logs(tmp_path, monkeypatch):
    _set_series(monkeypatch, ["chop"] * 3)
    log = str(tmp_path / "overlay.jsonl")
    cfg = _cfg(shadow_mode=True, shadow_log_path=log)
    # 前两次未到阈值。
    r1 = ro.evaluate_risk_overlay(cfg, coin="BTC")
    r2 = ro.evaluate_risk_overlay(cfg, coin="BTC")
    assert r1.applied is False and r2.applied is False
    # 第 3 次连续 chop → 姿态进入降险，但 shadow 不生效。
    r3 = ro.evaluate_risk_overlay(cfg, coin="BTC")
    assert r3.derisked is True
    assert r3.applied is False          # shadow：反事实，永不参与 live
    assert r3.regime == "chop"
    # shadow 下 applied knobs 等于 base（未覆盖），would knobs 为降险值。
    base = _base()
    applied = ro.resolve_applied_knobs(base, cfg, r3)
    assert applied["max_concurrent"] == 2
    assert applied["equity_fraction_mult"] == 1.0
    # JSONL 落了一条 enter_derisk 结构化记录。
    recs = _read_jsonl(log)
    events = [r for r in recs if r.get("event") == "enter_derisk"]
    assert len(events) == 1
    e = events[0]
    assert e["mode"] == "shadow"
    assert e["regime"] == "chop"
    assert "chop" in e["active_profile"]
    assert e["would_apply"] is True


def test_evaluate_enforce_applies_derisk(tmp_path, monkeypatch):
    _set_series(monkeypatch, ["neutral"] * 3)
    log = str(tmp_path / "overlay.jsonl")
    cfg = _cfg(shadow_mode=False, shadow_log_path=log)
    for _ in range(3):
        r = ro.evaluate_risk_overlay(cfg, coin="BTC")
    assert r.derisked is True
    assert r.applied is True
    base = _base()
    applied = ro.resolve_applied_knobs(base, cfg, r)
    assert applied["max_concurrent"] == 1
    assert applied["allow_shorts"] is False
    assert applied["equity_fraction_mult"] == 0.5
    assert applied["pullback_long_enabled"] is False
    recs = _read_jsonl(log)
    assert any(x.get("event") == "enter_derisk" and x["mode"] == "enforce"
               for x in recs)


def test_evaluate_exit_derisk_after_trend_streak(tmp_path, monkeypatch):
    _set_series(monkeypatch, ["chop"] * 3 + ["up"] * 3)
    log = str(tmp_path / "overlay.jsonl")
    cfg = _cfg(shadow_mode=False, shadow_log_path=log)
    r = None
    for _ in range(6):
        r = ro.evaluate_risk_overlay(cfg, coin="BTC")
    assert r.derisked is False
    assert r.applied is False
    recs = _read_jsonl(log)
    assert any(x.get("event") == "exit_derisk" for x in recs)


def test_evaluate_lookup_error_holds_prior_posture(monkeypatch):
    # 先进入降险态，之后连续查询失败 → 不自动恢复。
    _set_series(monkeypatch, ["chop"] * 3 + [RuntimeError("boom")] * 5)
    r = None
    for _ in range(8):
        r = ro.evaluate_risk_overlay(_cfg(shadow_mode=False), coin="BTC")
    assert r.derisked is True
    assert r.applied is True
    assert r.regime == "chop"  # 保留最后一次有效观测


def test_evaluate_singleton_persists_across_calls(monkeypatch):
    _set_series(monkeypatch, ["chop"] * 5)
    cfg = _cfg(shadow_mode=False)
    ro.evaluate_risk_overlay(cfg, coin="BTC")
    ro.evaluate_risk_overlay(cfg, coin="BTC")
    r3 = ro.evaluate_risk_overlay(cfg, coin="BTC")
    assert r3.derisked is True  # 状态在调用间累计（同一进程单例）


# ── 4. 判活心跳（事件型臂可观测性） ─────────────────────────────────────────


def test_heartbeat_rewritten_on_every_successful_sample(tmp_path, monkeypatch):
    _set_series(monkeypatch, ["up", "chop", "neutral"])
    hb = str(tmp_path / "overlay.state")
    cfg = _cfg(heartbeat_state_path=hb)
    ts0 = None
    for i, exp_regime in enumerate(("up", "chop", "neutral")):
        r = ro.evaluate_risk_overlay(cfg, coin="BTC")
        assert r.sampled is True
        st = _read_state(hb)
        assert st is not None
        assert st["version"] == ro._HEARTBEAT_VERSION
        assert st["sampled"] is True
        assert st["regime"] == exp_regime
        assert st["derisked"] == r.derisked
        assert st["run_regime"] == r.run_regime
        assert st["run_count"] == r.run_count
        assert st["shadow"] is True
        if i == 0:
            ts0 = st["ts"]
        else:
            assert st["ts"] >= ts0


def test_heartbeat_rewritten_even_without_posture_flip(tmp_path, monkeypatch):
    # 平稳趋势中无翻转（0 条 JSONL 事件），心跳仍每次重写——这正是评级器
    # 区分「健康无触发」与「盲跑」所依赖的契约。
    _set_series(monkeypatch, ["up"] * 5)
    hb = str(tmp_path / "overlay.state")
    log = str(tmp_path / "overlay.jsonl")
    cfg = _cfg(heartbeat_state_path=hb, shadow_log_path=log)
    for _ in range(5):
        ro.evaluate_risk_overlay(cfg, coin="BTC")
    assert _read_state(hb) is not None
    assert _read_jsonl(log) == []


def test_heartbeat_not_written_when_throttled(tmp_path, monkeypatch):
    _set_series(monkeypatch, ["up"] * 5)
    hb = str(tmp_path / "overlay.state")
    cfg = _cfg(heartbeat_state_path=hb, sample_interval_s=3600.0)
    r1 = ro.evaluate_risk_overlay(cfg, coin="BTC")
    assert r1.sampled is True
    assert _read_state(hb) is not None
    # 第二次落在节流窗内：不采样、不重写心跳。
    r2 = ro.evaluate_risk_overlay(cfg, coin="BTC")
    assert r2.sampled is False
    st = _read_state(hb)
    assert st["run_count"] == 1  # 仍是第一次采样的快照


def test_heartbeat_not_written_on_lookup_error(tmp_path, monkeypatch):
    _set_series(monkeypatch, [RuntimeError("boom")] * 3)
    hb = str(tmp_path / "overlay.state")
    cfg = _cfg(heartbeat_state_path=hb)
    for _ in range(3):
        r = ro.evaluate_risk_overlay(cfg, coin="BTC")
        assert r.sampled is True
    # 查询全失败：心跳只证明成功评估，失败不写。
    assert _read_state(hb) is None


def test_heartbeat_not_written_when_disabled(tmp_path, monkeypatch):
    _set_series(monkeypatch, ["up"])
    hb = str(tmp_path / "overlay.state")
    cfg = _cfg(enabled=False, heartbeat_state_path=hb)
    ro.evaluate_risk_overlay(cfg, coin="BTC")
    assert _read_state(hb) is None


def test_heartbeat_path_env_override(tmp_path, monkeypatch):
    _set_series(monkeypatch, ["up"])
    hb = str(tmp_path / "env.state")
    monkeypatch.setenv("HERMES_REGIME_OVERLAY_STATE_FILE", hb)
    # 配置块不给 heartbeat_state_path → 走 env 覆盖。
    ro.evaluate_risk_overlay(_cfg(), coin="BTC")
    assert _read_state(hb) is not None


def test_heartbeat_failure_never_perturbs_eval(tmp_path, monkeypatch):
    _set_series(monkeypatch, ["chop"] * 3)

    def _boom(*_a, **_kw):
        raise OSError("disk gone")

    import hermes_trader.agents.atomic_io as aio
    monkeypatch.setattr(aio, "write_json_atomic", _boom)
    hb = str(tmp_path / "overlay.state")
    cfg = _cfg(heartbeat_state_path=hb)
    # 心跳写失败被吞掉：状态机照常推进、不抛异常。
    r = None
    for _ in range(3):
        r = ro.evaluate_risk_overlay(cfg, coin="BTC")
    assert r.derisked is True
    assert r.applied is False


# ── 5. 配置登记 ─────────────────────────────────────────────────────────────


def test_e1_canonical_block_registered():
    blk = CANONICAL_DEFAULTS.get("regime_risk_overlay")
    assert isinstance(blk, dict)
    # 出厂安全默认：总开关关闭（E1 先 SHADOW，由部署显式启用）。
    assert blk["enabled"] is False
    assert blk["shadow_mode"] is True
    assert int(blk["hysteresis_bars"]) >= 2
    chop = blk["chop"]
    assert chop["max_concurrent"] == 1
    assert chop["allow_shorts"] is False
    assert chop["equity_fraction_mult"] == 0.5
    assert chop["pullback_long_enabled"] is False
    # trend profile 存在（恢复态参考值）。
    assert blk["trend"]["equity_fraction_mult"] == 1.0


def test_e1_cfg_get_resolves_block():
    from hermes_trader.agents.config_store import cfg_get
    cfg = {"regime_risk_overlay": _block(hysteresis_bars=5)}
    assert int(cfg_get("regime_risk_overlay.hysteresis_bars", config=cfg)) == 5
    # env 双下划线覆盖。
    import os
    os.environ["HERMES_CFG_REGIME_RISK_OVERLAY__ENABLED"] = "true"
    try:
        assert bool(cfg_get("regime_risk_overlay.enabled", config={})) is True
    finally:
        del os.environ["HERMES_CFG_REGIME_RISK_OVERLAY__ENABLED"]
