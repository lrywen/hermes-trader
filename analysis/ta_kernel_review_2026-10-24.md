# TA 内核完整分析（v11 姊妹篇）复核批注

> 复核时间：2026-10-24（Asia/Shanghai）
> 被复核文档：`Hermes-Trader_TA内核完整分析_2026-10-24.md`（07:40Z，基线 HEAD 7f8d228）
> 方法：不采信文档结论，全部从源码、容器实时配置、numpy 独立复算、轮转 events/session 日志重算。
> 复核时基线：HEAD 已含 d021122（candidate_gate_shadow，arm 默认关闭）。

## 0. 总评

文档**整体扎实、方法论可复现、绝大多数硬断言成立**：零依赖自研、指标公式逐位正确、NaN 毒化、平盘 RSI、build_hits 编排、B2–B11、测试数、audit 脚本口径错误——逐项独立复核一致。

但有 **1 个关键论断表述错误（会误导决策）**、**2 处参数/算术事实错误**、**1 处数据可复现性瑕疵**，需修正。核心结论方向（alpha 在信号/评分层、评分层权重×门槛失配是真 P1）不受影响。

---

## 1. 完全坐实（独立复算一致）

| 文档断言 | 独立复核 |
|---|---|
| 零第三方 TA 依赖、纯标准库 | pyproject 精确 9 个直接依赖；容器 `pip list` 56 包，grep numpy/pandas/talib/scipy/statsmodels **零命中**；`hermes_trader/` 源码零科学栈 import；wickra 零命中 |
| indicators 三文件 1,359 行 | `wc -l`：math **194** + triggers **990** + volume_profile **175** = **1359**，精确一致 |
| 指标与 numpy 逐位一致 | 宿主机 numpy 独立重算（seed=7，400 根随机游走 OHLC）：EMA/ATR/RSI/ADX/OBV `max_err=0`；SMA 仅浮点累加顺序差（复核 2.3e-13，文档 5.7e-14，同量级） |
| 平盘 RSI=100 / atr=0 / adx=0 | 复现一致（[math.py L92](../../hermes_trader/indicators/math.py) `avg_l==0 -> 100`） |
| `period=0/-1` ValueError（B10） | 复现：`ValueError: period must be a positive int` |
| 短序列 fail-closed 全 NaN | 5 根跑 atr(14) 返回全 NaN，不抛异常 |
| **单 NaN 毒化整条序列** | 复现：sma 从含 NaN 的窗口起全 NaN；ema 从该点起后段全 NaN；rsi/atr 同样毒化 |
| composite 公式与分母 | [triggers.py L922-925](../../hermes_trader/indicators/triggers.py)：分母 `sum(weights.values())`；生产 weights 求和实测 = **1.80** |
| build_hits 编排顺序、纯函数、实盘/回测共用 | [triggers.py L786-908](../../hermes_trader/indicators/triggers.py) 11 base→squeeze 耦合→可选 continuation→K 线→dailyMover，与文档一致 |
| B2–B11 修复落地 | 源码标记逐一在位（B2/B3/B4/B6/B7/B8/B9 在 triggers，B10 在 math，B11 在 volume_profile `_MAX_ATR_BINS=10_000`） |
| 6 个测试文件 143 用例 | grep `def test_`：34/45/35/19/5/5 = **143**，精确一致；核心三文件复核实跑 **89 passed** |
| rangeCompression 100 根 15m 只出 81 个带宽样本 | [triggers.py L374](../../hermes_trader/indicators/triggers.py) `bandwidths[-100:]`，前 bb_length−1=19 根 mid 为 NaN → 81，正确 |
| audit_triggers_1y.py 用 5m 喂 1h/15m 触发器 | [audit_triggers_1y.py L91-92](../../scripts/audit_triggers_1y.py) 确以 `c5m` 调 `range_compression`/`trend_strength`，与生产 15m/1h feed 不符，统计不可比 |

---

## 2. 需修正项

### 2.1【关键，会误导】scan `minCompositeScore=54` 不是硬门 —— "26 条是乐观上限"不成立

文档 §5.3 断言："生产实际门槛是 54 不是 45，所以 7f8d228 反事实报的 26 条是乐观上限，按 54 跑只会更少。"

