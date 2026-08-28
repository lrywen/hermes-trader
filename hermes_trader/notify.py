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

    webhook_url, webhook_secret = _resolve_webhook(category)
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

    payload: Dict[str, Any] = {
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
    if webhook_secret:
        ts = int(time.time())
        payload["timestamp"] = str(ts)
        payload["sign"] = _sign(webhook_secret, ts)

    try:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            webhook_url,
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
            logger.info("notify: Feishu push OK [%s] %s", category, title)
        else:
            logger.warning("notify: Feishu push rejected: %s", data)
        return ok
    except Exception as e:  # never let notification break the caller
        logger.warning("notify: Feishu push failed [%s] %s: %r",
                       category, title, e)
        return False


def send_text(text: str, *, category: str = "report") -> bool:
    """Send a plain-text message (used for the daily report digest)."""
    if not is_enabled(category):
        return False
    webhook_url, webhook_secret = _resolve_webhook(category)
    payload: Dict[str, Any] = {
        "msg_type": "text",
        "content": {"text": text[:30000]},
    }
    if webhook_secret:
        ts = int(time.time())
        payload["timestamp"] = str(ts)
        payload["sign"] = _sign(webhook_secret, ts)
    try:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            webhook_url,
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
            resp.read()
        logger.info("notify: Feishu text push OK [%s]", category)
        return True
    except Exception as e:
        logger.warning("notify: Feishu text push failed [%s]: %r", category, e)
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
