# Sizing-Bug 仓位系统性优化 + 风控漏洞闭环加固 — 交付文档

**日期**: 2026-08-26
**范围**: `executor.py` / `dsl_exit.py` / `risk_gates.py` / `memory.py` / `metrics.py` + 两份审计脚本
**兼容性**: 全部改动向后兼容；sizing v2 由 `atr_risk_sizing.sizing_v2_enabled`（默认 `false`）灰度门控，关闭时走旧逻辑。

---

## 一、复核结论：问题清单与根因（已用生产数据确认）

### 1. Sizing Bug（已确认，代码层已修复）

原 sizing 路径读取**顶层** `max_loss_pct=2.5` / `max_loss_roe=25`，而 DSL 实际止损经
`select_exit_params` + `atr_stop` clamp + ROE cap 三层计算后是 regime-aware 的。10x 杠杆下
ROE cap 是绑定约束：

| regime | DSL 三层有效止损（现货%） | 旧 sizing 假设 | 仓位被压缩倍数 |
|---|---|---|---|
| scalp/chop | `min(atr_clamp≥1.2%, 5/10=0.5%)` = **0.5%** | 2.5% | **5×（under-risk）** |
| trend/up/down | `min(atr_clamp≥1.2%, 10/10=1.0%)` = **1.0%** | 2.5% | **2.5×（under-risk）** |

根因：sizing 与 DSL 止损计算**两套逻辑、参数脱钩**，仓位按一个从不生效的宽止损计算，
导致每笔名义风险被系统性压低 2.5–5 倍。

### 2. 两处历史风控漏洞（生产数据已闭环/部分闭环）

**PURR #6（已证实，生产数据闭环）**
从容器 `/data/.agent-memory.json` outcome store 拉取到的真实记录：

| entry | exit | spot% | lev | ROE% | PnL | close_source |
|---|---|---|---|---|---|---|
| 0.106772 | 0.10238 | **-4.113%** | 3 | -12.42% | **-$0.49** | `exchange_trigger_manual_backfill` |
| 0.153300 | 0.14834 | **-3.236%** | 3 | -9.78% | -$0.45 | `exchange_trigger` |

两笔均由**交易所备份止损**触发，现货跌幅分别超 3% 上限 +37% / +8%；3x 杠杆下 ROE 回撤
约 -3.3%/-3.26%，与任务描述的"单笔回撤 -3.24%"完全吻合。根因：备份止损区间过宽 + 滑点
gap-through，超额亏损。

**BOME floor=0（代码层已修复，实盘数据确认无成交暴露）**
`bome_floor_audit.py` 扫描结果：
- 生产窗口内 BOME 共 53 条事件，**0 笔真实成交**（全部被 `max positions` /
  `runner_gate` / SHADOW 拦截）；outcome store 中 BOME closes/trades 均为 0。
- 当前 `[dsl:floor]` 日志中所有 floor 值为正且在正确侧（如 PURR 0.148701、SPX 0.505088）。
- 结论：**保留窗口内无任何 BOME 真实持仓，floor=0 无法用本窗口实盘成交复现**；漏洞在代码
  层已闭合（dsl_exit breakeven clamp + executor 备份 SL floor），实盘闭环待 BOME 下次真实
  成交时由 post-fill 偏差断言与 `ACTUAL_STOP_DEVIATION` 指标自动记录证据。

---

## 二、完整改造方案与代码落点

### 阶段 1：Sizing v2（仓位模块，`executor.py`）

