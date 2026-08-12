# hermes-trader 全项目逐行复核报告（GitHub 最新版）

> **代码来源**: `github.com/Julian-dev28/hermes-trader` (commit `e9dda9b`)
> **复核方法**: `git clone` + 全项目 `grep` + `diff` 对比 + 逐行 Read
> **本地版 vs GitHub**: `diff --strip-trailing-cr` 零差异（仅 CRLF/LF 换行符不同）
> **覆盖范围**: 84 个 Python 文件，约 25,800 行

---

## 最终 Bug 清单（P0/P1/P2 分级）

### P0 — 致命（资金损失风险）: 0 项

无 P0 问题。核心交易路径的 risk gates（14道）、日损 kill-switch、错误恢复（所有 API 调用 try/except + 安全默认值）设计到位。

---

### P1 — 高危（应修复）: 5 项

#### P1-1: daemon.py SIGALRM per-tick 超时完全失效

**状态**: ✅ 确认 — 用户发现，我之前遗漏

**代码证据** (`hermes_trader/client/daemon.py`):
```python
# L117: 循环外装一次 alarm
has_alarm = _arm_tick_alarm(tick_timeout)

# L127: 进入循环
while not stop_event.is_set():
    # L143-144: 每 tick 前 DISARM（取消 alarm）
    if has_alarm:
        _disarm_tick_alarm()  # "reset before starting" — 但这是取消，不是重装

    # L146: scan_fn() 全程无 alarm 保护运行
    scan_fn()

    # L149: except _TickTimeout — 死代码，alarm 已被取消
    except _TickTimeout as e:
        status = "timeout"
```

**分析**: `_arm_tick_alarm()` 在 L117 装一次 alarm（180秒），但 L144 的 `_disarm_tick_alarm()` 在每 tick 执行 `scan_fn()` 之前取消了这个 alarm，之后**从不重新装**。`except _TickTimeout` 是死路径。

**影响**: 任何卡死的 `scan_fn()` 会无限阻塞主循环，daemon 的 per-tick 超时保护形同虚设。

**修复**: L144 应改为 `_arm_tick_alarm(tick_timeout)` 而非 `_disarm_tick_alarm()`，或在 `scan_fn()` 调用后重新 arm。

---

#### P1-2: parallel.py 卡住调用导致无限阻塞

**状态**: ✅ 确认 — 用户发现，我之前遗漏

**代码证据** (`hermes_trader/client/parallel.py`):
```python
# L43: with 块退出时调用 shutdown(wait=True) — 等所有 worker
with ThreadPoolExecutor(max_workers=max_workers, ...) as pool:
    future_to_idx = {pool.submit(fn): idx for idx, fn in enumerate(calls)}

    # L48: as_completed 无 timeout 参数 — 阻塞等待所有 future
    for future in as_completed(future_to_idx):
        # L51: future.result(timeout=30) — 但 as_completed 已过滤到完成的 future
        results[idx] = (True, future.result(timeout=30))
```

**分析**: `as_completed` 不带 `timeout` 参数，会无限等待所有 future 完成。`future.result(timeout=30)` 对已被 `as_completed` yield 的 future 无意义（它们已经完成了）。真正的阻塞发生在: (1) `as_completed` 等待未完成的 future; (2) `with` 块退出时 `shutdown(wait=True)` 等待所有线程。

**影响**: 一个 API hang 会锁死整批并行调用。

**修复**: `as_completed(future_to_idx, timeout=X)` + `pool.shutdown(wait=False, cancel_futures=True)` 在超时时。

---

#### P1-3: PID 文件路径不一致

**状态**: ✅ 确认 — 双方一致

**代码证据**:
```python
# server.py L82:
PID_FILE = os.path.expanduser("~/.hermes-trader.pid")

# __main__.py L323:
pid_file = os.path.expanduser("~/.hermes.pid")
```

**影响**: Dashboard 的 `/api/agent/start` `/api/agent/stop` 端点无法管理通过 `hermes start` CLI 启动的进程，可能产生孤儿进程。

**修复**: 统一为一个路径常量。

---

#### P1-4: DSL 退出逻辑 5 处独立实现

**状态**: ✅ 确认 — 双方一致

**5 处实现**:

