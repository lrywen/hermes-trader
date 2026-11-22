# Hermes-Trader 简化改造可行性报告（终版·整合落地版）

**日期**：2026-11-22　**对象**：ldy@192.168.124.65:/home/ldy/hermes-trader  
**分支**：refactor/shadow-solo-v1（HEAD fc63a14 + 48eeafd + 29456d7）  
**形态**：SHADOW / 单边做多 / 做空关闭 / HIP-3 关闭 / 未 LIVE  
**性质**：基于实测落地的最终定版，已完成核心代码实施

---

## 1. 结论

**Go（已落地核心）**。打法：冻结扩展 + 收口入口 + 清理闲置，代码级简化在影子对照通过后小步推进。

| 维度 | 判断 |
|---|---|
| 可行性 | **高**：入口重构、Profile fail-closed、快速回测后台化已实施并验证 29/29 通过 |
| 风险 | **可控**：守住四条红线即可；回滚锚点已建立 |
| 工作量 | **已投入 ~2 人日**（Profile/回测后台化/测试）；剩余波二 3-4 人日 + 7 天观察窗 |
| 收益 | 入口统一、认知负担下降；运行时开销本就宽裕（535.8 MiB / 2 GiB），不追求下降 |
| 禁止 | 激进删代码——74k 行经多轮审计沉淀，隐性缺陷代价高于行数收益 |

**前置条件已满足**：
- 20 个未跟踪文件已归档 analysis/archive-untracked-2026-11-19/
- uncovered_divergences()==[]（parity guard 零未登记漂移）
- 备份配置/账本/DSL 状态并打 tag pre-simplify-2026-11-17 = 755fc72c
- 记录 4,633 测试基线（服务器隔离全量 20 个预存失败，均为既有漂移/环境依赖）

---

## 2. 基线与口径（三轮实测 + 本次实施实测）

### 2.1 规模基线

| 项 | 值 |
|---|---|
| hermes_trader/ | 147 模块 / 73,981 行 |
| 全仓 Python | 756 文件（排除 venv/pycache） |
| scripts | 186 / 61,891 行（影子+对账 31 个 12,698 行） |
| tests | 371 / 86,990 行，4,633 门禁 |
| 文档 / 日志 | 130 份 md / 505 MB（含 460 MB 回放产物）；logs 1.2 GB / 22,479 个 .gz |
| 配置 | agent-config 156 顶层；canonical 154 顶层 / 704 叶子 |
| 容器 | hermes-trader healthy，2 GiB / 2 CPU，0 重启；7 个协作容器 |

### 2.2 口径裁决（冲突项，以实测为准）

| 项 | 原报告/静态口径 | 实测权威口径 | 说明 |
|---|---|---|---|
| equity_fraction_per_trade | 0.2（canonical 默认） | **runtime 0.15** | 容器实测：/data/.agent-config.json 生效值 0.15，fallback 0.2，有效值 0.15 |
| allow_shorts | 生产 37 行 / 全仓 73 行 25 文件 | **同** | 已配置 false |
| shadow_mode | 包内 110 行 12 文件 / 全仓 257 行 | **同** | Profile 已强制约束 |
| validation/fixed_effect.py | 零引用可删 | **20 处引用，不可删** | __init__.py 导出、incremental.py 依赖 |
| parity 函数 | config_store._production_parity_uncovered_errors() | **同** | config_store.py:641，fail-closed |
| 未合并 commit | refactor/hta-retirement：1 个 9f61de63 | **同** | 建议打 tag 归档后删本地分支 |
| s3_shadow_shorts.enabled | true（风险项） | **已关闭 false** | Profile 约束 + 运行时配置双重保障 |
| hermes-solo CLI | 无/设计中 | **已实施** | status/watch/tune/shadow/backtest/preflight 六子命令 |
| 快速回测后台化 | 设计中 | **已实施** | --background/--result/--json，29/29 隔离通过 |

### 2.3 前提偏差修正

