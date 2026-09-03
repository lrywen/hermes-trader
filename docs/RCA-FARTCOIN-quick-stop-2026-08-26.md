# RCA：FARTCOIN 开仓 88 秒后被 DSL max_loss 止损（2026-08-26 01:52–01:53 UTC）

- 事件日期：2026-08-26（UTC，容器时间）
- 事件模式：SHADOW（$30 名义极小仓）
- 结论性质：**非系统故障**——DSL max_loss 杠杆 ROE 灾备闸门按设计正确触发；但暴露出两个策略侧观察项（见第 6 节）
- 关联复盘：`/data/postmortems/surge-FARTCOIN-20260826T015057Z.md`
- 审计标注：Audit 2026-09-03 P2-10

## 1. 结论摘要

2026-08-26 01:52:09 FARTCOIN 追多信号入场（$30 名义、10x），**持仓 88 秒后被
DSL `max_loss` 闸门平仓**，现货跌幅仅 -0.89%，但按 10x 杠杆折算 ROE 为
-8.90%，触及 `max_loss_roe_pct=5.0% / 10x = 0.5% 现货` 的硬止损下限（生效
现货止损 = min(ATR 止损 3.0%, ROE 止损 0.5%) = **0.5%**），而交易所 SL
触发器挂在 0.19173（-3.0%）处——**生效的硬止损是 0.5% 而非研究止损的
-3%**。净亏损 -$0.188（毛损 -$0.173 + 费 $0.015）。

## 2. 时间线（证据：events.jsonl + trading-loop.log.bak-20260902）

| 时间 (UTC) | 事件 | 证据 |
|---|---|---|
| 01:46:49–01:48:46 | 连续 3 次扫描评分 14.3–14.7，research 节流跳过 | postmortem 决策轨迹 |
| 01:49:54 | research 结论做多 conf=0.72，研究止损 0.19（-3.4%） | postmortem |
| 01:49:55 | execute **被 runner_gate 拦截**：`needs fresh breakout/burst and structure; score=15, slow=0` | events.jsonl execute executed=false |
| 01:50:53 | 综合评分跳变 14.7 → **69.2**（+54.5），re-research 节流**被绕过** | trading-loop.log "throttle BYPASSED" |
| 01:50:57 | Researching FARTCOIN（trigger 69.2, TA CONFIRMED）；数据预取 2.55s 完成 | log |
| 01:51:00 | debate 启动（max_latency_s=26.0，bull/bear per_call 18s） | log |
| 01:51:18 | bull/bear DONE（18.2s，bull 889 字符/bear 491 字符） | log |
| 01:51:44 | **synth FAILED → single fallback**：`TimeoutError: empty LLM response after 25615ms (role=synth)` | log WARNING |
| 01:51:44 | debate 返回 None → 单 LLM fallback | log WARNING |
| 01:51:59 | runner_gate：conf=0.78/0.70 score=69.2/45，fresh_impulse=1 → **ADMITTED** | log |
| 01:52:03 | 等风险 sizing：notional $30（risk $0.75 @ 2.50% stop，被 notional_cap 钳制） | log |
| 01:52:09 | IOC 下单 151.9 coin @ limit 0.19978 | log |
| 01:52:10 | **成交 @ 0.19766**（order_id 526868457729，size_usd 30.02） | events.jsonl order |
| 01:52:10 | DSL 注册：atr_stop=3.00%（1.2x ATR=7.58%，被 max_loss_pct 钳到 3.0%） | log "Registered FARTCOIN_long @ 0.19766 (10x)" |
| 01:52:11–12 | 交易所 SL 触发器 0.19173（-3.0%，reduce_only）+ TP 0.21264 挂单 CONFIRMED | log |
| 01:52:13 | execute executed=true，全部 15 道闸门 pass（debate via debate_consensus? **实际走 fallback**） | events.jsonl execute |
| 01:53:35 | WS user-fills：Close Long px=0.19652（持仓 88 秒） | user-fills |
| 01:53:39 | **dsl_exit：`max_loss (0.89% spot / 8.9% ROE >= 0.50% spot cap; spot_cap=3.00[atr], roe_cap=5.0/10x)`** | events.jsonl dsl_exit |
| 01:53:39 | close：realized -0.5767% 现货 / **-6.2675% ROE**（毛 -$0.1732、费 $0.015、净 -$0.1882），mfe_spot_pct=0.0，entry_slip 5.6bps | events.jsonl close |

