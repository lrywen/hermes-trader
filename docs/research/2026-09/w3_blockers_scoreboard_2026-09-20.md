# W3 批次 3：阻塞性澄清 A-1 ~ A-6 SCOREBOARD

> 澄清日期：2026-09-20（Asia/Shanghai）
> 方法：容器 `/data` 生产配置/轮转日志只读取证 + 仓库 git 考古 + 回测内核公式反推
> 性质：本批次是**澄清**（验收标准＝给出明确答案），不改生产交易语义；
> 仅落注释/CI 测试/本结论。生产权威配置 `/data/.agent-config.json`（mode=SHADOW）。

---

## A-1 —— 顶层 `dsl_exit.max_loss_pct = 1` 的来源与意图

**结论：历史手动调参遗留（②配置遗留），且 P6b「永远不可达」论断需修正为「出场不可达、sizing 可达」。**

| 证据 | 内容 |
|---|---|
| 配置演值（git `.agent-config.json`） | 3.5（06-11 lev4）→ 2.5（lev12）→ 0.4（09 月 lev10，仓库版） |
| 配置演值（容器 /data 备份） | 2.5/25（08-21、08-24 快照）→ **1/15（09-09 .bak 起至今）** |
| 切换窗口 | 2026-09-08 ~ 09-10 02:12 UTC 之间（log.4 截止 09-08；09-10 首条 loop_start 已为 1），**无 git 提交、无配置审计记录**（容器 /data 独立演化，不进仓库） |
| 出场引擎可达性 | **不可达**：`select_exit_params`（executor.py:940）在 regime_aware.enabled=true 下 trend 返回 0.8、non_trend 块显式给 0.4；`DSLTracker._effective_max_loss` 只读 per-regime 值 |
| sizing 路径可达性 | **可达**：`_v1_stop_width`（executor.py:3160）直接读顶层值。生产 `sizing_basis=primary_stop`、`sizing_v2_mode=shadow`（非 enforce），下单走 v1：`min(1.0, 15/10)/100 = 1.00%`。实盘下单日志逐笔 `@ 1.00% stop` |
| 当前实际影响 | 零——权益极小，notional 恒被 `$30 max_trade_notional_usd` 钳制（日志 `clamped:notional_cap`），1.0% 宽度不改变下单量 |
| 未来风险 | 放大资金 / notional_cap 放开后，v1 仍按 1.0% 假设 sizing，而真实出场止损是 0.8/0.4（sizing_v2 shadow 正在观测的 under-risk 错配） |

**处置**：定性为遗留默认值，**不可按「死值」删除**（v1 sizing 仍读）。两处读取点加权威注释
（commit `e46c1c5`）：`select_exit_params`（出场不可达说明）、`_v1_stop_width`（sizing 可达 + 历史溯源）。

---

## A-2 —— `max_concurrent = 2` 的意图

**结论：当前 10x/$30 小账户风险包的有意设计，非资金约束临时值；maxc 曲线非单调，不能靠回测扫描重选。**

| 证据 | 内容 |
|---|---|
| 配置演值（git） | 20（大账户时代，notional $5000）→ 10（$800）→ 4（lev4/$160）→ **2（83054e5 起，lev10/$30 包）** |
| 持续性 | 2 与 10x/$30 包同步确立后历经月余、多次提交、全部容器备份（08-21 至今）从未变动 |
| 代码固化 | canonical 默认即 2（config_store.py:177）；shadow_book.py:112-139 有 **SHADOW/LIVE cap parity 硬约束**（影子簿超配即告警）；回测内核 `guard.MAX_CONCURRENT_POSITIONS=2`（B-2） |
| 非单调性（P3-4） | filt_ra：maxc=2 为 **−5.20%**，maxc=6 峰 +21.21%，∞ 回落到 +19.43% —— 收益不随 maxc 单调，扫描选优无意义 |
| 实盘峰值并发 | 2（与配置一致） |

**处置**：固化为受保护常量并补意图注释（commit `6edccd8`）。任何变更改变资金暴露，
只能在 mode=SHADOW 下另行评估，不作为待优化参数。

