# Hermes-Trader 盈利入场模块改造效果 · 全面复核与实盘上线评估

- 日期：2026-10-28（Asia/Shanghai）
- 评估对象：盈利入场失效诊断后的 S0/S1/S2 + WF-A/B/C 改造链（maker v2、xs_reversal 超跌反弹臂、panic gate、退出冻结、扫描覆盖、影子取证基建）
- 评估方法：只读复核。所有结论来自仓库 `analysis/` 实证产物、生产源码行号、以及对运行容器 `hermes-trader` 的实测（非引用旧报告）。
- 当前代码锚点：`8e18a84`（已部署、healthy、双 remote 同步）。
- 一句话结论：**工程/构造缺陷整改到位、影子取证链已闭合且零资金风险；但新入场臂（xs_reversal）在"生产场所 Hyperliquid"上统计不显著，当前不具备把该臂转为实盘的条件。系统应继续以 SHADOW 运行，靠时间累积去相关独立恐慌事件。现有生产做多臂本就有完整风控，但本次改造未改变其 LIVE 授权状态。**

---

## 0. 结论速览（决策层先看）

| 维度 | 评级 | 摘要 |
|---|---|---|
| 原构造缺陷修复覆盖率 | **高（工程项 6/6，证据项部分成立）** | maker 乐观偏差、逆选择未计价、成交模型 bug、取证 5 天滚删、字段零信息、扫描覆盖、白名单 CI 红灯全部修复 |
| 入场信号准确率（新 xs 臂） | **有条件成立** | Binance 3.4 年门内 +297bp/t2.83/CI 全正；HL 生产场所去相关后仅 3 个独立恐慌事件、t=1.17，**统计未确认** |
| 回测稳定性/参数鲁棒性 | **中-高（Binance）/ 不适用（HL 样本不足）** | 逐年全正、邻域 12 格全正、OOS t=3.01、LOMO 正；但 edge 高度集中于"少数大恐慌 + SOL 币"，2026 全样本≈0 |
| 极端行情容错 | **中-高** | cloid 幂等、响应丢失三态对账、部分平仓补单、服务器端备份止损、LKG 熔断缓存；存在 4 个明确 fail-open 点（见 §5） |
| 实盘接口兼容性 | **高，且当前物理隔离** | SHADOW 在任何 SDK 调用之前硬拦截；mode=SHADOW、allow_shorts=false、真实成交=0 实测确认 |
| 风险控制有效性 | **高（生产臂）/ 不适用（xs 臂无实时风控）** | LIVE 三层闸（env + HMAC 记录 + 模式）；xs 臂 enforce 是 no-op，promote 前必须补组合级止损与仓位上限 |
| **是否具备实盘上线条件** | **否（针对本次盈利入场改造）** | 不是工程没做好，而是**证据与时间不足**：HL 去相关独立恐慌事件 n=3 < 闸门 10，且无任何口径 t≥2 |

**关键判断**：原"80% 构造缺陷 / 20% 市场环境"的归因，在**工程层面已被充分整改**；但整改后用更严格的去相关统计重测，反而证明了一件更重要的事——**这不是一个"修好 bug 就能稳定赚钱"的入场，而是一个 edge 真实但稀疏、强 regime 依赖、需要耐心等大恐慌的均值回归机会**。提前上线违背团队自己冻结的 promote 判据。

---

## 1. 原归因 → 改造点逐条核销（要求 1：覆盖率与修复有效性）

原始诊断：系统性构造缺陷 ~80%（策略范式只做多追突破 + maker 成交模拟双重乐观 + 取证/统计口径缺陷），市场环境 ~20%（低波震荡，钱在超跌反弹）。

