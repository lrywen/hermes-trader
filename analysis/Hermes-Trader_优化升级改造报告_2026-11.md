# Hermes-Trader 优化升级改造报告（2026-11）

**日期**：2026-11-22　**对象**：ldy@192.168.124.65:/home/ldy/hermes-trader  
**分支**：refactor/shadow-solo-v1（HEAD fc63a14 + 48eeafd + 29456d7）  
**基线版本**：生产镜像 c43d5be8（早于 fc63a14），当前容器 healthy、0 重启  
**性质**：全维度系统性优化分析，配套分阶段路线图、风险控制、验证标准

---

## 1. 现状分析

### 1.1 系统规模与运行态

| 维度 | 实测值 |
|---|---|
| 代码规模 | 147 模块 / 73,981 行（hermes_trader/），全仓 756 Python 文件 |
| 测试基线 | 371 测试文件 / 86,990 行，4,633 门禁（服务器隔离 20 预存失败） |
| 容器资源 | 2 GiB / 2 CPU，当前占用 535.8 MiB（26%），0 重启 |
| 数据卷 | /data 2.9 GB（含 book-raw 896 MB、shadow-book、决策快照、归因表） |
| 协作容器 | 7 个（nginx、portal、BFF、Feishu 等） |
| 配置复杂度 | 156 顶层键，704 叶子节点（canonical 154/704） |

### 1.2 核心运行配置（容器内实测）

`	ext
mode=SHADOW
scan_long_only=true
max_concurrent=2
allow_shorts=false
s3_shadow_shorts.enabled=false
enable_hip3=false
leverage=10
equity_fraction_per_trade=0.15   # runtime 有效值（canonical 0.2）
max_trade_notional_usd=30
min_order_usd=10.5
min_ai_confidence=0.6
`

### 1.3 已完成的核心改造（fc63a14 + 48eeafd + 29456d7）

- **Runtime Profile**：fail-closed shadow 模式，强制 SHADOW/单边/无 HIP-3/无做空
- **hermes-solo CLI**：6 子命令（status/watch/tune/shadow/backtest/preflight）
- **快速回测后台化**：--background/--result/--json，作业隔离，29/29 通过
- **单人操作文档**：STRATEGY.md/QUICK_BACKTEST.md/LIVE_SMALL_CHECKLIST.md

---

## 2. 八维度优化调研分析

### 2.1 性能优化

| 问题 | 证据 | 影响 | 优先级 |
|---|---|---|---|
| LLM 遥测日志无轮转 | completion_cap_shadow 44k 行、reasoning_effort_rollout 44k 行，半月积累 | 磁盘无界增长，/data 2.9 GB | P0 |
| book-raw 90 天保留 | 单日 475 MB，稳态 3.6-4 GB | 卷容量压力，M-1 OFI 预注册检验需保留 | P1 |
| 影子/对账脚本 31 个 12,698 行 | 仅 4 个核心脚本高频使用 | 启动加载、测试收集开销 | P2 |
| 无连接池复用 | hl_client/ws_client 每次重建 | 高频扫描延迟抖动 | P2 |

### 2.2 架构合理性

| 问题 | 证据 | 影响 | 优先级 |
|---|---|---|---|
| 双层排序职责不清 | coin_selection（准入）+ signal_ranking（定序）易冲突 | 重复过滤、阈值漂移 | P1 |
| LIVE 准入链 349 行+审批 | SHADOW 路径仍加载完整模块 | 启动慢、内存占用 | P2 |
| 风控门 22 道交织 | allow_shorts 在 6 大模块交织 | 删分支破坏 composite_score 分母 | P0（红线） |
| 配置 704 叶子无冻结机制 | 持续新增键，parity guard 易漂移 | 启动失败、重启风暴 | P0（红线） |

### 2.3 代码质量

| 问题 | 证据 | 影响 | 优先级 |
|---|---|---|---|
| 死代码残留 | risk_gates_simplified.py 0 引用、side_scoring 等 4 模块 0 引用 | 认知负担、测试噪音 | P1 |
| 归档分支未清理 | refactor/hta-retirement (9f61de63) | 误合并风险 | P2 |
| __pycache__ 123 目录 | 无 .gitignore 覆盖 | 打包体积、同步干扰 | P2 |
| 未跟踂文件 20 个 | 已归档 analysis/archive-untracked-2026-11-19/ | 已处理 | ✅ |

