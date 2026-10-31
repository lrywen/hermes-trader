# Hermes-Trader 可落地实盘改造方案（SHADOW → LIVE）· 合并正本

制定：2026-10-29（Asia/Shanghai）。
版本：合并正本 v1（以《可落地实盘改造方案 2026-10-29》为正本，并入 2026-10-29 对比复核的三处补丁 P1/P2/P3，差异见文末「合并变更记录」）。
前提：系统当前 SHADOW、零真实下单、可随时改配置重启（`scripts/deploy_prod.sh` 全量重建保留 /data）。

事实基线（已用真实数据验证，作为本方案输入）：
- 指标 math.py 6/6 标准 Wilder、位级正确（注：EMA 为首值种子而非教科书 SMA 种子，全库一致、尾部收敛、有金标准测试锁定），**不改**。
- 11 触发器扣成本后 0 个显著正 edge、2 个显著负（rangeCompression/volumeSpike）；权重–收益 Spearman≈0，调权重无意义。
- composite=45 通过率 0.074%，实盘仅 2.4% 入场受其约束；69.1% 走 fresh+slow_burn 旁路（分数被 OR 短路）、28.5% 走 daily-mover 30 门槛。
- 三路径 24h 超额（29% 可对齐子样本）全为负：A 旁路 −1.08%、C daily-mover −2.90%、B 分数腿 n=4 不足；分数带无单调。
- 样本面口径（已与 WF-C 报告统一）：eligible perp **234 个**（`min_market_volume_usd=5e6`），单周期扫描 cap **60**（≈覆盖 98.6% 成交额），sweep 用 7 个周期轮完全部 ≥$300k 市场；回测/影子常用 universe 仅 44 主流币。下文「真实扫描面」均指 234 eligible / cap 60 这套口径，非「700+ 全 market」。
- momentumBurst 生产 2bar/4% 近休眠，canonical 已改 3/3 但生产未同步（部署漂移）。
- LIVE 三闸（AND）：mode=LIVE ＋ `HERMES_ENABLE_LIVE=true` ＋ 有效 B-13 记录（`/data/live_acceptance_gate.json`，HMAC、绑定 config sha256、30 天 TTL，当前缺失）。B-13 隔离矩阵已演练 10/10 通过。
- maker 条件成交模型 v2 已用真实 Binance 1m/5m 实测证伪（1m −4.8bp、5m −15bp，队列穿透+逆选择后无正 edge），**不作为收益项**（见 M2-R5 与第五节修订）。

## 一、核心目标与优先级
| 级别 | 目标 | 成功标准（量化 Gate，全部在 SHADOW 验证） |
|---|---|---|
| G0 工程对齐 | 消除“代码默认≠生产生效”漂移；指标/影子证据可观测 | momentumBurst 等漂移项=0；parity guard 0 breach；三路证据端点持续有数据 |
| G1 样本基建 | 把统计样本扩到真实扫描面，并消除回测侧幸存者/前视选币 | 影子覆盖 ≥150 币（含微盘分层）、≥60 个交易日；关键路径每桶独立 coin-day ≥30；回测默认走 PIT 币池，早期缺快照窗口打 survivor 降权 |
| G2 重建入场 edge | 用证据选出/构造净成本后正 edge 的入场规则 | 候选规则在日块 bootstrap 95%CI 下界 > 0 且通过 Promotion Gate 六条；高相关信号另过簇级去相关闸门；SHADOW 纸面组合 60 交易日净期望 > 0 |
| G3 让分数真正约束入场 | 旁路与主门统一到“有 edge 的选择器” | 入场选择器在全部成交路径 100% 生效（无短路绕过），SHADOW 对照（旧旁路 vs 新门）净期望改善 CI 下界 > 0 |
| G4 组合级风控 | 上限/组合止损/相关性约束可执行 | 单笔/总敞口/日亏/连亏/同板块集中度全部 fail-closed 且经影子对账；**M4 全程 shadow，M3-c 之后才 enforce** |
| G5 LIVE 切换 | 受控、可秒级回滚 | B-13 验收签字齐备；小额灰度 KPI 达标；回滚演练通过 |

