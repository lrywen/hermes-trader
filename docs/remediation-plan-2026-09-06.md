# Hermes-Trader 震荡市改造与升级修复方案（Q1–Q3 + S0–S6 融合·总流程版）

> 落盘说明：本方案原仅存在于 2026-09-06 的 Agent 对话中、未存盘，跨会话上下文压缩后 Wave E/F 原文一度丢失；现据用户保存的会话转录原文原样落盘于此，作为后续波次与审计的唯一施工蓝图依据。内容未作改写。

> 本方案合并三条线：① 原整合版 P0–P2（Q1 新币 200MA 语义、Q2 regime 自动切换、Q3 pullback 止血）；② 全面复核新发现 S0–S6 / E1–E5 / 数据管道 / 工程面；③ 遗留 AZTEC 超时。
> **红线不变**：不改交易策略核心逻辑；新闸门一律先 SHADOW 探针后 enforce；改动后 py_compile + 实跑 pytest（禁编造）；Python 注释英文 + `Audit 2026-09-06` 标注；最小改动；质量门/fail-closed 不削弱；改动性操作逐项授权。

---

## 〇、总流程与波次依赖图

```
Wave 0 准备(备份/push基线/快照)
   └─► Wave A 安全止血(S0 凭据轮换)            〔独立，最先做〕
        └─► Wave B P0纯配置(B1风控 + B2退出)   〔热加载，分钟级回退〕
             └─► Wave C 风控/数据正确性修复(代码)
                  ├─ C1熔断fail-closed → C2 flatten抉择 → C3 leverage兜底
                  ├─ C8 universe TTL / C9 candle质量门   〔脏数据，震荡误信号之源〕
                  └─ C10超时预算体系(含AZTEC funding)
                       └─► Wave D Pathia功能移植(全部先探针)
                            └─► Wave E P2震荡市定制(regime overlay + pullback同源 + xs_reversal)
                                 └─► Wave F 工程卫生(可与各波并行/收尾)
```

**每道波次闸门（缺一不可）**：py_compile → pytest 全量实跑（基线只增不减）→ SHADOW 观察 → 结构化日志/指标确认 → 灰度 enforce。

---

## Wave 0：准备（无逻辑改动）

| 项 | 动作 | 回退 |
|---|---|---|
| 0-1 | 提交未归档改动：docs/CONFIG.md、tests/test_cleanup.py L1462-1495 回归用例 | git revert |
| 0-2 | **push 当前 commit `7791a9b`（refactor/audit-r11）到远端备份**（升级前必须有远端基线） | — |
| 0-3 | 打修复前 tag（如 `pre-remediation-20260906`） | git reset --hard <tag> |
| 0-4 | 快照生产 `/data/.agent-config.json`、`.dsl-state.json`、`.agent-memory.json`（带时间戳） | 配置分钟级回退 |
| 0-5 | SHADOW 模式冷启动验证（当前 mode=SHADOW，不带持仓跨版本） | — |

---

## Wave A：安全止血（最高优先，独立于交易逻辑）

| ID | 内容 | 类型 | 证据 |
|---|---|---|---|
| **S0a** | **飞书后台立即吊销/轮换**机器人 webhook+secret、app_id/app_secret（凭据已入库即视为泄露，改代码无用） | 运维操作 | scripts/push_optimization_feishu.py:36-37、scripts/push_feishu_app_card.py:45-47 |
| **S0b** | 两脚本改从环境变量读凭据、默认置空；test_notify_routing.py:75 假密钥改明显夹具值 | 代码 | 同上 + tests/test_notify_routing.py:75 |
| S0c | 评估 git 历史清理（filter-repo/BFG）或接受历史保留但凭据已废 | 运维决策 | — |

**验证**：轮换后实发一条测试卡片确认新凭据可达；grep 全仓无明文密钥。

---

## Wave B：P0 纯配置（改 /data/.agent-config.json，热加载，零代码风险）

> 分两批，均先备份；每批改后观察 1–2 个扫描周期日志。

### B1 风控收紧批（原 P0，**已剔除无效的 atr_stop 调参项**）

| # | 键 | 当前 → 建议 | 说明 |
|---|---|---|---|
| 1 | `counter_regime_min_conf` | 0.7 → **0.82** | 逆趋势门槛收紧 |
| 1b（可选更精准） | `chop_min_conf` | 0.75 → **0.82** | 震荡态直接收紧（Q2：chop 态门槛取 max 分层，已自动切换，调参即生效） |
| 2 | `loss_cooldown_min` | 90 → **180** | 恢复代码默认，亏损后冷却 |
| ~~3~~ | ~~atr_stop ceiling/mult~~ | **撤销** | **S5：regime cap 0.4/0.8% 恒小于 ATR floor 1.2%，min() 后 ATR 永不 bind，调参无效** |
| 3′ | `dsl_exit.atr_stop.enabled` | true → **false** | 让配置反映现实（止损宽度实由 regime 决定），消除死配置幻觉 |
| 4 | `daily_giveback_halt_pct` / `daily_giveback_min_peak_usd` | 0.35/3.0 → **0.30/2.0** | 回吐熔断提前 |
| 5 | `max_concurrent` | 4 → **2** | 震荡态降敞口 |
| 6 | `runner_entry_gate.allow_shorts` | true → **false** | 震荡市禁做空（Q2 点名项，先静态关，E2 再做自动切换） |
| 7 | `runner_entry_gate.pullback_long.shadow_mode` | false → **true** | **Q3 止血**：旁路先回影子验证，不实盘 |

### B2 退出/震荡市批（新发现 E 项，纯配置部分）

| # | 键 | 当前 → 建议 | 说明 / 证据 |
|---|---|---|---|
| 8 | `dsl_exit.retrace_threshold`（base/non-trend） | 0.20 → **0.12~0.15** | **E1**：Pathia 0.10，0.20 回吐容忍翻倍，震荡利润回吐主因。trend 态 0.55 保留 |
| 9 | `dsl_exit.stale_flat_timeout_minutes` | 480 → **240** | **E2**：死仓位 8h→4h |
| 9b | `dsl_exit.hard_timeout_minutes` | 1800 → **960** | **E2**：30h→16h |
| 10 | `dsl_exit.noise_band.enabled` | false → **true**（atr_mult 0.8~1.0） | **E4**：启用噪声带抑制区间反复扫 |
| 10b | `dsl_exit.breakeven_trigger_pct / breakeven_lock_pct` | 未配(0) → **1.5 / 0.3** | **E4**：浮盈 1.5% 后 floor 锁 +0.3% 保本 |
| 11 | 确认生产 phase2 tiers 已含低位档（生产述为 2.5/8/15/25） | 核对 | E1：1.25% 武装后尽快锁利；下发前 docker exec 读线上实际 dsl_exit 块核对 |

> 注：漂移 5 键（max_concurrent/max_total_notional_pct/max_daily_loss_usd/counter_regime_min_conf/loss_cooldown_min）生产配置中**均已显式存在**，无需补写；真正要修的是 canonical 注释/默认漂移，归 C7（代码）。

**回退**：配置快照分钟级还原。**验证**：热加载日志确认新值；SHADOW 下观察闸门拦截率/退出 reason 分布变化，无异常再保持。

---

## Wave C：风控与数据正确性修复（代码，先测试后灰度）

> 本波修复"防护失效/脏数据入场"，是震荡市反复误信号的根因层。每项独立可交付。

