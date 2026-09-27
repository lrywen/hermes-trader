"""Offline tests for the hermes-trader codebase.

Covers the pure/refactored logic — no network or Hyperliquid credentials
required. Run from the repo root: ``pytest`` (or ``python3 -m pytest``).

Network-dependent paths (live order placement, account-state fetches, the
OpenRouter research call, the full market scan) are not covered here — they
can only be exercised against the real exchange.
"""
import importlib.util
import json
import math
import pathlib
import subprocess
import sys

import pytest

from hermes_trader.models.types import Candle

ROOT = pathlib.Path(__file__).resolve().parents[1]
MCP_SCRIPT = str(ROOT / "scripts" / "hermes-mcp-server.py")


@pytest.fixture(autouse=True)
def _clear_dsl_trackers():
    """Isolate the DSL tracker registry between tests. The re-entry backstop in
    maybe_execute now reads dsl_exit._active_positions, so a tracker leaked by an
    earlier test would inject a phantom held-coin and block unrelated trades."""
    try:
        from hermes_trader.agents import dsl_exit
        dsl_exit._active_positions.clear()
    except Exception:
        pass
    yield
    try:
        from hermes_trader.agents import dsl_exit
        dsl_exit._active_positions.clear()
    except Exception:
        pass


def _candles(n=150):
    return [
        Candle(t=i, o=100 + i * 0.1, h=101 + i * 0.1, l=99 + i * 0.1,
               c=100 + i * 0.1 + math.sin(i) * 0.5, v=1000.0 + i)
        for i in range(n)
    ]


# ── models ──────────────────────────────────────────────────────────────


def _load_mcp():
    spec = importlib.util.spec_from_file_location("mcpsrv", MCP_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["mcpsrv"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_mcp_norm_coin_preserves_hip3_dex_prefix():
    """`_norm_coin` uppercases bare crypto tickers but never the lowercase
    HIP-3 dex prefix — a naive .upper() turns `xyz:MU` into `XYZ:MU` and
    breaks every HIP-3 position lookup the MCP server does."""
    mod = _load_mcp()
    assert mod._norm_coin("btc") == "BTC"
    assert mod._norm_coin("BTC") == "BTC"
    assert mod._norm_coin("xyz:mu") == "xyz:MU"
    assert mod._norm_coin("xyz:MU") == "xyz:MU"
    assert mod._norm_coin("vntl:nvda") == "vntl:NVDA"
    assert mod._norm_coin("") == ""


def test_mcp_stub_table_and_tool_coverage():
    mod = _load_mcp()
    # The stub list is now a list of tool names (not a dict of fake payloads).
    # Each stubbed tool returns an explicit `not_implemented` error so LLM
    # callers don't silently consume placeholder data.
    assert len(mod._STUB_TOOL_NAMES) == 48
    # Audit 2026-09-06 (F1): deep_research is dropped from tools/list at
    # startup when its HermesTradingAgents dependency directory is absent, so
    # the advertised count is 101 with the dep present and 100 without.
    expected_tools = 101 if mod._DEEP_RESEARCH_AVAILABLE else 100
    assert len({t["name"] for t in mod.TOOLS}) == expected_tools
    if mod._DEEP_RESEARCH_AVAILABLE:
        assert "deep_research" in {t["name"] for t in mod.TOOLS}
    else:
        assert "deep_research" not in {t["name"] for t in mod.TOOLS}
    handler = mod._make_stub_handler("get_rewards")
    res = json.loads(handler({}))
    assert res["error"] == "not_implemented"
    assert res["tool"] == "get_rewards"
    assert "stub" in res["reason"].lower()


def test_mcp_server_stdio_end_to_end():
    reqs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "get_rewards", "arguments": {}}},
    ]
    inp = "\n".join(json.dumps(r) for r in reqs) + "\n"
    proc = subprocess.run([sys.executable, MCP_SCRIPT], input=inp,
                          capture_output=True, text=True, timeout=90, cwd=str(ROOT))
    resps = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    assert len(resps) == 3, proc.stderr
    assert resps[0]["result"]["serverInfo"]["name"] == "hermes-trader"
    # Audit 2026-09-06 (F1): deep_research is unadvertised when the
    # HermesTradingAgents dependency directory is absent on this host.
    expected_tools = 101 if _load_mcp()._DEEP_RESEARCH_AVAILABLE else 100
    assert len(resps[1]["result"]["tools"]) == expected_tools
    call = json.loads(resps[2]["result"]["content"][0]["text"])
    assert call["error"] == "not_implemented"
    assert call["tool"] == "get_rewards"
