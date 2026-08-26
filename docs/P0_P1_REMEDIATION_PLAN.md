# P0/P1 整改计划表

> 生成日期：2026-08-23
> 测试基线：341 passed, 14 deselected
> 真值源：`.agent-config.json`（mode=LIVE）

---

## 一、P0 资金安全项（默认值对齐线上 LIVE 配置）

| 编号 | 文件:行号 | 参数 | 修改前（危险默认） | 修改后（线上保守值） | 预期效果 |
|------|-----------|------|--------------------|----------------------|----------|
| G-1 | `executor.py:975` | `equity_fraction_per_trade` | 0.01 | **0.2** | 单笔权益占比与线上一致（ATR equal-risk 主路径覆盖此值） |
| G-2 | `executor.py:623` | `min_ai_confidence` | 0.30 | **0.70** | 配置缺失时拒绝低置信度交易，不再裸奔放行 |
| G-3 | `executor.py:983` | `whale_size_multiplier` | 1.3 | **1.0** | 巨鲸信号默认不加码，与线上风控一致 |
| G-4 | `risk_gates.py:1256` | `max_trade_notional_usd` | 100000 | **800** | 单笔名义上限 $800，防止配置丢失时开出天价单 |
| G-5 | `risk_gates.py:1257` | `max_daily_loss_usd` | -100 | **-30** | 日亏熔断 $30 即停，匹配小账户实盘 |
| G-6a | `risk_gates.py:1278` | `max_total_notional_pct` | 1.0 (100%) | **10.0 (10%)** | 聚合权益名义上限 10%，语义修正 |
| G-6b | `risk_gates.py:1275` | `cooldown_min` | 60 | **30** | 交易冷却 30 分钟 |
| G-6c | `risk_gates.py:1277` | `max_crypto_long_correlated` | 2 | **3** | 相关性多头上限 3 个 |
| G-6d | `risk_gates.py:1265-1266` | `min_market_volume_usd` / `min_hip3_volume_usd` | 500000 / 500000 | **5,000,000 / 5,000,000** | 流动性门槛提升至 $5M，过滤仙币 |
| G-6e | `risk_gates.py:1239` | `min_ai_confidence`（gate 侧） | 0.35 | **0.70** | 闸门侧与执行器侧统一 |
| G-6f | `dsl_exit.py:275-284` | `max_loss_pct` / `max_loss_roe_pct` / `protect_pct` / `retrace_threshold` / `hard_timeout_minutes` | 2.5 / 50 / 1.5 / 0.30 / 180 | **0.4 / 5.0 / 1.25 / 0.20 / 1800** | 止损与退出参数全面对齐线上（0.4% 现货止损、5% ROE 封顶、30h 硬超时） |
| G-6g | `dsl_exit.py:315-319` | `stale_flat_timeout_minutes` / `phase2_tiers` | 0 / 单档 | **480 / 2 档 (8%→35%, 15%→40%)** | 8h 不创新高则砍仓；分段止盈让利润奔跑 |
| G-6h | `research.py` | `sl_atr_mult` | 3.0 | **2.0** | ATR 止损倍数收紧 |
| G-6i | `perception.py` | `min_volume_usd` | 低值 | **线上值** | 感知层成交量门槛对齐 |
| G-6j | `trading_loop.py` | `cooldown_min` | 60 | **30** | 循环侧冷却与配置一致 |

---

## 二、P1 中危项

### C 类：死代码清理

| 编号 | 文件:行号 | 清理内容 | 预期效果 |
|------|-----------|----------|----------|
| C-1 | `research.py` | 删除 LLM 连接池（`_LLM_POOL` / `_get_llm_client` 等） | 移除未使用的 httpx 连接池，消除虚假连接复用 |
| C-2 | `executor.py` | 删除 `max_retries` 形参（调用方已不传） | 简化下单签名，避免误导 |
| C-3 | `research.py` | 删除重复的 `obv()` 函数（保留一份） | 消除同名函数覆盖隐患 |
| C-4 | `market_regime.py` | 删除死 wrapper（仅转发未增加逻辑） | 减少一层无意义间接调用 |
| C-5 | `dsl_exit.py` | 删除未使用的 `urllib.error` import | 消除死 import |
| C-6 | `notify.py` | 删除未使用的 `urllib.error` import | 同上 |
| C-7 | `hl_client.py` | 删除未使用的 `as_completed` import | 同上 |
| C-8 | `whale_index.py:21` | 删除未使用的 `_http_post` import | 同上 |
| C-9 | `surge_postmortem.py:49` | 删除 `field` import（dataclass 未使用 field()） | 同上 |
| C-10 | `surge_postmortem.py:52` | 删除 `Tuple` typing import | 同上 |
| C-11 | `surge_postmortem.py:73-76` | 从 `_MOMENTUM_TRIGGERS` 删除 `"volatilityExpansion"`（triggers.py 不存在该触发器） | 防止匹配幽灵触发器 |

### G 类：配置缺失/矛盾补全

