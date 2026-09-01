# Phase 3 改造收益评估报告 —— WS 实时价替换 scan 快照价

- **日期**：2026-09-01
- **改造范围**：Phase 3 替代方案（alt）—— `check_all_positions()` 内部在做 DSL 退出评估前，优先用持久化 WebSocket allMids 实时价替换 REST scan 快照价；WS 不可用/不新鲜/缺币时静默回退原逻辑。
- **核心改动文件**：[dsl_exit.py](file:///home/ldy/hermes-trader/hermes_trader/agents/dsl_exit.py#L1861-L1930)
- **验证日志**：`/tmp/trading_loop_p3_verify.log`、`/tmp/trading_loop_userfills.log`
- **结论先行**：Phase 3 让**每次退出评估点使用的价格更新鲜**（亚秒级 WS 价 vs 决策时约 0.2–2s 龄的 REST 点快照），且**零并发风险、HIP-3 自动回退、A-F14 安全 gate 不受影响**。但它**不提高退出评估频率**，因此**不缩短真正决定退出时延的 inter-cycle（轮次间）盲窗**。真正把退出时延从"分钟级"压到"亚秒级"需要事件驱动架构（方案 B，此前因并发风险否决），Phase 3 是在不引入并发的前提下能拿到的最稳收益。

---

## 1. 改造内容回顾

### 1.1 前情

前端/退出链路数据延迟改造分三个阶段：

| 阶段 | 内容 | 状态 |
|---|---|---|
| Phase 1 | WS `userFills` 订阅 + 成交回调日志（log-only） | 已完成并验证 |
| Phase 2 | 主循环 drain `userFills` 队列 + SSE `ws_user_fill` 事件落盘 | 已完成并验证 |
| Phase 3（本报告） | `check_all_positions()` 内用 WS 实时 mid 替换 scan 快照 mid 做退出判定 | 已完成并验证 |

Phase 3 采用**替代方案（alt）而非事件驱动方案（B）**：不新增线程、不在 WS 回调里触发平仓，只在**原有退出评估点**把"读哪个价格"换成更新鲜的来源。

### 1.2 Phase 3 代码路径

[dsl_exit.py](file:///home/ldy/hermes-trader/hermes_trader/agents/dsl_exit.py#L1861-L1930) 在 `check_all_positions()` 中：

1. **探测 WS**（L1865-1873）：取全局 WS mids 单例，`is_connected()` 且 `get_data_age_seconds() < 2.0` 才认为新鲜；任何异常置 `_ws=None`。
2. **逐持仓替换**（L1876-1909）：若 WS 新鲜，`get_price(coin)>0 且有限`则用 WS 价并计数 `_ws_substituted`；否则回退 `mids.get(coin)`（REST scan 快照）。
3. **部分替换日志**（L1919-1929）：仅当 `0 < 替换数 < 总持仓数` 时打 INFO（提示有 HIP-3/缺币持仓走了回退）；全替换或全回退保持静默，避免 scan 节奏刷屏。

关键性质：**读取路径完全只读**。WS 客户端在独立线程写 `RealtimeSnapshot`，退出路径只读快照字典与墙钟时间戳，无锁、无回调触发平仓，因此与主循环无并发写竞争。

---

## 2. 主循环真实时序（收益量化的前提）

经代码核对，`scripts/trading_loop.py` 每轮循环顺序为（`monitor_exits` 在 **scan 之前**，位于循环前部）：

```
account_sync (L601)
  → userFills drain (L674) + SSE 事件
  → rehydrate trackers (L776)
  → REST mids 快照  get_all_hl_mids (L957)     ← 每次新 HTTP POST，无缓存/TTL
  → feed 新鲜度判定 (L978)
  → monitor_exits → check_all_positions (L990) ← Phase 3 替换发生在这里
  → 平仓执行 (L1014)
  → scan candles (L1121)
  → research / LLM 辩论 (L1337)
  → sleep 15s (L1457)
```

两条关键事实（决定了收益边界）：

- **REST mids 快照无缓存**：[exchange.py](file:///home/ldy/hermes-trader/hermes_trader/client/exchange.py#L467-L530) `get_all_hl_mids()` 每次主簿 `info.all_mids()` 外加约 8 个 HIP-3 dex 顺序 POST。快照在 L957 拉取、L990 消费，**决策点快照年龄仅约 0.2–2s**（主簿很快，HIP-3 顺序 POST 是主要尾部）。
- **真实退出盲窗 = 两轮 `monitor_exits` 之间的周期长度**，而非快照年龄：
  - warm（热循环，仅 sleep）：**约 15–30s**；
  - 冷 scan（首轮全量扫描 + 全部币 research）：实测 **约 3 分钟以上**；
  - research 超长（LLM 慢/超时叠加）：可达 watchdog 上限 **约 600s**。

> **这是本报告最重要的澄清**：退出"慢"的主因不是价格快照旧（快照本来就只有亚秒~2s 龄），而是**评估动作本身每轮只跑一次**，轮次之间（尤其被冷 scan / 长 research 阻塞时）完全不评估。

---

## 3. 价格链路：改造前 vs 改造后

| 维度 | 改造前 | 改造后（Phase 3） |
|---|---|---|
| 退出判定用价 | REST `get_all_hl_mids()` 点快照（L957） | 优先 WS allMids 实时价；不新鲜/缺币回退同一 REST 快照 |
| 价格在决策点的年龄 | 约 0.2–2s（主簿快，HIP-3 POST 为尾部） | WS 路径 **< 2.0s**（硬阈值，实测推送亚秒级）；回退路径同改造前 |
| 价格更新方式 | 每轮一次 HTTP 请求–响应 | WS 推送持续刷新 `RealtimeSnapshot`，评估点读最新值 |
| 评估频率 | 每轮 1 次 | **每轮 1 次（不变）** |
| inter-cycle 盲窗 | warm 15–30s / 冷 ~3min / research 最长 ~600s | **不变**（Phase 3 不改变循环节奏） |
| 并发模型 | 单线程主循环 | WS 写线程 + 主循环只读快照，**无锁、无回调平仓** |
| HIP-3 币（`xyz:XXX`） | REST 快照含 HIP-3 dex 价 | WS allMids 无 dex 参数、不含 HIP-3 → `get_price` 返回 0.0 → **自动回退 REST** |
| 安全 gate（A-F14 halt / FEED-FRESHNESS） | 跑在 REST scan mids 上 | **不变**，仍跑在 REST mids 上，WS 替换仅作用于持仓退出判定 |

---

## 4. 收益量化

### 4.1 直接收益：评估点价格新鲜度

- **REST 路径**：决策点快照龄约 **0.2–2s**（[exchange.py](file:///home/ldy/hermes-trader/hermes_trader/client/exchange.py#L467-L530) 无缓存，主簿 + HIP-3 顺序 POST）。
- **WS 路径**：`get_data_age_seconds() < 2.0` 才采用（[dsl_exit.py L1871](file:///home/ldy/hermes-trader/hermes_trader/agents/dsl_exit.py#L1871)）；WS allMids 为亚秒级推送，M-10 replay 去重（sha256 payload hash）后才刷新 `last_update_time`，因此**采用的价格通常是亚秒级**，且天然过滤了重复帧。
- **净效果**：在 warm 轮，把退出判定价从"最多约 2s 龄的请求快照"提升到"亚秒级推送价"。对**紧贴止损线、波动剧烈**的持仓，这 1–2s 的价格更新意味着 `floor_breach` / `hard_stop` 判定基于更接近真实市价的 mark，减少因快照滞后导致的"该触发未触发 / 触发价偏差"。

> 诚实说明：由于 REST 快照在决策点本就很新（0.2–2s），**这一项收益的绝对幅度有限**——它是"把 2s 缩到亚秒"，不是"把分钟缩到亚秒"。

### 4.2 消除了 REST 快照的尾部抖动风险

REST `get_all_hl_mids()` 需顺序 POST 主簿 + 约 8 个 HIP-3 dex。当某个 HIP-3 dex 响应慢时，整批快照等待，决策点拿到的主簿价也随之变老（尾部可达 2s 甚至更久）。WS 路径的主簿原生 perp 价**不依赖这批 HIP-3 POST**，原生 perp 持仓的退出判定价不受 HIP-3 慢请求拖累。

### 4.3 稳健性收益（主要价值所在）

1. **零并发风险**：只读快照，不新增线程、不在回调里平仓。相比事件驱动方案 B（需引入锁/条件变量/重入保护，且与主循环持仓状态存在写竞争），Phase 3 不改变任何并发模型，回退即恢复原行为。
2. **三重自动回退**：WS 未启动 / `is_connected()` 假 / `data_age ≥ 2.0s` / 该币 `get_price` 返回 0（HIP-3 或缺币）/ 任何异常 —— 全部静默回退 REST 快照，**不会因 WS 故障导致退出判定缺价**。
3. **脏数据防护继承**：WS 侧 M-11 spike 过滤（单帧跳变 >25% 抑制，[ws_client.py L266-L312](file:///home/ldy/hermes-trader/hermes_trader/client/ws_client.py#L266-L312)）+ M-10 replay 去重，退出判定不会吃到异常脉冲价。
4. **安全 gate 不受影响**：A-F14 halt / FEED-FRESHNESS 准入门仍跑在 REST scan mids 上（[exchange.py](file:///home/ldy/hermes-trader/hermes_trader/client/exchange.py#L543-L557) `MID_FEED_MAX_STALE_S=30.0`），WS 替换只影响"已准入持仓的退出价"，不改变开仓/停机决策，攻击面与故障域隔离。
5. **断线自愈**：WS 监视器每 5s 轮询，`data_age > 30s`（`ws_max_stale_s`）触发重连并自动重订阅 allMids + userFills（[ws_client.py L744-L783](file:///home/ldy/hermes-trader/hermes_trader/client/ws_client.py#L744-L783)）；重连期间 `data_age ≥ 2.0`，Phase 3 自动回退 REST，无缝降级。

### 4.4 可观测性

- 部分替换（原生 perp 用 WS、HIP-3 持仓回退）打 `[dsl:ws-mid] N/M positions used WS real-time mid` INFO（[dsl_exit.py L1919-L1927](file:///home/ldy/hermes-trader/hermes_trader/agents/dsl_exit.py#L1919-L1927)），运维可直接确认替换路径是否被执行。

---

## 5. 局限与诚实边界

### 5.1 不缩短 inter-cycle 盲窗（最核心局限）

Phase 3 **只在原评估点换价，不增加评估次数**。真正的退出时延由两轮 `monitor_exits` 间隔决定：

- warm 轮约 15–30s、冷 scan 约 3min、长 research 可达 ~600s。
- 在这些盲窗内，**即使 WS 价格早已越过止损线，也要等下一轮 `monitor_exits` 才会评估**。
- 因此，若改造目标是"价格一碰止损就亚秒级平仓"，**Phase 3 不达成该目标**——那需要方案 B（WS 价格帧事件驱动 / 条件变量唤醒退出评估），此前因与主循环持仓状态的并发写竞争风险而否决。

### 5.2 breach 确认 gate 的实际节奏未变

DSL 退出确认参数（[dsl_exit.py](file:///home/ldy/hermes-trader/hermes_trader/agents/dsl_exit.py)）：

- `breach_confirm_sec = 4.0s`（L483），设计为"跨两次 poll 持续"；但 poll 间隔约 15s ≫ 4s，实际表现为**首次 poll arm、下一次 poll（约 15s 后）才确认**。
- `consecutive_breaches_required = 1`（L465）、`hard_stop_confirm_sec = 1.0s`（L496）、noise_band 默认关闭（L507）。

Phase 3 让每次 poll 用的价更新鲜，但 **poll 节奏（约 15s）不变**，所以 `breach_confirm` 的"跨轮确认"时延结构不变。

### 5.3 HIP-3 持仓无 WS 收益

WS allMids 订阅 `{"type":"allMids"}` 无 dex 参数（[ws_client.py L648-L651](file:///home/ldy/hermes-trader/hermes_trader/client/ws_client.py#L648-L651)），只含原生 HL perp。`xyz:XXX` 币 `get_price` 返回 0.0 → 恒走 REST 回退。**HIP-3 持仓在 Phase 3 下行为与改造前完全一致**（日志中 `xyz:SHAZ/CRWD/SOFTBANK/QCOM/CRCL` 等均为 HIP-3，candles quality-gate 也显示它们走独立链路）。

### 5.4 `is_connected()` 为弱判定

[ws_client.py L808-L809](file:///home/ldy/hermes-trader/hermes_trader/client/ws_client.py#L808-L809) 仅判 `_running and _info is not None`，不反映 socket 真实存活。Phase 3 靠 `get_data_age_seconds() < 2.0` 硬阈值兜底——socket 假死但停止推送时，`data_age` 会迅速超过 2s 从而回退 REST，安全性成立；但这也意味着**网络抖动导致推送间隔 >2s 时会频繁回退**，WS 收益在抖动期暂时消失（功能无损，仅新鲜度退回改造前水平）。

### 5.5 本次验证未在实盘持仓下触发替换

两次验证运行 `rehydrated 0 tracker(s)`（无持仓），因此 `_ws_substituted` 替换计数路径**未被真实持仓执行**（循环体 0 次）。已验证的是：WS 数据流活跃、新鲜度判定与回退分支可达、无 traceback；**替换计数与部分替换日志有待有持仓时复测**。

---

## 6. 验证证据

### 6.1 Phase 3 WS 链路（`/tmp/trading_loop_p3_verify.log`）

- WS 管理器以 certifi SSL 正常启动；`[ws] Subscribed to allMids (sub_id=1)`（03:20:43）。
- 当时未配地址 → `[ws:user-fills] skipped — no user address configured`（Phase 1 正确跳过）。
- `[dsl] rehydrated 0 tracker(s) from disk`。
- 冷扫描 → 决策 → `Sleeping 15s` → 第二轮完整循环，节奏正常。
- **M-11 spike 过滤持续触发**，证明 WS 数据流活跃且脏数据防护生效，例如：
  - `M-11 spike suppressed: @698 mid jump 0.0311 -> 0.0514 (65.4% > cap 25.0%); keeping previous mid`（约每 5s 一帧）。
- 全程 **0 traceback / 0 FATAL**；仅有 `research OpenRouter-EMPTY`（本地未配 `OPENROUTER_API_KEY`，良性，与 WS/退出链路无关）。

### 6.2 userFills + SSE 全链路（`/tmp/trading_loop_userfills.log`）

配置 `HYPERLIQUID_MASTER_ADDRESS=0x7833…536E` 后重启：

- `[ws:user-fills] subscribed user=0x7833…536E sub_id=2 (Phase 1 log-only)`（04:28:47）——由 skipped 变为真实订阅。
- 收到 **30 条**真实成交帧（ZRO/SPX/FARTCOIN/PURR/WLFI/BTC/ETHFI/STX/JUP/CASHCAT 等 Open/Close Long）。
- 主循环 drain **30 条全部排空**，`is_close` 判定正确：**16 条 `is_close=True`**（Close Long 且 closedPnl≠0，如 PURR -0.487512、WLFI +0.209348、BTC +0.08205、ETHFI +0.655016）。
- session-log 落盘 **16 条** `ws_user_fill` SSE 事件（`~/.hermes-trader-session-log.jsonl`），与 drain 的 close 数一一对应。
- 说明：30 条为订阅瞬间 HL 推送的**历史成交快照重放**（`t` 为过去时间戳），非实时新成交；实时推送待下一笔真实成交，但数据链路已完整打通。

### 6.3 inter-cycle 盲窗实测（顺带佐证第 5.1 节）

userFills 运行中，成交帧于 **04:28:51** 到达，但主循环直到 **04:32:17** 才 drain（冷 scan 阻塞）：

- 启动 grace delay 12s（04:28:31）→ 首轮冷 scan + 全币 research → 首次 `Sleeping 15s` 在 **04:32:00**。
- 即 **fills 到达 → drain 延迟约 3 分 26 秒**，完全被冷 scan/research 阻塞。
- 第二轮（warm）：04:32:00 sleep → 04:33:53 sleep，周期约 **113s**（含该轮 research）。

这组数据直观印证：**轮次间阻塞（分钟级）才是退出/成交响应时延的主导项，价格快照龄（亚秒~2s）不是瓶颈**。Phase 3 优化的是后者，未触及前者。

---

## 7. 总体评价与后续建议

### 7.1 评价

| 项 | 结论 |
|---|---|
| 功能正确性 | 通过（WS 数据流、新鲜度 gate、回退分支、0 traceback） |
| 价格新鲜度收益 | 评估点价从约 0.2–2s REST 快照 → 亚秒级 WS 价（原生 perp）；**幅度有限但方向正确** |
| 时延瓶颈是否解决 | **否**——inter-cycle 盲窗（15s~600s）不变，评估频率不变 |
| 安全性 | 高——零并发、三重回退、A-F14/FEED gate 不受影响、断线自愈、spike 防护继承 |
| 风险 | 极低——纯只读加价源，回退即原行为；HIP-3 与无持仓场景完全等价改造前 |
| 待复测 | 有真实持仓时的 `_ws_substituted` 计数与 `[dsl:ws-mid]` 部分替换日志 |

**一句话**：Phase 3 是一次"低风险、稳收益、但不解决根本时延"的加固——它让每个退出决策点用的价格更接近真实市价，并把 WS 实时数据以零并发方式引入退出链路；但要实现"亚秒级止损响应"，仍需事件驱动架构（方案 B）。

### 7.2 后续建议（按性价比）

1. **有持仓复测**：在持有原生 perp 仓位时跑一轮，确认 `[dsl:ws-mid] N/M` 日志与 `_ws_substituted>0`，补全替换路径实证。
2. **缩短 inter-cycle 盲窗（真正降时延，不动并发模型的折中）**：
   - 在长 research / 冷 scan 期间，插入轻量 `monitor_exits` 检查点（只跑退出评估、不跑 scan/research），把最长盲窗从 ~600s 压到秒级。这比完整方案 B 并发风险小，且直接命中瓶颈。
3. **若未来采纳方案 B**：以条件变量唤醒独立退出评估线程，务必复用现有 `RealtimeSnapshot` 只读模式 + 单写者原则，并对平仓动作做幂等/重入保护。
4. **HIP-3 实时价**：如需 HIP-3 也获 WS 级新鲜度，需评估各 HIP-3 dex 是否提供 WS 推送（当前 allMids 通道不含），否则维持 REST 回退即可。
5. **`is_connected()` 强化（可选）**：纳入 `data_age` 判定或 socket 存活探测，使连接状态语义与实际推送健康一致，便于运维监控。

---

*附：本报告基于 2026-09-01 代码与两次实盘验证日志。时序常量（scan_interval=15s、ws data_age 阈值 2.0s、ws_max_stale 30s、MID_FEED_MAX_STALE 30s、breach_confirm 4s、watchdog ~600s）均取自当前源码。*
