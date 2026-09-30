#!/usr/bin/env python3
"""M-3 pre-registered replay: meta-labeling go/no-go filter over the `filt` arm.

Zero third-party ML deps (numpy only). Implements:
  * z-score standardisation fit on TRAIN only;
  * logistic regression (L2, full-batch GD) and a depth-limited CART;
  * model choice by 5-fold TimeSeriesSplit mean ROC-AUC on TRAIN;
  * threshold tau chosen once on TRAIN (max F1), frozen for TEST;
  * day-block bootstrap (2000) for the lift Delta and go-net CI.

All choices are frozen in docs/research/2026-09/m3_metalabel_filter_prereg_2026-09-30.md.
This script prints the TEST report; it never tunes against TEST.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from collections import defaultdict

import numpy as np

FEATS = ["score", "n_fired", "adx4h", "rsi4h", "extension"]


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------
def load_trades(path: str, arm: str) -> list[dict]:
    rows = []
    with open(path) as f:
        for line in f:
            o = json.loads(line)
            if o.get("type") != "trade" or o.get("arm") != arm:
                continue
            meta = o.get("meta") or {}
            rows.append({
                "t": o["entry_t"],
                "coin": o["coin"],
                "net": float(o["pnl_net"]),
                "y": 1.0 if float(o["pnl_net"]) > 0 else 0.0,
                "x": np.array([
                    float(o.get("score", 0.0)),
                    float(len(o.get("fired") or [])),
                    float(meta.get("adx4h", np.nan)),
                    float(meta.get("rsi4h", np.nan)),
                    float(meta.get("extension", np.nan)),
                ], dtype=np.float64),
            })
    rows.sort(key=lambda r: r["t"])
    return rows


def day_of(t_ms: int) -> str:
    return dt.datetime.utcfromtimestamp(t_ms / 1000).date().isoformat()


# --------------------------------------------------------------------------
# Models (numpy)
# --------------------------------------------------------------------------
def _sigmoid(z):
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


class Logistic:
    def __init__(self, C: float = 1.0, lr: float = 0.1, epochs: int = 4000):
        self.C = C
        self.lr = lr
        self.epochs = epochs
        self.w = None
        self.b = 0.0

    def fit(self, X, y):
        n, d = X.shape
        self.w = np.zeros(d)
        self.b = 0.0
        for _ in range(self.epochs):
            p = _sigmoid(X @ self.w + self.b)
            err = p - y
            gw = X.T @ err / n + self.w / self.C
            gb = err.mean()
            self.w -= self.lr * gw
            self.b -= self.lr * gb
        return self

    def predict_proba(self, X):
        return _sigmoid(X @ self.w + self.b)


class ShallowTree:
    """CART binary classifier, hard depth cap and min leaf size."""

    def __init__(self, max_depth: int = 3, min_samples_leaf: int = 200, n_thr: int = 12):
        self.max_depth = max_depth
        self.min_samples_leaf = min_samples_leaf
        self.n_thr = n_thr
        self.root = None

    @staticmethod
    def _gini(p):
        return 2 * p * (1 - p)

    def fit(self, X, y):
        self.root = self._build(X, y, 0)
        return self

    def _build(self, X, y, depth):
        node = {"leaf": True, "p": float(y.mean()) if len(y) else 0.5}
        n = len(y)
        if depth >= self.max_depth or n < 2 * self.min_samples_leaf:
            return node
        base = self._gini(y.mean()) * n
        best = None
        for j in range(X.shape[1]):
            col = X[:, j]
            qs = np.unique(np.quantile(col, np.linspace(0.1, 0.9, self.n_thr)))
            for thr in qs:
                m = col <= thr
                nl, nr = m.sum(), (~m).sum()
                if nl < self.min_samples_leaf or nr < self.min_samples_leaf:
                    continue
                pl, pr = y[m].mean(), y[~m].mean()
                gain = base - (self._gini(pl) * nl + self._gini(pr) * nr)
                if gain > 0 and (best is None or gain > best[0]):
                    best = (gain, j, float(thr))
        if best is None:
            return node
        _, j, thr = best
        m = X[:, j] <= thr
        node = {"leaf": False, "j": j, "thr": thr,
                "L": self._build(X[m], y[m], depth + 1),
                "R": self._build(X[~m], y[~m], depth + 1)}
        return node

    def _pred_one(self, node, x):
        while not node["leaf"]:
            node = node["L"] if x[node["j"]] <= node["thr"] else node["R"]
        return node["p"]

    def predict_proba(self, X):
        return np.array([self._pred_one(self.root, x) for x in X])


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------
def roc_auc(y, p) -> float:
    pos = p[y == 1]
    neg = p[y == 0]
    if not len(pos) or not len(neg):
        return 0.5
    order = np.argsort(np.concatenate([pos, neg]))
    ranks = np.empty(len(order), dtype=np.float64)
    vals = np.concatenate([pos, neg])[order]
    r = 1
    for i in range(len(vals)):
        if i and vals[i] != vals[i - 1]:
            r = i + 1
        ranks[order[i]] = r
    rpos = ranks[: len(pos)].sum()
    return float((rpos - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg)))


def f1_at(y, p, tau) -> float:
    pred = p >= tau
    tp = float((pred & (y == 1)).sum())
    if tp == 0:
        return 0.0
    prec = tp / max(pred.sum(), 1)
    rec = tp / max((y == 1).sum(), 1)
    return 2 * prec * rec / (prec + rec)


def standardize_fit(X):
    mu = X.mean(axis=0)
    sd = X.std(axis=0)
    sd[sd < 1e-9] = 1.0
    return mu, sd


def ts_cv_auc(model_factory, X, y, k: int = 5) -> float:
    n = len(y)
    folds = np.linspace(n // (k + 1), n - n // (k + 1), k).astype(int)
    aucs = []
    prev = 0
    for end in folds:
        tr = slice(prev, end)
        va = slice(end, end + (n - end) // 2 if end + (n - end) // 2 <= n else n)
        m = model_factory()
        m.fit(X[tr], y[tr])
        aucs.append(roc_auc(y[va], m.predict_proba(X[va])))
        prev = end
    return float(np.mean(aucs))


# --------------------------------------------------------------------------
# Bootstrap
# --------------------------------------------------------------------------
def day_block_stats(rows, go_mask):
    """Per-day: mean go net bps, mean all net bps, counts. Returns arrays."""
    days = defaultdict(lambda: {"go": [], "all": [], "skip": []})
    for r, g in zip(rows, go_mask):
        d = day_of(r["t"])
        bps = r["net"] / 10000.0 * 1e4  # notional=10000 -> $ /10000 -> bps
        days[d]["all"].append(bps)
        (days[d]["go"] if g else days[d]["skip"]).append(bps)
    keys = sorted(days)
    go_m, all_m = [], []
    for k in keys:
        go_m.append(np.mean(days[k]["go"]) if days[k]["go"] else np.nan)
        all_m.append(np.mean(days[k]["all"]))
    return np.array(keys), np.array(go_m), np.array(all_m)


def bootstrap(rows, go_mask, seed: int, B: int = 2000):
    keys, go_m, all_m = day_block_stats(rows, go_mask)
    rng = np.random.default_rng(seed)
    n = len(keys)
    # Go-only mean uses days that have go trades; weight by day, treating missing
    # go days as that day contributing 0 go trades (exclude from go mean via nanmean).
    delta_boot, go_boot = [], []
    valid = ~np.isnan(go_m)
    for _ in range(B):
        idx = rng.integers(0, n, n)
        delta_boot.append(np.nanmean(go_m[idx]) - np.mean(all_m[idx]))
        go_boot.append(np.nanmean(go_m[idx][valid[idx]]))
    delta_boot = np.array(delta_boot)
    go_boot = np.array(go_boot)

    def ci(a):
        return np.percentile(a, 2.5), np.percentile(a, 97.5)

    return {
        "delta_point": float(np.nanmean(go_m) - np.mean(all_m)),
        "delta_ci": ci(delta_boot),
        "go_point": float(np.nanmean(go_m[valid])),
        "go_ci": ci(go_boot),
        "n_days": n,
        "days_with_go": int(valid.sum()),
    }


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", default="logs/b3_81coin_filt_ra.jsonl")
    ap.add_argument("--arm", default="filt")
    ap.add_argument("--split-date", default="2026-06-17")
    ap.add_argument("--seed", type=int, default=20260930)
    args = ap.parse_args()

    rows = load_trades(args.log, args.arm)
    split_ms = int(dt.datetime.fromisoformat(args.split_date).replace(
        tzinfo=dt.timezone.utc).timestamp() * 1000)
    X = np.vstack([r["x"] for r in rows])
    # NaN -> column median (train-safe imputation done after split)
    tr_idx = np.array([r["t"] < split_ms for r in rows])
    te_idx = ~tr_idx
    med = np.nanmedian(X[tr_idx], axis=0)
    inds = np.where(np.isnan(X))
    X[inds] = np.take(med, inds[1])

    mu, sd = standardize_fit(X[tr_idx])
    Z = (X - mu) / sd
    y = np.array([r["y"] for r in rows])

    # Model choice on TRAIN via 5-fold time-series CV AUC
    auc_lr = ts_cv_auc(lambda: Logistic(), Z[tr_idx], y[tr_idx])
    auc_tr = ts_cv_auc(lambda: ShallowTree(), Z[tr_idx], y[tr_idx])
    chosen = "logistic" if auc_lr >= auc_tr else "tree"
    model = Logistic() if chosen == "logistic" else ShallowTree()
    model.fit(Z[tr_idx], y[tr_idx])
    p_train = model.predict_proba(Z[tr_idx])

    # tau: max-F1 on TRAIN, frozen
    cand_tau = np.unique(p_train)
    tau = float(cand_tau[int(np.argmax([f1_at(y[tr_idx], p_train, t) for t in cand_tau]))])

    # ---- TEST (single report) ----
    p_test = model.predict_proba(Z[te_idx])
    go = p_test >= tau
    test_rows = [r for r, m in zip(rows, te_idx) if m]
    go_m = go  # aligned to te order already (Z/te index aligns with rows order)

    net_te = np.array([r["net"] for r in test_rows])
    bps_all = net_te / 10000.0 * 1e4
    bps_go = bps_all[go]
    bps_skip = bps_all[~go]
    y_te = y[te_idx]

    boot = bootstrap(test_rows, go, args.seed)

    keep = go.mean()
    pass_rules = {
        "R1 delta CI lo > 0": boot["delta_ci"][0] > 0,
        "R2 go CI lo > 0": boot["go_ci"][0] > 0,
        "R3 skipped mean net < 0": bps_skip.mean() < 0 if len(bps_skip) else False,
        "R4 keep ratio >= 0.20": keep >= 0.20,
        "R5 go net >= 3 bps (economic)": bps_go.mean() >= 3.0,
    }
    decision = "H1 PASS" if all(list(pass_rules.values())[:4]) else "H1 CLOSED / no-go"
    if decision == "H1 PASS" and not pass_rules["R5 go net >= 3 bps (economic)"]:
        decision += " (stat-only, below economic threshold)"

    print("=" * 68)
    print("M-3 meta-labeling — TEST report (single, frozen protocol)")
    print("=" * 68)
    print(f"chosen model      : {chosen}  (train CV AUC: lr={auc_lr:.4f} tree={auc_tr:.4f})")
    print(f"frozen tau        : {tau:.4f}")
    print(f"test trades       : {len(test_rows)}  days: {boot['n_days']} "
          f"(with go: {boot['days_with_go']})")
    print(f"keep ratio        : {keep:.1%}  (go={int(go.sum())} skip={int((~go).sum())})")
    print("-" * 68)
    print(f"baseline all net  : {bps_all.mean():8.2f} bps/trade  win {y_te.mean():.1%}")
    print(f"GO  net           : {bps_go.mean():8.2f} bps/trade  win {y_te[go].mean():.1%}")
    print(f"SKIP net (actual) : {bps_skip.mean():8.2f} bps/trade  n={len(bps_skip)}")
    print("-" * 68)
    print(f"Delta (go - all)  : {boot['delta_point']:+.2f} bps  "
          f"CI[{boot['delta_ci'][0]:+.2f}, {boot['delta_ci'][1]:+.2f}]")
    print(f"GO net CI         : [{boot['go_ci'][0]:+.2f}, {boot['go_ci'][1]:+.2f}]")
    print("-" * 68)
    for k, v in pass_rules.items():
        print(f"[{'PASS' if v else 'FAIL'}] {k}")
    print("-" * 68)
    print(f"DECISION: {decision}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
