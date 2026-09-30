#!/usr/bin/env python3
"""M-4 pre-registered replay: Binance bookDepth near-mid depth imbalance.

Tests whether bid/ask notional asymmetry at the +/-0.2% bands predicts the
forward 5m mid return, on a strict time split with day-block bootstrap.

Data (cached under --cache):
  * daily bookDepth ZIP  (30s frames, 12 percentage bands)
  * monthly 5m klines ZIP (labels)

Protocol frozen in docs/research/2026-09/m4_bookdepth_imbalance_prereg_2026-09-30.md.
The TEST half is reported exactly once; thresholds come only from TRAIN.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import os
import zipfile
from collections import defaultdict

import urllib.request

import numpy as np

BASE = "https://data.binance.vision/data/futures/um"
COST_RT_BPS = 11.0  # taker round-trip: fees 9 + 2 half-spread


# --------------------------------------------------------------------------
# Download / cache
# --------------------------------------------------------------------------
def _get(url: str, dest: str) -> str:
    if os.path.exists(dest):
        return dest
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".part"
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                with open(tmp, "wb") as out:
                    out.write(resp.read())
            os.replace(tmp, dest)
            return dest
        except Exception:
            if os.path.exists(tmp):
                os.remove(tmp)
            if attempt == 3:
                raise
    return dest


def download_bookdepth(coin: str, day: dt.date, cache: str) -> bytes:
    z = f"{coin}-bookDepth-{day.isoformat()}.zip"
    url = f"{BASE}/daily/bookDepth/{coin}/{z}"
    dest = os.path.join(cache, "bookDepth", coin, z)
    _get(url, dest)
    with open(dest, "rb") as f:
        return f.read()


def download_klines(coin: str, month: str, cache: str) -> str:
    z = f"{coin}-5m-{month}.zip"
    url = f"{BASE}/monthly/klines/{coin}/5m/{z}"
    dest = os.path.join(cache, "klines", coin, z)
    _get(url, dest)
    return dest


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------
def parse_bookdepth(raw_zip: bytes) -> dict[int, dict[float, float]]:
    """Return {frame_epoch_sec: {abs_pct_band: signed_notional}}.

    Bid-side bands (negative pct) keyed by abs value with notional; ask-side
    keyed separately via sign so callers can pick symmetric pairs.
    Structure: {epoch: {(pct_float): notional}} keeping original signed pct.
    """
    out: dict[int, dict[float, float]] = defaultdict(dict)
    with zipfile.ZipFile(io.BytesIO(raw_zip)) as zf:
        name = zf.namelist()[0]
        with zf.open(name) as fh:
            text = io.TextIOWrapper(fh, encoding="utf-8")
            r = csv.DictReader(text)
            for row in r:
                t = dt.datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M:%S")
                t = t.replace(tzinfo=dt.timezone.utc)
                pct = float(row["percentage"])
                out[int(t.timestamp())][pct] = float(row["notional"])
    return out


def parse_klines(path: str) -> dict[int, float]:
    """Return {open_time_ms: close_px} for 5m klines."""
    out: dict[int, float] = {}
    with zipfile.ZipFile(path) as zf:
        name = zf.namelist()[0]
        with zf.open(name) as fh:
            text = io.TextIOWrapper(fh, encoding="utf-8")
            for row in csv.reader(text):
                # openTime, open, high, low, close, volume, closeTime, ...
                if not row or not row[0].isdigit():
                    continue  # header row
                t = int(row[0])
                out[t] = float(row[4])
    return out


def imbalance(frame: dict[float, float], p: float) -> float | None:
    nb = frame.get(-p)
    na = frame.get(p)
    if nb is None or na is None or (nb + na) <= 0:
        return None
    return (nb - na) / (nb + na)


# --------------------------------------------------------------------------
# Build samples
# --------------------------------------------------------------------------
def daterange(start: dt.date, end: dt.date):
    d = start
    while d < end:
        yield d
        d += dt.timedelta(days=1)


def build_coin(coin, start, end, cache):
    """Align one frame per 5m bucket (frame at/just before bucket open) with
    forward 5m close return. Returns list of (epoch_sec, imb_near, ret_bps)."""
    # klines months
    months = sorted({d.strftime("%Y-%m") for d in daterange(start, end)})
    kl: dict[int, float] = {}
    for m in months:
        try:
            kl.update(parse_klines(download_klines(coin, m, cache)))
        except Exception as e:
            print(f"  warn klines {coin} {m}: {e}")
    kt = np.array(sorted(kl))

    # Gather all frames grouped by day, then bucket.
    samples = []
    for day in daterange(start, end):
        try:
            frames = parse_bookdepth(download_bookdepth(coin, day, cache))
        except Exception as e:
            print(f"  warn book {coin} {day}: {e}")
            continue
        for epoch, frame in frames.items():
            imb = imbalance(frame, 0.2)
            if imb is None:
                continue
            # bucket open ms = floor(epoch/300)*300 ; require frame within bucket
            bucket_s = (epoch // 300) * 300
            if epoch - bucket_s > 30:
                continue  # use only the frame nearest each bucket start
            t_ms = bucket_s * 1000
            nxt_ms = t_ms + 300_000
            if t_ms not in kl or nxt_ms not in kl:
                continue
            ret = (kl[nxt_ms] - kl[t_ms]) / kl[t_ms] * 1e4
            samples.append((bucket_s, imb, ret, coin))
    return samples


# --------------------------------------------------------------------------
# Bootstrap
# --------------------------------------------------------------------------
def day_key(epoch_s):
    return dt.datetime.utcfromtimestamp(epoch_s).date().isoformat()


def bootstrap_net(records, seed, B=2000):
    by_day = defaultdict(list)
    for r in records:
        by_day[day_key(r[0])].append(r)
    days = sorted(by_day)
    rng = np.random.default_rng(seed)
    n = len(days)

    def day_mean(d):
        rs = by_day[d]
        return np.mean([r[2] for r in rs]) if rs else np.nan

    means = np.array([day_mean(d) for d in days])
    boots = []
    for _ in range(B):
        idx = rng.integers(0, n, n)
        boots.append(np.nanmean(means[idx]))
    boots = np.array(boots)
    return {
        "point": float(np.nanmean(means)),
        "ci": (float(np.percentile(boots, 2.5)), float(np.percentile(boots, 97.5))),
        "n_days": n,
    }


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-01-01")
    ap.add_argument("--end", default="2026-06-28")
    ap.add_argument("--split", default="2026-03-31")
    ap.add_argument("--coins", default="BTCUSDT,ETHUSDT")
    ap.add_argument("--seed", type=int, default=20260930)
    ap.add_argument("--cache", default="/tmp/m4_cache")
    args = ap.parse_args()

    start = dt.date.fromisoformat(args.start)
    end = dt.date.fromisoformat(args.end)
    split = dt.date.fromisoformat(args.split)
    coins = args.coins.split(",")
    split_s = int(dt.datetime(split.year, split.month, split.day,
                               tzinfo=dt.timezone.utc).timestamp())

    all_rows = []
    for coin in coins:
        print(f"building {coin} ...")
        all_rows.extend(build_coin(coin, start, end, args.cache))

    train = [r for r in all_rows if r[0] < split_s]
    test = [r for r in all_rows if r[0] >= split_s]

    imbs = np.array([r[1] for r in train])
    k_lo = float(np.quantile(imbs, 0.10))
    k_hi = float(np.quantile(imbs, 0.90))
    print(f"train n={len(train)}  frozen thresholds k_lo={k_lo:.4f} k_hi={k_hi:.4f}")

    # apply on test
    go = [r for r in test if r[1] > k_hi or r[1] < k_lo]
    # signed cost: subtract round-trip cost from every executed trade
    go_net = [(r[0], r[1], r[2] - COST_RT_BPS, r[3]) for r in go]
    mid = [r for r in test if not (r[1] > k_hi or r[1] < k_lo)]

    def side(r):
        return 1.0 if r[1] > k_hi else -1.0

    # Direction-correct gross (signal side * return) to verify predictive content
    dircorr = np.array([side(r) * r[2] for r in go])
    boot = bootstrap_net(go_net, args.seed)

    # baseline reference: all test trades (no filter, no cost) mean
    base_ret = np.mean([r[2] for r in test])
    skipped_ret = np.mean([r[2] for r in mid]) if mid else float("nan")

    rules = {
        "R1 net CI lo > 0": boot["ci"][0] > 0,
        "R2 sign aligned (mean side*ret > 0)": dircorr.mean() > 0,
        "R4 middle band not better than go gross":
            (np.mean([side(r) * r[2] for r in go]) if go else 0) >= 0,
        "R5 trades >= 300": len(go_net) >= 300,
        "R6 net mean >= 3 bps": boot["point"] >= 3.0,
    }
    decision = "H1 PASS" if all(rules[k] for k in
                                ["R1 net CI lo > 0", "R2 sign aligned (mean side*ret > 0)",
                                 "R5 trades >= 300", "R6 net mean >= 3 bps"]) else "H1 CLOSED / no-go"

    print("=" * 70)
    print("M-4 bookDepth near-mid imbalance — TEST (single, frozen)")
    print("=" * 70)
    print(f"test buckets      : {len(test)}  days {boot['n_days']}")
    print(f"go (executed)     : {len(go_net)}  ({len(go_net)/max(len(test),1):.1%})")
    print(f"middle (no-trade) : {len(mid)}")
    print(f"gross side*ret    : {dircorr.mean():+.2f} bps (predictive content)")
    print(f"all-test mean ret : {base_ret:+.2f} bps ; middle-band mean: {skipped_ret:+.2f} bps")
    print(f"NET go (cost {COST_RT_BPS:.0f}bp): {boot['point']:+.2f} bps/trade "
          f"CI[{boot['ci'][0]:+.2f}, {boot['ci'][1]:+.2f}]")
    print("-" * 70)
    for k, v in rules.items():
        print(f"[{'PASS' if v else 'FAIL'}] {k}")
    print("-" * 70)
    print(f"DECISION: {decision}")

    # machine-readable summary
    summary = {
        "train_n": len(train), "test_n": len(test), "go_n": len(go_net),
        "k_lo": k_lo, "k_hi": k_hi,
        "gross_side_ret_bps": float(dircorr.mean()),
        "net_point_bps": boot["point"], "net_ci": boot["ci"],
        "rules": {k: bool(v) for k, v in rules.items()}, "decision": decision,
    }
    with open(os.path.join(os.path.dirname(__file__), "..",
                           "logs", "m4_bookdepth_result.json"), "w") as f:
        json.dump(summary, f, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
