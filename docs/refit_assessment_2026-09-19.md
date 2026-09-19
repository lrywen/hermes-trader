# Hermes Trader 改造工作综合检查与评估报告

> **评估日期**：2026-09-19（Asia/Shanghai）
> **评估对象**：`ldy@192.168.124.65:/home/ldy/hermes-trader`，分支 `feat/optimization-v3` @ HEAD `eddcd77`；生产容器 `hermes-trader` Up / healthy
> **基准文档**：
> ① `~/hermes-trader_统一技术改造文档_2026-09-19.md`（1055 行，41 项任务 A–G / G1–G3，依据 19 份调研报告 8,394 行）
> ② `~/Hermes_改造缺口清单_未做不做做错_2026-09-18.md`（322 行，核对基线 `e9648f9`）
> **方法**：全程只读取证（git 历史/源码/容器 `/metrics`/compose/Dockerfile/宿主缓存），评估期间未改动运行系统；落盘前对工作树做了 ruff 机械修复与测试回归（见 §6）。

---

## 1. 原改造工作执行状态

### 1.1 09-18 缺口清单 10 项「未做」在 HEAD `eddcd77` 的实测结论

| # | 缺口项 | 09-18 判定 | 当前实测 | 关键证据 |
|---|---|---|---|---|
| ① | Dockerfile scripts 白名单（P0-1） | 未做 | **已闭环** | `scripts/runtime_whitelist.py`（19 条，逐条标注容器内调用方）+ `tests/test_runtime_scripts_whitelist.py` 三重锁定（存在性 / 禁研究脚本 / Dockerfile 精确匹配 / 禁止 blanket COPY）；提交 `a670273`；`Dockerfile` L44-74 逐条 COPY |
| ② | 研究负载物理隔离（P0） | 未做 | **逻辑隔离达成，物理隔离未做** | 研究脚本不进镜像；运行侧 `docker-compose.yml` 有 `mem_limit: 1g` + `cpus: 2.0` 硬配额。但研究负载仍可经同一容器/宿主执行，无独立研究容器与独立卷 |
| ③ | P0-2 缺 4 项失败模式指标 | 未做 | **已闭环且已上线** | `hermes_trader/metrics.py` L678-702：`hermes_feed_gap_fraction`/`hermes_feed_trustworthy`/`hermes_supervisor_age_seconds`/`hermes_alerts_firing`/`hermes_ai_brain_ready`；容器 `/metrics` 实测有真实值；提交 `6a21747` |
| ④ | P1-1 executor 三步解构 | 未做 | **进行中（约 55%）** | step① 完成（`hermes_trader/agents/shadow/` 5 模块，`7a294c0`）；step② 起步（`hermes_trader/execution/orders.py` 仅 62 行 facade，`81104e8`）；step③ 已提取 24 个 gate 叶子，但核心 `maybe_execute` 仍是单体（`agents/executor.py:3588`，文件 6,560 行） |
| ⑤ | P1-3 类型层集中 | 未做 | **基本完成** | `models/types.py` 35→154 行，GateContext 已集中（`0a51a15`） |
| ⑥ | P1-6′ 测试基线硬护栏 | 未做 | **已闭环（有瑕疵）** | `.github/workflows/ci.yml` L36-40 硬门 + `scripts/ci_test_guard.py` `MIN_OFFLINE_TESTS=4089` + ruff 新违规基线门（`a38df9e`）。瑕疵：当前实测 4,376 passed，floor 未随 P4 新增测试上抬，留有 287 个净删余量 |
| ⑦ | P2-1 scripts 瘦身 72→35-40 | 未做 | **反向恶化** | `scripts/` 现 79 个 .py（09-19 09:32 复核时 74），增量为未跟踪的 bt_p6/bt_ra/bt_ra_exch/backtest_block_bootstrap/collect_candles。白名单使其不进生产镜像，风险性质降为仓库可维护性 |
| ⑧ | P2-2 dashboard/server 解构 | 未做 | **未做（部分缓解）** | `dashboard.py` 3,103 行、`server.py` 2,671 行；路由已外移到 `dashboard_routes/` 8 个子模块 |
| ⑨ | P2-3 策略插件化 | 未做 | **未做** | 策略逻辑仍内联 executor/signals |
| ⑩ | P2-5 证伪台账 | 未做 | **文档侧替代、代码侧未做** | 仓库内无 SCOREBOARD/findings 机制；19 份报告在仓库外（宿主 `/home/ldy/`，未纳版本管理） |

**做错项**：A-2 过期注释 22→25 已修（`executor.py:4382`，`b1d2fb9`）；A-3 编号复用未修（纯文档卫生）。「不做 7 条」红线抽查无违反：`HERMES_ENABLE_LIVE` 三道守卫（`config_store.py:2173`、`server.py:1572`、`executor.py:3623`）完好，系统仍处 SHADOW。

**交叉印证**：`~/Hermes_改造进度复核_2026-09-19.md`（09:32，HEAD `fced777`，非指定文档，仅旁证）结论与上表一致：P0 全闭环 / P1 推进 / P2 全未动，4,158 passed。

