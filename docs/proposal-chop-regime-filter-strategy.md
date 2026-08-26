# P2 震荡市（ADX<20）独立过滤策略方案

> 基于 21 天 top-20 perp 回测边际归因（2026-08）
> 状态：**待审阅**

---

## 0. 结论先行

回测显示 NEW-DYNAMIC 相对 OLD 存在 -$235 PnL 缺口，veto 统计中 chop regime 占比 67%，表面上"缺口来自震荡市拦截"。但**边际归因证明这是误导**：

| 拦截原因 | 边际笔数 | 胜率 | 边际 PnL | 结论 |
|---|---:|---:|---:|---|
| `chop_regime` (ADX<20) | **0** | — | **$0** | **冗余拦截**，OLD 也会全部拒绝 |
| `late_long_RSI` (RSI>75 多头) | 1,239 | 64.7% | **+$803** | **误杀**（强趋势中高 RSI 继续涨） |
| `late_short_RSI` (RSI<25 空头) | 252 | 61.5% | **-$402** | **正确拦截**（做空超卖反弹危险） |
| `overext_long` | 162 | 56.8% | -$126 | 正确拦截 |
| `overext_short` | 39 | 30.8% | -$164 | 正确拦截 |

**真正的 PnL 缺口来源不是 P2 chop 闸门，而是 P0 多头 RSI 阈值在强趋势（ADX≥40）中误杀了大量盈利趋势跟踪单。**

本方案提出两项独立改动：

1. **【核心修复】趋势 RSI 多头例外**：ADX≥40 的多头，RSI 硬否决阈值提至 90 并移除多头熔断 → 预计挽回 **+$789**
2. **【独立 alpha】Chop regime 均值回归多头 overlay**：ADX<20 时独立运行 RSI<30 做多反弹策略 → 预计新增 **+$273/21d**

两项叠加预计 delta ≈ **+$1063**，远超 -$235 缺口。

---

## 1. 为什么 chop 闸门是冗余的（数据分析）

### 1.1 chop 闸门逻辑