| # | 文件 | 实现形式 | hard_timeout | 默认 max_loss_pct |
|---|------|----------|:---:|:---:|
| 1 | `hermes_trader/agents/dsl_exit.py` (live) | `DSLTracker` 类 | ✅ | 从 config |
| 2 | `scripts/backtest.py` L68-117 | `DSL` dataclass | ✅ `hard_timeout_bars=180` | 2.5 |
| 3 | `scripts/backtest_logged.py` L223-263 | `simulate_dsl_exit()` | ✅ `hard_timeout_minutes=180` | 2.0 |
| 4 | `scripts/backtest_full.py` L140-199 | `simulate_dsl_exit()` | ✅ `hard_timeout_minutes=180` | 2.0 |
| 5 | `scripts/backtest_portfolio.py` L43-75 | `Position.step()` | ❌ **缺失** | **0.75** |

**影响**: 任何 live DSL 引擎的变更必须手动同步到 4 个 backtest 文件，且 #5 已落后（缺 hard_timeout + 默认值不一致）。唯一使用真实 `DSLTracker` 的是 `phase3_replay.py`。

---

#### P1-5: backtest_portfolio.py Position 缺少 hard_timeout

**状态**: ✅ 确认 — 双方一致（从 P1-4 拆出）

**代码证据** (`scripts/backtest_portfolio.py` L55-75):
```python
def step(self, bar: Candle):
    # 检查 max_loss
    if loss >= self.max_loss:
        return px, "max_loss"
    # 检查 floor_breach
    if ... >= self.protect:
        if bar.l <= floor:
            return floor, "floor_breach"
    # ❌ 没有 hard_timeout 检查
    return None  # 仓位可能无限期持有
```

对比 `backtest_logged.py` L229:
```python
if i >= timeout_bars:  # hard_timeout 检查
    return (..., "hard_timeout", ...)
```

**影响**: 回测中仓位可无限期持有，仅靠"蜡烛数据耗尽"关闭，与 live 行为严重不符。

---

### P2 — 中低危: 12 项

#### P2-1: memory.py `_peak_daily_pnl` 不持久化

**状态**: ✅ 确认 — 用户发现，我之前遗漏

**代码证据** (`hermes_trader/agents/memory.py`):
- L45: `self._peak_daily_pnl: float = 0` — 初始化
- L228: `self._peak_daily_pnl = max(self._peak_daily_pnl, self._daily_pnl)` — 运行时更新
- `flush()` (L97-128): 写入 11 个字段到磁盘 → **`_peak_daily_pnl` 不在其中**
- `load()` (L59-96): 从磁盘加载 11 个字段 → **`_peak_daily_pnl` 不在其中**

**影响**: 重启后 `_peak_daily_pnl = 0`，give-back 保护退化直到当前 session 重新积累峰值。

**修复**: 在 `flush()` 的 dict 中加 `"peakDailyPnl": self._peak_daily_pnl`，在 `load()` 中加 `self._peak_daily_pnl = data.get("peakDailyPnl", 0)`。

---

#### P2-2: cache.py `in_flight` 防惊群为死代码

**状态**: ✅ 确认 — 用户发现，我之前遗漏

**代码证据** (`hermes_trader/client/cache.py`):
- L16: `__slots__ = ('value', 'expiry', 'in_flight')` — 声明
- L22: `self.in_flight: Optional[threading.Event] = None` — 初始化
- L55: `if entry.in_flight is not None:` — 检查（在 `get()` 中）
- **全代码库无任何位置将 `in_flight` 设为非 None**

文档字符串 (L27-30) 描述了完整的 thundering herd 防护逻辑，但实现未完成。

**影响**: 并发 miss 同一 key 时，N 个线程同时发起 API 调用而非只 1 个。

---

#### P2-3: rate_limit.py 漏 `openOrders` 权重

**状态**: ✅ 确认 — 用户发现，我之前遗漏

**代码证据** (`hermes_trader/client/rate_limit.py` L21-35):
```python
_ENDPOINT_WEIGHT = {
    "candleSnapshot": 20,
    "metaAndAssetCtxs": 20,
    "spotMetaAndAssetCtxs": 20,
    "meta": 20,
    "spotMeta": 20,
    "allMids": 2,
    "clearinghouseState": 2,
    "spotClearinghouseState": 2,
    "l2Book": 2,
    "userNonFundingLedgerUpdates": 2,
    "perpDexs": 2,
    "portfolio": 2,
    "userFills": 2,
    # ❌ 缺少 openOrders, userFillsByTime, ...
}
```

`openOrders` 实际权重为 2，但使用默认值 20 → **超估 10 倍**。

