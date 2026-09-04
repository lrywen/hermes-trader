# S2 信号源 PF 普查结果归档（2026-09）

对应 `docs/strategy-direction.md` §4 路线图 **S2**（信号源 PF 普查 → 信号质量排行榜 → 识别 PF<1.05 的信号源清单）与 §2 重构目标（"每个信号源/子信号按双周期 PF 排序，低于门槛的降权或剔除"）。

**结论先行：离线价格信号无一通过双周期净 PF ≥ 1.05 准入门槛；本次只产出清单，不修改生产权重。** 口径不匹配的原因见 §4，不改生产的决策见 §5。

## 1. 复现方式

```bash
HERMES_BACKTEST=1 uv run python scripts/signal_pf_census.py \
  --coins BTC,ETH,SOL --with-costs \
  --windows 4h:365d,1h:180d \
  --cache-file .backtest_cache.json
```

- 脚本：`scripts/signal_pf_census.py`（只读、不下单；复用 `scripts/pf_dual_period_report.py` 的 S1 会计与 `SIGNAL_SPECS`）。
- 成本：taker 2.5bps/侧 + 滑点（含成本净 PF；毛 PF 脚本亦输出，本表只列净 PF）。
- 门槛：净 PF ≥ 1.05（准入）/ ≥ 1.2（目标）；每桶样本 ≥ 30；信号在闭合 bar 触发、次 bar 开盘入场、持有固定 N bar 收盘出场，无前视。
- 报表只输出 stdout，不落结果文件；K 线缓存 `.backtest_cache.json` 已在 `.gitignore`。

## 2. A 表：离线信号排行榜（按 4h:365d 净 PF 降序）

| 信号 | 方向 | 4h:365d n / 净PF | 1h:180d n / 净PF | 判定 |
|---|---|---|---|---|
| momentum_continuation_1h | long | 149 / 0.948 | 23 / **5.766** | INSUFFICIENT（1h n<30） |
| downtrend_momentum | short | 2114 / 0.904 | 1155 / 0.865 | FAIL |
| breakout | long | 88 / 0.861 | 194 / 0.943 | FAIL |
| regime_trend | short | 2711 / 0.840 | 3993 / 0.612 | FAIL |
| bearish_reversal | short | 196 / 0.803 | 176 / 0.555 | FAIL |
| regime_trend | all | 4962 / 0.799 | 8326 / 0.705 | FAIL |
| momentum_burst | long | 139 / 0.794 | 24 / 1.302 | INSUFFICIENT（1h n<30） |
| breakout | all | 199 / 0.791 | 370 / 0.957 | FAIL |
| momentum_burst | all | 287 / 0.779 | 34 / 1.032 | FAIL |
| momentum_burst | short | 148 / 0.766 | 10 / 0.574 | INSUFFICIENT（1h n<30） |
| breakout | short | 111 / 0.759 | 176 / 0.977 | FAIL |
| regime_trend | long | 2251 / 0.747 | 4333 / 0.807 | FAIL |
| higher_lows_1h | long | 2860 / 0.744 | 5467 / 0.837 | FAIL |
| uptrend_momentum | long | 1570 / 0.587 | 1453 / 0.719 | FAIL |
| bullish_reversal | long | 274 / 0.522 | 153 / 0.636 | FAIL |
| trend_flip_1h | long | 470 / **0.371** | 947 / 0.799 | FAIL（最差） |

单方向信号的反向侧 n=0（INSUFFICIENT，非 FAIL）：uptrend_momentum/short、downtrend_momentum/long、bullish_reversal/short、bearish_reversal/long、trend_flip_1h/short、higher_lows_1h/short、momentum_continuation_1h/short。

要点：

- **没有任何信号双周期同时 ≥ 1.05。** 裸价格信号集合作为固定持有 24h 的方向策略本身不赚钱，与 S3 多周期 A/B 结论一致（baseline 4h 0.957 / 1h 0.992，方向一致性过滤救不回）。
- 唯一正苗头是 `momentum_continuation_1h/long`：1h 净 PF 5.766 但 n=23 不足 30，4h 净 PF 0.948 仍低于门槛 → 只能列为观察项，不能据此加权。
- 最差为 `trend_flip_1h/long`（4h 0.371），即"趋势翻转抄底/摸顶"在固定持有口径下亏损最严重。

