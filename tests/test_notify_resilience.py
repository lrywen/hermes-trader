"""R11-A1 — notify send-side resilience (retry / circuit / fallback).

Covers:
  * Retry on 429 / 5xx / URLError with exponential backoff.
  * Non-retryable errors (400/404) fail fast without burning retries.
  * Per-channel circuit breaker opens after N consecutive failures and
    short-circuits subsequent sends (no network call).
  * A successful send resets the breaker counter.
  * Cross-channel fallback: primary channel dead → non-trade / signal bot
    accepts the card.
  * send_text() goes through the same retry / circuit / fallback machinery.
  * All paths are NEVER-RAISING (a notification outage cannot break the
    caller) — a raised exception in any code path is itself a test failure.

The strategy: replace ``urllib.request.urlopen`` with a stub that returns
the response we want (success / 429 / 500 / 400 / connection error). The
retry backoff sleeps are patched out so the suite is fast.
"""
from __future__ import annotations

import json
import urllib.error
from unittest import mock

import pytest

from hermes_trader import notify
from hermes_trader import metrics


PRIMARY_URL = "https://open.feishu.cn/hook/PRIMARY"
PRIMARY_SECRET = "primary-secret"
SIGNAL_URL = "https://open.feishu.cn/hook/SIGNAL"
SIGNAL_SECRET = "signal-secret"
NONTRADE_URL = "https://open.feishu.cn/hook/NONTRADE"
NONTRADE_SECRET = "nontrade-secret"


# ── helpers ────────────────────────────────────────────────────────────


class FakeHTTPError(urllib.error.HTTPError):
    """A real ``urllib.error.HTTPError`` so ``_post_with_retry``'s HTTPError
    branch classifies it by ``.code`` (429/5xx → retry, 4xx → fail fast)."""

    def __init__(self, code: int, msg: str = "boom") -> None:
        # HTTPError signature: (url, code, msg, hdrs, fp). We don't need
        # real hdrs / fp for the notify code path.
        super().__init__(url="https://stub/", code=code, msg=msg, hdrs={}, fp=None)


class _FakeResponse:
    """Minimal stand-in for ``urllib.request.urlopen``'s context manager.

    Yields a body of ``self.body`` on ``.read()`` and exits cleanly.
    """
    def __init__(self, body: bytes) -> None:
        self.body = body

    def read(self) -> bytes:
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _ok_response():
    """Return a context manager whose .read() yields Feishu success JSON."""
    return _FakeResponse(json.dumps({"code": 0, "msg": "ok"}).encode("utf-8"))


def _http_response(status_payload: bytes):
    """Return a context manager whose .read() yields the given payload bytes
    (used to simulate Feishu's non-zero code response)."""
    return _FakeResponse(status_payload)


def _patch_sleep(monkeypatch):
    """Patch out ``time.sleep`` so backoff is instant."""
    monkeypatch.setattr(notify.time, "sleep", mock.MagicMock())


@pytest.fixture
def three_webhooks(monkeypatch):
    """Configure all three webhook pairs and a permissive allow-list."""
    monkeypatch.setattr(notify, "FEISHU_WEBHOOK_URL", PRIMARY_URL)
    monkeypatch.setattr(notify, "FEISHU_WEBHOOK_SECRET", PRIMARY_SECRET)
    monkeypatch.setattr(notify, "FEISHU_SIGNAL_WEBHOOK_URL", SIGNAL_URL)
    monkeypatch.setattr(notify, "FEISHU_SIGNAL_WEBHOOK_SECRET", SIGNAL_SECRET)
    monkeypatch.setattr(notify, "FEISHU_NON_TRADE_WEBHOOK_URL", NONTRADE_URL)
    monkeypatch.setattr(notify, "FEISHU_NON_TRADE_WEBHOOK_SECRET", NONTRADE_SECRET)
    monkeypatch.setattr(
        notify, "ENABLED_CATEGORIES",
        {"trade", "signal", "risk", "system", "ai", "surge", "report"},
    )
    monkeypatch.setattr(notify, "_last_sent", {})
    # Reset circuit-breaker state between tests.
    monkeypatch.setattr(notify, "_cb_state_by_channel", {})
    monkeypatch.setattr(notify, "_cb_open_until", 0.0)
    monkeypatch.setattr(notify, "_cb_failures", 0)
    # Tighter retry timing so tests run fast.
    monkeypatch.setattr(notify, "_RETRY_MAX_TRIES", 3)
    monkeypatch.setattr(notify, "_RETRY_BACKOFF_BASE_S", 0.0)
    monkeypatch.setattr(notify, "_RETRY_BACKOFF_MAX_S", 0.0)
    monkeypatch.setattr(notify, "_CB_FAILURE_THRESHOLD", 3)
    monkeypatch.setattr(notify, "_CB_OPEN_S", 60.0)
    # Patch out time.sleep so backoff is instant.
    _patch_sleep(monkeypatch)