**入口**: [executor.py#L1056-L1129](file:///home/ldy/hermes-trader/hermes_trader/agents/executor.py#L1056-L1129)
（`sizing_basis in ("primary_stop","dsl_stop")` 块内，`sizing_v2_enabled=true` 时激活）

1. **打通参数链路**：调用 `detect_regime(coin)` → `compute_effective_stop_pct(dsl, regime, lev, atr_pct, ...)`，
   内部经 `select_exit_params` 取 regime 参数，移除对顶层 2.5%/25 的依赖。
2. **复刻 DSL 三层风控**：[compute_effective_stop_pct](file:///home/ldy/hermes-trader/hermes_trader/agents/executor.py#L246-L324)
   - Layer 1+2: `spot_cap = min(max(atr_pct*mult, floor), ceiling)`（atr_stop 启用时）
   - Layer 3: `roe_cap = ml_roe / leverage`
   - `core_stop = min(spot_cap, roe_cap)` —— 与 `dsl_exit._evaluate` **字节级同源**。
3. **5% 偏差断言**：register_position 后用 `policy` + `entry_atr_pct` 重算 DSL core_stop，
   与 sizing breakdown 的 `core_stop` 对比，写 `SIZING_DSL_DEVIATION` gauge，>5% 告警
   （[executor.py#L1606+](file:///home/ldy/hermes-trader/hermes_trader/agents/executor.py)）。
4. **低杠杆 ATR floor 兼容**：三层 clamp 完整保留 `atr_floor`（默认 1.0–1.2%），低杠杆
   ROE cap 不绑定时，`min()` 自然落到 atr_clamp，恢复波动率自适应。
5. **ATR 极端波动熔断**：`get_atr_hist_mean_pct(coin,"4h",180)`（≈30 天，6 根/天）算历史
   均值；当前 ATR% > 2× 均值时 `effective *= 0.70`（仅收紧 sizing，不改 DSL 实际止损）。
6. **多币种差异化**：per-coin override 读
   `atr_risk_sizing.coin_overrides.<coin>.{sl_floor_pct,...}`。

> `core_stop`（纯三层结果，用于偏差断言）与 `effective`（core_stop 基础上叠加 ATR-spike
> ×0.70 和滑点补偿加宽，仅影响 sizing）严格区分，sizing 更保守但不改 DSL 真实止损。

### 阶段 2：分级熔断 + 备份 SL 加固

**分级熔断**
- 平仓 chokepoint 武装：[executor.py#L2615-L2703](file:///home/ldy/hermes-trader/hermes_trader/agents/executor.py#L2615-L2703)
  - 单币单笔现货亏损 ≥ `circuit_breaker.single_coin_loss_pct`(3%) → `set_coin_circuit` 暂停 60min
  - 当日累计亏损 ≥ SOD 权益 `circuit_breaker.daily_loss_pct`(5%) → `set_global_halt` 全停 120min
  - 触发即飞书告警（🛑/🚨）。
- 开仓 gate 执行：[risk_gates.py#L184-L221](file:///home/ldy/hermes-trader/hermes_trader/agents/risk_gates.py#L184-L221)
  `coin_circuit_breaker_gate` / `global_halt_gate`，已在
  [eval_all_gates L664-665](file:///home/ldy/hermes-trader/hermes_trader/agents/risk_gates.py#L664-L665) 装配；
  惰性 import memory，状态读取失败 fail-open（debug 日志，不误伤交易）。
- 状态持久化：[memory.py#L447-L487](file:///home/ldy/hermes-trader/hermes_trader/agents/memory.py#L447-L487)
  `set_coin_circuit` / `coin_circuit_remaining_min` / `set_global_halt` /
  `global_halt_remaining_min` / `record_loss_outcome`，随 memory flush 落盘。

**双 SL 冗余 + 滑点动态补偿（PURR #6 修复）**
- 备份 SL placement：[executor.py#L1743-L1825](file:///home/ldy/hermes-trader/hermes_trader/agents/executor.py#L1743-L1825)
  - `sl_floor_pct` 默认 **1.2%**（支持 per-coin override），`sl_ceiling_pct` 默认 3%、硬顶 15%
  - 三层 clamp `min(max(atr_stop_pct, sl_floor_pct), sl_ceiling_pct)`
  - 非正值校验 + floor>ceiling 守卫，防止反向/同侧止损
  - **滑点动态补偿**：`memory.avg_exit_slip_bps(coin, days=30)` 取近 30 天不利滑点均值，
    向不利方向加宽（cap ceiling×0.5 防噪声放大）
  - 下单失败两次 → `_pending_sl_retries` 永不放弃重试（backoff cap 300s）+ 飞书🚨告警
  - DSL 软止损（轮询 ~15s）+ 交易所 server-side trigger 两处同步写入，任一触发即平仓。

**BOME floor 防御**
- DSL: [dsl_exit.py#L521-L529](file:///home/ldy/hermes-trader/hermes_trader/agents/dsl_exit.py#L521-L529)
  breakeven ratchet `max(floor, entry*(1+lock/100))`（long）/ `min(...)`（short），
  叠加 `_last_floor` 单调保护（L531-537），floor 不可能为 0 或反向。

### 阶段 3：监控指标 + 审计脚本

**两项 Prometheus 指标**: [metrics.py#L34-L47](file:///home/ldy/hermes-trader/hermes_trader/metrics.py#L34-L47)
- `hermes_sizing_dsl_stop_deviation_pct`：sizing core_stop vs DSL 实际止损偏差（>5% 告警）
- `hermes_actual_stop_loss_deviation_pct`：已实现止损亏损 vs 配置 cap 超额（>10% 告警）
- 两者在交易热路径/平仓 chokepoint 直接 `.set()`，反映最近一笔交易。

**审计脚本**
- [scripts/bome_floor_audit.py](file:///home/ldy/hermes-trader/scripts/bome_floor_audit.py)：
  BOME 事件/outcome store/日志 floor 值/源码防御四维回溯，EXIT 0=闭环、2=发现异常。
- [scripts/weekly_risk_audit.py](file:///home/ldy/hermes-trader/scripts/weekly_risk_audit.py)：
  每周风控审计报告（止损上限合规、熔断触发、偏差指标、备份 SL 健康、滑点分布），
  输出 Markdown，存在超限行时 EXIT 2 可对接告警。

---

## 三、生产配置（上线前写入 `/data/.agent-config.json`）

代码已用 default 兜底，但上线前应显式配置：

```json
{
  "atr_risk_sizing": {
    "sizing_v2_enabled": false,
    "sizing_v2_cap_pct": 0.10,
    "risk_per_trade_pct": 0.026,
    "sizing_basis": "primary_stop",
    "coin_overrides": {
      "HYPE":   { "sl_floor_pct": 1.5, "atr_stop_floor_pct": 1.5 },
      "PURR":   { "sl_floor_pct": 1.2 },
      "BOME":   { "sl_floor_pct": 1.2 }
    }
  },
  "circuit_breaker": {
    "single_coin_loss_pct": 3.0,
    "single_coin_halt_min": 60.0,
    "daily_loss_pct": 5.0,
    "daily_halt_min": 120.0
  },
  "sl_floor_pct": 1.2,
  "sl_ceiling_pct": 3.0
}
```

`sizing_v2_enabled` 初始保持 `false`；灰度按验收清单逐步打开。

---

## 四、灰度验收测试清单

> 起始仓位权重 **10%**（`sizing_v2_cap_pct=0.10`），运行至少 **7 个交易日**。

### 阶段 0 — 部署前
- [ ] 四个生产文件 `python3 -m py_compile` 通过（已验证 COMPILE_OK）
- [ ] 备份现有 `/data/.agent-config.json`
- [ ] 写入上述新配置键，`sizing_v2_enabled=false`
- [ ] 重新构建/部署容器（**注意：当前运行容器 `/app` 仍是旧代码，必须重新部署后改动才生效**）
- [ ] 部署后立即运行 `python3 scripts/bome_floor_audit.py`，确认 4 项源码防御全 `[OK]`

### 阶段 1 — 静默观测（sizing_v2_enabled=false，1–2 天）
- [ ] 确认分级熔断日志正常（无异常武装）
- [ ] 确认备份 SL 日志含 `floor=` / `slip+=` 字段
- [ ] 运行 `weekly_risk_audit.py --days 7`，确认无新增 >3% 止损超限

### 阶段 2 — 10% 灰度（cap=0.10，≥7 交易日）
- [ ] 置 `sizing_v2_enabled=true`、`sizing_v2_cap_pct=0.10`
- [ ] 每笔检查 `[sizing-v2]` 日志：regime / spot_cap / roe_cap / effective_stop 合理
- [ ] `SIZING_DSL_DEVIATION` 持续 <5%（无 STOP DRIFT 告警）
- [ ] 单笔最大回撤（ROE%）不超过修复前同 regime 水平的 1.3 倍
- [ ] 止损滑点：`avg_exit_slip_bps` 有 ≥3 样本后，备份 SL 加宽生效
- [ ] 无单币/全局熔断误触发

### 阶段 3 — 放量
- [ ] 7 天无偏差告警、无超额止损 → cap 提至 0.25，观察 3 天
- [ ] 再提至 0.50，观察 3 天
- [ ] 全部指标平稳 → cap=1.0 全量

### 对比验收指标（修复前 vs 灰度）
- 仓位利用率（实际 notional / 目标 risk-based notional）：从 ~20–40% 提升至 ≥90%
- 单笔最大回撤（ROE%）：不恶化
- 止损滑点表现：备份 SL 加宽后 PURR 类 +37% 超限不再复现
- 偏差指标：双 gauge 全程低于阈值

---

## 五、上线风险提示

1. **仓位将放大 2.5–5 倍**：这是修复 under-risk 的预期结果，但 10x scalp 名义仓位
   将是原来的 5×。**10% 灰度起步**意味着真实风险增量仅约 0.25–0.5×，可控；切勿
   跳过灰度直接 cap=1.0。
2. **账户整体风险敞口**：放量前确认总名义仓位上限 `max_total_notional_pct`、
   `max_concurrent_positions` 与单币 notional cap 已与放大后的单仓匹配，避免单品种
   极端行情回撤叠加。
3. **ATR-spike 熔断只收紧 sizing、不改 DSL 止损**：高波动时仓位自动缩小 30%，但 DSL
   真实止损不变，不会因熔断造成提前止损。
4. **滑点补偿需要样本**：新币种/样本 <3 时 `avg_exit_slip_bps` 返回 0，不加宽；
   这是有意设计（不在噪声上过度加宽），但意味着全新币种前几笔的备份 SL 不含滑点
   补偿，需人工留意。
5. **熔断 fail-open**：memory 状态读取失败时 gate 放行（避免状态层故障卡死交易），
   但平仓武装失败会有 warning 日志；监控需覆盖 `tiered-breaker arm failed`。
6. **必须重新部署容器**：代码已写入宿主仓库，但运行中容器 `/app` 仍为旧代码
   （bome 审计显示容器内三项 executor 防御 MISSING 即为佐证）。仅修改配置不重新部署
   不会生效。

---

## 六、修改文件清单

| 文件 | 改动 |
|---|---|
| `hermes_trader/agents/executor.py` | sizing v2 块、`compute_effective_stop_pct`、`get_atr_hist_mean_pct`、5% 偏差断言、备份 SL floor+滑点补偿+告警、平仓分级熔断、`ACTUAL_STOP_DEVIATION` |
| `hermes_trader/agents/dsl_exit.py` | breakeven floor clamp（已有，本次复核确认） |
| `hermes_trader/agents/risk_gates.py` | `coin_circuit_breaker_gate` / `global_halt_gate` + 装配 |
| `hermes_trader/agents/memory.py` | 熔断状态、滑点聚合、币日亏损、连续亏损、SOD equity（本次复核确认） |
| `hermes_trader/metrics.py` | `SIZING_DSL_DEVIATION` / `ACTUAL_STOP_DEVIATION` Gauge |
| `scripts/bome_floor_audit.py` | BOME floor=0 回溯脚本（新增） |
| `scripts/weekly_risk_audit.py` | 每周风控审计脚本（新增） |
