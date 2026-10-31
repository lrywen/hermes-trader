# Hermes-Trader 可落地改造方案：从"无正 edge"到"可平稳对接实盘"

日期：2026-10-29（Asia/Shanghai）。
依据：本仓库两份已交叉验证的归因——
- 《不能正向收益·三层分析结论 2026-10-29》（44 币×17 交易日真实影子数据，日块 bootstrap）；
- 《盈利入场改造全面复核与上线评估 2026-10-28》（xs_reversal / maker v2 / 取证链 / LIVE 闸）。

当前姿态：`mode=SHADOW`、`allow_shorts=false`、零真实成交、容器 healthy、镜像 tag `fc508c8`，可随时改代码+重启。**本方案全部新逻辑先以 SHADOW/record-only 落地，任何一步都不直接动用真实资金。**

---

## 0. 已被证据锁定的事实（方案的前提，不再重复争论）

1. 指标层 math.py、结构层 volume_profile.py **正确**（独立 Wilder 参考实现 0 误差、无前视、NaN fail-closed）。**不改指标公式。**
2. 信号层 11 个触发器扣成本后 **0 个统计显著正 edge、2 个显著负**（rangeCompression、volumeSpike）；权重向量与收益 **Spearman≈0** → **调权重在数学上不可能产生正期望**。
3. composite=45 几乎不约束真实入场：69.1% 走 `fresh+slow_burn` OR 旁路（分数被短路不读取）、28.5% 走 30 分 daily-mover、仅 2.4% 真受 45 约束。三条路径前向超额**全为负**，看分数的 C 比不看分数的 A 更差。
4. 所有 edge 统计受**样本选择偏差**污染：实时仅 29% 信号能对齐缓存（系统性缺微盘）；回测用**当前** universe + **今天的**成交额选币（幸存者偏差 + 前视选币）。PIT 快照层 `hermes_trader/data/universe_snapshot.py` 已写好但**零调用（死代码）**。
5. 范式只做多：生产 17 处 LONG-only，`scan_long_only=true` + `allow_shorts=false`；做空在 10-15 后无任何近期证据。
6. 唯一有正向苗头的新臂 xs_reversal（超跌反弹）在生产场所 Hyperliquid 去相关后只有 **3 个独立恐慌事件**（闸门要求 ≥10），且 enforce 是 no-op、无实时退出/止损/仓位上限。

> 核心判断：**要修的不是"指标算错"，而是"证据样本 + 触发器构造 + 入场裁决结构"三件事。** 必须先修样本（否则一切统计不可信），再在干净样本上重建/筛选信号，最后才谈执行层与上线。顺序不可颠倒。

---

## 1. 改造核心目标与优先级

### 1.1 总目标
在**不动用真实资金**的前提下，用 1 个数据治理阶段 + 2 个信号阶段 + 1 个执行/上线阶段，得到一条"在生产场所、含微盘、去相关、扣全成本后仍有正 edge，且实时退出与风控闭环"的入场规则，并通过预注册闸门灰度到 LIVE。

### 1.2 优先级（强顺序，后项依赖前项数据）

| 优先级 | 工作流 | 目标 | 为什么是这个顺序 |
|---|---|---|---|
| **P0** | **WF-D：点在时间（PIT）数据治理** | 消除幸存者偏差/前视选币/微盘缺失，让所有后续 edge 统计可信 | 其余一切统计的前置；样本脏则结论全部不可用 |
| **P1** | **WF-E：触发器证伪与重建（研究臂）** | 在干净样本上判定旧触发器去留，产出 ≥1 个通过预注册闸门的新信号 | 必须在 P0 干净样本上做，否则重复旧错误 |
| **P2** | **WF-F：入场裁决结构改造（SHADOW 预注册）** | 让 composite 分数真正参与/可证伪地旁路，替代无信息 OR 短路 | 依赖 P1 已知"哪个信号有信息" |
| **P3** | **WF-G：执行层与风控闭环** | 为通过闸门的臂补齐实时退出/止损/仓位上限/对账 | 只对已证明 edge 的臂做，避免过早开发 |
| **P4** | **WF-H：灰度上线与 LIVE 闸门** | testnet→极小仓→放量，B-13 记录 + ADR 签字 | 最后一步，证据与工程双就绪才做 |