## 3. B 表：日志 LLM verdict 普查

INSUFFICIENT：近 30 天 `.agent-memory.json` 中 **0 条** LONG/SHORT analysis（现存 verdict 全为 PASS/观望）。whale/news/composite 自身不发方向 verdict——**生产中唯一产出 LONG/SHORT 的是 LLM**。随实盘 verdict 积累后 B 表自动出分，本次无数据可评。

## 4. C 段：below-gate 清单（净 PF < 1.05 且 n ≥ 30，共 19 桶）

```
[offline] downtrend_momentum / short      [offline] downtrend_momentum / all
[offline] breakout / long                 [offline] regime_trend / short
[offline] bearish_reversal / short        [offline] bearish_reversal / all
[offline] regime_trend / all              [offline] breakout / all
[offline] momentum_burst / all            [offline] breakout / short
[offline] regime_trend / long             [offline] higher_lows_1h / long
[offline] higher_lows_1h / all            [offline] uptrend_momentum / long
[offline] uptrend_momentum / all          [offline] bullish_reversal / long
[offline] bullish_reversal / all          [offline] trend_flip_1h / long
[offline] trend_flip_1h / all
```

## 5. 为什么本次不改生产权重（口径不匹配）

离线 PF 与生产 `composite_score` 权重是**两个口径**，不能直接拿离线 PF 去降权/裁剪：

1. **多数离线信号在生产中本就 weight = 0。** `TRIGGER_CONFIG["weights"]`（`hermes_trader/agents/config.py`）里 uptrend/downtrend_momentum、higherLows1h、trendFlip1h、bullish/bearish reversal 类仅作 surfacing/bypass，不参与方向计分；离线 FAIL 对它们的生产行为无新增信息量。
2. **生产真正带正权重的 trigger 大多无法离线评 PF。** trendStrength(0.55)、pctMoveSpike(0.40)、volumeSpike(0.25)、volumeBuildup1h(0.15) 是非方向的 surfacing 计分器，没有"方向 × 持有 N bar"的离线定义，不在 `SIGNAL_SPECS` 内。能映射的仅 breakout(0.30)、momentumBurst(0.20)、momentumContinuation1h（可选权重）。
3. **现有权重是按真实成交标定的，不是按裸信号 PF。** 权重表于 2026-06 按 n=497 笔真实交易的边际 lift 回归得到（括号内为边际收益）：trendStrength +2.08%、pctMoveSpike +1.49%、breakout +1.29%、volumeSpike +1.05%、momentumBurst +0.77%、volumeBuildup1h +0.41%；higherLows1h −0.51%、trendFlip1h −2.10%、rangeCompression −3.08% 已置 0。离线"裸方向固定持有 24h PF"衡量的是信号单独做方向策略的盈利能力，与"该 trigger 出现时 LLM 成交的边际 lift"不是一回事。
4. 生产中**不存在**按 PF 降权/裁剪的机制，也不存在时间衰减权重；贸然按离线 PF 改权重会破坏已用真实交易验证过的标定。

**决策（已与负责人确认）：只出清单，不改生产代码/权重。** 本清单作为 S4 SHADOW 候选筛选与后续信号整改的输入；任何权重改动仍须走 §3 双周期 PF 门槛 + A/B 对比 + SHADOW 灰度（S4）流程。

## 6. 后续

- `momentum_continuation_1h/long`：1h 窗口样本积累到 ≥30 后复算，若双周期仍 ≥1.05 可作为 S4 候选。
- B 表：随实盘 LONG/SHORT verdict 积累（memory TTL 30d）后自动出分，是唯一能评 LLM/whale/news 上下文真实方向质量的口径。
- §2 时间衰减因子（`advanced-optimization-roadmap.md`）依赖的普查已完成，但在权重口径统一（或有 SHADOW 证据）前暂缓落地。
- S5 季度复算时重跑本普查，连续两季 <1.0 的信号按 §3 规则下线。
