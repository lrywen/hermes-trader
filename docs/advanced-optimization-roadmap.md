# 进阶优化方向路线图

- 审计标注：Audit 2026-09-03 P3-15
- 性质：**研发方向登记**。以下方向均未实现（代码中无对应实现），本文件
  记录目标/原理/收益估计/难度/优先级/依赖，供排期；每项落地需单独评审，
  且遵守"新增功能带环境变量开关、默认安全值、不碰策略核心逻辑"约束。

## 总览表

| # | 方向 | 预估收益 | 实现难度 | 优先级 | 依赖 |
|---|------|----------|----------|--------|------|
| 1 | ATR 动态校准 | 中 | 低 | 中 | 无（get_atr_hist_mean_pct 已存在） |
| 2 | 时间衰减因子 | 中 | 中 | 中 | 信号重构（strategy-direction.md S1） |
| 3 | 极端行情熔断 | 高（防尾部） | 中 | 高 | 无（熔断框架已有 circuit_breaker） |
| 4 | 参数鲁棒性校验 | 高（防过拟合） | 中 | 高 | 双周期 PF 报表（strategy-direction.md S1） |
| 5 | 价格异动多 tick 去抖 | 低-中 | 低 | 低 | 见 P3-13 方案 |
| 6 | 出口 IP 漂移检测 | 运维 | 低 | 低 | 见 P3-14 方案 |

## 1. ATR 动态校准

- **目标**：止损/仓位使用的 ATR 随波动率 regime 自适应，避免在低波期
  止损过宽、高波期止损过窄。
- **原理**：sizing v2 已取 `get_atr_hist_mean_pct(coin,"4h",180)`
  （executor.py L797/L1899）并做 atr_spike 调整；本方向把"当前 ATR% /
  历史均值 ATR%"的比值显式做成校准因子，分 regime（低/正常/高波）映射
  止损宽度倍数，替代当前隐式钳制。
- **预估收益**：中。主要改善止损宽度与即时波动的匹配，减少被正常噪声扫损。
- **难度**：低——数据管线（ATR、历史均值）已具备，新增的是映射表 + 配置块。
- **优先级**：中（sizing v2 灰度放量后再做，避免两变量混样）。
- **依赖**：sizing-v2-rollout.md 阶段 4 完成；新增配置块走 R7 SPEC 登记。

## 2. 时间衰减因子

- **目标**：信号/评分随时间衰减——研究结论、新闻催化、whale 流等信号在
  产生后 N 分钟内权重最高，之后线性/指数衰减，杜绝"陈旧高分信号"入场。
- **原理**：当前 re-research 节流/冷却只控制"是否重新研究"，不控制
  "旧结论的权重"。FARTCOIN 事件中评分跳变绕节流后立即入场，信号新鲜度
  无折扣。引入 `weight *= exp(-age/halflife)`，halflife 按信号类型配置。
- **预估收益**：中。减少顶部/滞后入场，与入场信号重构同向。
- **难度**：中——需在评分聚合处统一注入时间戳与衰减，涉及多信号源。
- **优先级**：中；属于 strategy-direction.md S3 入场重构的子机制。
- **依赖**：信号源 PF 普查（S2）先确定哪些信号值得加衰减。

## 3. 极端行情熔断

- **目标**：市场级尾部风险（闪崩/拔网线/交易所异常）下全账户快速降险，
  不依赖单币信号。
- **原理**：现有 circuit_breaker 为单币/日内亏损维度；增加**市场维度**
  触发器：BTC/ETH 5-15min 跌幅超阈值、全市场多币同时触发 DSL stop
  （联动止损簇检测）、资金费率极端值、HL 保险基金/大额清算异常。
  触发后进入已有 global halt / auto_flatten 通道（auto_flatten_on_global_halt
  默认已开）。
- **预估收益**：高（尾部防护），但触发频率应极低——宁可不触发也不能误触发
  （误触发=正常行情被强平）。
- **难度**：中——触发逻辑新，但处置通道（halt/flatten）已存在。
- **优先级**：高（防灾难性亏损）。
- **依赖**：无新依赖；阈值需 shadow 模式先记录 would-trigger 灰度
  （复用 ta_late_entry 的 off/shadow/enforce 三段模式）。
- **开关**：`market_circuit.mode = off|shadow|enforce`，默认 **off**。

## 4. 参数鲁棒性校验

- **目标**：任何参数/信号改动上线前，证明其 PF 不是参数微调过拟合的产物。
- **原理**：对候选参数集做邻域扫描（±20% 网格）+ 分窗口（两个不重叠
  半年段）PF 验证；要求 PF 在邻域内平滑（无尖峰）、分窗口均 ≥1.0。
  与双周期 PF 门槛（strategy-direction.md §3）互补：双周期防"单周期侥幸"，
  鲁棒性防"参数点侥幸"。
