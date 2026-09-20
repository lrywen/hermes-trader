# B-3 81 币池全量重跑 SCOREBOARD（出场机制对齐）

> 重跑日期：2026-09-19/20（Asia/Shanghai）
> 数据：81 币 `bt_ready_pool`，Binance 现货 5m/1h/4h，180 天窗口
> 内核：B-1a-改（逐笔杠杆有效止损 + Phase2 交易所镜像 SL）+ B-2（maxc=2 后处理）
> + B-4（81 币逐币半价差）；产物 `logs/b3_81coin_filt_ra.jsonl`（gitignored，
> 318,195 笔交易，81 币全齐；11 币 DNS 风暴失败后用预热锚点纯缓存补跑合并）

## 验收口径

实盘平仓执行者地面真值（P6b1c，51 行≈19 笔）：

| 执行者 | 实盘占比 |
|---|---|
| DSL floor 类（本地 floor + 交易所镜像 exchange_trigger） | **52.6%** |
| max_loss 硬止损 | **42.1%** |

## 全臂出场原因占比（81 币，n=笔数）

| 臂 | n | floor 类 | max_loss | 对实盘 |
|---|---|---|---|---|
| **filt**（tuned，禁 short/relaxed/黑） | 14,288 | **52.5%**（floor 52.5） | **45.4%** | ✅ floor 精确吻合 |
| **filt_exch**（filt + 交易所侧单） | 14,289 | **54.0%**（floor 46.4 + exchange_trigger 7.7） | **43.8%** | ✅ 最接近实盘拆分 |
| filt_ld（实盘 DSL 出场） | 13,173 | 36.8% | 56.5% | floor 偏低 |
| **filt_ra**（regime 感知，非趋势 0.4% 止损） | 13,476 | 19.5% | **72.3%** | ❌ 反向偏离 |
| filt_ra_exch（regime + 交易所侧单） | 13,494 | 21.1%（floor 12.4 + exch 8.7） | 70.7% | ❌ 反向偏离 |

其余原因为 stale_flat_timeout / hard_timeout / end_of_data（合计 <10%）。

## 结论（对 B-3 假设的确认与修正）

B-3 原始假设：「币池（8 majors→81 币）→ 波动率分布 → 出场机制占比向实盘
靠拢」，预期 `filt_ra` 的 floor 占比 14.9%→52.6%、max_loss 59.9%→42.1%。

**实测确认了因果方向，但修正了作用臂：**

1. **币池对齐假设成立**——81 币池下，与实盘出场占比精确吻合的是 **tuned 出场
   口径 `filt`（52.5%/45.4%）和带交易所镜像的 `filt_exch`（54.0%/43.8%）**，
   floor 类命中实盘 52.6%（误差 0.1–1.4pp），max_loss 命中 42.1%（误差 1.7–3.3pp）。
2. **`filt_ra`（regime_aware，非趋势 0.4% 紧硬止损）被证据否定为实盘主路径**：
   0.4% 紧止损使 72% 交易在 max_loss 出场（floor 仅 19.5%），与实盘相反。这与
   A-1（顶层 max_loss_pct=1 永不达）、A-4 的 regime 真实切换存疑相互印证——
   实盘 floor 占比高对应的是**较宽的 tuned 止损 + Phase2 追踪**，不是非趋势 0.4%。
3. **交易所镜像（B-1a-改①）的贡献可量化**：filt→filt_exch 把 7.7pp 出场从
   floor_breach 重标为 exchange_trigger（快速下挫时镜像先触发），合计 floor 类
   从 52.5% 微升到 54.0%——镜像主要改变**标签拆分**而非总量，符合「exchange_trigger
   是 DSL floor 镜像而非独立机制」的定案。

## 对后续的含义

- edge 判断的**出场基线臂应取 `filt` / `filt_exch`**（与实盘机制占比对齐），
  **不再用 `filt_ra`** 作为"实盘对齐"臂；regime_aware 0.4% 路径列入 A 批次
  阻塞性澄清（A-1/A-4）后再定去留。
- 本批为**未叠加 maxc=2 的逐币口径**；组合层收益率需再过 block_bootstrap
  `--maxc 2`（B-2）得到容量校正后数字。
- 产物 154MB→合并后含 81 币；11 币补跑用 end_ms=1789515900000 锚点，与其余
  70 币（end_ms=最近收盘）存在数小时窗口差，对 180 天占比统计可忽略，记此 caveat。