原则：**先用 G0/G1 把证据做厚，再用既有的 shadow-arm→nightly reconcile→grade→promote 生命周期做 G2/G3，最后 G4/G5。任何 Gate 不过，不进入下一步、不碰 LIVE。**
样本量双口径（交易级 + 簇级）：coin-day/交易日是交易级样本；对全市场高相关的反转/恐慌类信号（xs 型），交易级 n 会被横截面相关严重高估（Hyperliquid 449 笔 naive −1.8bp、去相关后 208 天仅 3 个独立恐慌事件的教训），此类信号必须**另报 gap 去相关后的独立事件数与事件级 t**。

## 二、分模块改造、技术路径、验收标准

### 模块 M0｜配置与部署一致性（对应 G0，1–2 天，零交易行为风险）
改造内容：
1. 建一份《生效配置清单》：以 `trigger_thresholds_params()/trigger_weights_params()` 运行时返回为准（不是 `get_config()` canonical），逐项登记 canonical vs live。
2. 对 momentumBurst：决定 3/3 还是 2/4。在证据未足前**保持 2/4 不动**，但在 `config_parity_guard._REVIEWED_SAFE_DIVERGENCES` 显式登记 `(3,2)/(3.0,4.0)` 为“有意分叉”（值绑定，今后改值启动即重新变红），消除隐性漂移；修 `uptrend_momentum` 注释（weight 0）与生产 0.2 的矛盾（纯文档）。
3. 部署后自检：`scripts/deploy_prod.sh --preflight-only` + `postdeploy_shadow_checks.py` 必须 0 breach 才放行重建。
技术路径：复用现有 parity guard（canonical 默认 + live 差异登记）；新增一个 `scripts/audit_effective_config.py`，CI 与 postdeploy 双跑，输出运行时阈值/权重快照与 git 中 canonical 的 diff，任何未登记差异 fail。
验收：重启后 `trigger_thresholds_params()` 快照与登记一致；parity guard 输出 uncovered=0；文档矛盾清零。回滚：git revert + 重部署（/data 不动）。

### 模块 M1｜样本与 universe 基建（对应 G1，1 周，纯数据/只读，最高优先）
问题：所有 edge/路径结论受 44 币、17 交易日、29% 对齐率限制；且回测侧用「当前 universe + 今天成交额」选币，存在幸存者偏差 + 前视选币。

本模块分两条腿，**实时全候选埋点**与**历史回测 PIT** 互补，缺一不可：

1A. 实时全候选决策快照（解决“被门拦/未入场候选不落盘”的 29% 对齐偏差）：
1. 扩影子记录 universe：在真实扫描面（234 eligible / cap 60，sweep 轮全部 ≥$300k 市场）上，对**所有被评估候选**（含未入场/被门拦截）落盘决策快照，字段至少：ts、coin、成交量分层（mega/mid/micro）、side、composite、fired_names、fresh_src、slow_burn、各 gate 判定（would-admit/block + rule）、confidence、价格。落盘到新 `/data/decision_shadow.jsonl`（append-only，按日 rotate，并配套永不轮转的成熟 outcome 只增台账，沿用 xs ledger 模式）。
2. PIT 前向收益：复用 `hermes_trader.data.historical_candles.fetch_candle_range` 与既有 `reconcile_*_shadow.py` 模式，新增 `scripts/reconcile_decision_shadow.py`，对每条快照在无未来函数前提下补 1h/4h/24h/72h 净收益（同时记 9.26bp 与 19bp 两口径，**1h+ 与 72h 持仓必须含 funding**）与等权市场基准超额。
3. 分层：按 24h 成交额分 3 层（如 >100M / 10–100M / 5–10M USD），所有 edge 数字分层报告，避免主流币结论外推微盘。
技术路径：决策快照在 executor runner-gate 求值处统一埋点（一处写全部 rule，fail-open 记录但不影响交易）；夜间 `cron_reconcile.sh`（00:15 UTC）加挂该 reconciler；结果进 `shadow_grade.py` 数据源。

1B. 历史回测 PIT 币池（解决幸存者偏差 + 前视选币；决策埋点只能从今天起记录、救不回历史）：
1. 复活当前**零调用的死代码** `hermes_trader/data/universe_snapshot.py`（`record_universe_snapshot` / `snapshot_as_of` 已实现，append-only、5min 去重、best-effort）：
   - 在实时扫描取 universe 的唯一入口成功返回后 best-effort 调 `record_universe_snapshot(markets)`，落盘 `/data/universe-snapshots/date=YYYY-MM-DD.jsonl`（持久卷内，纯只增、不反馈当轮扫描/打分）；
   - 回测内核 [scripts/backtest.py](../scripts/backtest.py) 增加 `--universe pit|current`，**默认 pit**：历史时点 t 的币池 = `snapshot_as_of(t)`，选币只用 t 时刻快照内的 funding/OI/成交额。
