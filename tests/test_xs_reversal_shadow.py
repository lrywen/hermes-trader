"""E6 xs_reversal (Audit 2026-09-07, M2) oversold-bounce LONG shadow probe tests.

守护不变量（诊断中文）：
  - 纯函数口径：ext_pct 72 根最高高窗、2160 根百分位（bisect_right）、awake
    480 根中位数 + 7 根活跃计数（5/7 过、4/7 拒）、数据不足 fail-open 返回 None；
    EMA8<EMA21 + RSI[15,35) 决定 is_candidate，regime 仅快照不门控。
  - 三态：off 零 fetch / 零线程 / 零写入（fetch 替换为 _boom）；shadow 写
    JSONL 且永不下单（place_hl_order 替换为 pytest.fail）；enforce 行为同
    shadow（record-only），不抛不放行不拦截。
  - 热路径安全：fetch 异常 / worker 异常只 debug，不外抛；非 LONG side 不
    fetch；未收盘 bar 被剔除。
  - 配置旋钮：CANONICAL_DEFAULTS sentinel（mode=off/top_pct=85/rsi_long=35）、
    cfg_get 环境穿透 HERMES_CFG_XS_REVERSAL__*、HERMES_XS_REVERSAL_MODE
    env 覆盖、坏 mode 回落 off、schema 三态接受 / 越界与未知键拒绝。
"""

import json
import threading
import time
from pathlib import Path

import pytest

from hermes_trader.agents import xs_reversal as xs
from hermes_trader.agents.config_schema import validate_config_updates
from hermes_trader.agents.config_store import CANONICAL_DEFAULTS, cfg_get
from hermes_trader.models.types import Candle


# ── synthetic candle construction ──────────────────────────────────────────

_HOUR_MS = 3600 * 1000


def _candle(t_ms: int, o: float, h: float, l: float, c: float, v: float) -> Candle:
    return Candle(t=t_ms, o=o, h=h, l=l, c=c, v=v)


def _series(n: int, *, crash_len: int = 120, d: float = 0.006,
            bounce_every: int = 5, bounce: float = 0.008,
            base: float = 100.0, seed_vol: float = 1000.0,
            crash_vol_mult: float = 8.0, rng_frac: float = 0.02,
            downtrend: bool = True, rsi_oversold: bool = True) -> list[Candle]:
    """Build `n` 1h candles ending in a deep, active oversold drawdown.

    History (bars 0..n-crash_len-1) trades flat near `base` on steady, quiet
    volume so the 2160-bar ext_pct distribution is tight around 0 and the
    480-bar awake medians are well-defined. The last `crash_len` bars form an
    accelerating capitulation decline: steady down steps interrupted by
    occasional relief bounces (so RSI(14) settles ~20-25 rather than 0), on
    volumes/ranges that ramp up strictly above the flat-era medians (so every
    awake bar passes the strict `>` active test). The last bar is therefore:
      * deep in the bottom tail of the drawdown distribution (xs trigger),
      * active on all 7 awake bars,
      * EMA8 << EMA21 (downtrend) when downtrend=True,
      * RSI(14) in [rsi_floor, rsi_long) when rsi_oversold=True.

    Tuned against the real indicator functions (ext ~ -22%, pctile ~0.0,
    awake 1.00, RSI ~22 with the defaults below). The last bar timestamp is
    OLD (closed long ago) so gather_xs_reversal's forming-bar check keeps it.
    """
    t0 = int(time.time() * 1000) - (n + 10) * _HOUR_MS
    flat_n = max(0, n - crash_len)
    candles: list[Candle] = []
    prev_c = base
    for i in range(n):
        if i < flat_n:
            o = c = base
            v = seed_vol
            rng = base * 0.001
        else:
            k = i - flat_n  # 0..crash_len-1
            if k > 0 and k % bounce_every == 0:
                c = prev_c * (1.0 + bounce)   # relief bounce
            else:
                c = prev_c * (1.0 - d)        # capitulation step
            o = prev_c
            ramp = 0.5 + 0.5 * (k + 1) / crash_len  # 0.5 -> 1.0
            v = seed_vol * crash_vol_mult * ramp
            rng = base * rng_frac * ramp
            prev_c = c
        candles.append(_candle(t0 + i * _HOUR_MS, o,
                               max(o, c) + rng, min(o, c) - rng, c, v))

    if not downtrend:
        # Flip the crash tail into a sharp V-recovery ending ABOVE base, so
        # EMA8 > EMA21 at the last bar (the xs drawdown trigger typically
        # stops firing as price reclaims the 72-bar high — tests tolerate
        # a None record).
        for k in range(crash_len):
            idx = flat_n + k
            step = k / max(1, crash_len - 1)
            px = base * (0.55 + 0.65 * step)  # 0.55base -> 1.20base
            ramp = 0.5 + 0.5 * (k + 1) / crash_len
            v = seed_vol * crash_vol_mult * ramp
            rng = base * rng_frac * ramp
            o = candles[idx - 1].c if idx > 0 else px
            candles[idx] = _candle(t0 + idx * _HOUR_MS, o,
                                   px + rng, px - rng, px, v)

    if not rsi_oversold:
        # Last 14 bars bounce +6% each -> RSI(14) exits oversold (near 100).
        # The xs trigger may stop firing; tests tolerate None, but when a
        # record fires, RSI must be >= rsi_long and is_candidate False.
        bounce_from = n - 14
        for j in range(bounce_from, n):
            px = candles[j - 1].c * 1.06
            rng = base * rng_frac
            v = seed_vol * crash_vol_mult
            candles[j] = _candle(t0 + j * _HOUR_MS, candles[j - 1].c,
                                 px + rng, px - rng, px, v)
    return candles


