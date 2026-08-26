# HYPE 穿仓事故根因分析报告（RCA）

| 字段 | 值 |
|------|-----|
| 事故编号 | INC-2026-08-19-HYPE |
| 发生时间 | 2026-08-19 02:49 UTC（开仓） → 08-19 02:59 UTC（强平检测） |
| 标的 | HYPE（Hyperliquid 原生加密永续） |
| 方向 / 杠杆 | 多 / **8x** |
| 入场价 | **$74.00** |
| 平仓价 | **$50.69** |
| 现货跌幅 | **-31.50%** |
| 已实现 ROE | **-252.37%**（穿仓） |
| 持仓时长 | **9.8 分钟** |
| 退出 reason | `max_loss`（检测时现货已 -25.42%） |
| 报告日期 | 2026-08-21 |

---

## 一、事故摘要

2026-08-19 02:49 UTC，系统对 HYPE 开出一笔 8x 多单。开仓后 9.8 分钟内，HYPE 现货
从 $74.00 闪崩至 $50.69（-31.5%），以 8 倍杠杆计对应 -252% 名义 ROE，触发穿仓。

复盘认定：**本次损失不是 DSL 策略算错了止损价，而是"备份止损放得过宽 + 主止损轮询
过慢"两个独立缺陷叠加，在 DSL floor（-3%）与交易所备份 SL（-43.1%）之间制造了一个
40.1 个百分点的无保护缺口**。价格在两次 60 秒轮询之间直接砸穿 DSL floor 并一路跌到
强平区，整个区间内没有任何服务端订单能拦截。

---

## 二、时间线（UTC）

| 时间 | 事件 | 价格 / 指标 |
|------|------|-------------|
| 02:49:00 | 扫描器出信号，AI 通过，executor 开多 8x | mid ≈ $74.00 |
| 02:49:xx | 市价单成交；同时挂出交易所 trigger SL | 成交均价 $74.00 |
| 02:49:xx | 备份 SL 计算：`entry - ATR × 1.5`，**无 ceiling** | SL 价 **$42.08**（-43.1%） |
| 02:49:xx | DSL 初始化 floor：`clamp(ATR×1.2, 1.2%, 3.0%)` | floor ≈ **$71.78**（-3.0%） |
| 02:49–02:50 | 价格在 floor 附近震荡，首个 60s 轮询周期内未触发退出 | — |
| 02:5x（具体秒级时间戳未在日志中暴露） | HYPE 出现极速下挫，价格在 < 60s 内连续穿过 $71.78、$60、$55 | 从 -3% 砸到 -25%+ |
| 02:58:xx | 下一次 DSL 轮询检测到持仓已严重破位，触发 `max_loss` 路径 | 检测时约 -25.42% |
| 02:59:xx | 市价平仓成交（含极端滑点） | 成交均价 $50.69 |
| 02:59:xx | 事件落库：`dsl_exit` / `reason=max_loss`，ROE -252.37% | 持仓时长 9.8m |

> **关键事实**：交易所 trigger SL 放在 $42.08，价格从未跌到 $42.08——**备份止损根本没有
被触到**。它在事故全程是"哑弹"。真正起作用的只有 DSL 软件轮询，而轮询周期 60s
在闪崩面前等于不设防。

---

## 三、根因：备份 SL 与 DSL floor 的参数差异

这是本报告的核心。系统本来设计了"双重退出"——DSL 软件止损为主，交易所 trigger SL 为
备份——但两套机制用了**不同的 ATR 倍数和不同的 clamp 策略**，导致它们在高波动币上
彼此"脱钩"。

### 3.1 参数对照

