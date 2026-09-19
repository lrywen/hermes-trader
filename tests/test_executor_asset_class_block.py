"""Characterization tests for the asset-class enablement gate leaf.

_asset_class_block is a pure decision over the coin substring (colon = HIP-3
tokenized-equity perp, otherwise native HL crypto) against the enable_hip3 /
enable_crypto config flags; these tests pin its two block payloads and its
default-deny (hip3) / default-allow (crypto) semantics after the verbatim
extraction from maybe_execute.
"""

import pytest

from hermes_trader.agents.executor import _asset_class_block


def test_hip3_coin_disabled_by_default_blocks():
    block = _asset_class_block(
        aid="a1", mode="live", coin="xyz:MU", config={})
    assert block == {
        "executed": False,
        "mode": "live",
        "analysis_id": "a1",
        "reason": "hip3_disabled (set enable_hip3=true to trade tokenized-equity perps)",
    }


def test_hip3_coin_explicitly_disabled_blocks():
    block = _asset_class_block(
        aid="a1", mode="live", coin="xyz:MU",
        config={"enable_hip3": False})
    assert block is not None
    assert block["reason"].startswith("hip3_disabled")


def test_hip3_coin_enabled_clears():
    assert _asset_class_block(
        aid="a1", mode="live", coin="xyz:MU",
        config={"enable_hip3": True}
    ) is None


def test_native_coin_enabled_by_default_clears():
    assert _asset_class_block(
        aid="a1", mode="live", coin="BTC", config={}
    ) is None


def test_native_coin_disabled_blocks():
    block = _asset_class_block(
        aid="a1", mode="shadow", coin="BTC",
        config={"enable_crypto": False})
    assert block == {
        "executed": False,
        "mode": "shadow",
        "analysis_id": "a1",
        "reason": "crypto_disabled (set enable_crypto=true to trade native HL perps)",
    }


def test_native_coin_ignores_hip3_flag():
    # enable_hip3 defaults to deny, but a native coin must not consult it.
    assert _asset_class_block(
        aid="a1", mode="live", coin="ETH",
        config={"enable_hip3": False}
    ) is None


def test_hip3_coin_ignores_crypto_flag():
    # A colon-namespaced coin only consults enable_hip3, not enable_crypto.
    assert _asset_class_block(
        aid="a1", mode="live", coin="abc:ETH",
        config={"enable_hip3": True, "enable_crypto": False}
    ) is None


def test_missing_or_none_coin_is_treated_as_native():
    assert _asset_class_block(
        aid="a1", mode="live", coin="", config={}
    ) is None
    assert _asset_class_block(
        aid="a1", mode="live", coin=None, config={"enable_crypto": False}  # type: ignore[arg-type]
    ) is not None


def test_requires_keyword_arguments():
    with pytest.raises(TypeError):
        _asset_class_block("a1", "live", "BTC", {})
