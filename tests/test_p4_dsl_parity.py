"""P4 parity contract: the thin script wrappers must reproduce PRODUCTION exits.

Context (P4 unified backtest kernel): the production exit engine
(hermes_trader/agents/dsl_exit.py DSLTracker.check, tick-driven) is the single
source of truth, and the P4 kernel's :class:`DslBarExit`
(hermes_trader/backtest/exit_dsl.py) is its point-in-time bar adapter.

Before P4-5 three scripts carried hand-rolled, mutually divergent bar-level
re-implementations of that engine:

  1. scripts/backtest.py                 DSL.check_bar
  2. scripts/backtest_logged.py          simulate_dsl_exit
  3. scripts/backtest_majors_surge.py    _simulate_trade   (still standalone)

P4-5 converted (1) and (2) into thin wrappers over the SAME ``DslBarExit``
kernel (backtest_logged via ``replay_exit_bars``). The contract here therefore
changed:

  * ``prod``    - the in-test prototype PIT adapter over DSLTracker.
  * ``inline``  - backtest.py's path: drives ``DslBarExit`` directly.
  * ``logged``  - backtest_logged.py's ``replay_exit_bars`` (DslBarExit under a
                  zero-cost CostModel, so ref == fill).
  These three MUST now agree on every scenario (bar, reason, price) — they are
  the same engine. The scenarios below pin that equality; they also happen to
  be regression cases for the historical drift that the kernel eliminated:
  same-bar peak lookahead (D1), gap-through fills (D3), hard-timeout bar
  counting (D4) and single-vs-multi-tier floors (D6).

  * ``surge``   - scripts/backtest_majors_surge.py is deliberately still a
                  standalone one-off research engine and keeps its own replica;
                  it retains residual, CONSCIOUSLY PINNED drift:
                    D2: strict ``b.l < floor`` (a mark exactly on the floor does
                        NOT breach), scenario s3_floor_boundary;
                    D5: its own fallback defaults (test_fallback_defaults_diverge).
"""
from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from hermes_trader.agents.dsl_exit import DSLTracker, ExitPolicy, RetraceTier
from hermes_trader.backtest.cost import CostModel
from hermes_trader.backtest.exit_dsl import DslBarExit
from hermes_trader.models.types import Candle

_REPO = Path(__file__).resolve().parents[1]
_SCRIPTS = _REPO / "scripts"
if str(_SCRIPTS) not in sys.path:
    # backtest_logged imports the sibling `_memory_io` module.
    sys.path.insert(0, str(_SCRIPTS))

BAR_MS = 300_000
T0 = 1_700_000_000_000

# Env vars that hijack cfg_get resolution order (config_store.py L2117-2124).
_CFG_ENV_KEYS = [
    "HERMES_CFG_DSL_EXIT__MAX_LOSS_PCT",
    "HERMES_CFG_DSL_EXIT__MAX_LOSS_ROE_PCT",
    "HERMES_CFG_DSL_EXIT__PROTECT_PCT",
    "HERMES_CFG_DSL_EXIT__RETRACE_THRESHOLD",
    "HERMES_CFG_DSL_EXIT__HARD_TIMEOUT_MINUTES",
]