| 维度 | 交易所备份 SL（事故时） | DSL floor（事故时） |
|------|--------------------------|----------------------|
| 代码位置 | [executor.py](file:///home/ldy/hermes-trader/hermes_trader/agents/executor.py) `place_hl_trigger_order(..., "sl", ...)` | [dsl_exit.py](file:///home/ldy/hermes-trader/hermes_trader/agents/dsl_exit.py) `check()` |
| ATR 周期 | 4h ATR(14) | 4h ATR(14) |
| 倍数 | **1.5×** | **1.2×** |
| 下界（floor） | 无 | **1.2%** |
| 上界（ceiling） | **无** | **3.0%** |
| 完整公式 | `sl_px = entry - ATR × 1.5` | `spot_cap = clamp(ATR×1.2, 1.2%, 3.0%)` |
| HYPE 入场 ATR | **28.75%** of spot（≈ $21.27） | 同左 |
| 计算宽度 | `28.75% × 1.5 = 43.13%` | `min(28.75% × 1.2, 3.0%) = 3.0%` |
| 止损价位 | **$42.08** | **$71.78** |
| 距入场 | **-43.1%** | **-3.0%** |
| 服务端存在 | 是（trigger market，reduce_only） | 否（纯软件状态） |

### 3.2 缺口示意

```
入场 $74.00
   │
   ├─ DSL floor        $71.78  (-3.0%)   ← 软件止损，靠 60s 轮询
   │
   │   ╔══════════════════════════════════════╗
   │   ║  40.1pp 无服务端保护的空白区间          ║
   │   ║  价格在此区间内只有 DSL 轮询能挡         ║
   │   ║  60s 盲区 × 闪崩 = 灾难                ║
   │   ╚══════════════════════════════════════╝
   │
   ├─ 备份 SL           $42.08  (-43.1%)  ← 交易所 trigger，从未被触达
   │
   └─ 实际平仓          $50.69  (-31.5%)  ← 强平 / 延迟市价单成交
```

### 3.3 为什么 DSL 有 clamp 而备份 SL 没有

两套机制来自不同时期的代码层：

- **DSL floor** 的 `clamp(..., 1.2%, 3.0%)` 是策略语义的一部分——它明确要求
  "不管 ATR 多大，现货止损最多放到 3%"，因为这是策略可承受的损失上限。
- **备份 SL** 的 `ATR × 1.5` 最初被当作"给交易所的一个足够宽的硬兜底，防止 DSL
  因 API 抖动漏检"。设计假设是 ATR 反映"正常波动"，所以 1.5×ATR 不会频繁误触。
  这个假设在 BTC/ETH（ATR 1–3%）上成立，在 HYPE（ATR 28.75%）上直接破产。

二者没有共享同一个"最大可接受宽度"常量，也没有在代码里强制约束
`backup_sl_width ≤ dsl_floor_width + ε`。这是一个典型的**跨层参数耦合缺失**：
每层各自合理，组合起来却产生了巨大的保护断层。

### 3.4 轮询延迟把"缺口"变成"实损"

即使有 40pp 缺口，如果 DSL 轮询足够密，价格刚破 $71.78 就会被检测到并以接近 -3%
的价格平仓。事故时：

- `HERMES_SCAN_INTERVAL = 60s`
- HYPE 从 $74 跌到 $50 用了**不到 10 分钟**，其中致命一段（从 -3% 到 -25%）发生
  在**一个 60s 轮询窗口内**
- DSL 下次醒来时，价格已经在 -25% 区域，`max_loss` 触发，但市价单在流动性枯竭的
  order book 上又滑了 ~6pp，最终成交在 -31.5%

**8 倍杠杆把 -31.5% 的现货波动放大成 -252% 的账户权益损失**，直接穿仓。

---

## 四、为什么其他高 ATR 币没出事（BOME / CASHCAT / HEMI）

复盘 11 笔交易中，有 4 笔的备份 SL 宽度同样超过 5%：

| 币 | 入场 ATR% | 备份 SL 距入场 | 实际走势 | 结果 |
|----|-----------|----------------|----------|------|
| BOME #1 | 4.43% | -6.65% | 缓涨 +2.05% | DSL floor 正常止盈 +5.99% ROE |
| BOME #2 | 5.12% | -7.68% | 缓涨 +4.37% | DSL floor 正常止盈 +12.95% |
| BOME #3 | 5.54% | -8.31% | 横盘 -0.18% | floor 微亏 -0.68% ROE |
| CASHCAT | 7.74% | -11.61% | 阴跌 -2.85% | stale_flat_timeout 退出 -8.71% ROE |
| HEMI | 6.57% | -9.85% | 缓涨 +1.33% | floor 止盈 +3.84% ROE |
| **HYPE** | **28.75%** | **-43.1%** | **闪崩 -31.5%** | **-252% 穿仓** |

规律很清楚：

1. **备份 SL 宽度本身不是必然杀手**——只要币的波动是"慢"的（横盘 / 缓涨 / 阴跌），
   DSL 轮询能在破位时立即接管，备份 SL 哪怕放到 -10% 也不会被用到。
2. **杀手是"高 ATR × 极速单边行情 × 60s 轮询"三者叠加**。HYPE 是样本里唯一一个
   ATR 超过 15% 的币，也是唯一一个出现分钟级闪崩的币。
3. 这支持本次的双重防御：**ceiling clamp 解决宽度问题，ATR 闸门解决准入问题，
   轮询缩短解决延迟问题**——三者缺一不可。

---

## 五、修复措施（P0，已于 2026-08-21 上线）

### Fix 1：备份 SL 加 3% ceiling clamp

文件：[executor.py:1457-1474](file:///home/ldy/hermes-trader/hermes_trader/agents/executor.py#L1457-L1474)

```python
sl_atr_mult = float(config.get("sl_atr_mult", _DEFAULT_SL_ATR_MULT))
sl_ceiling_pct = float(config.get("sl_ceiling_pct", _DEFAULT_SL_CEILING_PCT))
# ...
atr_stop_pct = (atr / entry_px) * sl_atr_mult * 100
sl_width_pct = min(atr_stop_pct, sl_ceiling_pct)   # ← 新增 clamp
sl_px = entry_px * (1 - sl_width_pct / 100) if is_buy else entry_px * (1 + sl_width_pct / 100)
```

新常量 `_DEFAULT_SL_CEILING_PCT = 3.0`。效果：HYPE 若今天再开，备份 SL 会放在
$71.78（-3%），与 DSL floor 完全重合，40pp 缺口消失。

### Fix 2：开仓 ATR > 15% 直接拒绝

文件：[executor.py:1235-1247](file:///home/ldy/hermes-trader/hermes_trader/agents/executor.py#L1235-L1247)

```python
_max_atr_pct = float(os.environ.get("HERMES_MAX_ATR_PCT", "15.0"))
_atr_pct = (atr / mid_price * 100.0) if mid_price > 0 else 0.0
if _atr_pct > _max_atr_pct:
    return {"executed": False, ..., "reason": f"atr_too_high ({_atr_pct:.2f}% > {_max_atr_pct:.1f}%)"}
```

HYPE 入场时 ATR = 28.75%，本闸门若已存在会在 `set_leverage` 之前拦下该笔交易，
根本不会开仓。这是比 ceiling clamp 更上游的防御：clamp 让"能开的仓"有兜底，
ATR 闸门让"不该开的仓"进不来。

### Fix 3：DSL 轮询间隔 60s → 15s

文件：[trading_loop.py:243](file:///home/ldy/hermes-trader/scripts/trading_loop.py#L243)

```python
scan_interval = int(os.environ.get('HERMES_SCAN_INTERVAL', '15'))
```

异常兜底休眠同步 60s → 15s。部署侧 `k8s/configmap.yaml`、`fly.toml`、
`.env.local.example`、`hermes-deploy/.env.local` 全部同步更新。最大轮询盲区由
60s 压缩到 15s，闪崩场景下 DSL 错过的价格距离理论上缩为原来的 1/4。

---

## 六、修复前后的 HYPE 情景重演

| 阶段 | 事故时（修复前） | 修复后 |
|------|-------------------|--------|
| 开仓前 | ATR 28.75%，无准入检查，正常开仓 | **`atr_too_high` 闸门拒绝开仓**，无仓位 |
| 假设强制开仓 | 备份 SL 挂 $42.08（-43.1%） | 备份 SL 挂 $71.78（-3%，ceiling clamp） |
| 价格砸到 -3% | DSL floor 计算正确，但 60s 内未轮询 | DSL 在 ≤15s 内检测到破位，尝试市价平仓；若软件慢，交易所 trigger SL 同时触发 |
| 价格继续跌到 -25% | 下次轮询才发现，市价单滑到 -31.5% 成交 | 服务端 trigger SL 已在 -3% 附近成交（trigger market，reduce_only） |
| 预期结果 | -252% ROE，穿仓 | 该笔交易不存在（闸门拦截）；即便存在，损失被钉死在 -3% 附近 |

---

## 七、未解决的风险与后续建议（P1/P2，不在本次 P0 范围）

以下问题在本次复盘中识别但未在 P0 修复，列出供后续排期：

1. **DSL floor `f"{floor:.2f}"` 格式化 bug**（[dsl_exit.py:387](file:///home/ldy/hermes-trader/hermes_trader/agents/dsl_exit.py#L387)）：
   对 BOME 这类 0.001 价位的币，日志显示 `floor=0.00`。仅影响日志可读性，
   不影响策略判定，但建议改用 `:.6g`。

2. **DSL floor 与备份 SL 的参数仍然在两处定义**：本次通过 ceiling clamp 让它们在
   结果上对齐，但 `sl_atr_mult=1.5`（备份）与 DSL 的 `1.2×` 倍数仍然不一致。
   建议后续把"可接受最大止损宽度"提取成一个共享配置项，两层同时引用，从根上
   避免再次漂移。

3. **trigger market 的极端滑点保护**：交易所端用的是 trigger market 单，HYPE 这类
   流动性瞬间枯竭时，market 单成交价可能劣于 trigger 价。可考虑用 trigger limit
   加一个 worst-price band，或在触发后由 DSL 立刻补发一个保护单。

4. **轮询缩短到 15s 后的限流与缓存预算**：目前 5m candle cache TTL = 50s，15s
   轮询会复用同一份 K 线缓存 3–4 次，不增加 HL 历史 K 线请求；但 midpoint、
   orderbook、持仓查询等实时接口的 QPS 会涨 4 倍，建议上线后观察 HL 返回的
   429 / rate-limit 指标，必要时把"扫描"和"DSL 持仓检查"拆成两个独立 cadence
   （扫描仍可 30–60s，DSL exit 检查单独 10–15s）。

5. **穿仓级别的熔断**：单笔损失超过某阈值（如 -50% ROE）时，是否应自动把 bot
   切到 OFF 模式并告警。目前系统没有这个 self-halt 开关。

---

## 八、经验总结

1. **双重防御只有在"两层覆盖同一价格区间"时才成立**。备份 SL 放得比主止损宽 40pp，
   等于没有备份。安全网必须和主网在同一个位置。
2. **基于 ATR 的止损必须有 ceiling**。ATR 在低波动期看着合理，在极端行情下会给出
   反直觉的宽止损——而恰恰是极端行情才需要止损。
3. **软件止损的有效性 = 策略正确性 × 轮询频率**。DSL 的 floor 计算 100% 正确，
   但 60s 轮询让它在闪崩下等于不存在。任何"软件轮询 + 服务端兜底"的架构都必须
   量化轮询盲区 vs 标的近期最大分钟级波幅。
4. **高波动标的需要上游准入闸门**。ceiling clamp 是下游兜底，ATR 闸门是上游拒单。
   两者不能互相替代。

---

## 附录：数据来源

- `/data/session-log.jsonl`（容器内，24006 事件，含本次 HYPE execute / dsl_exit 记录）
- `/data/.dsl-state.json`（DSL 状态持久化）
- [executor.py](file:///home/ldy/hermes-trader/hermes_trader/agents/executor.py)（备份 SL / 新开仓闸门）
- [dsl_exit.py](file:///home/ldy/hermes-trader/hermes_trader/agents/dsl_exit.py)（DSL floor 计算）
- [trading_loop.py](file:///home/ldy/hermes-trader/scripts/trading_loop.py)（轮询 cadence）
- [AGENT_CONFIG_REFERENCE.md §5.4](file:///home/ldy/hermes-trader/docs/AGENT_CONFIG_REFERENCE.md)（P0 修复参数文档）
