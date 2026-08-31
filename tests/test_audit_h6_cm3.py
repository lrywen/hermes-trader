"""Deep-audit (2026-08-28) remediation tests: H6 + C-M3.

H6 (high): an order placement whose HTTP response is LOST (408 / read timeout /
SSL drop after submit) used to be treated as a definite failure — the in-flight
marker was cleared and nothing was recorded, even though the order may have
reached Hyperliquid and FILLED. Result: an exchange position with no local
tracker and no stop-loss (an orphan window). The executor now reconciles the
ambiguous outcome against userFills by Cloid before giving up:
  • confirmed FILLED   → backfill order_res with the REAL avgPx/total_sz and
                         fall through the normal register_position / record_trade
                         / backup-SL path (no orphan, no duplicated logic);
  • confirmed NOT filled → safe to clear + retry;
  • lookup itself FAILED (deaf exchange) → immediate rehydrate backstop + a
                         consecutive-unresolved streak; N in a row arms a global
                         auto-entry halt (C-M3).

C-M3 (medium): 408 was missing from the retry whitelist, and transport/timeout
exceptions after submit were not distinguished from definite rejections.
  • urllib3 Retry status_forcelist now includes 408 (POST retry is safe for
    order placement because the Cloid idempotency key makes a duplicate
    submission a rejected dup, not a double fill);
  • place_hl_order / place_hl_trigger_order tag such failures
    error_code="response_unknown" so callers reconcile; a 400/401/403 is a
    definite rejection and stays untagged.
"""
import time
import uuid

import pytest


# ── C-M3: exception classification ────────────────────────────────────────

def _exc(name, msg=""):
    """Build an exception class with the given __name__ (mimics requests'
    ReadTimeout / ConnectionError / HTTPError without importing requests)."""
    cls = type(name, (Exception,), {})
    return cls(msg)


def test_cm3_classifies_408_and_transport_as_response_unknown():
    from hermes_trader.client.exchange import _is_response_unknown_error
    assert _is_response_unknown_error(_exc("HTTPError", "408 Client Error: Request Timeout"))
    assert _is_response_unknown_error(_exc("HTTPError", "502 Bad Gateway"))
    assert _is_response_unknown_error(_exc("HTTPError", "503 Service Unavailable"))
    assert _is_response_unknown_error(_exc("HTTPError", "504 Gateway Timeout"))
    assert _is_response_unknown_error(_exc("ReadTimeout", "HTTPSConnectionPool: Read timed out"))
    assert _is_response_unknown_error(_exc("ConnectTimeout", "connect timed out"))
    assert _is_response_unknown_error(_exc("ConnectionError", "connection reset"))
    assert _is_response_unknown_error(_exc("SSLError", "UNEXPECTED_EOF_WHILE_READING"))
    assert _is_response_unknown_error(_exc("SSLEOFError", "EOF occurred"))
    assert _is_response_unknown_error(_exc("ProxyError", "proxy tunnel failed"))
    assert _is_response_unknown_error(_exc("ChunkedEncodingError", "incomplete chunk"))


def test_cm3_definite_rejections_are_not_response_unknown():
    from hermes_trader.client.exchange import _is_response_unknown_error
    # 4xx (other than 408) means HL definitively rejected — no order exists.
    assert not _is_response_unknown_error(_exc("HTTPError", "400 Client Error: Bad Request"))
    assert not _is_response_unknown_error(_exc("HTTPError", "401 Client Error: Unauthorized"))
    assert not _is_response_unknown_error(_exc("HTTPError", "403 Client Error: Forbidden"))
    # Unrelated exceptions are not ambiguous transport states.
    assert not _is_response_unknown_error(_exc("ValueError", "invalid price"))
    assert not _is_response_unknown_error(_exc("KeyError", "oid"))
    assert not _is_response_unknown_error(RuntimeError("slippage 5% > cap 1%"))


# ── C-M3: place_hl_order tags the envelope ────────────────────────────────