**影响**: `openOrders` 请求被限流器过度限制（每次消耗 20 token 而非 2）。

---

#### P2-4: universe.py `get_market_by_coin` 查不到 HIP-3

**状态**: ✅ 确认 — 用户发现，我之前遗漏

**代码证据** (`hermes_trader/client/universe.py`):
```python
# L201: 默认 include_hip3=False
def get_universe(force_refresh: bool = False, include_hip3: bool = False) -> List[Dict[str, Any]]:

# L271: 调用 get_universe() 不传 include_hip3
def get_market_by_coin(coin: str) -> Optional[Dict[str, Any]]:
    for m in get_universe():  # ← 默认 include_hip3=False
        if m["coin"] == coin:
            return m
    return None
```

**影响**: 用 `get_market_by_coin("xyz:AAPL")` 查找 HIP-3 市场永远返回 None。

---

#### P2-5: config_preset.py auto-pick 必然被 legacy 拒绝

**状态**: ✅ 确认 — 用户发现，我之前遗漏

**代码证据** (`scripts/config_preset.py`):
```python
# L161-166: 四个预设全在 LEGACY 集合中
LEGACY_RISK_PRESETS = {
    "small_aggressive",
    "small_conservative",
    "medium_balanced",
    "large_steady",
}

# L181-184: _auto_pick 只能返回这三个
def _auto_pick(equity: float) -> str:
    if equity < 500: return "small_aggressive"      # 在 LEGACY 中
    if equity < 2000: return "medium_balanced"      # 在 LEGACY 中
    return "large_steady"                            # 在 LEGACY 中

# L224: 不带 --allow-legacy-risk-preset 时拒绝
if name in LEGACY_RISK_PRESETS and not allow_legacy_risk_preset:
    print("refusing to apply legacy risk preset ...")
    return 2  # 退出
```

**影响**: `config_preset apply --account-size 500` 的默认路径必然失败。

---

#### P2-6: research.py `equity`/`dex_equity` 死参数

**状态**: ✅ 确认 — 用户发现，我之前遗漏

**代码证据** (`hermes_trader/agents/research.py`):
```python
# L185-196: 函数签名声明了 equity 和 dex_equity 参数
def _build_user_message(
    ...
    equity: float,           # L193
    ...
    dex_equity: Dict[str, float] | None = None,  # L196
    ...
) -> str:

# L577-581: 调用方确实传入了真实值
state = fetch_account_state(user, include_hip3=True)
equity = float(state.get("equity", "0"))
dex_equity = state.get("dex_equity") or {}

# L599-602: 传入 _build_user_message
user_message = _build_user_message(
    ..., equity, open_positions, mode,
    dex_equity=dex_equity, ...
)
```

但 `_build_user_message` 的返回值（L510-528 的 `"\n".join([...])` 列表）中 **`equity` 和 `dex_equity` 从未出现**。注释 L577 说 "so the LLM sees true capital" 但 LLM 从未看到。

**影响**: 每次研究调用白花一次全账户 API 请求（`fetch_account_state`），但 LLM 永远看不到结果。

---

#### P2-7: research.py `float()` 无 None 防护

**状态**: ⚠️ 部分确认 — 用户发现

**代码证据** (`hermes_trader/agents/research.py`):
```python
# L580: equity 来自 state.get("equity", "0")，默认值为字符串 "0"
equity = float(state.get("equity", "0"))  # 如果 HL 返回 null → float(None) → TypeError

# L584: szi 来自 position dict，默认值为 "0"
float(p.get("position", {}).get("szi", "0"))  # 如果 szi 为 null → float(None) → TypeError
```

**影响**: HL API 返回 null 时会 TypeError 崩溃。实际频率取决于 API 是否返回 null（通常 szi 为字符串）。

---

#### P2-8: parse_verdict 多行 JSON 静默 PASS

**状态**: ⚠️ 部分确认 — 用户发现

**代码证据** (`hermes_trader/agents/research.py` L450-526):
```python
# L453-457: 先找最后一行的 JSON
for line in reversed(lines):
    if line.startswith("{") and "verdict" in line and line.endswith("}"):
        json_str = line
        break

# L460-463: 回退 regex — 但只匹配单行 {^{}*$}
match = re.search(r'\{[^{}]*"verdict"[^{}]*\}', ai_text)

# L526: ai_down 标记
"ai_down": not ai_text.strip(),  # 只有 ai_text 完全为空才标记
```

