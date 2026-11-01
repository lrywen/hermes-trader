"""P4 工程解耦：订单生命周期簇（从 agents.executor 逐字搬迁）。

融合文档 P4：只拆订单生命周期簇（_place_backup_sl / _register_filled_position /
_reconcile_unknown_order_result），不碰 dsl_exit/dashboard/memory。

搬迁方式为"模块依赖注入"：三函数体逐字保留，executor 的模块级私有名以
``ex.`` 前缀引用（ex 即 agents.executor 模块本身），导入级符号在本模块顶部
import。executor 侧保留同名 wrapper 转调，调用方零改动；行为由现有
executor 回归测试 + DSL parity 测试证明等价。
"""
from __future__ import annotations

import logging
import math
import time
import uuid
from typing import Any

from hyperliquid.utils.types import Cloid

from hermes_trader.agents.memory import memory

logger = logging.getLogger("execution.order_lifecycle")


def _place_backup_sl(ex, *,
    atr: float, entry_px: float, sl_atr_mult: float,
    sl_floor_pct: float, sl_ceiling_pct: float, size_in_coin: float,
    is_buy: bool, coin: str, trade_side: str, memory: Any,
    sl_limit_band_pct: float = 0.0,
) -> bool:
    """Place the server-side backup stop for a freshly filled position.

    Mirrors the DSL atr_stop clamp (floor ≤ width ≤ ceiling), widens for the
    coin's recent adverse exit slip, and retries once on transient failure. On
    permanent failure records the coin in `ex._pending_sl_retries` and fires a
    risk alert; returns True when the stop is missing so the caller can flag
    `sl_missing` in its result.
    """
    sl_missing = False
    if atr > 0 and size_in_coin > 0:
        # Dynamic slippage compensation (PURR #6 root cause): widen the backup
        # stop by the coin's recent mean adverse exit slip so a gap-through at
        # trigger time still lands within the intended cap. Capped so a noisy
        # sample can't recreate an unbounded stop. Only widens (adverse side).
        _slip_widen_pct = 0.0
        try:
            _slip_widen_pct = max(0.0, float(memory.avg_exit_slip_bps(coin, days=30.0)) / 100.0)
            _slip_widen_pct = min(_slip_widen_pct, sl_ceiling_pct * 0.5)
        except Exception:
            _slip_widen_pct = 0.0
        # NEW-01：宽度统一走备份 SL SSOT（不再本地手写三层 clamp）。
        sl_width_pct = ex.compute_backup_sl_width_pct(
            atr=atr, entry_px=entry_px, sl_atr_mult=sl_atr_mult,
            sl_floor_pct=sl_floor_pct, sl_ceiling_pct=sl_ceiling_pct,
            slip_widen_pct=_slip_widen_pct)
        sl_px = ex._signed_price(entry_px, -entry_px * sl_width_pct / 100, is_buy)
        # C4-3: sl_limit_band_pct > 0 arms a trigger LIMIT (worst-case fill
        # capped `band` past the trigger); 0 keeps the market-on-trigger default.
        # Audit 2026-10-15 P1: the one-cloid / claim-before-retry / one-retry
        # state machine is shared with the manual web path via bracket_common
        # (E-2/P2-2). The deferred queue, metrics and tracker stay here.
        from hermes_trader.agents.bracket_common import place_backup_stop_once

        def _bracket_log(level: str, msg: str) -> None:
            getattr(logger, level if level in ("info", "warning", "error") else "info")(
                f"[executor] {msg}")

        _placed = place_backup_stop_once(
            is_buy=is_buy, size_in_coin=size_in_coin, sl_px=sl_px, coin=coin,
            limit_band_pct=sl_limit_band_pct,
            place_order=ex.place_hl_trigger_order,
            find_by_cloid=ex.find_open_order_by_cloid,
            sleep=time.sleep, log=_bracket_log, log_prefix="[executor] Backup SL",
        )
        sl_px = _placed["sl_px"]
        _sl_cloid = _placed["cloid"]
        sl_res = {"ok": _placed["ok"], "order_id": _placed["order_id"],
                  "error": _placed.get("error")}
        if sl_res.get("ok"):
            logger.info(f"[executor] Placed backup SL at {sl_px:.6g} "
                        f"({sl_width_pct:.2f}% from entry; atr_mult={sl_atr_mult}, "
                        f"floor={sl_floor_pct}%, ceiling={sl_ceiling_pct}%, "
                        f"slip+={_slip_widen_pct:.3f}%, "
                        f"limit_band={sl_limit_band_pct if sl_limit_band_pct > 0 else 'market'}%)")
            # Persist the new SL oid/px/size on the tracker so the dynamic mover
            # (batchModify) can target it and a restart reconciles correctly.
            ex.set_bracket(coin, trade_side,
                        sl_oid=sl_res.get("order_id"),
                        sl_px=sl_px, sl_size=size_in_coin)
        else:
            sl_missing = True
            logger.error(f"[executor] Backup SL FAILED twice for {coin} — POSITION HAS "
                         f"NO SERVER-SIDE STOP (DSL loop is sole protection): "
                         f"target_px={sl_px}, is_buy={is_buy}, size={size_in_coin}")
            ex._pending_sl_retries[coin] = {
                "is_buy": is_buy,
                "size": size_in_coin,
                "sl_px": sl_px,
                "coin": coin,
                "side": trade_side,
                # C4-3: carry the band so the retry queue re-arms the SAME
                # trigger-limit variant instead of silently falling back to
                # market after a transient placement failure.
                "limit_band_pct": sl_limit_band_pct if sl_limit_band_pct > 0 else 0.0,
                # E-2: reuse the SAME cloid on the deferred re-arm so a retry
                # cannot duplicate an SL that an earlier lost-response attempt
                # may have actually armed.
                "cloid": _sl_cloid,
                "retry_count": 0,
                "last_attempt": time.time(),
            }
            # Q6/Q8: persist the newly-naked position so a restart re-arms it.
            try:
                from hermes_trader import metrics as _m
                _m.PENDING_SL_MISSING_TOTAL.inc()
            except Exception:
                pass
            ex._persist_pending_sl()
            ex._set_pending_sl_gauge()
    # SL aliveness probe: if the bracket claims a resting SL oid but placement
    # just reported it missing, scream loudly so an operator can reconcile —
    # a "present but dead" stop is the worst failure mode (looks protected in
    # our state while the exchange has nothing).
    if sl_missing:
        try:
            from hermes_trader import notify
            notify.send_text(
                f"🚨 {coin} 备份止损下单失败\n"
                f"交易所端无止损单，DSL 软止损为唯一保护\n"
                f"请立即手动确认持仓并补单",
                category="risk")
        except Exception as _alert_e:
            logger.error("[executor] fund-safety risk alert failed: %r", _alert_e)
    return sl_missing