def test_cm3_place_order_tags_transport_failure(monkeypatch):
    """A ReadTimeout raised by the SDK exchange.order call must surface as
    error_code='response_unknown' (not a plain failure)."""
    from hermes_trader.client import exchange
    monkeypatch.setenv("HYPERLIQUID_PRIVATE_KEY", "0xabc")
    monkeypatch.setattr(exchange, "PRIVATE_KEY_HEX", "0xabc")
    monkeypatch.setattr(exchange, "get_coin_index", lambda coin: (0, 3, 2))
    monkeypatch.setattr(exchange, "_ioc_cross_price", lambda coin, is_buy, mid: mid)
    monkeypatch.setattr(exchange, "_round_price_for_hl", lambda px, sd, **kw: f"{px:.2f}")
    monkeypatch.setattr(exchange, "_min_order_size", lambda mid, sd: 0.001)

    class _FakeExchange:
        def order(self, *a, **k):
            raise _exc("ReadTimeout", "read timed out after submit")

    monkeypatch.setattr(exchange, "_make_exchange", lambda: _FakeExchange())
    r = exchange.place_hl_order(True, 1.0, 100.0, "BTC")
    assert r["ok"] is False
    assert r.get("error_code") == "response_unknown"


def test_cm3_place_order_untagged_for_definite_rejection(monkeypatch):
    """A plain envelope rejection (no exception / non-transport) must NOT carry
    error_code — the executor treats untagged failures as 'no order exists'."""
    from hermes_trader.client import exchange
    monkeypatch.setenv("HYPERLIQUID_PRIVATE_KEY", "0xabc")
    monkeypatch.setattr(exchange, "PRIVATE_KEY_HEX", "0xabc")
    monkeypatch.setattr(exchange, "get_coin_index", lambda coin: (0, 3, 2))
    monkeypatch.setattr(exchange, "_ioc_cross_price", lambda coin, is_buy, mid: mid)
    monkeypatch.setattr(exchange, "_round_price_for_hl", lambda px, sd, **kw: f"{px:.2f}")
    monkeypatch.setattr(exchange, "_min_order_size", lambda mid, sd: 0.001)

    class _FakeExchange:
        def order(self, *a, **k):
            raise _exc("HTTPError", "400 Client Error: Bad Request")

    monkeypatch.setattr(exchange, "_make_exchange", lambda: _FakeExchange())
    r = exchange.place_hl_order(True, 1.0, 100.0, "BTC")
    assert r["ok"] is False
    assert "error_code" not in r


# ── H6: reconcile_order_fill (userFills polling + aggregation) ────────────

def _fill(coin, side, sz, px, tms, oid, cloid=None):
    f = {"coin": coin, "side": side, "sz": str(sz), "px": str(px),
         "time": tms, "oid": oid, "tid": oid * 10, "closedPnl": "0"}
    if cloid is not None:
        f["cloid"] = cloid
    return f


def test_h6_reconcile_filled_aggregates_weighted_avg(monkeypatch):
    """Two fills for the same Cloid → weighted avg px, total sz, latest time."""
    from hermes_trader.client import exchange
    monkeypatch.setattr(exchange, "resolve_user_address", lambda: "0xUSER")
    clo = "0x" + "ab" * 16
    # newest-first: 3 @ 101, then 7 @ 100 → avg = (3*101 + 7*100)/10 = 100.3
    fills = [
        _fill("BTC", "B", 3, 101.0, 2000, 555, cloid=clo),
        _fill("BTC", "B", 7, 100.0, 1000, 444, cloid=clo),
    ]
    monkeypatch.setattr(exchange, "_http_post",
                        lambda *a, **k: fills)
    r = exchange.reconcile_order_fill("BTC", cloid=clo, is_buy=True,
                                      expect_size=10.0, retries=1)
    assert r["status"] == "filled"
    assert abs(r["total_sz"] - 10.0) < 1e-9
    assert abs(r["avg_px"] - 100.3) < 1e-9
    assert r["filled_at_ms"] == 2000
    assert r["oid"] == "555"
    assert r["n_fills"] == 2


def test_h6_reconcile_matches_by_oid(monkeypatch):
    from hermes_trader.client import exchange
    monkeypatch.setattr(exchange, "resolve_user_address", lambda: "0xUSER")
    fills = [_fill("ETH", "A", 2, 50.0, 1000, 999)]  # no cloid on the fill
    monkeypatch.setattr(exchange, "_http_post", lambda *a, **k: fills)
    r = exchange.reconcile_order_fill("ETH", oid="999", is_buy=False, retries=1)
    assert r["status"] == "filled"
    assert abs(r["total_sz"] - 2.0) < 1e-9
    assert r["oid"] == "999"


