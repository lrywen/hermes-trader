# runner_gate fresh_impulse 优化（D1–D5）

> 状态：**已部署上线，48h shadow 观察中**
> 变更日期：2026-08-22 ~ 2026-08-23
> 核心文件：[executor.py](file:///home/ldy/hermes-trader/hermes_trader/agents/executor.py)
> 生成日期：2026-08-23

---

## 0. 结论先行

把 runner_gate 的 `fresh_impulse` 从「**必须同时有 breakout 和 volumeSpike**」改为「**breakout 单独即可触发**」，消除了对成交量维度的双重门槛：

| 指标 | 变更前 | 变更后 |
|---|---:|---:|
| fresh_impulse 公式 | `(volume and breakout) or (volume and burst) or (burst and score≥30)` | `breakout or (volume and burst) or (burst and score≥30)` |
| 反事实新增放行（792 个 LONG 信号样本） | — | **+11 个**（+1.4pp 放行率） |
| 新增信号前向模拟（10 个去重） | — | **50% 胜率，+1.24R，avg +0.12R** |
| 本地单元测试 | — | **11/11 通过** |

`volumeSpike`（z ≥ 2.0σ）**保留不删**——它在另外三条路径上仍不可替代（见 §5）。

---

## 1. 背景

C3 阶段对 runner_gate 入口闸做反事实回放时发现：`fresh_impulse` 旧公式要求价格突破（breakout）**和**成交量尖峰（volumeSpike）同时成立。但 `breakout_fired` 自身的触发条件已经包含：

- RVOL ≥ 1.5x（相对均量）
- 连续 2 根已收盘 K 线站稳区间边缘之外

也就是说 breakout 在内部已经做过一次成交量确认。再外挂要求 `volumeSpike`（20 根 5m K 线均量的 z-score ≥ 2.0σ）相当于对**同一个成交量维度**设置了两道门槛，把一批「成交量达标但没到 2σ 极值」的有效突破误杀。反事实回放显示这部分约占全部 LONG 信号的 1.4pp。

---

## 2. D1–D5 变更清单

### D1：公式修改（核心）

[executor.py:1897-1907](file:///home/ldy/hermes-trader/hermes_trader/agents/executor.py#L1897-L1907)

```python
# 旧
fresh_impulse = (volume and breakout) or (volume and burst) or (burst and score >= min_score)

# 新
fresh_impulse = breakout or (volume and burst) or (burst and score >= min_score)
```

设计依据（写进了代码注释）：
- **breakout 单独成立**：breakout_fired 已内置 RVOL≥1.5x + 双 K 线收盘确认，成交量自证，额外的 2σ volumeSpike 属于冗余双重门槛。
- **volume+burst 保留**：没有成交量的 burst 只是低流动性插针；放量 burst 才代表机构参与。
- **burst+score≥min_score 保留**：强评分的 burst 即便没打出 2σ 量能尖峰，也有足够汇合度。

`structured_runner` 二次结构确认（[executor.py:1995](file:///home/ldy/hermes-trader/hermes_trader/agents/executor.py#L1995)）保持不变：

```python
structured_runner = fresh_impulse and (slow_count >= 1 or score >= min_score)
```

即 breakout 单独满足 fresh_impulse 后，仍需 `slow_burn_count≥1` 或 `score≥30` 的结构确认，防止裸突破。

### D2：决策日志

在 fresh_impulse 判断前后及各拦截分支加了 12+ 处 `logger.info`，核心汇总日志（[executor.py:1909-1915](file:///home/ldy/hermes-trader/hermes_trader/agents/executor.py#L1909-L1915)）：

```
[runner_gate] {coin} side={side} conf=.../... score=.../... slow={n} |
  vol={0/1} brk={0/1} burst={0/1} dMover={0/1} up={0/1} down={0/1}
  whale={0/1} forced={0/1} → fresh_impulse={0/1}
```

每个 BLOCKED 分支（confidence、RSI 超买、extension 超延伸、short 结构、无结构等）都有独立日志，便于线上复盘归因。

### D3：本地单元测试

新增 [test_runner_gate_breakout.py](file:///home/ldy/hermes-trader/scripts/test_runner_gate_breakout.py)，11 个用例覆盖：

| # | 用例 | 预期 |
|---|---|---|
| 1 | breakout only，无 volumeSpike | ADMIT（本次核心） |
| 2 | breakout only + 低分 + slow 结构 | ADMIT |
| 3 | breakout only + 低分 + 无结构 | BLOCK |
| 4 | volume+burst 经典组合 | ADMIT |
| 5 | burst+高分（无 volume） | ADMIT |
| 6 | burst+低分（无 volume/breakout） | BLOCK |
| 7 | 纯趋势无 impulse | BLOCK（late chase） |
| 8 | breakout 但 confidence 过低 | BLOCK |
| 9 | breakout 但 RSI4h 超买 | BLOCK |
| 10 | breakout 但 extension 超延伸 | BLOCK |
| 11 | daily_mover 路径 | ADMIT |

结果：**11 passed, 0 failed**。

### D4：Docker 重建部署 + 反事实验证

- `docker compose build` + `up -d` 重建上线。
- 对 792 个 LONG execute 事件做新旧公式反事实回放：新公式多放行 **11 个**信号，放行率 +1.4pp。

### D5：volumeSpike 是否删除——结论：保留

volumeSpike（z ≥ 2.0σ）虽然在 breakout 分支变冗余，但在另外三处不可替代：

1. **fresh_impulse 的 `volume AND burst` 分支**（[executor.py:1907](file:///home/ldy/hermes-trader/hermes_trader/agents/executor.py#L1907)）：momentumBurst 本身不含成交量确认，必须靠 volumeSpike 区分放量 burst 和低流动性插针。
2. **structured_daily_mover 路径**（[executor.py:1989-1994](file:///home/ldy/hermes-trader/hermes_trader/agents/executor.py#L1989-L1994)）：OR 选项之一。
3. **O'Neil breakout force-execute 旁路**（[executor.py:525-527](file:///home/ldy/hermes-trader/hermes_trader/agents/executor.py#L525-L527)、[L1824-1826](file:///home/ldy/hermes-trader/hermes_trader/agents/executor.py#L1824-L1826)）：要求 `volume_spike_fired AND (breakout OR slow-burn)`。

冗余仅存在于旧公式的 `volume AND breakout` 分支，已由 D1 消除。删除 volumeSpike 会破坏上述三条路径。

---

## 3. E1：11 个 breakout-only 信号前向盈亏报告

这 11 个信号是反事实信号（旧公式下被 runner_gate 拦截、未实际成交），无法从实盘成交日志取盈亏。改用 **Hyperliquid candleSnapshot API** 拉取信号时刻后的 K 线，按策略默认 bracket 做前向模拟：

- 止损 SL = 1.5 × ATR14(4h)，止盈 TP = 1.0 × ATR14(4h)（与 [server.py](file:///home/ldy/hermes-trader/hermes_trader/agents/server.py) 线上挂单默认一致）
- 入场 = 信号后首根 5m K 线 open
- 持有窗口 = 6h（72 根 5m K 线），同 bar 先判止损
- 15 分钟同币去重后，11 个原始信号落到 **10 个**可模拟样本

脚本：[breakout_only_forward.py](file:///home/ldy/hermes-trader/scripts/breakout_only_forward.py)，结构化结果：[breakout_forward_results.json](file:///home/ldy/hermes-trader/scripts/breakout_forward_results.json)。

### 3.1 逐笔明细

| coin | time(UTC) | score | R | MFE_R | MAE_R | bars | 退出 |
|---|---|---:|---:|---:|---:|---:|---|
| CASHCAT | 08-20 16:46 | 10.0 | -0.62 | +0.18 | -0.71 | 72 | timeout |
| BOME | 08-21 07:08 | 26.8 | **+0.67** | +0.70 | 0.00 | 4 | target |
| BRETT | 08-21 07:36 | 24.7 | **+0.67** | +0.69 | -0.30 | 26 | target |
| XPL | 08-21 08:37 | 32.2 | **+0.67** | +0.71 | -0.47 | 13 | target |
| ASTER | 08-21 19:11 | 36.5 | **+0.67** | +0.67 | -0.13 | 30 | target |
| INJ | 08-22 05:25 | 36.7 | -0.62 | +0.20 | -0.80 | 72 | timeout |
| UNI | 08-22 05:26 | 30.7 | -0.03 | +0.39 | -0.28 | 72 | timeout |
| FARTCOIN | 08-22 18:16 | 23.3 | -0.54 | +0.28 | -0.72 | 72 | timeout |
| CASHCAT | 08-22 18:55 | 26.4 | +0.44 | +0.59 | 0.00 | 72 | timeout |
| POL | 08-22 19:38 | 23.3 | -0.06 | +0.14 | -0.44 | 64 | timeout |

### 3.2 汇总

| 指标 | 值 |
|---|---:|
| 样本数 | 10 |
| 胜（达 target） | 4（40%） |
| 正 R（含 timeout 浮盈收盘） | 5（50%） |
| 总 R | **+1.24R** |
| 平均 R | **+0.12R** |
| 达 target 平均 | +0.67R × 4 |
| timeout 平均 | -0.24R × 6 |

### 3.3 观察

- 4 笔达标信号全部在 30 根 K 线（2.5h）内快速触达目标，没有拖累。
- 多笔 timeout 盘中曾有可观浮盈（CASHCAT 08-22 +0.59R、UNI +0.39R、FARTCOIN +0.28R），最终被静态 bracket 拖到超时微亏/小亏。这暗示若叠加 DSL 的动态追踪止盈（retrace 分级），结果可能进一步改善——本次前向模拟为保持可复现用的是静态 bracket。
- 最大回撤来自 INJ（-0.62R，MAE -0.80R）和 CASHCAT 08-20（-0.62R）。
- **结论：breakout-only 信号整体边际为正，质量可接受，D1 放行方向正确。**

---

## 4. E2：其他可优化信号特征排查

对 runner_gate 全量拦截信号做了下钻，脚本：[analyze_gate_optimization.py](file:///home/ldy/hermes-trader/scripts/analyze_gate_optimization.py)、[analyze_gate_optimization_b.py](file:///home/ldy/hermes-trader/scripts/analyze_gate_optimization_b.py)、[crowded_breakout_forward.py](file:///home/ldy/hermes-trader/scripts/crowded_breakout_forward.py)。

798 个 LONG execute 事件中，fresh_impulse=TRUE 共 142 个（17.8%）。

### 4.1 已排除的方向

| 特征 | 数量 | 结论 |
|---|---:|---|
| **volume-only**（无 breakout/burst） | 74 | score 中位数仅 27。单独放量无价格动量，不建议放行。 |
| **burst-only**（无 volume/breakout） | 56 | 其中 43 个 score≥30 已被新公式 `burst AND score≥30` 放行；剩余 13 个低分，不放行。 |
| max positions reached | 14 | 持仓上限，仓位管理范畴，非信号质量问题。 |
| OI cap | 5 | 持仓上限，非闸门优化范畴。 |
| late trend-only chase | 11 | RSI/extension 晚入场拦截，属正确保护。 |

### 4.2 发现的下一优化点：LONG_CROWDED 拥挤否决

fresh_impulse 通过后，最大的可识别额外瓶颈是 [risk_gates.py:316-355](file:///home/ldy/hermes-trader/hermes_trader/agents/risk_gates.py#L316-L355) 的 **with-crowd 拥挤否决**：

- 当 funding regime = `LONG_CROWDED` 且 side = long（顺拥挤方向），要求 `confidence ≥ crowded_with_min_conf`（线上 = **0.80**），否则拦截。
- 共 **44 个 fresh=TRUE 信号**被该闸拦截，其中 **25 个是 breakout**。
- 被拦 breakout 的 confidence 全部落在 [0.70, 0.80)，其中 15 个在 [0.75, 0.80)。

#### 被拦 breakout 前向模拟（21 个去重样本）

| 指标 | 值 |
|---|---:|
| 样本数 | 21 |
| 胜率 | **52%**（11 胜） |
| 总 R | **+1.23R** |
| 平均 R | **+0.06R** |
| confidence 区间 | 全部 [0.70, 0.80)，15 个 ≥0.75 |

逐笔亮点：8 笔快速达 target（BOME/ZEC/FARTCOIN/PEOPLE/TURBO/HEMI/ZORA/TRUMP，多在 10 根 K 线内 +0.67R）；2 笔触止损（STX、PEOPLE 各 -1.00R）；其余 timeout 小亏。

#### 建议

这是一个**逆风保护闸**（2026-06-06 曾发生拥挤反转日亏损），不能简单下调阈值。建议作为下一优化项：

1. **优先 shadow 验证**：对 breakout 信号单独记录「若放宽 crowded_with_min_conf 到 0.75 会放行哪些信号及其后续表现」，积累至少 2 周样本。
2. 若 shadow 数据持续为正，可考虑给 breakout 加一个带条件的 bypass（如 `breakout AND confidence≥0.75 AND score≥30`），而非全局下调阈值。
3. 当前**不做代码变更**，等 shadow 数据。

---

## 5. 涉及文件

| 文件 | 变更 |
|---|---|
| [hermes_trader/agents/executor.py](file:///home/ldy/hermes-trader/hermes_trader/agents/executor.py) | D1 公式 L1907；D2 日志 L1909-1915 及各 BLOCKED 分支；docstring L1860-1866 |
| [scripts/test_runner_gate_breakout.py](file:///home/ldy/hermes-trader/scripts/test_runner_gate_breakout.py) | D3 新增，11 个单测 |
| [scripts/breakout_only_forward.py](file:///home/ldy/hermes-trader/scripts/breakout_only_forward.py) | E1 前向盈亏模拟 |
| [scripts/breakout_forward_results.json](file:///home/ldy/hermes-trader/scripts/breakout_forward_results.json) | E1 结构化结果 |
| [scripts/analyze_gate_optimization.py](file:///home/ldy/hermes-trader/scripts/analyze_gate_optimization.py) | E2 拦截统计 |
| [scripts/analyze_gate_optimization_b.py](file:///home/ldy/hermes-trader/scripts/analyze_gate_optimization_b.py) | E2 下钻 |
| [scripts/crowded_breakout_forward.py](file:///home/ldy/hermes-trader/scripts/crowded_breakout_forward.py) | E2 拥挤闸代价量化 |

---

## 6. 后续跟进

- **线上观察**：新公式已部署，持续跟踪 breakout-only 信号的真实成交表现（对比 E1 前向模拟）。
- **48h shadow 窗口**：约 2026-08-22 23:05 UTC 启动，进行中。
- **E2 建议**：LONG_CROWDED 对 breakout 信号放宽阈值 → 先 shadow 2 周，再决定是否落地。
- **动态止盈**：E1 中多笔 timeout 信号盘中浮盈可观，可评估 DSL 追踪止盈对这类信号的改善（独立课题）。
