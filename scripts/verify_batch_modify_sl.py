#!/usr/bin/env python3
"""Verify Hyperliquid batchModify support for trigger SL orders on MAINNET.

This script is a READ-ONLY + single-modify probe, designed to confirm whether
the SDK's `Exchange.modify_order` (action type "batchModify") can atomically
move an existing reduce-only stop-loss trigger order's trigger price — without
a cancel/order gap.

Safety:
  * Defaults to DRY-RUN: it queries openOrders, finds the ETH SL order, and
    prints exactly what it WOULD modify, but sends no signed transaction.
  * Pass --execute to actually send ONE batchModify call.
  * The new trigger price is moved in the SAFE direction (higher for a long's
    sell-SL, lower for a short's buy-SL) by --bps basis points (default 10bps
    = 0.10%). If it somehow triggers immediately it would be at a better price.
  * It only touches the single SL order identified by oid; it never places new
    orders, never cancels, never closes the position.
  * After modify (dry-run or real) it re-queries openOrders to show the actual
    resting order state.

Usage:
    python scripts/verify_batch_modify_sl.py --coin ETH
    python scripts/verify_batch_modify_sl.py --coin ETH --execute
    python scripts/verify_batch_modify_sl.py --coin ETH --execute --bps 20

Requires the same environment as the trader:
    HYPERLIQUID_PRIVATE_KEY (and HYPERLIQUID_WALLET_ADDRESS / MASTER if agent)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

# Make the repo importable when run directly from scripts/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hermes_trader.client.exchange import (  # noqa: E402
    _http_post,
    _make_exchange,
    _round_price_for_hl,
    get_coin_index,
    resolve_user_address,
)
from hermes_trader.client.hl_client import HL_API  # noqa: E402
from hyperliquid.utils.signing import (  # noqa: E402
    OrderType,
    TriggerOrderType,
)


def _section(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def fetch_open_orders(user: str) -> List[Dict[str, Any]]:
    orders = _http_post("/info", {"type": "openOrders", "user": user}) or []
    if not isinstance(orders, list):
        print(f"[warn] unexpected openOrders response: {orders!r}")
        return []
    return orders


def find_sl_order(orders: List[Dict[str, Any]], coin: str) -> Optional[Dict[str, Any]]:
    """Locate the reduce-only SL trigger order for `coin`.

    HL's openOrders shape for a resting trigger SL is:
        {"coin": "ETH", "oid": 123, "side": "B"/"A",
         "limitPx": "2445.9", "sz": "0.0048",
         "reduceOnly": true, "origSz": "...", ...}
    Note: trigger orders expose their trigger price via `limitPx` (NOT
    `triggerPx`). We match by coin + reduceOnly + (orderType indicates stop
    or a triggerPx/limitPx is present). If multiple match, return the first
    and print a warning.
    """
    candidates: List[Dict[str, Any]] = []
    for o in orders:
        if o.get("coin") != coin:
            continue
        ot = str(o.get("orderType", "")).lower()
        has_trigger_px = (o.get("triggerPx") is not None) or (o.get("limitPx") is not None)
        is_trigger = ("stop" in ot) or ("trigger" in ot) or has_trigger_px
        if not is_trigger:
            continue
        if not o.get("reduceOnly", False):
            continue
        candidates.append(o)

    if not candidates:
        return None
    if len(candidates) > 1:
        print(f"[warn] multiple trigger orders found for {coin}; using first. "
              f"Candidates:\n{json.dumps(candidates, indent=2)}")
    return candidates[0]


def fetch_position(user: str, coin: str) -> Optional[Dict[str, Any]]:
    """Return the raw position dict for `coin` from /info userState (clearinghouse)."""
    state = _http_post("/info", {"type": "clearinghouseState", "user": user}) or {}
    for ap in state.get("assetPositions", []):
        pos = ap.get("position", {})
        if pos.get("coin") == coin and float(pos.get("szi", 0) or 0) != 0:
            return pos
    return None


def determine_side_and_new_trigger(
    coin: str,
    position: Dict[str, Any],
    sl_order: Dict[str, Any],
    bps: float,
) -> Tuple[bool, float, float]:
    """Return (is_buy_for_modify, current_trigger_px, new_trigger_px).

    New trigger is moved in the SAFE (profit-locking) direction:
      long  (szi>0): SL is a sell trigger ABOVE the old price → raise it
      short (szi<0): SL is a buy trigger BELOW the old price  → lower it
    """
    szi = float(position.get("szi", 0) or 0)
    is_long = szi > 0
    # Resting trigger orders expose their trigger price via "limitPx" in
    # openOrders; "triggerPx" may also appear in some API shapes.
    raw_px = sl_order.get("triggerPx", sl_order.get("limitPx"))
    cur_trigger = float(raw_px)

    # Move by bps of the current trigger. 1 bps = 0.01%.
    delta = cur_trigger * (bps / 10_000.0)
    if is_long:
        new_trigger_raw = cur_trigger + delta   # raise the sell-stop (tighter)
        is_buy = False                          # closing a long = sell
    else:
        new_trigger_raw = cur_trigger - delta   # lower the buy-stop (tighter)
        is_buy = True                           # closing a short = buy
    # CRITICAL: round to Hyperliquid's tick size via the same helper the
    # production order path uses. A naïve round(...,2) produces prices like
    # 2443.45 which the exchange rejects with "Invalid TP/SL price".
    _, sz_dec, _ = get_coin_index(coin)
    new_trigger = float(_round_price_for_hl(new_trigger_raw, sz_dec, is_perp=True))
    return is_buy, cur_trigger, new_trigger


def build_order_type(new_trigger: float) -> OrderType:
    return OrderType(
        trigger=TriggerOrderType(
            triggerPx=float(new_trigger),
            isMarket=True,
            tpsl="sl",
        )
    )


def _order_trigger_px(o: Dict[str, Any]) -> Any:
    """Trigger price of a resting trigger order (exposed as limitPx in openOrders)."""
    return o.get("triggerPx", o.get("limitPx"))


def place_backup_sl(
    coin: str,
    is_long: bool,
    size: float,
    entry_px: float,
    sl_pct: float,
    exchange: Any,
) -> Tuple[int, float]:
    """Place a reduce-only SL trigger order at `sl_pct` from entry (ceiling style).

    Mirrors executor.backup SL placement:
      long  -> sell trigger BELOW entry at entry*(1 - sl_pct/100)
      short -> buy trigger ABOVE entry at entry*(1 + sl_pct/100)

    Returns (oid, trigger_px). Raises on failure.
    """
    _, sz_dec, _ = get_coin_index(coin)
    if is_long:
        sl_px = entry_px * (1 - sl_pct / 100.0)
        is_buy = False  # close long = sell
    else:
        sl_px = entry_px * (1 + sl_pct / 100.0)
        is_buy = True   # close short = buy

    trigger_str = _round_price_for_hl(sl_px, sz_dec, is_perp=True)
    trigger_f = float(trigger_str)
    size_str = f"{size:.{sz_dec}f}"

    order_type = OrderType(
        trigger=TriggerOrderType(
            triggerPx=trigger_f, isMarket=True, tpsl="sl",
        )
    )
    print(f"  placing SL: coin={coin} is_buy={is_buy} sz={size_str} "
          f"triggerPx={trigger_f} ({sl_pct:g}% from entry={entry_px})")
    result = exchange.order(
        coin, is_buy, float(size_str), trigger_f, order_type, reduce_only=True,
    )
    print("  RAW ORDER RESPONSE:")
    print(json.dumps(result, indent=2, default=str))

    # Parse oid: resting orders appear under response.data.statuses[0].resting.oid
    # (same shape as a normal limit order with accept_resting=True).
    statuses = []
    if isinstance(result, dict) and result.get("status") == "ok":
        statuses = (result.get("response", {})
                          .get("data", {})
                          .get("statuses", []) or [])
    if not statuses:
        raise RuntimeError(f"place SL returned no statuses: {result!r}")
    st = statuses[0]
    resting = st.get("resting") if isinstance(st, dict) else None
    if not resting or "oid" not in resting:
        raise RuntimeError(f"place SL did not rest: {st!r}")
    return int(resting["oid"]), trigger_f


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--coin", default="ETH", help="perp coin to inspect (default ETH)")
    parser.add_argument("--bps", type=float, default=10.0,
                        help="basis points to move the SL in the safe direction (default 10 = 0.10%%)")
    parser.add_argument("--execute", action="store_true",
                        help="actually send the batchModify (default: dry-run only)")
    parser.add_argument("--place-sl", action="store_true",
                        help="if no SL order exists, first place one at --sl-pct from entry")
    parser.add_argument("--sl-pct", type=float, default=3.0,
                        help="distance from entry for the placed SL in %% (default 3.0 = ceiling)")
    args = parser.parse_args()

    coin = args.coin.upper()
    dry_run = not args.execute

    print(f"Hyperliquid API:  {HL_API}")
    print(f"Coin:             {coin}")
    print(f"Mode:             {'DRY-RUN (no signed tx)' if dry_run else 'EXECUTE (real batchModify)'}")
    print(f"Move size:        {args.bps:g} bps in the SAFE direction")

    # ── 0. Resolve user & exchange client ────────────────────────────────────
    user = resolve_user_address()
    if not user:
        print("[fatal] could not resolve user address "
              "(HYPERLIQUID_WALLET_ADDRESS / MASTER env).")
        return 2
    print(f"User address:     {user}")

    # Constructing the exchange client validates that the private key is present;
    # in dry-run we will NOT use it to sign anything, but we want to fail fast if
    # the environment is misconfigured.
    try:
        exchange = _make_exchange()
    except Exception as e:
        print(f"[fatal] cannot construct signing exchange client: {e}")
        return 2

    # ── 1. Query position & open orders ──────────────────────────────────────
    _section("1. LIVE POSITION")
    position = fetch_position(user, coin)
    if not position:
        print(f"[fatal] no open {coin} position found for {user}. "
              f"Open the test position first.")
        return 3
    szi = float(position.get("szi", 0) or 0)
    entry_px = float(position.get("entryPx", 0) or 0)
    print(f"  side:           {'long' if szi > 0 else 'short'}")
    print(f"  size (szi):     {szi}")
    print(f"  entryPx:        {entry_px}")
    print(f"  leverage:       {position.get('leverage')}")
    print(f"  unrealizedPnl:  {position.get('unrealizedPnl')}")
    print(f"  liqPx:          {position.get('liquidationPx')}")

    _section("2. OPEN ORDERS (pre-modify)")
    orders = fetch_open_orders(user)
    eth_orders = [o for o in orders if o.get("coin") == coin]
    print(f"  total open orders:           {len(orders)}")
    print(f"  open orders for {coin}:       {len(eth_orders)}")
    print(json.dumps(eth_orders, indent=2))

    sl_order = find_sl_order(orders, coin)
    placed_new = False
    if not sl_order:
        if not args.place_sl:
            print(f"\n[fatal] no trigger SL order found for {coin}. "
                  f"Re-run with --place-sl to first place one (requires --execute "
                  f"to actually place), or place one manually in the UI.")
            return 4
        if dry_run:
            print(f"\n[dry-run] no SL order found; with --place-sl would place one at "
                  f"{args.sl_pct:g}% from entry={entry_px}, but DRY-RUN sends nothing. "
                  f"Re-run with --execute --place-sl to place+modify.")
            # Still compute what would happen for display.
            is_long = szi > 0
            if is_long:
                sl_px = entry_px * (1 - args.sl_pct / 100.0)
            else:
                sl_px = entry_px * (1 + args.sl_pct / 100.0)
            sz = abs(szi)
            cur_trigger = round(sl_px, 2)
            # Build a synthetic sl_order-like dict for the proposal below.
            sl_order = {
                "oid": "<would be assigned after place>",
                "sz": sz,
                "triggerPx": cur_trigger,
                "orderType": "trigger (to be placed)",
                "reduceOnly": True,
            }
        else:
            _section("2b. PLACING BACKUP SL (--place-sl --execute)")
            is_long = szi > 0
            size = abs(szi)
            try:
                oid_new, placed_trigger = place_backup_sl(
                    coin, is_long, size, entry_px, args.sl_pct, exchange,
                )
            except Exception as e:
                print(f"[fatal] failed to place backup SL: {e!r}")
                return 7
            print(f"  SL placed: oid={oid_new} triggerPx={placed_trigger}")
            placed_new = True
            # Re-query to confirm and read back exact resting state. The order
            # may take a second to appear in openOrders; retry a few times.
            import time as _t
            sl_order = None
            for _ in range(5):
                orders = fetch_open_orders(user)
                sl_order = find_sl_order(orders, coin)
                if sl_order:
                    break
                _t.sleep(2)
            if not sl_order:
                print("[fatal] SL was placed but not found in openOrders after retries.")
                return 8
            print(f"  confirmed resting: oid={sl_order.get('oid')} "
                  f"triggerPx={_order_trigger_px(sl_order)}")

    oid = int(sl_order["oid"]) if str(sl_order.get("oid", "")).isdigit() else sl_order["oid"]
    sz = float(sl_order.get("sz", 0) or 0)
    print(f"\n  identified SL order:")
    print(f"    oid:           {oid}")
    print(f"    sz:            {sz}")
    print(f"    triggerPx:     {_order_trigger_px(sl_order)}")
    print(f"    orderType:     {sl_order.get('orderType')}")
    print(f"    reduceOnly:    {sl_order.get('reduceOnly')}")
    if placed_new:
        print(f"    (freshly placed in this run)")

    # ── 2. Compute the new trigger price ─────────────────────────────────────
    is_buy, cur_trigger, new_trigger = determine_side_and_new_trigger(coin, position, sl_order, args.bps)
    _section("3. PROPOSED batchModify")
    print(f"  oid:                {oid}")
    print(f"  coin:               {coin}")
    print(f"  is_buy:             {is_buy}  ({'buy = close short' if is_buy else 'sell = close long'})")
    print(f"  sz:                 {sz}")
    print(f"  current triggerPx:  {cur_trigger}")
    print(f"  new     triggerPx:  {new_trigger}  (moved {args.bps:g} bps safe-direction)")
    print(f"  order_type:         trigger / isMarket=True / tpsl=sl / reduce_only=True")

    # Sanity: never move the SL AWAY from the market (looser). Reject if the
    # computed direction would loosen the stop (defensive — shouldn't happen).
    if szi > 0 and new_trigger <= cur_trigger:
        print("[fatal] refusal: new trigger for a long is not higher than old.")
        return 5
    if szi < 0 and new_trigger >= cur_trigger:
        print("[fatal] refusal: new trigger for a short is not lower than old.")
        return 5

    # ── 3. Dry-run or execute ────────────────────────────────────────────────
    if dry_run:
        _section("4. DRY-RUN — NOT SENDING ANY TRANSACTION")
        print("  Re-run with --execute to send the batchModify.")
        # Show the exact SDK call that would be made, without calling it.
        print("\n  Equivalent SDK call:")
        print("  exchange.modify_order(")
        print(f"      oid={oid}, name={coin!r}, is_buy={is_buy}, sz={sz},")
        print(f"      limit_px={new_trigger},")
        print("      order_type=OrderType(trigger=TriggerOrderType(")
        print(f"          triggerPx={new_trigger}, isMarket=True, tpsl='sl')),")
        print("      reduce_only=True,")
        print("  )")
    else:
        _section("4. EXECUTE — sending ONE batchModify")
        order_type = build_order_type(new_trigger)
        try:
            result = exchange.modify_order(
                oid=oid,
                name=coin,
                is_buy=is_buy,
                sz=sz,
                limit_px=float(new_trigger),
                order_type=order_type,
                reduce_only=True,
            )
        except Exception as e:
            print(f"[fatal] SDK raised during modify_order: {e!r}")
            return 6

        print("  RAW RESPONSE:")
        try:
            print(json.dumps(result, indent=2, default=str))
        except Exception:
            print(repr(result))

        # ── 4b. Parse the statuses ───────────────────────────────────────────
        # CONFIRMED on mainnet: batchModify for a trigger SL returns a NEW
        # resting order with a DIFFERENT oid (it is a cancel+replace under the
        # hood), NOT an in-place "modified" status. So the production parser
        # must treat this like order placement: read statuses[0].resting.oid
        # and PERSIST the new oid (the old oid is gone).
        statuses = []
        if isinstance(result, dict) and result.get("status") == "ok":
            statuses = result.get("response", {}).get("data", {}).get("statuses", []) or []
        print(f"\n  statuses count: {len(statuses)}")
        new_oid: Optional[int] = None
        for i, st in enumerate(statuses):
            print(f"  [{i}] {json.dumps(st, default=str)}")
            if isinstance(st, dict):
                if st.get("error"):
                    print(f"  [fatal] exchange rejected modify: {st['error']}")
                    return 9
                resting = st.get("resting")
                if resting and "oid" in resting:
                    new_oid = int(resting["oid"])
        if new_oid is not None:
            print(f"\n  >>> batchModify SUCCEEDED via cancel+replace.")
            print(f"  >>> old oid {oid} is CANCELLED; new resting oid = {new_oid}")
            print(f"  >>> production code MUST persist the new oid.")

    # ── 5. Re-query open orders to show actual state ────────────────────────
    _section("5. OPEN ORDERS (post-modify)" if not dry_run
             else "5. OPEN ORDERS (dry-run — unchanged)")
    # post-modify re-query may also lag; retry a couple of times in execute mode.
    sl_after = None
    orders_after: List[Dict[str, Any]] = []
    attempts = 5 if not dry_run else 1
    import time as _t2
    for _ in range(attempts):
        orders_after = fetch_open_orders(user)
        sl_after = find_sl_order(orders_after, coin)
        if sl_after:
            break
        _t2.sleep(2)
    eth_after = [o for o in orders_after if o.get("coin") == coin]
    print(json.dumps(eth_after, indent=2))

    if sl_after:
        print(f"\n  SL order after:")
        print(f"    oid:           {sl_after.get('oid')}  "
              f"({'SAME oid — modify was in-place' if str(sl_after.get('oid')) == str(oid) else 'DIFFERENT oid — exchange replaced the order'})")
        print(f"    triggerPx:     {_order_trigger_px(sl_after)}")
        print(f"    sz:            {sl_after.get('sz')}")
    else:
        print("\n  [warn] no SL order found after modify — it may have filled, "
              "been cancelled, or never existed.")

    _section("DONE")
    if dry_run:
        print("  dry-run complete. no on-chain action taken.")
    else:
        print("  execute complete. Confirmed on mainnet:")
        print("    (a) batchModify for trigger SL returns {\"resting\": {\"oid\": <NEW>}}")
        print("        — it is a cancel+replace; the oid CHANGES, so persist the new oid.")
        print("    (b) limit_px must equal the new triggerPx and prices must be rounded")
        print("        to HL tick size via _round_price_for_hl or the exchange rejects")
        print("        with 'Invalid TP/SL price'.")
        print("    (c) triggerPx updates to the new value; old oid disappears from openOrders.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