# ── retry classification ──────────────────────────────────────────────


def test_429_is_retryable(monkeypatch):
    monkeypatch.setattr(notify, "_RETRY_MAX_TRIES", 2)
    monkeypatch.setattr(notify, "_RETRY_BACKOFF_BASE_S", 0.0)
    _patch_sleep(monkeypatch)
    calls = []

    def fake_urlopen(req, timeout=5.0):
        calls.append(req.full_url)
        if len(calls) == 1:
            raise FakeHTTPError(429, "rate limit")
        return _ok_response()

    with mock.patch.object(notify.urllib.request, "urlopen",
                           side_effect=fake_urlopen):
        ok = notify._post_with_retry({"x": 1}, PRIMARY_URL, "")
    assert ok is True
    assert len(calls) == 2  # one 429 + one success


def test_500_is_retryable(monkeypatch):
    monkeypatch.setattr(notify, "_RETRY_MAX_TRIES", 3)
    monkeypatch.setattr(notify, "_RETRY_BACKOFF_BASE_S", 0.0)
    _patch_sleep(monkeypatch)
    calls = []

    def fake_urlopen(req, timeout=5.0):
        calls.append(req.full_url)
        if len(calls) < 3:
            raise FakeHTTPError(500, "server boom")
        return _ok_response()

    with mock.patch.object(notify.urllib.request, "urlopen",
                           side_effect=fake_urlopen):
        ok = notify._post_with_retry({"x": 1}, PRIMARY_URL, "")
    assert ok is True
    assert len(calls) == 3


def test_400_is_not_retried(monkeypatch):
    """4xx other than 429 is treated as a permanent failure (caller will
    move on to the fallback channel rather than waste retries)."""
    _patch_sleep(monkeypatch)
    calls = []

    def fake_urlopen(req, timeout=5.0):
        calls.append(req.full_url)
        raise FakeHTTPError(400, "bad request")

    with mock.patch.object(notify.urllib.request, "urlopen",
                           side_effect=fake_urlopen):
        ok = notify._post_with_retry({"x": 1}, PRIMARY_URL, "")
    assert ok is False
    assert len(calls) == 1  # no retry


def test_401_is_not_retried(monkeypatch):
    _patch_sleep(monkeypatch)
    calls = []

    def fake_urlopen(req, timeout=5.0):
        calls.append(req.full_url)
        raise FakeHTTPError(401, "unauthorized")

    with mock.patch.object(notify.urllib.request, "urlopen",
                           side_effect=fake_urlopen):
        ok = notify._post_with_retry({"x": 1}, PRIMARY_URL, "")
    assert ok is False
    assert len(calls) == 1


def test_urlerror_is_retried(monkeypatch):
    monkeypatch.setattr(notify, "_RETRY_MAX_TRIES", 3)
    monkeypatch.setattr(notify, "_RETRY_BACKOFF_BASE_S", 0.0)
    _patch_sleep(monkeypatch)
    calls = []

    def fake_urlopen(req, timeout=5.0):
        calls.append(req.full_url)
        if len(calls) < 2:
            raise urllib.error.URLError("network down")
        return _ok_response()

    with mock.patch.object(notify.urllib.request, "urlopen",
                           side_effect=fake_urlopen):
        ok = notify._post_with_retry({"x": 1}, PRIMARY_URL, "")
    assert ok is True
    assert len(calls) == 2


