"""hermes-trader client utilities."""

from hermes_trader.client.cache import _Cache, _CacheEntry, cached_api_call, get_global_cache
from hermes_trader.client.daemon import check_daemon_state, producer_daemon
from hermes_trader.client.hl_client import (
    _MS_PER_CANDLE,
    HL_API,
    assess_candle_quality,
    fetch_account_state,
    fetch_all_mids,
    fetch_hl_candles,
    get_info,
    start_ws_mids,
    stop_ws_mids,
)
from hermes_trader.client.lock import check_lock_status, scanner_lock
from hermes_trader.client.parallel import parallel
from hermes_trader.client.universe import get_market_by_coin, get_universe

__all__ = [
    # HL API
    "HL_API",
    "_MS_PER_CANDLE",
    "assess_candle_quality",
    "fetch_account_state",
    "fetch_all_mids",
    "fetch_hl_candles",
    "get_info",
    "start_ws_mids",
    "stop_ws_mids",
    # Universe
    "get_universe",
    "get_market_by_coin",
    # Utilities
    "_Cache",
    "_CacheEntry",
    "cached_api_call",
    "get_global_cache",
    "scanner_lock",
    "check_lock_status",
    "parallel",
    "producer_daemon",
    "check_daemon_state",
]