@pytest.fixture(autouse=True)
def _clear_cfg_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Host HERMES_CFG_DSL_EXIT__* must not leak into simulate_dsl_exit."""
    for key in _CFG_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


# ── Script loaders (cached once per session) ────────────────────────────────

@dataclass
class _Scripts:
    backtest: Any
    logged: Any
    surge: Any


def _load(name: str, filename: str) -> Any:
    mod_name = f"p4_{name}"
    spec = importlib.util.spec_from_file_location(mod_name, _SCRIPTS / filename)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    # Required: the scripts use @dataclass with PEP 563 string annotations,
    # which resolves types via sys.modules[cls.__module__].
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def scripts() -> _Scripts:
    return _Scripts(
        backtest=_load("backtest", "backtest.py"),
        logged=_load("backtest_logged", "backtest_logged.py"),
        surge=_load("backtest_majors_surge", "backtest_majors_surge.py"),
    )


# ── Clock + production bar adapter (prototype for the P4-2 exit adapter) ────

class _Clock:
    wall: float = 0.0
    mono: float = 0.0


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    import hermes_trader.agents.dsl_exit as dx

    clk = _Clock()
    monkeypatch.setattr(dx.time, "time", lambda: clk.wall)
    monkeypatch.setattr(dx.time, "monotonic", lambda: clk.mono)
    return clk


@dataclass
class Exit_:
    bar: int
    reason: str
    px: float


def _norm(reason: str) -> str:
    r = reason.strip()
    for prefix in ("max_loss", "floor_breach", "hard_timeout",
                   "stale_flat_timeout", "time_scratch"):
        if r.startswith(prefix):
            return prefix
    if r == "trailing_stop":
        return "floor_breach"
    if r == "end_of_window":
        return "end_of_data"
    return r


def run_production(clk: _Clock, side: str, entry: float,
                   bars: List[Candle], pol: ExitPolicy) -> Exit_:
    """PIT-correct bar adapter over the TICK-driven production DSLTracker.

    Two check() calls per bar, both stamped at the bar CLOSE:
      1. adverse extreme (long=low) using the peak known from PRIOR bars —
         stop/floor decisions are made on information knowable at bar open;
      2. favorable extreme (long=high) purely to ratchet the peak for the
         next bar (its verdict is discarded).
    Gap fills: when the bar opens beyond the trigger floor the fill is the
    adverse open (a resting stop cannot fill better than the market).
    """
    entry_time = bars[0].t / 1000.0 - BAR_MS / 1000.0
    tr = DSLTracker("TEST", side, entry, entry_time, policy=pol, leverage=1)
    for r, b in enumerate(bars):
        clk.wall = entry_time + (r + 1) * (BAR_MS / 1000.0)
        clk.mono = r * 10.0
        adv = b.l if side == "long" else b.h
        v = tr.check(adv, index_px=None)
        if v.exit:
            if v.floor_price is not None:
                px = min(v.floor_price, b.o) if side == "long" else max(v.floor_price, b.o)
            else:  # timeout-class verdicts carry no floor; replicas close at bar close
                px = b.c
            return Exit_(r, _norm(v.reason), px)
        fav = b.h if side == "long" else b.l
        tr.check(fav, index_px=None)
    return Exit_(len(bars) - 1, "end_of_data", bars[-1].c)


def run_inline(side: str, entry: float, bars: List[Candle],
               pol: ExitPolicy) -> Exit_:
    """backtest.py's post-P4-5 exit path: the kernel ``DslBarExit`` driven bar
    by bar (bar 0 = entry bar). End-of-window is marked by the caller."""
    engine = DslBarExit(side=side, entry_px=entry, entry_time_ms=bars[0].t,
                        policy=pol, leverage=1, coin="INLINE")
    for i, b in enumerate(bars):
        ev = engine.on_bar(b, i)
        if ev is not None:
            return Exit_(ev.bar_index, _norm(ev.reason.value), ev.ref_px)
    return Exit_(len(bars) - 1, "end_of_data", bars[-1].c)


def run_logged(mod: Any, side: str, entry: float, bars: List[Candle],
               pol: ExitPolicy) -> Exit_:
    """backtest_logged.py's post-P4-5 path: ``replay_exit_bars`` wraps the same
    kernel adapter and applies a CostModel. A zero-cost model makes the fill
    equal the raw reference, so it is directly comparable to prod/inline. The
    replay itself marks end-of-window as ``end_of_data``."""
    zero = CostModel(round_trip_fee_bps=0.0, entry_slip_bps=0.0,
                     exit_slip_bps=0.0, stop_delay_slip_bps=0.0)
    reason, idx, ref, _fill, _gross, _net = mod.replay_exit_bars(
        entry, side, 1, bars[0].t, bars, pol, zero, 10_000.0)
    return Exit_(idx, _norm(reason), ref)


def run_surge(mod: Any, side: str, entry: float, bars: List[Candle],
              dsl: Any) -> Exit_:
    # _simulate_trade enters at bars[i+1].open; prepend a doji signal bar so
    # forward[0].open is the agreed entry price.
    sig = Candle(t=bars[0].t - BAR_MS, o=entry, h=entry, l=entry, c=entry, v=1.0)
    cand = mod.Candidate(bar_idx=0, side=side, arm="t", score=0.0,
                         fired=[], meta={})
    tr = mod._simulate_trade(cand, [sig] + bars, 0, dsl, 100.0,
                             0.0, 0.0, 0.0, "TEST")
    assert tr is not None
    return Exit_(tr.hold_bars - 1, _norm(tr.exit_reason), tr.exit_px)


# ── Scenario construction ───────────────────────────────────────────────────

def _bars(rows: List[Tuple[float, float, float, float]]) -> List[Candle]:
    return [Candle(t=T0 + i * BAR_MS, o=o, h=h, l=l, c=c, v=1.0)
            for i, (o, h, l, c) in enumerate(rows)]


@dataclass
class Profile:
    ml: float = 2.5
    roe: float = 100.0
    prot: float = 1.5
    retr: float = 0.30
    hard_min: float = 1e9
    be_trig: float = 0.0
    be_lock: float = 0.0
    stale: float = 0.0
    tiers: List[Tuple[float, float]] = field(default_factory=list)


@dataclass
class Scenario:
    name: str
    side: str
    entry: float
    bars: List[Candle]
    prof: Profile
    # engine -> (exit bar, canonical reason, exit price); None = engine skipped
    expected: Dict[str, Optional[Tuple[int, str, float]]]


def _mk(name: str, side: str, rows: List[Tuple[float, float, float, float]],
        prof: Profile,
        expected: Dict[str, Optional[Tuple[int, str, float]]]) -> Scenario:
    return Scenario(name, side, 100.0, _bars(rows), prof, expected)


# Profile A: aligned with backtest.py DSL defaults (2.5 / 1.5 / .30).
A = Profile()
# Profile B: majors_surge-style multi-tier + breakeven (1.0 / 1.5 / .15).
B = Profile(ml=1.0, prot=1.5, retr=0.15, hard_min=600.0,
            be_trig=2.5, be_lock=0.3, stale=240.0,
            tiers=[(2.0, 0.35), (6.0, 0.30), (12.0, 0.20), (20.0, 0.15)])
# Timeout profile: protect unreachable, 15min hard timeout (= 3 x 5m bars).
T = Profile(prot=100.0, hard_min=15.0)

SCENARIOS = [
    # Gap on the ENTRY bar: bar OPENS at 97 through the 97.50 hard stop.
    # prod/inline/logged all gap-fill at the adverse open 97.00 (regression for D3).
    _mk("s1_gap_stop", "long",
        [(97.0, 97.4, 96.0, 96.5)], A,
        {"prod": (0, "max_loss", 97.0),
         "inline": (0, "max_loss", 97.0),
         "logged": (0, "max_loss", 97.0),
         "surge": None}),                  # entry would be bar0 open (97); N/A

    # Peak 102.5 is knowable only at bar0 close; the trailing floor (101.75)
    # can breach no earlier than bar1 (regression for the D1 same-bar lookahead).
    _mk("s2_normal_trail", "long",
        [(100.0, 102.5, 99.8, 102.0),    # peak 102.5 -> floor 101.75
         (102.0, 102.2, 101.5, 101.6)],  # low 101.5 breaches prior-bar floor
        A,
        {"prod": (1, "floor_breach", 101.75),
         "inline": (1, "floor_breach", 101.75),
         "logged": (1, "floor_breach", 101.75),
         "surge": (1, "floor_breach", 101.75)}),

    # Bar low EXACTLY on floor 101.4. The kernel/prod treat an exact touch as a
    # breach (<= / isclose); surge's strict `<` holds (D2, still pinned there).
    _mk("s3_floor_boundary", "long",
        [(100.0, 102.0, 99.8, 101.8),    # peak 102.0 -> floor 101.4
         (101.8, 101.9, 101.4, 101.5)],  # low == floor exactly
        A,
        {"prod": (1, "floor_breach", 101.4),
         "inline": (1, "floor_breach", 101.4),
         "logged": (1, "floor_breach", 101.4),
         "surge": (1, "end_of_data", 101.5)}),   # D2: strict < does not fire

    # 15min hard timeout on flat 5m bars. The kernel measures wall time from the
    # entry open, so bar #2's close (= 15min) fires (regression for D4; the old
    # inline/logged replicas fired one bar late at #3).
    _mk("s4_hard_timeout", "long",
        [(100.0, 100.0, 100.0, 100.0)] * 5, T,
        {"prod": (2, "hard_timeout", 100.0),
         "inline": (2, "hard_timeout", 100.0),
         "logged": (2, "hard_timeout", 100.0),
         "surge": (2, "hard_timeout", 100.0)}),

    # Multi-tier selection + breakeven/monotonic clamp path: all three kernel
    # engines pick the armed >=2% tier (retrace .35) -> floor 101.95 at bar1
    # (regression for D6: the old single-tier logged replica produced 102.55
    # on the spike bar). prod == surge here.
    _mk("s5_tier_breakeven", "long",
        [(100.0, 103.0, 99.9, 102.8),
         (102.8, 102.9, 101.0, 101.5)], B,
        {"prod": (1, "floor_breach", 101.95),
         "inline": (1, "floor_breach", 101.95),
         "logged": (1, "floor_breach", 101.95),
         "surge": (1, "floor_breach", 101.95)}),

    # D1 short mirror: the short ceiling 98.25 from the spike can only be
    # tested on bar1, not on the spike bar itself.
    _mk("s6_short_mirror", "short",
        [(100.0, 100.2, 97.5, 98.0),    # trough(short) 97.5 -> ceiling 98.25
         (98.0, 98.5, 97.8, 98.4)],     # high 98.5 breaches prior-bar ceiling
        A,
        {"prod": (1, "floor_breach", 98.25),
         "inline": (1, "floor_breach", 98.25),
         "logged": (1, "floor_breach", 98.25),
         "surge": (1, "floor_breach", 98.25)}),

    # Nothing triggers -> all engines close at last bar's close.
    _mk("s7_end_of_data", "long",
        [(100.0, 100.05, 99.95, 100.0)] * 3, A,
        {"prod": (2, "end_of_data", 100.0),
         "inline": (2, "end_of_data", 100.0),
         "logged": (2, "end_of_data", 100.0),
         "surge": (2, "end_of_data", 100.0)}),

    # Gap-through short mirror: open 103 beyond the 102.50 stop -> fill 103.
    _mk("s8_gap_short", "short",
        [(103.0, 104.0, 102.8, 103.5)], A,
        {"prod": (0, "max_loss", 103.0),
         "inline": (0, "max_loss", 103.0),
         "logged": (0, "max_loss", 103.0),
         "surge": None}),
]


def _build_prod_policy(p: Profile) -> ExitPolicy:
    return ExitPolicy(
        max_loss_pct=p.ml, max_loss_roe_pct=p.roe, protect_pct=p.prot,
        retrace_threshold=p.retr, hard_timeout_minutes=p.hard_min,
        breakeven_trigger_pct=p.be_trig, breakeven_lock_pct=p.be_lock,
        stale_flat_timeout_minutes=p.stale,
        phase2_tiers=[RetraceTier(a, b) for a, b in p.tiers],
        hard_stop_confirm_sec=0.0, breach_confirm_sec=0.0)


def _run_engine(engine: str, scn: Scenario, scripts: _Scripts,
                clk: _Clock) -> Optional[Exit_]:
    p = scn.prof
    if engine == "prod":
        return run_production(clk, scn.side, scn.entry, scn.bars,
                              _build_prod_policy(p))
    if engine == "inline":
        return run_inline(scn.side, scn.entry, scn.bars, _build_prod_policy(p))
    if engine == "logged":
        return run_logged(scripts.logged, scn.side, scn.entry, scn.bars,
                          _build_prod_policy(p))
    if engine == "surge":
        dsl = scripts.surge.DslParams(
            max_loss_pct=p.ml, protect_pct=p.prot, retrace_threshold=p.retr,
            hard_timeout_minutes=p.hard_min, breakeven_trigger_pct=p.be_trig,
            breakeven_lock_pct=p.be_lock, stale_flat_timeout_minutes=p.stale,
            phase2_tiers=list(p.tiers))
        # Scenarios whose first forward bar opens away from entry are not
        # representable for surge (it fills at that open); skip them.
        if abs(scn.bars[0].o - scn.entry) > 1e-12:
            return None
        return run_surge(scripts.surge, scn.side, scn.entry, scn.bars, dsl)
    raise AssertionError(engine)


@pytest.mark.parametrize("scn", SCENARIOS, ids=[s.name for s in SCENARIOS])
def test_dsl_engine_parity(scn: Scenario, scripts: _Scripts,
                           clock: _Clock) -> None:
    for engine, want in scn.expected.items():
        got = _run_engine(engine, scn, scripts, clock)
        if want is None:
            assert got is None, f"{scn.name}/{engine}: expected skip"
            continue
        assert got is not None, f"{scn.name}/{engine}: engine not run"
        assert (got.bar, got.reason) == (want[0], want[1]), (
            f"{scn.name}/{engine}: got (bar={got.bar}, reason={got.reason}), "
            f"expected (bar={want[0]}, reason={want[1]})")
        assert got.px == pytest.approx(want[2], abs=1e-9), (
            f"{scn.name}/{engine}: exit px {got.px} != {want[2]}")


# ── D5: fallback defaults silently diverge between replicas and production ──

def test_fallback_defaults_diverge(scripts: _Scripts) -> None:
    """A config-read failure produces materially different strategies.

    majors_surge.DslParams.from_config({}) hardcodes its own defaults; the
    production ExitPolicy() (and canonical config_store defaults used by
    backtest_logged) use different values. Pin the gap so the P4 kernel's
    single parameter source cannot regress into per-engine defaults.
    """
    surge_p = scripts.surge.DslParams.from_config({})
    prod_p = ExitPolicy()

    assert (surge_p.max_loss_pct, surge_p.protect_pct,
            surge_p.retrace_threshold, surge_p.hard_timeout_minutes) == (
        1.0, 1.5, 0.15, 600.0)
    assert (prod_p.max_loss_pct, prod_p.protect_pct,
            prod_p.retrace_threshold, prod_p.hard_timeout_minutes) == (
        0.4, 1.25, 0.20, 1800.0)
    assert surge_p.breakeven_trigger_pct == 2.5 and prod_p.breakeven_trigger_pct == 0.0
    assert surge_p.stale_flat_timeout_minutes == 240
    assert prod_p.stale_flat_timeout_minutes == 480
    assert surge_p.phase2_tiers == [(2.0, 0.35), (6.0, 0.30),
                                   (12.0, 0.20), (20.0, 0.15)]
    assert [(t.pct_above_entry, t.retrace_threshold) for t in prod_p.phase2_tiers] == [
        (8.0, 0.35), (15.0, 0.40)]

    # backtest_logged with an empty config lands on the CANONICAL defaults,
    # i.e. production semantics — not the surge/backtest.py hardcodes.
    from hermes_trader.agents.config_store import cfg_get

    assert cfg_get("dsl_exit.max_loss_pct", config={}) == 0.4
    assert cfg_get("dsl_exit.protect_pct", config={}) == 1.25
    assert cfg_get("dsl_exit.retrace_threshold", config={}) == 0.20
    assert cfg_get("dsl_exit.hard_timeout_minutes", config={}) == 1800.0