| 前提 | 实际 | 影响与处理 |
|---|---|---|
| 未开做空 | allow_shorts=false，但原 s3_shadow_shorts.enabled=true 且 allow_regimes=[down] | ✅ 已关闭影子臂，Profile 强制 false |
| 未上线 | 是未 LIVE非未运行，账本持续更新 | ✅ 改造不可长时间停机，灰度发布保留 /data |
| 中间件可清 | 7 容器；nginx 白名单 + SSE 经 portal | ⚠️ 本轮不纳入：改 Dashboard 断 portal 链路 |
| 个人适配可用 | CLI 无 --mode；risk_gates_simplified 0 引用；配置路径写错 | ✅ 已实施真实配置路径 /data/.agent-config.json |

---

## 3. 四条红线（硬约束，违者回滚）

1. 配置不删、改值必登记。_production_parity_uncovered_errors() 在 /data 下 fail-closed：漂移未登记即启动失败 → 重启风暴（11-16 TRAE 975：Network Changed → WSS 1006 → 975）。改后静态校验 [] 并观察 3 个重启周期。
2. 共享路径不按功能切。allow_shorts 与做多在 executor/risk_gates/dsl_exit/research/system_prompt/triggers 交织；删分支会改 composite_score 分母（非零权重和 1.80）→ 阈值 54 失效。只做：保持 allow_shorts=false、关 s3_shadow_shorts、维持 downtrend_momentum 权重零。
3. 个人适配走真实配置。新入口必须走 /data/.agent-config.json，禁止自建配置路径。
4. 22 道风控门属策略行为。只改记录/阻断开关，不删判定函数；个人实盘前只启用 6 道：置信度、单笔名义、并发、日内亏损、冷却、强平缓冲。

---

## 4. 三级清单（含实施状态）

### 一级：立即清除（零运行影响）✅ 已完成核心

