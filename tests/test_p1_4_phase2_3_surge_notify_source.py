"""P1-4 Phase 2.3 - loop_start surge score reads the effective loop_runtime value.

The Feishu startup card's "暴涨通知分" field used to re-read
HERMES_SURGE_MIN_SCORE directly from the environment - a second truth source
that bypassed loop_runtime resolution (legacy env > agent-config > default).
After Phase 2.1 retired that env in the deployment, the card would silently
fall back to the hard-coded "40" even when agent-config carries a different
value.

The trading loop now emits the resolved effective value on the loop_start
event and the dispatcher renders that value. The legacy env stays only as a
fallback for records produced by older emitters (same precedence as
loop_runtime resolution).
"""
from __future__ import annotations

from unittest import mock

from hermes_trader import notify_dispatch


def _loop_start_record(**over):
    record = {
        "event": "loop_start",
        "scan_interval": 300,
        "min_score": 50,
        "config": {"mode": "SHADOW", "scan": {"minCompositeScore": 50}},
    }
    record.update(over)
    return record


def _dispatch_card_fields(record):
    with mock.patch.object(notify_dispatch.notify, "send_card") as m:
        notify_dispatch.dispatch(record)
    m.assert_called_once()
    return m.call_args.kwargs["fields"]


def test_loop_start_card_shows_effective_surge_score_from_event(monkeypatch):
    monkeypatch.delenv("HERMES_SURGE_MIN_SCORE", raising=False)
    fields = _dispatch_card_fields(_loop_start_record(surge_min_score=42.5))
    assert fields["暴涨通知分"] == "42.5"


def test_loop_start_card_integral_float_renders_without_dot_zero(monkeypatch):
    monkeypatch.delenv("HERMES_SURGE_MIN_SCORE", raising=False)
    fields = _dispatch_card_fields(_loop_start_record(surge_min_score=40.0))
    assert fields["暴涨通知分"] == "40"


def test_loop_start_card_event_value_takes_precedence_over_env(monkeypatch):
    monkeypatch.setenv("HERMES_SURGE_MIN_SCORE", "99")
    fields = _dispatch_card_fields(_loop_start_record(surge_min_score=35))
    assert fields["暴涨通知分"] == "35"


def test_loop_start_card_falls_back_to_env_for_legacy_records(monkeypatch):
    # Record from an older emitter without the surge_min_score field.
    monkeypatch.setenv("HERMES_SURGE_MIN_SCORE", "37")
    fields = _dispatch_card_fields(_loop_start_record())
    assert fields["暴涨通知分"] == "37"


def test_loop_start_card_defaults_to_40_without_event_field_or_env(monkeypatch):
    monkeypatch.delenv("HERMES_SURGE_MIN_SCORE", raising=False)
    fields = _dispatch_card_fields(_loop_start_record())
    assert fields["暴涨通知分"] == "40"
