"""Phase 4 (P0-1 / P0-3) tests for hermes_trader.realtime_feed.

Covers the three required paths for each feature:
  * normal (WS fresh / feed ok),
  * degraded (WS stale, REST live / feed degraded),
  * failed  (both feeds dead / feed down — the fail-closed classifier).
The pure decision module carries no I/O, so trading behaviour is never
exercised here; the trading-loop wiring is exercised separately.
"""

import time

import pytest

from hermes_trader.realtime_feed import (
    FEED_DEGRADED,
    FEED_DOWN,
    FEED_OK,
    FeedStatusTracker,
    classify_feed_status,
    dynamic_scan_interval,
)


# ── P0-1: dynamic_scan_interval ───────────────────────────────────────────────

class TestDynamicScanInterval:
    def test_disabled_always_returns_base(self):
        """Switch off (Phase-1 default): fixed cadence regardless of feed."""
        assert dynamic_scan_interval(15, ws_age_s=0.5, dynamic_on=False) == 15
        assert dynamic_scan_interval(15, ws_age_s=None, dynamic_on=False) == 15
        assert dynamic_scan_interval(15, ws_age_s=999.0, dynamic_on=False) == 15

    def test_normal_ws_fresh_uses_fast(self):
        # WS frame 1s old → fast 8s cadence.
        assert dynamic_scan_interval(15, ws_age_s=1.0, dynamic_on=True) == 8
        # Default fresh threshold is 10s; just under it still counts fresh.
        assert dynamic_scan_interval(15, ws_age_s=9.9, dynamic_on=True) == 8

    def test_degraded_ws_stale_uses_slow(self):
        # WS running but stale (frame older than the fresh budget) → slow.
        assert dynamic_scan_interval(15, ws_age_s=11.0, dynamic_on=True) == 20
        assert dynamic_scan_interval(15, ws_age_s=300.0, dynamic_on=True) == 20

    def test_failed_ws_absent_uses_slow(self):
        # WS not started at all (None, REST fallback) → slow cadence.
        assert dynamic_scan_interval(15, ws_age_s=None, dynamic_on=True) == 20

    def test_threshold_exact_boundary(self):
        # At exactly the fresh threshold, age is NOT < fresh_s → slow.
        assert dynamic_scan_interval(15, ws_age_s=10.0, dynamic_on=True) == 20

    def test_bad_age_input_does_not_raise(self):
        assert dynamic_scan_interval(15, ws_age_s="nonsense", dynamic_on=True) == 20


# ── P0-3: classify_feed_status ────────────────────────────────────────────────

class TestClassifyFeedStatus:
    def test_normal_ws_fresh_is_ok(self):
        assert classify_feed_status(ws_age_s=0.8, rest_age_s=None) == FEED_OK
        assert classify_feed_status(ws_age_s=9.0, rest_age_s=100.0) == FEED_OK

    def test_degraded_ws_dead_rest_live(self):
        # WS stale beyond its fresh budget but REST mids within 30s.
        assert classify_feed_status(ws_age_s=60.0, rest_age_s=5.0) == FEED_DEGRADED
        # WS absent entirely but REST healthy.
        assert classify_feed_status(ws_age_s=None, rest_age_s=20.0) == FEED_DEGRADED

    def test_failed_both_stale_is_down(self):
        # WS dead and REST beyond its freshness budget → down (fail-closed).
        assert classify_feed_status(ws_age_s=120.0, rest_age_s=120.0) == FEED_DOWN
        assert classify_feed_status(ws_age_s=None, rest_age_s=None) == FEED_DOWN

    def test_degraded_rest_exactly_at_budget(self):
        # REST age at exactly 30s is NOT < 30 → down.
        assert classify_feed_status(ws_age_s=None, rest_age_s=30.0) == FEED_DOWN


# ── P0-3: FeedStatusTracker edge + hysteresis ─────────────────────────────────

class TestFeedStatusTracker:
    def test_first_evaluation_is_silent_baseline(self):
        t = FeedStatusTracker(hold_seconds=30.0)
        # Normal startup: first classify (even degraded) seeds silently.
        assert t.evaluate(FEED_DEGRADED, now=1000.0) is None
        assert t.state == FEED_DEGRADED

    def test_upgrade_is_immediate(self):
        t = FeedStatusTracker(hold_seconds=30.0)
        t.evaluate(FEED_DOWN, now=0.0)
        # Recovery to ok fires right away, no hold.
        tr = t.evaluate(FEED_OK, now=1.0)
        assert tr is not None and tr["status"] == FEED_OK and tr["reason"] == "recovered"
        assert t.state == FEED_OK

    def test_downgrade_requires_persistence(self):
        t = FeedStatusTracker(hold_seconds=30.0)
        t.evaluate(FEED_OK, now=0.0)
        # First observation of degraded only ARMS the candidate (no event).
        assert t.evaluate(FEED_DEGRADED, now=1.0) is None
        assert t.state == FEED_OK
        # A blip that recovers within the hold window cancels the downgrade.
        assert t.evaluate(FEED_OK, now=2.0) is None
        # New degraded observation restarts the hold window.
        assert t.evaluate(FEED_DEGRADED, now=10.0) is None
        assert t.evaluate(FEED_DEGRADED, now=25.0) is None
        # Still degraded after hold → commit.
        tr = t.evaluate(FEED_DEGRADED, now=41.0)
        assert tr is not None and tr["status"] == FEED_DEGRADED
        assert tr["reason"] == "degraded"
        assert t.state == FEED_DEGRADED

    def test_down_after_degraded_commits_at_hold(self):
        t = FeedStatusTracker(hold_seconds=30.0)
        t.evaluate(FEED_OK, now=0.0)
        assert t.evaluate(FEED_DEGRADED, now=1.0) is None
        assert t.evaluate(FEED_DEGRADED, now=32.0) is not None
        assert t.state == FEED_DEGRADED
        # degraded → down is also a downgrade: needs its own hold window.
        assert t.evaluate(FEED_DOWN, now=33.0) is None
        assert t.evaluate(FEED_DOWN, now=64.0)["status"] == FEED_DOWN

    def test_unknown_status_ignored(self):
        t = FeedStatusTracker(hold_seconds=30.0)
        t.evaluate(FEED_OK, now=0.0)
        assert t.evaluate("weird", now=1.0) is None
        assert t.state == FEED_OK


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