2. 快照历史不足的早期窗口：回测结果必须打标 `universe_bias=survivor_only` 并降权/排除出 promote 证据，**不得静默当全样本**；快照缺失时显式报错或打标，不允许静默回退 current。
3. 微盘 K 线补齐：历史 K 线抓取面从 44 主流扩到真实扫描面 60+（含微盘），记录各币数据首末可用时点；信号→前向收益对齐改为「先取 PIT 币池，再按各币可用窗口对齐」，对齐率作为一级指标，<90% 的币组单独出结论。

验收：
- 实时：连续运行后每日 `/data/decision_shadow.jsonl` 行数与 scanner 评估数一致（抽样对账误差<1%）；覆盖币数 ≥150、三层均有样本；每条 72h 成熟后净收益非空率 >95%。
- 回测：同一策略同区间产出 `current` vs `pit` 两版 A/B 报告（`analysis/universe_bias_ab_<date>.json`），量化幸存者+前视偏差幅度；构造假快照单测验证 t1/t2 币池切换的 PIT 语义；`get_universe` 抛异常时主循环不受影响。
- gate-D：连续 ≥10 个交易日 PIT 快照完整、对齐率 ≥90%、A/B 偏差报告产出并评审，**未过不得进入任何新信号 promote 判定**。

回滚：埋点用 config flag `decision_shadow.enabled=false` 可一键关，纯旁路写盘；PIT 回测用 `--universe current` 退回旧口径；快照记录失败只 warning 不影响主循环。

### 模块 M2｜入场 edge 重建（对应 G2，2–4 周，核心）
原则：**不调旧权重**（Spearman≈0 证明无效）。用 M1 样本做“先证伪、再构造”。
2A. 候选规则来源（全部作为并列 SHADOW 臂，影子只记录不交易）：
- R1 反向利用已验证为负的信号：rangeCompression fired、volumeSpike fired 作为**回避/反向**特征（先只做回避：命中则禁多），独立臂验证。
- R2 位置/均值回归臂：用现成 `pullback_long`（已在 shadow_mode：min_composite 30、RSI≤65、extension≤2 ATR、macro 多头、slow≥2）作为种子，补齐前向对账（`reconcile_pullback_shadow.py` 已存在），重点看微盘层。
- R3 chase/位置门：把已计算但未消费的 volume-profile `chase_risk`/`distance_from_poc_atr` 作为**回避特征**（高位不追），先量化分桶 edge（区分度 < 成本即弃用），不直接进评分。
- R4 趋势过滤：weak_trend_chase（4h ADX<25 拦纯 burst 延续，生产已配 25）转成正式臂观察其拦单的前向收益差。
- R5 **xs_reversal 超跌反弹（当前先验最高的在研臂，继续 SHADOW，不加速）**：维持 shadow 累积 Hyperliquid 去相关独立恐慌事件到 ≥10。
- ~~maker v2 作为收益并行臂~~ → **修订（P3）**：maker v2 已被真实 Binance 1m/5m 证伪（成交率 8.98%、1m −4.76bp、5m −15.07bp，逆选择未被 maker edge 覆盖），**从“预期收益项”移除**；仅保留实时 v1 账本与 v2 影子字段作观测，不纳入任何 promote 收益论证，未来仅当某新臂确实需要 maker 执行时再单独重估。
- R6（研究，不开启 allow_shorts）做空对称性：先用 10-02→10-15 已有的 short ADMITTED 历史做离线前向审计，评估空头在下跌/震荡环境的 edge。
2B. 统一评估器：扩展 `scripts/maker_v2_param_grid.py` 的 day-block bootstrap 框架为通用 `scripts/arm_edge_eval.py`，对每臂输出：n、独立 coin-day/天数、1h/4h/24h/72h 净收益均值、日块 95%CI、WR、成本两口径（9.26/19bp，长持含 funding）、分层结果、Bonferroni 校正后 p、留一月剔除法最差月、前 N 笔集中度（Gini/单笔贡献）；**高相关臂另输出 gap 去相关独立事件数与事件级 t**。
2C. 选择规则（Promotion Gate，全部满足才升级）：
1. 日块 CI 下界 > 0（净成本，主口径 19bp）；
2. Bonferroni 后 p<0.05（**参数搜索/试臂次数计入多重检验**）；
3. 留一剔月：最差月仍不显著为负；
4. 前 5 笔贡献占比 <50%（非单笔拉动）；
5. 独立 coin-day ≥60 且跨 ≥40 交易日、三层中至少两层同向；
6. 对照组（同时期旧逻辑）净期望之差 CI 下界 > 0；
7. **簇级闸门（高相关/反转类信号强制）**：gap3d 去相关后独立事件 ≥10、事件级 t≥2、事件级 95%CI 下界 >0；逐年/分 regime（牛/熊/震荡/高波）不全靠单一环境；
8. DSR（deflated Sharpe）通过。
技术路径：沿用 shadow-arm 生命周期——臂配置 `shadow_mode:true` 只写 would-admit；`cron_shadow_grade.sh`（00:45 UTC）夜间评级；过 Gate 才进入 M3 的“影子强制（记录若真拦会怎样）”，再进灰度。任何一臂不过即弃用或重设计，**禁止为过 Gate 反复调参（参数搜索次数登记并计入多重检验）**。
验收（G2）：至少 1 个入场规则（含其回避特征组合）同时满足 2C 全部各条；其纸面组合在 ≥60 交易日净期望为正且最大回撤可接受。若 8 周内无规则达标，结论是“该市场/该信号族无可提取 edge”，则转向降低交易频率/暂停，而非放松 Gate（maker 已证伪，不再作为“仅 maker”降级出口的默认假设，需另行证明）。