def test_retry_exhausted_returns_false(monkeypatch):
    monkeypatch.setattr(notify, "_RETRY_MAX_TRIES", 3)
    monkeypatch.setattr(notify, "_RETRY_BACKOFF_BASE_S", 0.0)
    _patch_sleep(monkeypatch)
    calls = []

    def fake_urlopen(req, timeout=5.0):
        calls.append(req.full_url)
        raise FakeHTTPError(503, "all calls fail")

    with mock.patch.object(notify.urllib.request, "urlopen",
                           side_effect=fake_urlopen):
        ok = notify._post_with_retry({"x": 1}, PRIMARY_URL, "")
    assert ok is False
    assert len(calls) == 3


def test_retry_records_metric(monkeypatch):
    """Each backoff between retries increments NOTIFY_SEND_RETRIES."""
    monkeypatch.setattr(notify, "_RETRY_MAX_TRIES", 2)
    monkeypatch.setattr(notify, "_RETRY_BACKOFF_BASE_S", 0.0)
    _patch_sleep(monkeypatch)
    calls = []

    def fake_urlopen(req, timeout=5.0):
        calls.append(req.full_url)
        if len(calls) == 1:
            raise FakeHTTPError(429, "rl")
        return _ok_response()

    with mock.patch.object(notify.urllib.request, "urlopen",
                           side_effect=fake_urlopen):
        # Track every .inc() call across all labels.
        with mock.patch.object(
            metrics.NOTIFY_SEND_RETRIES, "labels",
            side_effect=lambda **kw: mock.MagicMock(inc=mock.MagicMock()),
        ) as labels_mock:
            notify._post_with_retry({"x": 1}, PRIMARY_URL, "")
            assert labels_mock.call_count >= 1, (
                "NOTIFY_SEND_RETRIES.labels should be called at least once"
            )


# ── circuit breaker ───────────────────────────────────────────────────


def test_circuit_opens_after_threshold(three_webhooks, monkeypatch):
    """After N consecutive failures, the breaker opens and short-circuits
    subsequent sends (no urlopen call at all)."""
    monkeypatch.setattr(notify, "_CB_FAILURE_THRESHOLD", 3)
    monkeypatch.setattr(notify, "_CB_OPEN_S", 60.0)
    monkeypatch.setattr(notify, "_RETRY_MAX_TRIES", 1)

    call_count = {"n": 0}

    def fake_urlopen(req, timeout=5.0):
        call_count["n"] += 1
        raise FakeHTTPError(500, "down")

    with mock.patch.object(notify.urllib.request, "urlopen",
                           side_effect=fake_urlopen):
        # First 3 calls: each opens a fresh attempt, all fail.
        for _ in range(3):
            notify._post_with_retry({"x": 1}, PRIMARY_URL, "")
        # After 3 failures, the breaker should be open.
        assert notify._cb_is_open(PRIMARY_URL) is True
        # Next call should short-circuit — no urlopen invocation.
        before = call_count["n"]
        ok = notify._post_with_retry({"x": 1}, PRIMARY_URL, "")
        assert ok is False
        assert call_count["n"] == before


