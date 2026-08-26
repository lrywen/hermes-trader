# DSL 止损 / Tracker 手动干预操作手册

适用对象：运行在 Docker 容器 `hermes-trader` 中的 Hermes 交易引擎。
所有涉及真实下单/改单的操作都**先 dry-run**，确认无误再加 `--execute`。

---

## 0. 容器与状态文件位置

| 项目 | 值 |
|---|---|
| 容器名 | `hermes-trader` |
| Compose 目录 | `/home/ldy/hermes-deploy` |
| DSL 状态文件（容器内） | `/data/.dsl-state.json`（由 `HERMES_DSL_STATE_FILE` 指定） |
| 状态文件持久化 | docker volume `hermes_data`（重建容器不丢失） |
| 代码模块 | `hermes_trader.agents.dsl_exit` / `hermes_trader.agents.executor` |

状态文件顶层结构：`{ "version": 2, "saved_at": <epoch ms>, "positions": [ {coin, side, leverage, entry_px, peak_px, last_floor, consecutive_breaches, sl_oid, sl_px, sl_size, tp_oid, tp_px, policy} ] }`

### 进入容器执行 Python 的通用前缀
```bash
docker exec -it hermes-trader python3
```
脚本内需要先加载状态：
```python
from hermes_trader.agents import dsl_exit
dsl_exit.load_state(force=True)          # 强制从磁盘重新加载
print(list(dsl_exit._active_positions)) # 形如 ['ETH_short']
```

---

## 1. 只读检查（不改任何东西）

### 1.1 查看所有 tracker 的实时 DSL 状态
```bash
docker exec -it hermes-trader python3 -c "
from hermes_trader.agents import dsl_exit
dsl_exit.load_state(force=True)
for k,t in dsl_exit._active_positions.items():
    print(k, 'entry=',t.entry_px,'peak=',t.peak_px,'floor=',t._last_floor,
          'sl_oid=',t.sl_oid,'sl_px=',t.sl_px,'breaches=',t.consecutive_breaches)
"
```

### 1.2 查看交易所侧真实挂单（核对 sl_oid 是否还在）
```bash
curl -s http://localhost:8000/api/hl/portfolio | python3 -m json.tool | head -60
# 或直接查 openOrders：
docker exec -it hermes-trader python3 -c "
from hermes_trader.client import exchange
print(exchange.INFO.post('/info', {'type':'openOrders','user':'<wallet>'}))
"
```

### 1.3 实时观察 SL 移动 / floor 更新日志
```bash
docker logs -f --since 1s hermes-trader 2>&1 | grep -iE --line-buffered 'sl-move|dsl:floor|backfill|sync_exchange'
```
正常触发时会出现：
```
[dsl:floor] ETH short phase=phase2 ... floor=...
[sl-move] ETH short moved exchange SL: old_oid=... new_oid=... floor=... target_sl=...
```

### 1.4 进程资源检查
```bash
docker stats --no-stream hermes-trader
docker inspect -f 'Restarts={{.RestartCount}} Health={{.State.Health.Status}}' hermes-trader
```
健康基线参考：容器内存约 240 MiB / 7.6 GiB；进程为 `python3 -m hermes_trader.server`（web）+ `python3 scripts/trading_loop.py`（循环）+ 一个 `sleep 60`（日报定时器），无僵尸进程。

---

## 2. 手动移动交易所备份 SL（不改 DSL tracker）

唯一现成的 CLI 是 `scripts/verify_batch_modify_sl.py`，直接走 `exchange.modify_sl_trigger`（batchModify，cancel+replace），**不会**自动更新 DSL 状态文件。它始终沿“安全方向”移动（多头 SL 上移、空头 SL 下移）。

```bash
# 1) 永远先 dry-run（默认），看清 old oid / 当前触发价 / 新触发价
docker exec -it hermes-trader python3 scripts/verify_batch_modify_sl.py --coin ETH --bps 20

# 2) 确认无误后真实执行一笔移动
docker exec -it hermes-trader python3 scripts/verify_batch_modify_sl.py --coin ETH --bps 20 --execute
```
- `--bps N`：向安全方向移动 N 个基点（10 = 0.10%）。
- batchModify 会产生**新 oid**。执行后，DSL 侧仍记着旧 oid。

### 执行后让 DSL 侧同步到新 oid（二选一）
1. **等一个 tick（约 15s）**：交易循环下轮 `sync_exchange_sl`/对账会用新 oid（若新价位不满足“只紧不松”，SL 不会再动，但 backfill 不依赖方向）。更稳妥的是手动触发 backfill：
   ```bash
   docker exec -it hermes-trader python3 -c "
   from hermes_trader.agents import dsl_exit
   from hermes_trader.client.hl_client import resolve_user_address
   dsl_exit.load_state(force=True)
   user = resolve_user_address()   # 优先 HYPERLIQUID_MASTER_ADDRESS，否则 HYPERLIQUID_WALLET_ADDRESS
   n = dsl_exit.backfill_brackets_from_exchange(user)
   print('backfilled', n, 'bracket oid(s); user=', user)
   "
   ```
   > 注意：`backfill_brackets_from_exchange` 只填补**空的** oid，不会覆盖已有 oid。若你是手动改了一张已被 DSL 跟踪的 SL，需要按第 4 节直接更新 tracker 字段。

---

## 3. 手动改 DSL tracker 字段（sl_oid / sl_px / peak / floor 等）