### 模块 M3｜统一入场选择器、取消无 edge 旁路（对应 G3，1–2 周）
现状根因：`structured_runner = fresh_impulse and (slow_count>=1 or score>=min_score)`，slow_burn 是时间计数非质量信号，69% 入场分数被短路；C 路用更弱的 30 门槛。
改造内容（在 M2 选出的规则达标后才做）：
1. 用 M2 胜出的“选择器函数 S(features)→{admit,block,reason}”替换 OR 旁路：所有 LONG 入场（含 daily-mover、force、AI 高置信）一律先过 S，**不允许 slow_burn/置信度单独替代质量门**；置信度只允许在 S 通过后做尺寸/冷却调节，不能开门。
2. 三阶段灰度（全部 SHADOW 先行）：
  - 阶段 a（shadow-enforce）：S 只记录 would-block（复用 `risk_tuning_shadow` 落盘 + reconcile），不动入场，跑 ≥20 交易日，验证被拦单真实前向为负、被放单为正。
  - 阶段 b（paper-enforce）：在影子账本里按 S 实际阻止模拟开仓，与“旧旁路对照组”并行（`shadow_control_group.py` 已具备对照能力），比较两组净期望。
  - 阶段 c（live-ready flag）：仅当对照组差 CI 下界>0，才把 S 设为真实硬门（LIVE 后先用最小名义）。
3. 保留 fail-closed：S 数据缺失/超时 → 拒绝入场（不得默认放行）。
技术路径：选择器落在新模块 `hermes_trader/agents/entry_selector.py`（纯函数、可单测），executor 单一调用点；daily-mover 路径的 30 门槛与主门统一；force_execute 各 flag 仍默认 false 且不得绕过 S（在 `_evaluate_force_override` 末尾加 S 否决）。
验收：单测覆盖短路消除（slow≥1 但 S 不过→block）；阶段 a/b 中 S 100% 路径生效、无未登记 bypass；对照组改善 CI 下界>0。回滚：一个 config flag `entry_selector.enforce=false` 回到现逻辑（影子记录保留）。

