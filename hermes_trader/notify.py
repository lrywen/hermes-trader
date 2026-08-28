"""Generic Feishu notification dispatcher for Hermes Trader.

Centralizes webhook delivery so any subsystem (trading loop, risk gates,
surge detector, daily report) can push a card with a single call. Three
webhook pairs can be configured so traffic is split across dedicated bots:

  FEISHU_WEBHOOK_URL              - trade-execution webhook, used for the
                                    "trade" category: open fills, AI/DSL/
                                    manual closes.
  FEISHU_WEBHOOK_SECRET           - optional HMAC-SHA256 signing secret.
  FEISHU_SIGNAL_WEBHOOK_URL       - signal webhook, used for the "signal"
                                    category: risk-intercepted opens and the
                                    AI research verdict (LONG/SHORT/CLOSE).
                                    Falls back to the primary webhook when
                                    unset so single-bot deployments keep
                                    working.
  FEISHU_SIGNAL_WEBHOOK_SECRET    - signing secret for the signal webhook.
  FEISHU_NON_TRADE_WEBHOOK_URL    - secondary webhook, used for all other
                                    categories ("risk", "system", "surge",
                                    "report"). Falls back to the primary if
                                    not set.
  FEISHU_NON_TRADE_WEBHOOK_SECRET - signing secret for the secondary webhook.
  HERMES_BASE_URL                 - base URL used for in-card action buttons

A category-based on/off switch lets the operator choose what gets pushed
without redeploying. FEISHU_NOTIFY_CATEGORIES is a comma-separated allow-list
(defaults to "trade,signal,risk,system,ai,surge,report"). The legacy "ai"
category is routed identically to "signal". Any category not listed is
silently dropped.

Delivery is best-effort: every public call swallows network/parse errors and
never raises, so a notification outage can never block trading.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
import threading
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("hermes_trader.notify")

# Primary webhook — carries trade-execution traffic (open fills, AI/DSL/
# manual closes).
FEISHU_WEBHOOK_URL = os.environ.get("FEISHU_WEBHOOK_URL", "").strip()
FEISHU_WEBHOOK_SECRET = os.environ.get("FEISHU_WEBHOOK_SECRET", "").strip()

# Signal webhook — carries risk interceptions and the AI research verdict.
# Falls back to the primary webhook when unset.
FEISHU_SIGNAL_WEBHOOK_URL = os.environ.get(
    "FEISHU_SIGNAL_WEBHOOK_URL", ""
).strip()
FEISHU_SIGNAL_WEBHOOK_SECRET = os.environ.get(
    "FEISHU_SIGNAL_WEBHOOK_SECRET", ""
).strip()

# Secondary webhook — carries non-trade traffic (risk/system/surge/report).
# If unset, these categories fall back to the primary webhook so a single-bot
# deployment keeps working without extra configuration.
FEISHU_NON_TRADE_WEBHOOK_URL = os.environ.get(
    "FEISHU_NON_TRADE_WEBHOOK_URL", ""
).strip()
FEISHU_NON_TRADE_WEBHOOK_SECRET = os.environ.get(
    "FEISHU_NON_TRADE_WEBHOOK_SECRET", ""
).strip()

HERMES_BASE_URL = os.environ.get("HERMES_BASE_URL", "").strip().rstrip("/")
_TIMEOUT_S = 5.0

# Categories routed to the dedicated signal webhook. "ai" is kept as a legacy
# alias so older call sites / deployments keep routing correctly.
SIGNAL_CATEGORIES = {"signal", "ai"}

# Category allow-list. "surge" keeps its own card (rich report + button) but
# still respects this gate so a single switch can mute everything.
_DEFAULT_CATEGORIES = {"trade", "signal", "risk", "system", "ai", "surge", "report"}
_raw_cats = os.environ.get("FEISHU_NOTIFY_CATEGORIES", "").strip()
if _raw_cats:
    ENABLED_CATEGORIES = {
        c.strip().lower() for c in _raw_cats.split(",") if c.strip()
    }
else:
    ENABLED_CATEGORIES = set(_DEFAULT_CATEGORIES)

# Card header template colors per severity.
_LEVEL_TEMPLATE = {
    "info": "blue",
    "success": "green",
    "warn": "orange",
    "danger": "red",
}
_LEVEL_EMOJI = {
    "info": "ℹ️",
    "success": "✅",
    "warn": "⚠️",
    "danger": "🚨",
}

# 卡片页脚分类中文名
_CATEGORY_CN = {
    "trade": "交易执行",
    "signal": "信号",
    "risk": "风控",
    "system": "系统",
    "ai": "AI 决策",
    "surge": "暴涨复盘",
    "report": "报告",
}

# In-process throttle: (category, dedup_key) -> last sent epoch seconds.
# Same (category, key) won't re-push within the throttle window, preventing
# storms such as a per-coin gate firing every 60s scan cycle.
_throttle_lock = threading.Lock()
_last_sent: Dict[Tuple[str, str], float] = {}
_THROTTLE_S = 600.0

# ── R11-A1: send-side resilience (retry + circuit breaker + fallback) ────
# Send attempts on 429 / network errors get up to ``_RETRY_MAX_TRIES`` tries
# with exponential backoff (base 0.5s, doubles per try, capped at 4s). After
# ``_CB_FAILURE_THRESHOLD`` consecutive failures the breaker opens for
# ``_CB_OPEN_S`` seconds and sends are short-circuited without touching the
# network. On a primary-channel failure that is NOT 429 we transparently fall
# back to the next configured webhook so a single-bot outage never drops the
# "trade" / "risk" alerts.
_RETRY_MAX_TRIES = int(os.environ.get("HERMES_NOTIFY_RETRY_MAX_TRIES", "3"))
_RETRY_BACKOFF_BASE_S = float(
    os.environ.get("HERMES_NOTIFY_RETRY_BACKOFF_BASE_S", "0.5")
)
_RETRY_BACKOFF_MAX_S = float(
    os.environ.get("HERMES_NOTIFY_RETRY_BACKOFF_MAX_S", "4.0")
)
_CB_FAILURE_THRESHOLD = int(
    os.environ.get("HERMES_NOTIFY_CB_THRESHOLD", "5")
)
_CB_OPEN_S = float(os.environ.get("HERMES_NOTIFY_CB_OPEN_S", "60.0"))
_cb_state_lock = threading.Lock()
_cb_open_until: float = 0.0  # epoch seconds; 0 = closed
_cb_failures: int = 0
# Per-channel failure counter so primary / signal / non-trade circuit
# independently — one dead channel must not block the others.
_cb_state_by_channel: Dict[str, Tuple[float, int]] = {}  # url -> (open_until, failures)


def _is_retryable_error(exc: BaseException, status: int) -> bool:
    """Classify a send attempt as retry-eligible.

    429 (rate-limited) and 5xx (transient server error) are retried in place.
    Timeout / connection / DNS errors are also retried because they map to
    transient network failures. Auth (401/403) and bad-payload (400/404)
    errors are NOT retried — the next attempt will fail the same way.

    Note: when ``status`` is set (i.e. an HTTPError with a known code), the
    status is the source of truth and we deliberately do NOT consult
    ``isinstance(exc, OSError)`` — an HTTPError always inherits OSError
    so that would mark every 4xx as retryable.
    """
    if status != -1:
        return status == 429 or 500 <= status < 600
    import urllib.error
    if isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError)):
        return True
    if isinstance(exc, OSError):
        return True
    return False


def _cb_is_open(channel_url: str) -> bool:
    """Return True if the per-channel breaker is currently open (skip send)."""
    with _cb_state_lock:
        # Legacy single-bucket state (kept for /metrics backward compat).
        if _cb_open_until and time.time() < _cb_open_until:
            return True
        entry = _cb_state_by_channel.get(channel_url)
        if entry is None:
            return False
        open_until, _failures = entry
        return open_until > 0 and time.time() < open_until


def _cb_record(channel_url: str, succeeded: bool) -> None:
    """Update the per-channel breaker counters after one send attempt."""
    global _cb_open_until, _cb_failures
    with _cb_state_lock:
        open_until, failures = _cb_state_by_channel.get(
            channel_url, (0.0, 0)
        )
        if succeeded:
            failures = 0
            open_until = 0.0
        else:
            failures += 1
            if failures >= _CB_FAILURE_THRESHOLD:
                open_until = time.time() + _CB_OPEN_S
                logger.warning(
                    "notify: circuit OPEN for %s after %d consecutive "
                    "failures — short-circuiting for %.0fs",
                    channel_url, failures, _CB_OPEN_S,
                )
        _cb_state_by_channel[channel_url] = (open_until, failures)
        # Mirror into the legacy single-bucket gauge.
        _cb_failures = failures
        _cb_open_until = open_until
        try:
            from hermes_trader import metrics
            metrics.NOTIFY_CIRCUIT_OPEN.labels(channel=channel_url).set(
                1.0 if open_until > time.time() else 0.0
            )
        except Exception:
            pass


def _post_with_retry(
    payload: Dict[str, Any], channel_url: str, channel_secret: str,
) -> bool:
    """POST ``payload`` to ``channel_url`` with exponential-backoff retry.

    Returns True on success, False on terminal failure. Increments the
    NOTIFY_SEND_RETRIES / NOTIFY_SEND_FAILURES metrics and updates the
    per-channel circuit breaker. NEVER raises (network errors are
    logged + swallowed so a notification outage cannot break the caller).
    """
    if _cb_is_open(channel_url):
        logger.debug("notify: circuit open, skipping send to %s", channel_url)
        return False

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if channel_secret:
        ts = int(time.time())
        payload["timestamp"] = str(ts)
        payload["sign"] = _sign(channel_secret, ts)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    import urllib.error
    last_exc: Optional[BaseException] = None
    last_status: int = -1
    for attempt in range(_RETRY_MAX_TRIES):
        try:
            req = urllib.request.Request(
                channel_url,
                data=body,
                headers={"Content-Type": "application/json; charset=utf-8"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                data = {"raw": raw}
            ok = data.get("code", data.get("StatusCode", -1)) == 0
            if ok:
                _cb_record(channel_url, succeeded=True)
                return True
            # Feishu returns code != 0 (e.g. 19001/19003). Treat as a soft
            # error: don't trip the breaker (the channel is up), but the
            # caller will fall back to the next channel.
            _cb_record(channel_url, succeeded=True)
            logger.warning(
                "notify: Feishu push rejected: %s (channel=%s)",
                data, channel_url,
            )
            return False
        except urllib.error.HTTPError as e:
            last_status = e.code
            last_exc = e
            if not _is_retryable_error(e, e.code):
                logger.warning(
                    "notify: non-retryable HTTP %s to %s: %r",
                    e.code, channel_url, e,
                )
                _cb_record(channel_url, succeeded=False)
                return False
        except Exception as e:  # noqa: BLE001 — never let notification break caller
            last_exc = e
            last_status = -1
            if not _is_retryable_error(e, -1):
                logger.warning(
                    "notify: non-retryable error to %s: %r",
                    channel_url, e,
                )
                _cb_record(channel_url, succeeded=False)
                return False

        # Backoff between retries.
        if attempt < _RETRY_MAX_TRIES - 1:
            sleep_for = min(
                _RETRY_BACKOFF_BASE_S * (2 ** attempt),
                _RETRY_BACKOFF_MAX_S,
            )
            try:
                from hermes_trader import metrics
                metrics.NOTIFY_SEND_RETRIES.labels(
                    channel=channel_url
                ).inc()
            except Exception:
                pass
            logger.debug(
                "notify: retry %d/%d after %.1fs (last_status=%s last_exc=%r)",
                attempt + 1, _RETRY_MAX_TRIES - 1, sleep_for,
                last_status, last_exc,
            )
            time.sleep(sleep_for)

    # Exhausted retries.
    logger.warning(
        "notify: send to %s exhausted after %d tries (last_status=%s "
        "last_exc=%r)",
        channel_url, _RETRY_MAX_TRIES, last_status, last_exc,
    )
    _cb_record(channel_url, succeeded=False)
    try:
        from hermes_trader import metrics
        metrics.NOTIFY_SEND_FAILURES.labels(channel=channel_url).inc()
    except Exception:
        pass
    return False


def _fallback_chain(category: str) -> List[Tuple[str, str]]:
    """Return the ordered list of (url, secret) pairs to try for a category.

    The first entry is the channel returned by ``_resolve_webhook``; the
    subsequent entries are the other configured channels so a primary-bot
    outage transparently degrades to the secondary / non-trade bot.

    Duplicates and empty URLs are filtered. The order is:
      1) resolved channel for this category
      2) all other configured webhooks (primary, signal, non-trade)
    """
    seen: set = set()
    chain: List[Tuple[str, str]] = []
    primary_url, primary_secret = _resolve_webhook(category)
    if primary_url:
        chain.append((primary_url, primary_secret))
        seen.add(primary_url)
    for url, secret in (
        (FEISHU_WEBHOOK_URL, FEISHU_WEBHOOK_SECRET),
        (FEISHU_SIGNAL_WEBHOOK_URL, FEISHU_SIGNAL_WEBHOOK_SECRET),
        (FEISHU_NON_TRADE_WEBHOOK_URL, FEISHU_NON_TRADE_WEBHOOK_SECRET),
    ):
        if url and url not in seen:
            chain.append((url, secret))
            seen.add(url)
    return chain


def _send_with_fallback(
    title: str, fields: Optional[Dict[str, Any]], category: str,
    level: str, markdown: str, button_text: str, button_url: str,
) -> bool:
    """Try the primary channel, then fall back to other configured webhooks.

    The card body is built ONCE (signatures are bound to a single body) and
    re-signed for each channel's secret. Returns True if any channel
    accepted the card.
    """
    chain = _fallback_chain(category)
    if not chain:
        logger.debug("notify: no channels configured for %s", category)
        return False
    primary_url, primary_secret = chain[0]

    # Build the card ONCE. Note: timestamp + sign are channel-specific and
    # get re-applied inside _post_with_retry so we MUST NOT sign here.
    template = _LEVEL_TEMPLATE.get(level, "blue")
    emoji = _LEVEL_EMOJI.get(level, "ℹ️")
    elements: List[Dict[str, Any]] = []
    fields = fields or {}
    if fields:
        elements.append({
            "tag": "div",
            "fields": [
                {"is_short": True,
                 "text": {"tag": "lark_md",
                          "content": f"**{label}**\n{_fmt_val(value)}"}}
                for label, value in fields.items()
            ],
        })
    if markdown:
        elements.append({"tag": "div",
                         "text": {"tag": "lark_md", "content": markdown}})
    if button_text and button_url:
        elements.append({
            "tag": "action",
            "actions": [{
                "tag": "button",
                "text": {"tag": "plain_text", "content": button_text},
                "type": "primary" if level != "danger" else "danger",
                "url": button_url,
            }],
        })
    elements.append({"tag": "hr"})
    cat_cn = _CATEGORY_CN.get(category.strip().lower(), category)
    elements.append({
        "tag": "note",
        "elements": [{"tag": "plain_text",
                      "content": f"Hermes Trader · {cat_cn}"}],
    })
    base_payload: Dict[str, Any] = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": f"{emoji} {title}"},
                "template": template,
            },
            "elements": elements,
        },
    }

    for idx, (url, secret) in enumerate(chain):
        # Re-build a fresh payload per channel so timestamp + sign reflect the
        # current time and the channel-specific secret.
        channel_payload: Dict[str, Any] = {
            "msg_type": base_payload["msg_type"],
            "card": base_payload["card"],
        }
        ok = _post_with_retry(channel_payload, url, secret)
        if ok:
            if idx > 0:
                try:
                    from hermes_trader import metrics
                    metrics.NOTIFY_FALLBACK_USED.labels(
                        category=category
                    ).inc()
                except Exception:
                    pass
                logger.info(
                    "notify: %s delivered via fallback channel %s "
                    "(primary %s failed)",
                    title, url, primary_url,
                )
            else:
                logger.info("notify: Feishu push OK [%s] %s", category, title)
            return True

    logger.warning(
        "notify: all %d channels failed for %s [%s]",
        len(chain), title, category,
    )
    return False


def _sign(secret: str, timestamp: int) -> str:
    string_to_sign = f"{timestamp}\n{secret}"
    h = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256)
    return base64.b64encode(h.digest()).decode("utf-8")


def _resolve_webhook(category: str) -> Tuple[str, str]:
    """Return (webhook_url, secret) to use for a given category.

    Routing order:
      - "signal" / "ai" → dedicated signal webhook, falling back to primary.
      - "trade"         → primary webhook.
      - everything else → non-trade webhook, falling back to primary.
    """
    cat = category.strip().lower()
    if cat in SIGNAL_CATEGORIES:
        if FEISHU_SIGNAL_WEBHOOK_URL:
            return FEISHU_SIGNAL_WEBHOOK_URL, FEISHU_SIGNAL_WEBHOOK_SECRET
        return FEISHU_WEBHOOK_URL, FEISHU_WEBHOOK_SECRET
    if cat == "trade":
        return FEISHU_WEBHOOK_URL, FEISHU_WEBHOOK_SECRET
    if FEISHU_NON_TRADE_WEBHOOK_URL:
        return FEISHU_NON_TRADE_WEBHOOK_URL, FEISHU_NON_TRADE_WEBHOOK_SECRET
    return FEISHU_WEBHOOK_URL, FEISHU_WEBHOOK_SECRET


def is_enabled(category: str) -> bool:
    """True if a webhook is configured for this category AND it is allow-listed."""
    url, _secret = _resolve_webhook(category)
    return bool(url) and category.strip().lower() in ENABLED_CATEGORIES


def _should_send_throttled(category: str, dedup_key: Optional[str]) -> bool:
    if not dedup_key:
        return True
    now = time.time()
    with _throttle_lock:
        last = _last_sent.get((category, dedup_key), 0.0)
        if (now - last) < _THROTTLE_S:
            return False
        _last_sent[(category, dedup_key)] = now
        return True


def send_card(
    title: str,
    fields: Optional[Dict[str, Any]] = None,
    *,
    category: str = "system",
    level: str = "info",
    markdown: str = "",
    button_text: str = "",
    button_url: str = "",
    dedup_key: Optional[str] = None,
) -> bool:
    """Send a generic interactive card. Returns True on success (never raises).

    R11-A1: dispatch goes through ``_send_with_fallback`` — primary channel
    failures transparently fall through to the other configured webhooks,
    and each channel has its own retry + per-channel circuit breaker.

    fields  - short label/value pairs rendered as a two-column Feishu field grid.
    markdown - optional longer lark_md block rendered below the fields.
    button_* - optional single action button (e.g. "view full report").
    dedup_key - if set, same (category,key) is throttled for 10 min.
    """
    if not is_enabled(category):
        return False
    if not _should_send_throttled(category, dedup_key):
        logger.debug("notify: throttled %s/%s", category, dedup_key)
        return False
    try:
        return _send_with_fallback(
            title=title,
            fields=fields,
            category=category,
            level=level,
            markdown=markdown,
            button_text=button_text,
            button_url=button_url,
        )
    except Exception as e:  # noqa: BLE001 — never let notification break caller
        logger.warning(
            "notify: send_card swallowed unexpected exception: %r", e
        )
        return False


def send_text(text: str, *, category: str = "report") -> bool:
    """Send a plain-text message (used for the daily report digest).

    R11-A1: routed through the same retry + circuit + fallback machinery as
    send_card so a dead primary bot cannot drop the daily report.
    """
    if not is_enabled(category):
        return False
    try:
        chain = _fallback_chain(category)
        if not chain:
            return False
        text = text[:30000]
        for idx, (url, secret) in enumerate(chain):
            payload: Dict[str, Any] = {
                "msg_type": "text",
                "content": {"text": text},
            }
            ok = _post_with_retry(payload, url, secret)
            if ok:
                if idx > 0:
                    try:
                        from hermes_trader import metrics
                        metrics.NOTIFY_FALLBACK_USED.labels(
                            category=category
                        ).inc()
                    except Exception:
                        pass
                logger.info("notify: Feishu text push OK [%s]", category)
                return True
        return False
    except Exception as e:  # noqa: BLE001 — never let notification break caller
        logger.warning(
            "notify: send_text swallowed unexpected exception: %r", e
        )
        return False


def postmortem_url(filename: str) -> str:
    """Build a direct link to the trader-hosted postmortem page (no login).

    经 nginx 反代访问：HERMES_BASE_URL=https://192.168.124.65:8443/trader
    最终链接形如 https://192.168.124.65:8443/trader/postmortems/{name}
    """
    if not HERMES_BASE_URL or not filename:
        return ""
    from urllib.parse import quote
    return f"{HERMES_BASE_URL}/postmortems/{quote(filename)}"


def _fmt_val(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:g}"
    text = str(value)
    return text if text else "—"