def _reconcile_unknown_order_result(order_res: dict[str, Any], *, coin: str,
                                    is_buy: bool, size_in_coin: float,
                                    mid_price: float, cloid: Any,
                                    config: dict[str, Any], user: str,
                                    mode: str, aid: str,
                                    gate_results: Any,
                                    intent: Any = None, ex=None) -> dict[str, Any] | None:
    """Handle a failed order placement. A DEFINITE failure (HL envelope
    rejection, slippage cap, local validation) clears the in-flight marker and
    returns an order_failed result. error_code="response_unknown" (408 / read
    timeout / SSL drop after submit) is ambiguous: reconcile against userFills
    by Cloid — a confirmed fill backfills order_res in-place (returns None so
    the caller falls through to the normal success path); a confirmed
    not-fill or an unresolvable lookup clears the marker (rehydrate + circuit
    breaker on the latter) and returns a rejection result dict."""
    if order_res.get("ok"):
        return None
    from hermes_trader.execution import lifecycle as _lc
    if order_res.get("error_code") != "response_unknown" or cloid is None:
        # Definite rejection / envelope error — no order on the exchange.
        _lc.mark_failed(intent, reason=f"order_failed: {order_res.get('error', 'unknown')}")
        ex._reset_resp_unknown_streak()
        with ex._EXEC_LOCK:
            ex._IN_FLIGHT_ANALYSES.discard(aid)
            ex._IN_FLIGHT_COINS.discard(coin)
        return {
            "executed": False, "mode": mode, "analysis_id": aid,
            "reason": f"order_failed: {order_res.get('error', 'unknown')}",
            "gate_results": gate_results,
        }
    # response_unknown：回执丢失，进入待对账状态。
    _lc.mark_unknown(intent, reason="response_unknown")
    try:
        from hermes_trader.client.exchange import reconcile_order_fill
        _rc = reconcile_order_fill(
            coin=coin, cloid=cloid,
            is_buy=is_buy, expect_size=float(size_in_coin or 0.0) or None,
        )
    except Exception as _rc_e:
        _rc = {"status": "unknown", "reason": f"reconcile_exception: {_rc_e!r}"}
        logger.warning(f"[executor] H6 reconcile raised for {coin}: {_rc_e!r}")

    if _rc.get("status") == "filled":
        # The order DID fill despite the lost response. Backfill the result
        # with the REAL fill economics so the caller falls through into the
        # normal success path (ex.register_position / record_trade / backup-SL
        # run exactly once with true avgPx/total_sz — no duplicated logic).
        ex._reset_resp_unknown_streak()
        logger.warning(
            f"[executor] H6 RECONCILED {coin} fill after response loss: "
            f"avg_px={_rc.get('avg_px')} total_sz={_rc.get('total_sz')} "
            f"n={_rc.get('n_fills')} oid={_rc.get('oid')} — registering "
            f"from userFills (no orphan)."
        )
        try:
            from hermes_trader import notify
            notify.send_text(
                f"⚠️ H6 下单响应丢失但已成交，已补登: {coin} "
                f"px={_rc.get('avg_px')} sz={_rc.get('total_sz')} "
                f"oid={_rc.get('oid')}", category="risk")
        except Exception as _alert_e:
            logger.error("[executor] fund-safety risk alert failed: %r", _alert_e)
        order_res.clear()
        order_res.update({
            "ok": True,
            "order_id": _rc.get("oid"),
            "cloid": str(cloid),
            "avg_px": float(_rc.get("avg_px") or 0.0),
            "total_sz": float(_rc.get("total_sz") or 0.0),
            "filled_at_ms": _rc.get("filled_at_ms"),
            "reconciled_after_response_unknown": True,
        })
        # FILLED 统一在 _register_filled_position 之后标记（正常成功路径），
        # 此处不重复标，避免终态二次迁移。
        return None
    if _rc.get("status") == "not_filled":
        # Exchange answered cleanly and has no fill → the order truly did not
        # execute. Safe to clear + retry later.
        _lc.mark_failed(intent, reason="response_unknown_not_filled")
        ex._reset_resp_unknown_streak()
        logger.info(f"[executor] H6 reconcile {coin}: confirmed NOT filled "
                    f"after response loss ({_rc.get('reason')}); safe to retry.")
        with ex._EXEC_LOCK:
            ex._IN_FLIGHT_ANALYSES.discard(aid)
            ex._IN_FLIGHT_COINS.discard(coin)
        return {
            "executed": False, "mode": mode, "analysis_id": aid,
            "reason": f"order_failed_response_unknown_not_filled: {order_res.get('error', 'unknown')}",
            "gate_results": gate_results,
        }
    # The LOOKUP itself failed — we cannot tell filled from unfilled. Shrink
    # the orphan window immediately via rehydrate (a real fill gets a synthetic
    # tracker on this same tick), count the deaf-exchange streak, and halt auto
    # entries at the configured threshold so we don't keep spraying orders.
    _streak = ex._bump_resp_unknown_streak()
    logger.error(
        f"[executor] H6 reconcile UNRESOLVED for {coin} "
        f"(streak={_streak}): {_rc.get('reason')} — fill state "
        f"unknown; running immediate rehydrate."
    )
    try:
        from hermes_trader.agents.dsl_exit import rehydrate_from_exchange
        _rstate = ex.fetch_account_state(user)
        _rpositions = _rstate.get("asset_positions", []) or []
        rehydrate_from_exchange(
            _rpositions,
            default_leverage=int(config.get("leverage", 1) or 1),
            queried_dexes={""},
            user=user,
        )
    except Exception as _rh_e:
        logger.error(f"[executor] H6 immediate rehydrate failed: {_rh_e}")
        try:
            from hermes_trader import event_log
            event_log.append("error", payload={
                "scope": "h6_rehydrate", "coin": coin,
                "error": str(_rh_e)})
        except Exception as _ev_e:
            logger.error("[executor] h6_rehydrate event log failed: %r", _ev_e)
    try:
        _halt_n = int(ex.cfg_get(
            "circuit_breaker.resp_unknown_halt_n",
            config=config, default=3) or 0)
    except Exception:
        _halt_n = 3
    if _halt_n > 0 and _streak >= _halt_n:
        try:
            _halt_min = float(ex.cfg_get(
                "circuit_breaker.resp_unknown_halt_min",
                config=config, default=60.0) or 60.0)
        except Exception:
            _halt_min = 60.0
        try:
            _until = int(time.time() * 1000 + _halt_min * 60_000)
            memory.set_global_halt(_until)
            logger.critical(
                f"[executor] C-M3: {_streak} consecutive response-unknown "
                f"order outcomes → GLOBAL HALT {_halt_min:.0f}min "
                f"(exchange unreachable / not answering).")
            try:
                from hermes_trader import notify
                notify.send_text(
                    f"🛑 C-M3 连续 {_streak} 次下单响应未知且无法核对，"
                    f"暂停自动开仓 {_halt_min:.0f} 分钟（交易所连接异常）",
                    category="risk")
            except Exception as _alert_e:
                logger.error("[executor] fund-safety risk alert failed: %r", _alert_e)
        except Exception as _h_e:
            logger.error(f"[executor] C-M3 halt arm failed: {_h_e}")
    with ex._EXEC_LOCK:
        ex._IN_FLIGHT_ANALYSES.discard(aid)
        ex._IN_FLIGHT_COINS.discard(coin)
    return {
        "executed": False, "mode": mode, "analysis_id": aid,
        "reason": f"order_response_unknown_unresolved: {order_res.get('error', 'unknown')}",
        "reconcile": _rc,
        "gate_results": gate_results,
    }


