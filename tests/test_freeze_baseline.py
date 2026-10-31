"""P0 freeze_baseline: pure ledger/inventory helpers + end-to-end write."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import freeze_baseline as fb  # noqa: E402


def test_summarize_ledger_reconciles_wallet_to_net_pnl():
    state = {
        "version": 2,
        "accounts": {
            "taker": {
                "wallet_balance": 98.5,
                "closed_count": 2,
                "positions": [{"id": "p1"}],
                "pending_orders": [],
                "fills": [
                    {"type": "open", "fee_usd": 0.0},
                    {"type": "close", "gross_pnl_usd": 1.0, "fee_usd": 0.3,
                     "realized_pnl_usd": 0.7},
                    {"type": "close", "gross_pnl_usd": -1.0, "fee_usd": 0.2,
                     "realized_pnl_usd": -1.2},
                ],
                "equity_curve": [{"ts": 1, "equity": 98.5}],
            }
        },
    }
    out = fb.summarize_ledger(state)
    acc = out["accounts"]["taker"]
    assert acc["sum_fee_usd"] == 0.5
    assert acc["sum_gross_pnl_usd"] == 0.0
    assert acc["sum_net_pnl_usd"] == -0.5
    # wallet - net = baseline offset; 98.5 - (-0.5) = 99.0
    assert acc["wallet_minus_sum_net"] == 99.0
    assert acc["open_positions"] == 1
    assert acc["last_equity_point"] == {"ts": 1, "equity": 98.5}


def test_summarize_ledger_tolerates_garbage():
    assert fb.summarize_ledger(None)["accounts"] == {}
    assert fb.summarize_ledger({"accounts": {"x": "not-a-dict"}})["accounts"] == {}
    out = fb.summarize_ledger({"accounts": {"x": {"fills": [{"type": "close",
                                                             "realized_pnl_usd": "bad"}]}}})
    assert out["accounts"]["x"]["sum_net_pnl_usd"] == 0.0


def test_inventory_sizes_directories(tmp_path):
    (tmp_path / "a.jsonl").write_text("x" * 10)
    sub = tmp_path / "raw"
    sub.mkdir()
    (sub / "b.jsonl").write_text("y" * 25)
    inv = fb.inventory(str(tmp_path))
    by_name = {e["name"]: e for e in inv["entries"]}
    assert by_name["a.jsonl"]["bytes"] == 10
    assert by_name["raw"]["bytes"] == 25
    assert by_name["raw"]["files"] == 1


def test_main_writes_baseline_bundle(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / ".shadow-book.json").write_text(json.dumps({
        "version": 2,
        "accounts": {"taker": {"wallet_balance": 100.0, "fills": [
            {"type": "close", "gross_pnl_usd": 0.5, "fee_usd": 0.1,
             "realized_pnl_usd": 0.4},
        ]}},
    }))
    out = tmp_path / "baselines"
    rc = fb.main(["--data-dir", str(data), "--out-dir", str(out),
                  "--skip-inventory"])
    assert rc == 0
    dirs = list(out.iterdir())
    assert len(dirs) == 1
    manifest = json.loads((dirs[0] / "manifest.json").read_text())
    assert manifest["files"]["ledger"] == "ledger.json"
    assert "git" in manifest and "era" in manifest
    ledger = json.loads((dirs[0] / "ledger.json").read_text())
    assert ledger["accounts"]["taker"]["wallet_balance"] == 100.0