| ID | 内容 | 类型 | 关键点 / 证据 |
|---|---|---|---|
| **C1** | 五个 memory 熔断门 except 分支 fail-**OPEN → fail-CLOSED**（coin_circuit/global_halt/consecutive_loss/per_coin_daily_loss/drawdown），至少 error 日志+metrics | 代码 | risk_gates.py:317-486；与 spread/opposite 门对齐。**不削弱质量门，反而是补强** |
| **C2** | `auto_flatten_on_*` 二选一：**(推荐保守)** 默认改 false + 配置注释/dashboard 标注"未实现"，移除虚假安全感；或实现 halt 武装后按开关 market flatten | 决策+代码 | config_store.py:133-134；全仓无消费者。**需你拍板** |
| **C3** | `get_max_leverage` 调用包 try，未知币/meta 故障 fail-closed 拒单（实际 reason 串为 `unknown_max_leverage_<coin>`） | 代码小改 | executor.py:2946-2952。**已落地 + 2026-09-07 补专门单测**（tests/test_c3_max_leverage_fail_closed.py，4 用例：SHADOW/LIVE/任意异常 fail-closed + 正常 lookup 控制组），原 14 处 monkeypatch 均返回正常 int 未覆盖该分支 |
| **C4** | sizing：**先 sizing_v2 转 SHADOW 观察**（对齐真实 DSL 止损宽度），不直接 enforce；v1 宽度脱节问题记录台账 | 代码+观察 | executor.py:2902-3008（S6）。enforce 待数据 |
| **C5** | 修正 9 处 cfg_get 传参：`config={}` → 传真实 config；chop_min_conf 等不传 config → 传入（否则 per-coin override 不生效、analyst 阈值改了不生效） | 代码小改 | risk_gates.py:792、950-955、982、643-651 |
| **C6** | 0 值语义三方对齐：max_total_notional_pct / max_daily_loss_usd 的 schema sane-floor、gate 行为、config_store 注释统一（建议 0=显式 disable 且 gate 加短路） | 代码+注释 | config_schema.py:97/688-714；risk_gates.py:590-598；config_store.py:103-112 |
| **C7** | canonical 漂移订正：config_store.py 注释（"Production pins 4.0"等）改实际值；debate_gate 代码内 magic 默认(0.6/3)对齐 canonical | 代码注释 | config_store.py:96-102 |
| **C8** | **universe 缓存 24h → 60~120s**；缓存带 `fetched_at/age_s`；陈旧 OI/funding 降权；**禁止缓存 midPx 做 eligible 兜底**（live mid 缺失即剔除）；meta fetch 加 timeout+1~2 重试；whale_index OI 快照比较前校验 source age | 代码（**P0 脏数据**） | universe.py:63；perception.py:974-978；whale_index.py:262-343（S1） |
| **C9** | 坏 candle 序列**不写 90s 缓存**（或负标记短 TTL）；research L2251 加 quality 硬门（stale/gap 超阈 → data_degraded 跳过 TA）；fetch 返回带 quality 元数据 | 代码（**P0 脏数据**） | hl_client.py:567-577、587-644；research.py:2251-2257（S2） |
| **C10** | **超时预算体系（含 AZTEC）**：① funding 单次 POST 5s→3.5s + 加 1 次重试、总墙钟 cap 6s（或 env `HERMES_RESEARCH_FETCH_TIMEOUT_FUNDING=12`）；② 所有 future 超时后 `cancel()` + 线程池 `shutdown(wait=False)` 不等待悬挂；③ 内层重试墙钟硬 cap < 外层预算 70%；④ signals 4 源共享总 deadline、FINRA 日期并行；⑤ account HIP-3 纳入 prefetch/加 10s 总超时；⑥ `_http_post` 失败类型化，funding 失败标 `degraded` 而非 "N/A"；⑦ 修 `_Cache.in_flight` 防惊群死代码 | 代码 | research.py:250/2004-2043/587-594/2178-2208；hl_client.py:1106-1131/277-294；cache.py:22；perception.py:1195-1210（S1/S2 数据管道 + AZTEC） |
| C11 | 新增 `min_tradable_equity_usd` 硬底（equity < 阈值 fail-closed 不开仓） | 代码小改 | Pathia 有($12)，Hermes 缺（S 项） |
| C12 | `_CRYPTO_COINS` 40 币硬编码改 universe 动态/入 config；tiered notional cap 的 $50/1.5× 入 config | 代码 | risk_gates.py:569-574；executor.py:165-168 |
| C13 | portal run_scan 传 include_hip3（与 loop 一致）+ scan_once 包 try/except 返结构化错误 + 错误带阶段标签(prefetch.funding/signals.finra/llm) | 代码小改 | server.py:552-555 |

**C 波验证**：每项补单测（尤其 C1 熔断 fail-closed 用 mock 抛异常断言拦截、C8/C9 陈旧/坏数据断言跳过）；py_compile + 全量 pytest；灰度先在 SHADOW 容器跑。

---

## Wave D：Pathia 功能移植（代码，全部先 SHADOW 探针）

> 沿用 F1/F2 反事实探针框架（risk_gates.py:1208-1234），新闸门先挂探针统计"会拦多少单/其中多少本亏损"，再转 enforce。

| ID | 内容 | 关键点 |
|---|---|---|
| **D1** | **trend_filter_200ma**（含 **Q1 两态语义**）：老币 K 线抓取失败 → fail-OPEN + WARNING；**确为日线 <200 根新币 → fail-CLOSED 或加严门槛**，配合 `min_history_bars` 下限；`block_unknown` 不照搬 Pathia 的 false；mover 旁路锁当日涨幅 **10%~30%**（`allow_daily_mover_long_bypass`）。落点 risk_gates 新闸门，先探针 | Q1 融合；P1-1 |
| **D2** | `override_max_daily_extension_pct=30.0` 单日涨幅硬上限（与 D1 mover 旁路上沿对齐），追高封顶。**已落地，默认 SHADOW**；2026-09-07 补齐 `daily_extension_cap` canonical mode 块 + schema Field/叶子表（与 D1/D3 对齐，默认 shadow = gate 原硬编码 fallback，行为零变化，此前仅 env/env 可切、配置块无登记） | P1-2。risk_gates.py:1771-1836 |
| **D3** | `reentry_cap` {max_per_coin:2, window_hours:24}（落 memory/state；区别于现有 momentum_reentry 的 bypass 语义，且补 LONG-only 之外的次数上限） | P1-3 |
| D4 | ~~（评估）`override_volume_confirm`{min_ratio:1.2,lookback:20}；`thin_short_relax` 需先确认 Pathia 语义再定~~ **已落地（2026-09-07）**：volume_confirm 旋钮入 canonical/schema（默认 1.2/20=原硬编码，行为零变化，11 单测）；thin_short_relax 经裁定**不放松**（Pathia 语义不可核实 + 17x 流动性数据逆证据），见文末「策略语义决策记录」 | 可选，已闭合 |

**验证**：探针跑足够样本后用 reconcile 脚本回填 outcome，确认拦截正收益（拦亏损 > 误杀盈利）才 enforce。

---

## Wave E：P2 震荡市定制（代码，Q2/Q3 核心）

| ID | 内容 | 关键点 |
|---|---|---|
| **E1** | **regime_risk_overlay 自动降险总开关**（Q2）：新增配置块，chop 态自动 `max_concurrent→1 / allow_shorts→false / equity_fraction_mult→0.5 / pullback_long_enabled→false`，trend 态恢复。**必须含滞回防抖**（连续 N 根 chop 才降险、M 根 trend 才恢复，防 ADX 在 20 上下抖动翻转）；`shadow_mode:true` 先行；落点 executor 开仓/sizing 读已缓存 regime（detect_regime_with_score 已 cached）；结构化覆盖日志 | P2-1。示例块：`{enabled:true, shadow_mode:true, chop_adx_max:20, hysteresis_bars:3, chop:{max_concurrent:1,allow_shorts:false,equity_fraction_mult:0.5,pullback_long_enabled:false}, trend:{max_concurrent:4,allow_shorts:true,equity_fraction_mult:1.0}}` |
| **E2** | **pullback_long 宏观 regime 同源化**（Q3）：executor.py:4135 旁路条件加"宏观 regime 必须为 up"（当前 `uptrend` 来自 4h TA `uptrend_momentum_fired`，与 BTC/SP500 代理+EMA20/30+ADX 宏观 regime **不同源**，震荡市假金叉买在区间上沿） | P2-2，**可提前到 C 波后做**（最小改动、收益直接）。与 B1-7(shadow)、E1(overlay 关) 三步递进 |
| E2t | **补 pullback_long 行为单测**（当前仅配置登记测试，核心旁路 executor.py:4134-4157 无 pytest 覆盖） | 随 E2 一起 |
| E3 | 退出侧 regime 时钟：stale_flat/hard_timeout 进 `select_exit_params`（trend 长时钟 / scalp 短时钟）；新增**时间止盈/scratch 退出**（持仓 N 分钟未武装 phase2 且浮盈回落即离场，补 E3 漏洞） | P2，dsl_exit.py check() 加 verdict 通道 |
| E4 | 接线 `smooth_transition`（当前死旋钮，_build_policy_from_config/executor 构造未传） | P2，dsl_exit.py:1621 + executor.py:2037 |
| E5 | 状态韧性：`.dsl-state.json`/`.agent-memory.json` 损坏时隔离备份(.corrupt-ts)+飞书告警+metrics；双文件轮换(.bak)；rehydrate 用交易所 bracket SL 反推保守 floor（防棘轮归零）；userFills 时间解析失败用保守时间而非 now（防僵尸仓时钟重置）；`_tracker_from_dict` stale_flat 默认值对齐；executor.py:2037 补传 hard_stop_confirm_sec | P2，dsl_exit.py:1392-1448/1759-1775/1205；memory.py:516-537 |
| E6 | （可选战略级）**xs_reversal 均值回归 arm**（抄底 book：lookback_d 3/awake 7/awake_min_frac 0.67/top_pct 90），长期 SHADOW。**这是解决"系统本质只追涨、震荡市买高卖低"的结构性补臂**，但改动最大，最后做 | P2-3，2-3 天 |

