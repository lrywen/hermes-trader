# W7：回测内核 guard 补杠杆 + 成本表守卫（生产可比性）SCOREBOARD

> 日期：2026-09-21（Asia/Shanghai）
> 授权：用户「立即对所有未完成改造项全面梳理与实施」
> 范围：承接 2026-09-21 只读取证识别的 **B 类防护缺口**（guard.py 缺 lev 校验、缺成本表齐全度校验、B-12/B-13 生产部署），本轮完成可安全编码的两项并接入调度；同步对 A 类工程解构做范围裁定。
> 性质：**只读型守卫增强，零交易语义变更**；生产配置 `/data/.agent-config.json`（mode=SHADOW）只读，**未重启容器、未 push**。

---

## 裁定：B 类两项守卫已实现、已接线、已被测试钉死；B-12/B-13 仓库侧早已完成，生产部署需重建镜像（属运维动作，本轮按纪律不做）；A 类高风险解构经评估明确不做。

一句话：**回测在调度前会硬校验"杠杆=生产 10"且"币池全部有逐币成本"，不满足即拒绝出结果，防止口径不可比与成本静默回退。新增 19 个离线测试，全量回归 4629 passed。**

---

## 一、改造内容与实现方式

落点：[hermes_trader/backtest/guard.py](../../../hermes_trader/backtest/guard.py)（既有结构 PIT 断言模块，沿用其 fail-closed 风格）。

### B-guard① 杠杆一致性

- 新增受保护常量 `LIVE_LEVERAGE = 10`（取自生产权威配置 `/data`，2026-09-21 实测）。
- 新增 `assert_leverage_allowed(leverage)`：非正整数/布尔/非 int 类型报 `ValueError`；与生产不等即硬拒绝。
- 依据：杠杆决定初始/维持保证金、爆仓距离与 `max_loss_roe` 的 ROE 缩放，不同倍数下回测与生产的强平/止损不可比。性质同 `MAX_CONCURRENT_POSITIONS`——固化而非待扫描参数。

### B-guard② 逐币成本表齐全度

- 新增 `check_cost_table_coverage(coins, covered_coins)`（返回缺币列表，大小写不敏感）与 `assert_cost_table_complete(...)`（缺币即硬拒绝，错误信息列出前 10 个）。
- 新增常量 `COST_TABLE_FALLBACK_BPS = 0.31`（与 bt_ra_exch 的回退值一致）。
- 依据：单边滑点取自 `hermes_trader/data/per_coin_half_spread_bps.json`（81 币半价差）；币不在表中时 `_slip_for` 静默回退 flat/0.31bps，会低估该币成本而不报错。守卫在调度前强制币池被成本表完整覆盖。

### 接入调度 chokepoint

落点：[scripts/bt_ra_exch.py](../../../scripts/bt_ra_exch.py) 的 `main()`：

- 解析 `coins` 后立即 `assert_cost_table_complete(coins, PER_COIN_SLIP_BPS.keys())`；
- 解析配置 `_live_leverage` 后校验其为整数倍并 `assert_leverage_allowed(int(...))`；
- 不满足直接抛错退出，**不产生任何回测产物**（fail-closed，杜绝"口径错了还出结论"）。

> 未改内核 `backtest/driver.run`（其 leverage 默认 1，是通用底层原语）；守卫只加在研究编排层，保持内核通用性与既有调用不破坏。

## 二、测试结果

新增 [tests/test_b_guard_leverage_costtable.py](../../../tests/test_b_guard_leverage_costtable.py)，**19 个离线测试全绿**：

- leverage：生产值放行；0/负值/10.0/"10"/布尔/None 等非法类型与 1,3,5,20 等异值全部拒绝（参数化）；
- 成本表：全覆盖为空、大小写不敏感、缺币正确列出、`assert_*` 缺币抛错/全过放行；
- **数据完整性回归**：加载随包 `per_coin_half_spread_bps.json`，断言其覆盖生产研究池 81 币（`/tmp/p6b1_recon.json`，文件缺失时 skip，不依赖 CI 外资产）。

回归与护栏：

| 项 | 结果 |
|---|---|
| 全量离线回归 | **4629 passed / 0 failed / 14 deselected**（307.8s） |
| CI 离线测试 floor | 4610 → **4629**，`ci_test_guard count` OK |
| walltime 守卫 | 307.3s ≤ 900s，OK |
| ruff | 改动文件 All checks passed（import 排序已自动修正） |

## 三、B-12/B-13 与 A 类的范围裁定（本轮为何不做）

- **B-12/B-13（SHADOW→LIVE 启动验收门）**：仓库侧**早已完成并接线**——`hermes_trader/agents/live_gate.py`（commit `ffd0002`）+ trading_loop 启动调用。只读取证确认**生产容器镜像（2026-09-19 05:24 构建）早于该提交、镜像内无 live_gate.py，故未上线**。要部署需重建镜像/重启容器，属运维动作；本轮按只读纪律不做。缓解事实：生产 `mode=SHADOW` 且账户未入金，该门禁未上线当前不产生真实资金风险。
- **A 类工程解构**：
  - P1-1 step①（shadow 5 模块）已完成；step② 低风险半（orders 门面 re-export 12 个订单函数）已完成；step③ `maybe_execute` 实质**已是编排外壳**（block/sizing/placement/register/brackets 各阶段均已抽成命名 helper，注释逐段标注）。
  - step② 剩余的**高风险半**（`_place_backup_sl` / `_register_filled_position` / `_reconcile_unknown_order_result` 等 bracket/lifecycle 函数）被**有意且正确地保留在 executor**：它们持有跨进程入场 flock、pending-SL 持久化与 DSL 注册写入；且锁释放在约 20 个返回分支间成对配对。纯搬位置零行为收益、却在真实下单路径上引入高回归风险，缺乏可靠测试保证。按最小改动原则，本轮**不搬**，维持 orders.py docstring 既定边界。
  - P2-1 scripts 瘦身（83 个）、P2-3 策略插件化属大规模范式改造，当前负期望结论下无功能价值且风险高，不投入；P2-2 server/dashboard 已抽 7 路由，维持现状。

## 四、潜在影响评估

- **交易语义**：零变更。守卫仅在研究/回测编排入口前置校验，不触碰下单、仓位、止损逻辑；生产 SHADOW 运行不受影响。
- **失败模式**：配置杠杆被改成非 10、或回测币池出现成本表中没有的币时，现在会**立即显式报错**（此前是静默按错误口径产出结果）。这是期望行为，但会使依赖"任意 leverage/任意币子集"的研究脚本在口径不一致时被拦下——属正确收紧。
- **兼容性**：内核原语与既有 API 未改；两 import 路径、既有测试全部保持通过。
- **后续**：B-12/B-13 真正生效需在某次计划内发布窗口重建镜像（建议与 live_gate 一并灰度）；C/D 等数据项（C-1/3/5/6、D-2/4/5/8）前置已解除、纯待样本积累；ADR-0003 末尾的注资/暂停仍待操作者决策。