没有专用 HTTP/MCP 接口，使用模块公共函数 `set_bracket` 改 bracket 字段并落盘；`peak_px`/`_last_floor` 直接改属性后手动保存。

```bash
docker exec -it hermes-trader python3 -c "
from hermes_trader.agents import dsl_exit
dsl_exit.load_state(force=True)
coin, side = 'ETH', 'short'

# 3a. 更新 bracket（自动写盘）——例如手动移动 SL 后同步新 oid/触发价
dsl_exit.set_bracket(coin, side, sl_oid=1234567890, sl_px=2341.48, sl_size=0.0048)

# 3b. 手动重置 peak 或 floor（直接改属性 + 私有 save）
t = dsl_exit.get_tracker(coin, side)
t.peak_px = 2374.70           # 重置峰值（空头为最低价）
t._last_floor = 2434.07       # 重置 floor
t.consecutive_breaches = 0    # 清零连续穿透计数
dsl_exit._save_state()        # 私有，手动改属性后必须调用才落盘
print('done', t.coin, t.sl_oid, t.sl_px, t.peak_px, t._last_floor)
"
```
注意：
- **正在运行的 trading_loop 进程内存里也持有 tracker**。改完磁盘后，loop 下一轮不会自动 force-reload，它会用内存中的旧对象继续，并在下次 `_save_state` 时**覆盖**你的改动。因此改 tracker 字段建议在**停掉交易循环**时进行（见第 5 节），或接受下一轮自然覆盖的风险。
- `set_bracket` 只接受 `sl_oid/sl_px/sl_size/tp_oid/tp_px` 这五个字段。

---

## 4. 清空 / 重建某个 tracker

### 4.1 平仓后正常注销（推荐）
```bash
curl -X POST http://localhost:8000/api/hl/close-position -H 'Content-Type: application/json' -d '{"coin":"ETH"}'
# executor 平仓路径会自动 deregister_position 并落盘
```

### 4.2 直接从注册表删除某个 tracker
```bash
docker exec -it hermes-trader python3 -c "
from hermes_trader.agents import dsl_exit
dsl_exit.load_state(force=True)
ok = dsl_exit.deregister_position('ETH','short')   # 自动写盘
print('deregistered:', ok)
"
```
> 这只删除 DSL 跟踪，**不会**平掉交易所真实仓位，也**不会**撤掉交易所 SL/TP 挂单。若仓位还在，下一轮 `rehydrate_from_exchange` 可能把 tracker 重新建回来。

### 4.3 清空全部 DSL 状态（核弹选项）
先停容器再操作，避免 loop 覆写：
```bash
cd /home/ldy/hermes-deploy
docker compose stop hermes-trader
# 备份
docker run --rm -v hermes_data:/data alpine cp /data/.dsl-state.json /data/.dsl-state.json.bak
# 清空 positions（保留文件结构）
docker run --rm -v hermes_data:/data alpine sh -c 'echo "{\"version\":2,\"saved_at\":0,\"positions\":[]}" > /data/.dsl-state.json'
docker compose start hermes-trader
```
启动后 `rehydrate_from_exchange` 会按真实持仓重建 tracker，`backfill_brackets_from_exchange` 会尝试从 openOrders 回填 oid。

---

## 5. 重启 / 重建容器

```bash
cd /home/ldy/hermes-deploy
# 仅重启（不加载新代码，状态保留）
docker compose restart hermes-trader
# 代码改动后需要重建镜像
docker compose build hermes-trader && docker compose up -d --force-recreate hermes-trader
# 看健康
sleep 30 && docker inspect -f '{{.State.Health.Status}}' hermes-trader
docker logs hermes-trader 2>&1 | grep -iE 'rehydrated|backfill|error' | tail
```

---

## 6. 故障排查清单

| 现象 | 检查 / 处理 |
|---|---|
| 仓位 sl_oid 为空 | 查启动日志 `[dsl] backfill`；手动跑第 2 节的 backfill；核对 openOrders 返回的单是否 `reduceOnly:true` 且 `coin` 匹配 |
| 有 `[sl-move] ... modify FAILED` | 看下一行 `error=`；多为 batchModify 被拒或 oid 已失效，下轮自动重试；必要时手动 cancel 并重新 place SL |
| floor 没更新 | 确认浮盈是否 ≥ `protect_pct`（ETH 空头为 2.0%）；查 `[dsl:floor]` 日志的 `peak_changed/prev_floor`；floor 只单向棘轮 |
| SL 没跟着动 | 三道护栏可能拦截：只紧不松、min-move 15bps、30s/币节流。属正常，等价格继续有利移动 |
| 改了磁盘文件但被还原 | trading_loop 内存对象下轮写盘覆盖了。按第 3 节提示，停 loop 后改，或用 `set_bracket` 在运行时改 |
| 怀疑内存泄漏 | `docker stats --no-stream`；基线约 240MiB。两个 python 进程分别约 170MB / 100MB；有僵尸进程用 `docker exec` 查 `/proc/[0-9]*/status` 的 State |

---

## 7. 安全红线

1. 任何 `--execute` / `close-position` / 直接写 state 的操作，**先备份** `/data/.dsl-state.json`。
2. `verify_batch_modify_sl.py` 只动单张 SL oid，永不下新单、永不平仓；但仍发送真实签名交易，dry-run 确认后再执行。
3. 直接编辑 state 文件时**先停容器**，否则会被运行中的 loop 覆盖。
4. 不要手改 `version` 字段（当前为 2），否则可能导致加载逻辑跳过或报错。
