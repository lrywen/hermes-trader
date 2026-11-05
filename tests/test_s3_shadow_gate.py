"""Tests for hermes_trader/agents/s3_shadow_gate.py (S3 shadow-shorts gate).

Pins:
  * default-off zero behaviour change
  * SHADOW-only (LIVE/OFF always closed)
  * regime allow-list (down admitted, chop/up/unknown rejected)
  * fail-closed on missing regime
  * adaptive long-only skip semantics
  * impact-floor tagging is observation-only
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from hermes_trader.agents import s3_shadow_gate as s3


class TestS3Config:
    def test_missing_block_returns_safe_defaults(self):
        cfg = s3.s3_config({})
        assert cfg["enabled"] is False
        assert cfg["allow_regimes"] == ["down"]
        assert cfg["adaptive_scan_long_only"] is True

    def test_enabled_block_passthrough(self):
        cfg = s3.s3_config({"s3_shadow_shorts": {"enabled": True}})
        assert cfg["enabled"] is True
        assert cfg["allow_regimes"] == ["down"]

    def test_custom_regimes(self):
        cfg = s3.s3_config({"s3_shadow_shorts": {
            "enabled": True, "allow_regimes": ["down", "chop"]}})
        assert cfg["allow_regimes"] == ["down", "chop"]

    def test_empty_regimes_falls_back_to_down(self):
        cfg = s3.s3_config({"s3_shadow_shorts": {
            "enabled": True, "allow_regimes": []}})
        assert cfg["allow_regimes"] == ["down"]

    def test_bad_floor_falls_back(self):
        cfg = s3.s3_config({"s3_shadow_shorts": {
            "impact_measure_floor_usd": "nope"}})
        assert cfg["impact_measure_floor_usd"] == 5_000_000.0


class TestShadowShortAllowed:
    def test_disabled_by_default_blocks(self):
        assert s3.shadow_short_allowed(
            mode="SHADOW", side="short", regime="down", config={}) is False

    def test_long_side_always_false(self):
        cfg = {"s3_shadow_shorts": {"enabled": True}}
        assert s3.shadow_short_allowed(
            mode="SHADOW", side="long", regime="down", config=cfg) is False

    def test_live_mode_always_closed(self):
        cfg = {"s3_shadow_shorts": {"enabled": True}}
        assert s3.shadow_short_allowed(
            mode="LIVE", side="short", regime="down", config=cfg) is False

    def test_off_mode_always_closed(self):
        cfg = {"s3_shadow_shorts": {"enabled": True}}
        assert s3.shadow_short_allowed(
            mode="OFF", side="short", regime="down", config=cfg) is False

    def test_down_regime_admitted_in_shadow(self):
        cfg = {"s3_shadow_shorts": {"enabled": True}}
        assert s3.shadow_short_allowed(
            mode="SHADOW", side="short", regime="down", config=cfg) is True

    def test_chop_regime_rejected_by_default(self):
        cfg = {"s3_shadow_shorts": {"enabled": True}}
        assert s3.shadow_short_allowed(
            mode="SHADOW", side="short", regime="chop", config=cfg) is False

    def test_up_regime_rejected_by_default(self):
        cfg = {"s3_shadow_shorts": {"enabled": True}}
        assert s3.shadow_short_allowed(
            mode="SHADOW", side="short", regime="up", config=cfg) is False

    def test_unknown_regime_fail_closed(self):
        cfg = {"s3_shadow_shorts": {"enabled": True}}
        assert s3.shadow_short_allowed(
            mode="SHADOW", side="short", regime=None, config=cfg) is False
        assert s3.shadow_short_allowed(
            mode="SHADOW", side="short", regime="", config=cfg) is False

    def test_custom_regime_allowlist(self):
        cfg = {"s3_shadow_shorts": {
            "enabled": True, "allow_regimes": ["down", "chop"]}}
        assert s3.shadow_short_allowed(
            mode="SHADOW", side="short", regime="chop", config=cfg) is True


class TestAdaptiveLongOnlySkip:
    def test_scan_long_only_false_never_skips(self):
        assert s3.adaptive_long_only_skip(
            mode="SHADOW", coin="BTC", ta={}, held_coins=set(),
            scan_long_only=False, regime="down", config={}) is False

    def test_held_coin_never_skipped(self):
        assert s3.adaptive_long_only_skip(
            mode="SHADOW", coin="BTC",
            ta={"intend_short": True, "intend_long": False},
            held_coins={"BTC"}, scan_long_only=True, regime="down",
            config={}) is False

    def test_disabled_s3_delegates_to_original_skip(self):
        # Pure short intent, scan_long_only on, S3 off → original skip True.
        assert s3.adaptive_long_only_skip(
            mode="SHADOW", coin="ETH",
            ta={"intend_short": True, "intend_long": False},
            held_coins=set(), scan_long_only=True, regime="down",
            config={}) is True

    def test_enabled_s3_allows_short_in_down_regime(self):
        cfg = {"s3_shadow_shorts": {"enabled": True}}
        assert s3.adaptive_long_only_skip(
            mode="SHADOW", coin="ETH",
            ta={"intend_short": True, "intend_long": False},
            held_coins=set(), scan_long_only=True, regime="down",
            config=cfg) is False

    def test_enabled_s3_still_skips_short_in_chop(self):
        cfg = {"s3_shadow_shorts": {"enabled": True}}
        assert s3.adaptive_long_only_skip(
            mode="SHADOW", coin="ETH",
            ta={"intend_short": True, "intend_long": False},
            held_coins=set(), scan_long_only=True, regime="chop",
            config=cfg) is True

    def test_enabled_s3_live_mode_keeps_original_skip(self):
        cfg = {"s3_shadow_shorts": {"enabled": True}}
        assert s3.adaptive_long_only_skip(
            mode="LIVE", coin="ETH",
            ta={"intend_short": True, "intend_long": False},
            held_coins=set(), scan_long_only=True, regime="down",
            config=cfg) is True


class TestAdaptiveProductionDecision:
    def test_default_off_preserves_skip_without_regime_lookup(self, monkeypatch):
        from hermes_trader.agents import executor

        def forbidden(*args, **kwargs):
            raise AssertionError("disabled S3 must not resolve a scan regime")

        monkeypatch.setattr(executor, "resolve_decision_regime", forbidden)
        decision = s3.adaptive_long_only_decision(
            mode="SHADOW", coin="ETH",
            ta={"intend_short": True, "intend_long": False},
            held_coins=set(), scan_long_only=True, config={})
        assert decision == {"skip": True, "regime": None,
                            "s3_scan_bypass": False}

    def test_enabled_shadow_probe_allows_short_without_duplicate_audit(self, monkeypatch):
        from hermes_trader.agents import executor

        seen = {}

        def fake_resolve(probe, config, *, record_bypass=True):
            seen.update(probe=probe, record_bypass=record_bypass)
            return "down"

        monkeypatch.setattr(executor, "resolve_decision_regime", fake_resolve)
        cfg = {"s3_shadow_shorts": {"enabled": True}}
        decision = s3.adaptive_long_only_decision(
            mode="SHADOW", coin="ETH",
            ta={"intend_short": True, "intend_long": False},
            held_coins=set(), scan_long_only=True, config=cfg)
        assert decision == {"skip": False, "regime": "down",
                            "s3_scan_bypass": True}
        assert seen["probe"]["coin"] == "ETH"
        assert seen["probe"]["side"] == "short"
        assert seen["record_bypass"] is False

    def test_live_never_opens_scan_or_resolves_regime(self, monkeypatch):
        from hermes_trader.agents import executor

        monkeypatch.setattr(
            executor, "resolve_decision_regime",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("LIVE lookup")))
        decision = s3.adaptive_long_only_decision(
            mode="LIVE", coin="ETH",
            ta={"intend_short": True, "intend_long": False},
            held_coins=set(), scan_long_only=True,
            config={"s3_shadow_shorts": {"enabled": True}})
        assert decision["skip"] is True
        assert decision["s3_scan_bypass"] is False


class TestImpactFloorTag:
    def test_long_side_returns_none(self):
        tag = s3.impact_floor_tag(side="long", volume_24h_usd=100.0, config={})
        assert tag["s3_alt_floor_would_block"] is None

    def test_unknown_volume_returns_none_block(self):
        tag = s3.impact_floor_tag(side="short", volume_24h_usd=None, config={})
        assert tag["s3_alt_floor_would_block"] is None
        assert tag["s3_alt_floor_usd"] == 5_000_000.0

    def test_below_alt_floor_tagged(self):
        cfg = {"s3_shadow_shorts": {"impact_measure_floor_usd": 5_000_000.0}}
        tag = s3.impact_floor_tag(side="short", volume_24h_usd=3_000_000.0,
                                  config=cfg)
        assert tag["s3_alt_floor_would_block"] is True

    def test_above_alt_floor_not_tagged(self):
        cfg = {"s3_shadow_shorts": {"impact_measure_floor_usd": 5_000_000.0}}
        tag = s3.impact_floor_tag(side="short", volume_24h_usd=60_000_000.0,
                                  config=cfg)
        assert tag["s3_alt_floor_would_block"] is False

    def test_runner_record_preserves_frozen_floor_and_adds_alt_impact(
            self, tmp_path, monkeypatch):
        from hermes_trader.agents import executor

        path = tmp_path / "runner_gate.jsonl"
        monkeypatch.setattr(executor, "_RUNNER_GATE_SHADOW_FILE", str(path))
        monkeypatch.setattr(executor, "_runner_gate_volume_24h", lambda coin: 3e6)
        cfg = {"min_short_volume_usd": 1_000_000.0,
               "s3_shadow_shorts": {"impact_measure_floor_usd": 5_000_000.0}}
        analysis = {"id": "rg-s3", "coin": "ETH", "side": "short",
                    "composite_score": 55.0, "confidence": 0.8,
                    "reasoning": "contract"}
        executor._record_runner_gate_verdict(
            analysis, SimpleNamespace(gate_results={}), config=cfg)
        row = json.loads(path.read_text().strip())
        assert row["analysis_id"] == "rg-s3"
        assert row["short_floor_usd"] == 1_000_000.0
        assert row["short_floor_would_block"] is False
        assert row["s3_alt_floor_usd"] == 5_000_000.0
        assert row["s3_alt_floor_would_block"] is True
        assert cfg["min_short_volume_usd"] == 1_000_000.0