**依赖**：E1 需 C5 先修（cfg_get 传参，否则 overlay 读不到 override）；E2 需 E2t 测试先行。

---

## Wave F：工程卫生（可并行/收尾，低风险）

| ID | 内容 | 证据 |
|---|---|---|
| F1 | deep_research MCP 工具：依赖目录 `~/HermesTradingAgents` 不存在则启动时不注册（移入 stub），消除必败工具 | hermes-mcp-server.py:163/1169-1198 |
| F2 | 7 个 shadow JSONL 抽公共写入器 + 按天/大小轮转 + 写失败 metrics（当前只追加无上限） | risk_gates.py:1059 等 7 处 |
| F3 | 补测试：test_market_regime.py 独立表驱动（三态/缓存/代理映射）；dsl-state 损坏容错用例 | 测试盲区 |
| F4 | 修热加载提示漂移（enable_hip3 已支持热翻转，config_preset.py:295 仍说需重启）；建 STARTUP_ONLY_KEYS 单一事实来源 | config_preset.py:295；trading_loop.py:593-602 |
| F5 | 删死代码：timeutil.py 孤儿垫片、14 个 .bak、scripts 约 40 个一次性脚本归档 archive/；修 3 处 F841；diag 脚本硬编码 /data 改 env | timeutil.py；cache.py in_flight（C10 已含） |
| F6 | orderbook l2_snapshot 加显式超时(3-5s)+短缓存；CBOE/FINRA 静默 except 补日志、负缓存 TTL 降到 60-120s；硬编码超时(Brave 10s/crosscheck 2.5s)入 canonical | exchange.py:818-861；options_gex.py:231 |

---

## 升级风险与回退（汇总）

1. **配置兼容**：新增键（trend_filter_200ma/reentry_cap/overlay/min_tradable_equity_usd 等）旧代码忽略无害；**禁止整体覆盖 Pathia 作者 $12.94 小账户配置**（max_concurrent 1/max_daily_loss -4 等绝对值不可照搬）；AI provider 若升级需重设 openrouter+base_url（Pathia 用 claude_cli）。
2. **状态迁移**：.dsl-state/.agent-memory 同源可识别，但建议 SHADOW 冷启动、不带持仓跨版本；E5 落地前损坏即棘轮归零。
3. **fail-closed 方向**：C1（熔断）、C3（leverage）、C8/C9（脏数据）、C11（equity 硬底）均为**补强**质量门，符合"不削弱 fail-closed"红线；C10 的超时收紧需防止过度 fail-CLOSED（funding 加重试 + 总 cap 而非单纯调大）。
4. **回退方案**：git tag 回退代码；配置快照分钟级还原；P1/P2 全部 feature 开关 + shadow_mode；独立容器 + 独立数据目录 SHADOW 并行验证，**禁止同数据目录原地切换**。
5. **删除模块**：Pathia 删的 v2/prediction-market/arb/strategies 在本地零残留；本地 arb 两处（risk_gates.py:571 ARB 代币、research.py:1417-1678 arbiter）**活跃，禁止误删**。

---

## 建议执行顺序与待授权决策点

**推荐顺序**：Wave 0 → **A（S0 凭据，立即）** → B1 → B2 → C1 → C8/C9（脏数据）→ C10（含 AZTEC）→ C3/C5/C11 → E2+E2t（pullback 同源，小而快）→ D1（200MA 探针）→ D2/D3 → E1（overlay）→ E3/E5 → E6（xs_reversal，最后）；F 波穿插。

**需你拍板的 6 个决策点**：
1. **S0**：是否立即吊销/轮换飞书两组凭据？（最优先）
2. **C2**：auto_flatten 是"改默认 false+标注未实现"（保守，推荐）还是"实现 halt 后真平仓"（功能增强）？
3. **B 波配置**：B1（8 项，含 atr_stop.enabled→false）+ B2（退出 4 项）是否授权？可分批。
4. **C4 sizing_v2**：同意先 SHADOW 观察、不直接 enforce 吗？
5. **E6 xs_reversal**：均值回归臂是否纳入本轮（战略级、改动最大），还是留待下轮？
6. **AZTEC/C10 funding**：用"单次 3.5s + 1 重试 + 总 cap 6s"（推荐，根治预算倒挂）还是简单 env `HERMES_RESEARCH_FETCH_TIMEOUT_FUNDING=12`？

---

## 决策点处置记录（2026-09-06 复核，全部闭合）

> 复核结论：6 个决策点中 **4 个前提已失效**（代码/配置在前序波次或运维中早已落地），仅 B 波 2 个可选项执行了生产改动。

1. **S0**：S0b 代码侧前序已完成（notify.py 全 env 多通道、push_* 脚本 env 化、测试假夹具、全仓无明文）。S0a 凭据轮换为**用户人工运维操作**——3 组泄露凭据（webhook hook/55e07104…+签名 secret、app cli_aa02713fdaf8dcfd 的 app_secret）在 git 历史 6e6cae9/9f61de6 且已推送远端 lrywen 3 分支，需在飞书后台吊销重建，新凭据经 env 注入，轮换后用 `surge_postmortem --test-feishu` 验证。**S0c 用户拍板：轮换即可，不清理 git 历史**（轮换后旧值作废，filter-repo/force-push 风险不必要）。
2. **C2**：**前提失效，已闭合**。auto_flatten_on_global_halt / auto_flatten_on_coin_circuit 早已完整实现且默认 ON——trading_loop.py:778-828 真实 close_position_market 平仓、market_circuit_tick L897-904 同 tick 硬平仓；config_store.py:133-134 默认 True；test_audit_p1/test_market_circuit 覆盖；L766-769 已有 C2 审计标注。方案"全仓无消费者"系过时判断。
3. **B 波配置**：生产 /data/.agent-config.json 核对，B1 八项中 7 项 + B2 五项**均已在线上生效**（counter_regime 0.82、loss_cooldown 180、giveback 0.30/2.0、max_concurrent 2、allow_shorts false、pullback_long shadow true、atr_stop.enabled false、stale_flat 240、hard_timeout 600[比建议 960 更紧]、noise_band on atr_mult 0.8、breakeven 2.5/0.3、phase2_tiers 已含 2% 低位档）。**用户授权后执行仅剩 2 项微调**（备份 .agent-config.json.bak-bwave-*，热加载）：chop_min_conf 0.75→**0.82**；dsl_exit.retrace_threshold（base/scalar，phase2 武装前 + chop 态）0.35→**0.15**。regime_aware trend_ride（retrace 0.4、tiers 0.4/0.38/0.35/0.3）与 base phase2_tiers（2% 档 0.35）未动。

   **【热加载观察裁定 · 2026-09-06，写入后约 6.8h / 数千扫描周期】** 两项值均经权威写路径（容器内 `update_agent_config(backup=True)`，禁宿主侧直改 bind-mount 单文件）落盘，`(mtime_ns,size)` 缓存自动拾取、容器未重启；最近周期连续 `0 errors / 0 data-gaps`、无 schema 报错。逐项裁定：
   - **chop_min_conf 0.75→0.82 为行为 no-op（重要）**：risk_gates.py:688-689 真实门限是 `chop_min_conf = max(counter_regime_min_conf, cfg_get("chop_min_conf"))`，而改动前生产 `counter_regime_min_conf` 早已是 **0.82**（.bak 备份佐证），故改动前 `max(0.82,0.75)=0.82`、改动后 `max(0.82,0.82)=0.82`——拦截门限始终 0.82，未变。日志佐证：写入前（12:40 PURR / 16:30 kBONK / 16:42 UNI）与写入后（18:27 TAO / 19:15 LIT）chop 拦截信息**均显示 `conf >= 0.82`**。此项仅把配置值对齐到早已生效的实际门限（消除误导性 0.75），**若策略本意是把震荡开仓门槛提到高于 0.82，必须调高 `counter_regime_min_conf`（真正的绑定项），单改 chop_min_conf 会被 max() 短路**。待决策。
   - **retrace_threshold 0.35→0.15 为真实收紧、代码层已生效**：容器内实测 `select_exit_params`：chop/neutral（scalp）→ retrace=**0.150**、protect=1.50、maxloss=0.40；up/down（trend_ride）→ 0.400（未动）；policy 缓存 TTL 仅 5s（dsl_exit.py:95）。但写后 events.jsonl 当日 497 条事件全为 execute/risk_gate、**无 entry/open/fill、无 close/dsl_exit**（上一次真实退出停在 2026-08-29），即当前空仓，0.15 的更紧 trailing 回吐尚未在真实持仓触发，退出 reason 分布暂无样本。
   - **后续观察**：待出现首笔 chop/neutral 持仓的 trailing 退出，核对退出 reason 与收益，确认 0.15 未因过紧提前扫损；chop 态武装后 phase2 最低档 0.35 仍偏宽，留待退出 reason 数据后再评估。