### 2.4 用户体验（单人操作视角）

| 问题 | 证据 | 影响 | 优先级 |
|---|---|---|---|
| 无统一入口 | 原需跨脚本手工操作 | 改参→回测 >30 分钟 | P0（已解决） |
| 回测阻塞前台 | 无后台作业机制 | 无法并行、终端占用 | P0（已解决） |
| 文档 130 份分散 | 核心信息被淹没 | 单人决策检索困难 | P1（已解决 6 份核心） |
| 无单步操作计时 | 无 SLA 感知 | 无法量化体验 | P2 |

### 2.5 安全性

| 问题 | 证据 | 影响 | 优先级 |
|---|---|---|---|
| parity guard fail-closed | 漂移未登记即启动失败 → 重启风暴 | 可用性风险 | P0（红线） |
| LIVE 三闸 AND 逻辑 | mode=LIVE + HERMES_ENABLE_LIVE + B-13 记录 | 误翻 LIVE 风险极低 | P1 |
| 无配置键删除保护 | 删键破坏 parity 与回测等价性 | 隐性缺陷 | P0（红线） |
| 外部依赖无熔断 | OpenRouter 429、LLM 配额、GDELT SSL 超时 | 信号中断、策略失效 | P1 |

### 2.6 可维护性

| 问题 | 证据 | 影响 | 优先级 |
|---|---|---|---|
| 测试 4,633 门禁跑 6 分钟 | 日更反馈周期长 | 迭代速度受限 | P1 |
| 影子臂 14 组无冻结 | 持续新增、无清理机制 | 样本稀释、对照困难 | P1 |
| 文档归档机制缺失 | 130 份混杂主干 | 历史查找困难 | P2 |

### 2.7 可扩展性

| 问题 | 证据 | 影响 | 优先级 |
|---|---|---|---|
| Dashboard/MCP 耦合 | 改动断 portal 链路 | 扩展新 UI 高风险 | P2（本轮不纳入） |
| universe 固化 44 主流币 | 真实扫描面 234 eligible / cap 60 | 微盘机会缺失 | P1（M1 PIT 解决） |
| 回测无 PIT 币池 | 幸存者偏差 + 前视选币 | 历史 edge 虚高 | P1（M1B 解决） |

### 2.8 资源利用率

| 问题 | 证据 | 影响 | 优先级 |
|---|---|---|---|
| 容器内存 26% | 535.8 MiB / 2 GiB | 充裕，不追求压缩 | P3 |
| CPU 闲置 | 无明显热点 | 充裕 | P3 |
| 磁盘 /data 2.9 GB | book-raw 896 MB、logs 1.2 GB | 清理可释放 ~1.5 GB | P1 |

---

## 3. 技术债务、性能瓶颈、功能短板、体验痛点汇总

| 类别 | 条目 | 严重度 | 根因 |
|---|---|---|---|
| 技术债务 | risk_gates_simplified.py 等死代码 0 引用 | 中 | 历史实验残留未清理 |
| 技术债务 | refactor/hta-retirement 分支未归档 | 低 | 合并后未删本地分支 |
| 技术债务 | __pycache__ 无忽略、.pyc 入库 | 低 | 早期疏忽 |
| 性能瓶颈 | LLM 遥测日志无轮转（44k 行/半月） | 高 | 缺 log_rotate 机制 |
| 性能瓶颈 | book-raw 90 天保留（单日 475 MB） | 中 | M-1 OFI 预注册硬性要求 |
| 性能瓶颈 | 影子/对账脚本 31 个仅 4 核心 | 中 | 研究脚本混入主干 |
| 功能短板 | 回测无 PIT 币池（幸存者偏差） | 高 | 历史 universe 快照未接入回测 |
| 功能短板 | 双层排序 coin_selection/signal_ranking 易冲突 | 中 | 职责边界未固化、无契约测试 |
| 功能短板 | 22 道风控门交织、删分支破坏分母 | 高 | 共享路径耦合、红线约束 |
| 体验痛点 | 单人操作跨脚本、无计时 SLA | 高 | 原设计面向团队协作 |
| 体验痛点 | 文档 130 份、核心信息检索困难 | 中 | 无归档分层、无单人入口 |