### 模块 M4｜组合级风险闸（对应 G4，1 周，可与 M2 并行“设计/影子”，不得提前 enforce）
现状缺口（LIVE 前置，ADR 已多次指出）：缺组合级宽止损与名义上限的完整证据。
**约束（P3 修订）：M4 全程 shadow_mode 落盘与对账，阈值可以设计、可以写影子逻辑，但在 M3-c 之前不得切 enforce**——在 M2 尚未证明 edge 的臂上提前收紧组合闸，会改变未来选择器的样本。
改造内容（默认先 shadow_mode，夜间对账，M3-c 后再 enforce）：
1. 名义闸：单笔名义上限、总敞口上限、按成交额分层的差异化上限（微盘更小）；`per_trade_notional_cap_gate`/`max_concurrent_positions_gate` 已存在，校准阈值。
2. 组合止损：账户日内回撤达阈→停止新开（daily_loss gate 已存在，校准并验证触发路径）；新增同板块/高相关持仓集中度上限。对未来 xs 型臂另需币级 −15~−20% 远宽灾难止损（ADR-0010：仅 72h 时间止损、不设紧价格止损）。
3. 连亏/同币冷却：`per_coin_cooldown`（已具备 shadow_mode）转正式前，用 M1 数据验证其拦单前向为负。
4. 空头：ADR-0008 任何开空前，先对 10-15 后被弃 short 候选补前向审计；默认继续 allow_shorts=false。
技术路径：全部走 risk_gates 既有 gate 模式 + shadow/enforce 两态 + reconcile；阈值进 config 与 parity guard。
验收：每道闸有“触发演练”单测/桌面演练（构造触发条件确认真的拦截）；shadow 期误拦/漏拦统计达标；所有闸 fail-closed。回滚：各闸独立 flag，可单独退回 shadow/off。

### 模块 M5｜LIVE 切换与灰度（对应 G5）
见第四节清单与第五节灰度/回滚。

## 三、验证步骤与回滚保障（贯穿全程）
分层验证（每层独立、缺一不升级）：
1. 单元层：指标位级回归（已有 Wilder 0 误差测试）；entry_selector/risk_gates/universe_snapshot 全分支单测（含 NaN、缺数据、短路消除、PIT t1/t2 币池切换）。`PYTHONPATH=. python3 -m pytest` 全绿、ruff 干净。
2. 离线层：`arm_edge_eval.py` day-block bootstrap（B≥5000、多种子）+ Promotion Gate 报告；PIT 无未来函数审计（closed_bars_as_of，与生产同源）。
3. 影子层：shadow_mode 只记录 → paper-enforce 对照组，真实时间推进 ≥40–60 交易日，含至少一个高波动周与一个低波周；高相关臂另满足簇级闸门。
4. 预发层：每次改造 `deploy_prod.sh --preflight-only` 通过 → 全量重建（保留 /data）→ postdeploy checks 0 breach → 容器 healthy → 端点抽样核对。新增 scripts/ 必须同步 Dockerfile `COPY` 与 `runtime_whitelist.py` 登记（否则 CI 红灯）。
5. 灰度层：LIVE 后小额（见第五节）。
回滚保障：
- 配置：改前备份 `/data/.agent-config.json.bak-<ts>`（沿用现有做法）；所有新行为默认 off/shadow、单 flag 可关；mode 回 SHADOW 即停止真实下单（配置按 mtime/size stat 缓存，秒级热生效、无需重启）。
- 代码：每个模块独立 commit/PR/tag（沿用 `shadow-evidence-v2-*` 形式）推两个 remote，可单独 git revert + 重部署；/data append-only 账本/快照不随回滚丢失。
- 应急：撤 `HERMES_ENABLE_LIVE` 或删除 B-13 记录立即全局阻断真实下单；保留 emergency flatten/kill（operator 终端），并验证其在 LIVE 仍可用；reduce-only 平仓走独立路径，急停不影响存量仓位止损/平仓。
- 数据：新增 jsonl 一律 append-only + rotate + ledger；reconciler 只读，可重跑幂等。
- 顺序回滚预案：M3 选择器 → 退回 shadow/enforce=false；M4 各闸 → 单闸 off；M2 臂 → 停止采纳其信号；M1 埋点/PIT → enabled=false 或 `--universe current`。任何回滚不影响既有 SHADOW 记账。
- 熔断冷启动：last-known-good 裁决已持久化到 `/data/.breaker-lkg.json`（commit 63f8d49），重启 + memory 故障在 TTL(1h) 内保持上次 BLOCK，不冷启动 fail-open；切 LIVE 前再做一次“重启+memory 不可读”实测。