def _register_filled_position(ex, *, analysis: dict[str, Any], config: dict[str, Any],
                              order_res: dict[str, Any], coin: str,
                              trade_side: str, mid_price: float,
                              size_in_coin: float, atr: float, leverage: int,
                              user: str, override_composite: float,
                              enf: Any, aid: str) -> dict[str, float]:
    """Post-fill LOCAL STATE persistence (Phase-1): settle fill economics,
    register the DSL exit tracker + sizing-v2 drift assertion, record the
    trade and the entry-context snapshot. On failure the exchange fill is
    ORPHANED — log critical and attempt an immediate rehydrate to shrink the
    orphan window. The in-flight marker is cleared in `finally` on every
    path. Returns {entry_px, position_notional, size_in_coin, filled_px,
    arrival_mid, regime}."""
    try:
        arrival_mid = float(mid_price or 0)
        try:
            filled_px = float(order_res.get("avg_px") or 0)
        except (TypeError, ValueError):
            filled_px = 0.0
        try:
            filled_size = float(order_res.get("total_sz") or 0)
        except (TypeError, ValueError):
            filled_size = 0.0
        entry_px = filled_px if filled_px > 0 else mid_price
        if not (filled_px > 0):
            # c9: avgPx missing in the fill response — entry/tracker/SL
            # anchoring falls back to the pre-trade mid instead of the actual
            # fill. Mirrors the close-side three-tier avgPx fallback warning
            # (executor close path) so a quote-based anchor is never silent.
            logger.warning(
                f"[executor] {coin} entry avgPx missing in fill response "
                f"(filled_sz={filled_size}) — anchoring tracker/brackets "
                f"from mid_price fallback px={entry_px}"
            )
        if filled_size > 0:
            size_in_coin = filled_size
        position_notional = abs(size_in_coin) * entry_px

        # Register the position with the DSL tracker; it re-evaluates the exit
        # floor on every scan tick (loss protection -> profit locking).
        dsl_config = config.get("dsl_exit", {})
        # Regime-aware exits: scalp (base) in chop/down to bank fast; trend-ride
        # params when regime=='up' to ride rippers. detect_regime is cached
        # (TTL) and already computed by the market_regime gate in this same
        # execute flow — no extra fetch.
        # Reuse the authoritative regime resolved once for this decision (it is
        # cached on the analysis). Never re-detect here: a boundary flip between
        # sizing and registration made the locked stop disagree with the sized
        # stop — the LIT defect.
        _regime = str(analysis.get("_decision_regime") or "neutral")
        if not analysis.get("_decision_regime"):
            try:
                from hermes_trader.agents.market_regime import detect_regime
                _regime = detect_regime(analysis["coin"])
            except Exception as _re_e:
                logger.debug(f"[executor] regime lookup failed (non-fatal): {_re_e}")
        _ex_protect, _ex_retrace, _tiers_raw, _ex_ml_pct, _ex_ml_roe, _ex_label = \
            ex.select_exit_params(dsl_config, _regime)
        # phase2_tiers is optional in config; when present it OVERRIDES the class
        # default ladder so profit-locking tightness is tunable without code edits.
        _tiers = [ex.RetraceTier(**t) for t in _tiers_raw] if _tiers_raw else None
        _atr_cfg = dsl_config.get("atr_stop", {}) or {}
        _noise_cfg = dsl_config.get("noise_band", {}) or {}
        # Audit 2026-09-06 (E4, P2): wire smooth-transition (was a dead knob;
        # default OFF/inert). Kept in lockstep with dsl_exit._build_policy_from_config.
        _smooth_cfg = dsl_config.get("smooth_transition", {}) or {}
        # Audit 2026-09-06 (E3, P2): regime-split clocks (inert → global clock
        # unless regime_aware.clocks.enabled) and the time-scratch knob block.
        _clocks = ex.resolve_regime_clocks(dsl_config, _regime)
        _scratch_cfg = dsl_config.get("time_scratch", {}) or {}
        logger.info(f"[executor] exit policy = {_ex_label} (regime={_regime}) "
                    f"protect={_ex_protect} retrace={_ex_retrace} "
                    f"max_loss={_ex_ml_pct}% max_loss_roe={_ex_ml_roe}%")
        policy = ex.ExitPolicy(
            max_loss_pct=_ex_ml_pct,
            max_loss_roe_pct=_ex_ml_roe,
            protect_pct=_ex_protect,
            retrace_threshold=_ex_retrace,
            # Audit 2026-09-06 (E3, P2): regime-split clocks when enabled.
            hard_timeout_minutes=_clocks["hard_timeout_minutes"],
            breakeven_trigger_pct=dsl_config.get("breakeven_trigger_pct", 0.0),
            breakeven_lock_pct=dsl_config.get("breakeven_lock_pct", 0.0),
            atr_stop_enabled=bool(_atr_cfg.get("enabled", False)),
            atr_stop_mult=float(_atr_cfg.get("atr_mult", 1.5)),
            atr_stop_floor_pct=float(_atr_cfg.get("floor_pct", 1.0)),
            atr_stop_ceiling_pct=float(_atr_cfg.get("ceiling_pct", 4.0)),
            stale_flat_timeout_minutes=float(_clocks["stale_flat_timeout_minutes"] or 0.0),
            consecutive_breaches_required=int(dsl_config.get("consecutive_breaches_required", 1) or 1),
            # A-F5: persist-confirmed (4s default) floor breach; config key
            # dsl_exit.breach_confirm_sec drives both this and policy_from_config.
            breach_confirm_sec=float(dsl_config.get("breach_confirm_sec", ex.cfg_get("dsl_exit.breach_confirm_sec", default=4.0)) or 0.0),
            # Audit 2026-09-06 (E5, P2): propagate hard_stop_confirm_sec the same
            # way breach_confirm_sec is wired; previously the dataclass default
            # (1.0s) was always used here even when config overrode it.
            hard_stop_confirm_sec=float(dsl_config.get("hard_stop_confirm_sec", ex.cfg_get("dsl_exit.hard_stop_confirm_sec", default=1.0)) or 0.0),
            noise_band_enabled=bool(_noise_cfg.get("enabled", False)),
            noise_band_atr_mult=float(_noise_cfg.get("atr_mult", 1.0)),
            # Audit 2026-09-06 (E4, P2): smooth phase1→phase2 ramp (default OFF).
            smooth_transition_enabled=bool(_smooth_cfg.get("enabled", False)),
            smooth_band_pct=float(_smooth_cfg.get("band_pct", 1.0)),
            # Audit 2026-09-06 (E3, P2): time scratch exit (default OFF/inert).
            time_scratch_enabled=bool(_scratch_cfg.get("enabled", False)),
            time_scratch_minutes=float(_scratch_cfg.get("minutes", 60.0)),
            time_scratch_min_peak_pct=float(_scratch_cfg.get("min_peak_pct", 0.3)),
            time_scratch_giveback_pct=float(_scratch_cfg.get("giveback_pct", 0.3)),
            phase2_tiers=_tiers if _tiers else ex.ExitPolicy().phase2_tiers,
        )
        # ATR as % of entry — captured once here so the DSL stop width is stable
        # for the life of the trade (the atr_stop feature scales off this).
        entry_atr_pct = (atr / entry_px * 100) if entry_px > 0 else 0.0
        # Use actual fill time from the exchange response when available;
        # this gives the DSL hard_timeout an accurate baseline instead of
        # counting from "now" (which would be early for slow fills).
        _fill_ms = order_res.get("filled_at_ms")
        _entry_time_sec = (_fill_ms / 1000.0) if _fill_ms else time.time()
        ex.register_position(coin, trade_side, entry_px, entry_time=_entry_time_sec,
                          policy=policy, leverage=leverage,
                          entry_atr_pct=entry_atr_pct, entry_regime=_regime)
        logger.info(f"[executor] Registered DSL exit for {coin} {trade_side} @ {entry_px} ({leverage}x)")

        # ── Sizing-v2 drift guard: assert the stop the sizer assumed matches
        # the stop the DSL actually registered, within 5%. This catches any
        # future silent divergence between compute_effective_stop_pct (sizing
        # mirror) and dsl_exit._evaluate (the live floor). The two use
        # different price bases (sizing: mid; DSL: fill) so atr_pct differs
        # by slippage-scale bps — we compare the PURE three-layer core_stop
        # (spike/slip adjustments are sizing-only and intentionally excluded).
        _sizing_bd = analysis.get("_sizing_v2_breakdown")
        if _sizing_bd:
            try:
                _sizing_core = float(_sizing_bd.get("core_stop", -1.0))
                _lev = max(1.0, float(leverage))
                # F1 sync (byte-aligned with dsl_exit._effective_max_loss
                # L632-641): spot = min(regime cap, ATR cap). The ATR stop may
                # only widen up to the regime cap — it must never override a
                # tighter regime max_loss, else this guard mirrors the sizer's
                # stale value and false-negatives a real divergence (dev=0).
                _dsl_regime = (float(policy.max_loss_pct)
                               if float(policy.max_loss_pct) > 0 else float("inf"))
                if policy.atr_stop_enabled and entry_atr_pct > 0:
                    _dsl_atr = min(
                        max(entry_atr_pct * policy.atr_stop_mult,
                            policy.atr_stop_floor_pct),
                        policy.atr_stop_ceiling_pct)
                    _dsl_spot = min(_dsl_regime, _dsl_atr)
                else:
                    _dsl_spot = _dsl_regime
                _dsl_roe = (float(policy.max_loss_roe_pct) / _lev
                            if float(policy.max_loss_roe_pct) > 0 else float("inf"))
                _dsl_spot = _dsl_spot if _dsl_spot > 0 else float("inf")
                _dsl_core = min(_dsl_spot, _dsl_roe)
                _dsl_core = _dsl_core if _dsl_core != float("inf") else -1.0
                _dev_pct = (abs(_dsl_core - _sizing_core) / _sizing_core * 100.0
                            if _sizing_core > 0 else 0.0)
                try:
                    from hermes_trader.metrics import SIZING_DSL_DEVIATION
                    SIZING_DSL_DEVIATION.set(_dev_pct)
                except Exception:
                    pass
                if _sizing_core > 0 and _dev_pct > ex._STOP_DRIFT_WARN_PCT:
                    _critical = _dev_pct > ex._STOP_DRIFT_CRITICAL_PCT
                    logger.warning(
                        f"[sizing-v2] STOP DRIFT {coin}: sizing_core={_sizing_core:.4f}% "
                        f"dsl_core={_dsl_core:.4f}% dev={_dev_pct:.1f}% "
                        f"(>{'CRITICAL' if _critical else 'warn'} "
                        f"{ex._STOP_DRIFT_CRITICAL_PCT if _critical else ex._STOP_DRIFT_WARN_PCT:g}%) — "
                        f"sizing/DSL logic desynced, investigate")
                    try:
                        from hermes_trader import notify
                        notify.send_card(
                            title=("🚨 仓位止损偏差严重脱钩 (STOP DRIFT CRITICAL)"
                                   if _critical else
                                   "⚠️ 仓位止损偏差告警 (STOP DRIFT)"),
                            level="danger",
                            category="risk",
                            fields={
                                "币种": coin,
                                "Sizing核心止损": f"{_sizing_core:.4f}%",
                                "DSL核心止损": f"{_dsl_core:.4f}%",
                                "偏差": (f"{_dev_pct:.1f}% (严重阈值 "
                                       f"{ex._STOP_DRIFT_CRITICAL_PCT:g}%，已全局暂停开仓 "
                                       f"{ex._STOP_DRIFT_CRITICAL_HALT_MIN} 分钟)"
                                       if _critical else
                                       f"{_dev_pct:.1f}% (阈值 "
                                       f"{ex._STOP_DRIFT_WARN_PCT:g}%)"),
                            },
                            markdown=("仓位 sizing 与 DSL 三层风控止损偏差超过严重阈值，"
                                      "风控逻辑疑似脱钩，已暂停后续开仓，请立即核查并手动恢复。"
                                      if _critical else
                                      "仓位 sizing 与 DSL 三层风控止损偏差超 5%，"
                                      "存在风控逻辑漂移脱钩风险，请核查。"),
                            dedup_key=f"stop_drift:{coin}:{'crit' if _critical else 'warn'}",
                        )
                    except Exception as _ne:
                        logger.warning(f"[sizing-v2] drift notify failed for {coin}: {_ne}")
                    # Q7 critical tier: the current fill can't be undone, but a
                    # >25% core-stop divergence means the two independent risk
                    # formulas disagree badly. Trip a short GLOBAL halt (the
                    # existing kill-switch the gates already consume) so no NEW
                    # position opens while the logic is suspect.
                    if _critical:
                        try:
                            _halt_until = int(time.time() * 1000
                                              + ex._STOP_DRIFT_CRITICAL_HALT_MIN * 60_000)
                            memory.set_global_halt(_halt_until)
                            logger.critical(
                                f"[sizing-v2] STOP DRIFT CRITICAL {coin} dev={_dev_pct:.1f}% "
                                f"-> GLOBAL HALT {ex._STOP_DRIFT_CRITICAL_HALT_MIN}min")
                            try:
                                from hermes_trader import notify
                                notify.send_text(
                                    f"🚨 {coin} 止损逻辑严重脱钩 dev={_dev_pct:.1f}%\n"
                                    f"已全局暂停新开仓 {ex._STOP_DRIFT_CRITICAL_HALT_MIN} 分钟，"
                                    f"核查 sizing/DSL 止损公式后手动恢复。",
                                    category="risk")
                            except Exception as _he:
                                logger.warning(
                                    f"[sizing-v2] drift halt alert failed for {coin}: {_he}")
                        except Exception as _hh:
                            logger.error(f"[sizing-v2] critical-drift halt failed: {_hh!r}")
                else:
                    logger.info(
                        f"[sizing-v2] drift check {coin}: sizing_core={_sizing_core:.4f}% "
                        f"dsl_core={_dsl_core:.4f}% dev={_dev_pct:.2f}% OK")
            except Exception as _dv_e:
                logger.warning(f"[sizing-v2] drift assertion failed for {coin}: {_dv_e}")

        _entry_ts = int(time.time() * 1000)
        # CS-E: 该笔下单瞬间实际生效（含 per-coin override）的阈值 era 指纹。
        # 纯读计算、内部吞异常，绝不阻断下单。order 仅带轻量 id 以便开仓即可
        # 归因；完整跟踪值随 entry_context→close 落库。
        _era = ex.compute_config_era(config)
        memory.record_trade({
            "id": str(uuid.uuid4()),
            "analysis_id": analysis["id"],
            "coin": coin,
            "side": trade_side,
            "entry_px": entry_px,
            "size_usd": position_notional,
            "order_id": order_res.get("order_id"),
            "executed_at": _entry_ts,
            "config_era_id": _era.get("era_id"),
        })

        # Entry-context snapshot for the forward signal backtest: record WHEN we
        # opened and WHAT the free signals said at entry (cache-only — no network
        # on the hot path) plus the enforcement decision. The matching close
        # pulls this so each outcome row carries (entry_time, signals_at_entry).
        try:
            from hermes_trader.agents.shadow_signals import gather_shadow_signals
            _entry_sig = gather_shadow_signals(coin, trade_side,
                                               config.get("shadow_signals") or {}, allow_fetch=False,
                                               config=config)
            # Execution-quality capture: arrival mid vs actual fill = real entry
            # slippage (the # the backtests don't model). Signed as adverse cost
            # bps (long paying above mid / short selling below = positive cost).
            _arr_mid = arrival_mid
            _fill = filled_px
            _slip_bps = None
            if _arr_mid > 0 and _fill > 0:
                raw = (_fill - _arr_mid) / _arr_mid * 1e4
                _slip_bps = round(raw if trade_side == "long" else -raw, 1)
            # Funding carry: capture the latest hourly funding rate at entry (one
            # call; entries are rare so this isn't the rate-sensitive scan path).
            # Realized funding cost is estimated at close from rate × hold_hrs ×
            # notional × side.
            _funding_hr = None
            try:
                from hermes_trader.client.hl_client import fetch_funding_history
                # P2-3: same config-driven lookback window as research._fetch_funding_rate.
                try:
                    _lb_h = int(ex.cfg_get("funding_lookback_hours", 24))
                    if _lb_h <= 0:
                        _lb_h = 24
                except (TypeError, ValueError):
                    _lb_h = 24
                _fh = fetch_funding_history(coin, int(time.time() * 1000) - _lb_h * 3_600_000)
                if _fh:
                    _r = float(_fh[-1].get("fundingRate", 0) or 0)
                    _funding_hr = _r if _r == _r else None  # NaN guard
            except Exception:
                _funding_hr = None
            memory.record_entry_context(coin, trade_side, {
                "entry_time": _entry_ts,
                "trace_id": analysis.get("trace_id") or "",
                "arrival_mid": _arr_mid,
                "entry_fill": _fill,
                "entry_slip_bps": _slip_bps,
                "funding_rate_hr": _funding_hr,
                "regime": _regime,          # market_regime at entry (already computed above)
                "config_era": _era,         # CS-E: 阈值 era 指纹 + 跟踪值快照
                "signals": _entry_sig,
                "enforcement": ({"veto": enf.veto, "veto_reason": enf.veto_reason,
                                 "boost": enf.boost, "boost_reason": enf.boost_reason}
                                if enf is not None else {}),
                "override_bar": override_composite,
                "forced_override": analysis.get("verdict") == "LONG"
                                   and "[structural override]" in (analysis.get("reasoning") or ""),
            })
        except Exception as _ec_e:
            logger.warning(f"[executor] entry-context capture failed (non-fatal): {_ec_e}")
            try:
                from hermes_trader import event_log
                event_log.append("error", payload={
                    "scope": "entry_context", "coin": coin,
                    "error": str(_ec_e)})
            except Exception as _ev_e:
                logger.error("[executor] entry_context event log failed: %r", _ev_e)
    except Exception as _state_e:
        logger.critical(f"[executor] LOCAL STATE WRITE FAILED after {coin} order "
                        f"placed on exchange — position is ORPHANED: {_state_e}")
        # The order FILLED on the exchange but ex.register_position/record_trade
        # failed, so the local tracker is missing until the next trading_loop
        # rehydrate cycle (up to ~60s away). During that window the DSL loop
        # won't monitor/manage this position (no stop trail, no exit). Shrink
        # the orphan window to near-zero by reconciling immediately against a
        # fresh account snapshot. Best-effort: a rehydrate failure must not
        # mask the original state-write error or abort the backup-SL path.
        try:
            from hermes_trader.agents.dsl_exit import rehydrate_from_exchange
            _rstate = ex.fetch_account_state(user)
            _rpositions = _rstate.get("asset_positions", []) or []
            _rdropped = rehydrate_from_exchange(
                _rpositions,
                default_leverage=int(config.get("leverage", 1) or 1),
                queried_dexes={""},
                user=user,
            )
            if _rdropped:
                logger.warning(f"[executor] immediate rehydrate dropped "
                               f"{len(_rdropped)} tracker(s): "
                               f"{[getattr(t, 'coin', '?') for t in _rdropped]}")
            logger.info(f"[executor] immediate rehydrate completed for orphan "
                        f"recovery after {coin} state-write failure")
        except Exception as _rh_e:
            logger.error(f"[executor] immediate rehydrate after state-write "
                         f"failure failed: {_rh_e} — position will be picked "
                         f"up on the next scheduled reconciliation cycle")
    finally:
        with ex._EXEC_LOCK:
            ex._IN_FLIGHT_ANALYSES.discard(aid)
            ex._IN_FLIGHT_COINS.discard(coin)
        # G-P1-3: register runs only on the filled success path — release the
        # cross-process entry flock here so the API process may enter again.
        ex._ENTRY_LOCK.release()

    return {
        "entry_px": entry_px,
        "position_notional": position_notional,
        "size_in_coin": size_in_coin,
        "filled_px": filled_px,
        "arrival_mid": arrival_mid,
        "regime": _regime,
    }