**这个表述错误。** scan 层的 54 是一条**带大量 OR-bypass 的浮出条件**，不是纯分数硬门。实据 [perception.py L962-964](../../hermes_trader/agents/perception.py)：

```python
if (score < min_score and not burst_fired and not whale_bypass
        and not trend_bypass and not pattern_bypass and not daily_mover_bypass
        and not sigma_burst_bypass):
    drop   # 只有"低分且无任一 bypass"才丢
```

即分数 <54 时，只要 momentumBurst / uptrend|downtrendMomentum（trend_bypass）/ K 线形态 / dailyMover / sigma-burst 任一触发，照样浮出送研究。rp2 已实测约 99.7% 的浮出走 bypass、纯分数过 54 的仅约 0.3%。

两层门的真实语义：

| 层 | 配置 | 时点 | 性质 |
|---|---|---|---|
| scan 浮出层 | `scan.minCompositeScore=54` | 研究 LLM **之前** | 分数 54 **或** 任一 bypass（burst/trend/pattern/mover/sigma） |
| runner 末端门 | `runner_entry_gate.min_composite=45` | 研究 verdict **之后**、下单之前 | 多 OR 判定（structured_runner 的 slow_count 腿主导，纯分数腿近死） |

而 7f8d228（ch_weight_calib）的 "100→26" 是在 fired bars 上机械重算**纯分数是否过 45、不含任何 bypass**，它和 scan 54 不是同一口径，**不能直接比较，也推不出"按 54 更少"**。

配套实证（本会话 runner_gate_shadow 实时）：到达 runner 门被拦的 LONG 候选 composite **全部 <40**（最高 35.9），拦因是 late-chase / confidence<0.6 / needs-fresh-breakout，**不是** 45 或 54 的分数门。因此更准确的描述是：

> 信号不是被"某一道 54/45 硬门"掐断，而是链路式收紧——权重 0.55→0.30 压低纯分数 → 浮出主要靠 trend/burst bypass → 进研究拿到 LONG verdict 后又被 runner 的 late-chase/confidence/fresh 多道关拦掉。

文档"权重×门槛失配是 P1"的大方向仍对（权重 promote 的确把纯分数分布整体左移），但应删除/改写"54 是实际硬门、26 条乐观上限"这句。

### 2.2【参数错误】momentumBurst 生产参数是 2 bar / 4%，不是 3 bar / 3%

文档 §4.1 表第 6 行写 momentumBurst = "近 3 bar 累计涨跌幅 ≥3%"。

生产实测（容器 `/data/.agent-config.json` trigger_thresholds）：

```
momentum_lookback = 2,  momentum_pct = 4
```

函数 [triggers.py L436-466](../../hermes_trader/indicators/triggers.py) 语义：`c[-lookback-1] -> c[-1]`，即跨 **2** 根 bar（c[-3]→c[-1]）累计 **≥4%** 才 fire。

文档把 **weight_shadow 候选参数（3 bar / 3%）** 误当成了生产值。生产值是 2 bar / 4%。

### 2.3【算术小错】两个最高权重触发器满分上限是 38.9，不是 39.4

文档 §5.2 表格："两个最高权重（0.40+0.30）满分 → 39.4"。

正确：`10 × (0.40+0.30) / 1.80 = 38.89`。单触发 22.2、三重（0.40+0.30+0.30）55.6 两栏正确。

### 2.4【可复现性瑕疵】历史 analysis 分布数字对不上任何可识别数据切片

文档 §5.3 / 附录引用：n=3,642，mean 24.47 / median 26.14 / p90 36.66，≥54 仅 43（1.18%），≥45 79（2.17%）。

我用容器轮转 events 重算，三组口径都对不上：

| 数据源 | n | mean | median | ≥54 | ≥45 |
|---|---|---|---|---|---|
| 文档 | 3,642 | 24.47 | 26.14 | 43 (1.18%) | 79 (2.17%) |
| 复核：events.jsonl（10-19 快照） | 3,446 | 25.00 | 26.00 | 80 (2.32%) | 112 (3.25%) |
| 复核：events + .1（当前全量） | 6,026 | 24.37 | 25.67 | 112 (1.86%) | 176 (2.92%) |

