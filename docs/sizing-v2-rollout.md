# Sizing v2（ATR/DSL 对齐等风险仓位）灰度放量方案

- 审计标注：Audit 2026-09-03 P2-9
- 当前部署：`atr_risk_sizing.sizing_v2_enabled` 未设置（代码默认 **false/关闭**）；
  SHADOW 模式运行中。

## 1. 背景：为什么需要 sizing v2

旧仓位路径（`executor.py` L1913-1917）用顶层 `max_loss_pct` / `max_loss_roe_pct`
估算止损宽度（10x 下 2.5%/25 → 约 2.5% 止损），但 DSL 实际挂出的止损经过
**regime + ATR + ROE 三层钳制**（现货头皮 0.5% / 趋势 1.0% 量级，见
dsl_exit.`compute_effective_stop_pct`）。结果是：**每笔风险被低估 2.5-5 倍**
——止损比测算的窄得多，等风险公式反推出的名义本金就偏大。

sizing v2（executor.py L1885-1953）在仓位侧**逐字节镜像 DSL 止损数学**：
regime 探测 → ATR% → ATR 均值/尖峰调整 → 30 天平均出场滑点 →
`compute_effective_stop_pct()` 得到与 DSL 注册一致的 effective_stop，再用
`risk_per_trade_pct × equity / stop_frac` 反推名义本金。成交后还有 5% 漂移断言
比对 sizing 侧止损与 DSL 实际注册止损（`_sizing_v2_stop_pct`）。

## 2. 灰度开关

配置块 `atr_risk_sizing`（config_schema.py `_ATR_RISK_SIZING_SPEC` L394-407）：

| 键 | 类型 | 范围 | 默认 | 说明 |
|----|------|------|------|------|
| `sizing_v2_enabled` | bool | — | false | 总开关。false=旧路径 |
| `sizing_v2_cap_pct` | float | 0.0-1.0 | 1.0 | 灰度系数：v2 名义本金 × cap_pct，日志记 `gray_NNpct` 钳制原因，metrics 记 `gray_pct` |
| `risk_per_trade_pct` | float | 0.0-1.0 | 0.02 | 单笔风险占权益比（两路径共用） |
| `sizing_basis` | enum | primary_stop/dsl_stop/atr_stop | primary_stop | v2 仅在 primary_stop/dsl_stop 分支生效 |

观测点：
- 日志 `[sizing-v2] {coin} regime=... spot_cap=... roe_cap=... slip+=... → effective_stop=...%`（每笔 v2 仓位一行）
- 日志 `[executor] primary-stop equal-risk sizing ... clamped:gray_25pct/notional_cap/...`
- metrics `sizing_clamped_total{reason="gray_pct"|"notional_cap"|...}`（metrics.py L101/L217）

## 3. 阶段定义与放量规则

| 阶段 | enabled | cap_pct | 最短运行 | 进入下一阶段条件 | 回滚条件 |
|------|---------|---------|----------|------------------|----------|
| 0 影子观察 | false | — | — | 代码部署 ≥3 天无异常 | — |
| 1 首灰 | true | **0.10** | ≥7 个交易日 | 见 §4 收敛标准全部满足 | 任一 §5 条件触发即回阶段 0 |
| 2 | true | **0.25** | ≥7 个交易日 | 同上 | 同上 |
| 3 | true | **0.50** | ≥7 个交易日 | 同上 | 同上 |
| 4 全量 | true | **1.00** | 持续 | — | 同上 |

说明：
- 阶段 1 先用 0.10 而非直接 0.25——v2 算出的名义本金比旧路径**大**（修正低估），
  首灰以最小敞口验证"止损宽度镜像"本身的正确性（5% 漂移断言零触发）。
- 每阶段至少 7 个交易日（覆盖一个周末 funding/波动周期）；SHADOW 模式下
  成交为影子账本，阶段 1-4 可在 SHADOW 全程完成，转 LIVE 时从阶段 3 重新起步。
- 每次只调 cap_pct 一个变量；调参当天不计入 7 天（避免混样）。

## 4. 收敛标准（每阶段晋级前必须全部满足）

1. **止损镜像偏差**：sizing 侧 `_sizing_v2_stop_pct` 与 DSL 注册 effective_stop
   的偏差 ≤5%（5% 漂移断言零触发；触发即视为镜像不同步，阻断晋级）。
2. **滑点假设**：影子成交的实际出场滑点中位数 ≤ 56.6 bps（与 sizing 使用的
   `avg_exit_slip_bps(30d)` 同量级；若实际滑点持续高于假设，v2 仍偏激进）。
3. **止损原因分布**：DSL `max_loss`/ROE 闸门触发占比无异常上升，无单笔
   ROE 损失接近 `roe_halt_threshold_pct` 的事件。
4. **钳制合理性**：`gray_pct` 以外的钳制（max_leverage/notional_cap）占比
   与阶段 0 基线相比无显著上升（v2 不应频繁打爆杠杆/名义上限）。
5. **熔断洁净**：阶段内全局 halt / 单币熔断 / 日内 giveback halt 无异常触发，
   max_daily_loss 闸门未被逼近。
6. **样本量**：阶段内影子成交 ≥20 笔（不足则延长，不凑数晋级）。

## 5. 回滚条件（任一触发，立即 set sizing_v2_enabled=false）

