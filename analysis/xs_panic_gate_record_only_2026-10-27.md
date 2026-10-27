# xs_reversal panic gate（record-only）落地说明 — 2026-10-27

> 纯影子/记录改动，**不改 `is_candidate`、不接任何下单路径、默认关闭**。

## 1. 为什么加
无门 xs 候选在 3.4 年 Binance 1h 上 72h 净 edge 仅 t=1.93、日块 CI 含零，且
2026 子样本消失（见 `xs_episode_stats_2026-10-27.json`）。条件面板
（`xs_regime_conditional_2026-10-27.json`）发现 edge 可被"真恐慌"稳定分离：

```
panic_gate_pass = ext_pct <= -8.0   (距 72h 高点深回撤)
              AND atr24_pct >= 1.0  (近24根1h 平均 TR/close，波动放大)
              AND rsi14 >= 20       (超卖但非自由落体接刀)
```

- Binance 3.4 年：120 独立 episode / 95 独立日，72h 净 **+276bp、WR64%、日块 t=2.98、
  CI[+79,+393]**；逐年 2023/24/25/26 全正（2026 +231bp，n=21）；
  阈值邻域 12 格全正；OOS 2025–26 t=3.01。
- Hyperliquid 同期（外生）：方向复现（5 ep/3 日，+744bp），但独立日不足，
  统计未确认（`xs_gate_hl_validation_2026-10-27.json`）。
- 与权重标定 down 桶重叠 **90.4%**（富集 2.99×），属同一现象，无需独立立论
  （`p1c_xs_down_overlap_2026-10-27.json`）。

## 2. 代码改动
- `hermes_trader/agents/xs_reversal.py`
  - 新增 `_atr_pct(candles, i, period=24)`：与离线发现脚本同口径（简单均值
    TR/close×100，非 Wilder）。
  - `evaluate_xs_reversal` 在**每个触发**上额外落盘：
    `atr24_pct`、`panic_gate_enabled`、`panic_gate_pass`、`panic_gate_reasons`
    （`ext_too_shallow` / `atr24_too_low` / `rsi_below_floor`）。
  - 阈值从配置读，默认 `panic_gate_enabled=false`；关闭时布尔恒 False 但连续
    特征仍记录，便于 M4 离线分组。
- `hermes_trader/agents/config_schema.py`：注册 4 个键（bool + 数值范围校验），
  未知键/越界仍被拒绝。
- 测试：`tests/test_xs_reversal_shadow.py` +3 用例（默认关闭仍记特征 / 深度恐慌
  通过 / 低波浅回撤被拒且不改 is_candidate）。55 项 xs 相关测试 + 115 项含
  schema 测试全绿，ruff 通过。

## 3. 历史数据 parity（时间记录一律先跑历史）
- Binance 1h：生产纯函数 `evaluate_xs_reversal` 对 333 个独立 episode 的
  `panic_gate_pass` 集合与离线脚本**逐一致（门内都是 120，零逻辑分歧）**；
  仅 atr24 的 round-4 记录差（<5e-5）。
- HL 1h（生产场所，2026-02→09 缓存）：生产纯函数逐根重放得 panic-gated
  **97 episode / 12 独立信号日**，仍 < 统计确认阈值 20 独立日。

## 4. 启用方式（仍只记录）
配置块（schema 已接受）：
```json
{"xs_reversal": {"mode": "shadow",
  "panic_gate_enabled": true,
  "panic_ext_max_pct": -8.0,
  "panic_atr24_min_pct": 1.0,
  "panic_rsi_min": 20.0}}
```
本阶段**不**置 `enforce`（其仍为 record-only no-op），也不把 panic_gate_pass
接入任何下单/资金决策。

## 5. promote 前未决项
1. HL/实盘口径门内独立信号日累积到 ≥20（理想 40–60）后重算日块 t/CI；
2. ATR 阈值跨币量纲：SOL 波动系统性高于 BTC，promote 前评估用"币内 ATR 分位"
   替代固定 1.0%，或分币设阈；
3. 退出参数（1.5% 止损 + 72h 时间止损）需用该臂自身信号离线验证后冻结；
4. 统一 `top_pct` 85(生产)/90(M1) 口径后再确认门 edge。