def test_circuit_resets_on_success(three_webhooks, monkeypatch):
    """A successful send resets the per-channel failure counter.

    Two send attempts against PRIMARY: first raises 5xx (failure recorded,
    counter → 1), then the URL succeeds (counter → 0, breaker closed).
    """
    monkeypatch.setattr(notify, "_CB_FAILURE_THRESHOLD", 3)
    monkeypatch.setattr(notify, "_RETRY_MAX_TRIES", 1)

    fail_then_succeed = [
        FakeHTTPError(500, "fail"),
        _ok_response(),
    ]

    def fake_urlopen(req, timeout=5.0):
        item = fail_then_succeed.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    with mock.patch.object(notify.urllib.request, "urlopen",
                           side_effect=fake_urlopen):
        # First call: raises 5xx → cb failures → 1, returns False.
        ok1 = notify._post_with_retry({"x": 1}, PRIMARY_URL, "")
        assert ok1 is False
        assert notify._cb_state_by_channel[PRIMARY_URL][1] == 1

        # Second call: succeeds → cb failures reset to 0, returns True.
        ok2 = notify._post_with_retry({"x": 1}, PRIMARY_URL, "")
        assert ok2 is True
        assert notify._cb_state_by_channel[PRIMARY_URL][1] == 0
        assert notify._cb_state_by_channel[PRIMARY_URL][0] == 0.0


def test_circuit_open_recorded_in_metric(three_webhooks, monkeypatch):
    """Opening the breaker sets NOTIFY_CIRCUIT_OPEN to 1.0 for that channel."""
    monkeypatch.setattr(notify, "_CB_FAILURE_THRESHOLD", 2)
    monkeypatch.setattr(notify, "_RETRY_MAX_TRIES", 1)

    def fake_urlopen(req, timeout=5.0):
        raise FakeHTTPError(500, "down")

    set_calls: list = []
    set_target = {"value": None}

    def fake_labels(**kw):
        m = mock.MagicMock()
        def fake_set(v):
            set_calls.append((kw.get("channel"), v))
            set_target["value"] = v
        m.set = fake_set
        return m

    with mock.patch.object(notify.urllib.request, "urlopen",
                           side_effect=fake_urlopen):
        with mock.patch.object(
            metrics.NOTIFY_CIRCUIT_OPEN, "labels", side_effect=fake_labels,
        ):
            notify._post_with_retry({"x": 1}, PRIMARY_URL, "")
            notify._post_with_retry({"x": 1}, PRIMARY_URL, "")
    # At least one set call should have set the gauge to 1.0 for PRIMARY.
    open_calls = [v for (chan, v) in set_calls
                  if chan == PRIMARY_URL and v == 1.0]
    assert open_calls, (
        f"expected at least one gauge.set(1.0) call for {PRIMARY_URL}, "
        f"got {set_calls}"
    )


# ── cross-channel fallback ────────────────────────────────────────────


def test_fallback_chain_orders_resolved_first(three_webhooks):
    """The primary channel for the category leads the chain; the others
    follow in deterministic order."""
    chain = notify._fallback_chain("trade")
    assert chain[0] == (PRIMARY_URL, PRIMARY_SECRET)
    assert SIGNAL_URL in [u for u, _ in chain]
    assert NONTRADE_URL in [u for u, _ in chain]
    # Deduped.
    urls = [u for u, _ in chain]
    assert len(urls) == len(set(urls))


def test_fallback_chain_signal_first_for_signal(three_webhooks):
    chain = notify._fallback_chain("signal")
    assert chain[0] == (SIGNAL_URL, SIGNAL_SECRET)


def test_fallback_chain_dedupes_when_one_unset(three_webhooks, monkeypatch):
    """When only primary is configured, the chain is just [primary]."""
    monkeypatch.setattr(notify, "FEISHU_SIGNAL_WEBHOOK_URL", "")
    monkeypatch.setattr(notify, "FEISHU_NON_TRADE_WEBHOOK_URL", "")
    chain = notify._fallback_chain("trade")
    assert chain == [(PRIMARY_URL, PRIMARY_SECRET)]