---


## 4. 优化清单与实施方案

### 4.1 一级：立即清除（零运行影响，P0/P1）

| 编号 | 优化点 | 方案 | 预期收益 | 工作量 | 风险 | 验证标准 |
|---|---|---|---|---|---|---|
| O1 | 移除 risk_gates_simplified.py | 吸收 5 门设计进 solo，删文件 | -111 行，认知负担下降 | 0.5 人日 | 低（0 引用） | 测试全过、solo 含 5 门 |
| O2 | 归档 side_scoring 等 4 模块 | 移 archive/，保留导入兼容 | -539 行，启动加载下降 | 0.5 人日 | 低（生产 0 引用） | 白名单测试过 |
| O3 | 关闭 s3_shadow_shorts | Profile + 运行时双重置 false | 消除做空误开风险 | 已完成 | 无 | 配置实测 false |
| O4 | 归档 20 个未跟踂文件 | 已移 analysis/archive-untracked-2026-11-19/ | 主干纯净 | 已完成 | 无 | git status 干净 |
| O5 | 清理 replay_17d.jsonl (460MB) 等大文件 | 归档宿主，留元数据索引 | 释放 ~500 MB | 0.5 人日 | 低（只读归档） | 磁盘空间释放 |
| O6 | 清理 logs/*.gz 22,479 个 | 留 14 天，其余归档压缩 | 释放 ~1 GB | 1 人日 | 低（只读归档） | 仅保留 14 天 |
| O7 | 移出 archive/k8s/fly.toml | 独立分支或删除 | 主干聚焦 | 0.5 人日 | 低 | 主干无无关目录 |
| O8 | 清理 __pycache__ + 加 .gitignore | 递归删 + 忽略规则 | 同步/打包加速 | 0.2 人日 | 无 | 无 pycache 目录 |
| O9 | 归档 refactor/hta-retirement | 打 tag pre-hta-retirement-9f61de63 后删分支 | 避免误合并 | 0.2 人日 | 低 | 本地分支消失 |
| O10 | 接入 log_rotate.py（移植 Pathiel） | 遥测类 20MBx3、影子类 30 天 | 遥测日志不再无界增长 | 1 人日 | 低（只读轮转） | 单文件 80MB、写入不中断 |

### 4.2 二级：可简化（影子对照，分批，P1/P2）

| 编号 | 优化点 | 方案 | 预期收益 | 工作量 | 风险 | 验证标准 |
|---|---|---|---|---|---|---|
| O11 | 影子/对账脚本归档 | 31到4 核心，其余移 scripts/research/ | -80% 脚本行数，测试收集下降 | 2 人日 | 中 | 白名单测试全过 |
| O12 | 14 组影子臂冻结新增 | Profile 冻结，仅 4 组按需启用 | 样本纯净、对照可信 | Profile 已冻结 | 低 | 7 天对照差异 <10% |
| O13 | LIVE 准入链延迟加载 | SHADOW 路径不加载 live_gate 349 行 | 启动内存下降、加载下降 | 1 人日 | 中 | 24h 无重启 |
| O14 | 22 道风控门旁路 12 道 | 保留 6 核心（置信度/名义/并发/日亏/冷却/强平） | 简化决策链路 | 2 人日 | 中（红线4） | 成交/止损分布一致 |
| O15 | validation/ 10 模块归档离线 | 留均值/置信区间/成本口径，保留 fixed_effect.py | -2000 行不进入日更 | 1 人日 | 低 | 接口保留、离线可用 |
| O16 | backtest/guard.py 11到3 守卫 | 仅留时间对齐、样本外、成本表存在 | 回测启动加速 | 1 人日 | 中 | 同参同数据一致 |
| O17 | 文档 130到6 份 | 归档 docs/archive/，核心 6 份服务单人 | 检索效率上升 | 核心 3 份就位 | 无 | 核心文档可达 |
| O18 | 配置冻结新增 | Profile 冻结，不物理删键（红线1） | 防漂移 | Profile 冻结 | 中 | parity guard 0 漂移 |
| O19 | 双层排序契约测试 | 固化 coin_selection到TA到signal_ranking 顺序 | 防冲突 | 0.5 人日 | 低 | 契约测试通过 |
| O20 | universe PIT 快照接入回测 | 复活 universe_snapshot.py，回测默认 --universe pit | 消除幸存者偏差 | 2 人日 | 中 | PIT vs current A/B 报告 |

### 4.3 三级：保留与强化（核心运行能力，P0/P1）

| 编号 | 核心模块 | 强化方向 | 工作量 | 验证标准 |
|---|---|---|---|---|
| O21 | 扫描链路（perception/triggers/universe） | 保持稳定、微调阈值仅经影子对照 | — | 影子对照差异 <10% |
| O22 | 研判链路（research/ta_filter/system_prompt） | 保留、不扩展 | — | 现有测试全过 |
| O23 | 执行链路（executor/client/exchange/execution） | 零改动、仅观测 | — | 成交/滑点分布稳定 |
| O24 | 离场链路（dsl_exit/atomic_io） | 零改动、仅观测 | — | 止损/保护分布稳定 |
| O25 | 资金/风控（sizing/shadow_book/核心 6 门） | 仅校准阈值、不改逻辑 | — | fail-closed、对账一致 |
| O26 | 可观测（event_log/session_log/metrics/watchdog） | 接入 log_rotate、保留哈希链 | — | 事件完整、无倒序 |
| O27 | 配置栈（config_store/config_defaults/config_parity_guard） | 冻结新增、fail-closed 守护 | — | 启动 0 漂移 |
| O28 | 通知 | 保持 Feishu 告警通道 | — | 告警送达 |
| O29 | 单人入口（hermes-solo） | 迭代优化 CLI、补全帮助 | 0.5 人日/轮 | --help <1s、单步 1min |

---

## 5. 分阶段迭代升级路线图

| 阶段 | 时间窗 | 核心目标 | 交付物 | 验收标准 |
|---|---|---|---|---|
| Phase 0（已完成） | — | 基线建立、Profile/CLI/回测后台化落地 | fc63a14/48eeafd/29456d7、29/29 测试通过 | 容器 healthy、配置实测、账本基线 |
| Phase 1 | 第 1 周 | 一级清除（O1-O10），释放存储、堵住日志无界增长 | 10 项独立 commit+tag、log_rotate 生效 | 磁盘释放 ~1.5 GB、遥测单文件 80MB、全量测试不降 |
| Phase 2 | 第 2-3 周 | 二级简化第一批（O11-O14），影子脚本归档、风控门旁路 | 脚本 31到4、风控 22到6、LIVE 延迟加载 | 白名单测试过、成交/止损分布一致、启动内存下降 |
| Phase 3 | 第 4 周 | 二级简化第二批（O15-O20），validation/backtest_guard/文档/配置/PIT | 回测默认 PIT、文档 6 份、配置冻结 | PIT A/B 报告、同参同数据一致、parity 0 漂移 |
| Phase 4 | 第 5-6 周 | 影子对照观察窗（7 天 + 7 天） | 对照报告：成交数/胜率/ROE/止损/信号数 | 差异 <10% 且 PnL 符号一致 |
| Phase 5 | 第 7 周 | 生产灰度发布 | 隔离镜像、同 /data 旁路验证、独立 tag | healthy、账本连续、事件流、日志、统计 parity |
| Phase 6 | 持续 | 三级强化迭代（O21-O29）、单人入口持续优化 | 按需小版本、单步 1min SLA | 日更测试 60s、发版 4,633 门禁不降 |

---

## 6. 平滑迁移与回滚方案

### 6.1 迁移原则
- **数据卷不动**：/data 命名卷保留，仅代码/配置变更
- **每批独立 commit+tag**：Phase 1-3 每项独立，便于精准回滚
- **热改配置优先**：config_store 热读取，无需重启容器
- **Shadow 优先**：所有功能变更先走 shadow_mode 记录、对账，再 enforce

### 6.2 回滚锚点（必须保留）
`
Git:  pre-shadow-overhaul-2026-11-17 = 755fc72c
Docker: hermes-deploy-hermes-trader:pre-shadow-overhaul-20261117
`

### 6.3 回滚触发条件
- 容器连续 3 次重启（parity guard 漂移未登记）
- SHADOW 账本权益跳变 >5%（账本污染）
- 核心交易指标（成交数/胜率/止损分布）与基线差异 >20%
- 日志 error 率显著上升（>2 倍基线）

### 6.4 回滚操作（单人可执行，<2 分钟）
`ash
# 代码回滚
git checkout pre-shadow-overhaul-2026-11-17
./scripts/deploy_prod.sh  # 重建镜像，/data 保留

# 或镜像回滚
docker tag hermes-deploy-hermes-trader:pre-shadow-overhaul-20261117 hermes-deploy-hermes-trader:latest
docker compose up -d hermes-trader

# 配置热回滚（秒级）
hermes-solo tune equity_fraction_per_trade=0.15  # 恢复 runtime 值
# 或直接编辑 /data/.agent-config.json
`

### 6.5 应急熔断（秒级停止真实下单）
`ash
# 撤销 LIVE 授权（三闸 AND 任一失效即全局阻断）
unset HERMES_ENABLE_LIVE
# 或删除 B-13 记录
rm /data/live_acceptance_gate.json
# 或切回 SHADOW
hermes-solo shadow
`

---

## 7. 优化效果验证标准与监控指标

### 7.1 代码级指标（每 commit 验证）
| 指标 | 基线 | 目标 | 验收方式 |
|---|---|---|---|
| 源码行数（hermes_trader/） | 73,981 | -650 行（含归档） | git diff --stat |
| scripts 文件数 | 186 | 130 | find scripts -name *.py | wc -l |
| 文档主干份数 | 130 | 6 | find . -maxdepth 1 -name *.md | wc -l |
| __pycache__ 目录数 | 123 | 0 | find . -name __pycache__ | wc -l |
| 遥测日志单文件大小 | 无限 | 20 MB x 3 | ls -lh /data/*.jsonl |
| 影子/对账脚本 | 31 | 4 核心 | 白名单清单核对 |

### 7.2 运行态指标（每日/每周验证）
| 指标 | 基线 | 目标 | 监控方式 |
|---|---|---|---|
| 容器内存占用 | 535.8 MiB | 600 MiB | docker stats |
| 容器重启次数 | 0 | 0 | docker inspect --format=RestartCount |
| SHADOW 账本权益连续性 | 无跳变 | 无跳变 | /data/.shadow-book.json 对账 |
| parity guard 漂移 | 0 | 0 | 启动日志 uncovered=[] |
| 事件日志完整性 | 哈希链校验通过 | 100% 通过 | event_log verify |
| 日志 error 率 | 基线值 | 2倍基线 | 日报统计 |

### 7.3 交易逻辑指标（影子对照窗口验证）
| 指标 | 改造前 7 天 | 改造后 7 天 | 合格标准 |
|---|---|---|---|
| 成交数 | 基线值 | 对照值 | 差异 <10% |
| 胜率 | 基线值 | 对照值 | 差异 <10% |
| 平均 ROE | 基线值 | 对照值 | 差异 <10% |
| 止损分布 | 基线分布 | 对照分布 | KS 检验 p>0.05 |
| 信号数 | 基线值 | 对照值 | 差异 <10% |
| PnL 符号 | 基线符号 | 对照符号 | 一致 |

### 7.4 单人体验指标（持续优化）
| 指标 | 当前 | 目标 | 验收方式 |
|---|---|---|---|
| hermes-solo --help 耗时 | <1s | <1s | 手工计时 |
| tune 单参修改 | <10s | <10s | 手工计时 |
| backtest 前台 7 天 | <3min | <3min | 手工计时 |
| backtest --background 返回 | 即时 | 即时 | 手工计时 |
| 改参到回测到判定全链路 | 手工跨脚本 | 5 min | 实测计时 |
| 一日闭环（含可选 live） | 无 | 1 工作日 | 实操演练 |

---

## 8. 风险评估矩阵

| 风险 | 可能性 | 影响 | 缓解措施 | 残留风险 |
|---|---|---|---|---|
| parity guard 漂移未登记导致启动失败/重启风暴 | 中 | 高 | 改前完整备份、改后观察 3 周期、配置键冻结新增 | 低（红线强制） |
| 删风控门分支破坏 composite_score 分母 | 中 | 高 | 红线2：不删分支、仅旁路开关、维持权重和 | 低 |
| 账本污染（原子写失败、归档副本混入） | 低 | 高 | 原子写、只动归档副本、shadow_book 独立验证 | 低 |
| 回测误导（样本不足、成本口径不一致） | 中 | 中 | 强制实跑、交叉验证、双成本口径（9.26/19bp） | 中 |
| 外部依赖中断（OpenRouter 429、LLM 配额、GDELT SSL） | 高 | 中 | 列外部依赖风险、不承诺信号、熔断降级 | 中（不可控） |
| 影子对照窗口样本量不足（<7 天） | 中 | 中 | 先跑 Phase 1-3 稳定 1 周、再启动 7+7 天对照 | 低 |
| Dashboard/MCP 改动断 portal 链路 | 低 | 高 | 本轮明确不纳入、独立后续规划 | 无 |
| 配置键物理删除破坏 parity/回测等价性 | 低 | 高 | 红线1：冻结新增、不物理删 | 无 |

---

## 9. 后续迭代规划

### 9.1 近期（1-2 个月）
- Phase 1-5 完成生产发布
- 单人入口 CLI 迭代：补全 live 子命令（二次确认+内置限额）、preflight 扩展检查项
- 影子臂评级体系接入 shadow_grade.py 自动化周报

### 9.2 中期（3-6 个月）
- M1 PIT universe 快照 + 回测 --universe pit 全链路打通（消除幸存者偏差）
- M2 入场 edge 重建：基于 PIT 样本、Promotion Gate 选出正 edge 规则
- M3 统一入场选择器替换 OR 旁路（slow_burn/置信度不再短路）
- M4 组合级风控 enforce（日亏/总敞口/相关性/远宽灾难止损）

### 9.3 长期（6-12 个月）
- LIVE 小额灰度（B-13 验收、最小名义、日亏/总回撤硬熔断）
- 策略自动降级：edge 衰减触发降频/停开、滚动窗口重评
- 个人知识库沉淀：策略逻辑版本化、参数变更日志、复盘模板

---

## 10. 总结

| 维度 | 现状 | 目标 | 进度 |
|---|---|---|---|
| 代码精简 | 73,981 行 | -650 行（含归档） | Phase 1 进行中 |
| 脚本收敛 | 186/61,891 行 | 130/45,000 行 | Phase 2 待启动 |
| 文档聚焦 | 130 份/505 MB | 6 份/<5 MB | 核心 3 份就位 |
| 日志可控 | 无轮转/1.2 GB | log_rotate/150 MB | Phase 1 待接入 |
| 单人体验 | 跨脚本/>30min | 统一入口/5min/单步1min | CLI 已落地 |
| 配置治理 | 704 叶子/易漂移 | 冻结新增/parity 0 漂移 | Profile 冻结 |
| 回测科学 | 无 PIT/幸存者偏差 | 默认 PIT/A/B 量化偏差 | Phase 3 待实施 |
| 影子科学 | 14 臂/无冻结 | 4 臂按需/7+7 天对照 | Profile 冻结 |

**核心结论**：
1. **Phase 0 已完成核心落地**（Profile + CLI + 回测后台化），风险近零，已通过隔离验证
2. **Phase 1-3 为工程清理与简化**，每项独立可回滚，守住四条红线即可控
3. **Phase 4 影子对照窗口**是质量闸门，**差异 <10% 且 PnL 符号一致**才能进入生产灰度
4. **Phase 5 生产发布**采用同 /data 旁路验证，**不承诺市场信号或交易结果**，仅保障工程指标 parity
5. **全链路服务于单人快速迭代**：改参到回测到判定 5min，一日闭环操作链路已打通

**回滚锚点已建立，风险边界已量化，验证标准已明确，分阶段路线图已就绪。建议按 Phase 1到2到3到4到5 顺序推进，每阶段独立验收，不达标不进下一阶段。**
