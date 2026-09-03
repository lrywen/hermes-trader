"""Phase-4 P1: REST-weight observability counters on the HL token bucket.

Covers the three required paths:
  * normal   — granted weight/requests accumulate (in-process AND shared file);
  * degraded — budget exhaustion counts exactly ONE denial per give-up,
               429 penalize() calls count, and a second bucket instance on the
               same state file sees the first one's totals (cross-process);
  * failure  — legacy 2-field / corrupt state files never crash and reset
               counters to zero; HERMES_HL_RATE_STATS=0 reverts to no counting.

The shared-bucket tests use a tmp_path state file instead of /dev/shm so the
suite never touches production limiter state.
"""
from __future__ import annotations

import pytest

from hermes_trader.client import rate_limit
from hermes_trader.client.rate_limit import SharedTokenBucket, TokenBucket

# ── in-process TokenBucket ──────────────────────────────────────────────

class TestTokenBucketStats:
    def test_grant_counts_weight_and_requests(self, monkeypatch):
        monkeypatch.delenv("HERMES_HL_RATE_STATS", raising=False)
        bucket = TokenBucket(600, 20.0)
        assert bucket.acquire(20, max_wait=1.0) is True
        assert bucket.acquire(40, max_wait=1.0) is True
        st = bucket.stats()
        assert st["granted_weight"] == 60.0
        assert st["granted_requests"] == 2
        assert st["denied_requests"] == 0
        assert st["penalized_requests"] == 0
        assert st["shared"] is False
        assert 0.0 <= st["tokens_available"] <= 600.0

    def test_denial_deadline_branch_counts_once(self, monkeypatch):
        # weight far exceeds capacity; refill can't cover it within max_wait
        # → the out-of-lock deadline branch fires and bumps denied exactly once.
        monkeypatch.delenv("HERMES_HL_RATE_STATS", raising=False)
        bucket = TokenBucket(capacity=10, refill_per_sec=20.0)
        assert bucket.acquire(100, max_wait=0.05) is False
        st = bucket.stats()
        assert st["denied_requests"] == 1
        assert st["granted_requests"] == 0

    def test_denial_no_refill_branch_counts(self, monkeypatch):
        # refill <= 0 → immediate False inside the lock; must still count.
        monkeypatch.delenv("HERMES_HL_RATE_STATS", raising=False)
        bucket = TokenBucket(capacity=10, refill_per_sec=0.0)
        assert bucket.acquire(100, max_wait=0.05) is False
        assert bucket.stats()["denied_requests"] == 1

    def test_penalize_counts(self, monkeypatch):
        monkeypatch.delenv("HERMES_HL_RATE_STATS", raising=False)
        bucket = TokenBucket(600, 20.0)
        bucket.penalize(50)
        bucket.penalize(10)
        assert bucket.stats()["penalized_requests"] == 2

    def test_stats_disabled_by_env(self, monkeypatch):
        # Rollback switch: HERMES_HL_RATE_STATS=0 → counters stay zero even
        # though granting/penalizing still works normally.
        monkeypatch.setenv("HERMES_HL_RATE_STATS", "0")
        bucket = TokenBucket(600, 20.0)
        assert bucket.acquire(20, max_wait=1.0) is True
        bucket.penalize(30)
        assert bucket.acquire(10_000, max_wait=0.02) is False
        st = bucket.stats()
        assert st["granted_weight"] == 0.0
        assert st["granted_requests"] == 0
        assert st["denied_requests"] == 0
        assert st["penalized_requests"] == 0


# ── cross-process SharedTokenBucket ─────────────────────────────────────

