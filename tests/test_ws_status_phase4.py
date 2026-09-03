"""Phase 4 P0-3 — ws_status edge event: Feishu routing, feed visibility,
and fail-closed independence.

Three required paths:
  * normal  — ws_status records are routed by notify_dispatch.dispatch():
              degraded → warn card, down → danger card ("已停止开新仓"),
              ok → success recovery card; distinct dedup keys per direction
              so a recovery card is never throttled by the prior alert.
  * degraded — the SSE public-feed filter keeps ws_status operator-only
              (NOT in _PUBLIC_FEED_EVENTS → filtered to None for anonymous
              clients, exactly like position_update / ws_user_fill).
  * failure — the feed halt / fail-closed gate (af14_feed_decision, a pure
              function lifted from scripts/trading_loop.py) closes entries
              on empty/stale/blind mids INDEPENDENTLY of the ws_status
              observability event: ws_status is pure observation and never
              opens or closes the gate itself.
"""
from __future__ import annotations

import ast
import logging
from pathlib import Path
from unittest import mock

import pytest

from hermes_trader import dashboard, notify, notify_dispatch

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def webhooks_on(monkeypatch: pytest.MonkeyPatch):
    """Enable the system notification category with a dummy webhook."""
    monkeypatch.setattr(notify, "FEISHU_WEBHOOK_URL", "https://open.feishu.cn/hook/X")
    monkeypatch.setattr(notify, "FEISHU_WEBHOOK_SECRET", "sec")
    monkeypatch.setattr(notify, "ENABLED_CATEGORIES",
                        {"trade", "signal", "risk", "system", "ai", "surge", "report"})
    monkeypatch.setattr(notify, "_last_sent", {})


def _status_record(status: str, previous: str = "",
                   ws_age: float | None = 60.0, rest_age: float | None = 5.0) -> dict:
    return {
        "event": "ws_status",
        "status": status,
        "previous": previous,
        "reason": "degraded" if status != "ok" else "recovered",
        "ws_age_s": ws_age,
        "rest_age_s": rest_age,
        "ts": 1700000000000,
    }


# ---------------------------------------------------------------------------
# Normal path — Feishu routing of the three edge states
# ---------------------------------------------------------------------------

class TestWsStatusDispatch:
    def test_down_routes_to_danger_card(self, webhooks_on) -> None:
        with mock.patch.object(notify, "send_card") as send_card:
            notify_dispatch.dispatch(_status_record("down", previous="degraded"))
        send_card.assert_called_once()
        _, kwargs = send_card.call_args
        assert kwargs["level"] == "danger"
        assert kwargs["category"] == "system"
        assert "停止开新仓" in send_card.call_args.args[0]
        # Distinct dedup key per transition direction.
        assert kwargs["dedup_key"] == "ws_status:degraded->down"

    def test_degraded_routes_to_warn_card(self, webhooks_on) -> None:
        with mock.patch.object(notify, "send_card") as send_card:
            notify_dispatch.dispatch(_status_record("degraded", previous="ok"))
        send_card.assert_called_once()
        _, kwargs = send_card.call_args
        assert kwargs["level"] == "warn"
        assert "REST" in send_card.call_args.args[0]
        assert kwargs["dedup_key"] == "ws_status:ok->degraded"

    def test_recovery_routes_to_success_card(self, webhooks_on) -> None:
        with mock.patch.object(notify, "send_card") as send_card:
            notify_dispatch.dispatch(
                _status_record("ok", previous="degraded", ws_age=2.0, rest_age=3.0))
        send_card.assert_called_once()
        _, kwargs = send_card.call_args
        assert kwargs["level"] == "success"
        assert "恢复" in send_card.call_args.args[0]
        assert kwargs["dedup_key"] == "ws_status:degraded->ok"

    def test_recovery_key_differs_from_degradation_key(self, webhooks_on) -> None:
        """The 10-min dedup must NOT swallow a recovery after a degradation."""
        keys: list[str] = []
        with mock.patch.object(notify, "send_card") as send_card:
            def _record(_title, **kw):
                keys.append(kw.get("dedup_key", ""))
                return True
            send_card.side_effect = _record
            notify_dispatch.dispatch(_status_record("degraded", previous="ok"))
            notify_dispatch.dispatch(_status_record("ok", previous="degraded"))
        assert len(keys) == 2
        assert keys[0] != keys[1]

    def test_unknown_status_ignored(self, webhooks_on) -> None:
        with mock.patch.object(notify, "send_card") as send_card:
            notify_dispatch.dispatch(_status_record("bogus"))
        send_card.assert_not_called()

    def test_dispatch_swallows_handler_errors(self) -> None:
        """A malformed record must never propagate (notifier best-effort)."""
        bad = {"event": "ws_status", "status": "down"}
        # send_card blows up internally; dispatch must not raise.
        with mock.patch.object(notify, "send_card", side_effect=RuntimeError("boom")):
            notify_dispatch.dispatch(bad)  # no exception


