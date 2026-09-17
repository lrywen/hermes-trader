"""P0 风险调优三臂 shadow 采数面板（Audit 2026-09-12 主流币漏抓修复，M-呈现）。

把「主流币漏抓」P0 落地的三个 shadow 灰度臂的原始采数记录以只读聚合
暴露给 portal 独立面板：

  * GET /api/dashboard/risk-tuning/arms — 三臂模式 + 命中计数 + 每日分桶
    + 近 N 条原始记录（含 reconcile 回填的 outcome/pnl）

Posture（INERT 红线）：纯只读报表面。不写配置、不翻臂模式、不触碰交易
热路径；三臂的 shadow→enforce 翻牌永远走 config_store 权威写路径。

三臂（全部默认 shadow，零实盘行为变更）：
  * #4 sigma_burst_gate           — perception：σ 爆发币 composite∈[45,54)
                                    降档放行研究，记录 would-surface。
                                    落盘 risk_tuning_shadow.jsonl（rule 过滤）。
  * #8 research_cooldown_adaptive — trading_loop：热 σ 爆发币研究冷却
                                    10min→2min，记录 would re-research。
                                    同文件（rule 过滤）。
  * #7 breakout_exemption         — ta_filter：真突破（breakout fired +
                                    RVOL≥4）撞 late-entry 否决时降级放行，
                                    记录 would-downgrade。落盘
                                    ta_late_entry_shadow.jsonl
                                    （layer=prefilter_breakout_exemption 过滤），
                                    reconcile 每日回填 outcome/pnl_pct。

Implementation notes:
  * 两个 JSONL 均为小文件（千行级），整读 + 内存过滤即可；60s TTL 缓存
    （dashboard._ttl_cached singleflight），asyncio.to_thread 不阻塞事件循环。
  * 臂模式解析镜像运行时双布尔语义：enabled=False→off；enabled+shadow_mode
    →shadow；enabled+!shadow_mode→enforce。配置经 config_store.read_agent_config
    读取（含 canonical 默认），读失败降级为 {}（模式显示 unknown，不 500）。
  * 读端 anonymous-safe（只有计数与交易公开字段，无密钥），与 shadow-arms
    grades 读姿态一致；portal BFF 侧再做 RBAC 收紧。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

from hermes_trader.dashboard import _ttl_cached

logger = logging.getLogger("hermes-dashboard")

_TTL_S = 60.0
_CACHE_KEY = "risk_tuning_arms"

# 臂注册表：id / 标题 / 说明 / 落盘文件 env / 默认路径 / 记录过滤（键, 值）/
# 配置块路径（config_store 块名链）。
_ARMS: tuple[dict, ...] = (
    {
        "id": "sigma_burst_gate",
        "title": "#4 σ 爆发降档",
        "desc": "σ 爆发币 composite∈[45,54) 降档放行研究；记录 would-surface",
        "env": "HERMES_RISK_TUNING_SHADOW_FILE",
        "default_path": "/data/risk_tuning_shadow.jsonl",
        "match": ("rule", "sigma_burst_gate"),
        "cfg_path": ("sigma_burst_gate",),
    },
    {
        "id": "research_cooldown_adaptive",
        "title": "#8 研究冷却自适应",
        "desc": "热 σ 爆发币研究冷却 10min→2min；记录 would re-research",
        "env": "HERMES_RISK_TUNING_SHADOW_FILE",
        "default_path": "/data/risk_tuning_shadow.jsonl",
        "match": ("rule", "research_cooldown_adaptive"),
        "cfg_path": ("research_cooldown_adaptive",),
    },
    {
        "id": "breakout_exemption",
        "title": "#7 真突破豁免",
        "desc": "真突破（breakout fired + RVOL≥4）撞 late-entry 否决降级放行；记录 would-downgrade",
        "env": "HERMES_TA_LATE_ENTRY_SHADOW_FILE",
        "default_path": "/data/ta_late_entry_shadow.jsonl",
        "match": ("layer", "prefilter_breakout_exemption"),
        "cfg_path": ("ta_late_entry", "breakout_exemption"),
    },
)


def _arm_file(spec: dict) -> str:
    """路径解析与运行时一致：env 覆盖 > 默认 /data 路径。"""
    p = os.environ.get(spec["env"], "").strip()
    return os.path.expanduser(p) if p else spec["default_path"]


def _arm_mode(cfg: dict, spec: dict) -> str:
    """镜像运行时双布尔解析：enabled 假→off；enabled+shadow→shadow；
    enabled+!shadow→enforce。配置缺失时按 canonical 默认（enabled=True,
    shadow_mode=True）报 shadow——与 config_store 注册的默认一致。"""
    blk: dict = cfg
    for key in spec["cfg_path"]:
        blk = blk.get(key) if isinstance(blk, dict) else {}
        if not isinstance(blk, dict):
            blk = {}
            break
    if not blk:
        return "shadow"  # canonical 默认：enabled=True, shadow_mode=True
    if not bool(blk.get("enabled", True)):
        return "off"
    return "shadow" if bool(blk.get("shadow_mode", True)) else "enforce"


def _parse_ts(raw) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _pnl_stats(values) -> dict | None:
    xs = sorted(v for v in values if isinstance(v, (int, float)))
    if not xs:
        return None
    n = len(xs)
    median = xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2
    return {
        "n": n,
        "win_rate": round(sum(1 for v in xs if v > 0) / n, 4),
        "avg_pct": round(sum(xs) / n, 4),
        "median_pct": round(median, 4),
        "min_pct": round(xs[0], 4),
        "max_pct": round(xs[-1], 4),
    }


def _is_enforced(rec: dict) -> bool:
    """enforced 标志位置两臂不一：breakout_exemption 在顶层；
    sigma_burst_gate / research_cooldown_adaptive 写在 detail 内层。"""
    if rec.get("enforced") is True:
        return True
    d = rec.get("detail")
    return isinstance(d, dict) and d.get("enforced") is True


def _read_jsonl(path: str) -> list[dict]:
    rows: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if isinstance(rec, dict):
                    rows.append(rec)
    except OSError:
        pass
    return rows


def _summarize(spec: dict, cfg: dict, *, recent_n: int, days: int) -> dict:
    path = _arm_file(spec)
    match_key, match_val = spec["match"]
    rows = [r for r in _read_jsonl(path) if r.get(match_key) == match_val]
    now = time.time()
    since_7d = now - 7 * 86400.0
    daily: dict[str, int] = {}
    last_7d = 0
    for r in rows:
        dt = _parse_ts(r.get("timestamp"))
        if dt is None:
            continue
        if dt.timestamp() >= since_7d:
            last_7d += 1
        day = dt.date().isoformat()
        daily[day] = daily.get(day, 0) + 1
    day_list = sorted(daily)[-days:]
    outcomes = {"win": 0, "loss": 0, "open": 0, "pending": 0}
    for r in rows:
        oc = r.get("outcome")
        if oc in ("win", "loss", "open"):
            outcomes[oc] += 1
        else:
            outcomes["pending"] += 1
    return {
        "id": spec["id"],
        "title": spec["title"],
        "desc": spec["desc"],
        "mode": _arm_mode(cfg, spec),
        "path": path,
        "file_exists": os.path.exists(path),
        "total": len(rows),
        "enforced": sum(1 for r in rows if _is_enforced(r)),
        "last_7d": last_7d,
        "daily": [{"date": d, "count": daily[d]} for d in day_list],
        "outcomes": outcomes,
        "pnl": _pnl_stats([r.get("pnl_pct") for r in rows]),
        "recent": list(reversed(rows[-recent_n:])),
    }


def _load_cfg() -> dict:
    try:
        from hermes_trader.agents.config_store import read_agent_config
        cfg = read_agent_config()
        return cfg if isinstance(cfg, dict) else {}
    except Exception as e:  # 配置读失败不 500：模式降级为 canonical 默认
        logger.warning("risk-tuning arms: agent config unreadable (%s)", e)
        return {}


def _payload(recent_n: int, days: int) -> dict:
    cfg = _load_cfg()
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cache_ttl_s": _TTL_S,
        "arms": [_summarize(spec, cfg, recent_n=recent_n, days=days) for spec in _ARMS],
    }


def register_risk_tuning_routes(app: FastAPI) -> None:
    """Mount the P0 risk-tuning shadow-arm read routes (before public SPA)."""

    @app.get("/api/dashboard/risk-tuning/arms")
    async def risk_tuning_arms(
        recent: int = Query(50, ge=1, le=200),
        days: int = Query(30, ge=1, le=90),
    ) -> JSONResponse:
        """三臂模式 + 命中计数 + 每日分桶 + 近 N 条原始记录。60s TTL；
        anonymous-safe（计数/公开交易字段），portal BFF 再做 RBAC。"""
        payload = await asyncio.to_thread(
            _ttl_cached, _CACHE_KEY, _TTL_S, lambda: _payload(recent, days),
        )
        return JSONResponse(payload)