def test_h6_reconcile_not_filled_after_retries(monkeypatch):
    """Clean answers with no matching fill → the IOC truly did not execute."""
    from hermes_trader.client import exchange
    monkeypatch.setattr(exchange, "resolve_user_address", lambda: "0xUSER")
    sleeps = []
    monkeypatch.setattr(exchange, "_time", type("T", (), {"sleep": staticmethod(lambda s: sleeps.append(s))}))
    monkeypatch.setattr(exchange, "_http_post", lambda *a, **k: [])
    r = exchange.reconcile_order_fill("BTC", cloid="0x" + "cd" * 16,
                                      is_buy=True, retries=3, retry_delay_s=0.01)
    assert r["status"] == "not_filled"
    assert len(sleeps) == 2  # sleeps between the 3 polls, not after the last


def test_h6_reconcile_unknown_when_lookup_fails(monkeypatch):
    """userFills itself raising → status unknown (caller must rehydrate/halt),
    NOT a false 'not_filled'."""
    from hermes_trader.client import exchange
    monkeypatch.setattr(exchange, "resolve_user_address", lambda: "0xUSER")
    monkeypatch.setattr(exchange._time, "sleep", lambda s: None, raising=False)

    def _boom(*a, **k):
        raise ConnectionError("userFills endpoint unreachable")

    monkeypatch.setattr(exchange, "_http_post", _boom)
    r = exchange.reconcile_order_fill("BTC", cloid="0x" + "ef" * 16,
                                      retries=2, retry_delay_s=0.0)
    assert r["status"] == "unknown"
    assert "userFills_fetch_exception" in r["reason"]


def test_h6_reconcile_oversized_fill_is_unknown(monkeypatch):
    """A fill wildly larger than requested (>0.5%) signals an id collision —
    stay ambiguous rather than register a phantom size."""
    from hermes_trader.client import exchange
    monkeypatch.setattr(exchange, "resolve_user_address", lambda: "0xUSER")
    clo = "0x" + "12" * 16
    fills = [_fill("BTC", "B", 20.0, 100.0, 1000, 777, cloid=clo)]  # asked for 10
    monkeypatch.setattr(exchange, "_http_post", lambda *a, **k: fills)
    r = exchange.reconcile_order_fill("BTC", cloid=clo, is_buy=True,
                                      expect_size=10.0, retries=1)
    assert r["status"] == "unknown"
    assert "id mismatch" in r["reason"]


def test_h6_reconcile_wrong_side_fill_ignored(monkeypatch):
    """Same Cloid but opposite side → not our fill → not_filled (safer than
    adopting the wrong economics)."""
    from hermes_trader.client import exchange
    monkeypatch.setattr(exchange, "resolve_user_address", lambda: "0xUSER")
    clo = "0x" + "34" * 16
    fills = [_fill("BTC", "A", 10.0, 100.0, 1000, 888, cloid=clo)]  # asked buy, got sell
    monkeypatch.setattr(exchange._time, "sleep", lambda s: None)
    monkeypatch.setattr(exchange, "_http_post", lambda *a, **k: fills)
    r = exchange.reconcile_order_fill("BTC", cloid=clo, is_buy=True,
                                      retries=1, retry_delay_s=0.0)
    assert r["status"] == "not_filled"


def test_h6_reconcile_ignores_other_coins(monkeypatch):
    from hermes_trader.client import exchange
    monkeypatch.setattr(exchange, "resolve_user_address", lambda: "0xUSER")
    clo = "0x" + "56" * 16
    fills = [_fill("ETH", "B", 10.0, 100.0, 1000, 333, cloid=clo)]  # ETH fill, BTC ask
    monkeypatch.setattr(exchange._time, "sleep", lambda s: None)
    monkeypatch.setattr(exchange, "_http_post", lambda *a, **k: fills)
    r = exchange.reconcile_order_fill("BTC", cloid=clo, is_buy=True,
                                      retries=1, retry_delay_s=0.0)
    assert r["status"] == "not_filled"


def test_h6_reconcile_no_identifier_is_unknown():
    from hermes_trader.client.exchange import reconcile_order_fill
    r = reconcile_order_fill("BTC", retries=1)
    assert r["status"] == "unknown"
    assert "no_cloid_or_oid" in r["reason"]


# ── executor fixture (mirrors tests/test_cleanup.py::_exec_baseline) ──────

