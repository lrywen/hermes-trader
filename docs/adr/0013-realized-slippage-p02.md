# 0013：真实滑点测量 P0-2 —— 5m 实现差不支持 10× 空头地板，但证据尚不足以放松

- 状态：**Measured（测量完成）；结论 = 不放松地板，需补盘口深度测量**
- 日期：2026-11-02（Asia/Shanghai）
- 关联：[ADR-0008](./0008-allow-shorts-single-source-runner-gate.md)、
  [ADR-0012](./0012-direction-aware-scoring-short-mirror-freshness.md)、
  v2 复核 §0（真实滑点决定 50M 空头地板能否下调）、
  证据 [evidence-slippage-2026-11-02.json](./evidence-slippage-2026-11-02.json)、
  脚本 [measure_realized_slippage.py](../../scripts/measure_realized_slippage.py)

## 1. 目的

空头流动性地板 `runner_entry_gate.min_short_volume_usd = 50_000_000`（是多头 5M 地板的 10×）。
v2 融合路线要求：放松该地板必须有真实滑点证据。P0-2 即测真实成交成本，判断小成交额币（5–50M，
当前禁止做空）的执行成本是否真的显著高于大成交额币（≥50M，允许做空）。

## 2. 为什么不能直接用真实成交

生产真实 fill 仅 22 笔（9 月，多 21 / 空 1）；其中带 entry_slip 的 10 笔**全是多头**，
无空头、无小币分层、无统计意义。shadow book 的 fill.price 是 `_apply_slippage` 模型价，
不是市场真实滑点，不能当证据。

## 3. 测量口径（可复现）

- 总体：runner_gate_shadow 全量去重候选（2430），按候选**自身记录的** volume_24h_usd 分层
  （不用今日快照，避免幸存者偏差）。
- 决策价 = 候选 ts 所在 5m bar 收盘价；实现价 = **下一 5m bar 开盘价**（taker 市价单最早可成交价）。
- 原始实现差 bp = (fill_open − signal_close)/signal_close×1e4；
  **方向化**：long 取原值（买得更高=不利），short 取负（卖得更低=不利），正=不利。
- 数据源：Binance 现货 5m K线（与 backtest_short_5m 同源，缓存 ch_slippage_candles.json）。
- 严格防前视：只用信号 bar 之后的下一根 bar。
- 分层：micro <5M / small 5–50M / medium 50–200M / large ≥200M。

## 4. 结果（2026-11-02，1717 配对 / 87 币 / 10 个自然日）

| 层 | n | 平均不利(bp) | iid CI95 | 中位 | p90 | 不利占比 |
|---|---|---|---|---|---|---|
| 总体 | 1717 | 0.365 | [−0.11, 0.85] | 0.0 | 9.93 | 36.4% |
| micro <5M | 1296 | 0.313 | [−0.31, 0.95] | 0 | 10.81 | 36.8% |
| **small 5–50M（禁空）** | 278 | **0.470** | [−0.15, 1.09] | 0 | 6.56 | 36.3% |
| medium 50–200M | 74 | 0.108 | [−1.03, 1.14] | 0 | 4.68 | 32.4% |
| large ≥200M | 69 | 1.192 | [−0.91, 3.25] | 0 | 19.19 | 33.3% |

分方向：long 平均 0.088bp（n=733），short 0.572bp（n=984，CI 跨 0）。

**稳健性（更严格口径）：**
- 按币聚合（防高频币主导）：small 每币均值 0.320bp，ge50M 0.406bp —— 小币不更高。
- 日块 bootstrap（独立日）：small − ge50M 差异 CI = **[−0.60, +0.72] bp，跨 0**。
  即「小币 5m 实现滑点更差」**无统计证据**；方向上甚至略低，但同样不显著。

## 5. 裁定：不据此放松 50M 地板

证据方向**不支持** 10× 地板（5–50M 币的下一开盘实现差并不更差），但**强度与口径都不足以放松**：

1. **独立信号日仅 5 天**（runner_gate 轮转只留近 9 天），远低于验收墙 d≥30；样本期短、可能单 regime。
2. **本测量是 mid-to-mid 的 5m 实现差，不含盘口深度冲击**。50M 地板防的恰恰是薄盘口里
   市价单的吃单深度/价差成本，next-open 看不到这部分。在 ~$30 notional 下深度冲击可能小，
   但本测量未测，不能假设。
3. 中位 0bp、仅 36% 不利，说明多数候选下一开盘无不利移动；真正风险在**右尾**（p90 7–19bp），
   需尾部/深度口径而非均值来定地板。

## 6. 后续（放松地板的前置证据）

1. **盘口深度测量**：记录信号时刻 order book 前 N 档，按目标 notional（$30）模拟吃单
   加权价 vs mid，得到含深度的滑点，按 volume_24h 分层，比较 small vs ge50M 的右尾。
   这是回答 50M 地板的正确口径（当前系统未持久化 book 快照，需新增采集，shadow-only）。
2. **延长样本**：将 runner_gate / 滑点配对样本积累到 d≥30 再判，避免单 regime 结论。
3. 即使放松，也按 ADR-0008 显式变更 + 与 scan_long_only / allow_shorts 成套（v2 关键推论），
   且先 shadow 后 canary，不得单独下调。

## 7. 产物与边界

- 新增 `scripts/measure_realized_slippage.py`（只读、不下单、防前视；--no-network 可离线）。
- 不改任何生产配置；allow_shorts / min_short_volume_usd 维持原值。
- 测量脚本不在生产 hot path；缓存 ch_slippage_candles.json 在容器 /data（约数 MB）。