三组 `max` 都 = 84.42，说明同源、只是切片/时点不同，但文档未注明用了哪些轮转分卷、时间窗或 verdict 过滤。**结论方向不受影响**（≥54 通过率都在 1–2% 量级，分数普遍远低于门），但精确数字不可复现，建议在文档中补注数据源与时间窗。

---

## 3. 对结论的净影响

- **不变**：自研、零依赖、指标层质量高（可替换）、B2–B11 工程修复到位、NaN 单点毒化和平盘 RSI 是真实缺口、alpha 与经济有效性问题集中在信号层/评分层。
- **需弱化措辞**：把问题归结为"一道 54 硬门"过于简单；真相是权重左移 + bypass 浮出 + runner 多道关的**链路**。这也解释了为什么单纯调一个分数门槛的离线反事实不能直接外推到端到端成交。
- **建议优先级维持**：P1 golden 值回归测试、NaN 传播修复、权重×门槛的**真实 AI-gated 双臂对照**（而非直接改门）；P2 平盘 RSI、rangeCompression 取足样本；P3 修 audit 脚本 feed 口径。

---

## 4. candidate_gate_shadow 样本收集时长估算

> arm 已于 d021122 部署但 **enabled=false**（零 LLM 花费、零行为变化）。以下回答"打开后要多久"。

### 4.1 触发口径回顾

arm 只对**同时满足**的候选付费产 1 条隔离 verdict：研究前 TA 门判定 skip、composite ∈ [40,45)、long-intent、TA 非 REJECTED、非持仓，且同一 closed-bar fingerprint 只采一次。结论写独立 `candidate_gate_shadow.jsonl`，不 route/不交易/不入冷却。

### 4.2 两个差距很大的经验速率（口径必须分清）

| 数据源 | 口径 | [40,45) 频率 |
|---|---|---|
| session near_miss（近月，旧权重 0.55 + gate54） | 全候选原始事件，**未按 closed-bar 去重**、同 setup 多周期重复计数 | ~136/天 |
| signal_ranking（今天 8h，**当前权重 0.30**、去重后 top-ranked） | 最贴近 arm 真实触发群体（arm 也按 fingerprint 去重） | 8h 仅 **1** 个 → 约 3/天 |

差异主因：①权重 promote 把分数整体左移，落 [40,45) 的候选变少；②near_miss 是重复计数的原始流，arm 的 fingerprint 去重后会显著下降。

arm 覆盖的是"所有 TA-skip 候选"，比 top-ranked 略宽，但有去重和 REJECTED/short 过滤。**现实估计落 [40,45) 合格 long 触发约 3–15 个/天**（低活跃/低波动日靠近 3，行情活跃日靠近 15）。这是数量级估计，arm 打开后头几天即可用真实计数校准。

### 4.3 达到可判定样本量需要多久

判定门槛优劣不能只看 verdict 数，要看"band 内 AI 给 LONG 且后续可配前瞻收益"的有效样本。历史 AI 对中分候选的 LONG 率约 1/3：

| 目标 | band 候选样本（约） | 预计自然时长 |
|---|---|---|
| 初步观察（~20–30 LONG，方向性） | ~60–90 | **1–2 周** |
| 可做分层结论（~60 LONG，按 regime/触发签名拆桶） | ~180–200 | **3–6 周** |
| 稳健判定（~100 LONG，配对前瞻收益显著） | ~300 | **5–9 周** |

**结论：打开后约 1–2 周能看方向，3–6 周才够支撑"是否把绑定门从 45 降到 40"的分层判断。** 当前 low-volatility（自 10-22 起实盘近乎零信号）会让区间偏上界。

### 4.4 可缩短周期的杠杆（不违背纪律）

1. **窗口放宽到 [35,45)**：今天 8h top-ranked 里 [35,40) 有 3 个、[40,45) 有 1 个，扩窗约把采样率提高 2–4×，代价是含更多噪声、LLM 成本上升；配置改 `min_composite=35` 即可，无需改码。
2. **不建议离线回放 LLM 补历史**：LLM 可能从输入感知后续行情，存在前视污染，历史快照也不全，结论不可信。
3. 即使样本攒够，**改绑定门 / 转 LIVE 仍是独立决策**，需走真实 tagged fills 与 B-13，不由此 shadow 自动晋升。