def _h6_baseline(monkeypatch, cfg_overrides=None):
    """Patch executor's I/O surface for the H6 paths; returns (executor, caps)."""
    from hermes_trader.agents import executor
    cfg = {
        "mode": "LIVE", "enable_crypto": True, "enable_hip3": True,
        "equity_fraction_per_trade": 0.10, "leverage": 10,
        "max_trade_notional_usd": 100000, "max_concurrent": 18,
        "max_total_notional_pct": 40.0, "max_daily_loss_usd": -1000,
        "min_available_margin_pct": 0.10, "cooldown_min": 60,
        "min_ai_confidence": 0.30, "counter_regime_min_conf": 0.65,
        "max_crypto_long_correlated": 5, "min_market_volume_usd": 5_000_000,
        "min_hip3_volume_usd": 500_000, "conviction_sizing": True,
        "dsl_exit": {"max_loss_pct": 2.0, "max_loss_roe_pct": 30.0,
                     "protect_pct": 0.5, "retrace_threshold": 0.3,
                     "hard_timeout_minutes": 180.0},
        "debate_gate": {"enabled": False},
        "circuit_breaker": {"resp_unknown_halt_n": 3,
                            "resp_unknown_halt_min": 60.0},
    }
    cfg.update(cfg_overrides or {})
    state = {"equity": 1000.0, "available": 500.0, "total_ntl": 0.0,
             "asset_positions": [], "dex_equity": {"": 1000.0},
             "dex_available": {"": 500.0}}
    caps = {"registered": [], "trades": [], "halts": [], "rehydrates": 0,
            "reconcile_calls": 0}

    monkeypatch.setattr(executor, "read_agent_config", lambda: cfg)
    monkeypatch.setattr(executor, "resolve_user_address", lambda: "0xMASTER")
    monkeypatch.setattr(executor, "fetch_account_state", lambda u, **kw: state)
    monkeypatch.setattr(executor, "get_hl_price", lambda c: 100.0)
    monkeypatch.setattr(executor, "get_hl_atr", lambda *a, **k: 2.0)
    # H-6 (supplemental audit 2026-08-30): this fixture's world prices every
    # coin at a synthetic 100.0; the live Binance cross-check would (correctly)
    # veto that divergence before place_hl_order is ever reached, making the
    # reconcile paths below unreachable. Stub the safety net fail-open, matching
    # every other network surface isolated in this fixture.
    monkeypatch.setattr(
        "hermes_trader.client.price_crosscheck.crosscheck_price",
        lambda coin, px: {"ok": True, "checked": False, "reason": "test_stub"})
    monkeypatch.setattr(executor, "get_max_leverage", lambda c: 40)
    monkeypatch.setattr(executor, "get_orderbook_spread",
                        lambda c: {"ok": True, "spread_pct": 0.01,
                                   "best_bid": 99.9, "best_ask": 100.1,
                                   "bid_depth_1pct_usd": 1e9,
                                   "ask_depth_1pct_usd": 1e9})
    monkeypatch.setattr(executor, "min_entry_notional_usd", lambda c, mid: 10.5)
    monkeypatch.setattr(executor, "entry_size_for_notional", lambda c, n, mid: n / mid)
    monkeypatch.setattr(executor, "set_leverage", lambda c, l: {"ok": True})
    monkeypatch.setattr(executor, "place_hl_trigger_order", lambda *a, **k: {"ok": True})
    monkeypatch.setattr("hermes_trader.client.hl_client._http_post",
                        lambda *a, **k: {"marginSummary": {"accountValue": "500"}})
    monkeypatch.setattr("hermes_trader.agents.market_regime.detect_regime_with_score",
                        lambda c, force=False: ("neutral", 0.0))
    monkeypatch.setattr("hermes_trader.agents.hyperfeed.market_get_funding_regime",
                        lambda: {"regime": "NEUTRAL", "regimes_by_class": {}})

    def _place(is_buy, size, mid, coin, **kw):
        return {"ok": True, "order_id": "OID1", "avg_px": mid}

    monkeypatch.setattr(executor, "place_hl_order", _place)

    def _register(coin, side, px, **kw):
        caps["registered"].append({"coin": coin, "side": side, "px": px, **kw})

    monkeypatch.setattr(executor, "register_position", _register)
    monkeypatch.setattr(executor.memory, "track_daily_pnl", lambda *a, **k: None)
    monkeypatch.setattr(executor.memory, "get_daily_pnl", lambda: 0.0)
    monkeypatch.setattr(executor.memory, "peak_equity", lambda: 0.0)
    monkeypatch.setattr(executor.memory, "consecutive_losses", lambda coin: 0)
    monkeypatch.setattr(executor.memory, "coin_daily_realized_pnl_pct", lambda coin, sod: 0.0)
    monkeypatch.setattr(executor.memory, "get_recent_trades", lambda n=10: [])
    monkeypatch.setattr(executor.memory, "record_trade",
                        lambda t: caps["trades"].append(t))
    monkeypatch.setattr(executor.memory, "set_global_halt",
                        lambda until: caps["halts"].append(until))
    monkeypatch.setattr(executor.memory, "global_halt_remaining_min", lambda: 0.0)

    # H6-specific surfaces:
    def _rehydrate(*a, **k):
        caps["rehydrates"] += 1

    monkeypatch.setattr("hermes_trader.agents.dsl_exit.rehydrate_from_exchange", _rehydrate)
    monkeypatch.setattr("hermes_trader.client.exchange.verify_order_exists",
                        lambda **k: {"verified": True})
    monkeypatch.setattr("hermes_trader.notify.send_text", lambda *a, **k: None)

    executor._reset_resp_unknown_streak()
    monkeypatch.setenv("HYPERLIQUID_PRIVATE_KEY", "0xabc")
    return executor, caps, cfg