**分析**: 如果 LLM 输出跨多行的 JSON（如 `{\n  "verdict": "LONG"\n}`），第一轮逐行搜索找不到（因为单行不以 `{` 开头且以 `}` 结尾），regex fallback 也不匹配（`[^{}]*` 不跨行）。结果是 verdict 保持默认 `"PASS"`，且 `ai_down = not ai_text.strip()` 为 `False`（因为 ai_text 非空），所以这个 PASS 不会被标记为 AI 失败。

**影响**: 多行 JSON 输出会静默变为 PASS 且不标记 `ai_down`，executor 的 override 逻辑可能将其升级为 LONG。

---

#### P2-9: backtest_portfolio.py 默认值不一致

**状态**: ✅ 确认 — 双方一致

| 文件 | max_loss_pct | max_loss_roe_pct | hard_timeout |
|------|:---:|:---:|:---:|
| backtest_portfolio.py L50/L87 | **0.75** | **6.0** | **1800.0** |
| backtest_logged.py L225 | 2.0 | 40.0 | 180.0 |
| backtest_full.py L148 | 2.0 | 40.0 | 180.0 |
| backtest.py L72 | 2.5 | — | 180 bars |
| config_preset.py (实盘基准) | 2.0~3.0 | — | 180~360 |

**影响**: portfolio 回测用 0.75% 止损 / 30 分钟超时，与实盘 2% / 3 分钟差异巨大 → 回测结果显著偏离。

---

#### P2-10: `detect_regime_at()` / `passes_counter_regime()` 重复实现

**状态**: ✅ 确认 — 双方一致

在 `backtest_logged.py` 和 `backtest_full.py` 中各有一份几乎相同的副本。`backtest_portfolio.py` 从 `backtest_logged.py` 导入复用。

---

#### P2-11: `_is_pid_alive` 重复实现

**状态**: ✅ 确认 — 用户发现

- `hermes_trader/client/lock.py` L36: `def _is_pid_alive(pid) -> bool:`
- `hermes_trader/client/daemon.py` L233: `def _is_pid_alive(pid) -> bool:`

两个实现完全相同（`os.kill(pid, 0)` + try/except）。

---

#### P2-12: 双 Info() 单例

**状态**: ✅ 确认 — 用户发现

- `hermes_trader/client/exchange.py` L114-126: `_info_instance` + `_get_info()` → `Info(skip_ws=True, perp_dexs=...)`
- `hermes_trader/client/hl_client.py` L48-50: `_info_instance` + `_info_lock` → 不同的初始化路径

两个模块各自维护一个 Info() 单例，可能导致重复 meta 抓取和 HIP-3 mids 合并。

---

### P2-13: exchange.py `_is_isolated_only()` 未使用缓存

**状态**: ✅ 确认 — 我独有发现

`_is_isolated_only()` 直接调用 `info.meta()` 和 `info.meta(dex=dex)`，而非使用同文件中的 `_cached_universe()`。burst load 下可能触发 429。

---

### P2-14: lock.py / daemon.py Unix-only API

**状态**: 设计决策 — 双方认可

`fcntl` / `SIGALRM` 仅 Unix 可用。项目设计用于 Linux/Docker 部署，非 bug。若未来跨平台需换 `msvcrt.locking` / 线程 timeout 方案。

---

## 争议项: dashboard.py L238 `/100` bug

### 最终复核结论: ❌ 排除 — 代码中不存在 `/ 100.0`

**验证方法**: 对 GitHub 最新版（commit `e9dda9b`）执行以下搜索:

| 搜索 | 范围 | 结果 |
|------|------|------|
| `grep '/ 100\.0'` | dashboard.py | **0 匹配** |
| `grep 'spot_pct.*leverage.*100'` | dashboard.py | **0 匹配**（仅 L234 的 `* 100` 乘法） |
| `grep '/ 100\.0'` | 全项目所有 .py | **0 匹配** in dashboard.py（17 匹配全在 scripts/） |
| `grep 'roe_pct\|margin_used'` | dashboard.py | 确认存在（L219/L236/L238/L260） |
| `git log --all -p` 搜索 `/ 100.0` | 全 git 历史 | dashboard.py 中 **从未出现** |
| `diff --strip-trailing-cr` GitHub vs 本地 | dashboard.py | **零差异** |

**实际 L238 代码** (GitHub HEAD, `git show HEAD:hermes_trader/dashboard.py | sed -n '238p'`):
```python
roe_pct = (unrealized_usd / margin_used * 100) if margin_used > 0 else spot_pct * leverage
```