贯穿全程（P0 起即做）：**卫生项清理**（部署漂移、过期注释、chase_risk 去留）。

---

## 2. 分模块改造内容、技术路径与验收标准

### WF-D（P0）：点在时间数据治理 —— 最高优先

#### D1. 接线已存在但闲置的 universe 快照
- **现状**：[universe_snapshot.py](../hermes_trader/data/universe_snapshot.py) 提供 `record_universe_snapshot` / `snapshot_as_of`，全库零调用。
- **技术路径**：
  1. 在实时扫描取 universe 的唯一入口（`client.universe.get_universe` 的生产调用方，perception/scanner 主循环每轮）成功返回后，best-effort 调 `record_universe_snapshot(markets)`（函数本身已保证失败只 warning、不影响主循环）。
  2. 落盘到 `/data/universe-snapshots/date=YYYY-MM-DD.jsonl`（已实现 append-only、5min 去重、字段精简）。
  3. Docker 卷确认该目录在持久卷 `/data` 内（与 book-raw 同卷，已持久化）。
- **不改决策**：快照纯只增记录，不反馈到当轮扫描/打分。
- **验收标准**：
  - 部署后 24h，`/data/universe-snapshots/` 至少出现当天文件，行数 ≥ 扫描轮次的理论下界（考虑 5min 去重）；
  - 每行含全量扫描面（≥60 市场，含当前被 29% 漏掉的微盘），字段含 `coin,dayNtlVlm,funding,openInterest`；
  - 注入 `get_universe` 异常时主循环不受影响（单测：抛异常→快照返回 None、扫描继续）；
  - 新单测：append 原子性、5min 去重、`snapshot_as_of` 取"≤ts 最近一版"的 PIT 语义。