---

## A-3 —— `spot_cap = 3.00[atr]` 的 ATR 推导公式（含 8 笔 reason 验证）

**公式（与 stop_model / DSLTracker byte-aligned）：**

```
atr_cap   = clamp(entry_atr_pct × atr_mult, atr_floor_pct, atr_ceiling_pct)
spot_cap  = min(regime_cap, atr_cap)          # [atr] 标签显示的是 atr_cap
effective = min(spot_cap, max_loss_roe_pct / lev)
```

2026-09-05 时 `atr_stop.enabled=true`、mult=1.2 / floor=1.2 / ceiling=3.0
（09-08/09 调参窗口后 atr_stop 被关，故此后 reason 不再带 `[atr]`）。

**8 行 reason（log.4，2026-09-05，4 个唯一持仓 × shadow+paper 双行）逐笔复现：**

| 币 | lev | regime cap | entry_px | entry_atr_pct | 公式 atr_cap | reason 标签 | effective/binding |
|---|---|---|---|---|---|---|---|
| TAO | 5 | 1.00% | ≈249.1（atr_abs 5.4997） | 2.208% | **2.65** | `2.65[atr]` ✅ | 1.00 / regime |
| CASHCAT | 3 | 1.00% | 0.16305 | 20.37%（触顶） | **3.00** | `3.00[atr]` ✅ | 1.00 / regime |
| SUI | 10 | 1.00% | 0.80247 | ≥2.50%（入场 tick 锁定值） | **3.00** | `3.00[atr]` ✅ | 1.00 / regime |
| CRV | 10 | 0.40%（non_trend） | 0.382 | 3.103%（触顶） | **3.00** | `3.00[atr]` ✅ | 0.40 / regime |

`[atr]` 数字是 ATR 层宽度（仅展示），**实际生效止损恒为更紧的 regime cap**
（4 笔 binding 全是 regime）。SUI 用 research 日志 12:40 的 atr_abs 算得 2.926，
略低于 ceiling；reason 的 3.00 说明入场 tick 锁定的 4h ATR 快照 ≥2.5%（触顶阈值），
公式自洽。CI 固化为 4 个测试（commit `e5b57f3`，parity 文件 155→159）。

---

## A-4 —— 实盘 breach 确认窗口是否长 2~7 倍

**结论：不是 confirm_sec 漂移（生产确为 4s）；`held` 长是「二次连续穿越 + index wick 抑制」的耗时。**

- 生产配置：`breach_confirm_sec = 4`、**`consecutive_breaches_required = 2`**（非常认默认 1）。
- reason 里的 `held Ns` ＝ `breach_elapsed`（dsl_exit.py:1299），即**首次穿越 tick → 触发成交**的总墙钟，
  触发需**同时**满足三个条件（dsl_exit.py:1296）：
  1. `breach_elapsed >= 4s`（时间门）；
  2. `consecutive_breaches >= 2`（连续 2 个 poll 都在 floor 外，价格回到 floor 内即清零，dsl_exit.py:1323）；
  3. **index（oracle）价也确认穿越**，mid 单腿穿越判为 wick 并继续等待（dsl_exit.py:1291-1321）。
- 故 `held 29.4s/95.5s/150s` 长是价格反复穿越（清零重计）+ index 未确认抑制的累积，
  **不是确认窗口被设成了 2~7 倍**。
- 回测（`bt_ra_exch.py`）是 5m bar 级、未建模秒级确认/二次穿越/index 确认——
  但该过程全在一根 5m K 线内完成，对 bar 级成交价的影响已被 B-1a-改 的当根成交模型覆盖，
  无需在回测里复刻秒级状态机。

**处置：非 bug**，记录语义即可；回测假设无需改动。

---

## A-5 —— DOT 2026-09-08 标 `exchange_trigger` 但订单是本地 IOC（10 笔唯一例外）

**结论：镜像止损单挂单失败（cloid API 参数 bug）导致无交易所止损，本地 IOC 兜底；属已修复的历史一次性故障。**