- **预估收益**：高——直接抑制"回测好看、上线失效"的主要来源。
- **难度**：中（回测算力为主，逻辑直接）。
- **优先级**：高，应作为信号重构 S1 的一部分先建。
- **依赖**：双周期 PF 报表（strategy-direction.md S1）。

## 5. 价格异动多 tick 去抖（P3-13 可执行方案）

**现状核查（2026-09-03）**：并非"完全无去抖"，已有两层：

- `hl_client_io.ws_max_tick_jump_frac = 0.25`（config_store.py L571）：
  单 tick 价格跳变 >25% 判为脏读数丢弃（M-11 spike-suppression，WS 数据层）。
- `pct_move_spike` / `volume_spike` 触发器（perception.py L326-327，
  config_store.py L748-750，阈值 0.40/0.25）：K 线维度的异动触发。
- surge_postmortem.py 已有"多 bar 加速确认"思路（L19-27：单 bar 噪声 vs
  acceleration = Δscore ≥ min_jump）。

**缺口**：以上均不做"**连续 N 个 tick 同方向才采信**"的信号去抖——
一个方向的单次脉冲即可触发研究/评分跳变。

**评估结论**：不建议在信号路径直接加多 tick 硬确认。理由：
① 入场延迟与"信号重构"目标冲突（crypto 短窗口机会，N tick 确认会系统性
迟滞入场，且 FARTCOIN 类问题的根因是顶部追高而非噪声误触发）；
② 已在策略方向文档（strategy-direction.md）中以"多周期方向一致性 +
PF 门槛"从更高层解决同问题，多 tick 去抖是其低质量子集。

**如仍要实施（最小方案，默认关）**：
- 新增配置块 `entry_debounce`：`enabled`(默认 **false**)、
  `confirm_ticks`(默认 3)、`window_s`(默认 90)、`min_move_frac`(默认 0.001)。
  登记 CANONICAL_DEFAULTS + config_schema SPEC（走 R7 流程）。
- 落点：perception.py 扫描结果出口处维护 per-coin 方向计数
  （{coin: (direction, count, first_ts)}），连续 confirm_ticks 个扫描 tick
  同向且累计幅度 ≥ min_move_frac 才置"方向已确认"；未确认则该币本轮
  不产生新触发（研究/评分照旧记录，不执行）。
- 环境变量：`HERMES_ENTRY_DEBOUNCE=0` 默认关。
- 红线：只做"是否采信触发"的门控，**不改任何评分/止损/仓位逻辑**。
- 验证：单测（构造 3 同向 tick 序列 vs 抖动序列）+ SHADOW 下
  enabled=true 灰度对比触发数，确认不误伤趋势行情。

## 6. 出口 IP 漂移检测（P3-14 可执行方案）

**现状核查（2026-09-03）**：全仓无 egress_ip/public_ip/ip_change 相关
代码，确认未实施。家宽出口 IP 漂移会影响：交易所 API 限频/白名单、
LLM 网关（host.docker.internal:4000 走本地不受影响）、Webhook 回调。

**评估结论**：值得做，纯运维观测、零交易链路风险、工作量小。

**最小实施方案**：
- 新增独立守护脚本 `scripts/ip_drift_watch.py`（不 import 任何交易模块，
  仅用 stdlib urllib + logging）：
  - 每 `CHECK_INTERVAL_S`（默认 300，env `HERMES_IP_DRIFT_CHECK_S`）
    查询出口 IP（`https://api.ipify.org?format=json`，失败换
    `https://ifconfig.me/ip`，双源容错）。
  - 与上次值（持久化到 `/data/ip_drift_state.json`）比对：变化时
    `logger.warning("[ip-drift] egress IP changed: old=... new=...")`
    并写一条 events.jsonl（经 event_log.append，type=`ip_drift`）；
    查询失败连续 N 次记 warning 不告警（避免网络抖动误报）。
  - 总开关 env `HERMES_IP_DRIFT_WATCH=0`（默认关；部署确认后再置 1）。
- 部署：docker-compose hermes-trader 服务加 sidecar 进程或 cron
  （推荐 sidecar：`python3 scripts/ip_drift_watch.py`，与主进程同容器、
  共享 /data 卷）。
- 验证：手动改 state 文件模拟 IP 变化 → 确认 warning + 事件落盘；
  断网测试确认失败静默不刷日志。
- 不做：IP 变化时自动阻断交易（风控语义需另行评审，本期仅观测告警）。

## 验证方式

- 本文档为方向登记；各方向落地时按自身验收（回测报表 / shadow 灰度 /
  单元测试 + 全量 pytest）。
- 所有新增配置块必须登记 CANONICAL_DEFAULTS 并（如有 SPEC）纳入 R7
  漂移 sentinel（见 R7-config-pydantic.md R7-2）。