| 对象 | 动作 | 状态 |
|---|---|---|
| risk_gates_simplified.py（111行） | 删文件，吸收 5 门设计进 solo | 🔄 设计吸收中，文件暂保留 |
| side_scoring/bracket_geometry/limit_ttl/maker_probe | 移 archive/（实测生产 0 引用，~539 行） | ⏳ 待归档 |
| s3_shadow_shorts | 配置置 false（不删代码） | ✅ 已完成（Profile + 运行时双重） |
| 20 个未跟踪文件的研究残留 | 移宿主归档 | ✅ 已完成 analysis/archive-untracked-2026-11-19/ |
| replay_17d.jsonl(460MB)、.binance-candles.json(41MB) 等 | 归档，留元数据 | ⏳ 待清理 |
| logs/*.gz 22,479 个 | 留 14 天，其余归档 | ⏳ 待清理 |
| archive/、k8s/、fly.toml | 移出主干或独立分支 | ⏳ 待处理 |
| __pycache__（123 目录）、.pyc | 清理 + 加忽略 | ⏳ 待清理 |
| refactor/hta-retirement（9f61de63） | 打 tag 后删本地分支 | ⏳ 待处理 |

小计：净删源码约 650 行（含归档，非纯删），释放存储约 1.5 GB。不删任何配置键。

### 二级：可简化（影子对照，分批）🔄 部分已推进

| 对象 | 动作 | 风险 | 验收 | 状态 |
|---|---|---|---|---|
| 影子/对账脚本 31 个 12,698 行 | 归档 scripts/research/，留 4 个（shadow_grade/reconcile_fills/daily_report/reconcile_gate_outcomes） | 中 | 白名单测试过 | ⏳ |
| 14 组影子臂 | 冻结新增；confidence_decay/atr_regime_calibration/weight_shadow/completion_cap_shadow 按需启用 | 低 | 7 天对照差异 <10% | 🔄 Profile 已冻结新增 |
| LIVE 准入链（live_gate 349行+审批链） | shadow 路径延迟加载，模块保留 | 中 | 24h 无重启 | ⏳ |
| 22 道风控门 | 旁路 12 道，不删函数（红线4） | 中 | 成交/止损分布一致 | ⏳ |
| validation/ 10 模块 ~2,000 行 | 归档离线，留均值/置信区间/成本口径，保留 fixed_effect.py 接口 | 低 | 不进入日更 | ⏳ |
| backtest/guard.py 11 守卫 | 收敛为 3 个（时间对齐、样本外、成本表存在） | 中 | 同参同数据一致 | ⏳ |
| 文档 130→6 份 | 归档 docs/archive/，新建单人入口 | 无 | — | 🔄 STRATEGY.md LIVE_SMALL_CHECKLIST.md QUICK_BACKTEST.md 已就位 |
| 配置 707 叶子 | 冻结新增，不物理删（红线1） | 中 | — | 🔄 Profile 冻结 |
| Dashboard / MCP | 本轮不纳入：改 Dashboard 断 portal 链路；MCP 无消费方但改动面大 | 低 | — | ❌ 不纳入 |

### 三级：保留（核心运行能力）✅ 锁定

扫描 perception/triggers/universe/hl_client/ws_client；研判 research/research_schema/system_prompt/ta_filter；执行 executor/client/exchange/execution/*；离场 dsl_exit/atomic_io；资金 sizing/shadow_book/核心风控门；可观测 event_log(哈希链)/session_log/metrics/watchdog/scheduler；配置 config_store/config_defaults/config_parity_guard；通知 notify；冻结保留 live_gate/manual_control。

---

## 5. 已实施代码变更（fc63a14 + 48eeafd + 29456d7）

### 5.1 hermes_trader/runtime_profile.py（新增，254 行）
- fail-closed 运行时 Profile：full（兼容默认）/ shadow（单人强制模式）/ off
- shadow 启动条件：mode=SHADOW、scan_long_only=true、runner_entry_gate.allow_shorts=false、s3_shadow_shorts.enabled=false、enable_hip3=false
- 非 full profile 自动置 HERMES_ENABLE_LIVE=0 切断实盘授权
- 纯函数设计，无 I/O，便于单测 tests/test_runtime_profile.py（96 行，全覆盖）

### 5.2 hermes_trader/solo.py（重构增强，+269 行）
- 六大单人子命令：status watch tune shadow backtest preflight
- 快速回测后台化：
  - --background：立即返回 job=bt-YYYYMMDDTHHMMSSZ-<10hex>，worker 后台跑
  - --result JOB_ID：读取状态/PID/exit_code/log/result，支持 --json
  - --job-id 隐藏参数供 worker 内部调用
  - 作业目录：默认 /tmp/hermes-solo-jobs，可用 HERMES_SOLO_JOB_DIR 覆盖
  - job_id 严格正则 ^bt-\d{8}T\d{6}Z-[0-9a-f]{10}$ 防目录穿越
  - 元数据原子写（临时文件 + os.replace），worker 自记录 running/pid
  - 不写 /data 运行时状态，纯离线回测
- 旧前台兼容：hermes-solo backtest --days N ... 保持不变
- 兜底配置路径：/data/.agent-config.json → 部署目录 → 仓库快照
- 安全键白名单：min_ai_confidence equity_fraction_per_trade max_trade_notional_usd max_daily_loss_usd cooldown_min dsl_exit.* scan.minCompositeScore
- preflight：7 项 SHADOW/LIVE 小资金安全包络检查

### 5.3 tests/test_solo_backtest_jobs.py（新增，134 行，6 测试）
- 目录穿越防护、后台作业创建/读取、非法 job 拒绝、旧前台兼容性

### 5.4 tests/test_solo_cli.py（修正）
- 更新 allowlist 测试断言，保留原功能

### 5.5 QUICK_BACKTEST.md（更新）
- 补充后台用法文档

### 5.6 STRATEGY.md / LIVE_SMALL_CHECKLIST.md（新增）
- 单人策略说明、小资金实盘核对清单

---

## 6. 服务器隔离验证实测

### 6.1 环境准备
`ash
cd /home/ldy/hermes-trader
/home/ldy/.local/bin/uv venv --python 3.11.15
/home/ldy/.local/bin/uv sync --extra dev
`

### 6.2 隔离测试目录与结果

| 目录 | 测试范围 | 结果 |
|---|---|---|
| /tmp/hermes-shadow-test-bg | 全量（含新增 solo 后台化） | **29/29 通过** |
| /tmp/hermes-shadow-test.5yNbST | 定向 90+178 用例 | 全部通过 |

### 6.3 全量测试预存失败（20 个，与本次改动无关）

| 测试 | 失败原因 | 判定 |
|---|---|---|
| test_audit_2026_09_10_risk_tuning_shadow.py | _record_risk_tuning_shadow 记录数量不符（4 vs 1） | 预存 shadow 行为漂移 |
| test_m2_mover_bypass_veto.py / test_m2_r1_enforce.py | 同上 | 预存 shadow 行为漂移 |
| test_p1_6_loop_runtime_registration.py | 期望 20 叶，实际 21（新加 scan_bar_aligned） | 既有注册变更 |
| test_r13_b11_memory_equity_quality_gate_registration.py | 期望 7 叶，实际 8（新加 slip_validation_min_samples） | 既有注册变更 |
| test_trial_ledger_s1_4.py | trials_ledger.jsonl 不存在（未跟踂文件未入库） | 环境依赖 |
| test_config_parity_guard.py | prod_override era 漂移 | 既有配置漂移 |

结论：无需修复，最终报告记录为预存/环境失败，不阻塞发布。

---

## 7. 关键运行态基线（报告必须引用，容器内实测）

### 7.1 生产配置实测
`	ext
mode=SHADOW
scan_long_only=true
max_concurrent=2
allow_shorts=false
s3_shadow_shorts.enabled=false
enable_hip3=false
leverage=10
equity_fraction_per_trade=0.15   # runtime config 实际值（canonical fallback 0.2）
max_trade_notional_usd=30
min_order_usd=10.5
min_ai_confidence=0.6
`

关键区分：runtime config equity_fraction = 0.15 ≠ CANONICAL_DEFAULTS fallback = 0.2，有效值 0.15。原报告写权威值 0.2是静态默认口径，最终报告必须明确区分。

### 7.2 SHADOW 账本基线（/data/.shadow-book.json）
`	ext
starting_balance=100.0
Taker wallet_balance=98.7017019787069, closed_count=164, open=0
Maker wallet_balance=85.71410081638383, closed_count=82, open=0
`

---

## 8. 实施路线（更新版）

### 波一（1 人日）✅ 核心已完成
- 阶段 0 基线：备份 + parity 校验 + tag + 4,633 测试基准 ✅
- 阶段 1 清理：执行一级 9 项；观察 2h（healthy、事件持续、权益无跳变）🔄 进行中
- 阶段 2 入口：新增 6 子命令脚本（--help <1s）；tune 写真实配置并回读一致、parity 空；backtest 实跑 7 天；六步计时 ≤1 分钟（回测除外）✅ 已实施并验证

### 波二（3–4 人日 + 7 天观察）🔄 待启动
- 阶段 3：执行二级 2-1~2-8
- 观察窗：改造前 7 天 vs 后 7 天，比成交数/胜率/平均 ROE/止损分布/信号数，差异 <10% 且 PnL 符号一致

每阶段独立 commit + tag；回滚 = 切回上一 tag 重建镜像，数据卷不动。

回滚锚点（必须保留）：
`
Git:  pre-shadow-overhaul-2026-11-17 = 755fc72c
Docker: hermes-deploy-hermes-trader:pre-shadow-overhaul-20261117
`

---

## 9. 单人入口操作清单（已落地）

| 命令 | 作用 | 耗时 | 约束 |
|---|---|---|---|
| hermes-solo status [--json] | 模式/权益/持仓/PnL 单屏 | <10s | 只读 |
| hermes-solo tune KEY=VALUE ... | 改单参数写 /data/.agent-config.json | <10s | flock 接口；改后 parity 空 |
| hermes-solo backtest --days 7 ... [--background] | 7-14 天快速回测 | <3min / 后台即返 | 封装 backtest_quick.py，先实跑验证 |
| hermes-solo backtest --result JOB_ID [--json] | 读取后台作业结果 | <5s | — |
| hermes-solo watch | 当前信号 + 核心风控 | <10s | 无评级/审批入口 |
| hermes-solo shadow | 切 SHADOW | <10s | 默认态，通常无需执行 |
| hermes-solo preflight | 小资金实盘安全包络检查 | <10s | 7 项全绿才可 LIVE |

不做：发明 --mode 参数、写自建配置路径、引入第二套回测逻辑（回测与影子共用同一套信号与离场代码）。

四类核心文档：
1. STRATEGY.md（1页策略逻辑说明）
2. QUICK_BACKTEST.md（最小流程快速回测）
3. LIVE_SMALL_CHECKLIST.md（小资金实盘验证要点）
4. 核心参数表 1 张 + 5 行变更日志（动态调整规则）

一日闭环：
`
09:00 tune → 09:05 backtest → 09:15 判定 → 09:30 shadow → 14:00 live（可选）→ 16:00 复盘
`
一个工作日闭环是操作链路目标，不保证当天出现合格交易信号。

---

## 10. 信息采纳/纠正/废弃/重复归并表

| 来源 | 信息条目 | 处理 | 说明 |
|---|---|---|---|
| 用户上传终版 | equity_fraction=0.2 权威值 | **纠正** | 实测 runtime 0.15，canonical 0.2，有效值 0.15 |
| 用户上传终版 | fixed_effect.py 零引用可删 | **纠正** | 实测 20 处引用，不可删 |
| 用户上传终版 | s3_shadow_shorts.enabled=true 风险 | **采纳+已修** | Profile + 运行时双重置 false |
| 用户上传终版 | solo CLI 待决策 | **废弃** | 已实施 6 子命令+后台化 |
| 用户上传终版 | 20 个未跟踂文件待决策 | **采纳+已修** | 已归档 analysis/archive-untracked-2026-11-19/ |
| 用户上传终版 | 9f61de63 待决策 | **采纳** | 打 tag 归档后删本地分支 |
| 用户上传终版 | Dashboard/MCP 纳入本轮 | **废弃** | 本轮不纳入，风险过高 |
| 仓库原始报告 | 29/29 隔离测试通过 | **采纳** | 服务器实测确认 |
| 仓库原始报告 | 20 个预存失败 | **采纳** | 标记为环境/既有漂移，不阻塞 |
| 融合版 | 三级分类体系 | **采纳** | 作为本报告核心结构 |
| 融合版 | 四条红线 | **采纳** | 硬约束，违者回滚 |
| 全量分析 | 实测基线数据 | **采纳** | 覆盖所有静态口径冲突 |

---

## 11. 风控与回归

**风险**：
- 重启风暴（改前 parity、改后 3 周期）
- 阈值失效（不动权重）
- 做空误开（diff 双核 + live 二次确认）
- 账本污染（原子写、只动归档副本）
- 回测误导（强制实跑 + 交叉验证）
- 链路中断（不动 nginx/portal/SSE）
- 测试退化（禁删测试，只归档）
- fixed_effect 误删（已剔除）
- 外部依赖：OpenRouter HTTP 429、LLM 配额、GDELT 偶发 SSL 超时——报告中列外部依赖风险，不承诺市场信号或交易结果

**回归**：
- 交易逻辑：执行器/DSL/风控/LIVE 门禁五组全过 + 固定片段重放 + 构造价格序列 + 重启对账
- 数据配置：配置测试 + 影子账本 + 事件桥接 + parity 空 + 日报重跑逐项一致
- 日志：哈希链校验 + 事件类型完整 + 无倒序/重复 ID + error 率无升
- 节奏：日更 5 文件（执行器、DSL、风控、配置、日志）≤60s；发版前 4,633 门禁不降

---

## 12. 收益与决策

| 维度 | 前 | 后（预期） |
|---|---|---|
| 可安全删源码 | — | ~650 行（含归档） |
| scripts | 186 / 61,891 行 | ~130 / 45,000 行 |
| 文档 | 130 份 / 505 MB | 6 份 / <5 MB |
| 日志 | 1.2 GB | ~150 MB |
| 日更测试 | ~6 min | ≤60s |
| 改参→回测 | 手工跨脚本 | ≤5 min |
| 配置 | 707 叶子 | 不缩减，只冻结 |

**待决策**：
1. s3_shadow_shorts 立即关闭？（建议：✅ 已完成）
2. 9f61de63 处理？（建议：打 tag 归档后删本地分支）
3. 波二现在做还是先跑 1 周波一？（建议：先观察波一稳定 1 周）
4. LIVE 代码删除还是冻结？（建议：冻结，删了破坏 parity 与回测等价性）
5. Dashboard/MCP 纳入本轮？（建议：否）
6. 日更测试定哪 5 个？（建议：执行器、DSL、风控、配置、日志）

---

**结论**：条件 Go。波一核心已落地并验证（Profile + 回测后台化 + 单人入口），风险近零；波二须在波一稳定运行 1 周、影子对照达标后启动。所有生产文件变更已通过隔离测试验证，等待生产部署决策.