- 5% 漂移断言触发（sizing 与 DSL 止损不一致 = 风险测算失真）。
- 出现 v2 路径专属异常：`[sizing-v2] regime detect failed` 高频出现
  （>10% 交易）或 compute_effective_stop_pct 抛错。
- 影子组合最大回撤较阶段 0 同期扩大 >50%。
- 任一单笔影子 ROE 损失 ≥ roe_halt_threshold_pct 的 50%（预警线）。

回滚操作：`atr_risk_sizing.sizing_v2_enabled=false`（热更新，无需重启）；
cap_pct 调小不回 false 也算部分回滚。

## 6. 监控项清单

- 每笔：`[sizing-v2]` 行（regime/spot_cap/roe_cap/slip/effective_stop）
- 每笔：`clamped:` 原因分布（gray_pct / notional_cap / max_leverage）
- 每日：影子成交笔数、止损原因分布、实际滑点中位数/P95、回撤
- 配置：dashboard 配置转储确认 enabled/cap_pct 为预期值

## 7. 验证方式

```bash
# 代码侧
python3 -m pytest tests/ -q
# 运行态（容器内）
docker exec hermes-trader grep -c '\[sizing-v2\]' /data/trading-loop.log
docker exec hermes-trader grep 'clamped:.*gray' /data/trading-loop.log | tail -20
# 配置热更新示例（阶段 1）：
# atr_risk_sizing.sizing_v2_enabled = true, sizing_v2_cap_pct = 0.10
```

## 8. CS-G 空头侧与成本上限 shadow 遥测（2026-09-09）

CS-G 只在 **shadow 评级记录**上新增字段，applied 仓位与下单路径完全不动；
`HERMES_SIZING_V2_MODE` 须保持 `shadow`。空头/多头序列按方向分别累计
（出场滑点、费率、结果不再混用一个池子），成本分母加宽为
「方向化实测出场滑点 + 实测费率 + 预期持仓时长内的 funding/借币 carry」。

每笔 shadow 评级新增 `v2_cost_*` 字段（仅遥测，不参与 applied size）：

| 字段 | 含义 |
|------|------|
| `v2_cost_denom_pct` | 加宽后的成本分母（占名义本金 %，含滑点+费+carry） |
| `v2_cost_slip_bps` / `v2_cost_slip_extra_pct` | 方向化实测不利出场滑点（bps）与相对旧假设的增量（%） |
| `v2_cost_slip_source` | 滑点来源：`measured_long` / `measured_short` / `fallback`（样本不足冷启动） |
| `v2_cost_fee_rt_pct` / `v2_cost_fee_measured_bps` | 实测手续费率（%）与记忆层实测 bps |
| `v2_cost_hold_hours` / `v2_cost_hold_source` | 预期持仓时长（小时）与来源：`config` / `fallback`（默认 8.0h） |
| `v2_cost_funding_rate_hr` | 当期 funding 小时费率（空头符号方向化） |
| `v2_cost_carry_pct` | 预期持仓内 funding/借币 carry 合计（%） |
| `v2_cost_borrow_bps` | 借币成本 bps（冷启动保守取 0.0） |
| `v2_cost_notional_usd` / `v2_cost_notional_clamped_usd` | 宽分母反推名义本金与经成本上限钳制后的名义本金 |
| `v2_cost_cap_binds` | 该笔是否被成本上限钳制（bool） |
| `v2_cost_vs_v2_ratio` | 宽分母口径相对现 v2 口径的名义本金比（<1=成本上限更紧） |

### 8.1 168h 评级口径（CS-G 观察窗，不改变 §4 晋级闸门）

CS-G 观察窗与既有 24/72/168h 评级并行，168h 末同时满足以下条件才允许把
宽分母成本上限列入 PROMOTE_CANDIDATE（仍需 §4 六条全过 + 人工晋升）：

1. **分方向样本量**：168h 内 long、short 各自 ≥10 笔 shadow 评级；不足则
   延长窗口，不跨方向凑数。
2. **来源成熟度**：`v2_cost_slip_source=fallback` 或
   `v2_cost_hold_source=fallback` 的占比 ≤20%（冷启动默认值不得主导评级）。
3. **上限绑定率**：`v2_cost_cap_binds=true` 占比稳定且方向间差异可解释；
   绑定率 >50% 视为成本上限过紧或实测成本异常，禁止晋升，先核 funding/滑点。
4. **比率合理性**：`v2_cost_vs_v2_ratio` 的 P50 ∈ [0.5, 1.0]（宽分母应更紧
   但不应腰斩到失真）；空头比率须与空头 carry 符号方向一致，出现
   ratio>1 的系统性反向即阻断。
5. **carry 校验**：`v2_cost_carry_pct` 与窗口内实际 funding 同号同量级，
   `v2_cost_borrow_bps=0.0` 期间结论按「未计借币」标注，不作为最终上限。
6. **零路径副作用**：168h 内 applied 仓位零变化（v2 仍 false / cap 路径
   不变）、无 `v2_cost_*` 相关异常、§5 回滚条件零触发。

口径产出仍由离线 `scripts/shadow_grade.py --json` 汇总；任何一项不满足都只
延长观察，不调闸门、不改阈值、不改 `scripts/trading_loop.py`。

