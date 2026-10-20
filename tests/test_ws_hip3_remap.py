"""HIP-3 (xyz:/km: perpDex) WS subscription remap fix.

The official SDK's Info.name_to_coin is built from main-dex meta only, so
Info._remap_coin_subscription raises KeyError('xyz:X') before the trades /
l2Book subscribe frame is sent — even though the HL WS feeds accept
namespaced coins. _seed_hip3_ws_remap populates that map from cached HIP-3
meta. These tests cover the merge behaviour without any network access.
"""
import types

import pytest

from hermes_trader.client import ws_client
from hermes_trader.client.ws_client import _seed_hip3_ws_remap


class _FakeInfo:
    def __init__(self, names):
        self.name_to_coin = dict(names)


def test_seed_adds_missing_hip3_names(monkeypatch):
    monkeypatch.setattr(
        "hermes_trader.client.universe.hip3_coin_names",
        lambda: ["xyz:SKHX", "xyz:XYZ100", "km:GOLD"], raising=False)
    # import target after patch path is valid
    import hermes_trader.client.universe as uni
    monkeypatch.setattr(uni, "hip3_coin_names",
                        lambda: ["xyz:SKHX", "xyz:XYZ100", "km:GOLD"])
    info = _FakeInfo({"BTC": "BTC"})
    added = _seed_hip3_ws_remap(info)
    assert added == 3
    assert info.name_to_coin["xyz:SKHX"] == "xyz:SKHX"
    assert info.name_to_coin["xyz:XYZ100"] == "xyz:XYZ100"
    assert info.name_to_coin["km:GOLD"] == "km:GOLD"
    assert info.name_to_coin["BTC"] == "BTC"


def test_seed_is_idempotent_and_skips_existing(monkeypatch):
    import hermes_trader.client.universe as uni
    monkeypatch.setattr(uni, "hip3_coin_names",
                        lambda: ["xyz:SKHX", "xyz:SKHX"])
    info = _FakeInfo({"xyz:SKHX": "xyz:SKHX"})
    assert _seed_hip3_ws_remap(info) == 0
    # running again still adds nothing
    assert _seed_hip3_ws_remap(info) == 0


def test_seed_fail_open_when_universe_errors(monkeypatch):
    import hermes_trader.client.universe as uni

    def _boom():
        raise RuntimeError("cache unavailable")

    monkeypatch.setattr(uni, "hip3_coin_names", _boom)
    info = _FakeInfo({"BTC": "BTC"})
    assert _seed_hip3_ws_remap(info) == 0
    assert info.name_to_coin == {"BTC": "BTC"}


def test_seed_ignores_object_without_map():
    assert _seed_hip3_ws_remap(object()) == 0


def test_sdk_remap_resolves_after_seeding(monkeypatch):
    """Reproduce the real SDK failure path: before seeding, remapping a
    trades subscription for an xyz coin raises KeyError; after seeding it
    passes the coin through unchanged (which the server accepts)."""
    import hermes_trader.client.universe as uni
    monkeypatch.setattr(uni, "hip3_coin_names", lambda: ["xyz:SKHX"])

    class _SdkLike:
        def __init__(self):
            self.name_to_coin = {}

        def remap(self, sub):
            sub["coin"] = self.name_to_coin[sub["coin"]]

    sdk = _SdkLike()
    sub = {"type": "trades", "coin": "xyz:SKHX"}
    with pytest.raises(KeyError):
        sdk.remap(sub)
    _seed_hip3_ws_remap(sdk)
    sub2 = {"type": "trades", "coin": "xyz:SKHX"}
    sdk.remap(sub2)
    assert sub2["coin"] == "xyz:SKHX"