# ---------------------------------------------------------------------------
# Degraded path — ws_status stays operator-only on the public SSE feed
# ---------------------------------------------------------------------------

class TestWsStatusFeedVisibility:
    def test_ws_status_not_public(self) -> None:
        """Anonymous SSE clients must never receive feed-state transitions
        (they reveal operational posture). Allowlist-by-default: the event
        type is simply absent from _PUBLIC_FEED_EVENTS."""
        assert "ws_status" not in dashboard._PUBLIC_FEED_EVENTS

    def test_public_filter_drops_ws_status(self) -> None:
        assert dashboard._public_feed_filter(_status_record("down")) is None
        assert dashboard._public_feed_filter(_status_record("degraded")) is None

    def test_public_filter_passes_whitelisted_events(self) -> None:
        """Sanity: the filter still passes a genuinely public event type."""
        hb = {"event": "loop_heartbeat", "equity": 100.0,
              "available": 50.0, "daily_pnl": 1.0,
              "open_positions": 0, "config": {"mode": "OFF"}}
        out = dashboard._public_feed_filter(hb)
        assert out is not None
        assert out["event"] == "loop_heartbeat"


# ---------------------------------------------------------------------------
# Failure path — fail-closed gate is independent of the ws_status event
# ---------------------------------------------------------------------------

def _load_af14():
    """Lift af14_feed_decision + MID_FEED_MAX_STALE_S out of trading_loop.py
    (module-level ``while True`` makes it un-importable)."""
    repo_root = Path(__file__).resolve().parent.parent
    src = (repo_root / "scripts" / "trading_loop.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    wanted = {"af14_feed_decision"}
    nodes = []
    for n in tree.body:
        if isinstance(n, ast.FunctionDef) and n.name in wanted:
            nodes.append(n)
    # MID_FEED_MAX_STALE_S is imported (from exchange), not assigned in the
    # loop module; provide the real constant in the exec namespace.
    from hermes_trader.client.exchange import MID_FEED_MAX_STALE_S
    ns = {"logger": logging.getLogger("test.af14"),
          "MID_FEED_MAX_STALE_S": MID_FEED_MAX_STALE_S}
    module = ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[]))
    exec(compile(module, "<trading_loop extracted>", "exec"), ns)
    return ns["af14_feed_decision"], ns.get("MID_FEED_MAX_STALE_S", 30.0)


af14_feed_decision, _MAX_STALE = _load_af14()


class TestFailClosedIndependence:
    def test_healthy_mids_allow_entries(self) -> None:
        halt, skip_exits = af14_feed_decision({"BTC": 50000.0}, stale_age=2.0,
                                              missing_mids=set())
        assert halt is None
        assert skip_exits is False

    def test_empty_mids_halt_entries_but_keep_exits(self) -> None:
        """WS+REST both dead → empty snapshot → entries halted (fail-closed),
        monitor_exits still runs (exchange SLs backstop)."""
        halt, skip_exits = af14_feed_decision({}, stale_age=None,
                                              missing_mids=set())
        assert halt is not None
        assert "empty" in halt
        assert skip_exits is False

    def test_stale_mids_halt_entries_and_skip_exits(self) -> None:
        halt, skip_exits = af14_feed_decision({"BTC": 50000.0},
                                              stale_age=_MAX_STALE + 1,
                                              missing_mids=set())
        assert halt is not None
        assert "stale" in halt
        assert skip_exits is True  # do NOT close on a stale mid (wick exit)

    def test_blind_held_coin_halt_entries(self) -> None:
        halt, _ = af14_feed_decision({"ETH": 3000.0}, stale_age=1.0,
                                     missing_mids={"BTC"})
        assert halt is not None
        assert "BTC" in halt

    def test_ws_status_event_does_not_gate_trading(self) -> None:
        """The observability path (ws_status emission) and the control path
        (af14 gate) are decoupled: a feed classified as 'down' for the
        notifier does NOT itself block trading — only the actual mids
        snapshot fed to af14 does. This pins the single-writer / pure
        observation design so a future change can't accidentally wire the
        SSE event into the order path."""
        # The gate's decision depends ONLY on (mids, stale_age, missing_mids);
        # there is no status/state parameter it could read.
        import inspect
        sig = inspect.signature(af14_feed_decision)
        params = set(sig.parameters)
        assert params == {"mids", "stale_age", "missing_mids", "max_stale_s"}
        # Healthy inputs → allowed even though a ws_status=down event may be
        # emitted in the same cycle (they are independent code paths).
        halt, _ = af14_feed_decision({"BTC": 50000.0}, 1.0, set())
        assert halt is None