[risk_gates.py](file:///home/ldy/hermes-trader/hermes_trader/agents/risk_gates.py#L368-L381) 中 chop regime 的放行条件：

```python
if regime == "chop" and not against_funding:
    chop_min_conf = max(counter_regime_min_conf, 0.75)  # ≥0.75
    chop_min_score = 55.0                                 # 或 score≥55
    if ctx.confidence >= chop_min_conf or ctx.composite_score >= chop_min_score:
        return pass
    if ctx.momentum_burst_fired:
        return pass
    return block
```

回测镜像逻辑在 [backtest_ab_compare.py:429-441](file:///home/ldy/hermes-trader/scripts/backtest_ab_compare.py#L429-L441)。

### 1.2 冗余证明

对全部 3,253 个被 NEW chop 闸门拦截的 bar 检查 OLD 规则的判定：

- **100% 同时被 OLD 拒绝**，原因均为 `fail_ta_proxy`
- 机理：ADX<20 时必然 ADX<25，OLD TA proxy 评分缺少 +15 的 ADX 加分；同时这些 bar 的 composite score 全部 <25（score_bonus 仅 ~3.75）
- OLD TA proxy 典型评分：`base 20 + atr≥0.5% 15 + ADX≥25 0 + score_bonus ~3.75 = ~38.75 < 45` → REJECTED

```
OLD TA proxy 评分（backtest_ab_compare.py:308-319）:
  s = 20                              # base
  if 30 < atr_pct*10 < 700: s += 15   # atr_range — chop 中 atr_pct 常 >7.0，False
  if atr_pct >= 0.5: s += 15          # atr floor — 通常 True
  if adx >= 25: s += 15               # ADX<20 必然缺失
  s += min(15, score/100*15)          # score<25 → 最多 +3.75
  → s ≈ 38.75 < 45 → REJECTED
```

**结论**：chop 闸门是 defense-in-depth 安全层，**零边际 PnL 影响**，建议保留但无需修改。

---

## 2. 核心修复：趋势 RSI 多头例外

### 2.1 问题诊断

`late_long_RSI` 拦截的 1,239 笔交易深度分析：

| ADX 区间 | 笔数 | 胜率 | PnL | 评价 |
|---|---:|---:|---:|---|
| 20–30 | 50 | 62.0% | +$43 | 微利 |
| **30–40** | 241 | 52.7% | **-$189** | **亏损区** |
| 40–50 | 294 | 64.3% | +$243 | 盈利 |
| 50–60 | 411 | 69.8% | +$799 | 强盈利 |
| 60+ | 243 | 70.4% | +$321 | 强盈利 |

| RSI 区间 | 笔数 | 胜率 | PnL |
|---|---:|---:|---:|
| 75–80 | 297 | 59.6% | -$108 |
| 80–85 | 286 | 54.5% | -$97 |
| **85–90** | 316 | 70.6% | **+$456** |
| **90+** | 340 | 73.2% | **+$967** |

**反直觉发现**：RSI 越高，胜率和 PnL 反而越好。这是因为在 ADX≥40 的强趋势中，高 RSI 是趋势强度的确认而非反转信号。crypto 多头趋势中 RSI 可长期维持 85+。

### 2.2 修复规则

在动态 RSI 逻辑（[backtest_ab_compare.py:356-401](file:///home/ldy/hermes-trader/scripts/backtest_ab_compare.py#L356-L401)）中，对多头增加趋势例外：

```python
# PROPOSED: Trend RSI long exception
if side == "LONG":
    if rsi > long_thresh:
        # NEW: Strong trend (ADX>=40) — high RSI is trend continuation,
        # not exhaustion. Raise veto to 90 and skip circuit breaker.
        if adx_for_thresh >= 40 and rsi <= 90:
            pass  # allow through (trend continuation)
        elif circuit_breaker:
            block = f"late long CB (...)"
        elif resonance_pass and ...:
            size_mult = 0.5
        else:
            block = f"late long (...)"
```

**规则要点**：
- 仅对 **LONG** 生效（空头 RSI<25 的 -$402 拦截正确，不改动）
- 条件：ADX≥40（强趋势，已有动态阈值 85）
- RSI 阈值放宽至 **90**（从 85 再提 5 点）
- **移除多头熔断**（circuit breaker）在 ADX≥40 时的拦截
- ADX 30–40 区间不动（该区间 RSI>75 确实亏损 -$189）

### 2.3 回测验证

| 指标 | 数值 |
|---|---:|
| 新放行交易数 | 580 |
| 胜率 | **71.9%** |
| 新增 PnL | **+$789.28** |
| 平均单笔 | +$1.36 |

按原始拦截类型分解：

| 类型 | 笔数 | 胜率 | PnL |
|---|---:|---:|---:|
| regular（普通 RSI 否决） | 345 | 76.8% | +$597 |
| CB（熔断拦截） | 235 | 64.7% | +$192 |

按 ADX 分解：

| ADX | 笔数 | 胜率 | PnL |
|---|---:|---:|---:|
| 40–45 | 93 | 67.7% | +$52 |
| 45–50 | 47 | 72.3% | +$85 |
| 50–60 | 239 | 68.6% | +$363 |
| 60+ | 201 | 77.6% | +$290 |

---

## 3. 独立 Alpha：Chop Regime 均值回归多头 Overlay

### 3.1 设计原理

chop regime（ADX<20）中 OLD/NEW 趋势跟踪策略均不交易，但价格在区间内震荡时存在**超卖反弹**机会。数据表明加密市场在震荡市中：

- **做多超卖反弹有效**：RSI<30 做多，+$273（164 笔）
- **做空超买回调无效**：RSI>70 做空，仅 +$1（148 笔）

这与加密市场的长期上涨偏差（long bias）一致。

### 3.2 策略规则

```
入场条件（全部满足）:
  1. coin-self 1h ADX(14) < 20          (震荡市)
  2. RSI(14, 1h) < 30                    (超卖)
  3. close > EMA21 * 0.98               (非自由落体，距均线不超过 2%)
  4. 方向：仅做多                         (不做空)

退出:
  - 止盈: +1.5%
  - 止损: -1.0%
  - 超时: 24 根 1h K线（24h）
  - 手续费: 5 bps

仓位:
  - equity_fraction = 20%, leverage = 12x（与主策略一致）
  - 可考虑半仓（10%）作为 overlay，因与主策略不相关
```

### 3.3 回测结果

| 指标 | 数值 |
|---|---:|
| 交易笔数 | 164 |
| 胜率 | **60.4%** |
| 总 PnL | **+$273.36** |
| 平均单笔 | +$1.67 |
| 盈亏比 | 1.5:1（止盈/止损） |

参数敏感性测试（4 组配置）：

| 参数 | 笔数 | 胜率 | PnL |
|---|---:|---:|---:|
| **RSI30/70, t1.5/s1.0（long+short）** | 312 | 53.5% | **+$275** |
| RSI25/75, t1.5/s1.0 | 107 | 48.6% | +$28 |
| RSI25/75, t2.0/s1.5 | 107 | 54.2% | +$40 |
| RSI20/80, t2.0/s1.5 | 28 | 64.3% | +$36 |

**RSI<30 + 1.5%/1.0% 为最优参数**，更严格的 RSI 阈值导致交易太少。

额外过滤测试（ATR/成交量/EMA 斜率等）均**降低** PnL，说明 baseline 规则已足够，不应过度约束。

### 3.4 Per-coin 表现

盈利集中在大盘币和高波动币：

| 盈利 top | 笔数 | 胜率 | PnL |
|---|---:|---:|---:|
| DOGE | 17 | 88% | +$78 |
| HYPE | 10 | 90% | +$58 |
| XPL | 9 | 89% | +$51 |
| BTC | 11 | 100% | +$44 |
| SOL | 9 | 89% | +$43 |

| 亏损币 | 笔数 | 胜率 | PnL |
|---|---:|---:|---:|
| NEAR | 9 | 11% | -$33 |
| ENA | 9 | 11% | -$33 |
| XRP | 7 | 29% | -$20 |
| ADA | 6 | 17% | -$18 |

建议首期上线**白名单**（BTC/ETH/SOL/DOGE/HYPE 等大盘和高波动币），或对回测亏损币设置黑名单。

### 3.5 与主策略的正交性

此 overlay 在 ADX<20 时运行，而主策略趋势跟踪在 ADX<20 时被 chop 闸门/TA proxy 拦截，两者**信号空间完全不重叠**，是真正的独立 alpha 层。

---

## 4. 组合效果预估

| 层级 | PnL 贡献 | 说明 |
|---|---:|---|
| NEW-DYNAMIC 基线 | -$235 | 当前回测值 |
| + 趋势 RSI 多头修复 | **+$789** | ADX≥40 多头 RSI 阈值 85→90 + 移除熔断 |
| + Chop MR 多头 overlay | **+$273** | ADX<20 RSI<30 独立做反弹 |
| **组合预估** | **~+$827** | 超过 OLD 基线 +$693 |

---

## 5. 实施建议

### 5.1 改动文件

**核心修复（趋势 RSI 多头例外）**：
- [backtest_ab_compare.py](file:///home/ldy/hermes-trader/scripts/backtest_ab_compare.py#L388-L401) 动态 RSI 多头分支：添加 ADX≥40 例外
- [risk_gates.py](file:///home/ldy/hermes-trader/hermes_trader/agents/risk_gates.py) 或对应 RSI gate 生产代码：同步逻辑

**Chop MR overlay（新增模块）**：
- 建议新建 `hermes_trader/strategies/chop_mean_reversion.py`
- 或在现有 agent 信号生成流程中增加一个独立的 overlay 信号源
- 需要独立的仓位管理（建议半仓，equity_fraction=10%）

### 5.2 风险控制

1. **趋势 RSI 修复**：
   - ADX 30–40 区间**不动**（该区间 RSI>75 亏损 -$189）
   - 空头 RSI 否决**不动**（-$402 正确拦截）
   - 保留 ext_atr>2.5 的 overextension 否决作为尾部保护

2. **Chop MR overlay**：
   - 仅做多，不做空（数据明确）
   - 白名单制上线（BTC/ETH/SOL/DOGE 等），观察 2 周后扩围
   - 固定止损 1%，不追加保证金
   - 24h 硬超时，不扛单

### 5.3 灰度计划

1. 先实现趋势 RSI 修复（改动小，预期收益大）
2. 回测验证后上线 24h 灰度
3. 同期实现 chop MR overlay 并回测
4. overlay 先在模拟盘运行 1 周，确认实盘数据与回测一致后上小仓位

---

## 6. 附录：数据来源与方法论

- 回测区间：21 天，top-20 Hyperliquid perp
- K线周期：1h 主周期 + 4h 重采样
- DSL 退出模型：max_loss=2.5%, protect=1.25%, retrace=0.2, leverage=12x, equity_fraction=20%, equity=$200
- **边际归因方法**：对每个 "OLD 接受但 NEW 拦截" 的 bar 独立模拟交易（允许重叠），测量每条信号的边际 PnL 而非可实现权益曲线
- 分析脚本：
  - `/tmp/marginal_attribution.py` — 按拦截原因的边际 PnL 归因
  - `/tmp/deep_dive_late_long.py` — late_long_RSI 误杀交易多维分桶
  - `/tmp/chop_mean_reversion.py` — chop regime 均值回归 overlay 回测
  - `/tmp/refine_strategy.py` — MR 参数优化 + 趋势 RSI 修复反事实