def _h6_analysis(**kw):
    base = {"id": str(uuid.uuid4()), "coin": "BTC", "verdict": "LONG",
            "side": "long", "confidence": 0.70, "composite_score": 30,
            "entry_px": 100, "stop_px": 95, "tp_px": 110,
            "news_context": "no news"}
    base.update(kw)
    return base


def _set_reconcile(monkeypatch, result):
    """Patch the lazily-imported reconcile_order_fill to return `result`."""
    from hermes_trader.client import exchange
    monkeypatch.setattr(exchange, "reconcile_order_fill",
                        lambda **k: result)


# ── H6: executor failure-branch three-state handling ──────────────────────

def test_h6_filled_after_response_loss_is_registered(monkeypatch):
    """response_unknown + userFills confirms a fill → executed True, position
    registered at the REAL fill px/sz (fall-through), trade recorded."""
    ex, caps, _ = _h6_baseline(monkeypatch)
    monkeypatch.setattr(ex, "place_hl_order",
                        lambda b, s, m, c, **kw:
                        {"ok": False, "error": "408 timeout",
                         "error_code": "response_unknown"})
    _set_reconcile(monkeypatch, {
        "status": "filled", "avg_px": 99.5, "total_sz": 12.0,
        "filled_at_ms": int(time.time() * 1000), "oid": "OID777",
        "cloid": "0xx", "n_fills": 1,
    })
    r = ex.maybe_execute(_h6_analysis())
    assert r["executed"] is True, r
    assert r["order_id"] == "OID777"
    # registered at the reconciled economics, not the requested mid:
    assert caps["registered"] and caps["registered"][0]["px"] == 99.5
    assert caps["trades"] and caps["trades"][0]["order_id"] == "OID777"
    # notional recorded from the true size 12 × 99.5
    assert abs(caps["trades"][0]["size_usd"] - 12.0 * 99.5) < 1.0


def test_h6_not_filled_after_response_loss_returns_retryable_failure(monkeypatch):
    """response_unknown + exchange cleanly answers 'no fill' → executed False
    with the explicit not_filled reason; nothing registered."""
    ex, caps, _ = _h6_baseline(monkeypatch)
    monkeypatch.setattr(ex, "place_hl_order",
                        lambda b, s, m, c, **kw:
                        {"ok": False, "error": "ReadTimeout",
                         "error_code": "response_unknown"})
    _set_reconcile(monkeypatch, {"status": "not_filled", "reason": "no_matching_fill"})
    r = ex.maybe_execute(_h6_analysis())
    assert r["executed"] is False
    assert "order_failed_response_unknown_not_filled" in r["reason"]
    assert caps["registered"] == []
    assert caps["trades"] == []
    assert caps["halts"] == []


def test_h6_unresolved_triggers_rehydrate_but_no_halt_below_threshold(monkeypatch):
    """response_unknown + the LOOKUP itself failed → rehydrate runs, streak
    bumps, but a single occurrence (threshold 3) must NOT halt."""
    ex, caps, _ = _h6_baseline(monkeypatch)
    monkeypatch.setattr(ex, "place_hl_order",
                        lambda b, s, m, c, **kw:
                        {"ok": False, "error": "conn reset",
                         "error_code": "response_unknown"})
    _set_reconcile(monkeypatch, {"status": "unknown",
                                 "reason": "userFills_fetch_exception: boom"})
    r = ex.maybe_execute(_h6_analysis())
    assert r["executed"] is False
    assert "order_response_unknown_unresolved" in r["reason"]
    assert caps["rehydrates"] == 1
    assert caps["halts"] == []  # streak 1 < 3