### 1.2 09-19 统一文档任务执行状态

| 任务 | 状态 | 证据 |
|---|---|---|
| P4 统一回测内核（前置基础） | **已完成、2026-09-19 已提交** | `hermes_trader/backtest/`（types/cost/exit_dsl/driver/signals/stats/guard）+ `data/historical_candles.py`（PIT/20k 上限/原子写）；`scripts/backtest.py` 薄 CLI；`DslBarExit` 为生产 `DSLTracker` 的 bar 适配器，parity 测试锁定 |
| B-5 P5-1bd 块自助固化 | **工具已成、2026-09-19 已提交，CI 固化待做** | `scripts/backtest_block_bootstrap.py` + bt_ra*；往返成本 9.26 bps（手续费 8.64 + 滑点 0.62），flat 模式 5,453 笔 0 偏差自检 |
| B-1a-改（杠杆/穿透/10bps/[atr] 四改） | **未开始** | `scripts/bt_ra_exch.py:603-606` 仍硬编码 `lev=1`，注释自认"恒取 max_loss_pct" |
| B-2 全回测强制 maxc=2 | **未开始** | bt_ra*/backtest_majors_surge/内核 grep `max_concurrent` 零命中；当前峰值并发 7（filt +13.86%→−5.20%） |
| B-3 币池 8→81 + 预热 | **预热已跑完，有 8 币 5m 缺口** | `--pool /tmp/p6b1_recon.json`（bt_ready_pool=81）；缓存 `logs/binance_klines_cache/`（235MB，gitignored，宿主侧不占生产卷）：4h 81/81、1h 78/81（缺 LTC/MINA/NIL）、5m 73/81（缺 MINA/MORPHO/NIL/NOT/SUPER/TAO/TNSR/UNI） |
| B-4 per-coin 滑点表扩 81 币 | 未开始 | 半价差实测 2.54 vs 当前 0.31 bps |
| B-12/13 SHADOW 硬门禁 + LIVE 验收门 | **增强项，非新建** | 三道 LIVE 守卫已在，缺"组合断言 + 块自助正面验收门" |
| A/C/D/E/F 其余批次 | 未开始 | 调研结论已定案，代码未落；§4 trading_loop.py:1445-1665 对账块标签待改 |

### 1.3 已提交代码变更记录（`e9648f9..eddcd77`，32 提交，09-17 22:22 → 09-19 05:22）

| 主题 | 数量 | 代表提交 |
|---|---|---|
| `refactor(executor): P1-1 step 3 extract … leaf` | 24 | S2/S3/S6-S11、H4/H-6、spread/ATR/news/equity-floor 等 gate 叶子 |
| `build(docker)` P0-1 白名单 | 1 | `a670273` |
| `feat(metrics)` P0-2 五项指标 | 1 | `6a21747` |
| `ci` P1-6′ 护栏 | 1 | `a38df9e` |
| `refactor(types)` P1-3 GateContext | 1 | `0a51a15` |
| `refactor(execution)` orders facade | 1 | `81104e8` |
| `refactor(executor)` step① shadow 外移 | 1 | `7a294c0` |
| `test(executor)` 特征化测试预备 | 1 | `560f20f` |
| `fix(executor)` S2 helper 自递归 | 1 | `046bdbb` |
| `docs(executor)` 22→25 注释 | 1 | `b1d2fb9` |

### 1.4 部署态

- 容器 `hermes-trader` healthy（web+loop+scheduler 三进程，critical-process guard），`hermes-portal`、`litellm` 均在跑。
- 权威配置：容器内 `/data/.agent-config.json`（宿主 `/home/ldy/hermes-deploy/.agent-config.json` bind，0600，142 键），符合 G15。
- `/data` 为命名卷 `hermes-deploy_hermes_data`；研究 K 线缓存在宿主仓库 `logs/`（gitignored），未污染生产卷。

---

## 2. 新方案 v2.0 与原改造的技术兼容性