class TestSharedBucketStats:
    @pytest.fixture()
    def state_path(self, tmp_path):
        return str(tmp_path / "hl_rate.state")

    def test_shared_grant_persists_to_file(self, monkeypatch, state_path):
        monkeypatch.delenv("HERMES_HL_RATE_STATS", raising=False)
        bucket = SharedTokenBucket(600, 20.0, state_path)
        assert bucket.available is True
        assert bucket.acquire(20, max_wait=1.0) is True
        st = bucket.stats()
        assert st["shared"] is True
        assert st["granted_weight"] == 20.0
        assert st["granted_requests"] == 1

    def test_cross_process_visibility(self, monkeypatch, state_path):
        # Second instance on the same file = another process's view.
        monkeypatch.delenv("HERMES_HL_RATE_STATS", raising=False)
        writer = SharedTokenBucket(600, 20.0, state_path)
        reader = SharedTokenBucket(600, 20.0, state_path)
        assert writer.acquire(40, max_wait=1.0) is True
        st = reader.stats()
        assert st["granted_weight"] == 40.0
        assert st["granted_requests"] == 1
        assert st["denied_requests"] == 0

    def test_shared_denial_counts_once(self, monkeypatch, state_path):
        monkeypatch.delenv("HERMES_HL_RATE_STATS", raising=False)
        bucket = SharedTokenBucket(capacity=10, refill_per_sec=20.0, path=state_path)
        assert bucket.acquire(100, max_wait=0.05) is False
        assert bucket.stats()["denied_requests"] == 1

    def test_shared_penalize_counts(self, monkeypatch, state_path):
        monkeypatch.delenv("HERMES_HL_RATE_STATS", raising=False)
        bucket = SharedTokenBucket(600, 20.0, state_path)
        bucket.penalize(100)
        assert bucket.stats()["penalized_requests"] == 1

    def test_legacy_two_field_file_is_compatible(self, monkeypatch, state_path):
        # A file written by pre-Phase-4 code (tokens + monotonic only) must
        # read as zeroed counters, not crash, and counting resumes cleanly.
        monkeypatch.delenv("HERMES_HL_RATE_STATS", raising=False)
        with open(state_path, "w") as f:
            f.write("600.000000 1234.500000\n")
        bucket = SharedTokenBucket(600, 20.0, state_path)
        st = bucket.stats()
        assert st["granted_weight"] == 0.0
        assert st["granted_requests"] == 0
        # Next write migrates the file to the 6-field format.
        assert bucket.acquire(20, max_wait=1.0) is True
        assert bucket.stats()["granted_weight"] == 20.0

    def test_corrupt_file_never_raises(self, monkeypatch, state_path):
        monkeypatch.delenv("HERMES_HL_RATE_STATS", raising=False)
        with open(state_path, "w") as f:
            f.write("garbage-not-floats\n")
        bucket = SharedTokenBucket(600, 20.0, state_path)
        st = bucket.stats()  # must not raise
        assert st["granted_weight"] == 0.0
        assert bucket.acquire(20, max_wait=1.0) is True

    def test_stats_disabled_shared(self, monkeypatch, state_path):
        monkeypatch.setenv("HERMES_HL_RATE_STATS", "0")
        bucket = SharedTokenBucket(600, 20.0, state_path)
        assert bucket.acquire(20, max_wait=1.0) is True
        bucket.penalize(10)
        st = bucket.stats()
        assert st["granted_weight"] == 0.0
        assert st["granted_requests"] == 0
        assert st["penalized_requests"] == 0

    def test_unavailable_bucket_stats_are_zeros(self, monkeypatch, tmp_path):
        # A state path that can't be opened → bucket degrades to unavailable
        # (grant-on-failure); stats must report zeros with shared=False.
        monkeypatch.delenv("HERMES_HL_RATE_STATS", raising=False)
        bad_path = str(tmp_path / "missing_dir" / "rate.state")
        bucket = SharedTokenBucket(600, 20.0, bad_path)
        assert bucket.available is False
        st = bucket.stats()
        assert st["shared"] is False
        assert st["granted_weight"] == 0.0


# ── /metrics exposure ───────────────────────────────────────────────────

class TestMetricsExposure:
    def test_render_metrics_contains_hl_rest_gauges(self):
        from hermes_trader import metrics

        body, _ct = metrics.render_metrics()
        text = body.decode("utf-8", "ignore")
        assert "hermes_hl_rest_granted_weight_total" in text
        assert "hermes_hl_rest_granted_requests_total" in text
        assert "hermes_hl_rest_denied_requests_total" in text
        assert "hermes_hl_rest_penalized_requests_total" in text
        assert "hermes_hl_rest_tokens_available" in text

    def test_refresh_reads_limiter_stats(self, monkeypatch, tmp_path):
        # Point the module-level limiter at a tmp shared bucket, drive one
        # grant, and confirm _refresh() surfaces it without raising.
        from hermes_trader import metrics

        state = str(tmp_path / "hl_rate.state")
        monkeypatch.setattr(
            rate_limit, "HL_LIMITER", SharedTokenBucket(600, 20.0, state)
        )
        monkeypatch.delenv("HERMES_HL_RATE_STATS", raising=False)
        rate_limit.HL_LIMITER.acquire(20, max_wait=1.0)
        # _refresh imports HL_LIMITER from the rate_limit module namespace.
        metrics._refresh()
        body, _ = metrics.render_metrics()
        text = body.decode("utf-8", "ignore")
        assert "hermes_hl_rest_granted_requests_total 1.0" in text