4. **C4 sizing_v2**：**已闭合且线上在运行**。executor.py:1080-1141 off/shadow/enforce 三态框架完整（env HERMES_SIZING_V2_MODE 优先），生产 .env.local:115 已设 `HERMES_SIZING_V2_MODE=shadow`，容器内 env 确认为 shadow，/data/sizing_v2_shadow.jsonl 持续写入（实测 notional_ratio 2.5x，与 v1 低估风险结论一致）。enforce 待数据，现阶段勿开。
5. **E6 xs_reversal**：**用户拍板本轮不做、留待下轮**单独立项（先出落点设计：闸门/state/配置块/探针，长期 SHADOW）。全仓零代码。
6. **AZTEC/C10**：**前提失效，已闭合**。research.py:435-511 已按推荐方案落地：专用 funding 线程池、单次 3.5s 硬 cap、1 次重试、总墙钟 cap 6s、超时 cancel()、耗尽返回显式 degraded marker（"funding unavailable … not 'N/A' zero-rate"）；env HERMES_RESEARCH_FETCH_TIMEOUT_FUNDING 亦可覆盖（fetch_timeout_funding_sec=8.0）。

**闸门**：本轮无代码改动，全量 pytest 实跑 **2900 passed / 14 deselected**（基线保持）。

---

## 策略语义决策记录（2026-09-07，用户拍板"直接按经验调参"）

> 针对遗留两项策略语义决策（chop 门限是否提至 >0.82、D4 thin_short_relax 是否放松），用户选择直接按经验调参而非先上 outcome 探针。三项改动**均为生产配置层**，经容器内权威写路径 `update_agent_config(backup=True)` 落盘（自动 `.bak` 备份、schema 校验通过、`(mtime_ns,size)` 热加载自动拾取、容器未重启），代码仓库零改动、不 commit。

| 键 | 旧值 → 新值 | 依据 / 作用 |
|---|---|---|
| `chop_min_conf` | 0.82 → **0.85** | 震荡态 conviction 门槛。risk_gates.py 真实门限 `chop_min_conf = max(counter_regime_min_conf, cfg_get("chop_min_conf"))`，故单抬 chop_min_conf 到 0.85 即让 chop 态门槛升到 0.85，而 `counter_regime_min_conf` 保持 0.82，**趋势市逆趋势门槛不受影响**（精准打击震荡态，非全局收紧）。 |
| `chop_min_score` | 55 → **60** | score 是与 conf 并列的放行通道（`conf>=门限 OR score>=门限`）。只抬 conf 不抬 score，0.82–0.85 的单仍会从 `score>=55` 侧漏过，收紧形同虚设；抬到 60 与 against_funding 门槛对齐，chop 收紧才真正生效。 |
| `min_short_volume_usd` | 5,000,000（5M） → **50,000,000（50M）** | **意外发现的真问题（护栏漂移）**：生产值 5M 远低于 canonical 默认与数据结论 50M，导致 short_liquidity_floor（risk_gates.py:273-288，做空 24h 量低于地板即 fail-closed 硬封）形同虚设——亏损做空单中位量 ~$13M（13M ≥ 5M）全部漏过地板。修回 50M 后，5–50M 区间 thin short（14 笔止损 short 所在区间）被正确拦截。此为**收紧/fail-closed 方向**，与 17x 流动性数据（亏损 short ~$13M vs 盈利 ~$223M）一致。仅作用 short 侧，long 仍走 `min_market_volume_usd=5M`。 |

**thin_short_relax 裁定：不放松（关闭该项）**。`short_liquidity_floor` 是有 17x 流动性数据支撑的硬护栏，放松方向逆证据（等于放行挤压亏损单）；且 Pathia 为外部系统、本地无源码/文档，`thin_short_relax` 语义无法核实——全仓 grep 该键零实现，仅方案 D4 行的悬空占位。本次反而发现现有地板因漂移值 5M 未生效，已修正为 50M（收紧）。D4 代码侧旋钮（volume_confirm{min_ratio:1.2,lookback:20}）已落地并通过测试，thin_short_relax 不再推进。

**写入校验**：BEFORE/AFTER/REREAD 三读一致；`.bak` 备份生成（15570→15632 bytes）；`_validate_or_raise` schema 校验未报错。当前 **SHADOW 模式、0 持仓、权益 ~$30.9**，调整在影子窗口内生效。

**回退（分钟级）**：
```
docker exec hermes-trader python3 -c "from hermes_trader.agents.config_store import restore_backup; print(restore_backup())"
```
或逐键改回 `chop_min_conf=0.82 / chop_min_score=55 / min_short_volume_usd=5000000`。

**后续观察**：
- trading-loop.log 中 chop 拦截信息应从 `conf >= 0.82 or score >= 55` 变为 `conf >= 0.85 or score >= 60`；short 拦截应出现 `short on thin market … < short floor $50M`。
- 若 0.85/60 导致震荡态几乎零信号（误杀过多），**优先回退 score（60→55）而非 conf**——score 侧放宽对门限刚性影响更小。
- 缺 outcome 回填（被拦单若放行的盈亏），本次为经验调参；待 E6 同批的 shadow outcome 探针立项后，可用数据复核 0.85/60/50M 是否最优。

---

## 全量剩余项复核记录（2026-09-07，用户指令"检查剩余/验证生效/按优先级继续，不得遗漏"）

> 方法：三项并行只读核查（特征标识符全仓 grep + 关键文件逐行复核接线点），对 Wave C/D/E/F 逐项验证"非孤儿模块、真实接线、默认惰性"。

**逐项复核结论（均为已真实接线生效，非占位）**：

| 项 | 状态 | 关键证据 |
|---|---|---|
| C3 leverage fail-closed | 代码早已落地（executor.py:2946-2952，reason=`unknown_max_leverage_<coin>`），**本次前无专门测试**（14 处 monkeypatch 全返回正常 int）。**已补 4 用例** | tests/test_c3_max_leverage_fail_closed.py |
| C12 硬编码入 config | 已生效：`correlation_crypto_coins` canonical+schema+eval_all_gates 解析（risk_gates.py:649 pool fallback）；tier cap 参数化（executor.py:172-201/2962-2969） | tests/test_c11_c12_config_tuning.py |
| D1 trend_filter_200ma | 已接线，canonical mode=**off**（零网络直到翻转），shadow JSONL + env 覆盖 | risk_gates.py:1563/2074；config_store.py:990 |
| D2 daily_extension_cap | 已接线，默认 **shadow**（只记录不拦）。**本次补 canonical mode 块 + schema 登记**（原仅 env 可切、配置块无登记；gate 硬编码 fallback 与新默认值一致，行为零变化） | risk_gates.py:1771-1836 |
| D3 reentry_cap | 已接线，canonical mode=**off**，memory 计数 + shadow | risk_gates.py:1839/2076；memory.py:1805 |
| E1 regime_risk_overlay | 三处真实接线（max_concurrent / equity_fraction_mult clamp 乘 trade_notional / allow_shorts+pullback_long），enabled=False + shadow_mode=True **双重关闭**，snap.applied 恒 False | regime_overlay.py 全模块；executor.py:3195-3204/4095-4108 |
| E2 + E2t pullback 宏观同源 | 已接线（require_macro_uptrend 默认 True，lookup 异常 fail-closed pb_macro_up=False，与 4h TA uptrend AND 并存非替换）+ 8 行为测试 | executor.py:4304-4336；tests/test_e2_pullback_macro_regime.py |
| E3 regime 时钟 + time_scratch | resolve_regime_clocks 接线（enabled=False 回退全局时钟）；time_scratch 退出 reason 通道接线，canonical enabled=False | executor.py:759-788；dsl_exit.py:819-841；14 测试 |
| E4 smooth_transition | 端到端接线（构造→rehydrate→_smooth_phase2_floor 消费），enabled=False 时逐字节一致返回原 trail | dsl_exit.py:709-730；6+既有测试 |
| E5 状态韧性 | 八项全部落地：损坏隔离(.corrupt-<ms>)+飞书+metrics 三独立 try/except；.bak 双文件轮换；bracket SL 反推保守 floor；fill 时间失败保守定龄 now-(hard_timeout+60s)；stale_flat 默认 480；hard_stop_confirm_sec 补传 | dsl_exit.py:1365/315/149-226；memory.py:33；tests/test_e5_state_resilience.py |
| F1 deep_research 条件注册 | 目录缺失即不注册工具，handler 保留结构化错误 | hermes-mcp-server.py:931-953 |
| F3 测试补强 | test_market_regime.py 26 测试（三态边界/ADX/BTC 代理/缓存 TTL/22 外国指数）+ E5 损坏用例 | — |
| F4 热加载提示 + STARTUP_ONLY_KEYS | STARTUP_ONLY_KEYS 刻意空集（所有键已热加载），enable_hip3 翻转每周期 force_refresh 重建 universe、"需重启"提示已消除 | config_store.py:1447；trading_loop.py:1642-1674 |

