# Hermes 交易系统完整参数清单

> 本文档完整列出交易系统从 **Scan → TA → AI → Risk → Execute → DSL Exit** 全管线预设配置参数、16 道风控闸门及其他系统参数设定。
>
> **数据来源**：代码默认值（`config.py` / `risk_gates.py` / `executor.py` / `dsl_exit.py` 等）+ 线上生效配置 `.agent-config.json`。
> **当前线上模式**：`mode: LIVE`。

---

## 目录

1. [Scan 扫描层](#一scan-扫描层)
2. [TA 技术过滤层](#二ta-技术过滤层)
3. [AI 研究层](#三ai-研究层)
4. [Risk 风控层（16 道闸门）](#四risk-风控层16-道闸门)
5. [Execute 执行层](#五execute-执行层)
6. [DSL Exit 动态止损层](#六dsl-exit-动态止损层)
7. [其他系统参数](#七其他系统参数)

---

## 一、Scan 扫描层

来源：`hermes_trader/agents/config.py`（`TRIGGER_CONFIG`）+ `perception.py` + 环境变量。

### 1.1 触发权重（`TRIGGER_CONFIG.weights`，2026-06-02 重校准）

| 触发器 | 权重 | 说明 |
|--------|------|------|
| `trendStrength` | **0.55** | 最佳信号，lift +2.08%（原 0.10） |
| `pctMoveSpike` | **0.40** | lift +1.49% |
| `breakout` | **0.30** | lift +1.29% |
| `volumeSpike` | **0.25** | lift +1.05% |
| `momentumBurst` | **0.20** | lift +0.77%（n=9，保留温和权重） |
| `volumeBuildup1h` | **0.15** | lift +0.41%（原 0.60，过度加权） |
| `higherLows1h` | **0** | lift -0.51%，已剔除 |
| `trendFlip1h` | **0** | lift -2.10%，净亏损，已剔除 |
| `rangeCompression` | **0** | lift -3.08%，最差，已剔除 |
| `uptrendMomentum` | **0** | 对称趋势浮出（不进评分分母） |
| `downtrendMomentum` | **0** | 对称趋势浮出 |
| `dailyMover` | **0** | 对称趋势浮出 |

> 权重基于实测边际提升（fired vs not-fired ROE，n=497 笔交易）重新校准；净负收益触发器权重清零。

### 1.2 扫描阈值（`TRIGGER_CONFIG.thresholds`）

| 参数 | 值 | 说明 |
|------|-----|------|
| `sigmaThreshold` | **2.0** | 标准差阈值 |
| `trendMomentumLookback` | **72** | 5m bar（约 6h）趋势浮出回看 |
| `trendMomentumPct` | **5.0** | 6h 最低波动 %（5% 为 3.0 过度浮出后校准） |
| `breakoutLookback` | **48** | 突破回看 |
| `bbLength` | **20** | 布林带长度 |
| `bbStdDev` | **2** | 布林带标准差倍数 |
| `adxPeriod` | **14** | ADX 周期 |
| `momentumLookback` | **2** | momentum_burst 窗口（5m bar → 10 分钟） |
| `momentumPct` | **4.0** | 该窗口最低 % 波动触发 momentum_burst |
| `volBuildupRatio` | **2.5** | 4h vs 前 20h 均量（1h K 线） |
| `trendFlipBars` | **3** | 最近 N 根 1h bar 内 EMA8/21 交叉 |
| `higherLowsRequired` | **4** | 最近 6 根 1h bar 中抬高低点数量 |

### 1.3 扫描运行参数（`TRIGGER_CONFIG.scan`）

| 参数 | 值 | 说明 |
|------|-----|------|
| `minCompositeScore` | **54** | 复合分门槛（新权重校准，等价旧 35 选择率） |
| `candleInterval` | **5m** | 扫描 K 线周期 |
| `candleCount` | **100** | 拉取 K 线数量 |
| `cacheTtlMs` | **50,000** | 缓存 TTL（50s） |
| `cacheTtlMs1h` | **600,000** | 1h K 线缓存（10 分钟） |

### 1.4 扫描循环（`scripts/trading_loop.py`）

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `HERMES_SCAN_INTERVAL` | **15** | 扫描周期（秒）；HYPE 事故后由 60s 缩短至 15s，缩小 DSL 轮询盲区 |
| `HERMES_STARTUP_GRACE_S` | **12** | 启动宽限（让 HL 限流桶回充） |
| `HERMES_MIN_SCORE` | 54 | 最小复合分 |
| `HERMES_MAX_MARKETS` | **60** | 每轮 K 线拉取预算 |
| `HERMES_MAX_MARKETS_HIP3` | **25** | HIP-3 预留槽位 |
| `HERMES_MAX_MARKETS_MOVERS` | **10** | 日波动者槽位 |
| `HERMES_BATCH_SIZE` | **20** | 每批并行市场数 |
| `HERMES_BATCH_SLEEP` | **0.3** | 批间休眠（秒） |

### 1.5 浮出 / 旁路开关（线上值）

| 参数 | 值 | 说明 |
|------|-----|------|
| `trend_surface_enabled` | **true** | 对称趋势浮出 |
| `whale_scan_bypass` | **false** | 鲸鱼信号扫描旁路 |
| `runner_mover_surface.enabled` | **true** | 日波动者浮出 |
| `runner_mover_surface.min_crypto_24h_pct` | **10** | 加密 24h 波动 ≥10% |
| `runner_mover_surface.min_hip3_24h_pct` | **8** | HIP-3 24h 波动 ≥8% |
| `runner_mover_surface.min_volume_usd` | **500 万** | 最低成交额 |

---

## 二、TA 技术过滤层

来源：`hermes_trader/agents/ta_filter.py`。纯统计验证，评分门槛：
**CONFIRMED ≥ 22**，**WEAK ≥ 12**，否则 **REJECTED**。

| 因子 | 加分 | 条件 |
|------|------|------|
| 趋势对齐（看涨） | +20 | 4h/1d 趋势看涨（看涨满额） |
| 趋势对齐（看跌） | +10 | 看跌可交易但边缘低 |
| RSI(14, 4h) | +15 | 30 < RSI < 70 |
| ATR(14, 4h) | +15 | ≥ 0.5% |
| ADX(14, 4h) | +15 | ≥ 25 |
| EMA8/21 交叉 | +10 | 最近 3 根内交叉 |
| 量能确认 | +10 | 最后量 ≥ 20 均量 × 0.8 |
| 复合分 | +15 | min(15, composite/100 × 15) |

> 方向性加权：我们实测边缘偏向做多/顺势，因此看涨趋势给满额，看跌仅给半额，矛盾则不给。

---

## 三、AI 研究层

来源：`research.py`、`executor.py`、`.env.local.example`。

| 参数 | 线上值 | 说明 |
|------|--------|------|
| `OPENROUTER_MODEL` | `x-ai/grok-4.3` | AI 研究模型（默认，可覆盖 Qwen） |
| `override_requires_ai` | **true** | AI 挂掉时禁止盲升级 |
| `held_research_interval_min` | **10** | 持仓重研间隔（分钟） |
| `research_cooldown_min` | **15** | 同一候选两次研究的最小间隔（分钟，避免重复付费 LLM） |
| `min_ai_close_hold_min` | **25** | AI 最小持仓后平仓（分钟） |
| `force_execute_composite` | **30** | 结构化升级复合分门槛 |
| `composite_force_execute` | **false** | 复合分强制升级 |
| `ta_sidestep_force_execute` | **true** | TA 旁路强制升级 |
| `force_execute_slow_burn_count` | **2** | slow-burn 触发数量门槛 |
| `ta_sidestep_min_slow_burn_count` | **99** | TA 旁路 slow-burn 数量门槛；线上设 99 等于禁用该分支（slow_burn_count 不可能达到，见下） |
| `breakout_force_execute` | **false** | 突破强制升级（O'Neil） |
| `whale_force_execute` | **false** | 鲸鱼信号强制升级 |

### 3.1 `ta_sidestep_strong` 的 AND 语义（2026-08-21 收紧）

`executor.py` 的 `ta_sidestep_strong` 曾把三个条件用 `or` 连接，任意一条成立就触发升级，
`ta_sidestep_min_slow_burn_count` 因此成为**死配置**——实盘 2026-08-20 的 5 次 override
全部是 `slow_burn_count=1` 对 `min=2`，靠单条 `momentum_burst_fired` 通过。

现行语义（与相邻的 `breakout_strong` 结构一致）：

```
ta_sidestep_strong =
      ta_sidestep_force_execute
  AND slow_burn_count >= ta_sidestep_min_slow_burn_count      ← 硬门槛，不可绕过
  AND ( composite_score >= force_execute_composite
        OR momentum_burst_fired )                             ← 确认信号，二选一
```

即：**积累基础是必要条件，动能确认二选一**。调 `ta_sidestep_min_slow_burn_count`
现在会真实改变行为——设 `99` 等于彻底关闭该通道，设 `1` 只要求一根 slow-burn。

`route_verdict` 里的 `sidestep_hint` 必须与此保持 lockstep：hint 更宽松只会把候选路由到
executor 再被拒（`reason: pass_no_override`），白跑一趟研究。

---

## 四、Risk 风控层（16 道闸门）

来源：`hermes_trader/agents/risk_gates.py`。`eval_all_gates` 顺序评估，任一不过即整单拦截（不做短路，全部收集用于遥测）。

| # | 闸门 | 参数 | 线上值 | 说明 |
|---|------|------|--------|------|
| 1 | `confidence` | `min_ai_confidence` | **0.7** | 置信度下限（顺势 0.7，逆势用默认 0.8） |
| 2 | `max_concurrent` | `max_concurrent` | **10** | 最大并发仓位 |
| 3 | `notional_cap` | `max_trade_notional_usd` | **$800** | 单笔名义上限（含精度容差） |
| 4 | `daily_loss` | `max_daily_loss_usd` | **-$30** | 日亏损熔断 |
| 5 | `daily_giveback` | `daily_giveback_halt_pct` / `min_peak_usd` | **0.35** / **$25** | 盈利日回吐熔断 |
| 6 | `liquidity` | `min_market_volume_usd` / `min_hip3_volume_usd` | **$500 万** / **$500 万** | 市场流动性下限 |
| 7 | `short_liquidity` | `min_short_volume_usd` | **$5000 万** | 做空流动性（挤压风险） |
| 8 | `coin_filter` | `coin_allowlist` / `coin_blocklist` | `[]` / `[TON, TRX]` | 币种白/黑名单 |
| 9 | `cooldown` | `cooldown_min` | **30** | 交易冷却（分钟） |
| 10 | `opposite_guard` | — | — | 持仓禁止反手/加仓（金字塔） |
| 11 | `correlation` | `max_crypto_long_correlated` | **3** | 加密多头相关性上限 |
| 12 | `equity_risk` | `max_total_notional_pct` | **10.0** | 总敞口上限（聚合权益名义上限的 10%） |
| 13 | `market_regime` | `counter_regime_min_conf` / `block_counter_trend_bypass` / `crowded_with_min_conf` | **0.8** / **true** / **0.8** | 市场制度闸（逆势需高置信/评分） |
| 14 | `news` | AI `news_risk` | negative 停手 | 二元新闻熔断 |
| 15 | `debate` | `debate_gate` | enabled, min_agreement 0.6 | 多智能体共识闸（5 路投票，需 ≥3 票且比例 ≥0.6） |
| 16 | `hta_risk` | `hta_risk_gate` | enabled, fail_closed_shorts true | HTA 三方风险评审闸（熔断时空单 fail-closed，多单 fail-open） |

### 4.1 runner_entry_gate 入口闸（额外，`enabled: true`）

| 参数 | 值 | 说明 |
|------|-----|------|
| `allow_shorts` | **false** | 禁止做空 |
| `bypass_sidestep_overrides` | **true** | 是否让 sidestep override 绕过本闸门（`AND` 语义，仅对 `sidestep_override=true` 的候选生效） |
| `min_confidence` | **0.7** | 最低置信度。structural override 会把 `confidence` 抬到 `min_ai_confidence`，本闸门判的是抬升前的 `ai_confidence_raw`（无该字段时回退 `confidence`） |
| `min_composite` | **30** | 最低复合分 |
| `min_hip3_composite` | **50** | HIP-3 最低复合分 |
| `min_short_confidence` | **0.72** | 做空最低置信度 |
| `min_short_composite` | **25** | 做空最低复合分 |
| `mover_min_confidence` | **0.72** | 日波动者最低置信度 |
| `mover_min_composite` | **20** | 日波动者最低复合分 |

#### 4.1.1 fresh_impulse 公式（代码内固化，非 config）

来源：[executor.py:1897-1907](file:///home/ldy/hermes-trader/hermes_trader/agents/executor.py#L1897-L1907)。

```
fresh_impulse = breakout
             or (volume and burst)
             or (burst and score >= min_composite)
```

- `breakout` 单独即可触发：`breakout_fired` 已内置 RVOL≥1.5x + 双 K 线收盘确认，成交量自证，不再外挂要求 2σ volumeSpike（2026-08 D1 优化，消除双重成交量门槛，放行率 +1.4pp）。
- `volume` = `volume_spike_fired`（20 根 5m K 线均量 z-score ≥ 2.0σ）。
- `burst` = `momentum_burst_fired`。
- fresh_impulse 通过后仍需结构确认：`structured_runner = fresh_impulse and (slow_burn_count >= 1 or score >= min_composite)`。

完整优化过程（D1–D5、前向盈亏、后续建议）见 [runner-gate-fresh-impulse-optimization-2026-08.md](file:///home/ldy/hermes-trader/docs/runner-gate-fresh-impulse-optimization-2026-08.md)。

### 4.2 其他风控

| 参数 | 值 | 说明 |
|------|-----|------|
| `min_available_margin_pct` | **0.1** | 可用保证金下限（10%） |
| `loss_cooldown_min` | **180** | 亏损冷却（3h，防复仇交易） |
| `hip3_dex_allowlist` | `[xyz]` | HIP-3 允许 DEX |
| `hip3_dex_blocklist` | `[]` | HIP-3 禁止 DEX |

---

## 五、Execute 执行层

来源：`hermes_trader/agents/executor.py`。

### 5.1 仓位计算（线上启用 ATR 等风险 sizing）

| 参数 | 值 | 说明 |
|------|-----|------|
| `atr_risk_sizing.enabled` | **true** | 启用 ATR 等风险 sizing |
| `atr_risk_sizing.risk_per_trade_pct` | **0.02** | 每笔风险 = 权益 2% |
| `atr_risk_sizing.sizing_basis` | **primary_stop** | 以 DSL 主止损为基准 |
| `sl_atr_mult` | **1.5** | 备份 SL ATR 宽度倍数（clamp 前） |
| `sl_ceiling_pct` | **3.0** | 备份 SL 距入场价的硬上限（%）；HYPE 事故后新增的 ceiling clamp |
| `TP_ATR_MULT` | **1.0** | TP = 1.0×ATR |
| `equity_fraction_per_trade` | **0.2** | legacy 兜底权益比例 |
| `leverage` | **12** | 杠杆（legacy） |
| `conviction_sizing` | **false** | 置信度加成 sizing |
| `whale_size_multiplier` | **1.0** | 鲸鱼信号仓位倍率 |

### 5.2 成交参数

| 参数 | 值 | 说明 |
|------|-----|------|
| `max_trade_notional_usd` | **$800** | 单笔名义上限 |
| `tp_scale_fraction` | **0.5** | TP 落袋一半 |
| 最小订单名义 | **~$10.50** | HL 层强制的精度最小额 |
| HIP-3 dex 资金预检 | — | 独立清算所资金检查 |
| 可用保证金下限 | 10% | 见 Risk 层 |

### 5.3 信号增强

| 参数 | 值 | 说明 |
|------|-----|------|
| `signal_enforcement.enabled` | **true** | 启用 veto/boost |
| `signal_enforcement.veto` | **true** | 信号否决 |
| `signal_enforcement.boost` | **true** | 信号增强 |
| `signal_enforcement.gex_veto` | **true** | GEX 否决 |
| `signal_enforcement.boost_bar_delta` | **4** | 增强时降低门槛幅度 |
| `signal_enforcement.whale_veto_min_usd` | **$25 万** | 鲸鱼否决最低额 |
| `signal_enforcement.whale_boost_min_usd` | **$25 万** | 鲸鱼增强最低额 |
| `shadow_signals.enabled` | **true** | 影子信号套件 |
| `shadow_signals.gex / short_volume / crypto_whale / news` | **true** | 全部开启 |
| `shadow_signals.whale_window_min` | **15** | 鲸鱼窗口（分钟） |

### 5.4 HYPE 穿仓事故后的 P0 风控加固（2026-08-21）

2026-08-19 的 HYPE 8x 多单在约 10 分钟内录得 **-252% ROE**（穿仓），根因是备份止损
与 DSL floor 之间形成 40pp 的无保护缺口。以下三项改动同时上线：

1. **备份 SL ceiling clamp**（`sl_ceiling_pct`，默认 3.0%）
   - 旧公式：`sl_width_pct = (atr / entry) × sl_atr_mult × 100`（裸倍数，可任意宽）
   - 新公式：`sl_width_pct = min(atr_pct × sl_atr_mult, sl_ceiling_pct)`
   - 语义：交易所端的 trigger SL 永远不会比入场价宽过 3%，即使 4h ATR 巨大。
   - 配置：在 agent config 中覆盖 `sl_ceiling_pct`，或修改代码常量
     `_DEFAULT_SL_CEILING_PCT`。

2. **开仓 ATR 闸门**（`HERMES_MAX_ATR_PCT`，默认 15.0%）
   - 入场前若 `atr / mid_price × 100 > 15%`，直接拒绝开仓，返回
     `reason: "atr_too_high (...% > 15.0%)"`。
   - 原因：即使备份 SL 被 clamp 到 3%，在 ATR 28% 的币上 3% 止损会被 1-2 根
     4h K 线的正常噪声打掉；这类品种本身就不该进入仓位簿。
   - HYPE 入场时 ATR ≈ 28.75%，本闸门若已存在会直接拦下该笔交易。

3. **DSL 轮询间隔 60s → 15s**（`HERMES_SCAN_INTERVAL`）
   - DSL 软件止损依赖主循环轮询。60s 间隔在闪崩场景下意味着价格可在两次检查
     之间跨过 DSL floor 数十个百分点；缩短到 15s 把最大轮询盲区压缩到 1/4。
   - 异常兜底休眠也同步由 60s 缩短至 15s。

> **参数差异速查（HYPE 入场时）**
>
> | 止损层 | 公式 | 距入场 | 实际价位（入场 $74.00） |
> |--------|------|--------|--------------------------|
> | 备份 SL（事故时） | `ATR×1.5` 无 clamp | **-43.1%** | $42.08 |
> | DSL floor | `clamp(ATR×1.2, 1.2%, 3.0%)` | **-3.0%** | $71.78 |
> | 无保护缺口 | 备份 - floor | **40.1pp** | 价格在此区间内只有软件轮询能挡 |
>
> 事故时备份 SL 实际上没有起到兜底作用——价格在 9.8 分钟内从 $74 跌到 $50.69，
> DSL floor 虽然在 -3% 计算正确，但 60s 轮询 + 极端滑点导致最终在 -31.5%
> 才被强平检测到。修复后：备份 SL 被 clamp 到 -3%（与 DSL floor 对齐），
> 轮询 4× 更密，ATR > 15% 的币根本进不来。

---

## 六、DSL Exit 动态止损层

来源：`hermes_trader/agents/dsl_exit.py`。两阶段：Phase 1 亏损保护 → Phase 2 利润锁定。

| 参数 | 线上值 | 说明 |
|------|--------|------|
| `max_loss_pct` | **0.4%** | 现货最大止损 |
| `max_loss_roe_pct` | **5.0%** | 杠杆感知 ROE 止损（5.0 / lev） |
| `protect_pct` | **1.25%** | 进入 Phase 2 门槛 |
| `retrace_threshold` | **0.2** | 默认回吐比例 |
| `hard_timeout_minutes` | **1800**（30h） | 紧急超时退出 |
| `breakeven_trigger_pct` | **0** | 保本棘轮触发（关闭） |
| `breakeven_lock_pct` | **0** | 保本锁定（关闭） |
| `stale_flat_timeout_minutes` | **480**（8h） | 长期滞涨剪仓 |
| `consecutive_breaches_required` | **1** | 连续破位次数 |
| `atr_stop.enabled` | **false** | ATR 止损关闭 |
| `atr_stop.atr_mult` | **1.5** | ATR 倍数 |
| `atr_stop.floor_pct` | **1.0** | ATR 止损下限 |
| `atr_stop.ceiling_pct` | **4.0** | ATR 止损上限 |
| `noise_band.enabled` | **false** | 噪声带抑制关闭 |
| `noise_band.atr_mult` | **1.0** | 噪声带 ATR 倍数 |
| `phase2_tiers` | 8%→35%；15%→40% | 利润锁定阶梯 |
| `regime_aware.enabled` | **true** | 顺势放宽开启（trend 0.8%/10% ROE，non-trend 0.4%/5% ROE） |
| `regime_aware.trend_ride.protect_pct` | **3.0** | 顺势保护门槛 |
| `regime_aware.trend_ride.retrace_threshold` | **0.55** | 顺势回吐 |
| `regime_aware.trend_ride.phase2_tiers` | 3%/8%/15% | 顺势阶梯 |

> 代码内置备用策略模板（类默认值，线上未采用）：
> - **Conservative**：max_loss 5 / retrace 10% / protect 3 / timeout 360min
> - **Moderate**：max_loss 2.5 / retrace 7% / protect 1.5 / timeout 180min
> - **Aggressive**：max_loss 1.5 / retrace 5% / protect 0.8 / timeout 90min

---

## 七、其他系统参数

### 7.1 模式与市场

| 参数 | 值 | 说明 |
|------|-----|------|
| `mode` | **LIVE** | 运行模式（OFF / SHADOW / LIVE） |
| `enable_crypto` | **true** | 允许原生加密永续 |
| `enable_hip3` | **true** | 允许 HIP-3 代币化权益 |

### 7.2 辅助信号模块（线上状态）

| 模块 | 状态 | 说明 |
|------|------|------|
| `gex_signal` | **enabled** | GEX 信号（`caution_near_wall_pct`=10） |
| `shadow_signals` | **enabled** | 影子信号套件 |
| `runner_mover_surface` | **enabled** | 日波动者浮出 |
| `capital_rotation` | **disabled** | 资金轮动（shadow_mode=false） |
| `momentum_continuation` | **disabled** | 动量延续（`min_trend_pct`=8、`max_pullback_pct`=6、`weight`=0.4） |
| `candlestick_patterns` | **disabled** | K 线形态（`wick_body_ratio`=2.0、`context_lookback`=6、`context_pct`=1.5） |
| `momentum_reentry` | **disabled** | 动量再入场（`reclaim_pct`=1.0、`min_composite`=30） |

### 7.3 运行环境变量

| 变量 | 说明 |
|------|------|
| `OPENROUTER_MODEL` | AI 模型（默认 `x-ai/grok-4.3`） |
| `OPENROUTER_API_KEY` | OpenRouter 密钥 |
| `BRAVE_API_KEY` | Brave Search 密钥 |
| `HYPERLIQUID_WALLET_ADDRESS` / `HYPERLIQUID_PRIVATE_KEY` | Hyperliquid 钱包 / 私钥 |
| `HYPERLIQUID_MASTER_ADDRESS` | 主账户地址（可选） |
| `HERMES_SCAN_INTERVAL` | 扫描周期（默认 **15s**，2026-08-21 由 60s 下调） |
| `HERMES_MAX_ATR_PCT` | 开仓 ATR 上限（默认 **15.0%**），4h ATR(14)/现价 超过则拒绝开仓（`reason: atr_too_high`） |
| `HERMES_MAX_SPREAD_PCT` | 开仓盘口价差上限（默认 1.0%） |
| `HERMES_MAX_MARKETS*` / `HERMES_BATCH_*` | 扫描预算 |
| `HERMES_DSL_STATE_FILE` | DSL 状态文件路径 |
| `HERMES_AGENT_CONFIG_FILE` | 代理配置路径（默认 `.agent-config.json`） |
| `HERMES_STARTUP_GRACE_S` | 启动宽限 |

---

## 附：代码内置常量

| 常量 | 值 | 说明 |
|------|-----|------|
| `_DEFAULT_SL_ATR_MULT` | **1.5** | 备份止损 ATR 倍数默认（`executor.py`） |
| `_DEFAULT_SL_CEILING_PCT` | **3.0** | 备份止损距入场价的硬上限（%），`min(atr_pct×mult, ceiling)` |
| `TP_ATR_MULT` | **1.0** | 止盈 ATR 倍数 |
| `_DEFAULT_CONVICTION_TIERS` | [(0.80,1.5),(0.65,1.0),(0,0.7)] | 置信度分级倍率 |
| `_CRYPTO_COINS` | 40 币 | 相关性上限币池 |
| `_MAJOR_VOLUMES` | BTC/ETH/SOL/... | 静态成交量兜底 |
| `REGIME_TTL_S` | **300** | 市场制度缓存（5 分钟） |
| `_SLOPE_LOOKBACK` | **8** | 制度趋势回看（8 根 1h bar） |
| `_SLOPE_UP` / `_SLOPE_DOWN` | **+0.001 / -0.001** | 制度斜率阈值 |