def _cfg(tmp_path, mode="shadow", **over) -> dict:
    blk = {
        "mode": mode,
        "shadow_log_path": str(tmp_path / "xs_shadow.jsonl"),
        "lookback_d": 3,
        "top_pct": 85,
        "awake_bars": 7,
        "awake_min_frac": 0.67,
        "rsi_long": 35.0,
        "rsi_floor": 15.0,
    }
    blk.update(over)
    return {"xs_reversal": blk}


def _read_jsonl(path) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines()]


def _patch_fetch(monkeypatch, candles):
    import hermes_trader.client.hl_client as hl_client
    monkeypatch.setattr(hl_client, "fetch_hl_candles",
                        lambda *a, **k: list(candles))


def _wait_threads(timeout: float = 3.0) -> None:
    """Wait for xs-reversal daemon workers to finish (tests are sync)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        alive = [t for t in threading.enumerate()
                 if t.name.startswith("xs-reversal-") and t.is_alive()]
        if not alive:
            return
        time.sleep(0.02)
    raise AssertionError("xs-reversal worker thread did not finish")


# ══════════════════════════════════════════════════════════════════════════
# 配置层
# ══════════════════════════════════════════════════════════════════════════
def test_config_defaults_registered():
    assert "xs_reversal" in CANONICAL_DEFAULTS
    blk = CANONICAL_DEFAULTS["xs_reversal"]
    assert blk["mode"] == "off"              # 默认 inert（零网络直到翻转）
    assert blk["top_pct"] == 85              # M1 拍板：90+ 样本腰斩
    assert blk["lookback_d"] == 3
    assert blk["awake_bars"] == 7
    assert blk["awake_min_frac"] == 0.67
    assert blk["rsi_long"] == 35.0           # M1 甜点
    assert blk["rsi_floor"] == 15.0
    assert "shadow_log_path" in blk
    # cfg_get 穿透 canonical（空 config 也能拿到默认）
    assert cfg_get("xs_reversal.top_pct", config={}) == 85
    assert cfg_get("xs_reversal.rsi_long", config={}) == 35.0


def test_config_env_override_penetrates(monkeypatch):
    monkeypatch.setenv("HERMES_CFG_XS_REVERSAL__TOP_PCT", "90")
    monkeypatch.setenv("HERMES_CFG_XS_REVERSAL__RSI_LONG", "40.5")
    assert cfg_get("xs_reversal.top_pct", config={}) == 90
    assert cfg_get("xs_reversal.rsi_long", config={}) == 40.5


def test_schema_accepts_three_modes():
    for mode in ("off", "shadow", "enforce"):
        errors = validate_config_updates({"xs_reversal": {"mode": mode}},
                                         strict_keys=True)
        assert errors == [], (mode, errors)


def test_schema_rejects_bad_mode_and_leaves():
    errors = validate_config_updates({"xs_reversal": {"mode": "bogus"}},
                                     strict_keys=True)
    assert any("mode" in e for e in errors), errors
    errors = validate_config_updates({"xs_reversal": {"not_a_leaf": 1}},
                                     strict_keys=True)
    assert any("unknown" in e for e in errors), errors


def test_schema_rejects_out_of_range():
    bad = [
        {"lookback_d": 0},      # int 下界 1
        {"lookback_d": 31},     # int 上界 30
        {"top_pct": 40},        # int 下界 50
        {"top_pct": 100},       # int 上界 99
        {"awake_bars": 99},     # int 上界 48
        {"awake_min_frac": 1.5},
        {"rsi_long": 120.0},
        {"rsi_floor": -1.0},
    ]
    for upd in bad:
        errors = validate_config_updates({"xs_reversal": upd}, strict_keys=True)
        assert errors, upd


# ══════════════════════════════════════════════════════════════════════════
# 纯函数口径
# ══════════════════════════════════════════════════════════════════════════
def test_ext_pct_series_matches_backtest_geometry():
    # 72 根窗：前 71 根 NaN，第 72 根起 = close/72bar-high - 1
    closes = [100.0] * 100
    highs = [100.0] * 100
    highs[50] = 200.0  # rolling high spike
    out = xs._ext_pct_series(closes, highs, 72)
    import math
    assert all(math.isnan(v) for v in out[:71])
    # bar 71 still includes the spike at index 50 (window 0..71)
    assert out[71] == pytest.approx((100.0 / 200.0 - 1.0) * 100.0)
    # bar 122 would drop the spike — but series is length 100; bar 99 window
    # is 28..99 which still contains index 50; use a longer series instead.
    closes2 = [100.0] * 200
    highs2 = [100.0] * 200
    highs2[50] = 200.0
    out2 = xs._ext_pct_series(closes2, highs2, 72)
    # window at bar 122 is 51..122 -> spike at 50 excluded -> back to 0%
    assert out2[122] == pytest.approx(0.0)
    assert out2[121] == pytest.approx((100.0 / 200.0 - 1.0) * 100.0)


def test_pctile_rank_bisect_right():
    # bottom-tail semantics: the most negative value ranks ~0.
    win = sorted([-10.0, -9.0, -8.0, -1.0, -0.5])
    assert xs._pctile_rank(win, -10.0) == pytest.approx(1 / 5)
    assert xs._pctile_rank(win, -0.5) == pytest.approx(5 / 5)
    assert xs._pctile_rank([], -1.0) != xs._pctile_rank([], -1.0)  # NaN


def test_evaluate_insufficient_data_returns_none():
    short = _series(100)
    assert xs.evaluate_xs_reversal(short) is None
    assert xs.evaluate_xs_reversal([]) is None


def test_evaluate_candidate_downtrend_oversold(monkeypatch, tmp_path):
    candles = _series(xs.MIN_BARS + 2)
    cfg = _cfg(tmp_path)
    _patch_fetch(monkeypatch, candles)
    rec = xs.evaluate_xs_reversal(candles, config=cfg)
    assert rec is not None
    # xs trigger fired (bottom-tail drawdown, active awake window)
    assert rec["ext_pct"] < -20.0
    assert rec["ext_percentile"] <= (100.0 - 85.0) / 100.0
    assert rec["awake_frac"] >= 0.67
    # M1 edge cell: downtrend + RSI oversold -> candidate
    assert rec["ema8_gt_ema21"] is False
    assert rec["rsi_floor"] <= rec["rsi14"] < rec["rsi_long"]
    assert rec["is_candidate"] is True
    assert rec["side"] == "long"
    # regime label is a snapshot only (never gates); must be one of the four
    assert rec["macro_regime"] in ("CHOP", "NEUTRAL", "TREND", "STRONG_TREND")
    # outcome fields reserved for M4 reconcile
    assert rec["outcome"] is None and rec["exit_px"] is None and rec["pnl_usd"] is None


def test_evaluate_uptrend_not_candidate_but_snapshot_recorded(monkeypatch, tmp_path):
    candles = _series(xs.MIN_BARS + 2, downtrend=False, rsi_oversold=False)
    cfg = _cfg(tmp_path)
    rec = xs.evaluate_xs_reversal(candles, config=cfg)
    # The xs trigger may or may not still fire after the recovery; when it
    # does, the direction/RSI gates must mark it a NON-candidate.
    if rec is not None:
        assert rec["is_candidate"] is False
        assert rec["ema8_gt_ema21"] is True or rec["rsi14"] >= rec["rsi_long"]


def test_evaluate_rsi_bounce_exits_oversold(monkeypatch, tmp_path):
    # Downtrend intact but last bars bounce hard: RSI >= rsi_long -> not a
    # candidate even though the xs drawdown trigger fires.
    candles = _series(xs.MIN_BARS + 2, downtrend=True, rsi_oversold=False)
    cfg = _cfg(tmp_path)
    rec = xs.evaluate_xs_reversal(candles, config=cfg)
    if rec is not None:
        assert rec["rsi14"] >= rec["rsi_long"]
        assert rec["is_candidate"] is False


# ══════════════════════════════════════════════════════════════════════════
# 三态 + 热路径
# ══════════════════════════════════════════════════════════════════════════
def test_off_mode_zero_fetch_zero_thread_zero_write(monkeypatch, tmp_path):
    def _boom(*_a, **_k):
        raise AssertionError("off mode must not fetch candles")
    import hermes_trader.client.hl_client as hl_client
    monkeypatch.setattr(hl_client, "fetch_hl_candles", _boom)

    before = {t.name for t in threading.enumerate()}
    cfg = _cfg(tmp_path, mode="off")
    xs.run_xs_reversal_async("TESTCOIN", "long", config=cfg)
    time.sleep(0.1)
    after = {t.name for t in threading.enumerate()}
    assert not (after - before), f"off mode started a thread: {after - before}"
    assert _read_jsonl(cfg["xs_reversal"]["shadow_log_path"]) == []


def test_off_mode_env_var_also_blocks(monkeypatch, tmp_path):
    # No mode block at all + no env -> off (inert default).
    def _boom(*_a, **_k):
        raise AssertionError("default mode must not fetch candles")
    import hermes_trader.client.hl_client as hl_client
    monkeypatch.setattr(hl_client, "fetch_hl_candles", _boom)
    xs.run_xs_reversal_async("TESTCOIN", "long", config={})
    time.sleep(0.1)


def test_bad_mode_env_falls_back_to_off(monkeypatch, tmp_path):
    def _boom(*_a, **_k):
        raise AssertionError("unparsable mode must fall back to off (no fetch)")
    import hermes_trader.client.hl_client as hl_client
    monkeypatch.setattr(hl_client, "fetch_hl_candles", _boom)
    monkeypatch.setenv("HERMES_XS_REVERSAL_MODE", "banana")
    xs.run_xs_reversal_async("TESTCOIN", "long", config={})
    time.sleep(0.1)


def test_env_mode_overrides_config_to_shadow(monkeypatch, tmp_path):
    candles = _series(xs.MIN_BARS + 2)
    _patch_fetch(monkeypatch, candles)
    monkeypatch.setenv("HERMES_XS_REVERSAL_MODE", "shadow")
    cfg = _cfg(tmp_path, mode="off")  # config says off, env arms shadow
    xs.run_xs_reversal_async("TESTCOIN", "long", config=cfg)
    _wait_threads()
    recs = _read_jsonl(cfg["xs_reversal"]["shadow_log_path"])
    assert recs and recs[-1]["coin"] == "TESTCOIN"


def test_shadow_writes_jsonl_and_never_orders(monkeypatch, tmp_path):
    candles = _series(xs.MIN_BARS + 2)
    _patch_fetch(monkeypatch, candles)
    # The probe must never place an order in any mode (M2 is record-only).
    import hermes_trader.agents.executor as executor
    monkeypatch.setattr(executor, "place_hl_order",
                        lambda *_a, **_k: pytest.fail("xs_reversal must never order"))
    cfg = _cfg(tmp_path, mode="shadow")
    xs.run_xs_reversal_async("BTC", "long", config=cfg)
    _wait_threads()
    recs = _read_jsonl(cfg["xs_reversal"]["shadow_log_path"])
    assert len(recs) == 1
    rec = recs[0]
    assert rec["coin"] == "BTC" and rec["side"] == "long"
    assert rec["is_candidate"] is True
    assert rec["entry_px"] > 0


def test_enforce_is_record_only_same_as_shadow(monkeypatch, tmp_path):
    candles = _series(xs.MIN_BARS + 2)
    _patch_fetch(monkeypatch, candles)
    import hermes_trader.agents.executor as executor
    monkeypatch.setattr(executor, "place_hl_order",
                        lambda *_a, **_k: pytest.fail("enforce must not order in M2"))
    cfg = _cfg(tmp_path, mode="enforce")
    # enforce must behave exactly like shadow: record written, no exception.
    xs.run_xs_reversal_async("ETH", "long", config=cfg)
    _wait_threads()
    recs = _read_jsonl(cfg["xs_reversal"]["shadow_log_path"])
    assert len(recs) == 1 and recs[0]["coin"] == "ETH"


def test_non_long_side_no_fetch(monkeypatch, tmp_path):
    def _boom(*_a, **_k):
        raise AssertionError("short side must not fetch for a LONG-only arm")
    import hermes_trader.client.hl_client as hl_client
    monkeypatch.setattr(hl_client, "fetch_hl_candles", _boom)
    cfg = _cfg(tmp_path, mode="shadow")
    xs.run_xs_reversal_async("BTC", "short", config=cfg)
    time.sleep(0.1)
    assert _read_jsonl(cfg["xs_reversal"]["shadow_log_path"]) == []


def test_fetch_failure_swallowed(monkeypatch, tmp_path):
    def _boom(*_a, **_k):
        raise RuntimeError("candle API down")
    import hermes_trader.client.hl_client as hl_client
    monkeypatch.setattr(hl_client, "fetch_hl_candles", _boom)
    cfg = _cfg(tmp_path, mode="shadow")
    # Must not raise (hot-path safety); worker logs debug only.
    xs.run_xs_reversal_async("BTC", "long", config=cfg)
    _wait_threads()
    assert _read_jsonl(cfg["xs_reversal"]["shadow_log_path"]) == []


def test_empty_candles_no_record(monkeypatch, tmp_path):
    _patch_fetch(monkeypatch, [])
    cfg = _cfg(tmp_path, mode="shadow")
    xs.run_xs_reversal_async("BTC", "long", config=cfg)
    _wait_threads()
    assert _read_jsonl(cfg["xs_reversal"]["shadow_log_path"]) == []


def test_forming_bar_dropped(monkeypatch, tmp_path):
    # Same series but make the LAST bar still forming (open time = now).
    candles = _series(xs.MIN_BARS + 2)
    now_ms = int(time.time() * 1000)
    last = candles[-1]
    candles[-1] = _candle(now_ms, last.o, last.h, last.l, last.c * 0.5, last.v)
    _patch_fetch(monkeypatch, candles)
    cfg = _cfg(tmp_path, mode="shadow")
    xs.run_xs_reversal_async("BTC", "long", config=cfg)
    _wait_threads()
    recs = _read_jsonl(cfg["xs_reversal"]["shadow_log_path"])
    # The forming bar (crazy 50% print) must be dropped; the record's entry is
    # the prior closed bar, not the forming one.
    if recs:
        assert recs[-1]["entry_px"] != pytest.approx(last.c * 0.5)


def test_awake_threshold_boundary(monkeypatch, tmp_path):
    # 5/7 active passes; 4/7 rejected — construct directly via _awake_frac.
    candles = _series(xs.MIN_BARS + 2)
    vols = [c.v for c in candles]
    n = len(candles)
    i = n - 1
    # All 7 active in the crash series -> frac 1.0
    assert xs._awake_frac(candles, vols, i, 7) == pytest.approx(1.0)
    # Quiet the last 3 bars (below both medians) -> 4/7 active -> < 0.67
    for j in range(i - 2, i + 1):
        candles[j] = _candle(candles[j].t, candles[j].o, candles[j].c + 1e-9,
                             candles[j].c - 1e-9, candles[j].c, 1.0)
    vols = [c.v for c in candles]
    frac = xs._awake_frac(candles, vols, i, 7)
    assert frac == pytest.approx(4 / 7)
    assert frac < 0.67


def test_gather_returns_record_and_writes(monkeypatch, tmp_path):
    candles = _series(xs.MIN_BARS + 2)
    _patch_fetch(monkeypatch, candles)
    cfg = _cfg(tmp_path, mode="shadow")
    rec = xs.gather_xs_reversal("SOL", "long", config=cfg)
    assert rec is not None and rec["coin"] == "SOL"
    recs = _read_jsonl(cfg["xs_reversal"]["shadow_log_path"])
    assert len(recs) == 1 and recs[0]["coin"] == "SOL"


def test_gather_short_side_returns_none_without_fetch(monkeypatch, tmp_path):
    def _boom(*_a, **_k):
        raise AssertionError("gather short must not fetch")
    import hermes_trader.client.hl_client as hl_client
    monkeypatch.setattr(hl_client, "fetch_hl_candles", _boom)
    assert xs.gather_xs_reversal("BTC", "short", config=_cfg(tmp_path)) is None