**本次改动（仅测试 + 配置登记，零策略逻辑变更、不 commit）**：
1. 新增 `tests/test_c3_max_leverage_fail_closed.py`（C3 fail-closed 分支唯一测试缺口）。
2. config_store.py 新增 `daily_extension_cap` canonical 块（mode="shadow"）；config_schema.py 新增 Field + 叶子表（mode enum + shadow_log_path）；test_wave_d_pathia_gates.py 补 canonical/schema 断言。
3. 全量闸门实跑：**2927 passed / 14 deselected**（基线 2923 净增 4，退出码 0）；py_compile 全绿。

**仍待数据/运维（非代码缺口，不属本轮可落地项）**：
- D1/D3/E1/E3(clocks+scratch)/E4 默认 off、D2/E2 shadow——均需 shadow 样本积累 + reconcile outcome 回填证明"拦亏损 > 误杀盈利"后才转 enforce，与 E6（xs_reversal，用户已拍板下轮立项）同批。
- C4 sizing_v2 生产 shadow 持续运行中，enforce 待数据。
- S0a 凭据轮换为用户飞书后台人工操作（用户指示先不管）。
- 生产经验调参三键（chop_min_conf 0.85 / chop_min_score 60 / min_short_volume_usd 50M）观察拦截日志变化，误杀时优先回退 score。

---

## E6 xs_reversal 落点设计（2026-09-07 立项·设计稿，用户拍板"先出落点设计"）

> 目标：为"系统本质只追涨、震荡市买高卖低"补结构性第二臂——超额下杀后的均值回归（抄底）信号。**长期 SHADOW**，先用 shadow outcome 数据证明边缘，再谈 enforce。本稿为纯设计，零代码；Pathia 源码本地不存在，四参数语义为本设计定义，**必须先离线回测钉死再写探针**。

### 1. 形态与挂接（候选侧独立异步臂，仿 shadow_signals）

- **新建** `hermes_trader/agents/xs_reversal.py`，纯 TA 计算（K 线只读，TTL 缓存复用现有 candle 源），**无 LLM、无交易副作用**。
- **挂接点**：`maybe_execute` 内 shadow_signals 派发点同构位置（executor.py:2493-2504 之后），`mode in (shadow, enforce)` 时 fire-and-forget daemon 线程调 `run_xs_reversal_async(coin, side, config=config)`——热路径零延迟、零网络放大（K 线走缓存，cache miss 时该周期跳过而非现拉）。全部异常 try/except 吞掉，永不阻断热路径。
- **覆盖面**：每枚已入场候选币评估一次（与 shadow_signals 同生命周期），不做全市场扫描（避免 API 放大教训）；如需 universe 级扫描，后续单独设计节流批处理，本稿不含。
- **方向**：仅 LONG 抄底（系统缺的是下跌后的多头臂；SHORT 顶背离不在本项范围）。
- **enforce 路径预留**（本稿不实现）：未来若证明边缘，enforce 形态同构 pullback_long——影子记录放行/拦截位在 runner_entry_gate（executor.py:4285-4352 同构），而非候选侧；shadow→enforce 翻转不需要重写信号本体。

### 2. 信号语义（四参数定义，⚠️ 待回测钉死）

数据：复用现有 K 线源（1h K 线，与 perception/regime 同源缓存）。

| 参数 | Pathia 原值 | 本设计定义（Hermes 语义，待回测确认） |
|---|---|---|
| `lookback_d` | 3 | 超额下杀的度量窗口 = 3 个自然日（72 根 1h K）。`ext_pct = (close_now / max(high, 72h) - 1) * 100`（距 3 日高点的回撤幅度，负值）。 |
| `top_pct` | 90 | **极端度分位**：`ext_pct` 在该币自身近 N 天（建议 N=90d 滚动样本）3 日回撤分布中的分位 ≤ 10%（即"跌得比自身历史 90% 的时候都狠"，xs = eXceSs）。用分位而非固定跌幅，适配各币波动率差异。 |
| `awake` | 7 | 流动性/活跃度确认窗口 = 近 7 根 1h K。 |
| `awake_min_frac` | 0.67 | 近 7 根 K 中"活跃 K"（成交量 > 该币 20d 成交量中位数 **或** 振幅 > 中位数）的占比 ≥ 0.67（即 ≥5/7）。过滤阴跌无量的流动性陷阱/僵尸币。 |

**触发条件（全部满足才记一条 shadow 候选）**：
1. ext_pct 处于自身 90 分位极端区（`top_pct`）；
2. awake 活跃度 ≥ 0.67（`awake`/`awake_min_frac`）；
3. **regime 门控**（防接飞刀，复用现成件不新写）：宏观 `detect_regime_with_score(coin)` ∈ {chop, neutral} 才记录；`down`（强下跌趋势）跳过——MR 在趋势市里是逆势；
4. **可选质量过滤**（回测决定开关，参照 archive/scripts/backtest_chop_mr.py:224-228 的 Plan D 结论）：EMA8 > EMA21（上升趋势中的回调）边缘最好；强下跌趋势中 EMA8 < EMA21 时不抄。回测对比"裸 xs 极端度" vs "xs + EMA8>EMA21" vs "xs + RSI(14) ∈ [rsi_floor, rsi_long]"三组的胜率/期望后钉死。

### 3. 配置块（canonical + schema 双登记，默认 off 最保守）

config_store.py CANONICAL_DEFAULTS 新增（与 D1/D2/D3 同构）：

```python
"xs_reversal": {
    "mode": "off",            # off | shadow | enforce；默认 off（探针未验证前零行为）
    "shadow_log_path": "",    # 空 = ~/.hermes-trading/xs_reversal_shadow.jsonl
    "lookback_d": 3,
    "top_pct": 90,            # ext_pct 分位阈值（>= 自身 90 分位极端才触发）
    "awake_bars": 7,
    "awake_min_frac": 0.67,
    "regime_allow": ["chop", "neutral"],   # 强下跌趋势不抄
    "require_bullish_ema": False,          # 回测后决定是否默认开
    "rsi_long": 35.0, "rsi_floor": 15.0,   # 回测后决定是否启用 RSI 夹逼
},
```

config_schema.py：Field(default_factory=lambda: _dict_default("xs_reversal")) + 叶子表（mode enum、shadow_log_path str、四个数值叶子范围校验：lookback_d 1-30、top_pct 50-99、awake_bars 1-48、awake_min_frac 0-1、rsi 0-100）。env 覆盖惯例 `HERMES_CFG_XS_REVERSAL_*` + mode 专用 `HERMES_XS_REVERSAL_MODE`（与 D1-D3 env 惯例一致）。生产翻转走容器内 update_agent_config 权威写路径。

### 4. state：无持久化持仓状态

探针纯只读、无持仓，**不需要 .dsl-state.json 一类持久状态**；仅进程内 TTL 缓存（ext 分位/awake 计算结果，建议 30-60min TTL，与 regime 缓存同量级）。重启无恢复负担。enforce 化时若成为真实入场臂，仓位/退出复用既有 DSL/闸门体系，届时再补 state 设计。

### 5. shadow JSONL 记录 schema（outcome 回填位仿 pullback）

写盘统一走 shadow_log.append_jsonl(path, rec, stream="xs_reversal")（轮转 + 写失败 metric + never raises）。每条记录：

```json
{
  "timestamp": "...Z", "coin": "...", "side": "long",
  "entry_px": 0.0,              // 记录时刻价，reconcile 基准
  "ext_pct": -12.3,             // 距 lookback_d 高点回撤
  "ext_percentile": 0.04,       // 该 ext_pct 在自身分布的分位
  "awake_frac": 0.71,           // 近 awake_bars 活跃占比
  "macro_regime": "chop",       // regime 门控标签（分 regime 评估）
  "ema8_gt_ema21": true, "rsi14": 28.5,  // 质量过滤快照
  "outcome": null, "exit_px": null, "pnl_usd": null  // reconcile 回填
}
```

### 6. reconcile（离线，post-run）