def test_send_card_falls_back_when_primary_5xx(three_webhooks, monkeypatch):
    """If the primary channel returns 5xx, send_card should fall through to
    the non-trade bot and return True."""
    monkeypatch.setattr(notify, "_RETRY_MAX_TRIES", 1)
    monkeypatch.setattr(notify, "_CB_FAILURE_THRESHOLD", 100)  # never trip cb
    seen = []

    def fake_urlopen(req, timeout=5.0):
        seen.append(req.full_url)
        if req.full_url == PRIMARY_URL:
            raise FakeHTTPError(500, "primary dead")
        if req.full_url == SIGNAL_URL:
            raise FakeHTTPError(500, "signal dead")
        return _ok_response()  # non-trade succeeds

    with mock.patch.object(notify.urllib.request, "urlopen",
                           side_effect=fake_urlopen):
        ok = notify.send_card("测试", fields={"a": 1}, category="trade")
    assert ok is True
    assert NONTRADE_URL in seen
    # The primary must have been tried first.
    assert seen[0] == PRIMARY_URL


def test_send_card_returns_false_when_all_channels_fail(three_webhooks, monkeypatch):
    """If every channel fails, send_card returns False (never raises)."""
    monkeypatch.setattr(notify, "_RETRY_MAX_TRIES", 1)
    monkeypatch.setattr(notify, "_CB_FAILURE_THRESHOLD", 100)

    def fake_urlopen(req, timeout=5.0):
        raise FakeHTTPError(500, "all dead")

    with mock.patch.object(notify.urllib.request, "urlopen",
                           side_effect=fake_urlopen):
        ok = notify.send_card("测试", category="trade")
    assert ok is False


def test_send_card_short_circuits_when_breaker_open(three_webhooks):
    """If the primary channel's breaker is open, send_card tries the next
    channel without even attempting a request to the dead one."""
    # Manually open the primary breaker.
    notify._cb_state_by_channel[PRIMARY_URL] = (
        notify.time.time() + 600, 5,  # open 10 min, 5 failures
    )
    seen = []

    def fake_urlopen(req, timeout=5.0):
        seen.append(req.full_url)
        return _ok_response()

    with mock.patch.object(notify.urllib.request, "urlopen",
                           side_effect=fake_urlopen):
        ok = notify.send_card("测试", category="trade")
    assert ok is True
    # Primary must NOT have been called.
    assert PRIMARY_URL not in seen


def test_send_card_records_fallback_metric(three_webhooks, monkeypatch):
    """When the primary is dead and a fallback channel succeeds, the
    NOTIFY_FALLBACK_USED counter is incremented for the category."""
    monkeypatch.setattr(notify, "_RETRY_MAX_TRIES", 1)
    monkeypatch.setattr(notify, "_CB_FAILURE_THRESHOLD", 100)

    def fake_urlopen(req, timeout=5.0):
        if req.full_url == PRIMARY_URL:
            raise FakeHTTPError(500, "primary dead")
        return _ok_response()

    label_calls: list = []
    def fake_labels(**kw):
        label_calls.append(kw)
        m = mock.MagicMock()
        m.inc = mock.MagicMock()
        return m

    with mock.patch.object(notify.urllib.request, "urlopen",
                           side_effect=fake_urlopen):
        with mock.patch.object(
            metrics.NOTIFY_FALLBACK_USED, "labels", side_effect=fake_labels,
        ):
            notify.send_card("测试", category="trade")
    assert label_calls, "NOTIFY_FALLBACK_USED.labels should be called"


# ── send_text fallback ────────────────────────────────────────────────


def test_send_text_uses_fallback_chain(three_webhooks, monkeypatch):
    """send_text goes through the same retry/circuit/fallback machinery."""
    monkeypatch.setattr(notify, "_RETRY_MAX_TRIES", 1)
    monkeypatch.setattr(notify, "_CB_FAILURE_THRESHOLD", 100)
    seen = []

    def fake_urlopen(req, timeout=5.0):
        seen.append(req.full_url)
        if req.full_url == PRIMARY_URL:
            raise FakeHTTPError(500, "primary dead")
        return _ok_response()

    with mock.patch.object(notify.urllib.request, "urlopen",
                           side_effect=fake_urlopen):
        ok = notify.send_text("daily report", category="report")
    assert ok is True
    assert NONTRADE_URL in seen


