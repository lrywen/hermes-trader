# W6：统一技术改造文档遗留项收口 SCOREBOARD（B-6~B-11 + D-7）

> 日期：2026-09-21（Asia/Shanghai）
> 授权：用户「立即执行所有未完成项改造」（承接 [v2.0 改造方案](../../refit_plan_v2_2026-09-19.md) 全面复核）
> 范围：复核中识别的两类真实缺口——① B-6~B-11 六项防护工程；② D-7 reconcile outcome 回填（含 A-6 聚合 fill）
> 性质：防护/记账收口，**不改变结局 B 结论与生产交易语义**；生产权威配置 `/data/.agent-config.json`（mode=SHADOW）只读。

---

## 裁定：全部遗留项已闭环；结局 B 不受影响。

一句话：**B-6/B-7/B-8/B-10/B-11 已落地，B-9 经查在更早阶段已实现，D-7 外部平仓聚合回填链路已修复并被单测钉死；新增 13 个离线测试，全量回归 4610 passed。**

## 逐项收口

| ID | 内容 | 处置 | 落地 |
|---|---|---|---|
| **B-6** | 回测币池 vs 实盘可交易币池一致性自动 diff | 新建脚本，多源（run_meta/研究 JSON/funding_cache，可 `--online`），报 STALE（已退市凭空成交，exit 4）/MISSING（漏评，exit 5） | `scripts/check_pool_consistency.py` |
| **B-7** | 配置来源强制断言 | source-gated：仅当 CONFIG_PATH=`/data/.agent-config.json` 时校验文件存在、为 JSON 对象、7 个关键顶键齐全；本地/CI 放行 | `config_store._authoritative_config_errors` |
| **B-8** | regime_aware 参数生效路径显式化 | `select_exit_params` 两分支（trend_ride / scalp）加 debug 日志，记录 regime、enabled 与最终 protect/retrace/max_loss，选档可观测 | `executor.select_exit_params` |
| **B-9** | notional 规模建模（tier_equity=50 + 8.4% 利用率） | **经查已实现**：`executor._tiered_notional_cap`（equity<50 走 base、≥50 按 1.5× 缩放）+ `bt_ra_exch._simulate_trade` 的 atr_equal_risk + tiered cap（filt_exch 臂即 live-sizing 2.08~30）。本轮仅确认，无重复实现 | 既有 |
| **B-10** | noise_band 保持开启的强制断言 | source-gated：仅在 `/data` 生产源上，noise_band 显式 enabled=false 时报错（可经 HERMES_SKIP_STARTUP_SAFETY 豁免）。canonical 代码默认 false（由 /data 打开），故不对本地/CI 强制 | `config_store._production_noise_band_errors` |
| **B-11** | atr_stop 死代码标记 deprecated | 在 canonical 默认、config_schema、stop_model 三处加 DEPRECATED 说明（§2.7 P4：regime cap 恒胜出、atr_cap 永不 bind）；保留仅为 byte-aligned parity，禁止重新启用 | `config_store` / `config_schema` / `stop_model` |
| **D-7** | reconcile outcome 回填（A-6：分批 TP 漏记） | 见下「D-7 核心修复」 | `dsl_exit.py` / `trading_loop.py` |

## D-7 核心修复（A-6 记账缺口）

- **缺口**：生产 `tp_scale_fraction=0.4` 时交易所分两批减仓（先 40% 再 60%）；旧回填块对每个 dropped tracker 只调一次 `resolve_close_fill`，该函数 newest-first 只返回最近一笔——漏记前一批已实现 PnL、size 只算最后一批，`record_close` 的净 PnL/notional 偏小。
- **修复**：
  - 抽出 `_collect_reducing_fills`（收集 since_ts 后**全部**减仓腿，保留 newest-first 顺序）；
  - 新增 `aggregate_close_fills`（∑sz、∑closedPnl、∑fee、sz 加权 exit px、time 取最旧一笔=仓位完全平完时刻）；
  - 新增 `resolve_close_fills`（收集+聚合），回填块改用它；单腿平仓聚合结果与原单腿一致。
  - 原 `resolve_close_fill` 保留（向后兼容，被 risk_wiring 等引用），docstring 指明分批场景应改用聚合版。
- **边界不变**：HL 侧逐笔真实结算始终正确，缺口仅在本地 closes/outcome/影子统计；近期未走外部回填路径（`backfilled external close`=0）。

## 测试与回归

- 新增离线测试 **13 个**：
  - `tests/test_d7_reconcile_aggregate.py`（+7）：聚合数学（∑PnL/fee/sz、sz 加权 px、time）、收集全腿、端到端、空/失败回落；
  - `tests/test_b_startup_source_guards.py`（+6）：B-7/B-10 source-gated（/data 触发、非 /data 放行、缺键/非对象/关 noise_band）。
  - 同步修正 `tests/test_risk_wiring_contract.py` 的回填 harness（注入名改为 `resolve_close_fills`）。
- CI 离线测试 floor：4597 → **4610**（`scripts/ci_test_guard.py`）。
- 全量离线回归：**4610 passed / 0 failed / 14 deselected**。
- ruff：新增/改动文件无新增 lint；仓库内既存的函数内 I001（dsl_exit L187/2301、trading_loop L80 等）改动前即存在，按最小改动原则不动。

## 边界与后续

- 本轮是 v2.0 任务书的**工程账目收口**，不产生新的交易方向；结局 B（回测可信、净期望为负）与 ADR-0002/0003（换周期 NO-GO、四范式否证）的结论均不变。
- 结构性「等数据」项（C-1/C-3/C-5/C-6、D-2/D-4/D-5/D-8 转正）仍需新月份/更多平仓样本；D-7 落地后其前置已解除，但数据本身仍待时间积累。
- 系统仍处 ADR-0003 末尾的决策分叉：**注资小资金实盘取样（P-B maker）** 或 **暂停研发维持 SHADOW 采数**，交用户决策。
