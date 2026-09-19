"""P4-7 contract: backtest_logged is a thin PIT shim over the shared data layer.

scripts/backtest_logged.py used to own its own whole-response candle cache
(_CANDLE_CACHE / _DISK_CANDLE_CACHE, keyed by (coin, interval, count, end)).
P4-7 deleted that cache and rerouted every historical fetch through the kernel
bar store (hermes_trader.data.historical_candles), the same append-only
(coin, interval, t) store collect_candles.py pre-warms. The legacy call surface
is preserved as shims so the four pf_*/signal_* descendants need no changes:

  * fetch_candles_at(coin, interval, count, end_ms) -> Optional[List[Candle]]
  * fetch_forward_bars(coin, interval, entry_ms, end_ms)
  * _load_disk_cache(path) / _save_disk_cache(path)
  * _API_SLEEP_S / _API_FAILURES

These tests are fully offline (the kernel transport is monkeypatched).
"""
from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

import pytest

from hermes_trader.data import historical_candles as hc

_REPO = Path(__file__).resolve().parents[1]
_SCRIPTS = _REPO / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

STEP = hc.INTERVAL_MS["5m"]

# Data-layer symbols the descendant research scripts are allowed to rely on.
_SHIM_SURFACE = {
    "fetch_candles_at", "_load_disk_cache", "_save_disk_cache", "_API_SLEEP_S",
}
_DOWNSTREAM = [
    "pf_dual_period_report.py",
    "signal_pf_census.py",
    "param_robustness_report.py",
    "pf_multitf_ab_report.py",
]


def _load_logged():
    name = "p47_backtest_logged"
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / "backtest_logged.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def btlog(tmp_path):
    hc.reset_cache()
    hc.set_cache_file(str(tmp_path / "bars.json"))
    mod = _load_logged()
    mod._API_FAILURES = 0
    mod._API_SLEEP_S = 0.0
    yield mod
    hc.reset_cache()


def _serve(prices_by_t, calls):
    """Fake kernel transport: serves rows by exact bar-open time."""
    def _post(path, payload, *a, **k):
        req = payload["req"]
        calls.append(req)
        start, end = int(req["startTime"]), int(req["endTime"])
        return [{"t": t, "o": p, "h": p + 1, "l": p - 1, "c": p, "v": 1.0}
                for t, p in sorted(prices_by_t.items()) if start <= t <= end]
    return _post


def _grid(t):
    return t - (t % STEP)


# ── fetch_candles_at: PIT, truncation, thin history ─────────────────────────

def test_fetch_candles_at_is_point_in_time(btlog, monkeypatch):
    calls: list[dict] = []
    g = _grid(1_700_000_000_000)
    prices = {g + i * STEP: 100.0 + i for i in range(8)}
    monkeypatch.setattr(hc, "_http_post", _serve(prices, calls))

    # as_of exactly on the grid of the newest bar: that bar is still forming
    # (closes one step later) and must be excluded.
    bars = btlog.fetch_candles_at("AAA", "5m", 10, g + 7 * STEP)
    assert [b.t for b in bars] == [g + i * STEP for i in range(7)]


def test_fetch_candles_at_truncates_to_last_count(btlog, monkeypatch):
    calls: list[dict] = []
    g = _grid(1_700_000_000_000)
    prices = {g + i * STEP: 100.0 + i for i in range(10)}
    monkeypatch.setattr(hc, "_http_post", _serve(prices, calls))

    bars = btlog.fetch_candles_at("AAA", "5m", 4, g + 10 * STEP)
    assert len(bars) == 4
    assert [b.t for b in bars] == [g + i * STEP for i in range(6, 10)]


def test_fetch_candles_at_thin_history_returns_short(btlog, monkeypatch):
    calls: list[dict] = []
    g = _grid(1_700_000_000_000)
    prices = {g + i * STEP: 100.0 for i in range(2)}
    monkeypatch.setattr(hc, "_http_post", _serve(prices, calls))

    bars = btlog.fetch_candles_at("AAA", "5m", 50, g + 2 * STEP)
    assert bars is not None
    assert len(bars) == 2
    assert btlog._API_FAILURES == 0


