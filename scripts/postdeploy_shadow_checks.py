#!/usr/bin/env python3
"""部署后统一健康核验：panic gate 影子落盘 + maker v2 账本（只读）。

在容器内（cd /app）部署并跑过若干扫描周期后执行。两类检查各自独立、只读、
不下任何单；整体退出码用于 CI/cron：

  0  OK          —— 该查的都正常；对"尚在预热（数据不足）"给 WARN 而非失败
  2  FAIL        —— 发现真实接线缺陷（典型：v2 开了却零成交空账本 / panic gate
                    配置声称开启却没有任何记录字段）
  3  MISSING     —— 关键文件/配置缺失（无法判断）

为什么需要它：
  * maker v2 曾有"确定性 p>=1 才成交、base=0.30 时永不成交"的启用即坏缺陷，
    会静默产生零成交空账本。这里直接读账本 adverse_selection.v2，校验
    zero_fill_warning 与成交率带。
  * panic gate 是 record-only（绝不改 is_candidate/不接下单），部署后要确认它
    真的在每个触发上落了 panic_gate_enabled/pass/reasons/atr_pctile 字段，且
    开启时不会出现"全部 enabled=True 却字段缺失"的接线断裂。

路径默认按容器布局（可被参数/环境变量覆盖）：
  shadow book : $HERMES_SHADOW_BOOK_FILE 或 /data/.shadow-book.json
  xs shadow   : $HERMES_XS_REVERSAL_SHADOW_FILE 或 /data/xs_reversal_shadow.jsonl
  xs state    : $HERMES_XS_REVERSAL_STATE_FILE 或 /data/.xs-reversal.state

用法：
  python scripts/postdeploy_shadow_checks.py                 # 自动判定 v2 是否开启
  python scripts/postdeploy_shadow_checks.py --min-v2-decided 20 --min-xs-rows 1
  python scripts/postdeploy_shadow_checks.py --json
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import time
from typing import Any, Optional

DEFAULT_BOOK = os.environ.get(
    "HERMES_SHADOW_BOOK_FILE", "/data/.shadow-book.json")
DEFAULT_XS = os.environ.get(
    "HERMES_XS_REVERSAL_SHADOW_FILE", "/data/xs_reversal_shadow.jsonl")
DEFAULT_XS_STATE = os.environ.get(
    "HERMES_XS_REVERSAL_STATE_FILE", "/data/.xs-reversal.state")

EXIT_OK, EXIT_FAIL, EXIT_MISSING = 0, 2, 3

# v2 校准带（1m 真实回放 ~9% 成交；长期确定性期望≈base 0.30）。低于下界且样本
# 足够即视为接线退化；这是"明显坏掉"的护栏，不是 edge 判据。
V2_FILL_RATE_MIN_PCT = 1.0
V2_WARN_RESIDUAL_BPS = 20.0   # kappa=1 时残差漂移应≈0；超过仅告警


def _read_json(path: str) -> Optional[Any]:
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def check_maker_v2(book_path: str, expect_v2: Optional[bool],
                   min_decided: int) -> dict[str, Any]:
    """读取 maker_shadow 账本的 adverse_selection.v2 块并核验。"""
    res: dict[str, Any] = {"check": "maker_v2", "path": book_path}
    book = _read_json(book_path)
    if book is None:
        res.update(status="MISSING",
                   detail="shadow book not present/unreadable (pre-warm?)")
        res["exit"] = EXIT_MISSING
        return res

    # 直接复用生产类，避免重复实现统计口径。
    try:
        from hermes_trader.agents.shadow_book import ShadowBook
        stats = ShadowBook(book_path).get_stats()
    except Exception as e:  # 账本结构异常也算接线问题
        res.update(status="FAIL", detail=f"failed to read book stats: {e}")
        res["exit"] = EXIT_FAIL
        return res

    maker = (stats or {}).get("maker_shadow", {}) or {}
    adv = maker.get("adverse_selection", {}) or {}
    v2 = adv.get("v2")
    # 账本统计不回写 maker_fill_model 配置串；v2 块仅在出现 fill_model=
    # maker_shadow_v2 的成交/撤单后才生成。auto 下"见到 v2 块=已激活"；
    # --expect-v2 on/off 可显式强制（on 且无块即接线断裂）。
    v2_enabled = (bool(v2) if expect_v2 is None else expect_v2)
    res["v2_enabled"] = bool(v2_enabled)

    if not v2_enabled:
        res.update(status="OK", detail="maker_fill_model != v2 (v1/default); "
                                       "no v2 ledger expected. Nothing to check.")
        res["exit"] = EXIT_OK
        return res
    if not v2:
        # 明确开了 v2，却完全没有 v2 统计块 → 账本没走 v2 路径（接线断裂）。
        res.update(status="FAIL",
                   detail="maker_fill_model=v2 but no adverse_selection.v2 block")
        res["exit"] = EXIT_FAIL
        return res

    decided = int(v2.get("fills", 0)) + int(v2.get("cancels", 0))
    res["v2"] = {k: v2.get(k) for k in (
        "fills", "cancels", "touched_cancels", "fill_rate_pct",
        "zero_fill_warning", "avg_fill_prob", "avg_residual_drift_bps",
        "avg_reject_count", "fills_after_reject_pct")}
    res["decided"] = decided

    if decided < min_decided:
        res.update(status="WARN",
                   detail=f"v2 wired but only {decided} decided orders "
                          f"(<{min_decided}); re-check after warm-up")
        res["exit"] = EXIT_OK  # 预热不足不算失败
        return res
    if v2.get("zero_fill_warning"):
        res.update(status="FAIL",
                   detail=f"v2 zero-fill: {v2.get('touched_cancels')} touched "
                          "cancels but 0 fills — probability wiring regressed")
        res["exit"] = EXIT_FAIL
        return res
    rate = v2.get("fill_rate_pct")
    if rate is not None and rate < V2_FILL_RATE_MIN_PCT:
        res.update(status="FAIL",
                   detail=f"v2 fill_rate {rate}% < {V2_FILL_RATE_MIN_PCT}% "
                          "after adequate sample — near-silent empty ledger")
        res["exit"] = EXIT_FAIL
        return res
    warns = []
    rd = v2.get("avg_residual_drift_bps")
    if isinstance(rd, (int, float)) and abs(rd) > V2_WARN_RESIDUAL_BPS:
        warns.append(f"residual_drift {rd}bp larger than expected (~0 at kappa=1)")
    res.update(status="OK", warnings=warns,
               detail=f"v2 active: {v2.get('fills')} fills / {decided} decided "
                      f"({rate}%)")
    res["exit"] = EXIT_OK
    return res


def check_panic_gate(xs_path: str, state_path: str,
                     min_rows: int, staleness_s: int,
                     deployed_after_ms: int = 0) -> dict[str, Any]:
    """核验 xs panic gate 影子字段落盘与心跳活性。只读。

    deployed_after_ms：部署/容器重建时刻（epoch ms，可留几秒 grace）。早于它
    的 jsonl 行是上一个镜像写的历史记录，不含 panic_gate_* 属正常，必须排除
    在"接线断裂"判据之外——否则 append-only 文件在每次刚部署、新代码尚未产生
    触发时都会被旧行误判为 FAIL。
    """
    res: dict[str, Any] = {"check": "panic_gate", "path": xs_path}
    if not os.path.exists(xs_path):
        res.update(status="MISSING",
                   detail="xs_reversal_shadow.jsonl not present (arm only writes "
                          "on a real trigger; check heartbeat for liveness)")
        # 没有触发记录不一定是坏，先看心跳；心跳也没有才算缺失。
        hb = _read_json(state_path)
        if not hb:
            res["exit"] = EXIT_MISSING
            return res
        res["heartbeat_fallback"] = _heartbeat_summary(hb, staleness_s)
        res["exit"] = EXIT_OK if res["heartbeat_fallback"]["alive"] else EXIT_FAIL
        res["status"] = "OK" if res["heartbeat_fallback"]["alive"] else "FAIL"
        return res

    total = 0
    legacy = 0          # 部署前旧镜像写的行（无 panic 字段但时间早于 cutoff）
    enabled_rows = 0
    field_missing = 0   # 仅统计部署后、应由新代码写出却缺字段的行
    pass_n = 0
    cond_pass_n = 0     # 纯条件判定通过的行（与开关无关，影子证据累积量）
    reasons: dict[str, int] = {}
    have_pctile = 0
    last_ts = 0
    required = ("panic_gate_enabled", "panic_gate_active",
                "panic_gate_pass", "panic_gate_reasons")
    with open(xs_path) as fh:
        for line in fh:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            total += 1
            ts = int(r.get("timestamp") or 0)
            last_ts = max(last_ts, ts)
            has_fields = all(k in r for k in required)
            if not has_fields and deployed_after_ms and ts < deployed_after_ms:
                legacy += 1
                continue
            if not has_fields:
                field_missing += 1
                continue
            # panic_gate_pass is now the PURE conditional verdict (computed even
            # while the switch is off), so it accrues evidence regardless of
            # enabled state; gate-active rows additionally require the switch.
            if r.get("panic_gate_pass"):
                cond_pass_n += 1
            if r.get("panic_gate_enabled"):
                enabled_rows += 1
                if r.get("panic_gate_pass"):
                    pass_n += 1
                if r.get("atr_pctile") is not None:
                    have_pctile += 1
                for why in (r.get("panic_gate_reasons") or []):
                    reasons[why] = reasons.get(why, 0) + 1

    # 部署后由新代码实际写出的行数（含字段行 + 缺字段行；旧行不计入）。
    new_rows = total - legacy
    res.update(rows=total, new_rows=new_rows, legacy_rows=legacy,
               enabled_rows=enabled_rows, gate_pass=pass_n,
               conditional_pass=cond_pass_n,
               rows_with_atr_pctile=have_pctile,
               new_rows_missing_panic_fields=field_missing,
               block_reasons=reasons,
               last_trigger_age_s=(int(time.time()) - last_ts // 1000)
               if last_ts else None)

    # 真正的接线断裂：部署后新代码写出的行里出现缺字段。
    if field_missing > 0:
        res.update(status="FAIL",
                   detail=f"{field_missing} post-deploy rows missing panic_gate_* "
                          "fields — running image predates the record-only gate")
        res["exit"] = EXIT_FAIL
        return res
    # 部署后尚无任何新触发行：无法从 jsonl 判断，回退心跳，给 WARN 而非 FAIL。
    if new_rows == 0:
        hb = _read_json(state_path)
        hb_sum = _heartbeat_summary(hb, staleness_s) if hb else None
        res["heartbeat_fallback"] = hb_sum
        if hb_sum and hb_sum["alive"]:
            res.update(status="WARN",
                       detail=f"all {total} rows predate deploy (legacy, no panic "
                              "fields); no post-deploy trigger yet but heartbeat alive")
            res["exit"] = EXIT_OK
        else:
            res.update(status="FAIL",
                       detail="no post-deploy trigger rows and heartbeat stale/"
                              "missing — arm may not be running the new image")
            res["exit"] = EXIT_FAIL
        return res
    if new_rows < min_rows:
        res.update(status="WARN",
                   detail=f"only {new_rows} post-deploy trigger rows (<{min_rows}); "
                          "gate field schema present, awaiting samples")
        res["exit"] = EXIT_OK
        return res
    # 字段在、且开启行的 atr_pctile 覆盖率健康即可；pass 少属正常（门本来就严）。
    if enabled_rows and have_pctile < enabled_rows:
        res.update(status="WARN",
                   detail=f"{enabled_rows} enabled rows but only {have_pctile} "
                          "carry atr_pctile (vol leg may be misconfigured)")
        res["exit"] = EXIT_OK
        return res
    res.update(status="OK",
               detail=f"panic gate schema healthy: {enabled_rows}/{new_rows} "
                      f"post-deploy rows enabled, {pass_n} active-passed, "
                      f"{cond_pass_n}/{new_rows} conditionally pass (shadow "
                      f"evidence accrues even while switch is off)")
    res["exit"] = EXIT_OK
    return res


def _heartbeat_summary(hb: dict[str, Any], staleness_s: int) -> dict[str, Any]:
    ts = float(hb.get("ts") or 0)
    age = (time.time() - ts) if ts else None
    alive = bool(age is not None and age <= staleness_s and hb.get("data_ok", True))
    return {"mode": hb.get("mode"), "coin": hb.get("coin"),
            "data_ok": hb.get("data_ok"), "triggered": hb.get("triggered"),
            "age_s": round(age, 0) if age is not None else None,
            "alive": alive, "error": hb.get("error")}


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--book", default=DEFAULT_BOOK)
    ap.add_argument("--xs", default=DEFAULT_XS)
    ap.add_argument("--xs-state", default=DEFAULT_XS_STATE)
    ap.add_argument("--expect-v2", choices=["on", "off", "auto"], default="auto",
                    help="on/off 强制，auto 读账本 maker_fill_model")
    ap.add_argument("--min-v2-decided", type=int, default=20)
    ap.add_argument("--min-xs-rows", type=int, default=1)
    ap.add_argument("--heartbeat-staleness-s", type=int, default=6 * 3600)
    ap.add_argument("--deployed-after-ms", type=int, default=0,
                    help="epoch ms 部署时刻；早于它的 jsonl 行视为旧镜像 legacy，"
                         "不计入 panic 字段缺失判据（默认 0=不区分）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    expect = {"on": True, "off": False, "auto": None}[args.expect_v2]
    checks = [
        check_maker_v2(args.book, expect, args.min_v2_decided),
        check_panic_gate(args.xs, args.xs_state, args.min_xs_rows,
                         args.heartbeat_staleness_s,
                         deployed_after_ms=args.deployed_after_ms),
    ]
    # FAIL 优先于 MISSING 优先于 OK（WARN 用 OK 退出码）。
    worst = EXIT_OK
    if any(c["exit"] == EXIT_FAIL for c in checks):
        worst = EXIT_FAIL
    elif any(c["exit"] == EXIT_MISSING for c in checks):
        worst = EXIT_MISSING
    payload = {
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
        "overall": {0: "OK", 2: "FAIL", 3: "MISSING"}[worst],
        "checks": checks,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        print(f"post-deploy shadow checks: {payload['overall']}")
        for c in checks:
            extra = c.get("detail", "")
            warn = f" warnings={c.get('warnings')}" if c.get("warnings") else ""
            print(f"  [{c['status']:7}] {c['check']:11} {extra}{warn}")
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