## 四、对接实盘前置检查清单（LIVE Gate，逐项签字，缺一不上）
A. 证据与策略
- [ ] gate-D：PIT 快照 ≥10 交易日完整、对齐率 ≥90%、universe current-vs-pit A/B 偏差报告已评审。
- [ ] 至少一个入场选择器通过 M2 Promotion Gate 全部各条（CI 下界>0、Bonferroni、留一剔月、集中度、样本量/分层、对照改善；高相关臂另过簇级事件≥10/t≥2/CI>0、DSR）。
- [ ] M3 选择器在全部成交路径 100% 生效，无 slow_burn/置信度/daily-mover/force 旁路；对照改善 CI 下界>0。
- [ ] SHADOW 纸面组合 ≥60 交易日净期望>0，含压力周；成本用 19bp 压力口径且含 funding 仍为正。
- [ ] 极小仓实际 edge 与影子预测一致（多个完整持仓周期）。
B. 风控
- [ ] 单笔/总敞口/日亏/连亏/同币冷却/板块集中度/远宽灾难止损全部 enforce（M3-c 后）且有触发演练记录；全部 fail-closed。
- [ ] allow_shorts 决策有 10-15 后空头候选审计支撑（默认保持 false）。
- [ ] emergency close/kill、自动 de-risk 在 LIVE 演练通过；裸仓备份止损挂失败是否改强平已显式决策。
C. 工程与一致性
- [ ] parity guard 0 uncovered divergence；M0 漂移清零；effective-config 审计在 CI/postdeploy 通过。
- [ ] 决策快照/对账/证据端点（decision_shadow、xs panic、xs 净收益、runner 40/45、maker 观测）持续有数据并看板可见。
- [ ] 全套 pytest + vue-tsc + portal vitest 通过；端到端 BFF 鉴权（无 token 401）回归；postdeploy 两脚本全绿、白名单/Dockerfile 同步。
D. 资金与开关
- [ ] B-13 记录由授权人签署（含策略版本、证据快照哈希、config sha256、风控参数、限额、责任人、30 天 TTL；CI 下界≤0 时 `build_acceptance_record` 代码层拒绝铸造）。
- [ ] mode=LIVE 与 HERMES_ENABLE_LIVE=true 的双人确认；**建议 LIVE 前先 unset HERMES_ENABLE_LIVE，切换窗口再临时置位**，避免 env 长期武装的 footgun（B-12）。
- [ ] 起始资金/单笔名义取最小（如沿用 $20 起点、单笔最小名义），杠杆设保守档；明确逐日亏损与总回撤熔断阈值。
E. 运维
- [ ] 回滚演练（撤 env/删 B-13 记录 / 回 SHADOW / 单 flag 关闭）实测有效且 RTO 明确（目标 <2 分钟停止真实下单）；LIVE 切换/急停演练在切 LIVE 前再做一次。
- [ ] 夜间 reconcile/grade 正常运行并有人工评审节奏；Feishu 告警（开仓/闸触发/日亏/数据健康）验证收到；值班文档与实际部署一致（DEPLOY.md 已修正为自建主机 docker-compose）。

