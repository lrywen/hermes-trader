# M-10 预注册：整合候选策略整体 OOS 对比

- 预注册日期：2026-10-01（Asia/Shanghai）
- 作者：量化研究（看结果前冻结）
- 性质：**把 M-7/M-8/M-9 三个已成立部件整合为单一候选，做整体 OOS 对比**，
  验证"越改越好"是否在组合层成立（而非各部件单独成立）。

## §1 候选策略定义

信号：1h 多头动量（M-7），只做多，同币非重叠。

出场（M-8 形态：让利润奔跑、1–2天后保护）——固定参数，看数前列死：

| 参数 | 值 |
|---|---|
| 初始硬止损 | −10%（入场后未达最低浮盈前兜底） |
| 最低浮盈门槛 | +2%（达到后才启用 trailing） |
| trailing 规则 | Chandelier：从持仓最高高点回撤 **5%** 即离场（只升不降） |
| 最长持有 | 120h（兜底） |

仓位（M-9）：V20 = clip(0.20/sigma, 0, 2)，sigma=trailing 30d 1h 年化波动。

## §2 三臂（看数前列死）

| 臂 | 出场 | 仓位 |
|---|---|---|
| BASELINE | 固定 H=8h 收盘 | w=1 |
| TRAIL | trailing（§1） | w=1 |
| FULL | trailing（§1） | V20 |

TRAIL vs BASELINE 隔离出场效应；FULL vs TRAIL 隔离波动目标效应。

## §3 路径模拟

逐信号沿未来 1h bar 的 high/low 推进：先判硬止损（保守，adverse 先判），再更新最高高点；
达门槛后按 trailing floor 判离场；到120h强平。注：用1h bar的 high/low（build_bars 需补
高低价），adverse 极端先判属保守近似。成本 taker 往返 9bp。

## §4 样本与切分

BTCUSDT、ETHUSDT；2026-01-01→09-27；切分 2026-03-31；**测试段唯一报告**。

## §5 指标与判读

每臂：累计净、Sharpe、最大回撤、ES、胜率、平均持有。按天块 bootstrap 给 FULL−BASELINE
净值差 CI。

- FULL 相对 BASELINE：净值/Sharpe 不劣且回撤显著下降（CI 支持）→ **候选成立，进入新数据确认**。
- 若 trailing 单独(TRAIL)已把收益大幅削掉 → 重新审视门槛/回撤宽度（仅在新数据，不在本段改）。
- 不做参数网格；−10/+2/5%/120h 为预列固定值。

## §6 限制

1h bar high/low 的路径近似（非逐tick）；样本薄（test约百余笔）。结论需新数据/Shadow 再确认。

## §7 复现

```bash
cd /home/ldy/hermes-trader
PYTHONPATH=. python3 scripts/integrated_candidate.py
```

## §8 变更记录

| 日期 | 变更 |
|---|---|
| 2026-10-01 | 初版冻结 |