**计算验证** (5% 涨幅 + 10x 杠杆):
- Open 主路径: `$5 / $10 * 100` = **50** → 前端 L1134 显示 `"50.0%"` ✓
- Open 回退: `5 * 10` = **50** → 前端显示 `"50.0%"` ✓  
- Closed (L323): `5 * 10` = **50** → 显示 `"50.0%"` ✓
- **三处一致，无矛盾**

**用户引用的行号与实际内容对比**:
| 用户引用 | 实际内容 |
|----------|----------|
| L238: `"unrealized_pct": spot_pct * leverage / 100.0` | L238: `roe_pct = (unrealized_usd / margin_used * 100) if margin_used > 0 else spot_pct * leverage` |
| L276: closed-trades 无 /100 | L276: docstring (`for older events...`) |
| L942: 前端 `${p.unrealized_pct.toFixed(1)}%` | L1134: `${p.unrealized_pct.toFixed(1)}%` |

**验证建议**: 在终端执行以下命令直接验证:
```bash
sed -n '238p' hermes_trader/dashboard.py
# 或
git show HEAD:hermes_trader/dashboard.py | sed -n '238p'
```

**结论**: 当前 GitHub 和本地代码中均不存在 `/ 100.0`。`* 100` 是将比率转为百分数的乘法（`unrealized_usd / margin_used` 得到小数比率，`* 100` 转为百分比数字），而非除法。Open positions 和 closed trades 的计算口径一致。

---

## 冗余/结构性问题汇总

### 1. scripts/ 层大面积复制粘贴

- **DSL 两阶段止损**: 4+ 份独立实现（backtest.py / backtest_full.py / backtest_logged.py / backtest_portfolio.py），参数与分支各有差异
- **PnL 统计、regime 判定、candle 解析、counter-regime 判定、候选准入状态机**: 多脚本逐字复制
- **backtest_logged.py 被当作库 import** 并读写其模块级全局（backtest_portfolio、strategy_grid_search）→ 高危耦合

### 2. 重复的工具函数

- `_is_pid_alive` 在 lock.py 和 daemon.py 各一份
- 双 Info() 单例（exchange.py + hl_client.py）
- 重复 meta 抓取、重复 HIP-3 mids 合并
- research.py 8 处重复的"触发器命中判定"模式
- 各信号模块（news/gex/short_volume/crypto_whale）重复的 TTL+CACHE 模板
- dashboard/server/operator 5+ 处重复解析 asset_positions

---

## 代码质量亮点

1. **测试隔离设计**: conftest.py 强制重定向状态文件，注释记录了 2026-06-15 的真实事故
2. **Cross-process 位置快照**: 解决 429 竞争
3. **Prometheus metrics**: 网络隔离（只 bind 内部端口）
4. **退化读取过滤**: 防 kill-switch 误触发（partial-dex degraded read）
5. **MCP stub 工具**: 39 个未实现工具返回明确 `not_implemented` 错误而非假数据
6. **phase3_replay.py**: 唯一正确使用 live DSLTracker 的 backtest 脚本
7. **config_preset.py**: LEGACY_RISK_PRESETS 安全防护（虽然存在 auto-pick 矛盾）
8. **memory.py flush() guard**: 防止未加载的进程截断磁盘数据

---

## 修复优先级

| 优先级 | Bug | 修复时间 | 风险 |
|:---:|------|:---:|:---:|
| 1 | P1-1 daemon SIGALRM | 5 min | 卡死无超时 |
| 2 | P1-3 PID 路径 | 5 min | 孤儿进程 |
| 3 | P1-2 parallel 阻塞 | 15 min | 整批锁死 |
| 4 | P1-5 portfolio hard_timeout | 15 min | 回测失真 |
| 5 | P2-1 peak_daily_pnl | 5 min | give-back 退化 |
| 6 | P2-5 config_preset | 10 min | 功能落空 |
| 7 | P2-6 research 死参数 | 10 min | 白花 API |
| 8 | P2-4 universe HIP-3 | 5 min | 查不到市场 |
| 9 | P2-3 rate_limit | 5 min | 过度限流 |
| 10 | P2-2 cache in_flight | 30 min | 防惊群失效 |
| 11 | P1-4 DSL 统一 | 2-4h | 维护风险 |
| 12 | P2-9 默认值统一 | 15 min | 回测一致性 |