def test_fetch_candles_at_error_returns_none_and_counts(btlog, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("transport down")
    monkeypatch.setattr(hc, "closed_bars_as_of", _boom)

    assert btlog.fetch_candles_at("AAA", "5m", 10, 1_700_000_000_000) is None
    assert btlog._API_FAILURES == 1


def test_fetch_candles_at_uses_shared_cache(btlog, monkeypatch):
    calls: list[dict] = []
    g = _grid(1_700_000_000_000)
    # Include g-STEP: the kernel pads its request endTime by one step, so the
    # first fetch otherwise leaves that boundary point as an unmet missing grid.
    prices = {g + i * STEP: 100.0 + i for i in range(-1, 6)}
    monkeypatch.setattr(hc, "_http_post", _serve(prices, calls))

    end = g + 6 * STEP
    first = btlog.fetch_candles_at("AAA", "5m", 6, end)
    assert len(calls) == 1
    second = btlog.fetch_candles_at("AAA", "5m", 6, end)
    assert second == first
    # Fully covered by the kernel bar store: no second range request.
    assert len(calls) == 1


# ── fetch_forward_bars: entry grid + closed-only forward window ─────────────

def test_fetch_forward_bars_entry_first_closed_only(btlog, monkeypatch):
    calls: list[dict] = []
    g = _grid(1_700_000_000_000)
    prices = {g + i * STEP: 100.0 + i for i in range(6)}
    monkeypatch.setattr(hc, "_http_post", _serve(prices, calls))

    # Ungrid entry snaps down to g; end on a grid boundary means the bar
    # opening exactly at end is still forming and is filtered out.
    bars = btlog.fetch_forward_bars("AAA", "5m", g + STEP // 2, g + 4 * STEP)
    assert bars is not None
    assert bars[0].t == g                      # entry bar
    assert [b.t for b in bars] == [g + i * STEP for i in range(4)]


def test_fetch_forward_bars_error_returns_none(btlog, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("transport down")
    monkeypatch.setattr(hc, "fetch_candle_range", _boom)
    assert btlog.fetch_forward_bars("AAA", "5m", 0, STEP) is None
    assert btlog._API_FAILURES == 1


# ── disk-cache shims pin / flush the kernel store ───────────────────────────

def test_load_save_shims_pin_and_flush_kernel(btlog, tmp_path, monkeypatch):
    cache = tmp_path / "pinned.json"
    btlog._load_disk_cache(str(cache))
    assert btlog._DISK_CACHE_FILE == str(cache)
    assert hc._DISK_CACHE_FILE == str(cache)

    # An empty pin releases the override (kernel falls back to its env default).
    btlog._load_disk_cache("")
    assert hc._DISK_CACHE_FILE is None

    # After fetching bars through the shim, _save_disk_cache must flush the
    # kernel store to the pinned file and report success.
    btlog._load_disk_cache(str(cache))
    g = _grid(1_700_000_000_000)
    prices = {g + i * STEP: 100.0 for i in range(-1, 3)}
    monkeypatch.setattr(hc, "_http_post", _serve(prices, []))
    assert btlog.fetch_candles_at("AAA", "5m", 3, g + 3 * STEP)
    assert btlog._save_disk_cache(str(cache)) is True
    assert cache.exists() and cache.stat().st_size > 0


# ── downstream zero-change contract ────────────────────────────────────────

def test_downstream_scripts_only_use_preserved_shim_surface():
    mod = _load_logged()
    for filename in _DOWNSTREAM:
        path = _SCRIPTS / filename
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=filename)
        alias = None
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for n in node.names:
                    if n.name == "backtest_logged":
                        alias = n.asname or n.name
        assert alias is not None, f"{filename} does not import backtest_logged"
        used = {n.attr for n in ast.walk(tree)
                if isinstance(n, ast.Attribute)
                and isinstance(n.value, ast.Name)
                and n.value.id == alias}
        data_symbols = used & _SHIM_SURFACE
        assert data_symbols, f"{filename} references no shim data symbols"
        missing = [s for s in data_symbols if not hasattr(mod, s)]
        assert not missing, f"{filename} needs missing shims: {missing}"
