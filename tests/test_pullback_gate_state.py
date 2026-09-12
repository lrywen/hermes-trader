"""M17：pullback-long 旁路评估心跳状态的往返/容错测试。

心跳是评级中心在 regime=up 过渡期区分「写路径故障」与「在跑但无合格候选」的
唯一证据，契约与 market_circuit_state 相同：best-effort、原子写、损坏/缺失
文件返回 None 而不抛异常。
"""
import json
import os

from hermes_trader.agents import pullback_gate_state as pgs


def test_record_and_read_roundtrip(tmp_path):
    p = tmp_path / ".pullback-gate.state"
    pgs.record_evaluation(
        coin="NEAR", macro_regime="up", macro_up=True, score=33.5,
        slow_count=2, rsi4h=52.1, extension_atr=0.8, uptrend=True,
        admitted=False, path=str(p))
    d = pgs.read_state(str(p))
    assert d is not None
    assert d["version"] == 1
    assert d["coin"] == "NEAR"
    assert d["macro_regime"] == "up"
    assert d["macro_up"] is True
    assert d["slow_count"] == 2
    assert d["admitted"] is False
    assert isinstance(d["ts"], float) and d["ts"] > 0


def test_rewrite_overwrites_previous_payload(tmp_path):
    p = tmp_path / ".pullback-gate.state"
    pgs.record_evaluation(coin="AAA", macro_regime="down", macro_up=False,
                          path=str(p))
    pgs.record_evaluation(coin="BBB", macro_regime="up", macro_up=True,
                          admitted=True, path=str(p))
    d = pgs.read_state(str(p))
    assert d["coin"] == "BBB"
    assert d["macro_regime"] == "up"
    assert d["admitted"] is True


def test_read_missing_file_returns_none(tmp_path):
    assert pgs.read_state(str(tmp_path / "nope.state")) is None


def test_read_corrupt_file_returns_none(tmp_path):
    p = tmp_path / ".pullback-gate.state"
    p.write_text("{not json", encoding="utf-8")
    assert pgs.read_state(str(p)) is None


def test_read_future_version_returns_none(tmp_path):
    p = tmp_path / ".pullback-gate.state"
    p.write_text(json.dumps({"version": 999, "ts": 1.0}), encoding="utf-8")
    assert pgs.read_state(str(p)) is None


def test_record_never_raises_on_unwritable_path():
    # best-effort：不可写路径只静默失败，绝不扰动调用方（交易循环）。
    pgs.record_evaluation(coin="X", path="/nonexistent-dir-xyz/state")


def test_default_state_file_env_override(monkeypatch, tmp_path):
    # 模块在导入时读取一次 env：reload 后默认路径必须指向 env 指定位置，
    # record_evaluation 不传 path 时应写到那里；结束后恢复原模块。
    import importlib

    target = str(tmp_path / "env.state")
    original = os.environ.get("HERMES_PULLBACK_GATE_STATE_FILE")
    monkeypatch.setenv("HERMES_PULLBACK_GATE_STATE_FILE", target)
    try:
        reloaded = importlib.reload(pgs)
        assert reloaded.STATE_FILE == target
        reloaded.record_evaluation(coin="ENV", macro_regime="up", macro_up=True)
        assert reloaded.read_state()["coin"] == "ENV"
    finally:
        # 恢复 conftest 注入的 env 后再 reload，避免污染同会话后续测试。
        if original is not None:
            os.environ["HERMES_PULLBACK_GATE_STATE_FILE"] = original
        importlib.reload(pgs)
