# M-4 结果：Binance bookDepth 近 mid 深度失衡 — H1 CLOSED（no-go）

- 日期：2026-09-30
- 对应预注册：[m4_bookdepth_imbalance_prereg_2026-09-30.md](m4_bookdepth_imbalance_prereg_2026-09-30.md)
- 数据：Binance U本位 bookDepth（30s帧，±0.2/1/2%）+ 5m klines；BTC+ETH，
  2026-01-01→06-28（179天），切分 2026-03-31；seed 20260930
- 脚本：`scripts/binance_bookdepth_imbalance_replay.py`

---

## §1 唯一主假设 H1 裁决：**CLOSED / no-go**

> ±0.2% 近 mid 的 bid/ask 挂单名义额失衡，对未来 5m mid 收益**没有可覆盖成本的预测力**。

### 1.1 测试段（89 天，49,556 个 5m 桶）实测

| 项 | 值 |
|---|---|
| 训练 n / 冻结阈值 | 44,344；k_lo=−0.1821，k_hi=0.1756 |
| go（执行） | **8,965（18.1%）** |
| middle（不交易） | 40,591 |
| **gross 方向收益（side×ret）** | **+0.07 bps**（预测内容≈随机） |
| 全测试均值 / middle 均值 | −0.07 / −0.10 bps |
| **NET go（扣11bp往返）** | **−10.93 bps/笔**，CI [−11.25, −10.60] |

### 1.2 门槛

| 规则 | 结果 |
|---|---|
| R1 净 CI 下界 > 0 | ❌（[−11.25, …]） |
| R2 方向一致（side×ret>0） | ✅（+0.07，量可忽略） |
| R4 middle 不优于 go gross | ✅ |
| R5 交易数 ≥ 300 | ✅（8,965） |
| R6 净均值 ≥ 3 bps | ❌（−10.93） |

---

## §2 解读

1. **毛利仅 +0.07 bps**：近 mid 深度不对称对5m方向的预测力与抛硬币无异。
   净 −10.93bps 恰好≈一整个 round-trip 成本（11bp），与 M-2（Binance tape CVD，
   gross≈−0.04bp、净−11.04bp）几乎完全同构。
2. middle 带实际均值 −0.10bp，与 go 带无实质差异 → 信号**没有在区分好坏时段**。
3. 这是**第七类**被独立证伪的公开信号。结论再次收敛到元事实：
   **凡公开、即时、可免费回溯的数据，无论 K线 / 逐笔 tape / 深度总量，edge 都已被套利到零。**

## §3 对 M-1 的含义（严格边界）

- 本数据非逐档 L2，证伪的是"深度总量不对称"，**不能**据此判定 M-1 的真 OFI 失败。
- 但它提示：即便未来拿到第三方历史 L2，若其同样公开可买，需以很低预期对待；
  真正的 edge 更可能在"不可即时回溯"的私有/实时微结构（M-1，2026-10-28 检验）。

## §4 复现

```bash
cd /home/ldy/hermes-trader
PYTHONPATH=. python3 scripts/binance_bookdepth_imbalance_replay.py
# summary -> logs/m4_bookdepth_result.json ; console -> logs/m4_run.log
```

## §5 变更记录

| 日期 | 变更 |
|---|---|
| 2026-09-30 | 初版结果：H1 CLOSED |
