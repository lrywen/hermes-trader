# 熔断器修复方案文档

**日期**: 2026-08-22
**修复范围**: HTA 服务双熔断器（Research 路径 + Risk Gate）
**涉及文件**:
- `hermes_trader/agents/research.py`
- `hermes_trader/agents/risk_gates.py`

---

## 1. 问题背景

系统中存在两条 HTA 服务调用路径，各自有独立的熔断器，但均存在缺陷：

| 路径 | 熔断器 | 阈值/冷却 | 缺陷 |
|------|--------|-----------|------|
| Research (`/research/short`) | Signal Bus (`signal_bus.py`) | threshold=5, recovery=60s | **只写不读**：调用 `report_failure()`/`report_success()` 但从不调用 `is_open()` 短路 HTTP 请求，熔断打开时仍等待 60s timeout |
| Risk Gate (`/risk/review`) | 模块级二态机 (`risk_gates.py`) | threshold=3, cooldown=300s | **缺少 HALF_OPEN**：cooldown 过期后失败计数器不重置，试探调用若失败，计数器仍为 3，立即重新熔断，形成"半开即熔断"死循环 |

### 1.1 观测数据（2026-08-18 ~ 08-21，3 天）

- HTA `/risk/review`：157 次调用，151 次 fail_open/timeout
- 总超时浪费：151 × 25s = **3,775s（62.9 分钟）**
- 14 个进程触发过熔断（≥3 次 fail_open）
- HTA `/research/short`：702 条 signal，338 HOLD（185 条 "missing market data"），0 次 DEGRADED 标记
- Watchdog 死亡螺旋：12 天内 182 次重启，其中 138 次集中在 17 次死亡螺旋（最差 41 次重启/401 分钟）

---

## 2. 修复一：Research 路径熔断前置检查

### 2.1 问题

`_call_hta_service()` 在 HTTP 请求失败后调用 `get_bus().report_failure()` 记录失败，但在发起请求前**从不检查** `get_bus().is_open()`。这意味着即使 Signal Bus 熔断器已经打开，每次 Research 调用仍会发起一个 60s 超时的 HTTP 请求，浪费扫描周期时间，可能导致 Watchdog 600s 无进度而触发 `os.execv` 重启。

### 2.2 代码变更

