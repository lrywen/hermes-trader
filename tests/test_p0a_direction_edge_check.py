"""Tests for the P0-A coarse directional-edge analyzer."""
import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "p0a_direction_edge_check",
    Path(__file__).resolve().parent.parent
    / "scripts" / "p0a_direction_edge_check.py")
m = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(m)


def _book(rows):
    """rows: list of (coin, side, regime, roe, usd, open_ts) -> minimal book."""
    fills = []
    for i, (coin, side, reg, roe, usd, ts) in enumerate(rows):
        pid = f"p{i}"
        fills.append({"type": "open", "position_id": pid, "coin": coin,
                      "side": side, "ts": ts, "price": 100.0, "qty": 1})
        fills.append({"type": "close", "position_id": pid, "coin": coin,
                      "side": side, "entry_regime": reg,
                      "realized_pnl_pct": roe, "realized_pnl_usd": usd,
                      "opened_at": ts, "reason": "test"})
    return {"accounts": {"taker": {"fills": fills}}}


def test_load_pairs_open_and_close(tmp_path):
    book = _book([("A", "long", "up", 5.0, 1.0, 1000),
                  ("B", "short", "down", -3.0, -0.5, 2000)])
    p = tmp_path / "b.json"
    p.write_text(__import__("json").dumps(book))
    rows = m.load_closed_trades(str(p))
    assert len(rows) == 2
    by = {r["coin"]: r for r in rows}
    assert by["A"]["side"] == "long" and by["A"]["regime"] == "up"
    assert by["A"]["roe"] == 5.0 and by["A"]["open_ts"] == 1000
    assert by["B"]["side"] == "short" and by["B"]["roe"] == -3.0


def test_aligned_predicate():
    a = {"side": "long", "regime": "up"}
    b = {"side": "short", "regime": "down"}
    c = {"side": "long", "regime": "chop"}
    d = {"side": "short", "regime": "up"}
    assert m._aligned(a) and m._aligned(b)
    assert not m._aligned(c) and not m._aligned(d)


def test_sign_test_p_in_unit_interval():
    assert m._sign_test_p(50, 100) == 1.0          # exactly half
    assert 0.0 <= m._sign_test_p(100, 100) <= 1.0  # all wins
    assert m._sign_test_p(100, 100) < 0.001
    assert 0.0 <= m._sign_test_p(30, 100) <= 1.0


def test_bootstrap_ci_brackets_median():
    rows = [{"roe": float(v), "usd": float(v) / 10.0} for v in
            [-10, -5, -1, 0, 1, 2, 3, 8, 12, 4, -2, 6]]
    s = m._summ(rows)
    lo, hi = s["roe_med_ci"]
    assert s["n"] == 12
    assert lo <= s["roe_med"] <= hi


def test_mwu_separates_distributions():
    a = [1.0, 2.0, 3.0, 4.0, 5.0]
    b = [-5.0, -4.0, -3.0, -2.0, -1.0]
    p_sep = m._mwu_p(a, b)
    assert p_sep < 0.05
    # identical-ish groups -> not significant
    same = [0.0, 1.0, -1.0, 0.5, -0.5]
    assert m._mwu_p(same, list(same)) == 1.0 or m._mwu_p(same, list(same)) > 0.5


def test_overall_summary_direction():
    # clearly winning set
    win = _book([("A", "long", "up", 5, 1, i) for i in range(8)])
    lose = _book([("A", "long", "up", -5, -1, i) for i in range(8)])
    import json, tempfile, os
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(win, f); wp = f.name
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(lose, f); lp = f.name
    sw = m._summ(m.load_closed_trades(wp))
    sl = m._summ(m.load_closed_trades(lp))
    assert sw["roe_med"] > 0 and sw["win"] == 1.0 and sw["usd_sum"] > 0
    assert sl["roe_med"] < 0 and sl["win"] == 0.0 and sl["usd_sum"] < 0
    os.unlink(wp); os.unlink(lp)