#### D2. 回测/研究改用 PIT 币池，废弃"当前 universe + 当前成交额"选币
- **现状**：[backtest.py:387-389](../scripts/backtest.py#L387) `get_universe()` 后按**当前** `dayNtlVlm` top-N 回测全历史。
- **技术路径**：
  1. 回测内核增加 universe 来源参数：`--universe pit`（默认）/ `current`（仅用于偏差对照）。
  2. PIT 模式下，每个历史时点 t 的币池 = `snapshot_as_of(t)`；快照历史不足的早期区间，回测结果**必须打标 `universe_bias=survivor_only` 并降权/排除出 promote 证据**，不得静默当全样本。
  3. 选币打分只用 t 时刻快照内的 funding/OI/成交额（PIT），不用当前值。
- **验收标准**：
  - 同一策略同区间产出 `current` vs `pit` 两版结果 A/B 报告，量化幸存者+前视偏差幅度（胜率/净收益/各触发器 edge 的差异），存档 `analysis/universe_bias_ab_<date>.json`；
  - PIT 模式下不存在"t 时刻买入当时已退市币"或"用未来成交额选币"（用构造的假快照单测验证 t1/t2 币池切换）；
  - 快照缺失时显式报错或打标，不允许静默回退到 current。

#### D3. 微盘 K 线与对齐率补齐
- **技术路径**：
  1. 把历史 K 线缓存（binance vision 归档 / HL info）抓取面从 44 主流扩到真实扫描面 60+，含微盘；记录每个币的数据首末可用时点（避免用"后来才有数据"的币污染早期）。
  2. 信号→前向收益对齐作业改为"先取 PIT 币池，再按各币可用窗口对齐"，输出**对齐率**作为一级指标；对齐率 <90% 的币组单独出结论，不与主流池混合。
- **验收标准**：
  - 信号事件可对齐缓存比例从 29% 提升到 **≥90%**；
  - 微盘组与主流组的 baseline（任意次开做多前向）分开报告；
  - 数据缺口（某币某日无 K 线）有显式计数，不静默丢弃。

> **WF-D 出口闸门（gate-D）**：连续 ≥10 个交易日，PIT 快照完整、对齐率 ≥90%、A/B 偏差报告产出并评审。**未过 gate-D 不得进入任何新信号的 promote 判定。**

---

### WF-E（P1）：触发器证伪与重建（全部以研究臂 / SHADOW 落地）

> 复用现成的"派发驱动事件型研究臂"模板（xs_reversal 已跑通）：纯函数 `evaluate_*` + shadow jsonl + cron reconcile + **永不轮转只增台账** + `shadow_control_group` 注册 + scheduler + Dockerfile 白名单 + config parity 登记。新臂物理上不 import executor，enforce 为 no-op。

#### E1. 旧触发器处置（先证伪，不先删）
- **技术路径**：在 P0 干净（含微盘、PIT 币池）样本上重跑 11 触发器单因子 edge（coin-day 去重、次开入场、1h/4h/24h、扣 19bp 压力 + 9.26bp 敏感、日块 bootstrap CI、事件去相关）。
  - rangeCompression / volumeSpike：已稳健为负 → 从**评分权重移除但保留 record-only**（不直接删，先做 E3 反向特征立项）。
  - 其余点估计为负/不显著：权重置 0、保留观察，不参与 composite。
  - trendFlip1h（唯一正点估计但权重 0）：进 E2 候选重点验证。
- **验收标准**：每个触发器一张四件套报告（留一剔月 LOMO + 日块 CI + 信号集中度（单币/单事件占比）+ Bonferroni 多重检验校正）；只有四项全过才允许重新进入评分。

#### E2. 新信号构造（≥1 个，方向优先补"非追多"与"反转"）
候选（均须先用数据证伪/证实，不预设结论）：
1. **超跌反弹（xs_reversal，继续）**：当前唯一有跨数据源正向苗头的臂；维持 SHADOW 累积 HL 去相关独立恐慌事件到 ≥10。这是已有投资，继续但不加速。
2. **反转/均值回归因子**：基于 E1 中 trendFlip1h 正点估计、以及旧 rangeCompression/volumeSpike 的**负 edge** 构造反向特征（如"缩量+带宽极窄后"不追突破而是反向/观望）。
3. **（研究）做空对称性**：用 10-02→10-15 已有的 689 笔 short ADMITTED 历史先做**离线审计**（报告 P2-7），评估空头在下跌/震荡环境的 edge；**仅研究，不开启 allow_shorts**。
- **技术路径**：每个候选=一个独立研究臂（独立 jsonl/台账/config 键），先历史 PIT 回测，过历史闸门后再接实时 SHADOW。
- **验收标准（新信号 promote 预注册闸门，沿 xs 口径）**：
  - 生产场所（Hyperliquid）去相关独立事件 **≥10**；
  - 事件级 **t≥2** 且日块/事件 **95% CI 下界 >0**（扣全成本，含 funding 见 WF-E/E4）；
  - LOMO 剔掉最差月后仍正；逐年/分 regime（牛/熊/震荡/高波）不全靠单一环境；
  - 信号集中度：单币贡献 <50%、单事件贡献不过半；
  - Bonferroni 校正后仍显著；DSR（deflated Sharpe）通过。

#### E4. 成本模型补 funding
- **现状**：[cost.py](../hermes_trader/backtest/cost.py) 无资金费，高周期/逆势持仓高估收益。
- **技术路径**：`CostModel` 增加 funding 项，按持仓时长 × 历史 funding rate（PIT，来自 D1 快照或 HL funding 历史）计提；5m 短持影响小，1h+ 与 xs 72h 持仓必须计入。保留无 funding 的对照开关。
- **验收标准**：funding 计提有单测（已知费率×已知时长=已知值）；xs 72h 与各新信号的 edge 在含 funding 后 CI 下界仍 >0 才算通过。

> **WF-E 出口闸门（gate-E）**：至少 1 个信号在 PIT+微盘+全成本样本上满足上述全部预注册指标。**没有信号达标 = 诚实地继续 SHADOW，不构造信号、不调权重。**

---

### WF-F（P2）：入场裁决结构改造（SHADOW 预注册，不直接硬堵）

- **问题**：[executor.py:6134](../hermes_trader/agents/executor.py#L6134) 的 `fresh_impulse AND (slow_count≥1 OR score≥min)` OR 结构使分数在 69% 入场中被短路；旧报告证明"硬堵可能更差"，所以**先做对照实验再决定**。
- **技术路径（三阶段，全部 shadow 计数）**：
  1. **可观测性**：对每笔 LONG ADMITTED 落盘"实际走了 A/B/C 哪条路径、分数是否被读取、若要求 score≥40/45 会不会被挡、该笔含微盘的真实前向超额"。沿用 40/45 双门 record-only 的现成模式（passes40/passes45）。
  2. **预注册实验**：shadow 跑"硬否决阈值候选（40/45）的反事实面板" ≥20 个去相关交易日，比较被挡组合 vs 放行组合的前向分布。
  3. **裁决**：仅当"被挡组合显著负、且挡掉后整体 CI 改善"在含微盘样本成立，才把硬否决切 enforce；否则保留旁路但关闭无信息的 slow_burn 直通，改用 WF-E 证明有信息的信号承载。
- **验收标准**：面板证明硬否决净改善（CI 下界>0 的差异）前，**不改变生产下单逻辑**；切换有配置开关 + parity 登记 + 灰度回滚。

---

### WF-G（P3）：执行层与风控闭环（只对 gate-E 通过的臂做）

以 xs_reversal 若通过为模板，新臂 promote 前必须补齐（当前缺失，见评估报告 §4.2）：
1. **实时退出**：实现 ADR-0010 冻结的"仅 72h 时间止损、不设紧价格止损"的**实时版**（当前只有离线 replay，无实时代码）。
2. **远宽组合级灾难止损**：币级 −15~−20% 远宽硬保护（防止黑天鹅裸奔），与 72h 时间退出并存。
3. **仓位上限**：单票 notional 上限、组合总 notional/权益比、分币权重与相关性限额（针对 edge 可能偏单一币，如 SOL 53%）。
4. **enforce 接线但先 testnet/paper**：研究臂接入 executor 时第一步只到 testnet/极小 notional。
5. **回放 parity**：用该臂自己的 gated 信号，实时退出/止损回放结果与离线 +edge 口径偏差在容忍带内。
- **验收标准**：新退出/止损/仓位单测全绿；实时回放与离线 parity；故意制造的极端行情（缺口、流动性枯竭）下灾难止损必触发；cloid 幂等、response_unknown 三态对账、部分平仓补单在新臂同样生效。

---

### WF-H（P4）：灰度上线与 LIVE 闸门

严格走已验证存在的机制（本次 B-13 演练已确认可靠）：
1. testnet / 主网极小 notional enforce，跑若干个完整持仓周期（xs 为多个 72h），每日 reconcile 成交/滑点/账本。
2. 实时 fill 与 72h outcome 只增台账逐日一致。
3. 翻 LIVE→急停回 SHADOW 演练：mode 改 OFF/SHADOW 秒级热生效（已验证，无需重启），撤 env / 删 gate 文件为二道闸。
4. B-13 接受记录：仅当 gate-E 统计通过（CI 下界严格>0）才允许 `build_acceptance_record` 铸造（CI≤0 代码层拒绝），记录绑定当前 config sha256、30 天 TTL。
5. 新 ADR：策略/风险/资金三方记录（参考 ADR-0010 流程，状态 Proposed→Accepted）。
- **放量门**：极小仓连续 N 个周期实际净 edge 与影子预测一致后，才按阶梯放大 notional；任一阶梯实际 edge 跌破 CI 下界即回退。

---

### 贯穿项：卫生清理（低风险，可立即做，独立 PR）
- momentumBurst 生产 2bar/4% 与 canonical 3bar/3% 的部署漂移：显式决策并纳入 config parity guard（二选一，锁定）。
- 修 uptrend_momentum 注释"weight 0"与生产 0.20 的矛盾（纯文档）。
- [executor.py:4609](../hermes_trader/agents/executor.py#L4609) 注释 "25 fixed gate keys" → 实际 28 个。
- chase_risk：按风险分桶量化经济价值，区分度（当前仅 25.6%）<成本即删除该观测层。
- EMA 文档措辞："标准 Wilder" → "首值种子 EMA，全库一致、尾部收敛、金标准锁定"。

---

## 3. 验证步骤与回滚保障

### 3.1 每阶段通用验证（Definition of Done）
1. **单测**：新增/修改逻辑必须有针对性单测（PIT 语义、研究臂纯函数、退出/止损、parity）；全库相关测试套件通过、ruff 干净。
2. **本地/容器双验**：纯函数本地 venv 验；接线类改动容器内打包代码 grep + 行为自检（沿用本次部署的验证方式）。
3. **部署后只读体检**：新增检查进 [postdeploy_shadow_checks.py](../scripts/postdeploy_shadow_checks.py) 与 [postdeploy_smoke.py](../scripts/postdeploy_smoke.py)，纳入 [deploy_prod.sh](../scripts/deploy_prod.sh) 3d 步骤；检测脚本须断言真实现行符号（避免再次出现旧字符串误报）。
4. **Dockerfile 白名单同步**：任何新增 scripts/ 必须同时加 `COPY` 与 [runtime_whitelist.py](../scripts/runtime_whitelist.py) 登记，否则 CI 红灯。

### 3.2 回滚保障（分层）
| 层 | 机制 | 回滚动作 | 恢复时间 |
|---|---|---|---|
| 研究臂 | 默认 off/shadow、独立 config 键、物理不下单 | config 键置 off | 秒级热加载 |
| 入场裁决(WF-F) | 先 shadow 反事实，enforce 有独立开关 | 开关回 shadow | 秒级 |
| 执行臂(WF-G) | testnet/极小仓、mode 热加载 | mode=OFF/SHADOW | 秒级，不重启 |
| LIVE | env `HERMES_ENABLE_LIVE` + B-13 记录 + per-coin enabled 三闸 | 撤 env 或删 gate 文件 | 下 tick / 重启即拦 |
| 发布 | deploy_prod.sh 打印回滚锚点 commit | `docker compose` 回滚到上一镜像 tag | 分钟级 |
| 数据 | universe 快照/台账 append-only，不改历史 | 数据层独立，回滚代码不丢证据 | — |

- 每个阶段一个**独立 commit + 独立 tag**（沿用 `shadow-evidence-v2-*` 形式），推两个 remote，保证任一阶段可独立回滚而不污染证据。
- 熔断 LKG 已持久化（commit `63f8d49`）：重启 + memory 故障在 TTL 内保持上次 BLOCK，不冷启动 fail-open。
- 配置任何新增键必须登记 config parity guard（本次 panic gate 已踩过"未登记→启动安全闸拦截→重启循环"，流程已明确）。

### 3.3 数据/实验纪律（防止自欺）
- 所有 edge 判定：去相关独立事件/日块 bootstrap，**禁止 iid t**；多重检验 Bonferroni + DSR；先预注册阈值再看结果。
- 时间相关验证**一律用历史/已落盘数据**，不靠等实时。
- 信号参数（top_pct、阈值、权重）一经预注册冻结，不得看到结果后回调（ADR-0010 已有先例）。

---

## 4. 对接实盘前置检查清单（上线 gate 汇总）

**证据门（必须全绿）**
- [ ] gate-D：PIT 快照 ≥10 交易日完整、对齐率 ≥90%、universe A/B 偏差报告已评审
- [ ] 至少 1 个臂满足 gate-E 全指标（HL 独立事件 ≥10、t≥2、CI 下界>0、LOMO、分 regime、集中度、Bonferroni、DSR）
- [ ] 含 funding 全成本后 edge 仍正；延长持有反事实不更差
- [ ] 极小仓实际 edge 与影子预测一致（多个完整持仓周期）

**工程门**
- [ ] 该臂实时退出 + 远宽组合灾难止损 + 仓位/相关性上限已实现并单测/parity
- [ ] 极端行情（缺口/流动性枯竭）灾难止损必触发
- [ ] 回放与离线 parity；cloid 幂等/三态对账/部分平仓补单覆盖新臂
- [ ] postdeploy 两脚本全绿（本次已清零历史 2 个 smoke 误报）；白名单/Dockerfile 同步
- [ ] 熔断 fail-open 收口项已评审（LKG 已做；裸仓备份止损挂失败是否改强平需决策）

**流程门**
- [ ] B-13 接受记录铸造成功且绑定当前 config sha256（CI 下界>0 才允许）
- [ ] 新 ADR 三方记录 Accepted；放量阶梯与回退阈值写明
- [ ] LIVE 切换/急停演练在切 LIVE 前再做一次（mode/env/gate 三闸）
- [ ] 值班文档与实际部署一致（DEPLOY.md 已修正为自建主机 docker-compose）

**当前即可确认的既有就绪项**：SHADOW 硬隔离（任何 SDK 调用前拦截）、LIVE 三层闸（B-13 演练 10/10）、28 个 risk gates、DSL+交易所端备份止损、每日 userFills 对账、熔断 LKG 持久化、只增台账、健康检查。

---

## 5. 预期收益与风险防控

### 5.1 预期收益（按阶段，务实口径）
- **WF-D（最确定的收益）**：不直接产生 alpha，但把"回测正/影子不赚"的最大测量水分挤掉。交付物是**可信的事实基线**——可能显示旧策略真实 edge 比现在更差（剔除幸存者），这本身是收益（避免上线负期望）。
- **WF-E**：以概率而非承诺看待。目标是 ≥1 个通过严格闸门的信号；**完全可能的诚实结果是"无信号达标"**，此时正确动作是继续 SHADOW/收集，而非放水。xs_reversal 是当前先验最高的候选。
- **WF-F**：让入场裁决从"无信息 OR 短路"变为"由证明有信息的信号承载"，预期改善的是**已入场组合的边际质量**，而非凭空造 edge。
- **WF-G/H**：把已证明的 edge 以可控尾部风险变现，量级由 gate-E 的 CI 下界（保守口径）决定，不用点估计承诺。

### 5.2 风险与防控
| 风险 | 防控 |
|---|---|
| 在脏样本上重建信号，重复旧错误 | 强制 gate-D 前置，未过不做 edge 判定 |
| 多重检验/过拟合挖出假 edge | Bonferroni + DSR + 事件去相关 + LOMO + 预注册冻结 |
| 微盘流动性/滑点远超假设 | 微盘单独分组、滑点按实测分布、流动性硬门（已有 liquidity/short_liquidity gate）、极小仓实盘校准 |
| 新臂尾部风险（xs 型深跌抄底遇崩盘） | 远宽组合灾难止损 + 仓位/相关性上限 + 单币集中度限 |
| 过早开发执行层浪费 + 引入风险 | WF-G 只在 gate-E 通过后开工 |
| 配置漂移/启动拦截/部署回滚 | parity 登记、白名单同步、独立 tag、分钟级回滚（本次已踩平流程） |
| 误翻 LIVE | 三闸 AND + B-13 30 天 TTL + 演练；CI≤0 代码层禁止铸记录 |
| 数据层 bug 污染只增证据 | 数据/代码解耦、append-only、快照 best-effort 不影响主循环 |

### 5.3 明确禁止（与归因报告一致）
- 现阶段按 iid/小样本调权重；把 45 降到 30；改 breakout confirm/rvol；延长持有期（反事实已显示 8h/12h 更差）；把 chase_risk 接入评分；在扩 universe 前用 gate 前向下因果结论；在 gate-D/E 未过前翻任何臂到 LIVE。

---

## 6. 立即可启动的第一步（建议）
**WF-D 接线**（零决策风险、是其余一切前置）：
1. 接线 `record_universe_snapshot` 到扫描主循环 + 单测 + parity/白名单 + 部署，启动 PIT 快照累积；
2. 回测内核加 `--universe pit/current` 与 A/B 偏差报告；
3. 扩微盘 K 线抓取面，做对齐率提升作业。

这三步不改变任何当前下单逻辑（本就 SHADOW），可独立提交、独立回滚，并从明天起为 WF-E 产出干净样本。