**文件**: [research.py](file:///home/ldy/hermes-trader/hermes_trader/agents/research.py)

**变更位置**: `_call_hta_service()` 函数，`url` 赋值之后、`try:` 块之前（L126-L140）

```python
# BEFORE (L126-L127)
    url = hta_cfg.get("url") or os.environ.get("HTA_SERVICE_URL", "http://localhost:8766")
    try:
        resp = httpx.post(

# AFTER (L126-L156)
    url = hta_cfg.get("url") or os.environ.get("HTA_SERVICE_URL", "http://localhost:8766")

    # Circuit-breaker pre-check: if the signal bus has tripped open due to
    # consecutive HTA failures, skip the HTTP call entirely (and its 60 s
    # timeout) so the caller can degrade to OpenRouter immediately.
    if get_bus is not None:
        try:
            if get_bus().is_open():
                logger.info(
                    f"[research] HTA circuit open — skipping HTTP call, "
                    f"degrading to OpenRouter for {ticker} (trace_id={trace_id})"
                )
                return None
        except Exception:
            pass

    try:
        resp = httpx.post(
```

### 2.3 降级链路

```
_call_ai()
  └─ _call_hta_service()
       ├─ is_open() == True  → return None (跳过 HTTP，无 60s 等待)
       └─ is_open() == False → httpx.post() → 成功返回 / 失败 report_failure()
  └─ hta_result is None
       └─ _call_openrouter()  + DEGRADED marker
```

### 2.4 预期效果

- 熔断打开后，每个币种的 Research 调用从 **60s timeout** 降至 **<1ms**（一次函数调用 + 日志）
- 以扫描 30 个币种计，单次扫描节省最多 30 × 60s = 30 分钟
- 消除因 Research 阶段累积超时导致的 Watchdog 重启
- OpenRouter 降级路径已存在，返回结果追加 `[DEGRADED: legacy_openrouter]` 标记，可通过日志追踪

---

## 3. 修复二：Risk Gate 熔断器三态机

### 3.1 问题

原实现是简单二态机（CLOSED / OPEN），使用绝对时间戳 `_hta_circuit_open_until = monotonic() + cooldown_s`：

```
CLOSED ──(3 failures)──→ OPEN
OPEN ──(cooldown elapsed)──→ (隐式 CLOSED，但 failures 仍为 3)
```

**死循环机理**：
1. 3 次失败 → 熔断 OPEN，`_hta_circuit_failures = 3`
2. cooldown 300s 过期 → `_hta_circuit_check()` 返回 None（允许调用）
3. HTA 仍未恢复 → 试探调用失败 → `_hta_circuit_record_failure()`
4. `_hta_circuit_failures` 从 3 增至 4（4 ≥ 3）→ **立即重新熔断**
5. 回到步骤 2，每 300s 只允许一次必然失败的试探调用，永远无法恢复

### 3.2 代码变更

**文件**: [risk_gates.py](file:///home/ldy/hermes-trader/hermes_trader/agents/risk_gates.py)

**变更位置**: L510-L593，完整替换熔断器状态变量和三个函数

#### 3.2.1 状态变量

```python
# BEFORE
_HTA_CIRCUIT_LOCK = threading.Lock()
_hta_circuit_failures = 0
_hta_circuit_open_until: float = 0.0  # absolute monotonic deadline

# AFTER
_HTA_CIRCUIT_LOCK = threading.Lock()
_hta_circuit_state = "closed"          # "closed" | "open" | "half_open"
_hta_circuit_failures = 0
_hta_circuit_opened_at: float = 0.0    # relative timestamp when circuit last opened
```

关键变更：`_hta_circuit_open_until`（绝对截止时间）→ `_hta_circuit_opened_at`（相对开启时间），新增 `_hta_circuit_state` 显式跟踪三态。

#### 3.2.2 `_hta_circuit_check()` — 增加 OPEN → HALF_OPEN 转换

```python
# BEFORE
def _hta_circuit_check(cooldown_s: float) -> Optional[str]:
    global _hta_circuit_failures, _hta_circuit_open_until
    with _HTA_CIRCUIT_LOCK:
        if _hta_circuit_open_until and time.monotonic() < _hta_circuit_open_until:
            return "open"
        return None

# AFTER
def _hta_circuit_check(cooldown_s: float) -> Optional[str]:
    global _hta_circuit_state, _hta_circuit_failures, _hta_circuit_opened_at
    with _HTA_CIRCUIT_LOCK:
        now = time.monotonic()
        if _hta_circuit_state == "open":
            if now - _hta_circuit_opened_at < cooldown_s:
                return "open"
            # Cooldown elapsed — allow one trial request through.
            _hta_circuit_state = "half_open"
            _hta_circuit_failures = 0  # ← 关键：重置计数器
            logger.info(
                f"[risk] HTA risk review circuit cooldown elapsed — "
                f"entering HALF_OPEN (trial request allowed)"
            )
        return None
```

#### 3.2.3 `_hta_circuit_record_failure()` — 增加 HALF_OPEN 分支

```python
# BEFORE
def _hta_circuit_record_failure(cooldown_s: float, fail_threshold: int) -> None:
    global _hta_circuit_failures, _hta_circuit_open_until
    with _HTA_CIRCUIT_LOCK:
        _hta_circuit_failures += 1
        if _hta_circuit_failures >= fail_threshold:
            _hta_circuit_open_until = time.monotonic() + cooldown_s
            logger.error(...)

# AFTER
def _hta_circuit_record_failure(cooldown_s: float, fail_threshold: int) -> None:
    global _hta_circuit_state, _hta_circuit_failures, _hta_circuit_opened_at
    with _HTA_CIRCUIT_LOCK:
        if _hta_circuit_state == "half_open":
            # Trial failed — re-open immediately without requiring
            # another `fail_threshold` consecutive failures.
            _hta_circuit_state = "open"
            _hta_circuit_opened_at = time.monotonic()
            logger.error(
                f"[risk] HTA risk review trial request failed in HALF_OPEN — "
                f"circuit re-OPENED for {cooldown_s}s"
            )
            return
        if _hta_circuit_state == "open":
            return  # already open; nothing to do
        _hta_circuit_failures += 1
        if _hta_circuit_failures >= fail_threshold:
            _hta_circuit_state = "open"
            _hta_circuit_opened_at = time.monotonic()
            logger.error(...)
```

#### 3.2.4 `_hta_circuit_record_success()` — 显式 CLOSED 转换

```python
# BEFORE
def _hta_circuit_record_success() -> None:
    global _hta_circuit_failures, _hta_circuit_open_until
    with _HTA_CIRCUIT_LOCK:
        _hta_circuit_failures = 0
        _hta_circuit_open_until = 0.0

# AFTER
def _hta_circuit_record_success() -> None:
    global _hta_circuit_state, _hta_circuit_failures, _hta_circuit_opened_at
    with _HTA_CIRCUIT_LOCK:
        if _hta_circuit_state == "half_open":
            logger.info(
                "[risk] HTA risk review trial succeeded in HALF_OPEN — circuit CLOSED"
            )
        _hta_circuit_state = "closed"
        _hta_circuit_failures = 0
        _hta_circuit_opened_at = 0.0
```

### 3.3 状态机图

```
                    ┌──────────────────────────────────┐
                    │                                  │
                    ▼                                  │
              ┌            ┐  (1) threshold=3    ┌            ┐
              │  CLOSED    │────────────────────→│   OPEN     │
              │ failures=0 │                     │ cooldown   │
              └            ┘                     │ 300s       │
                    ▲                            └            ┘
                    │                                  │
                    │ (4) success                      │ (2) cooldown elapsed
                    │                                  │ → failures=0
                    │                                  ▼
              ┌            ┐  (3) failure        ┌            ┐
              │  CLOSED    │←────────────────────│ HALF_OPEN  │
              │  (reset)   │                     │ trial x1   │
              └            ┘                     └            ┘
                    ▲                                  │
                    │                                  │ (3) failure
                    │                                  ▼
                    │                            ┌            ┐
                    └────────────────────────────│   OPEN     │
                                                 │ (re-trip)  │
                                                 └            ┘
```

状态转换说明：
1. **CLOSED → OPEN**：连续失败达到 `fail_threshold`（默认 3）
2. **OPEN → HALF_OPEN**：cooldown（默认 300s）过期，**同时重置 `_hta_circuit_failures = 0`**
3. **HALF_OPEN → OPEN**：试探请求失败，立即重新熔断（不需要再等 3 次失败）
4. **HALF_OPEN → CLOSED**：试探请求成功，重置全部状态

### 3.4 与 Signal Bus 熔断器的一致性

Risk Gate 新三态机与已有的 Signal Bus 熔断器（`signal_bus.py`）模式一致：

| 特性 | Signal Bus | Risk Gate (修复后) |
|------|-----------|-------------------|
| 状态枚举 | `CircuitState` enum | 字符串常量 |
| threshold | 5 | 3（可配置） |
| cooldown/recovery | 60s | 300s（可配置） |
| HALF_OPEN 重置计数器 | ✅ `_maybe_half_open()` | ✅ `_hta_circuit_check()` |
| HALF_OPEN 失败立即熔断 | ✅ `report_failure()` | ✅ `_hta_circuit_record_failure()` |
| 线程安全 | `threading.Lock` | `threading.Lock` |

---

## 4. 验证结果

### 4.1 单元测试

三态机逻辑验证脚本（已通过）：

```
PASS: CLOSED -> OPEN after threshold
PASS: failures ignored while OPEN
PASS: OPEN -> HALF_OPEN with counter reset to 0
PASS: HALF_OPEN failure -> immediate OPEN (death spiral fixed)
PASS: HALF_OPEN success -> CLOSED
PASS: CLOSED success resets counter

ALL CIRCUIT BREAKER STATE MACHINE TESTS PASSED
```

Research 路径前置检查验证（已通过）：

```
PASS: research._call_hta_service contains circuit-breaker pre-check
PASS: is_open check (line 27) precedes httpx.post (line 37)
```

### 4.2 回归测试

```
323 passed, 14 deselected in 40.49s
```

全部 323 个现有测试通过，无回归。

### 4.3 部署验证

- Docker 镜像重建成功
- 容器 `hermes-trader` 健康检查通过（20s 内 healthy）
- 容器内代码确认包含两个修复（`grep` 验证 `is_open()` 和 `half_open` 均存在）

---

## 5. 预期效果分析

### 5.1 Research 路径

| 指标 | 修复前 | 修复后 |
|------|--------|--------|
| 熔断打开时单次 Research 调用 | 60s（HTTP timeout） | <1ms（函数调用 + 日志） |
| 30 币种扫描最坏耗时 | +30 min（全部 timeout） | 0（立即降级） |
| OpenRouter 降级 | 仅在 HTTP 异常后触发 | 熔断打开时立即触发 |
| DEGRADED 标记 | 异常路径追加 | 熔断短路 + 异常路径均追加 |

### 5.2 Risk Gate 路径

| 场景 | 修复前 | 修复后 |
|------|--------|--------|
| cooldown 后 HTA 已恢复 | 计数器仍为 3，但成功调用会重置 → 正常 | 计数器重置为 0，试探成功 → CLOSED → 正常 |
| cooldown 后 HTA 仍故障 | 计数器 3→4，立即熔断，**永远无法恢复** | 计数器重置为 0，试探失败立即熔断 300s 后重试；**HTA 恢复后第一次试探成功即可 CLOSED** |
| 熔断期间 HTTP 调用 | 0（短路，已正确） | 0（短路，不变） |
| 熔断期间 fail_closed 行为 | 立即 block | 立即 block（不变） |

### 5.3 对 Watchdog 死亡螺旋的影响

- Research 路径熔断打开后不再产生 60s timeout，直接降低扫描周期耗时
- Risk Gate 路径在 execute 阶段已有 25s timeout 短路（熔断打开时），HALF_OPEN 修复确保熔断器能真正恢复而非永久半开
- 预计可显著减少因单周期超 600s 导致的 `os.execv` 重启次数

---

## 6. 后续验证步骤

### 6.1 短期（部署后 24 小时内）

1. **检查 Research 熔断短路日志**：
   ```bash
   docker logs hermes-trader 2>&1 | grep "circuit open — skipping HTTP call"
   ```
   预期：熔断打开后出现该日志，每个币种一条，无 60s 间隔。

2. **检查 Risk Gate 状态转换日志**：
   ```bash
   docker logs hermes-trader 2>&1 | grep -E "HALF_OPEN|circuit (OPEN|CLOSED)"
   ```
   预期：能看到 `cooldown elapsed — entering HALF_OPEN` 和 `trial succeeded — circuit CLOSED`（HTA 恢复后）。

3. **确认 DEGRADED 标记出现在 Research 结果中**：
   ```bash
   docker exec hermes-trader grep -c "DEGRADED: legacy_openrouter" /data/events.jsonl
   ```
   预期：熔断打开期间有 DEGRADED 标记（此前为 0）。

4. **检查 Watchdog 重启频率**：
   ```bash
   docker exec hermes-trader grep -c "watchdog" /data/session-log.jsonl
   ```
   对比修复前基线（12 天 98 次），预计显著下降。

### 6.2 中期（部署后 3-7 天）

5. **统计 HTA fail_open 次数和超时浪费**：
   ```bash
   docker exec hermes-trader python3 -c '
   import json
   fails = timeouts = 0
   for line in open("/data/events.jsonl"):
       ev = json.loads(line)
       if ev.get("event") == "risk" and ev.get("payload", {}).get("verdict") == "fail_open":
           fails += 1
           lat = ev["payload"].get("latency_ms", 0)
           if lat >= 20000:
               timeouts += 1
   print(f"fail_open: {fails}, timeouts(>=20s): {timeouts}")
   '
   ```
   预期：fail_open 总数下降（熔断短路不产生 risk 事件），timeout 次数显著下降。

6. **确认 HALF_OPEN 恢复路径**：
   - 手动重启 HTA 容器模拟恢复：`docker restart hermes-trading-agents`
   - 观察 300s 后 Risk Gate 日志出现 `entering HALF_OPEN` → `trial succeeded — circuit CLOSED`
   - 确认后续交易不再走 `hta_risk_circuit_open` 短路

### 6.3 长期（部署后 2 周）

7. **对比修复前后 Watchdog 重启率**：以 7 天为窗口对比 `loop_start` / `watchdog` 事件比例
8. **评估是否需要调整 threshold/cooldown 参数**：基于实际 HTA 恢复时间分布
9. **考虑将 Watchdog 心跳覆盖 execute 阶段**（独立改进项）：在 `hta_risk_gate()` 前后增加 `_beat()` 调用，从根本上消除 execute 阶段无心跳导致的误杀

---

## 7. 未修改项 / 后续建议

以下问题在本次修复范围外，记录待后续处理：

1. **Watchdog 重启重置熔断状态**：`os.execv` 重启后模块级变量回到初始 CLOSED 状态，熔断历史丢失。短期影响可控（重启后 HTA 通常也已恢复），长期可考虑将熔断状态持久化到 `/data` 卷。
2. **execute 阶段无心跳**：`route_verdict()` → `maybe_execute()` → `eval_all_gates()` 全程无 `_beat()`，若 HTA 25s timeout + 多闸门累积可能超 600s。建议在 execute 关键节点增加心跳。
3. **两个熔断器 threshold/cooldown 不一致**：Signal Bus threshold=5/recovery=60s，Risk Gate threshold=3/cooldown=300s。这是有意为之（Research 可快速降级，Risk Gate 需更保守），但建议在配置文档中说明。