| # | 原构造缺陷 | 改造点 | 证据 | 是否真正解决 |
|---|---|---|---|---|
| C1 | maker v1"触及即成交"虚高成交率 | maker v2 队列穿透概率 p=base·decay^k | [maker_shadow.py](file:///home/ldy/hermes-trader/hermes_trader/execution/maker_shadow.py)；BTC 1m 实测 v1 touch 71.1% → v2 无条件良性成交 21.2% | **是**（成交率乐观被纠正） |
| C2 | 固定 +5bp maker edge 未计逆选择 | v2 引入 adverse drift（κ=1.0）计价 | 1m 逆漂移中位 +2.49bp（触及后），5m +6.46bp | **是**（且结论是 maker 路径负 edge，**否决上线**） |
| C3 | v2 实时账本 p 恒<1 → 永不成交（硬 bug） | blake2b 等概率 hash draw | 4000 单实测成交率 0.301≈base；单测固化 | **是**（但 v2 仍默认关） |
| C4 | 策略范式错配（只追突破，错过超跌反弹） | 新增 xs_reversal 超跌反弹臂（只做多、影子） | [xs_reversal.py](file:///home/ldy/hermes-trader/hermes_trader/agents/xs_reversal.py)，record-only | **机制补齐，但 edge 待生产场所确认**（见 §2） |
| C5 | 无门 xs edge 不显著（t1.93、CI 含零、2026 消失） | panic gate（ext≤−8 ∧ ATR 分位≥0.6 ∧ RSI≥20），record-only | Binance 门内 +297bp/t2.83；币内 ATR 分位解决跨币量纲 | **是（在 Binance 上）** |
| C6 | 借用 5m scalp 的 1.5% 硬止损会摧毁均值回归 edge | 退出参数冻结：仅 72h 时间止损 | [ADR-0010](file:///home/ldy/hermes-trader/docs/adr/0010-xs-exit-72h-time-stop-freeze-top-pct-85.md) Accepted；1.5% 止损 76-87% 先洗出，net 仅 +50bp | **是（研究冻结），但实时退出代码尚未实现** |
| C7 | 统计口径虚高（重叠 episode 当 iid） | 72h 去相关 episode + 日块 bootstrap + 事件级 gap3d 聚类 | 449 笔 naive −1.8bp → 去相关后 HL 仅 3 独立事件 | **是，且这是最重要的"诚实收紧"** |
| C8 | top_pct 85/90 配置-验证错配 | 同信号流双计数，定格 85 | 85-only 带 +295bp/t2.26/CI 全正；90 砍 40% 样本无增益 | **是** |
| C9 | 扫描覆盖疑似缺口（只扫 60 币） | WF-C 反事实覆盖核算 | 单周期扫 25.6% 币种但覆盖 98.6% 成交额；7 周期轮完全部 ≥$300k | **是，机制无结构缺陷**（残余：28 个 $1-4M 市场反应慢） |
| C10 | 影子取证 5 天滚删 → 60 信号日闸门不可达 | xs 流留存改 90 天 + reconcile 落**永不轮转只增台账** | commit `8e18a84`，[shadow_log.py](file:///home/ldy/hermes-trader/hermes_trader/shadow_log.py) per-path 留存、[reconcile](file:///home/ldy/hermes-trader/scripts/reconcile_xs_reversal_shadow.py) ledger | **是**（本轮验收修复） |
| C11 | panic_gate_pass 恒 false（开关关着）零信息 | pass 改纯条件判定 + 新增 gate_active | commit `8e18a84`，关闭态也累积条件证据 | **是** |
| C12 | 同 bar 重复写（8s 扫描重复 append） | (ts,coin) 查盘去重 | commit `8e18a84`，跨扫描/重启只留一条 | **是** |
| C13 | 部署白名单 CI 红灯 | 登记 postdeploy_shadow_checks.py | 白名单测试转绿 | **是** |

**覆盖率结论**：可识别的工程/构造缺陷 **13 项全部有对应修复并有证据**，其中 11 项已验证有效、2 项（C6 实时退出、C4 生产场所 edge）属于"研究已决、工程/promote 待办"，不是缺陷未修。maker 方向（C1/C2/C3）的最终结论是**负 edge、否决上线**——这本身就是诊断成功：避免了把一个虚高 +5bp 的 maker 臂推到实盘。

---

## 2. 入场信号准确率与回测稳定性（要求 2、3）

### 2.1 xs_reversal 超跌反弹臂（本次盈利入场的主角）

**无门全样本（3.4 年 Binance 1h，BTC/ETH/SOL 池）** — [xs_regime_conditional](file:///home/ldy/hermes-trader/analysis/xs_regime_conditional_2026-10-27.json)：
- 333 独立 episode / 197 独立日，72h 净 **+87.5bp，日块 t=1.93，CI[−0.7,+169] 含零**；
- **2026 子样本 72 ep：−1.6bp / t=0.19** —— edge 在近期消失。
- 结论：无门不达标，必须靠条件分离。

**panic gate 门内（ext≤−8% ∧ 币内 ATR 分位≥0.6 ∧ RSI≥20）**：
- Binance：98-120 ep / 79-95 独立日，净 **+297bp、WR 68%、日块 t=2.83、CI[+65,+375]/[+79,+393]**；
- **逐年 2023/24/25/26 全正**（2026 +231bp，n=21）；阈值邻域 12 格全正；OOS 2025-26 t=3.01；LOMO（剔最差月）正；
- 条件单调性清晰：ext T1（最深，≤−9.29%）+296bp/t3.35 vs T3（浅）−33bp；ATR T3（高波）+233bp/t2.48 vs T1（低波）−6bp；RSI 极低（≤25，接刀）−29bp。机制自洽。

**跨数据源外生验证（生产场所 Hyperliquid，81 币 ×208 天）** — [panic_event_cluster](file:///home/ldy/hermes-trader/analysis/xs_hl_panic_event_cluster_2026-10-27.json)，这是最关键、也最不利的一关：
- naive 按笔 449 笔：**−1.8bp / t=−0.03 / WR 47.7%**（"量大"是横截面相关造成的方差低估）；
- 日等权 80 日：+173bp / t=1.27 / CI 含零；
- **按 72h 持仓窗 gap3d 去相关：208 天只有 3 个真正独立的全市场恐慌事件**，虽全正（+521bp/WR100%）但 **n=3、t=1.17、CI 极宽**；
- 逐月 2026-06 显著为负（−151bp/t−0.86）；
- **没有任何口径 t≥2**。

> 这是"失效案例统计"的核心：在 HL 生产场所，按笔口径的 449 个信号里，去相关后能提供独立统计信息的只有 3 个事件，naive 的"高笔数"是**假样本量**。门在日常小超跌上不赚钱，edge 几乎全部来自少数大恐慌——而 HL 这 7 个月大恐慌 regime 太少。

### 2.2 maker 挂单入场（已被否决，不上线）

真实 Binance 1m（BTC 30 天 43200 根）[maker_v2_calib](file:///home/ldy/hermes-trader/analysis/maker_v2_calib_BTCUSDT_1m_30d_2026-10-27.json)：
- v1：71.1% 触及成交、记 +3.43bp（双重乐观）；
- v2：无条件良性成交率仅 21.2%，计逆选择后 72h 净 **−4.76bp（1m）/ −15.07bp（5m）**；
- 触及后 runaway（价格脱离）概率 60.5%，逆漂移中位 +2.49bp（1m）/ +6.46bp（5m）。
- 结论：**maker 路径无正 edge，维持 v1 默认、不启用 v2**。运行账本实测 155 笔 fill_model 全 v1、0 笔 v2，与此一致。

### 2.3 分周期 / 分市场环境表现（要求 3）

| 环境切片 | 门内表现 | 解读 |
|---|---|---|
| 2023（Binance，熊末/震荡） | +325bp，n=7 / 或池 +1.13%/ep | 正，但 n 小 |
| 2024（含牛） | +293bp，WR67.5% | 正 |
| 2025 | +275bp，WR63.5%；OOS 25-26 t3.01 | 正，样本外稳定 |
| 2026（近期，低波/震荡） | 门内 +231bp（n=21）；但**无门全样本 −1.6bp** | edge 仅在真恐慌时存在，日常消失 |
| 高波动（ATR T3 >1.19%） | +233bp / t2.48 / CI 全正 | 核心盈利区 |
| 低波动（ATR T1） | −6bp / t0.36 | 不赚钱（被门正确排除） |
| 深回撤（ext≤−9.29%） | +296bp / t3.35 | 核心盈利区 |
| 浅回撤（ext>−6.31%） | −33bp | 被门排除 |
| BTC 24h 大跌（≤−3.61%） | +184bp（CI 下界 −6.7，临界） | 方向富集但跨币相关强 |
| **HL 2026-06（生产场所熊月）** | **−151bp / t−0.86** | **唯一显著为负的月份，promote 前必须能解释/承受** |

**失效案例口径**：
- 若按"门内 72h 净收益为负"计：Binance 门内 WR 68%，即约 **32% 单笔失效**，但日块/事件级聚合后总期望显著为正（单笔亏损由深跌反弹的右尾补偿，这正是"仅时间止损、不设紧止损"的原因）。
- 真正意义的"策略级失效"是 **HL 2026-06 这类没有大恐慌、阴跌的月份**：日等权 −151bp。去相关后全 HL 样本 3 事件不足以排除这种月份再现。

### 2.4 参数鲁棒性
- top_pct：85 vs 90 同流双计数，85-only 反事实带 +295bp/t2.26/CI[+49,+517] 全正 → 定格 85 对邻域不敏感；
- 退出：所有紧价格止损（1.0/1.5/2/3% × H48/72/96）净收益都远低于仅时间止损、CI 全含零 → 结论对止损格稳健；
- ATR 阈值：固定 1.0% 有跨币偏斜（SOL 占 64%），改用币内 trailing-90d 分位 q0.6 后三币各自 CI 下界全正、占比极差 59pp→45pp → 量纲鲁棒性提升；
- 风险提示：edge 在币种上仍偏 SOL（q0.6 下 SOL 53%），**未做到完全去单币依赖**。

---

## 3. 扫描/数据/接口/部署状态（要求 2 接口兼容、要求 4 现状）

### 3.1 扫描覆盖（WF-C）
- 单周期扫 60/234 币（25.6%）但覆盖 **98.6% 成交额 / 67% OI**；42 量 + 10 movers + 8 sweep；
- sweep 环 101 个、**7 个周期轮完**所有 ≥$300k 市场；机制无结构缺陷；
- 唯一缺口：28 个日成交额 $1-4M 的尾部市场（PENDLE/ETHFI/DOT/FIL 等）最坏 6 周期才扫到，反应慢——对一个持仓 72h 的臂影响有限。

### 3.2 数据
- 生产行情仅 Hyperliquid（HTTP /info + 持久 WS），K 线 fail-closed（3 次重试失败抛 CandleFetchError），有 gap/stale/coverage 质量门、mid 30s 陈旧上限、WS 坏报价 >25% 跳变剔除；
- Binance 仅作第二源比价且 fail-OPEN；研究脚本用的 Binance/本地 cache 不打包进运行镜像；
- 实测近 30 分钟 scan：0 errors、0 data-gaps。

### 3.3 实盘接口与 SHADOW 隔离（实测）
- 唯一下单封装 [exchange.py](file:///home/ldy/hermes-trader/hermes_trader/client/exchange.py)，所有写函数第一行 `if not PRIVATE_KEY_HEX: return {ok:False}`；
- SHADOW 终端拦截在 [executor.py:4594-4604](file:///home/ldy/hermes-trader/hermes_trader/agents/executor.py)，位于私钥检查和任何 SDK 调用**之前**，只写纸面账本返回 `executed:False`；
- 实测容器：**mode=SHADOW、allow_shorts=false、scan_long_only=true、maker_execution.enabled=false、maker_fill_model 默认 v1、panic gate 未启用、HYPERLIQUID_TESTNET=false、真实 executed fills=0**；纸面 taker 312/ maker 155 笔，当前持仓 0；
- 容器 `Up (healthy)`、FailingStreak=0、启动以来 0 Traceback（仅 GDELT/LLM debate 的外部 SSL 超时，有降级兜底）。
- 高敏感事实（需知情）：生产 env 里 `HERMES_ENABLE_LIVE=true` 已预置。当前因 mode=SHADOW 在上层被拦，但**一旦 Web 端把 mode 翻 LIVE 且存在有效 B-13 HMAC 准入记录（绑定 config sha256、30 天 TTL），即对主网发真单，且 mode 配置热加载无需重启**。这是设计使然，但意味着上线动作是"一次配置切换"，必须在前置闸门全部通过后才执行。

---

## 4. 风险控制逻辑有效性（要求 2）

### 4.1 生产做多臂（成熟、多层）
- **LIVE 三层闸**：mode 默认 OFF（[config_defaults.py](file:///home/ldy/hermes-trader/hermes_trader/agents/config_defaults.py)）→ env `HERMES_ENABLE_LIVE` → B-13 HMAC 限时准入（启动 + 每单复检，绑定 config sha256）；per-coin enabled；
- **入场门**：confidence≥0.60、价格偏离、晚追、max_concurrent=2、单票 notional cap、日内亏损 kill switch（USD 与权益%取紧）、盈利回撤 30%、流动性地板、白名单、冷却、强平缓冲、反手/金字塔禁止（持仓记录缺失 fail-closed）、主流币相关性上限、权益名义金倍上限；
- **熔断**：单币亏 3% 停 60min、日亏 5% 全局停 120min、连亏 3 停、单币日亏 5% 停、**14 天滚动 15% 最大回撤冻结 24h**、response_unknown 连 3 全局停；市场级熔断（BTC/ETH 5m 三根≥2% 崩、3 币 DSL 聚集）生产 enforce；
- **ROE ≤ −50% 一次性翻 OFF**；全局/单币熔断默认自动市价平仓；
- **出场**：DSL 价格止损（生产 1.5%，趋势 4%）+ ROE 15% + hard_timeout 240min + 二次确认/4s 窗/指数价防插针 + **交易所端 reduce-only 备份止损**（HYPE 事故后加 ceiling clamp）；
- **成交一致性**：cloid 幂等、response_unknown 三态对账（confirmed fill/not-fill/查询失败→rehydrate+连 3 全局停）、部分平仓残留自动补 reduce-only、重启 rehydrate（未知年龄仓位 fail-closed 按超时退出）、每日 userFills 对账 cron。

### 4.2 xs_reversal 臂（当前无任何实时风控——这是 promote 的硬缺口）
- 模块物理上无法下单（不 import executor/exchange）；enforce 分支是 record-only no-op（[xs_reversal.py:578-587](file:///home/ldy/hermes-trader/hermes_trader/agents/xs_reversal.py)）；
- **"仅 72h 时间止损"只存在于离线研究，实时交易代码里没有任何 exit/止损/仓位上限/notional 上限**；
- ADR-0010 已明确：promote 前必须先实现 72h 实时时间退出 + **远宽组合级灾难止损（建议币级 −15~−20%，禁用 1.5%）** + 仓位/notional 上限。

---

## 5. 待优化风险点（按优先级，均带代码/数据事实）

| 级别 | 风险点 | 事实 | 整改建议 |
|---|---|---|---|
| **P0** | xs 臂无实时退出/止损/仓位上限 | enforce no-op，ADR 列为前置 | promote 前实现 72h 时间退出 + 组合级远宽止损 + notional 上限，并补单测/影子对账 |
| **P0** | 生产场所 edge 统计未确认 | HL 去相关 n=3、无口径 t≥2、6 月显著负 | 维持 SHADOW，等独立恐慌事件 ≥10（gap3d）且事件级 t≥2、CI 下界>0 |
| P1 | 记忆型熔断冷启动/失明>1h  fail-OPEN | [risk_gates.py:721-723](file:///home/ldy/hermes-trader/hermes_trader/agents/risk_gates.py)，LKG 仅进程内不跨重启 | 把 LKG 持久化到 /data，使重启后仍保持上次 BLOCK；或对冷启动 fail-closed 开仓 |
| P1 | 备份止损挂单失败默认不强平裸仓 | flatten_on_sl_missing=false（生产），HYPE 曾 −252% ROE | LIVE 前评估对新臂/高波币改为 opt-in 强平或缩短补挂轮询 |
| P1 | HERMES_ENABLE_LIVE=true 已预置 + mode 热加载 | 一次 Web 切换即对主网发单 | 上线前用演练确认 B-13 记录/config sha256/切换审计与回滚分钟级可行；考虑加二次人工确认 |
| P2 | 市场熔断对数据缺口 fail-OPEN | market_circuit.py:38-41 | 可接受（不误杀），但需在监控大盘显式标"行情降级中，崩盘保护未 arm" |
| P2 | 限频共享锁故障 fail-open | rate_limit.py flock 异常 grant | 有 429 有界重试兜底，低优先 |
| P2 | 开仓侧无对称部分成交补单 | 仅有平仓侧补单 | LIVE 放量前补开仓残留补单或显式接受 rehydrate 延迟 |
| P2 | edge 偏 SOL + 偏大恐慌事件 | q0.6 下 SOL 53%；HL edge 集中 3 事件 | promote 时设分币权重上限/相关性限额，避免单币 regime 决定盈亏 |
| P3 | dead-man STALE 默认只告警不平仓；零成交对账仅 WARN | watchdog.py | 切 LIVE 前按注释重审并决定是否配 emergency-close-cmd |
| P3 | DEPLOY.md 仍写旧 Fly.io 部署 | 与当前 docker 宿主分叉 | 更新文档，避免值班按错操作手册 |

---

## 6. 实盘上线可行性结论（要求 4）

### 6.1 结论
- **本次"盈利入场改造"（xs_reversal 臂 / maker v2）当前不具备实盘上线条件，应继续 SHADOW。** 这不是工程失败——13 项构造缺陷已整改、风控与隔离成熟——而是**生产场所的统计证据不足且时间不可压缩**：HL 去相关后只有 3 个独立恐慌事件，团队自设闸门是 ≥10 且事件级 t≥2、CI 下界>0。任何基于 Binance 显著或 HL 笔数多的提前上线，都会重蹈"naive 449 笔实际 −1.8bp"的假样本量覆辙。
- maker v2 已被真实数据证伪（负 edge），**明确不上线**，维持 v1 纸面。
- **现有生产做多臂**的风控/容错/接口成熟度本身是可 LIVE 水准（三层闸 + 全套熔断 + 备份止损 + 幂等对账），但它的 LIVE 授权是独立决策，**本次评估不替它背书、也未改变其当前 SHADOW 状态**。

### 6.2 上线前置验证步骤（按顺序，全部满足才把 xs 臂 mode 改 enforce→LIVE）
1. **证据闸门（只能靠时间）**：HL/实盘影子累积去相关独立恐慌事件（gap3d）**≥10**，事件级 **t≥2 且日块/事件 CI 下界>0**；并能对 2026-06 型阴跌月给出"可承受"的组合级回撤结论。取证基建（90 天留存 + 只增台账 + 纯条件 pass）已在 `8e18a84` 就位，无需再改。
2. **实现实时退出与风控（P0 工程）**：72h 时间止损 + 远宽组合级灾难止损（−15~−20%，禁止 1.5%）+ 单票/组合 notional 上限 + 分币权重/相关性限额；在 enforce 路径落真实下单但先接 testnet/paper。
3. **退出回放再验**：用该臂自己的 gated 信号对新实现的实时退出做历史回放，确认与离线 +297bp/t2.83 口径 parity（含 9.26bp 成本、次根 open 入场）。
4. **灰度**：先 testnet 或主网极小 notional enforce，跑 ≥ 若干个 72h 持仓周期，做成交/滑点/账本每日对账（reconcile cron），验证实时 fill 与 72h outcome 台账一致。
5. **熔断 fail-open 收口（P1）**：至少把记忆型熔断 LKG 持久化，确认重启窗口不裸奔；决定裸仓强平策略。
6. **切换演练**：在 B-13 记录/config sha256/mode 热加载机制上做一次"翻 LIVE→急停回 SHADOW"演练，确认分钟级回滚与飞书告警；HERMES_ENABLE_LIVE 预置项双人复核。
7. **闸门签字**：更新 ADR-0010 状态附录，记录上述每条的实测结果与策略/风险/资金签字，再执行配置切换。

### 6.3 配套操作指引（当前可立即做的安全动作）
- 维持现状：容器 healthy、SHADOW、零真实成交，让 xs 影子流与只增台账开始累积（每次 cron reconcile 写 `/data/xs_reversal_outcomes.ledger.jsonl`）。
- 可选（零资金风险、提升证据质量）：在配置中把 `xs_reversal.panic_gate_enabled` 置 true（mode 仍 shadow）——新语义下 pass 已是纯条件量，开启 active 标记后可直接交叉验证"开关判定"与离线门，仍不下任何单。
- 每月用 `xs_hl_panic_event_cluster.py` 同口径（不调参）复核事件数与事件级 t；达到 §6.2-1 闸门再进入工程实现阶段。
- 回滚锚点：`8e18a84`（部署脚本已打印回滚步骤）。

---

## 附：本报告引用的关键实证产物
- `analysis/xs_episode_stats_2026-10-27.json`（3.4 年去重叠 episode/日块 bootstrap/逐年）
- `analysis/xs_regime_conditional_2026-10-27.json`（条件分层 + 推荐门 + OOS）
- `analysis/xs_exit_param_freeze_2026-10-27.json`（退出网格，仅时间止损胜出）
- `analysis/xs_hl_panic_event_cluster_2026-10-27.json`（HL 81 币事件级去相关，n=3）
- `analysis/xs_top_pct_dual_count_2026-10-28.json`（85/90 同流双计数）
- `analysis/maker_v2_calib_*_2026-10-27.json`（maker 1m/5m 负 edge 证据）
- `analysis/wfc_scan_coverage_2026-10-27.json`（扫描覆盖反事实）
- `docs/adr/0010-xs-exit-72h-time-stop-freeze-top-pct-85.md`（Accepted 退出/参数冻结 + promote 闸门）
- 运行容器实测（2026-10-28 ~12:10 UTC）：mode/allow_shorts/fills/健康/日志