def test_h6_three_consecutive_unresolved_arms_global_halt(monkeypatch):
    """Three deaf-exchange outcomes in a row → set_global_halt armed."""
    ex, caps, _ = _h6_baseline(monkeypatch)
    monkeypatch.setattr(ex, "place_hl_order",
                        lambda b, s, m, c, **kw:
                        {"ok": False, "error": "conn reset",
                         "error_code": "response_unknown"})
    _set_reconcile(monkeypatch, {"status": "unknown", "reason": "fetch_exception"})
    for _ in range(3):
        r = ex.maybe_execute(_h6_analysis())  # fresh UUID each time → new in-flight
        assert r["executed"] is False
    assert caps["rehydrates"] == 3
    assert len(caps["halts"]) == 1  # halted once, on the 3rd consecutive


def test_h6_resolved_outcome_resets_streak(monkeypatch):
    """Two unresolved, then a confirmed fill → streak resets; two more
    unresolved do NOT halt (streak never reaches 3)."""
    ex, caps, _ = _h6_baseline(monkeypatch)
    unknown = {"status": "unknown", "reason": "fetch_exception"}
    filled = {"status": "filled", "avg_px": 100.0, "total_sz": 10.0,
              "filled_at_ms": int(time.time() * 1000), "oid": "OID9",
              "cloid": "0xx", "n_fills": 1}

    def _place(b, s, m, c, **kw):
        return {"ok": False, "error": "x", "error_code": "response_unknown"}

    monkeypatch.setattr(ex, "place_hl_order", _place)

    _set_reconcile(monkeypatch, unknown)
    ex.maybe_execute(_h6_analysis())
    ex.maybe_execute(_h6_analysis())  # streak = 2
    assert caps["halts"] == []
    _set_reconcile(monkeypatch, filled)  # resolved → reset
    assert ex.maybe_execute(_h6_analysis())["executed"] is True
    _set_reconcile(monkeypatch, unknown)
    ex.maybe_execute(_h6_analysis())
    ex.maybe_execute(_h6_analysis())  # streak back to only 2
    assert caps["halts"] == []


def test_h6_definite_failure_skips_reconcile(monkeypatch):
    """An untagged failure (definite rejection) must NOT call reconcile and
    keeps the legacy order_failed reason."""
    ex, caps, _ = _h6_baseline(monkeypatch)
    monkeypatch.setattr(ex, "place_hl_order",
                        lambda b, s, m, c, **kw:
                        {"ok": False, "error": "no match"})
    called = {"n": 0}

    def _recon(**k):
        called["n"] += 1
        return {"status": "not_filled", "reason": "noop"}

    from hermes_trader.client import exchange
    monkeypatch.setattr(exchange, "reconcile_order_fill", _recon)
    r = ex.maybe_execute(_h6_analysis())
    assert r["executed"] is False
    assert "order_failed:" in r["reason"]
    assert called["n"] == 0


def test_h6_response_unknown_without_cloid_takes_definite_path(monkeypatch):
    """response_unknown but the analysis id is not a valid UUID → no Cloid to
    reconcile by → the executor cannot poll and must treat it as a definite
    failure (never silently register a phantom position)."""
    ex, caps, _ = _h6_baseline(monkeypatch)
    monkeypatch.setattr(ex, "place_hl_order",
                        lambda b, s, m, c, **kw:
                        {"ok": False, "error": "408",
                         "error_code": "response_unknown"})
    called = {"n": 0}
    from hermes_trader.client import exchange
    monkeypatch.setattr(exchange, "reconcile_order_fill",
                        lambda **k: called.__setitem__("n", called["n"] + 1))
    r = ex.maybe_execute(_h6_analysis(id="a1"))  # not a UUID → _cloid is None
    assert r["executed"] is False
    assert "order_failed:" in r["reason"]
    assert called["n"] == 0


# ── C-M3: canonical config defaults ───────────────────────────────────────

def test_cm3_canonical_defaults_present():
    """The streak-halt thresholds must ship enabled with conservative defaults
    and be visible to config audit / live cfg_get."""
    from hermes_trader.agents.config_store import cfg_get
    assert int(cfg_get("circuit_breaker.resp_unknown_halt_n",
                       config={"circuit_breaker": {}})) == 3
    assert float(cfg_get("circuit_breaker.resp_unknown_halt_min",
                         config={"circuit_breaker": {}})) == 60.0
