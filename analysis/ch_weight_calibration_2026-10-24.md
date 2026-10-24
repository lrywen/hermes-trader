# 权重 × min_composite 联合标定反事实回测 — 2026-10-24

回答 v11 评估 §2.1 留下的唯一"需回测确认"：`trigger_weights` 把
`trend_strength 0.55→0.30`、新增 `uptrend_momentum 0→0.20` 之后，
`min_composite` 仍停在 **45** 没动——这次权重 promote 是不是在静默掐断原本能进场的信号？
被掐的信号是好是坏？

纯离线、PIT、确定性，零交易风险，不改任何生产配置。

## 1. 方法（口径先讲清楚）

- 宇宙：真实 taker 成交 top40 币 + 近期 runner_gate 被拦的 REZ/ZORA/GRAM/INIT，共 **44 币**
  （覆盖 155 笔真实成交的 95%）。
- 数据：近 32 天 5m（主扫描，~5000/币）+ 15m + 1h，经 `historical_candles` 不可变 bar 缓存
  PIT 拉取；1h 用 trailing 100 CLOSED bar warmup。
- 每个 closed 5m bar（每 6 个取 1 个 = 30min 网格，共评估 **26,699 个有点燃触发的 bar**）：
  - `triggers.build_hits(...)` 只跑**一次**。hits 与权重无关（权重只影响分母），
    所以 OLD/NEW 两套权重打在**完全相同的 fired triggers / scores** 上，是干净的配对反事实。
  - OLD 权重：`trendStrength=0.55, uptrend/downtrendMomentum=0`（promote 前）
  - NEW 权重：生产现值 `trendStrength=0.30, uptrend/downtrendMomentum=0.20`
  - 两套**阈值完全相同**（只隔离权重效应；weight_shadow 里那个 3bar/3% 动量候选**刻意不混入**）。
- 前瞻收益：信号点后 15m K线的 4h/12h/24h **做多**收益，3x，扣 taker 往返 9bp；另算 24h MFE/MAE。
  方向用 long 结构的 composite（对应生产 `allow_shorts=false` 现状）。
- regime：复刻生产横截面语义——**BTC proxy** trailing 100 根 1h `classify_candles`，全市场共用。

**关键口径警告（决定结论能走多远）**：这是**机械 TA edge**（与 backtest/signals 的
heuristic 口径一致），**没有 LLM verdict、没有 confidence 0.6 门、没有 late_chase、
没有冷却/槽位互斥、没有 aligned-conf bar**。因此它回答的是"被掐的触发信号本身好不好"，
**不是**"放行后账户就赚这么多"。下游 AI/执行门仍会再筛。

脚本（离线，不进生产镜像）：`ch_weight_calib_fetch.py`（拉数）、
`ch_weight_calib.py`（反事实）、`ch_weight_calib_report.py`（聚合）。

## 2. 主结果：promote 把过闸量砍了 74%

| 象限（对 gate=45） | 数量 | 占点燃 bar |
|---|---:|---:|
| keep_pass（新旧都过） | 24 | 0.1% |
| **culled（旧过、新被掐）** | **76** | **0.3%** |
| new_pass（只有新过） | 2 | 0.0% |
| keep_block（新旧都不过） | 26,597 | 99.6% |

- **OLD 权重过 45 = 100 个信号；NEW 权重过 45 = 26 个。净变化 −74（−74%）。**
- uptrend_momentum 新增的 0.20 几乎没补偿：new_pass 只有 2 个（它是 trend 家族内的替代腿，
  在已点燃趋势的 bar 上与 trendStrength 共线，拉不回被砍掉的分数）。
- 这把 v11 的"REZ 单点可疑"升级为**系统性结论**：与 runner_gate_shadow 现场一致——
  最近 15 条结构化 gate 记录里 14 条被拒、composite 全部 <45（最高 33），与这里 NEW 过线极少吻合。

## 3. 被掐的信号是好是坏：不比保留的差（甚至略好），但方差大

前瞻做多 ROE（3x、净 9bp）：

| 组 | n | fwd4h med/win | fwd12h med/win | fwd24 med/mean/win | MFE24 med |
|---|---:|---|---|---|---:|
| keep_pass（保留） | 24 | +0.38 / 54% | +2.07 / 63% | **+2.02** / +7.04 / 56% | +4.90 |
| **culled（被掐）** | 76 | +0.91 / 61% | +2.17 / 64% | **+3.65** / +2.00 / **65%** | +3.64 |

- 被掐组 fwd24 **中位 +3.65%、胜率 65%**，不劣于、点估计还略高于保留组（+2.02 / 56%）；
  98.7% 的被掐信号 24h 内曾有正 MFE。
- **但分布极宽**：fwd24 p10=−19%、p90=+19%；最大赢家 CASHCAT +46 抬高均值，
  最大亏损 INJ −30 / CASHCAT −35。**应看中位和胜率，不要看均值。**
- 触发签名高度集中：绝大多数是 `pctMoveSpike + volumeSpike + trendStrength`
  （±breakout/volumeBuildup1h）——即"放量趋势突破"，正是 trendStrength 权重腰斩直接打击的形态。
- 集中在 21 个币（ONDO/VVV/REZ/XPL/INJ…），非单一币操纵。

