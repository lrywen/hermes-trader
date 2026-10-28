"""xs top_pct 85 vs 90 同信号流双计数（只读）单测。

不依赖真实 K 线：切带逻辑用合成 episode 验证，jsonl 聚合用临时文件验证。
关键不变量：90 是 85 的纯子集；85-only = 过 85 但不过 90；未回填 outcome 的
候选计入 pending 不计入 edge。
"""
import json
import os

from scripts.xs_top_pct_dual_count import _bands, jsonl_dual

HOUR = 3_600_000


def test_bands_partition_subset_and_counterfactual():
    # 入参约定：已是过 85 门的超集流（调用方用 top_pct=85 重放后再传入）。
    # pctile: 0.05(过90)、0.12(85-only)；fwd72_net 为百分数。
    rows = [
        {"ts": 1 * HOUR, "pctile": 0.05, "fwd72_net": 3.0},
        {"ts": 2 * HOUR, "pctile": 0.12, "fwd72_net": 2.0},
        # 0.15 恰为 85 边界（<=），归 85-only
        {"ts": 9 * HOUR, "pctile": 0.15, "fwd72_net": 1.0},
    ]
    bands = _bands(rows, boot=200, seed=1)
    assert bands["all85_triggers"]["n"] == 3
    assert bands["top90_subset"]["n"] == 1            # 只有 0.05 过 90
    assert bands["band_85_only"]["n"] == 2            # 0.12 与边界 0.15
    # 0.12 一笔 2.0% -> 200bp（带内均值取该带全部笔，这里仅校验换算存在）
    # 90 与 85-only 互不相交且并集 = 85 全集
    assert (bands["top90_subset"]["n"]
            + bands["band_85_only"]["n"]
            == bands["all85_triggers"]["n"])


def _write_jsonl(path, rows):
    with open(path, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def test_jsonl_dual_counts_and_pending_outcomes(tmp_path):
    p = os.path.join(tmp_path, "xs.jsonl")
    _write_jsonl(p, [
        # 过 90、是候选、已回填 outcome +1%（net_pct=100 -> 1.0% -> 100bp）
        {"timestamp": HOUR, "ext_percentile": 0.05, "is_candidate": True,
         "outcome": {"fwd72_net_pct": 100.0}},
        # 85-only、是候选、outcome 缺失 -> pending
        {"timestamp": 2 * HOUR, "ext_percentile": 0.12, "is_candidate": True,
         "outcome": None},
        # 85-only、非候选（不计候选数，也不计 edge）
        {"timestamp": 3 * HOUR, "ext_percentile": 0.12, "is_candidate": False},
        # 两阈值都不过
        {"timestamp": 4 * HOUR, "ext_percentile": 0.30, "is_candidate": True},
        # 旧账本无 percentile -> 跳过
        {"timestamp": 5 * HOUR, "is_candidate": True},
    ])
    res = jsonl_dual(p, boot=200, seed=1, days=None)
    assert res["rows_with_percentile"] == 4
    assert res["all85_rows"] == 3
    assert res["top90_rows"] == 1
    assert res["band85_only_rows"] == 2
    assert res["cand85"] == 2            # 0.05 与 0.12(pending) 两条候选
    assert res["cand90"] == 1
    assert res["cand85only"] == 1
    assert res["outcome_pending"]["p85only"] == 1
    assert res["top90_subset"]["n"] == 1
    assert res["top90_subset"]["mean_net_bp"] == 100.0
    assert res["band_85_only"]["n"] == 0  # 唯一 85-only 候选 pending，无 edge


def test_jsonl_dual_missing_file(tmp_path):
    res = jsonl_dual(os.path.join(tmp_path, "nope.jsonl"), 200, 1, None)
    assert res["rows"] == 0
