# -*- coding: utf-8 -*-
"""Coin-scoped maker v2 selection tests (shadow-only, no funds).

Validates maker_v2_coins: global model must be v2 AND, when an allow-list is
configured, the coin must appear in it (bare symbol, USDT suffix stripped).
Empty allow-list => v2 applies to every coin (prior semantics). Any non-v2
model fails closed to v1.
"""
from __future__ import annotations

from hermes_trader.agents import shadow_book as sb


def _cfg(tmp, monkeypatch, **kw):
    monkeypatch.setattr(sb, "_shadow_cfg", lambda: dict(kw))


def test_v1_global_means_no_coin_uses_v2(monkeypatch):
    _cfg(None, monkeypatch, maker_fill_model="v1", maker_v2_coins=["BTC"])
    assert sb._maker_uses_v2("BTC") is False
    assert sb._maker_uses_v2("ETH") is False


def test_v2_empty_allowlist_applies_all_coins(monkeypatch):
    _cfg(None, monkeypatch, maker_fill_model="v2", maker_v2_coins=[])
    for coin in ("BTC", "ETH", "SOL"):
        assert sb._maker_uses_v2(coin) is True


def test_v2_allowlist_restricts_to_named_coin(monkeypatch):
    _cfg(None, monkeypatch, maker_fill_model="v2", maker_v2_coins=["BTC"])
    assert sb._maker_uses_v2("BTC") is True
    assert sb._maker_uses_v2("ETH") is False
    assert sb._maker_uses_v2("SOL") is False


def test_v2_allowlist_accepts_usdt_suffix_and_case(monkeypatch):
    _cfg(None, monkeypatch, maker_fill_model="v2", maker_v2_coins=["btcusdt"])
    assert sb._maker_uses_v2("BTC") is True
    assert sb._maker_uses_v2("BTCUSDT") is True
    assert sb._maker_uses_v2("ETH") is False


def test_missing_or_none_allowlist_means_all(monkeypatch):
    _cfg(None, monkeypatch, maker_fill_model="v2")
    assert sb._maker_v2_coins() is None
    assert sb._maker_uses_v2("BTC") is True
    _cfg(None, monkeypatch, maker_fill_model="v2", maker_v2_coins=None)
    assert sb._maker_uses_v2("ETH") is True
    _cfg(None, monkeypatch, maker_fill_model="v2", maker_v2_coins=[])
    assert sb._maker_uses_v2("ETH") is True


def test_malformed_or_empty_after_norm_fails_closed_to_v1(monkeypatch):
    # present but wrong type => restrict to nothing (v1 everywhere)
    _cfg(None, monkeypatch, maker_fill_model="v2", maker_v2_coins="BTC")
    assert sb._maker_v2_coins() == set()
    assert sb._maker_uses_v2("BTC") is False
    # present list that normalizes empty => v1 everywhere
    _cfg(None, monkeypatch, maker_fill_model="v2", maker_v2_coins=[None, "  "])
    assert sb._maker_uses_v2("BTC") is False


def test_v2_coin_set_parses_norm(monkeypatch):
    _cfg(None, monkeypatch, maker_v2_coins=["BTCUSDT", "eth"])
    assert sb._maker_v2_coins() == {"BTC", "ETH"}