**小结论：新权重不是在识别并剔除坏信号。它无差别地把通过量压到 1/4，
而被压掉的那批在原始触发层面质量与保留批相当。**

## 4. 唯一真正有判别力的维度是 regime——而权重 promote 完全没碰它

culled 组按 BTC-proxy regime 拆 fwd24：

| regime | n | fwd24 中位 | 胜率 |
|---|---:|---:|---:|
| **down** | 31 | **+10.71%** | **90%** |
| up | 27 | **−4.38%** | 44% |
| chop | 11 | −1.28% | 45% |
| neutral | 2 | −1.31 | 50% |

- 同样的模式在 keep_pass 组复现（down med +9.1/win85%，up med −9.4/win0%）——
  **两组同构**，说明这是这段窗口 long 信号的真实 regime 结构，不是 culled 组的偶然：
  **BTC down 之后的超跌小币反弹做多最赚；BTC up 里追高做多最亏。**
  与此前历史 B 类结论"BTC-up 入场 fwd24 −3.16 最差"以及 v11 的 mover/追高发现一致。
- 含义：(a) 当前 regime overlay 把 down 也当 trend_ride 收紧，可能放掉了 down→反弹这条最肥的
  long 腿（需另立专题，不在本回测范围）；(b) 真正该加的是**按 regime 的入场门**，
  而不是无 regime 区分的 trendStrength 降权。

## 5. 时间漂移（重要的反面证据）

culled 组 fwd24 按周：week41（10/初）med +5.8 win80%；week42 med +2.3 win67%；
**week43（10/19 起）med −3.3 win47%**。

最近一周所有 long 触发信号转负——恰好覆盖实盘 10-22 后 0 成交的窗口。
即"最近不进场"在近一周客观上没错过什么好 long；但这是 regime/行情转弱，
**不构成"权重 promote 合理"的证据**——它在 week41/42 同样掐掉了大量正期望信号。

## 6. 门槛该怎么动：不是等比例 43.78，方向是降到 ~40

- Σw 只从 1.85 微降到 1.80，等比重标定 `G' = 45×1.80/1.85 = 43.78`，
  只能救回 **3/76** 个被掐信号。**掐断来自 trendStrength 分子权重腰斩，不是归一化分母，
  所以等比调门槛基本无效。**
- 在 NEW 权重下对 gate 扫描（仅含至少一套权重过线的样本，3x、净费）：

| NEW gate | n | fwd24 中位 | 胜率 |
|---:|---:|---:|---:|
| 45（现值） | 24 | +1.9% | 54% |
| **40** | 38 | **+2.8%** | **61%** |
| 35 | 81 | +3.7% | 60% |
| 30 | 95 | +2.3% | 62% |

35–45 分这批被现门槛挡掉的信号，中位/胜率反而更高；到 30 分边际质量开始回落（+2.3），
说明真垃圾区在更低分。**45 在新权重下是被错误抬高的门，~40 是数据指向的重定标点。**

## 7. 裁决与建议

1. **v11 §2.1 成立且被强化**：权重 0.55→0.30 与 min_composite=45 未联合标定，
   事实是把机械触发的过闸量无差别砍了 74%，被砍信号质量不劣于保留信号。
2. **不应直接回滚权重**：新权重在"防 up-regime 追高"上方向不算错，且下游还有
   AI/执行门；机械 TA 的正收益不保证端到端可盈利（高方差、无 LLM、无成本外冲击）。
3. **推荐（仍是 SHADOW 优先，B 类纪律不变）**：
   - **第一步（record-only，零风险）**：把 `min_composite` 的反事实门 40（对照 45）
     做成 shadow 双门计数——在每个点燃 bar 同时记 `passes45 / passes40`，
     用真实 tagged 信号+AI verdict 累积 2–4 周，验证"40 分那批在 AI 门之后是否仍更优"。
   - **第二步（更大 upside，另立）**：入场侧加 **regime 条件门**（up 追高收紧、
     down 反弹放松），这是本回测里判别力最强的维度，但须独立专题 + shadow 验证，
     不与门槛调整打包。
4. **不要**据此直接改 LIVE 门或回滚权重：n=100/76 是单一 32 天窗口、30min 采样、
   机械口径；week43 已显示 regime 转弱，需真实 AI-gated 样本确认。

## 8. 复现

```
# 容器内（有数据预算；host 匿名 budget 会被限流）
HERMES_HIST_CANDLE_CACHE=/data/ch_hist_candles.json \
CH_LIVE_CONFIG=/data/.agent-config.json CH_BOOK=/tmp/shadow_book_1024.json \
CH_DAYS=32 CH_FETCH_SLEEP=0.25 \
  python3 /tmp/ch_weight_calib_fetch.py
CH_WT_STEP=6 CH_WT_OUT=/data/ch_weight_calib.jsonl \
  python3 /tmp/ch_weight_calib.py
python3 /tmp/ch_weight_calib_report.py
```

局限清单：30min 采样（STEP=6）会漏掉短脉冲；无 AI verdict/confidence/late_chase/
冷却/槽位；long-only；单一 32 天窗口；fwd 用固定时间平仓、不含 DSL 退出路径
（MFE/MAE 是 bar 级上界/下界）；regime 用 BTC proxy 横截面（生产口径）。