## 五、预期收益与风险防控
预期收益（以“可平稳 LIVE 且期望不为负”为首要目标，不承诺高收益）：
- 把入场从“无 edge 动量追高 + 时间计数旁路”替换为“通过统计 Gate 的选择器”，目标是把纸面组合 24h/72h 净期望从当前各路径负值提升到 CI 下界>0；在 19bp 压力成本（含 funding）下成立。
- ~~执行侧 maker v2 降低往返成本抬升 edge~~ → **修订（P3）**：maker v2 已证伪，**不计入预期收益**；执行成本优化须等新臂需要时用真实成交重新证明。位置/追高回避（R3）减少顶部成交的收益保留，但需其独立通过 Gate。
- 可观测/可审计/可回滚的工程闭环，使 LIVE 风险可控、问题可在分钟级切断。
风险与防控：
| 风险 | 防控 |
|---|---|
| 扩 universe 后微盘 edge 与主流币不同、滑点更大 | 强制分层评估与分层名义上限；微盘单独 Gate，未达标层不交易；成本用更保守滑点 + 实测分布 |
| 回测幸存者/前视选币让历史 edge 虚高 | M1B PIT 币池 + 早期窗口 survivor 降权 + current/pit A/B 量化偏差 |
| 多重检验/过拟合（试很多臂总有一个显著） | Bonferroni + **参数搜索/试臂次数登记** + 留一剔月 + DSR + 前向冻结期（fit 窗与评估窗分离） |
| 高相关信号虚增有效样本 | 簇级 gap 去相关独立事件 + 事件级 t（xs 型强制），不只看 coin-day |
| 影子→实盘成交差异（队列/冲击） | LIVE 灰度用最小名义实测 fill/滑点；以真实成交滚动校准；不达标即回 SHADOW |
| 选择器在结构变化市场失效 | 滚动窗口重评 + 月度再校验 + 绩效自动降级（edge 衰减触发降频/停开） |
| 新臂尾部风险（xs 型深跌抄底遇崩盘） | 远宽组合灾难止损 + 仓位/相关性上限 + 单币集中度限 |
| 旁路改造误拦正常机会/影响稳定性 | shadow→paper 对照 ≥20–60 交易日、单 flag 回滚、fail-closed 但开关可控 |
| 过早收紧组合闸污染选择器样本 | M4 全程 shadow，M3-c 后才 enforce |
| 配置漂移复发/未登记键致启动拦截 | 新键必须登记 parity guard；effective-config 审计进 CI/postdeploy，差异未登记即阻断部署 |
| 资金风险/误翻 LIVE | 三闸 AND + B-13 双签 + 切换窗口临时置 env + 最小名义灰度、日亏/总回撤硬熔断、秒级撤签停单 |

明确禁止（现阶段）：按 iid/小样本调权重；把 45 降到 30；改 breakout confirm/rvol；延长持有期（反事实已显示 8h/12h 更差）；把 chase_risk 直接接评分；在 gate-D / Promotion Gate 未过前翻任何臂到 LIVE；把已证伪的 maker v2 计入收益论证。

## 六、排期建议（SHADOW 期，可并行）
- 第 1 周：M0 工程对齐 + M1A 决策埋点/对账上线 + M1B 接 PIT 快照与回测 `--universe`（纯只读，立即开始）。
- 第 2–5 周：M1 持续累积样本（实时目标 ≥150 币、滚动到 ≥40–60 交易日；历史侧用 PIT + 微盘补抓做 edge 初筛，尽量以历史数据缩短等待）；M4 风控闸仅做影子设计并行；M2 各臂开始登记与 arm_edge_eval。
- 第 6–8 周：M2 Promotion Gate 评审；达标臂进入 M3 阶段 a/b 对照；不达标则按“无可提取 edge”分支处理（降频/暂停），不放松标准。
- 第 9 周起：仅在 Gate 全过后，M3 阶段 c + M4 enforce + M5 小额 LIVE 灰度，按第四节清单逐项签字。
硬约束：任一 Gate 不达标则不进入下一阶段；**时间服从证据**，60 交易日是最小确认窗而非排期承诺，能靠历史/PIT 不靠等实时，但簇级独立事件无法工程加速，不为赶时间窗放松统计标准。

附：本方案只改 SHADOW/影子与只读基建，未对当前系统做任何代码/配置变更；落地时各模块独立 PR、独立回滚。

---

## 合并变更记录（相对原《可落地实盘改造方案 2026-10-29》）
- **P1 回测侧 PIT**：M1 拆为 1A 实时决策埋点（原文）+ 1B 历史回测 PIT 币池（新增，复活零调用的 `universe_snapshot.py`、回测 `--universe pit|current`、早期窗口 survivor 降权、微盘 K 线补齐与对齐率）；G1/G2、第四节、风险表、排期同步补 gate-D。
- **P2 簇级样本**：Promotion Gate 由六条扩为含第 7 条簇级去相关（独立事件 ≥10、事件级 t≥2、CI>0）与第 8 条 DSR；第一节加“样本量双口径”；arm_edge_eval、第四节、风险表同步。
- **P3 三项修订**：① R5 maker v2 由“收益并行臂”改写为“已证伪、不计收益”（事实基线、M2、第五节、禁止项同步）；② M4 明确全程 shadow、M3-c 后才 enforce（防污染选择器样本）；③ market 口径由“700+”统一为“234 eligible / cap 60 / sweep 7 周期”，并补 LIVE 三闸为 AND、B-13 已演练、EMA 种子措辞、funding 计入长持成本、熔断 LKG 等已验证事实。