## 3. 根因（代码级）

### 3.1 直接原因：DSL max_loss 的杠杆 ROE 闸门在 0.5% 现货处触发

`hermes_trader/agents/dsl_exit.py` `ExitPolicy._effective_max_loss()`：

```python
spot_cap = min(max(self.entry_atr_pct * pol.atr_stop_mult,
                   pol.max_loss_pct), pol.max_loss_pct)   # = 3.0%
roe_cap  = pol.max_loss_roe_pct / lev                     # = 5.0 / 10 = 0.5%
return min(spot_cap, roe_cap)                             # = 0.5%
```

当前部署 `max_loss_pct=3.0`、`max_loss_roe_pct=5.0`、`leverage=10`：
- ATR 止损空间 7.58%（1.2×ATR）被 `max_loss_pct` 钳到 3.0%；
- ROE 灾备上限 5% ÷ 10x = **0.5% 现货**，比 3.0% 更紧；
- 生效硬止损 = min(3.0%, 0.5%) = **0.5% 现货**。
- 价格入场后回落 0.89%（ROE -8.90%），早于交易所 SL（-3.0%）触发 DSL 平仓。

### 3.2 市场原因：追在 surge 局部顶部，入场后无任何有利波动

postmortem 记录：6.1σ 涨幅、19.6σ 量能、48 根突破，01:45 单根 5m 成交
1690 万；信号入场点 0.19766 即为本轮 surge 的局部高点，`mfe_spot_pct=0.0`
（持仓期从未浮盈），随后立即回落。

### 3.3 流程观察：入场结论实际来自单 LLM fallback，而非 debate 共识

debate 链路中 synth 在 25.6s 超时失败（与项 P0-2 记录的 LLM 网关超时
同一问题域），最终入场结论 conf=0.78 由**单 LLM fallback** 产生。
execute 事件里 `gate_results.debate.via="debate_consensus"` 与日志事实
不符（gate 对 debate/fallback 两种 verdict 都记 pass，via 字段未区分）。

## 4. 影响

- 资金：净亏 -$0.188（$30 名义，SHADOW 模式极小仓），无连锁风险。
- 系统：无故障。DSL 灾备闸门、交易所 SL/TP 挂单、事件哈希链均正常。
- 数据：该笔交易作为「高杠杆下 ROE 止损远紧于研究止损」的实证样本。

## 5. 判断：是否需要修复

**不需要代码修复**。0.5% 现货 / 5% ROE 是账户层面的杠杆灾备上限，属
fail-closed 正确行为；在 10x 下它先于研究止损动作是设计使然
（`max_loss_roe_pct` 的注释明确说明高杠杆下 min() 取 ROE cap）。削弱该
闸门会放大爆仓风险，红线明确不得动。

## 6. 观察项（转策略侧决策，不在本次审计修改）

1. **研究止损与 DSL 生效止损不一致**：研究报告给的 -3% 止损在 10x 下永远
   不会到达（DSL 0.5% 先触发）。若希望研究止损在高杠杆下有意义，需要
   `max_loss_roe_pct` 与 leverage 联合标定（策略参数决策，非 bug）。
2. **fallback verdict 的风控标识**：debate 失败后单 LLM fallback 的结论
   与 debate 共识在 gate/事件里不可区分。建议后续在 execute 事件
   `gate_results.debate` 增加 `via="single_fallback"` 标记（可观测性改进，
   待排期，不改交易逻辑）。
3. surge 顶部追单：runner_gate 已在 01:49:55 正确拦过一次（score=15 无
   fresh impulse）；01:50:53 评分跳变到 69.2 后绕过节流入场，追在局部顶。
   属入场信号质量问题，与项 16「入场信号重构」方向一致。

## 7. 验证方式

- 事件复核：`docker exec hermes-trader grep '2026-08-26T01:5' /data/events.jsonl | grep -i fartcoin`
- 日志复核：`docker exec hermes-trader grep -E '2026-08-26 01:5[0-4]' /data/trading-loop.log.bak-20260902 | grep -i fartcoin`
- 代码复核：`hermes_trader/agents/dsl_exit.py` `_effective_max_loss()`（spot_cap/roe_cap min 逻辑）