## 6. 波动腿量纲校准（2026-10-27 历史数据，项 2 已决）
脚本 `scripts/xs_vol_gate_calibration.py`，存档
`analysis/xs_vol_gate_calibration_2026-10-27.json`。共同条件 ext≤−8 ∧ RSI≥20，
只隔离波动腿：

| 波动腿 | n/独立日 | 净均值 | WR | 日块t | 日块CI | BTC/ETH/SOL 占比 |
|---|---|---|---|---|---|---|
| 固定 atr≥1.0% | 120/95 | +276bp | 64% | 2.98 | [+79,+393] | 5/31/**64** |
| **币内 ATR 分位≥0.6** | **98/79** | **+297bp** | **68%** | 2.83 | [+65,+375] | 8/39/53 |
| 币内 ATR 分位≥0.7 | 84/66 | +329bp | 71% | 2.92 | [+85,+423] | 10/40/50 |
| 无波动腿 | 137/108 | +246bp | 63% | 3.10 | [+85,+354] | 7/31/62 |

- **固定 1.0% 存在跨币偏斜**：deep 集合 SOL 已占 62%，固定门进一步压到 SOL 64%/
  BTC 5%（占比极差 59pp）；ETH 单币日块 CI [−5,+418] 含零。
- **币内 trailing-90d ATR 分位（自我归一化、零前视，与 ext 百分位同做法）更公平
  且质量更高**：占比极差降到 45pp（q0.6），**三币各自 CI 下界全转正**
  （ETH q0.6 [+8.5,+404]/q0.7 [+32,+457]，BTC/SOL 均正），逐年 2023–26 全正、
  非单币拉动。
- **选型：波动腿 promote 时采用币内 ATR 分位，阈值 q=0.6**（在 edge 强度、
  跨币公平与独立日数 79 之间折中；q0.7 edge 更高但独立日仅 66，不利于尽快累积）。
  **已实现（2026-10-27 更新）**：`evaluate_xs_reversal` 现对每个触发落盘
  `atr_pctile`（PIT trailing-90d），panic gate 支持
  `panic_vol_mode = "fixed" | "pctile"`（默认仍 fixed、gate 默认关）。生产纯函数
  在历史上对 q0.6 门集与离线脚本逐 episode parity（98=98、零分歧）。HL 1h 生产
  重放：q0.6 门 128 ep / 14 独立信号日（BTC/ETH/SOL=31/44/53，比固定门 12/37/48
  更均衡），仍未到自设 ≥20 独立日确认线。固定 1.0% 仅保留为离线对照。

## 7. 退出参数冻结（2026-10-27 历史数据，项 3 已决）
脚本 `scripts/xs_exit_param_freeze.py`，存档
`analysis/xs_exit_param_freeze_2026-10-27.json`。在 gated 信号（top85 ∧
ext≤−8 ∧ RSI≥20 ∧ atrP≥0.6，n=98/79 独立日）上模拟次根 open 入场、真实止损/
时间止损出场（往返 9.26bp）：

| 退出方案 | 净均值 | WR | 日块t | 日块CI | 止损触发率 |
|---|---|---|---|---|---|
| **仅 72h 时间止损（无价格止损）** | **+300bp** | **68.4%** | **2.83** | **[+70,+370]** | 0% |
| 1.5% 止损 + H72 | +50bp | 23.5% | 1.31 | [−20,+150] | 76.5% |
| 2.0% 止损 + H72 | +100bp | 30.6% | 1.59 | [−20,+190] | 69.4% |
| 3.0% 止损 + H72 | +130bp | 40.8% | 1.68 | [−10,+220] | 58.2% |
| 1.0% 止损 + H72 | +20bp | 15.3% | 0.66 | [−40,+90] | 84.7% |

**冻结结论：xs 臂退出 = 仅 72h 时间止损，不设 1.5% 紧价格止损。** 深跌反弹单有
76–87% 会先下探 1.5% 再收回，硬止损在底部把它们洗出（WR 68%→15–24%、CI 全部
含零），摧毁该均值回归 edge。借用 5m 突破 scalp 的 1.5% 止损（原 S3 假设）被
本数据否决。若需灾难保护，应另用一个**远宽**的组合级止损（如 ext 继续恶化或
币级 −15~−20%），不得用 1.5%。H48/H96 全面弱于 H72，时间止损取 72h。