| 编号 | 文件:行号 | 补全内容 | 预期效果 |
|------|-----------|----------|----------|
| G-7 | `.agent-config.json` 末尾 | 显式写入 `debate_gate`（enabled / min_agreement=0.6 / min_agree_count=3） | 消除 5 路辩论闸门的隐式默认 |
| G-8 | 同上 | 显式写入 `hta_risk_gate`（fail_closed=false / fail_closed_shorts=true / circuit_*） | 空单 fail-closed、多单 fail-open 显式化，熔断器参数落盘 |
| G-9 | `.agent-config.json:17` | 显式写入 `research_cooldown_min: 15` | 研究冷却不再依赖代码默认 |
| G-10 | `AGENT_CONFIG_REFERENCE.md:81` | 文档化 `HERMES_MAX_MARKETS_MOVERS=10` 环境变量 | 运维可发现该阈值 |
| G-14 | （线上已为 false，无需改动） | `conviction_sizing: false` | 确认与代码默认一致 |

### F/D 类：文档不符与行号漂移

| 编号 | 文件 | 修正内容 |
|------|------|----------|
| F-1 | `docs/AGENT_CONFIG_REFERENCE.md` | 14→16 道闸门；`ta_sidestep_min_slow_burn_count` 2→99；新增 `research_cooldown_min`；equity_risk 语义修正；新增 debate/hta_risk 行；`bypass_sidestep_overrides` false→true；`max_loss_roe_pct` 3→5；`regime_aware.enabled` false→true（共 8 处） |
| F-2 | `docs/CONFIG.md` | 23 处默认值修正（equity_fraction、notional、daily_loss、cooldown、confidence、correlation、whale、volume、dsl_exit 全套等） |
| F-3 | `docs/ARCHITECTURE.md` | 闸门数 11/12→16；Kelly sizing 死引用替换为 ATR equal-risk；journal-schema 死链接修正；scan 60s→15s |
| F-4 | `README.md` | REST routes 22→27；MCP tools 100→101；health 路径 `GET /`→`GET /api/health`；scan 60s→15s |
| D-1 | `docs/HYPE_LIQUIDATION_RCA_2026-08-21.md` | 3 处行号漂移修正（executor.py:1220→1457、1028→1235、trading_loop.py:201→243） |
| D-2 | `docs/regime-score-final-config-abc.md` | Plan 状态"待落地"→"已生产落地"；executor.py:155→201 |
| D-3 | `docs/proposal-chop-regime-filter-strategy.md` | risk_gates.py#L338-L358→#L368-L381 |

---

## 三、验证结果

```
341 passed, 14 deselected in 44.66s
```

修复过程中同步调整了 3 个因默认值收紧而需要显式传参的测试：

- `tests/test_atr_stop.py:27`：`_policy()` helper 显式 `stale_flat_timeout_minutes=0.0`（新默认 480 会让"off"分支误触发）
- `tests/test_cleanup.py:2180`：ta_sidestep 测试显式 `ta_sidestep_min_slow_burn_count=1`（新默认 99 会禁用该分支）
- `tests/test_cleanup.py:2489`：whale boost 测试显式 `whale_size_multiplier=1.3`（新默认 1.0 不再加码）

---

## 四、修改文件清单

### 代码文件（10 个）

1. `hermes_trader/agents/executor.py` — P0 默认值 7 处 + C-2 max_retries 删除
2. `hermes_trader/agents/risk_gates.py` — P0 默认值 8 处
3. `hermes_trader/agents/dsl_exit.py` — P0 dataclass 默认值 7 处 + C-5 urllib.error 删除
4. `hermes_trader/agents/research.py` — C-1 LLM 池删除 + C-3 OBV 去重 + P0 sl_atr_mult
5. `hermes_trader/agents/market_regime.py` — C-4 死 wrapper 删除
6. `hermes_trader/agents/perception.py` — P0 min_volume_usd
7. `hermes_trader/agents/whale_index.py` — C-8 _http_post 删除
8. `hermes_trader/surge_postmortem.py` — C-9/C-10/C-11 清理
9. `hermes_trader/notify.py` — C-6 urllib.error 删除
10. `hermes_trader/client/hl_client.py` — C-7 as_completed 删除

### 配置文件（1 个）

11. `.agent-config.json` — G-7/G-8/G-9 补全 debate_gate、hta_risk_gate、research_cooldown_min

### 脚本文件（2 个）

12. `scripts/trading_loop.py` — P0 cooldown_min
13. `scripts/verify_fixes.py` — 配合 C-1 反转检查

### 测试文件（2 个）

14. `tests/test_cleanup.py` — C-4 改用公开 API + 3 处默认值适配
15. `tests/test_atr_stop.py` — stale_flat_timeout 默认值适配

### 文档文件（7 个）

16. `docs/AGENT_CONFIG_REFERENCE.md` — 8 处修正
17. `docs/ARCHITECTURE.md` — 闸门数/Kelly/死链接/scan interval
18. `docs/CONFIG.md` — 23 处默认值修正
19. `docs/HYPE_LIQUIDATION_RCA_2026-08-21.md` — 3 处行号
20. `docs/regime-score-final-config-abc.md` — 状态 + 行号
21. `docs/proposal-chop-regime-filter-strategy.md` — 行号
22. `README.md` — 6 处修正
