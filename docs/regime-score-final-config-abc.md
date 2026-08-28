# Regime Score 策略最终配置（A+B+C）

> 状态：**已生产落地**（Plan A/B/C 均已上线，见 `.agent-config.json` 的 `plan_b.enabled=true`、`dsl_exit.regime_aware.enabled=true`）
> 回测区间：30 天，top-20 Hyperliquid perp，1h 主周期 + 4h 重采样
> 回测引擎：[backtest_ab_compare.py](file:///home/ldy/hermes-trader/scripts/backtest_ab_compare.py)
> 生成日期：2026-08-22（状态更新于 2026-08-23）

---

## 0. 结论先行

三项优化 A+B+C 叠加后，REGIME 列从基线亏损翻正：

| 指标 | OLD | NEW-STRICT | NEW-DYNAMIC | **NEW-REGIME (A+B+C)** |
|---|---:|---:|---:|---:|
| 交易笔数 | — | — | — | **2493** |
| 胜率 | — | — | — | **34.1%** |
| PnL | — | — | — | **+$218.22** |
| ROE（$200 本金） | — | — | — | **109.1%** |

按 regime 拆分：

| Regime | 笔数 | 胜率 | PnL | 平均仓位 |
|---|---:|---:|---:|---:|
| STRONG_TREND | 1748 | 36.5% | **+$390.29** | x1.00 |
| TREND | 462 | 31.4% | -$116.89 | x0.80 |
| NEUTRAL | 236 | 23.7% | -$44.47 | x1.00 |
| CHOP | 47 | 21.3% | -$10.70 | x0.50 |

退出原因分布（REGIME 全样本）：

| 退出原因 | 笔数 | PnL |
|---|---:|---:|
| max_loss 0.4%（CHOP/NEUTRAL） | 217 | — |
| max_loss 0.8%（TREND/STRONG_TREND） | 1420 | — |
| trailing_stop | 856 | — |

---

## 1. Regime Score 模型

### 1.1 加权组件（Plan A 最终权重）

[backtest_ab_compare.py:86-92](file:///home/ldy/hermes-trader/scripts/backtest_ab_compare.py#L86-L92)

| 组件 | 权重 | 归一化区间 | 说明 |
|---|---:|---|---|
| ADX(14) | **0.25** | ADX 15→0, 45→1 | 趋势强度（由 0.35 下调） |
| ATR% | **0.225** | 0.2%→0, 1.0%→1 | 波动率确认 |
| EMA 对齐 | **0.175** | \|EMA8-EMA21\|/EMA21，0%→0, 0.5%→1 | 趋势方向 |
| 价格延伸 | **0.175** | \|close-EMA21\|/ATR，0→0, 2.0→1 | 趋势确认 |
| OBV | **0.175** | 离散 0/0.3/1.0 | 量能方向 |

权重总和 = 1.0。

### 1.2 阈值映射（Plan A 最终阈值）

[backtest_ab_compare.py:191-198](file:///home/ldy/hermes-trader/scripts/backtest_ab_compare.py#L191-L198)

| Regime | 分数区间 | 变动 |
|---|---|---|
| STRONG_TREND | score ≥ **0.70** | 不变 |
| TREND | **0.55** ≤ score < 0.70 | 下界 0.45→0.55 |
| NEUTRAL | **0.40** ≤ score < 0.55 | 区间整体上移 |
| CHOP | score < **0.40** | 上界 0.25→**0.40** |

**改动理由**：原 ADX 权重 0.35 + CHOP 上界 0.25 导致 CHOP bucket 在 30 天内 0 笔交易，所有临界震荡 tape 被推入 NEUTRAL。降低 ADX 权重到 0.25、上移 CHOP 上界到 0.40 后，CHOP 捕获约 47 笔真实震荡交易。

### 1.3 各 Regime 入场参数

[backtest_ab_compare.py:107-128](file:///home/ldy/hermes-trader/scripts/backtest_ab_compare.py#L107-L128)

| Regime | long_thresh | short_thresh | ext_long | ext_short | size_mult | mr_overlay |
|---|---:|---:|---:|---:|---:|:---:|
| STRONG_TREND | 95.0 | 5.0 | 3.5 | 3.5 | 1.0 | ✗ |
| TREND | 85.0 | 15.0 | 3.0 | 3.0 | 1.0 | ✗ |
| NEUTRAL | 75.0 | 25.0 | 2.5 | 2.5 | 1.0 | ✗ |
| CHOP | 68.0 | 32.0 | 1.8 | 1.8 | **0.5** | ✓ |

- `long_thresh`：RSI 高于此值否决做多（强趋势中放宽到 95）
- `short_thresh`：RSI 低于此值否决做空
- `ext_long/short`：close 相对 EMA21 的 ATR 倍数超过此值否决
- `size_mult`：仓位乘数（CHOP 半仓）
- `mr_overlay`：是否启用均值回归 overlay（CHOP）

---

## 2. Plan B — TREND 中段 RSI 40-60 降仓

[backtest_ab_compare.py:601-607](file:///home/ldy/hermes-trader/scripts/backtest_ab_compare.py#L601-L607)

```python
# TREND 中段 RSI 40-60 无方向优势（多空双向均被 max_loss 止损），
# 半仓以降低风险，同时保留信号覆盖。
if rsi_variant == "regime" and regime_label == "TREND" and 40.0 <= rsi < 60.0:
    size_mult = min(size_mult, 0.5)
```

| 指标 | Plan B 前 | Plan B 后 |
|---|---:|---:|
| TREND 平均仓位 | x1.00 | **x0.80** |
| TREND PnL | -$270 | **-$116.89**（收窄 57%） |

仅影响 `regime_label == "TREND"` 且 RSI ∈ [40, 60) 的入场，取 `size_mult` 与 0.5 的较小值。

---

## 3. Plan C — Regime 自适应硬止损

[backtest_ab_compare.py:718-734](file:///home/ldy/hermes-trader/scripts/backtest_ab_compare.py#L718-L734)

```python
eff_max_loss = max_loss_pct  # live config 默认 0.4%
if rsi_variant == "regime" and regime_label in ("TREND", "STRONG_TREND"):
    eff_max_loss = max(max_loss_pct, 0.8)  # 趋势段放宽到 0.8%
```

| Regime | max_loss_pct | 逻辑 |
|---|---:|---|
| CHOP | **0.4%** | 震荡市紧止损，快速认错 |
| NEUTRAL | **0.4%** | 同上 |
| TREND | **0.8%** | 给趋势 1h 噪音留呼吸空间 |
| STRONG_TREND | **0.8%** | 同上，避免在 trailing protect 启动前被震出 |

**改动理由**：live config 的 `protect_pct=1.25%` 意味着价格需先走到 +1.25% 才启动追踪止损。若 max_loss 只有 0.4%，大量趋势单在 -0.4% 被止损，根本等不到 +1.25% 的保护启动。放宽趋势段 max_loss 到 0.8% 后，STRONG_TREND 贡献 +$390.29。

**DSL 两阶段退出（live config）**：

| 参数 | 值 | 来源 |
|---|---:|---|
| max_loss_pct | 0.4% / 0.8%（regime 自适应） | [.agent-config.json:34](file:///home/ldy/hermes-trader/.agent-config.json#L34) + Plan C |
| protect_pct | 1.25% | [.agent-config.json:36](file:///home/ldy/hermes-trader/.agent-config.json#L36) |
| retrace_threshold | 0.20 | [.agent-config.json:37](file:///home/ldy/hermes-trader/.agent-config.json#L37) |
| hard_timeout | 1800 min | [.agent-config.json:38](file:///home/ldy/hermes-trader/.agent-config.json#L38) |
| round-trip fee | 5 bps | [backtest_ab_compare.py:66](file:///home/ldy/hermes-trader/scripts/backtest_ab_compare.py#L66) |

回测仓位：equity=$200，equity_fraction=0.20，leverage=12x，size_mult 按 regime。

---

## 4. 否决项：RSI<25 超卖过滤（任务1结论）

[diag_chop_rsi_filter.py](file:///home/ldy/hermes-trader/scripts/diag_chop_rsi_filter.py)

诊断结果：在 CHOP+NEUTRAL 段，RSI<25 LONG 仅 **4 笔**（全部在 NEUTRAL），净亏 -$2.16。过滤后 PnL 仅改善 +$2.16（-$93.19 → -$91.03），**无统计意义**。

CHOP/NEUTRAL 的真正亏损来源是 **RSI 35-65 中段的趋势追随单**（非超卖抄底），RSI<25 过滤无法解决：

| Regime | RSI bucket | 方向 | 笔数 | PnL |
|---|---|---|---:|---:|
| NEUTRAL | 35-50 | long | 31 | -$32.70 |
| NEUTRAL | 50-65 | long | 61 | -$17.29 |
| NEUTRAL | 50-65 | short | 54 | -$17.61 |
| CHOP | 50-65 | long | 18 | -$11.86 |

**结论：不引入 RSI<25 过滤。**

---

## 5. Trailing Stop 审计结论（任务2）

[audit_trailing_stop.py](file:///home/ldy/hermes-trader/scripts/audit_trailing_stop.py)

STRONG_TREND 段 trailing_stop 触发的 664 笔交易：

| 指标 | 值 |
|---|---:|
| 平均实现盈利（剥离手续费） | **+1.60%** |
| 中位实现盈利 | +1.25% |
| 平均 MFE（峰值浮盈） | +2.82% |
| 中位 MFE | +2.21% |
| 平均 MAE（峰值浮亏） | -0.34% |
| 平均峰值回吐（giveback） | +1.22% |
| **Capture Ratio（realized/MFE）** | **56.8%** |
| 平均持仓时长 | **3.0h**（中位 2.0h） |
| 胜率 | 99.1% |
| trailing_stop PnL | +$4014.81 |
| max_loss 0.8% PnL（对比） | -$3803.58 |

**结论：trailing stop 处于 MODERATE 区间（capture 57%），不过于保守。**

- 回吐 1.22% 是 protect_pct=1.25% + retrace=20% 的数学预期（floor 锁定在 peak-0.25%，从 +2.82% 峰值回吐约 1.2% 合理）
- 持仓 74% 在 1-3h 内退出，符合 scalp 定位
- max_loss 0.8% 的亏损（-$3803）几乎被 trailing_stop 盈利（+$4015）完全对冲，净 +$211

**不建议放宽 trailing stop。** 放宽 protect_pct 或 retrace 会增加 giveback 但不一定提高 capture——MFE 中位数仅 2.21%，大量交易峰值本身就不大。

---

## 6. 生产落地映射

> ⚠️ 当前生产代码使用与回测**不同**的 regime 架构。A+B+C 已在回测引擎中验证，但落地到生产需要以下映射工作。

### 6.1 架构差异

| 维度 | 回测（A+B+C） | 生产现状 |
|---|---|---|
| Regime 分类 | 5 组件加权 score → 4 档 | EMA20/EMA50 + ADX(14) → up/down/neutral/chop |
| 分类代码 | [backtest_ab_compare.py:131](file:///home/ldy/hermes-trader/scripts/backtest_ab_compare.py#L131) | [market_regime.py](file:///home/ldy/hermes-trader/hermes_trader/agents/market_regime.py) |
| RSI 入场阈值 | 按 regime 动态查表 | [risk_gates.py](file:///home/ldy/hermes-trader/hermes_trader/agents/risk_gates.py) composite_score 闸门 |
| DSL 止损 | regime 自适应 0.4%/0.8% | [.agent-config.json](file:///home/ldy/hermes-trader/.agent-config.json) 全局 `max_loss_pct=0.4` |
| 仓位调节 | TREND RSI 40-60 → 0.5x | `conviction_sizing`（当前关闭） |

### 6.2 生产文件改动清单

| 优化 | 生产文件 | 所需改动 |
|---|---|---|
| **A** Regime score + 阈值 | [market_regime.py](file:///home/ldy/hermes-trader/hermes_trader/agents/market_regime.py) | 新增 5 组件加权 score 模型，或映射现有 4 态到 4 档 |
| **A** Regime 参数表 | 新增配置块 | 在 `.agent-config.json` 或 `config.py` 中定义 `regime_params` 表（long/short_thresh, ext, size_mult） |
| **B** TREND 中段降仓 | [executor.py](file:///home/ldy/hermes-trader/hermes_trader/agents/executor.py) `_conviction_multiplier` | TREND regime + RSI 40-60 时 size_mult=0.5 |
| **B** RSI 阈值查表 | [risk_gates.py](file:///home/ldy/hermes-trader/hermes_trader/agents/risk_gates.py) | 按 regime 标签查 long_thresh/short_thresh，替代固定 75/25 |
| **C** Regime 自适应止损 | [dsl_exit.py](file:///home/ldy/hermes-trader/hermes_trader/agents/dsl_exit.py) + [executor.py:201](file:///home/ldy/hermes-trader/hermes_trader/agents/executor.py#L201) `select_exit_params` | 注册 DSL 时按 regime 传入 max_loss_pct（0.4 或 0.8） |

### 6.3 推荐的配置块结构

```json
{
  "regime_score": {
    "enabled": true,
    "weights": {
      "adx": 0.25,
      "atr": 0.225,
      "ema_align": 0.175,
      "price_ext": 0.175,
      "obv": 0.175
    },
    "thresholds": {
      "strong_trend": 0.70,
      "trend": 0.55,
      "neutral": 0.40
    },
    "params": {
      "STRONG_TREND": {
        "long_thresh": 95.0, "short_thresh": 5.0,
        "ext_long_thresh": 3.5, "ext_short_thresh": 3.5,
        "size_mult": 1.0, "max_loss_pct": 0.8
      },
      "TREND": {
        "long_thresh": 85.0, "short_thresh": 15.0,
        "ext_long_thresh": 3.0, "ext_short_thresh": 3.0,
        "size_mult": 1.0, "max_loss_pct": 0.8,
        "mid_rsi_range": [40.0, 60.0], "mid_rsi_size_mult": 0.5
      },
      "NEUTRAL": {
        "long_thresh": 75.0, "short_thresh": 25.0,
        "ext_long_thresh": 2.5, "ext_short_thresh": 2.5,
        "size_mult": 1.0, "max_loss_pct": 0.4
      },
      "CHOP": {
        "long_thresh": 68.0, "short_thresh": 32.0,
        "ext_long_thresh": 1.8, "ext_short_thresh": 1.8,
        "size_mult": 0.5, "max_loss_pct": 0.4,
        "mr_overlay": true
      }
    }
  }
}
```

---

## 7. 验证脚本

| 脚本 | 用途 |
|---|---|
| [backtest_ab_compare.py](file:///home/ldy/hermes-trader/scripts/backtest_ab_compare.py) | A+B+C 主回测（OLD/STRICT/DYNAMIC/REGIME 四列对比） |
| [diag_chop_rsi_filter.py](file:///home/ldy/hermes-trader/scripts/diag_chop_rsi_filter.py) | CHOP/NEUTRAL RSI 分布归因 + RSI<25 过滤反事实 |
| [audit_trailing_stop.py](file:///home/ldy/hermes-trader/scripts/audit_trailing_stop.py) | STRONG_TREND trailing_stop MFE/MAE/capture ratio 审计 |

复现命令：

```bash
# 主回测
HERMES_BACKTEST=1 python3 scripts/backtest_ab_compare.py --days 30 --coins 20

# RSI 过滤诊断
python3 scripts/diag_chop_rsi_filter.py --days 30 --coins 20

# Trailing stop 审计
python3 scripts/audit_trailing_stop.py --days 30 --coins 20
```

---

## 8. 风险与注意事项

1. **回测 vs 生产架构差异**：A+B+C 在回测的 regime score 模型上验证，生产使用不同的 regime 分类，落地需先对齐分类逻辑
2. **NEUTRAL/TREND 仍亏损**：两档合计 -$161，Plan B 已收窄 TREND 亏损但未转正；NEUTRAL 23.7% 胜率偏低，未来可考虑 NEUTRAL 降仓或收紧阈值
3. **max_loss 0.8% 触发 1420 次**：趋势段放宽止损后被止损次数较多，但 trailing_stop 的 +$4015 对冲了 max_loss 的 -$3803
4. **样本量**：30 天 top-20，CHOP 仅 47 笔，统计置信度有限；建议扩到 60-90 天验证
5. **无滑点/资金费率**：回测仅扣 5 bps 手续费，实盘高频 1h scalp 需考虑滑点和 funding