仿 pullback 范式：脚本按 coin 用后续 1h K 线回排每条记录——固定持有窗口（建议 24h/72h/7d 三档分别统计）或回测脚本同款 target/stop/timeout 退出，回填 outcome/exit_px/pnl_usd。**转 enforce 判据**（与 D1/D3/E1 同批口径）：chop/neutral regime 下样本量 ≥ 约定阈值后，期望收益为正且"盈利单均幅 > 亏损单均幅 × 盈亏比门槛"、最大回撤可接受；分 regime、分"裸 xs / +EMA / +RSI"三组对比，用数据决定质量过滤默认值。**未达标则永久停留 shadow 或下线。**

### 7. 测试计划（test_xs_reversal_shadow.py，新文件）

- 纯函数单测：ext_pct/percentile 计算（含数据不足→不触发 fail-open 语义）、awake_frac 边界（5/7、4/7）、regime 门控（down 跳过/chop 记录/unknown 保守跳过还是记录——回测前暂定 unknown 不记录）；
- 三态：off 零写入零线程；shadow 写 JSONL 且不触达任何下单函数（place_hl_order 替换为 pytest.fail，同 C3 测试范式）；enforce 路径本稿不实现，mode=enforce 在本阶段行为同 shadow（记录不放行）并在日志标注"enforce not implemented, recording only"——**禁止半实现的 enforce**；
- 热路径安全：worker 异常不外抛、cache miss 跳过不现拉网络；
- 配置旋钮：canonical sentinel、默认参=硬编码值、cfg_get/env 覆盖、schema 越界拒绝（仿 test_c11_c12_config_tuning.py 范式）。
- 全量闸门基线只增不减；英文注释 + `Audit 2026-09-07 (E6)` 标注。

### 8. 里程碑（按序，每步独立闸门）

1. **M1 离线回测（无生产代码）**：在 archive/scripts/backtest_chop_mr.py 同款框架上，用历史 1h K 线跑 xs 四参数 + 三组质量过滤的网格，产出胜率/期望/分 regime 表，钉死默认参与 top_pct/质量过滤开关；
2. **M2 探针实现**：xs_reversal.py + 配置双登记 + 测试（本稿第 1/3/5/7 节），默认 **off**；
3. **M3 生产 shadow**：容器内 update_agent_config 翻 shadow，采数 ≥ 样本阈值；
4. **M4 reconcile + 决策**：outcome 回填，达标才设计 enforce（届时另立 state/仓位设计），不达标维持 shadow 或下线。

**红线复核**：不改交易策略核心逻辑（shadow 臂只读不交易）；默认 off inert；写盘/线程/网络全部 try/except 不阻断；不 commit 待授权；M2 之前生产零行为变化。

### 9. M1 离线回测结果（2026-09-07 实跑，⚠️ 原设计 regime 门控假设被证伪，语义转向待拍板）

**脚本/数据**：archive/scripts/backtest_xs_reversal.py（M1 离线脚本，不碰生产代码）。universe top-20 by dayNtlVlm，18 币有效（PONS/CASHCAT 因 K 线覆盖率质量门被跳过）；每币 4501 根 1h K ≈ 188 天；次根 bar 开盘入场、固定持有 24/72/168h 收盘退出、5bps round-trip 费、单币同时一仓（信号后 168h cooldown）；base notional ≈ $400（equity $200 × fraction 0.20 × lev 12，read_agent_config 实读）。扫描层**不做硬门控、全量记录**，报告层按方向感知三 gate 分组：`chop_neutral`（CHOP/NEUTRAL）/ `trend_bull`（TREND 类 + EMA8>EMA21）/ `trend_bear`（TREND 类 + EMA8<EMA21）。

**核心发现 1——regime 门控假设证伪**：canonical `regime_strength_score` 是**方向无关**的（market_regime.py:163 docstring 明确 "Direction-agnostic"），强下跌趋势同样打 TREND/STRONG_TREND 高分。实测极端 3 日回撤（tail）事件约 90% 落在 TREND/STRONG_TREND 标签下（BTC 90/97、ETH 109/115、SOL 107/135），与原设计"仅 chop/neutral 门控"近乎互斥——188 天 18 币样本中 chop_neutral 门仅 7 个信号。

**核心发现 2——边缘不在震荡组，在下跌趋势 + RSI 超卖确认组**（top_pct=85，共 101 信号：chop_neutral 7 / trend_bull 11 / trend_bear 83-84；avg 为费后毛收益均值）：

| gate / 过滤 | n | 24h WR / avg | 72h WR / avg | 168h WR / avg | 最大连亏 24/72/168 |
|---|---|---|---|---|---|
| chop_neutral bare（原设计组） | 7 | 57.1% / **-1.97%** | 42.9% / **-1.72%** | 57.1% / **-1.28%** | — |
| trend_bull bare | 11 | 27.3% / -2.12% | 45.5% / +0.13% | 36.4% / -1.43% | — |
| trend_bear bare（裸接飞刀） | 84 | 51.2% / -0.72% | 51.2% / +0.80% | 51.2% / +3.25% | 7 / 4 / 5 |
| **trend_bear + RSI(14)∈[15,35)** | **49** | **67.3% / +0.63%** | **61.2% / +2.25%** | **59.2% / +5.02%** | 4 / 3 / 4 |
| trend_bear + RSI(14)∈[20,35) | 46 | 69.6% / +0.83% | 63.0% / +2.70% | 60.9% / +5.73% | 4 / 3 / 3 |

- **top_pct 网格**：tp=90（40 信号）trend_bear+RSI 组 n=8，72h WR75% +10.47%、168h +16.72%——同向但样本过小；chop_neutral n=3 全负。tp=95（21 信号）RSI 组 n=2，无统计意义。**tp=85 为样本量与极端度的最优平衡**。
- **RSI 阈值敏感性**（tp=85，trend_bear）：rsi_long=30 太紧（n=16-17，[10,30) 72h 仅 +0.45%，边缘不稳）；**rsi_long=35 为甜点**（n=46-49，72h +2.25~2.70%、168h +5.0~5.7%）；rsi_long=40 稀释（n=63-66，72h +1.36~1.65%）。rsi_floor=10 与 15 结果**完全相同**（[10,15) 区间零样本，floor 实际不绑定，仅为防自由落体护栏）；floor=20 风险调整略优但少 3 样本。
- **EMA 过滤无独立信息量**：trend_bear gate 本身即 EMA8<EMA21，"+EMA8>EMA21"在该 gate 下不可能触发；trend_bull gate 下 ema 组与 bare 同值（gate 已隐含 bullish）。EMA 质量过滤旋钮被 gate 方向吸收，M2 不再单列。

**信号画像变化（⚠️ 需拍板）**：若按数据转向，xs_reversal 性质从原设计的"震荡市均值回归"变为"**下跌趋势中的超卖反弹**"——xs 百分位是触发层、RSI 超卖是质量层；边缘主要在 72h-168h（多日摆动反弹），24h 仅微正；最大连亏 3-4。频率约 46-49 笔 / 18 币 / 188 天 ≈ 0.15 笔/币/月，M3 shadow 积样本较慢（与"长期 SHADOW"定位相容）。

**M1 建议默认参数（待拍板后 M2 定稿）**：lookback_d=3 / awake_bars=7 / awake_min_frac=0.67 维持；**top_pct=85**（90+ 样本腰斩）；门控从 `regime_allow=["chop","neutral"]` 转向**方向 + RSI 确认**：EMA8<EMA21（下跌趋势）且 RSI(14) ∈ [15, 35) 才记录 shadow（rsi_long=35、rsi_floor=15 护栏不绑定，可选 20）；取消 require_bullish_ema 旋钮。lookback_d/awake_min_frac 敏感性本轮未跑（边缘由 gate/RSI 主导，证据已足；M2 后如需可补）。

**待用户拍板（M2 入门闸）**：门控语义是否按数据转向？①**按数据转向**（下跌趋势 + RSI 超卖确认，M2 按新语义实现探针，推荐）；②维持原 chop/neutral 设计仅 shadow（数据已示全 horizon 负期望，大概率永久 shadow）；③暂缓 M2，补 lookback/awake 敏感性或扩样本再定。

> **拍板记录（2026-09-07）**：用户选定 **①按数据转向**。M2 探针按新语义实现——xs 百分位触发（top_pct=85）+ awake 活跃度 + EMA8<EMA21（下跌趋势）+ RSI(14)∈[15,35) 超卖确认才记 shadow 候选；regime 标签/RSI/EMA 快照仍全量写入 JSONL 供 M4 分组复核；取消 require_bullish_ema 与 regime_allow=[chop,neutral] 旋钮，改为方向 + RSI 门控（rsi_long=35、rsi_floor=15 护栏）。lookback_d=3 / awake_bars=7 / awake_min_frac=0.67 维持原设计值。