事件流（trading-loop.log，2026-09-08 15:33 UTC）：

```
15:33:31 DOT Open Long 25.5 @ 1.172（入场成交）
15:33:33 ERROR: place_hl_trigger_order() got an unexpected keyword argument 'cloid'
         → Phase2 交易所镜像 Stop Market 挂单失败，该笔无交易所止损保护
...      持仓由 DSL 本地监控
15:33:30(实际为收盘) place_hl_order(DOT, ..., {'limit': {'tif': 'Ioc'}})  ← 本地 IOC 平仓
16:27:26 [outcome-store] backfilled external close DOT @ 1.1846 oid=539488396836
```

对账回填块（trading_loop.py:1452+）用启发式把该笔标成 `close_source="exchange_trigger"`，
但实际成交单是本地 IOC —— 标签与成交通道矛盾的根因是镜像 SL 因异常没挂上。

**修复状态**：次日提交 `944f79d fix(exchange): accept and forward cloid on trigger orders (E-2 P0)`
已让 `place_hl_trigger_order` 接受并转发 `cloid`（exchange.py:1188，幂等键）。
现网该 TypeError 不可能复现。**现存风险＝无**，仅历史标签语义不精确（F-1 已统一 exchange_trigger ≡ floor 镜像）。

---

## A-6 —— 7 笔 TP 分批触发的 PnL 未入账

**结论：真实的本地记账缺口（只影响对账/影子统计，不影响交易所真实结算）；当前生产未触发，修复并入 D-7 reconcile 专项。**

- 生产 `tp_scale_fraction = 0.4`（存在分批：先平 40% 再平 60%，产生两笔减仓 fill）。
- 回填块（trading_loop.py:1457）对每个 dropped tracker 只调一次 `resolve_close_fill`，
  而该函数 newest-first **只返回最近一笔减仓 fill**（dsl_exit.py:316-356，first match 即 return）。
- 后果：交易所侧分批 TP 平仓时，回填只记最后一批的 px/sz/closedPnl —— 漏记前一批已实现 PnL，
  且 size 只是最后一批而非全仓，`memory.record_close` 的净 PnL/notional 偏小。
- **边界**：真实资金在 HL 侧逐笔正确结算（每笔 fill 带独立 closedPnl），缺口仅在本地
  closes/outcome/影子统计；走 DSL 监控内正常平仓路径的单不经过此回填。
- **当前触发情况**：trading-loop.log / log.1（约 09-15 至今）`backfilled external close` = 0、
  `external_close_unattributed` = 0，近期未走外部回填路径。7 笔是 08 月 P6b 统计窗口的历史样本。

**处置（不在 W3 改实盘对账路径，控风险）**：标记为 D-7 reconcile 回填专项的必修子项
（D-3′ 前置）。修复方向：`resolve_close_fill` 增加「聚合 since_ts 后同持仓全部减仓 fill」
模式（∑closedPnl、∑fee、∑sz、按 sz 加权 px），回填块改用聚合结果。需配套单测后再动。

---

## 批次总结

| ID | 性质 | 处置 | 落地 |
|---|---|---|---|
| A-1 | 配置遗留 + 文档论断修正 | 两处读取点加注释，禁删 | `e46c1c5` |
| A-2 | 有意设计 | 固化受保护常量 + 注释 | `6edccd8`
| A-3 | 公式澄清 | CI 4 测试对 8 行 reason 逐笔验证 | `e5b57f3` |
| A-4 | 非 bug（确认机制语义） | 记录，回测无需改 | 本文档 |
| A-5 | 历史故障，已修复 | 确认 944f79d 闭环 | 本文档 |
| A-6 | 真实记账缺口，当前未触发 | 转 D-7 reconcile 专项（含聚合 fill 修复方向） | 本文档 + 待办 |

**无一项阻塞 G1（回测可信）主线**：A-1/A-2/A-3 把回测与实盘的参数语义全部钉死，
A-4/A-5 解释了历史出场通道的两个「看似矛盾」，A-6 是边界记账问题且不碰真实结算。