def test_send_text_never_raises(three_webhooks, monkeypatch):
    """Even if every channel raises a non-retryable error, send_text
    returns False instead of letting the exception escape."""
    monkeypatch.setattr(notify, "_RETRY_MAX_TRIES", 1)
    monkeypatch.setattr(notify, "_CB_FAILURE_THRESHOLD", 100)

    def fake_urlopen(req, timeout=5.0):
        raise RuntimeError("complete outage")

    with mock.patch.object(notify.urllib.request, "urlopen",
                           side_effect=fake_urlopen):
        ok = notify.send_text("daily report", category="report")
    assert ok is False


# ── never-raises contract ─────────────────────────────────────────────


def test_send_card_never_raises_even_on_unexpected_exception(three_webhooks):
    """A completely unexpected exception inside _send_with_fallback must
    not propagate — the caller (trading loop) is sacred.

    Note: send_card wraps _send_with_fallback in a bare ``except`` so even
    if the helper raises mid-flight we return False rather than letting
    the exception break the trading loop.
    """
    with mock.patch.object(notify, "_fallback_chain",
                           side_effect=RuntimeError("boom")):
        ok = notify.send_card("测试", category="trade")
    assert ok is False


def test_retry_metrics_increment_on_retry(three_webhooks, monkeypatch):
    """Each backoff between retries increments NOTIFY_SEND_RETRIES."""
    monkeypatch.setattr(notify, "_RETRY_MAX_TRIES", 3)
    seen = []

    def fake_urlopen(req, timeout=5.0):
        seen.append(req.full_url)
        if len(seen) < 3:
            raise FakeHTTPError(429, "rl")
        return _ok_response()

    label_calls: list = []
    def fake_labels(**kw):
        label_calls.append(kw)
        m = mock.MagicMock()
        m.inc = mock.MagicMock()
        return m

    with mock.patch.object(notify.urllib.request, "urlopen",
                           side_effect=fake_urlopen):
        with mock.patch.object(
            metrics.NOTIFY_SEND_RETRIES, "labels", side_effect=fake_labels,
        ):
            notify._post_with_retry({"x": 1}, PRIMARY_URL, "")
    # 2 retries between 3 attempts → 2 NOTIFY_SEND_RETRIES.labels() calls.
    assert len(label_calls) == 2, (
        f"expected 2 retry labels, got {len(label_calls)}"
    )


# ── Feishu non-zero code ─────────────────────────────────────────────


def test_feishu_non_zero_code_does_not_trip_breaker(three_webhooks, monkeypatch):
    """Feishu returning code != 0 (e.g. 19003) means the channel is up but
    the payload was rejected. We don't want to open the breaker on that —
    we'd start skipping all sends to a healthy bot."""
    monkeypatch.setattr(notify, "_RETRY_MAX_TRIES", 1)
    bad_payload = json.dumps({"code": 19003, "msg": "rate limit"}).encode()

    def fake_urlopen(req, timeout=5.0):
        return _http_response(bad_payload)

    with mock.patch.object(notify.urllib.request, "urlopen",
                           side_effect=fake_urlopen):
        ok = notify._post_with_retry({"x": 1}, PRIMARY_URL, "")
    assert ok is False
    # Breaker must NOT have opened — the channel is up.
    entry = notify._cb_state_by_channel.get(PRIMARY_URL)
    if entry is not None:
        assert entry[0] == 0.0


# ── per-channel isolation ─────────────────────────────────────────────


def test_per_channel_breaker_isolation(three_webhooks, monkeypatch):
    """A dead primary must not prevent sends to the signal channel."""
    monkeypatch.setattr(notify, "_CB_FAILURE_THRESHOLD", 2)
    # Open the primary breaker manually.
    notify._cb_state_by_channel[PRIMARY_URL] = (
        notify.time.time() + 600, 5,
    )
    # Signal channel should still be open for business.
    assert notify._cb_is_open(PRIMARY_URL) is True
    assert notify._cb_is_open(SIGNAL_URL) is False
    assert notify._cb_is_open(NONTRADE_URL) is False