---

## C1 熔断门 fail-open 复核与飞书告警上线（2026-09-07，用户拍板"维持 fail-open + 接飞书告警"）

> 背景：C1 原方案（本文第 94 行）规划五个 memory 熔断门 except 分支由 fail-OPEN 改 fail-CLOSED。复核部署事实后发现该规划与既有有意决策冲突，故单独立项复核，用户拍板**不改 fail-open 姿态，改为补强可观测性**。

### 事实链（代码实测，非推测）

- **fail-open 是有意回退而非缺陷**：2026-09-06 曾短暂改 fail-closed，因共享 memory 瞬态异常会导致 fail-closed **同时 disarm 全部闸门 = 静默全局停摆**（比单门变盲更危险），故回退为 fail-open。
- **memory 读是无 I/O 的进程内加锁内存读**（如 `get_daily_pnl` 直接 `return self._daily_pnl`），真实失败概率极低；executor.py:2906 在闸门链前另有一处无保护 `memory.track_daily_pnl()`。
- **关键新发现——原"LOUD 兜底"实际哑火**：`hermes_memory_gate_read_errors_total` counter（metrics.py:363，注释自称 "alert on any sustained rate"）在本部署**无任何告警盯着**：①Prometheus 容器属 litellm 栈，只 scrape job `litellm`(127.0.0.1:4000)，**不抓 hermes-trader /metrics**（sse_alerts.yml 仅 4 条 SSE 规则）；②risk_gates.py **无任何 notify import**，门变盲时仅留容器日志一行——"变盲"实际静默。

### 拍板与落地（2026-09-07）

用户选定 **维持 fail-open + 接飞书告警**：五门继续 `pass:True`（不承担瞬态 memory 异常→全局静默停摆的误杀风险），但把"门变盲"从静默变可见。

- **代码**（risk_gates.py）：新增模块级 helper `_alert_memory_gate_blind(gate, ctx, exc)`——懒加载 `from hermes_trader import notify`、整体 try/except 全包（notify import/lookup 失败也不外抛）、调 `notify.send_card(category="risk", level="danger", dedup_key=f"mem_gate_blind:{gate}")`；利用 notify 内置 **(category, dedup_key) 10 分钟限流**防每 tick 刷屏。五个熔断门 except 分支（coin_circuit / global_halt / consecutive_loss / per_coin_daily_loss / drawdown）在原有 metric.inc 旁各调一次，**fail-open 姿态（return pass:True）与 metric/日志均不变**。生产 `notify.is_enabled("risk") = True`（FEISHU_NOTIFY_CATEGORIES 默认含 risk/system）。
- **测试**（test_audit_bf2_bf6_bf7.py 追加 3 例）：五门读 memory 失败→必发 risk/danger 卡片且仍 pass:True；告警链路自身抛异常/notify import 失败→门仍静默放行（告警纯 best-effort 不阻断）；门因真实原因（连亏达限）拦截时**不**发变盲卡片。
- **闸门**：py_compile 全绿；全量 pytest 实跑 **2954 passed / 14 deselected**（基线 2951 净增 3，只增不减）。

### 同波顺带修复（观察脚本驱动发现）

- **测试污染根治**：排查"只读挂载 ~/.hermes-trading 却有当前 mtime shadow 文件"矛盾，根因为宿主跑 pytest 时 tests/conftest.py 仅重定向 12 个 shadow 臂中的 2 个（ta_late_entry/market_circuit），漏网臂（daily_extension_cap/regime_overlay 等）把测试合成行（coin="TESTCOIN"、daily_change_pct=100.0、run_count=3）写进开发者真实 HOME（容器内该路径 ro 写不进、宿主可写，还触发 .1 轮转）。已在 conftest.py 用单一 for 循环把**全部 12 臂** `*_SHADOW_FILE` env 重定向到 tmp；污染文件归档 /tmp，复跑 pytest 验证宿主 HOME 不再新增。
- **生产 daily_extension_cap 采数缺口（真问题）**：该臂 mode=shadow 但 config `shadow_log_path=""`、compose env 与 .env.local 两处均无 `HERMES_DAILY_EXTENSION_CAP_SHADOW_FILE` → 路径三级解析落到容器内只读默认路径，append 被 try/except 静默吞、采不到数。经容器内 `config_store.update_agent_config()` 权威写 `shadow_log_path=/data/daily_extension_cap_shadow.jsonl`（热加载即时生效、自动 .bak、免重建），重启后首条**真实生产行**已落盘（GMX / daily_change_pct 3.24 / ext_would_block false）。
- **3 处代码小尾巴**：config_store.py 陈旧注释订正；shadow_validate.py 硬编码日志路径 env 化（HERMES_TRADING_LOOP_LOG）；perception.py movers 排序缓存价复用 C8 龄期门兜底。

### 上线与现状

- 镜像从 /home/ldy/hermes-trader 源码重建（非 bind-mount），`docker compose build && up -d` 滚动重启；新容器 **8ccb174fe85d** healthy（替换旧 466aeae08efd）。
- 生产冒烟：C1 helper 在镜像内、五门均接线、notify risk 通道 enabled；daily_extension_cap 配置保留指向 /data；启动日志无 error/traceback，trading loop + WebSocket 正常。
- 回滚标签 `pre-remediation-deploy-20260907` 在；**代码改动仍未 commit**（按用户指示）。
- **后续 M4（等数据）**：各 shadow 臂（含修复后 daily_extension_cap）积样本，24/72/168h 后 reconcile outcome 回填，达标才转 enforce。

---

## Pathia 吸收审计缺口闭环：C11 硬底测试 + shadow 巡检修复 + 夜间评级器（2026-09-07）

> 背景：Pathia（autonomous_cycle.py 每晚读 forward ledger 自动评级/降级）吸收审计后确认三项可代码化缺口——①C11 `$10 min_tradable_equity` 硬底无专属拦截测试；②`shadow_progress.py` 巡检有三处路径/配置解析缺陷会"静默说谎"；③缺 Pathia 那种夜间 forward-ledger 自动评级器（shadow 臂靠人工 reconcile）。用户指令"先将未优化的项完整优化"，三项全部落地，**评级器刻意 inert（只评级+飞书建议，绝不自动改配置/闸门/下单）**。

### 1. C11 硬底专属拦截测试（tests/test_c11_min_equity_floor.py，新增 5 例）

- 覆盖 executor.py:2871-2887 `agg_equity < min_tradable_equity_usd` → `executed:False, reason="below_min_tradable_equity ..."`：SHADOW 拦、LIVE 拦、边界 `==`（`<` 非 `<=`）不拦、上方不拦、阈值 0 禁用该门。复用 C3 测试 mock 范式（synthesis 账户权益、place_hl_order 被禁）。**5 passed**。

### 2. shadow_progress 三处解析缺陷修复（scripts/shadow_progress.py）

- **trend_filter env 名错**：旧表写 `HERMES_TREND_FILTER_200MA_SHADOW_FILE`，写入侧（risk_gates.py:1657）实为 `HERMES_TREND_FILTER_SHADOW_FILE`（mode env 同为 `HERMES_TREND_FILTER_MODE`）→ 巡检读的 env 永远不命中。已对齐写入侧。
- **sizing_v2 寄生块读不到**：sizing_v2 无独立配置块，mode 在 `atr_risk_sizing.sizing_v2_mode`（env `HERMES_SIZING_V2_MODE` 优先；legacy 布尔 `sizing_v2_enabled=true`→enforce），路径键 `atr_risk_sizing.sizing_v2_shadow_log_path`。旧表按独立块解析必落空。
- **ARMS 4 元组 → 6 元组** `(label, blk, env_file, default_name, mode_key, path_key)`，`_arm_mode`/`_arm_path` 参数化；新增 legacy 布尔映射。回归测试 tests/test_shadow_progress_paths.py（5 例）锁定两处修正 + 结构守卫。

### 3. 夜间 forward-ledger 评级/建议器（scripts/shadow_grade.py，新增，INERT 只读）