| 层面 | 判定 | 分析 |
|---|---|---|
| 架构设计 | **高** | 新方案落点为研究侧（scripts/bt_* + `hermes_trader/backtest` 内核）与启动门禁；P0/P1 落点为生产侧（Dockerfile/executor/CI/metrics），代码面几乎不相交。P4「内核+薄 CLI」与 P1「叶子提取+facade」同为减法式重构 |
| 接口规范 | **高** | 无新对外 API/协议；`DslBarExit` 适配生产 `DSLTracker`（parity 锁定）；B-1a 复用生产 `sync_exchange_sl`（`executor.py:4793`）与 `sl_buffer_bps=10`；B-12/13 在既有三守卫上加断言 |
| 数据模型 | **高，一处须显式约束** | 回测缓存（logs/）与生产 JSONL（/data/*.jsonl）物理分离。约束：未来研究输出/预热数据禁止写入 `/data` 生产卷（容器内已有 09-16 的 `bt_out_*` 残留目录，建议清理） |
| 业务逻辑 | **中-高，一处真冲突面** | B-1a 是对回测语义的纠偏（与生产对齐），非冲突；但 trading_loop 对账块归因重标（28 交易所侧/22 本地 DSL/1 无匹配）直接触碰生产交易循环，须与 P1 executor 叶子提取错峰 |
| 配置/门禁 | **高** | 真相源一致（容器内 `/data/.agent-config.json`），新方案只增 loop_runtime/门禁键，不改加载机制 |

---

## 3. 潜在影响与风险

| # | 风险 | 类别 | 等级 | 说明 |
|---|---|---|---|---|
| R1 | P4/B-5 未提交资产丢失 | 资产安全 | 高 | 落盘前 1,445+ 行新代码仅存工作树。**已于 2026-09-19 提交消除**，见 §6 |
| R2 | 研究数据写入共享 `/data` 卷 | 数据/容量 | 中 | 当前预热落 logs/ 已规避；需把"研究输出禁入 /data"写成纪律（guard 或文档），并清理 `bt_out_*` 残留 |
| R3 | 研究负载与生产同主机 | 性能 | 中 | 1g/2cpu 配额兜底容器内负载；宿主 venv 预热不受配额约束，CPU/IO 高峰仍可能影响容器，重回测应避开交易活跃时段 |
| R4 | 对账块重标与 P1 并发改动 | 功能冲突 | 中 | 两线同改 executor/trading_loop 文件族，交错期合并冲突与回归概率高 |
| R5 | B-1a/B-2 使历史 edge 结论整体重排 | 决策 | 中 | 方案目的（PURR 穿透 −3.236% vs cap 1.67%；maxc=2 后 filt +13.86%→−5.20%），但在三者全部落地前任何数字不得用于 LIVE 决策 |
| R6 | `docker exec` 白名单旁路 | 安全 | 低-中 | 白名单只控镜像内容不控 exec；属既有信任边界 |
| R7 | CI floor 4089 低于实测 4,376 | 回归防护 | 低 | 允许净删 287 测试不报警；M0 冻结门应上抬 |
| R8 | 19 份调研依据未纳 git | 可追溯 | 中 | 报告引用的行号/文件（如 bt_ra_exch.py）已发生漂移；建议归档进 `docs/research/2026-09/` |

---

## 4. 新问题引入可能性与严重程度分级

| 级别 | 潜在问题 | 可能性 |
|---|---|---|
| 严重 | 未发现新方案必然引入的严重问题 | — |
| 高 | 新旧回测脚本并存期误用旧路径（lev=1/maxc=7）得出矛盾 edge 结论 | 中-高。规避：`backtest/guard.py` 把 maxc=2 与逐笔杠杆校验写成硬错误 |
| 中 | 预热/重回测写满生产卷 | 中（独立路径可根除） |
| 中 | P1 暂缓后恢复时与 B-12/13/对账改动交叉，特征化测试需重标定 | 中 |
| 低 | scripts 数量膨胀抬高认知成本（生产不可达） | 高概率/低影响 |
| 低 | 白名单命名规则误伤未来运行时脚本 | 低（fail-closed，良性） |

**维护难度净评估**：长期下降（1,445 行内核取代 bt_p6/bt_ra/bt_ra_exch 多套分叉）；短期并存期上升，窗口应尽量短。

---

## 5. 综合结论与建议

**结论：新方案 v2.0 可行，评级 GO（可推进）。** 与已完成 P0/P1 资产四层兼容性高、无正面冲突；生产 healthy、4,376 测试基线与 SHADOW 红线提供安全网；主要风险在工程管理而非技术方向。

**实施建议（按序）**：
1. ~~抢救式提交 P4/B-5 资产~~（2026-09-19 已完成，见 §6）。
2. W0 冻结门：全量 4,376 留证；CI floor 4089 上抬到实测值；研究输出路径白名单（禁入 `/data`）。
3. 补齐预热 8 币 5m / 3 币 1h 缺口或记录豁免理由（B-3 闭环条件）。
4. 批次 1→2：先只读取证（D-3′/D-1），再落 B-1a-改/B-2/B-5 CI 固化；maxc=2、lev≠1 校验写入 guard.py 硬错误。
5. 与 P1 错峰：动 trading_loop 对账块前冻结 executor 叶子提取；P1 剩余 step③ 与 P2-1/2/3/5 正式标记 deferred（资产保留，M3 后评估）。
6. 19 份调研报告 + SCOREBOARD + 两篇基准文档纳入仓库 `docs/research/2026-09/`。
7. B-12/13 收尾：沿用 `HERMES_ENABLE_LIVE` 扩展组合断言；块自助 ≥1 项正面结果前不离开 SHADOW（§5.2 三结局 A/B/C 判据）。

---

## 6. 落盘当日的处置记录（2026-09-19）

- 用户授权后执行：ruff 30 个机械违规（29 个冗余 `# noqa: E402` + 1 个未用 import）`--fix` 清零，全部落在本次新增/修改文件内。
- 定向回归 396 passed（P4 内核六件套 + 数据层 shim + collect_candles + cleanup 内核编排）。
- P4/P5-1bd 成果分 4 个语义化提交落地（仅 commit，未 push），提交后工作树仅剩本文档与方案文档。
- 全量离线基线结果以提交后 CI/本地记录为准。
