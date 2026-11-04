# slowBurn IC + R1 入场触发器/regime 重放对照（只读）

- 日期：2026-11-04（数据截至当日 UTC）
- 依据：`Hermes_入场可观测性补齐与slowBurnIC_方案_2026-11-03.md` v6 §4
- 性质：SHADOW 零敞口、纯只读；**不改任何 admit/block 判定、不改配置**
- 机读数据：[slowburn_ic_2026-11-03.json](slowburn_ic_2026-11-03.json)

> **一句话结论：三组口径（B 二元、A 序变量、R1 重放 per-hit）全部没有可统计识别的增量。
> 独立日 d=6~11，远低于预注册门槛 d≥30，本报告仅为描述统计，不触发「去旁路 / 入表」任何动作。**

---

## 0. 三个数据口径（严禁混算）

| 口径 | 来源 | 行/d | 自变量 | 可信度 |
|---|---|---|---|---|
| **B 二元** | `decision_shadow_outcomes.jsonl` | 2,886 行 / 12 日 | 老 7 名 `fired_names` 含 slowBurn | as-produced，但老口径 |
| **A 序变量** | 同上，`slow_burn_count∈{0,1,2,3}` | 882 行（1h 有收益）/ 11 日 | 计数 0/1/2/3 | as-produced |
| **R1 重放** | `research_replay_trigger_hits.jsonl`（扩展 `scripts/replay_gate.py`） | 4,785 行 / 44 币 / 6 日 | 新 14-hit trigger_hits | **recomputed，非 as-produced**，long-only |

R1 重放的诚实边界：AI 置信度被中性化为 0.9、whale/新闻/账户态缺失；
`regime_gate` 用 as-of 收盘 1h K线走纯分类器 `classify_candles` 重建（PIT）；
`regime_decision`（含 coin-breakout RVOL 覆盖）**不可重放**，置 null；
`flow_confirm` 来自扫描时刻实时 WS 逐笔内存、K线无法重建（tape 仅 BTC/ETH），置 null/available=false。

## 1. B 口径：slowBurn 有/无二元组间对照（net @9.26bp）

主口径＝逐日先算组均值差、再对日差 bootstrap（seed=7, B=4000）：

| horizon | T 均值 | F 均值 | Δ | 95% CI | 显著 |
|---|---|---|---|---|---|
| **1h** | −45.61 (n716,d9) | +22.97 (n433,d10) | **−12.51bp** | [−66.57, +52.06] | 否 |
| 4h | — | — | −87.65 | [−211.05, +33.04] | 否 |
| 24h | — | — | −405.99 | [−753.18, −136.71] | 表面显著* |
| 72h | — | — | −737.17 | [−1207.08, −340.11] | 表面显著* |

\* 24h/72h 的显著是**假显著**：long 层 T(−641bp) 与 F(−643bp) 几乎相同，差异来自整批 cohort 在下跌行情；
`excess_*` 仅 6 行非 null（基准序列缺失），长 horizon 含市场 beta，**不能做方向归因**。

- 方法敏感性：按行重采样（日按行数加权）1h Δ=−68.58 CI[−107.95,−16.45] 看似显著；
  与等权日口径差异全在日权重。**以等权日口径为准**，两者在 d<10 下都不能定论。
- 分层：long Δ=+29.89 CI[−73.4,+164.1]；short Δ=−29.75 CI[−81.4,+26.0]；均不显著、方向相反，不可外推。

## 2. A 口径：slow_burn_count 序变量 rank IC（新增，历史字段本就落盘）

- **Spearman ρ（net vs count）：1h +0.023 / 4h −0.009 / 24h +0.019 / 72h −0.008 ≈ 0**
- 1h 分档均值（bp）：c0 −14.4(n153)、**c1 −66.8(n499)**、c2 −0.7(n188)、c3 −20.3(n42) —— **非单调**（最差是 c1 不是 c3）。
- c≥1 vs c0 等权日 Δ：1h +8.3 CI[−52.3,+78.2]；4h/24h/72h 全部跨 0。

**rank-IC 假设在描述层证伪**：计数高低与前向净收益既无秩相关、也无单调关系。

## 3. R1 重放：新 14-hit per-hit + PIT regime（long-only，recomputed，d=6）

PIT regime 分布合理（修复了「detect_regime_with_score 只拉当前 K线、给所有历史行贴今天 regime」的伪历史问题）：
up 1938 / down 1926 / chop 621 / neutral 267。

1h 净收益按 regime：up −0.6 / down −14.9 / chop −11.8 / neutral −20.1 bp（方向符合直觉，但只是描述）。

per-hit fired vs not（等权日 Δ，CI 全跨 0，无一显著）：

| 触发器 | Tn | Δ (bp) | 95% CI |
|---|---|---|---|
| breakout | 123 | +69.6 | [−74.5, +388.8] |
| trendFlip1h | 284 | +52.5 | [−15.1, +210.1] |
| uptrendMomentum | 136 | +45.9 | [−49.5, +273.2] |
| trendStrength | 2662 | +8.0 | [−20.1, +31.7] |
| pctMoveSpike | 418 | +9.1 | [−39.3, +54.8] |
| rangeCompression | 624 | +5.6 | [−24.9, +41.3] |
| volumeSpike / volumeBuildup1h / downtrendMomentum / higherLows1h | — | +4.0 / +2.1 / +1.8 / −2.7 | 全跨 0 |

- 逆势旁路探针：down/chop regime 下仍出现 long 冲动（up/breakout）n=94，均值 +57.4 vs 同类其他 −3.9，
  Δ **+61.3bp 但 CI[−86.6,+384.3]** 极宽，不能定论。
- breakout / trendFlip1h / uptrendMomentum 点估计最正，是唯一值得继续累积观察的方向；**当前不得据此调权**。

## 4. 独立性与外推限制

1. R1 每小时对同一批币采样，币×日内高度相关；**有效样本是币×日，不是行数**（d=6）。
2. R1 long-only，per-hit 符号不能外推 short；新 14-hit 的生产真实样本目前 d=1（全 CASHCAT 一段行情）。
3. 老 7 名 fired_names 与新 14-hit trigger_hits **不是同一份数据，禁止合并**。
4. B 口径 short 行的收益已按 P0-3 方向化（sgn=−1）重算。

## 5. 决定

- 预注册门槛（主口径 net_bp9.26 1h，n≥100 **且 d≥30**）**未达标**（n 够、d=9~11）。
- **slowBurn 维持现状：不入表、不去旁路、不加权**。A/B/R1 三口径一致地「测不出增量」。
- 继续让 SHADOW 累积真实生产 14-hit 样本，按币×日计 d；到 d≥30 再做正式判定。
- 配套工程项（short 短路行新键兜底 U1、方案文档 §3.5 漂移 U2）见同批改动，与本报告结论独立。