- **复用** shadow_progress 的 ARMS/路径/mode 解析 + memory 真实成交账本（`from hermes_trader.agents.memory import memory` 单例，`get_payoff_stats`/`get_win_rate`）。
- **三窗口** 24/72/168h 统计各臂触发量、命中率、（reconcile 回填的）反事实 outcome 胜率与 pnl。**六档评级**：`PROMOTE_CANDIDATE`（可人工考虑升 enforce）/ `COLLECTING`（enforce 臂或命中率过低继续观察）/ `INSUFFICIENT_DATA`（shadow 臂样本<60）/ `DATA_GAP`（shadow/enforce 却 0 条，最危险的"门变盲"）/ `REVIEW`（回填反事实提示误伤/有害）/ `OFF`（mode=off，如实标注不误导）。
- **命中字段按臂实测映射**（容器 /data 逐臂取证）：block 类含 ta_late_entry 的 `blocked`、daily_extension 的 `ext_would_block` 等；change 类 `would_change`/`would_block_gate`，sizing_v2 无布尔字段改用 `|v2_notional - v1_notional|/v1 > 1%` 判定；signal 类 `is_candidate`/`tripped`，pullback 用 `composite_score>0`。时间戳兼容 ISO 字符串与 ms/s epoch。
- **红线守护**：`grade_arm` 为纯函数（records 注入，无 I/O，可单测）；`collect_grades` 只读 config+memory；`_push_feishu` 仅在有 gap/review/promo 时发 `category="risk"` 卡片（dedup_key=`shadow_grade_nightly` 10 分钟限流），整体 try/except 永不抛；**PROMOTE 仅是建议，切 enforce 仍走 config_store 权威写+人工**。main 支持 `--json/--push/--windows`，仅 DATA_GAP 时 exit 1（供 cron 暴露），评级本身不阻断任何东西。
- **单测** tests/test_shadow_grade_arms.py（18 例）：五/六档评级分支、ISO/ms 两种时间戳、窗口截断、block/change/signal 三类命中字段、ta_late_entry/sizing_v2/pullback 三处定制字段、报告渲染。

### 闸门与上线

- py_compile 全绿；全量 pytest 实跑 **2982 passed / 14 deselected**（基线 2954 净增 28：C11 5 + shadow_progress 5 + grader 18，只增不减）。
- 镜像重建滚动上线，当前容器 **f3c52610a973** healthy（替换 41956ab5b60a / 8ccb174fe85d）。
- 容器内冒烟（真实 /data）：评级器 12 臂全出评级、exit 0、0 缺口；字段映射修复后 sizing_v2 命中率 77.2%、ta_late_entry 99.8%（该 gate 日志范式为"评估为迟到入场"事件，1803 blocked/3 allowed，grader 如实读取）；`--json` 机读正常；`_push_feishu` 代码路径（stub notify）验证不抛异常。生产 closes=0（SHADOW 无真实成交），报告已注明"评级仅基于 shadow 采数，缺真钱对照"。
- **夜间调度已接（2026-09-07 用户拍板）**：容器内无 cron，仿 cron_reconcile.sh 在宿主机加包装脚本 scripts/cron_shadow_grade.sh（容器名/日志目录/窗口可 env 覆盖；容器不在则记日志 exit 3；调 `docker exec hermes-trader python /app/scripts/shadow_grade.py --push --windows 24 72 168`，best-effort 不阻断），日志落 `~/.local/state/hermes-trader/shadow_grade.log`。宿主机 crontab 已注册 `45 8 * * *`（CRON_TZ=Asia/Shanghai，即每日 08:45 CST / 00:45 UTC，排在 08:15 fills reconcile 之后）。手动冒烟 exit 0、日志正常。
- **代码改动仍未 commit**（按用户指示）。

## 前端接入 M1：评级中心只读 API + 夜间历史快照 + blind SSE 告警（2026-09-07 落地）

> 依据 docs/frontend-integration-assessment-2026-09-07.md 的 M1 阶段（trader 侧铺路，不动前端）。全部只读/告警镜像，**评级器 INERT 红线不变**：API refresh 不写历史快照（防止人工刷新伪造夜间趋势）、不自动改配置/闸门/下单，PROMOTE 仍仅建议。

### 1. 夜间评级历史快照（scripts/shadow_grade.py，m1a）

- 新增 `HISTORY_FILE`（env `HERMES_SHADOW_GRADE_HISTORY` 可覆盖，默认 `/data/shadow_grade_history.jsonl`）+ `HISTORY_MAX_LINES=400`（每晚 1 条约 13 个月）。
- 新函数：`append_history(d)`（best-effort：makedirs + 追加 JSONL + 超限时 `_trim_history` 保留最新 400 行，任何失败打 stderr 返回 False，绝不抛）、`read_history(since_ms, limit)`（缺文件/坏行返回 `[]`）、`_slim_snapshot(d)`（投影为 `{ts, generated_at, window_h(最长窗), real_closes, arms[]{arm,mode,kind,verdict,total,hits,decisions,hit_rate,mature_outcomes}}`）。
- **仅 main()/cron 路径在 `_push_feishu` 之后追加快照**；API refresh 不触发写盘。

### 2. 评级中心三端点（hermes_trader/dashboard_routes/shadow_arms.py 新建，m1b）

- `GET /api/dashboard/shadow-arms/grades?windows=24,72,168`：**匿名可读**（读端点，数据含风控姿态但无密）；包 `shadow_grade.collect_grades()` 经 `asyncio.to_thread` + `_ttl_cached` **60s TTL**（per-key singleflight，防 MB 级 JSONL 实时解析）；windows 参数逗号分隔、范围 1..2160h，非法 422。
- `POST /api/dashboard/shadow-arms/refresh`：`_require_operator(write=True)`（Bearer/X-Operator-Token，per-IP 限速）；body 解析失败 422；重算后**暖写 TTL 缓存**；追加审计事件 `session_log.append({"event":"shadow_arms_refresh", ..., "via":"web", "counts":{verdict 计数}})`；返回 `{"ok":true, ...report}`。
- `GET /api/dashboard/shadow-arms/grade-history?days=30&limit=400`：经 `to_thread` 调 `sg.read_history(since_ms, limit)`，返回 `{snapshots, count, days}`。
- scripts/ 非包导入：`importlib.util.spec_from_file_location("hermes_shadow_grade", path)` 懒加载，候选路径 `$HERMES_SCRIPTS_DIR` → `/app/scripts` → 相对 `../../scripts`，scripts dir 入 sys.path；加载失败缓存异常、三端点统一 503。
- 路由注册：dashboard.py `register_routes` 在 shadow 之后、public（SPA catch-all）之前调 `register_shadow_arms_routes(app)`，防 `/{full_path:path}` 吞 API。

### 3. blind-gate 告警补发 SSE（hermes_trader/agents/risk_gates.py，m1c/F4）

- `_alert_memory_gate_blind` 在飞书卡片之后**独立 try 块**追加 `session_log.append({"event":"risk_gate_blind", ts, gate, coin, posture:"fail-open", error})`。
- 事件经 feed = session_log tail 自动进 `/api/feed/stream` 与 `/api/feed/history`；**故意不加入 `_PUBLIC_FEED_EVENTS` 白名单**（operator 认证客户端可见，匿名客户端不泄露风控姿态），也不进 `fork_from_session`/`notify_dispatch` 白名单（不重复发飞书、不写 events.jsonl）。
- 测试驱动出的结构修正：飞书 send_card 与 SSE 镜像各占独立 try 块——飞书故障不得压制 Web 实时告警（初版嵌套 try 会被测试模拟的 send_card 抛错跳过）。

### 4. 闸门与上线

- 单测 tests/test_shadow_arms_api.py（新增 15 例）：历史快照 6 例（slim 投影取最长窗、append/read 往返、缺文件 []、since_ms 过滤、trim 保留最新、坏路径不抛）；端点 7 例（grades 匿名 200 + 二次命中缓存、bad windows 422、grade-history 200、refresh 无 token 401/带 token 200 暖缓存 + 审计事件 counts、grader 不可用三端点 503）；blind SSE 2 例（send_card 抛 RuntimeError 时仍出 1 条 risk_gate_blind 且字段齐全；append 自身抛异常函数不抛）。
- py_compile 全绿；全量 pytest 实跑 **2997 passed / 14 deselected**（基线 2982 净增 15，只增不减）。
- 镜像重建滚动上线，当前容器 **6b029550aced** healthy（替换 f3c52610a973）。
- 容器内冒烟（真实 /data）全通：grades 返回 12 臂真实评级（pullback OFF、ta_late_entry enforce/COLLECTING、atr_regime_calib 与 sizing_v2 shadow/PROMOTE_CANDIDATE）；grade-history 初始 count=0；refresh 无 token 401、带 Bearer token 200/12 臂；windows=abc 422；容器内跑 `shadow_grade.py --json`（exit 0）后 `/data/shadow_grade_history.jsonl` 生成（2129 字节）、grade-history count=1；手动触发 `_alert_memory_gate_blind('coin_circuit', ...)` 后 operator feed-history 见 1 条 risk_gate_blind（gate/coin/posture/error 齐全），匿名 feed 确认不泄露。
- **M1 代码改动仍未 commit**（按用户指示，等"提交并推送"指令）。下一步 M2：hermes-portal BFF `_PATH_RULES` 登记 `/api/dashboard/shadow-arms/*`。
