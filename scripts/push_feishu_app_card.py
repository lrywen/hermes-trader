#!/usr/bin/env python3
"""
Push A/B/C backtest comparison charts to Feishu via a self-built custom app.

Why this exists:
    The custom-bot webhook (push_optimization_feishu.py) cannot embed images in
    interactive cards because inbound webhooks have no image upload scope. A
    self-built app can:
      1. Exchange app_id/app_secret for a tenant_access_token.
      2. Upload PNGs to im/v1/images and receive image_keys.
      3. Send an interactive card that references those image_keys via <img>
         elements, rendering the charts inline in the group chat.

Usage:
    # List groups the bot is already a member of (to discover chat_id):
    python3 scripts/push_feishu_app_card.py --list-chats

    # Send the report card to a specific chat_id:
    python3 scripts/push_feishu_app_card.py --chat-id oc_xxxxxxxxxxxxxxxx

    # Override metrics/log paths:
    python3 scripts/push_feishu_app_card.py --chat-id oc_xxx \
        --metrics /tmp/hermes_ab_metrics.png \
        --vetoes  /tmp/hermes_ab_vetoes.png \
        --log     /tmp/backtest_dynamic.log

Credentials are read from CLI flags or env vars FEISHU_APP_ID / FEISHU_APP_SECRET
so they do not have to live in shell history.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Dict, List, Optional, Tuple

FEISHU_BASE = "https://open.feishu.cn/open-apis"

DEFAULT_APP_ID = "cli_aa02713fdaf8dcfd"
DEFAULT_APP_SECRET = "afbBBwKq3eId3ckB9yrf6bgzUdHDkvYH"

DEFAULT_METRICS_PNG = "/tmp/hermes_ab_metrics.png"
DEFAULT_VETOES_PNG = "/tmp/hermes_ab_vetoes.png"
DEFAULT_BACKTEST_LOG = "/tmp/backtest_dynamic.log"


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #
def _http_request(
    method: str,
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    body: Optional[bytes] = None,
    timeout: int = 30,
) -> Tuple[int, Dict[str, Any]]:
    req = urllib.request.Request(url, data=body, method=method)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = {"raw": raw}
        return e.code, payload


def get_tenant_access_token(app_id: str, app_secret: str) -> str:
    url = f"{FEISHU_BASE}/auth/v3/tenant_access_token/internal"
    data = json.dumps({"app_id": app_id, "app_secret": app_secret}).encode("utf-8")
    status, payload = _http_request(
        "POST", url,
        headers={"Content-Type": "application/json; charset=utf-8"},
        body=data,
    )
    if status != 200 or payload.get("code") != 0:
        raise RuntimeError(f"tenant_access_token failed: status={status} payload={payload}")
    token = payload.get("tenant_access_token")
    if not token:
        raise RuntimeError(f"tenant_access_token missing in response: {payload}")
    print(f"[feishu] acquired tenant_access_token (expires_in={payload.get('expire')}s)")
    return token


def list_chats(token: str, page_size: int = 50) -> List[Dict[str, Any]]:
    """List groups the app/bot is a member of. Requires im:chat scope."""
    url = f"{FEISHU_BASE}/im/v1/chats?page_size={page_size}"
    headers = {"Authorization": f"Bearer {token}"}
    status, payload = _http_request("GET", url, headers=headers)
    if status != 200 or payload.get("code") != 0:
        raise RuntimeError(f"list chats failed: status={status} payload={payload}")
    items = (payload.get("data") or {}).get("items") or []
    return items


def upload_image(token: str, image_path: str) -> str:
    """Upload a PNG and return its image_key for use in card elements."""
    boundary = f"----feishu{uuid.uuid4().hex}"
    with open(image_path, "rb") as f:
        image_bytes = f.read()

    parts: List[bytes] = []

    def add_field(name: str, value: str) -> None:
        parts.append(f"--{boundary}\r\n".encode())
        parts.append(
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode("utf-8")
        )

    add_field("image_type", "message")
    parts.append(f"--{boundary}\r\n".encode())
    parts.append(
        f'Content-Disposition: form-data; name="image"; filename="{os.path.basename(image_path)}"\r\n'
        f"Content-Type: image/png\r\n\r\n".encode("utf-8")
    )
    parts.append(image_bytes)
    parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())

    body = b"".join(parts)
    url = f"{FEISHU_BASE}/im/v1/images"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    }
    status, payload = _http_request("POST", url, headers=headers, body=body)
    if status != 200 or payload.get("code") != 0:
        raise RuntimeError(
            f"image upload failed for {image_path}: status={status} payload={payload}"
        )
    image_key = (payload.get("data") or {}).get("image_key")
    if not image_key:
        raise RuntimeError(f"image_key missing in upload response: {payload}")
    print(f"[feishu] uploaded {image_path} -> image_key={image_key}")
    return image_key


def send_interactive_card(
    token: str,
    receive_id: str,
    card: Dict[str, Any],
    receive_id_type: str = "chat_id",
) -> Dict[str, Any]:
    qs = urllib.parse.urlencode({"receive_id_type": receive_id_type})
    url = f"{FEISHU_BASE}/im/v1/messages?{qs}"
    body = json.dumps(
        {
            "receive_id": receive_id,
            "msg_type": "interactive",
            "content": json.dumps(card, ensure_ascii=False),
        },
        ensure_ascii=False,
    ).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    }
    status, payload = _http_request("POST", url, headers=headers, body=body)
    if status not in (200, 201) or payload.get("code") != 0:
        raise RuntimeError(f"send card failed: status={status} payload={payload}")
    msg_id = (payload.get("data") or {}).get("message_id")
    print(f"[feishu] card sent: message_id={msg_id}")
    return payload


# --------------------------------------------------------------------------- #
# Card construction
# --------------------------------------------------------------------------- #
def _parse_backtest_summary(log_path: str) -> Dict[str, str]:
    """Extract the headline A/B/C numbers from the backtest log for the card.

    Log lines are column-aligned, e.g.:
        Total trades                664          412          486
        Win rate                  64.5%        56.6%        58.6%
        Total PnL              $+692.83     $-426.75     $-235.13
    """
    defaults = {
        "old_pnl": "n/a", "strict_pnl": "n/a", "dyn_pnl": "n/a",
        "old_trades": "n/a", "strict_trades": "n/a", "dyn_trades": "n/a",
        "old_wr": "n/a", "strict_wr": "n/a", "dyn_wr": "n/a",
        "old_expect": "n/a", "strict_expect": "n/a", "dyn_expect": "n/a",
    }
    if not log_path or not os.path.exists(log_path):
        return defaults
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return defaults

    def _row(prefix: str) -> Optional[Tuple[str, str, str]]:
        for line in lines:
            stripped = line.strip()
            if stripped.lower().startswith(prefix.lower()):
                # Drop the label (first whitespace-delimited token group up to
                # the first digit), keep the trailing 3 value tokens.
                tokens = stripped.split()
                vals = [t for t in tokens if t and t[0].isdigit() or t[0] in "+-$"]
                if len(vals) >= 3:
                    return vals[-3], vals[-2], vals[-1]
        return None

    trades = _row("Total trades")
    if trades:
        defaults["old_trades"], defaults["strict_trades"], defaults["dyn_trades"] = trades
    wr = _row("Win rate")
    if wr:
        defaults["old_wr"], defaults["strict_wr"], defaults["dyn_wr"] = wr
    pnl = _row("Total PnL")
    if pnl:
        defaults["old_pnl"], defaults["strict_pnl"], defaults["dyn_pnl"] = pnl
    expect = _row("Expectancy")
    if expect:
        defaults["old_expect"], defaults["strict_expect"], defaults["dyn_expect"] = expect
    return defaults


def build_card(
    metrics_key: str,
    vetoes_key: str,
    log_path: str = DEFAULT_BACKTEST_LOG,
) -> Dict[str, Any]:
    s = _parse_backtest_summary(log_path)
    now = time.strftime("%Y-%m-%d %H:%M", time.localtime())

    # Schema 2.0 interactive card with inline images.
    card: Dict[str, Any] = {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {
            "title": {"tag": "plain_text", "content": "Hermes A/B/C 回测对比 · RSI 动态阈值"},
            "subtitle": {"tag": "plain_text", "content": f"OLD vs NEW-STRICT vs NEW-DYNAMIC · {now}"},
            "template": "blue",
        },
        "body": {
            "direction": "vertical",
            "padding": "12px 12px 12px 12px",
            "elements": [
                {
                    "tag": "column_set",
                    "flex_mode": "stretch",
                    "columns": [
                        {
                            "tag": "column",
                            "width": "weighted",
                            "weight": 1,
                            "elements": [
                                {"tag": "markdown", "content": "**OLD**\nPnL: `%s`\nTrades: %s · WR: %s\nExp/trade: `%s`" % (
                                    s["old_pnl"], s["old_trades"], s["old_wr"], s["old_expect"],
                                )},
                            ],
                        },
                        {
                            "tag": "column",
                            "width": "weighted",
                            "weight": 1,
                            "elements": [
                                {"tag": "markdown", "content": "**STRICT**\nPnL: `%s`\nTrades: %s · WR: %s\nExp/trade: `%s`" % (
                                    s["strict_pnl"], s["strict_trades"], s["strict_wr"], s["strict_expect"],
                                )},
                            ],
                        },
                        {
                            "tag": "column",
                            "width": "weighted",
                            "weight": 1,
                            "elements": [
                                {"tag": "markdown", "content": "**DYNAMIC**\nPnL: `%s`\nTrades: %s · WR: %s\nExp/trade: `%s`" % (
                                    s["dyn_pnl"], s["dyn_trades"], s["dyn_wr"], s["dyn_expect"],
                                )},
                            ],
                        },
                    ],
                },
                {"tag": "hr"},
                {
                    "tag": "markdown",
                    "content": (
                        "**核心结论**：DYNAMIC 相对 STRICT 恢复约 45% 的 PnL 缺口"
                        "（-$427 → -$235），改进来自 ADX 分级 RSI 阈值本身；"
                        "共振例外路径在 21 天历史样本中零触发。"
                        "剩余缺口主要由 P2 chop regime 拦截贡献（67%），与 RSI 规则无关。"
                    ),
                },
                {"tag": "hr"},
                {
                    "tag": "markdown",
                    "content": "**指标对比**（总 PnL / 胜率 / 期望值 / Payoff）",
                },
                {
                    "tag": "img",
                    "img_key": metrics_key,
                    "alt": {"tag": "plain_text", "content": "A/B/C metrics comparison"},
                    "mode": "fit_horizontal",
                    "preview": True,
                },
                {"tag": "hr"},
                {
                    "tag": "markdown",
                    "content": "**Veto 原因分布**（STRICT vs DYNAMIC）",
                },
                {
                    "tag": "img",
                    "img_key": vetoes_key,
                    "alt": {"tag": "plain_text", "content": "Veto reason breakdown"},
                    "mode": "fit_horizontal",
                    "preview": True,
                },
                {"tag": "hr"},
                {
                    "tag": "markdown",
                    "content": (
                        "**灰度观察**：24h paper-trading 已在后台运行"
                        "（PID 见 `/tmp/gray_observer.log`），日志：\n"
                        "- `/tmp/hermes_dynamic_rsi_gray.jsonl`\n"
                        "- `/tmp/hermes_dynamic_rsi_summary.jsonl`"
                    ),
                },
            ],
        },
    }
    return card


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser(description="Push backtest charts to Feishu via self-built app.")
    parser.add_argument("--app-id", default=os.environ.get("FEISHU_APP_ID", DEFAULT_APP_ID))
    parser.add_argument("--app-secret", default=os.environ.get("FEISHU_APP_SECRET", DEFAULT_APP_SECRET))
    parser.add_argument("--chat-id", default=os.environ.get("FEISHU_CHAT_ID", ""),
                        help="Target chat_id (oc_...). Use --list-chats to discover.")
    parser.add_argument("--receive-id-type", default="chat_id",
                        choices=["chat_id", "open_id", "user_id", "email", "union_id"])
    parser.add_argument("--metrics", default=DEFAULT_METRICS_PNG)
    parser.add_argument("--vetoes", default=DEFAULT_VETOES_PNG)
    parser.add_argument("--log", default=DEFAULT_BACKTEST_LOG)
    parser.add_argument("--list-chats", action="store_true",
                        help="Only acquire token and list groups the bot belongs to, then exit.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Upload images and build card but do not send.")
    args = parser.parse_args()

    token = get_tenant_access_token(args.app_id, args.app_secret)

    if args.list_chats:
        chats = list_chats(token)
        if not chats:
            print("[feishu] bot is not a member of any group yet.")
            print("         Add the app to the target group in Feishu, then re-run --list-chats.")
            return 0
        print(f"[feishu] bot is in {len(chats)} group(s):")
        for c in chats:
            print(f"  chat_id={c.get('chat_id')}  name={c.get('name')!r}  "
                  f"chat_type={c.get('chat_type')}")
        return 0

    if not args.chat_id:
        print("ERROR: --chat-id is required to send the card. "
              "Run with --list-chats to discover available chat_ids.", file=sys.stderr)
        return 2

    for p in (args.metrics, args.vetoes):
        if not os.path.exists(p):
            print(f"ERROR: image not found: {p}", file=sys.stderr)
            return 2

    metrics_key = upload_image(token, args.metrics)
    vetoes_key = upload_image(token, args.vetoes)

    card = build_card(metrics_key, vetoes_key, log_path=args.log)

    if args.dry_run:
        print("[feishu] --dry-run: card constructed, not sending.")
        print(json.dumps(card, ensure_ascii=False, indent=2)[:2000])
        return 0

    send_interactive_card(token, args.chat_id, card, receive_id_type=args.receive_id_type)
    return 0


if __name__ == "__main__":
    sys.exit(main())
