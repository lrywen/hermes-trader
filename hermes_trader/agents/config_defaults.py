"""Canonical configuration defaults for the Hermes Trader agent.

Pure data module: the deep-merge base that ``read_agent_config()`` falls back
to when a key is missing from the live ``.agent-config.json``. Extracted from
config_store.py (god-module cleanup 2026-09-27) so the ~1,700-line data
literal lives apart from the config read/write logic. This module MUST NOT
import config_store (it sits below it in the dependency graph); the values are
self-contained nested literals with no computed references.

Keep in sync with .agent-config.json.
"""
from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
CANONICAL_DEFAULTS: dict[str, Any] = {
    "mode": "OFF",
    "enable_crypto": True,
    "enable_hip3": False,
    "equity_fraction_per_trade": 0.2,
    "leverage": 10,
    # F4 (supplemental audit 2026-08-31): keys that config_schema declares as
    # float must have a float canonical default too — _TYPE_KIND_BY_KEY derives
    # the kind from type(default), so an int literal made the store-side gate
    # expect int while the on-disk/pydantic value is float, spamming
    # "expected int, got float" schema warnings on every config read. The float
    # branch accepts both int and float; the literals just must be float-typed.
    "max_trade_notional_usd": 30.0,
    "tp_scale_fraction": 0.5,
    "max_concurrent": 2,
    "max_total_notional_pct": 2.0,
    # Audit 2026-09-04 P0-3 (dimensional clarity): `max_total_notional_pct` is
    # read as a MULTIPLE of aggregated equity (e.g. 4 → 400% of equity), NOT a
    # percentage fraction. The `_pct` suffix is historical and misleading; the
    # runtime and schema both treat it as an equity multiple. Production pins
    # 4.0 (= 4× equity total-open-notional ceiling, within the 10x leverage
    # band).
    # Audit 2026-09-06 (C6): zero / tiny-value semantics. The pydantic field
    # (config_schema.py) accepts ge=0.0, and a SEPARATE sane-floor validator
    # (test_config_safety_floors) rejects the DANGER ZONE 0 < x < 0.5 (one
    # trade would fill the cap and freeze the rest of the day), while:
    #   * 0 is the EXPLICIT "cap disabled" signal — equity_risk_cap
    #     (risk_gates.py) short-circuits pass=True for None/<=0 (it must
    #     never read pct=0 as "cap at $0", which would reject every entry);
    #   * x >= 0.5 is a normal working multiple (production pins 4.0).
    # So "set it to 0 to disable" is CORRECT, and there is no need to use a
    # huge positive value for that purpose. Do NOT set 0.04 expecting 4% —
    # the unit is an equity MULTIPLE, so 0.04 means 4% of equity (rejected
    # by the sane-floor) and 4.0 means 400%.
    # Daily-loss kill-switch — P1-15 unified semantics (see
    # risk_gates.effective_daily_loss_cutoff): the ENTRY gate halts new entries
    # at the TIGHTER of (a) circuit_breaker.daily_loss_pct × equity [PRIMARY,
    # scales with the account] and (b) this absolute USD floor [BACKSTOP for
    # large accounts / misconfiguration]. Crossover is equity = $2 / 5% = $40:
    # BELOW ~$40 the 5% leg is tighter (e.g. at the current ~$21 micro account
    # 5% = -$1.05, which fires before the -$2 floor); ABOVE ~$40 the -$2 USD
    # floor binds (at $200 the 5% leg would allow -$10). Neither leg is
    # silently dead — the tighter (least negative) cutoff always fires first.
    "max_daily_loss_usd": -2.0,  # F4: float default aligns with schema (supplemental audit 2026-08-31)
    # B-M11 (deep audit 2026-08-28): the global-halt and per-coin circuit
    # breakers only block NEW entries — positions already open keep bleeding
    # to their DSL stops during the halt window. These switches make them
    # HARD: when armed, the trading loop market-closes every open position
    # (global halt) / the halted coin's position (coin circuit) the moment
    # the breaker trips. H-1 (audit 2026-08-29): flipped to DEFAULT ON — a
    # tripped breaker means risk is already out of control and leaving a
    # 10-U micro-book naked (relying solely on each coin's own resting stop)
    # is the more dangerous contract. Operators who want the old "block
    # entries only" behavior can set either key to false explicitly.
    "auto_flatten_on_global_halt": True,
    "auto_flatten_on_coin_circuit": True,
    # C3 (HYPE RCA 2026-08-21 item 5): blow-up-level self-halt. When a SINGLE
    # closing trade realizes a leveraged ROE loss at/under `roe_halt_threshold_pct`
    # (default -50%, i.e. half the margin gone), flip the bot to mode=OFF and
    # fire a risk alert. This is the nuclear kill-switch that the tiered breakers
    # (time-windowed, open-blocking only) and the daily-loss USD switch do not
    # cover: a single catastrophic gap-through (HYPE: -252% ROE). Audit
    # 2026-09-04 P1-16: dsl_exit.max_loss_roe_pct (3-5% ROE) is a PLANNED stop
    # ORDER, not a realized-fill kill switch — a gap-through that fills at
    # -50% ROE would never arm it, so the two are NOT redundant. Enabled by
    # default; set false explicitly to opt out.
    "roe_halt_enabled": True,
    "roe_halt_threshold_pct": -50.0,
    "daily_giveback_halt_pct": 0.3,
    "daily_giveback_min_peak_usd": 2.0,
    "crowded_with_min_conf": 0.8,
    "min_available_margin_pct": 0.2,
    "cooldown_min": 30,
    "research_cooldown_min": 3,
    "held_research_interval_min": 10,
    "min_ai_confidence": 0.60,
    # Signal-price deviation gate: max allowed % gap between the verdict's
    # planned entry and the fresh live mid. A larger gap means the model
    # decided on prices that no longer exist (stale candle setup). 2.0%.
    "max_signal_price_deviation_pct": 2.0,
    # Score-invariant gate: after the snapshot gates pass, re-scan the coin and
    # block if the live composite score has fallen below the runner floor
    # (prevents booking on a score the market no longer supports).
    "score_invariant_enabled": True,
    # Late-chase gate: (a) move-state machine blocks joining a move that has
    # already run more than fresh_move_band_pct beyond its anchor, and (b) a
    # terminal 1h-RSI blowoff blocks entries regardless of trigger freshness.
    "late_chase": {
        "enabled": True,
        "fresh_move_band_pct": 12.0,
        "rsi1h_overbought": 88.0,
        "rsi1h_oversold": 12.0,
        # P2：锚点防重锚。
        "min_anchor_age_sec": 300.0,
        "min_move_extension_pct_for_reset": 3.0,
        # P1 Leg3：短周期、对未收盘 bar 实时计算的耗尽度。
        "realtime": {
            "enabled": True,
            "interval": "5m",
            "rsi_overbought": 80.0,
            "rsi_oversold": 20.0,
            "max_extension_atr": 3.0,
            # 锁存迟滞：触及极端后，RSI 须回到此中性带（或冷却满）才解禁。
            "release_rsi_high": 60.0,
            "release_rsi_low": 40.0,
            "latch_cooldown_s": 900.0,
        },
        # Leg4：绝对区间分位硬闸——不管 RSI，多头顶部分位 / 空头底部分位直接拦。
        "range_position": {
            "enabled": True,
            "interval": "15m",
            "lookback_bars": 96,      # 24h
            "block_long_above_pct": 90.0,
            "block_short_below_pct": 10.0,
        },
    },
    # 抓住启动点（leading signals，shadow-only）：用主动成交流(CVD)、盘口失衡、
    # 波动压缩与 HTF 关键位共振，在突破确认之前/当根给出预触发。不直接下单，
    # 仅放宽 breakout 的确认根数与标记 launch 证据。
    "launch_capture": {
        "enabled": True,
        "aggression_min": 0.55,       # burst 窗口最低单向主动占比
        "imbalance_min": 0.2,         # 盘口失衡阈值
        "compression_pct_max": 20.0,  # BB 带宽分位 ≤ 此值视为压缩
        "require_key_level": True,    # 要求 HTF 关键位共振
        "flow_confirm_min": 0.7,      # CVD 强到可替代 N 根确认
        "breakout_trend_rvol_min": 2.0,  # 强突破据此升级为 trend（宽止损）
        "breakout_trend_rvol_lookback": 3,  # 近N根5m内出现过强RVOL即升级（决策可能晚启动bar一两根）
        # 订阅时序修复：在突破发生“之前”就对尚未触发、但处于压缩蓄力状态的
        # 候选币持续订阅 trades，使 CVD 累加器在启动当根已有连续读数。
        "presubscribe_enabled": True,
        "presubscribe_pool": 60,       # 从流动 universe 头部取多少币做压缩筛查
        "presubscribe_max": 12,       # 额外提前订阅的未触发币上限
        "presubscribe_compress_pct_max": 20.0,  # 带宽分位 ≤ 此值视为蓄力候选
        # 低位启动放行（默认关闭，开启后也只在 shadow 记录/放行；先影子验证）。
        "low_position_relax": {
            "enabled": False,
            "confidence_min": 0.58,     # raw conf 低于门槛但≥此值才考虑
            "pre1h_max_pct": 1.5,       # 入场前1h涨幅须≤此值（低位、非追高）
            "aggression_min": 0.7,      # 真实主动买盘 flow 须≥此值
        },
        "cvd_divergence": {
            "left": 8,
            "right": 5,
            "min_strength_pct": 10.0,
        },
        "volume_profile": {
            "bins": 50,
            "atr_bins": True,
            "atr_period": 14,
            "atr_multiple": 0.25,
        },
        "trades_capture": False,
    },
    "counter_regime_min_conf": 0.82,
    "max_crypto_long_correlated": 2,
    "min_market_volume_usd": 5_000_000.0,  # F4: float per schema (supplemental audit 2026-08-31)
    "min_hip3_volume_usd": 5_000_000.0,    # F4: float per schema (supplemental audit 2026-08-31)
    "min_short_volume_usd": 50_000_000.0,  # F4: float per schema (supplemental audit 2026-08-31)
    "coin_allowlist": [],
    "coin_blocklist": ["TON", "TRX"],
    "hip3_dex_allowlist": ["xyz"],
    "hip3_dex_blocklist": [],
    "dsl_exit": {
        # ───────────────────────── PARAMETER LAYERING (Zorro ≤3 discipline) ──
        # This block has ~15 knobs but they are NOT free fit parameters. Before
        # tuning anything, read its layer:
        #   * FROZEN CONSTANTS — fixed by a 1y event study / audit, do NOT
        #     re-optimize: max_loss_pct, max_loss_roe_pct, protect_pct,
        #     retrace_threshold, breakeven_trigger/lock, hard_timeout,
        #     stale_flat_timeout, phase2_tiers, regime_aware (trend_ride /
        #     max_loss / clocks).
        #   * ACTIVE DEGREE OF FREEDOM — only these may be adjusted under the
        #     "≤3 optimized exit params" budget; currently effectively 0 (all
        #     of the above are data-pinned, not optimizer output).
        #   * OBSERVATION ARMS (shadow, no fills) — stop_tuning_shadow records
        #     counter-factual arms; keep until the audit concludes.
        #   * DISABLED-BY-EVIDENCE sub-blocks — atr_stop ("no edge on/off",
        #     409-trade study 2026-09-25), smooth_transition (net-negative tick
        #     replay), time_scratch (does not lift portfolio EV, A/B 2026-09-30).
        #     Kept OFF + documented on purpose; do not re-enable without new
        #     evidence. Removing these keys is SAFE (consumers .get() → class
        #     defaults) but pointless for production, which pins them explicitly.
        "max_loss_pct": 1.0,
        "max_loss_roe_pct": 15.0,
        "protect_pct": 1.5,
        "retrace_threshold": 0.15,
        # Intraday-short tightening (2026-09-22): strategy enters on 5m and
        # realised holds are minute-scale, so the legacy 30h hard / 8h stale
        # ceilings let drifters occupy scarce slots far too long. Hard cap now
        # 4h; a never-protected drifter is cut at 90m.
        "hard_timeout_minutes": 240.0,
        "breakeven_trigger_pct": 2.5,
        "breakeven_lock_pct": 0.5,
        "stale_flat_timeout_minutes": 90.0,
        # ATR 自适应止损：经 409 笔全分布回测裁决「ATR 开/关无 edge」，生产
        # 明确关闭（enabled=false，2026-09-25）。代码/parity 路径保留；canonical
        # 默认同步为 false，避免配置丢失时静默回落到 true（与运维意图相反）。
        "atr_stop": {
            "enabled": False,
            # 关态参数对齐生产（S3-A 收口）：不沿用 09-23 实验宽口径 1.5/1.0/4.0。
            "atr_mult": 1.2,
            "floor_pct": 1.2,
            "ceiling_pct": 3.0,
        },
        # R12-C1: noise band tolerates a pull-back of atr_mult × entry ATR%
        # below the floor before an exit fires (sub-first-tier only); was
        # implicit via .get("noise_band", {}) in executor/dsl_exit. B-10 符号
        # 一致性检验判其为正贡献，生产启用 atr_mult=0.8。
        "noise_band": {
            "enabled": True,
            "atr_mult": 0.8,
        },
        # Audit 2026-09-10 (risk-tuning shadow 3): record whether a wider
        # max-loss cap / lower breakeven trigger would have mattered; live
        # stop behaviour is unchanged. shadow_mode=true = record-only
        # (dsl_exit._record_stop_tuning_shadow). The two candidate percents
        # default to None semantics at the read site, but explicit 0.0 values
        # are inert (no wider cap, no lower breakeven) so the synthesised
        # default block records nothing new.
        "stop_tuning_shadow": {
            "shadow_mode": True,
            "candidate_max_loss_pct": 0.0,
            "candidate_breakeven_trigger_pct": 0.0,
        },
        # Audit 2026-09-06 (E4, P2): smooth phase1→phase2 floor transition.
        # Ramps the phase-2 floor from the hard stop up to the full trailing
        # floor across a peak-profit band of width band_pct instead of snapping
        # in one tick at the arm instant. Tick A/B replay (scripts/
        # p2_smooth_replay.py) showed it net-negative, so it ships DEFAULT OFF
        # (inert) — an operator opts in via this block. Was a dead knob:
        # ExitPolicy fields existed but no construction path fed them.
        "smooth_transition": {
            "enabled": False,
            "band_pct": 1.0,
        },
        # Audit 2026-09-06 (E3, P2): time-based scratch exit. Closes a choppy
        # position that has aged past `minutes`, never armed phase-2, printed a
        # small favorable pop (>= min_peak_pct) but has since given back
        # giveback_pct from peak while still green. Default OFF (inert).
        "time_scratch": {
            "enabled": False,
            "minutes": 60.0,
            "min_peak_pct": 0.3,
            "giveback_pct": 0.3,
        },
        # R12-C1: floor-breach confirmation. Was implicit: executor built
        # ExitPolicy with hardcoded defaults (1 / 0.0).
        # A-F5 (deep audit 2026-08-28): breach_confirm_sec default 0.0 → 4.0
        # (audit: 3–5s). A single instantaneous mid tick through the floor no
        # longer closes; the breach must persist 4s AND the oracle index price
        # must confirm it (dsl_exit.get_index_prices).
        "consecutive_breaches_required": 2,
        "breach_confirm_sec": 4.0,
        "phase2_tiers": [
            {"pct_above_entry": 2.0, "retrace_threshold": 0.35},
            {"pct_above_entry": 6.0, "retrace_threshold": 0.3},
            {"pct_above_entry": 12.0, "retrace_threshold": 0.2},
            {"pct_above_entry": 20.0, "retrace_threshold": 0.15},
        ],
        "regime_aware": {
            "enabled": True,
            "trend_ride": {
                "protect_pct": 2.5,
                "retrace_threshold": 0.4,
                "phase2_tiers": [
                    {"pct_above_entry": 2.5, "retrace_threshold": 0.4},
                    {"pct_above_entry": 8, "retrace_threshold": 0.38},
                    {"pct_above_entry": 15, "retrace_threshold": 0.35},
                    {"pct_above_entry": 25, "retrace_threshold": 0.3},
                ],
            },
            "max_loss": {
                # P0：trend 上限 0.8% → 4.0%（天花板，非目标宽度）。配合 atr_stop
                # 启用，实际止损 = min(4.0%, clamp(1.5×ATR%, 1%, 4%))，给趋势单
                # 足够噪声空间；杠杆帽同步放宽 trend ROE 10%→20%（5x 下=4% 现货），
                # 否则 ROE 帽恒为 2% 现货，仍会架空 ATR 宽度。
                "trend": {"max_loss_pct": 4.0, "max_loss_roe_pct": 20.0},
                # non_trend（scalp）止损 0.4/5 → 1.5/15（PRM-02，2026-09-25）：
                # 旧 0.8%≈0.23×4h ATR 深陷噪声带，固定 cap 抬到 1.5、ROE 15。
                "non_trend": {"max_loss_pct": 1.5, "max_loss_roe_pct": 15.0},
            },
            # Audit 2026-09-06 (E3, P2): regime-split position-lifetime clocks
            # (minutes). Trend regimes get LONGER hard/stale timeouts (let
            # rippers ride); non-trend get SHORTER (prune chop faster).
            # Intraday-short tightening (2026-09-22): now ENABLED with minute/
            # hour-scale ceilings — trend hard 4h / stale 2h, non-trend hard
            # 2h / stale 1h — matching the 5m-entry short style.
            "clocks": {
                "enabled": True,
                "trend": {"hard_timeout_minutes": 240.0,
                          "stale_flat_timeout_minutes": 120.0},
                "non_trend": {"hard_timeout_minutes": 120.0,
                              "stale_flat_timeout_minutes": 60.0},
            },
        },
    },
    "force_execute_composite": 30,
    "composite_force_execute": False,
    # O-3 (P1 audit): the TA sidestep force-execute switch bypasses AI
    # confirmation, so like every other force_*/bypass switch its canonical
    # default is False — arming it is an explicit operator decision (and is
    # caught by the FORBIDDEN_OVERRIDE config gate).
    "ta_sidestep_force_execute": False,
    "ta_sidestep_min_slow_burn_count": 99,
    "force_execute_slow_burn_count": 2,
    "conviction_sizing": False,
    # R12-C1: legacy conviction sizing ladder (only consulted when
    # conviction_sizing=true). [[min_confidence, size_multiplier], ...];
    # was implicit via executor._DEFAULT_CONVICTION_TIERS.
    "conviction_tiers": [[0.80, 1.5], [0.65, 1.0], [0.0, 0.7]],
    "whale_regime_bypass": False,
    "whale_force_execute": False,
    "whale_size_multiplier": 1.0,
    "block_counter_trend_bypass": True,
    "trend_surface_enabled": True,
    # Audit 2026-09-06 (D1): minimum number of DAILY candles a coin must have
    # for the 200MA trend filter to be considered "established". Production
    # pins min_history_bars=200 so coins without an established daily history
    # are not traded on an ungrounded trend; aligned to live (2026-09-25).
    "min_history_bars": 200,
    # Audit 2026-09-06 (D2): hard 24h price-extension ceiling for LONGS. A coin
    # up more than this percent over the last 24h is a blow-off chase and is
    # refused regardless of mover/score. 0 disables. Ported from Pathia
    # override_max_daily_extension_pct=30.0. Distinct from the ta_late_entry
    # "extension" (which is a 4h ATR-multiple) — this is a raw 24h percent.
    "override_max_daily_extension_pct": 30.0,
    # Audit 2026-09-06 (C11/C12): previously hard-coded module constants now
    # tunable via config. Each keeps the exact historical value as its default
    # so behaviour is unchanged until an operator overrides it.
    #   notional_cap_tier_* — _tiered_notional_cap (executor): below this equity
    #     the absolute max_trade_notional_usd stays a hard floor for micro
    #     accounts; at/above it the effective cap scales with equity*multiple.
    "notional_cap_tier_equity_usd": 50.0,
    "notional_cap_tier_multiple": 1.5,
    # Audit 2026-09-06 (C11): hard account-equity floor for new entries.
    #   Below this aggregate equity the executor fail-closes (no trade sized
    #   above exchange min-notional with real stop room). Pathia uses ~$12;
    #   $10 mirrors the HL min-order floor. Set 0 to disable.
    "min_tradable_equity_usd": 10.0,
    #   min_order_usd — Hyperliquid rejects orders below $10 notional; 10.5 is
    #     the buffered hard floor used by exchange sizing (_min_order_size /
    #     min_entry_notional_usd / entry_size_for_notional). Must stay >= 10.
    "min_order_usd": 10.5,
    #   correlation_crypto_coins — the major-crypto pool the correlation cap
    #     counts long exposure against (was risk_gates._CRYPTO_COINS frozenset).
    #     Empty/None falls back to the built-in 40-coin list.
    "correlation_crypto_coins": [
        # Delisted tickers removed: MATIC (→ POL), FTM (→ S).
        "BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "AVAX", "POL",
        "LINK", "DOT", "UNI", "ATOM", "NEAR", "S", "APT", "ARB", "OP",
        "INJ", "TIA", "SUI", "SEI", "WIF", "PEPE", "BONK", "FLOKI", "TRX",
        "LTC", "BCH", "ETC", "XLM", "ALGO", "AAVE", "MKR", "SNX", "CRV",
        "COMP", "YFI", "SUSHI", "1INCH",
    ],
    "loss_cooldown_min": 180,
    "min_ai_close_hold_min": 25,
    "breakout_force_execute": False,
    "sl_atr_mult": 1.2,
    # R12-C1: backup stop clamp width (%) and manual/TP bracket ATR mult.
    # Was implicit via executor module constants (_DEFAULT_SL_CEILING_PCT=3.0,
    # _DEFAULT_SL_FLOOR_PCT=1.2) and server.py tp default 1.0. sl_floor_pct
    # has a per-coin override via atr_risk_sizing.coin_overrides.<coin>.
    "sl_ceiling_pct": 3.0,
    "sl_floor_pct": 1.2,
    "tp_atr_mult": 1.0,
    "min_trend_score": 0.55,
    # Regime classifier thresholds (chop / against-funding conviction bars)
    "chop_min_conf": 0.75,
    "chop_min_score": 45.0,
    # P1-4: momentum-burst bypass in chop requires at least this composite score
    "chop_burst_min_score": 20.0,
    "against_funding_min_conf": 0.85,
    "against_funding_min_score": 60.0,
    # Regime strength label thresholds
    "strong_trend_threshold": 0.70,
    "trend_threshold": 0.55,
    "neutral_threshold": 0.40,
    # Pre-trade volatility / spread gates (previously env-only)
    "max_atr_pct": 15.0,
    "max_spread_pct": 1.0,
    "spread_gate_fail_open": False,
    # Audit 2026-09-21 (#3): live post-only (Alo) maker execution scaffold.
    # DEFAULT OFF — the resting-order lifecycle (fill polling / TTL cancel /
    # post-fill DSL wiring) is not yet implemented; the executor branch is
    # fail-CLOSED while `resting_lifecycle_ready` is false, so flipping enabled
    # on can never produce an untracked resting order. Activate only after the
    # lifecycle is built and validated with small funded size (ADR-0003).
    "maker_execution": {
        "enabled": False,
        "resting_lifecycle_ready": False,
        "offset_bps": 5.0,
        "max_notional_usd": 100.0,
    },
    "runner_entry_gate": {
        "enabled": True,
        "allow_shorts": False,
        "bypass_sidestep_overrides": False,
        "min_confidence": 0.7,
        "min_composite": 45.0,
        "min_hip3_composite": 50.0,
        "min_short_confidence": 0.72,
        "min_short_composite": 45.0,
        "mover_min_confidence": 0.72,
        "mover_min_composite": 30.0,
        # 4h late-entry veto (mirrors the code defaults that _runner_entry_block
        # _reason used before these were schema-configurable).
        "rsi_overbought": 75.0,
        "trend_rsi_overbought": 80.0,
        "rsi_oversold": 25.0,
        "max_extension_atr": 2.5,
        # Counter-regime direction probe (observation only): path the
        # would_block verdicts append to. Empty falls back to
        # /data/regime_direction_shadow.jsonl.
        "regime_direction_shadow_path": "",
        # R12-C1: pullback-long bypass admits uptrend longs that have pulled
        # back to a lower-risk zone. Off by default; was implicit via
        # gate.get("pullback_long") hardcoded defaults in executor.
        "pullback_long": {
            "enabled": False,
            "min_composite": 30.0,
            "max_rsi": 65.0,
            "max_extension_atr": 2.0,
            "min_slow_burn": 2,
            "shadow_mode": False,
            # Audit 2026-09-06 (E2, Q3): require the MACRO regime (BTC / SP500
            # proxy EMA20/30 + ADX via detect_regime_with_score) to be "up"
            # before the bypass fires. The 4h per-coin uptrendMomentum flag
            # alone fires on false golden crosses in a choppy macro and buys
            # the range top. fail-closed: any non-"up" macro (chop/neutral/
            # down) or a lookup error withholds the bypass so the trade falls
            # through to the normal late-chase veto. Set false to restore the
            # pre-E2 behaviour (4h uptrend only).
            "require_macro_uptrend": True,
        },
        # Audit 2026-09-10 (risk-tuning shadow): record-only counter-factual
        # arms in executor.py's runner gate. shadow_mode=true = observation
        # only (never blocks live admission); per_coin_cooldown is the one
        # exception where false ENFORCES a runner-gate block, so its default
        # is true (record-only) to keep a synthesised default block inert.
        # The production file sets per_coin_cooldown.shadow_mode=false and
        # the deep merge preserves it.
        # breakout_score_floor: long-running observation-only arm whose record
        # never converted to a decision (P1-2 cleanup, 2026-09-25). Defaulted
        # OFF now so it stops writing risk_tuning_shadow data; the block is kept
        # for a future explicit re-enable. Set shadow_mode=true to observe.
        "breakout_score_floor": {
            "shadow_mode": False,
            # Aligned to live breakout floor (production pins 31.5).
            "min_composite": 31.5,
        },
        "per_coin_cooldown": {
            "shadow_mode": True,
            # Audit 2026-09-21 (#4): on a memory/history read failure, fail
            # CLOSED (treat as cooldown) instead of admitting the re-entry.
            "fail_closed": True,
            "window_hours": 24,
            "repeat_min_composite": 45.0,
            "max_consecutive_losses": 2,
            "loss_cooldown_hours": 24,
        },
    },
    "plan_b": {
        "enabled": True,
        "rsi_low": 40.0,
        "rsi_high": 60.0,
        "size_mult": 0.5,
    },
    "atr_risk_sizing": {
        "enabled": True,
        "risk_per_trade_pct": 0.02,
        "sizing_basis": "primary_stop",
        # P1-4 Phase 1: sizing v2 gray-release mode as a canonical file
        # leaf so the truth source can move out of HERMES_SIZING_V2_MODE.
        # PRM-06 收口（2026-09-25）：canonical 默认 enforce，按止损宽度 SSOT
        # sizing；legacy boolean sizing_v2_enabled was retired in Phase 1 step 5.
        "sizing_v2_mode": "enforce",
        # P1-4 Phase 3 batch 2: gray-release cap (0-1) scaling the v2
        # notional. 1.0 mirrors executor.py's .get(..., 1.0) fallback so a
        # key-absent config behaves identically (no scale-down).
        "sizing_v2_cap_pct": 1.0,
        # R12-C1: per-coin overrides for the ATR sizing / SL floor params.
        # Canonical 对齐生产：HYPE 现货深度/波动特征使用更宽止损地板 1.5%；
        # PURR/BOME 钉 1.2%。配置丢键深合并时不得回落到空 dict，否则 HYPE 地板
        # 会静默收紧到默认 1.2%，与实盘口径分叉。
        "coin_overrides": {
            "HYPE": {"sl_floor_pct": 1.5},
            "PURR": {"sl_floor_pct": 1.2},
            "BOME": {"sl_floor_pct": 1.2},
        },
    },
    "regime_classifier": {
        "fast_ema": 20,
        "slow_ema": 30,
        "slope_threshold": 0.002,
        "chop_adx_max": 20.0,
        # R13-B5: fast-EMA slope lookback window (bars) for the trend
        # classifier (market_regime._SLOPE_LOOKBACK) and the per-proxy regime
        # cache freshness TTL (market_regime.REGIME_TTL_S, 5 min). Was
        # module-literal / function-local — now visible + tunable.
        "slope_lookback": 8,
        "ttl_sec": 300,
    },
    # R13-B5: 5-component continuous trend-strength score (byte-aligned with
    # scripts/backtest_ab_compare._regime_score). Consumed by
    # market_regime.regime_strength_score() AND executor.regime_strength_label()
    # (single source via market_regime.regime_score_params()); before R13-B5
    # the weights/calibration were two byte-copied literal sets (one per file)
    # invisible to cfg. Defaults are the exact calibrated literals.
    "regime_score": {
        # component weights (sum == 1.0)
        "weight_adx": 0.25,
        "weight_atr": 0.225,
        "weight_ema_align": 0.175,
        "weight_price_ext": 0.175,
        "weight_obv": 0.175,
        # calibration anchors: ADX 15 -> 0, 45 -> 1 (full span 30)
        "adx_zero": 15.0,
        "adx_full_span": 30.0,
        # ATR% 0.2% -> 0, 1.0% -> 1 (full span 0.8)
        "atr_pct_zero": 0.2,
        "atr_pct_full_span": 0.8,
        # |EMA8-EMA21| gap% reaching 0.5% -> 1.0
        "ema_gap_full_pct": 0.5,
        # distance from EMA21 reaching 2.0 ATR -> 1.0
        "price_ext_full_atr": 2.0,
        # OBV flat (no slope) partial credit (aligned=1.0, opposing=0.0)
        "obv_flat_score": 0.3,
        # score indicator periods (distinct from classifier EMA20/30)
        "ema_fast": 8,
        "ema_slow": 21,
        "ind_period": 14,
        "min_candles": 50,
        "obv_slope_period": 10,
    },
    # R13-B6: funding-crowding regime classifier (hyperfeed.py). Consumed by
    # hyperfeed._compute_funding_regime() / market_get_funding_regime() and
    # read on the risk-gate hot path (risk_gates._funding_regime_for). The
    # 5-min cache TTL was the twin of regime_classifier.ttl_sec (R13-B5) —
    # one was wired, the other still a module literal. Defaults are the exact
    # hyperfeed literals: ±0.0001 funding crowding bar, OI floors 1e7 (crypto)
    # / 1e6 (HIP-3 equity+commodity), per-class long-vs-short count margin 5.
    "funding_regime": {
        "ttl_sec": 300,
        # Audit 2026-09-04 P1-11: HL perp funding routinely sits ~0.01% per 8h
        # (0.0001), so a threshold AT the baseline flagged nearly every coin as
        # "crowded" and tripped the crowded-trade protection when nothing was
        # actually crowded. Raised to 0.04% (0.0004) — only genuinely abnormal
        # funding (3-5x baseline) now marks a coin crowded.
        "crowded_funding_threshold": 0.0004,
        "oi_floor_crypto": 10000000.0,
        "oi_floor_other": 1000000.0,
        "class_dominance_margin": 5,
    },
    "debate_gate": {
        "enabled": True,
        # Audit 2026-09-04 P1-21 note: min_agreement=0.4 and min_agree_count=2
        # are numerically equivalent under the 5-role debate (2/5 == 0.4); the
        # effective rule is "the stricter of the two wins". Pinned to match
        # production (0.4 / 2).
        "min_agreement": 0.4,
        "min_agree_count": 2,
        # R12-C1: when true, a bull/bear split defaults to a third-analyst
        # tiebreak instead of fail-closed disagreement. Was implicit via
        # debate_cfg.get("analyst3_default", False) in risk_gates.
        "analyst3_default": False,
    },
    # Native in-process multi-perspective research debate. Off by default —
    # when enabled, research() runs bull/bear LLM calls in parallel plus an
    # arbiter synthesis with a hard latency cap and a single-LLM fallback on
    # any failure.
    "debate_research": {
        "enabled": False,
        # Audit 2026-09-04 P0-7: unified with research_llm.timeout_sec=25 so
        # the debate's overall latency budget and the LLM per-call timeout
        # agree — when the debate gives up, the LLM call has already timed
        # out (no residual 12s burning of quota after a debate abort).
        # Audit 2026-09-23: raised from 25 to 55 so the debate legs (bull/bear
        # 0.7×=38.5s, synth 0.92×=50.6s) cover the observed slow-reasoning p85.
        "max_latency_s": 55.0,
        # Audit 2026-09-04 P0-1: optional per-leg timeouts. When set they win
        # over the max_latency_s fractions; when unset the legs scale directly
        # off max_latency_s (bull 0.7×, synth 0.92×) with no hard cap, so a
        # raised max_latency actually lengthens the debate legs. Added because
        # the former hard 18/24s clamps made max_latency_s>25.7s a no-op.
        "bull_timeout_s": None,
        "synth_timeout_s": None,
        "cache_ttl_s": 300.0,
        # P2-2: max entries in the in-process verdict cache (composite key of
        # coin + score bucket + trigger hash); oldest-expiry evicted past cap.
        "cache_max_entries": 128,
        "parallel": True,
        "use_structured_output": True,
        # Shadow A/B (absorbed from TradingAgents). When enabled, run the
        # bull/bear/arbiter debate in the BACKGROUND for eligible candidates
        # purely as a comparison signal — it NEVER replaces the single-LLM
        # verdict that routes/executes. The debate verdict is logged so the
        # shadow-grading pipeline can measure whether debate improves on the
        # single LLM before it is ever promoted.
        "shadow_ab": {
            "enabled": True,
            "min_composite": 60.0,   # only high-value candidates get the probe
            "sample_rate": 0.25,     # fraction of eligible candidates (0–1)
        },
    },
    # Post-close decision reflection (absorbed from TradingAgents). After a
    # close, one lightweight background LLM call writes a short qualitative
    # review that is injected into the next research prompt. Off the trading
    # critical path; INERT (never sizes/vetoes/changes gates).
    "reflection": {
        "enabled": True,
        "max_chars": 400,        # cap on one review's length
        "inject_limit": 3,       # most-recent reviews injected into the prompt
        "timeout_s": 20.0,       # per-reflection LLM read timeout
    },
    "signal_enforcement": {
        "enabled": True,
        "veto": True,
        # boost canonical 对齐生产 false：boost 会把 force_execute 门槛自动下调
        # boost_bar_delta(4) 点（shadow_signals.enforce_signals + executor
        # L2027-2030 真实消费）。配置丢键深合并时不得默认重新武装，否则会静默
        # 放宽强制开仓门槛。与其它 gray-release 一致取 inert 方向。
        "boost": False,
        "gex_veto": True,
        "boost_bar_delta": 4,
        "whale_window_min": 15,
        "whale_veto_min_usd": 500000,
        "whale_boost_min_usd": 500000,
    },
    # 动量延续因子（趋势中继回调入场）
    "momentum_continuation": {
        "enabled": False,
        "log_near_miss": True,
        "min_trend_pct": 8.0,
        "max_pullback_pct": 4.0,
        "weight": 0.4,
    },
    # K线形态识别
    "candlestick_patterns": {
        "enabled": False,
        "wick_body_ratio": 2.0,
        "context_lookback": 6,
        "context_pct": 1.5,
    },
    # 人工干预需AI二次研判
    "override_requires_ai": True,
    # 鲸鱼扫描绕过趋势检查
    "whale_scan_bypass": False,
    # 持仓评分变化豁免研判冷却
    "research_rescore_delta": 0.0,  # F4: float per schema (supplemental audit 2026-08-31)
    # Audit 2026-09-12 (#8 majors-missed-surge): adaptive re-research cooldown.
    # In a hot σ burst the long calm cooldown window locks research out of the
    # fastest part of a BTC/ETH surge. shadow_mode records would-admits but keeps
    # the long window; enforce actually admits the re-research after active_min.
    # Default SHADOW — observation only until the gray-release JSONL accrues.
    "research_cooldown_adaptive": {
        "enabled": True,
        "shadow_mode": True,
        "active_min": 2,
        "pct_sigma_min": 3.0,
        "vol_sigma_min": 5.0,
    },
    # Audit 2026-09-12 (#4 majors-missed-surge): σ-burst surfacing gate. A coin
    # that prints a large return/volume σ spike but falls short of the composite
    # gate (54) may surface for research at a lower effective gate. shadow_mode
    # records would-surface but still drops; enforce surfaces. Default SHADOW.
    "sigma_burst_gate": {
        "enabled": True,
        "shadow_mode": True,
        "pct_sigma_min": 3.0,
        "vol_sigma_min": 5.0,
        "gate_override": 45.0,
    },
    # 资金轮动（弱势仓换强势标的）
    "capital_rotation": {
        "enabled": False,
        "shadow_mode": False,
        "min_candidate_composite": 40.0,
        "min_hold_minutes": 30,
        "protect_winner_roe_pct": 3.0,
    },
    # GEX（Gamma Exposure）信号
    "gex_signal": {
        "enabled": True,
        "shadow_mode": False,
        "caution_near_wall_pct": 10.0,
    },
    # 影子信号（只记录不执行）
    "shadow_signals": {
        "enabled": True,
        "gex": True,
        "short_volume": True,
        "crypto_whale": True,
        "news": True,
        "whale_window_min": 15,
    },
    # R13-B7: free-signal-suite tunables. Each block mirrors the literals that
    # were hardcoded in the corresponding module; the modules keep their module
    # constants as fallback symbols but resolve every leaf through cfg_get so
    # env (HERMES_CFG_<BLOCK>__<KEY>) and dashboard overrides actually reach
    # the hot path (direct dict reads silently ignored env — the gex caution
    # drift fixed here: canonical said 10.0 while three fallbacks said 1.0).
    "options_gex": {
        "ttl_sec": 900,              # CBOE delayed feed; 15-min structural cache
        # Audit 2026-09-06 (F6, engineering hygiene): a fetch FAILURE / empty
        # feed used to be cached for the full positive TTL (15 min), so a
        # transient CBOE outage made GEX cautions blind for a quarter hour.
        # Misses get a short TTL so recovery is picked up promptly while still
        # shielding the hot path from re-hammering a down source.
        "negative_ttl_sec": 90,
        "http_timeout_s": 12.0,      # per-request CBOE fetch bound
    },
    "short_volume": {
        "ttl_sec": 3600,             # FINRA file is daily; an hour is plenty
        # Audit 2026-09-06 (F6): same short negative TTL for FINRA misses
        # (was 3600s — an hour blind after a transient FINRA failure).
        "negative_ttl_sec": 120,
        "http_timeout_s": 12.0,      # per-day FINRA fetch bound
        "crowded_ratio": 0.60,       # >= → squeeze fuel
        "light_ratio": 0.35,         # <= → little short pressure
        "trend_delta": 0.03,         # series first-vs-last move for rising/falling
        "lookback_days": 5,          # trading days walked back per scan
    },
    "crypto_whale": {
        "ttl_sec": 120,              # Binance aggTrades rolling window cache
        "http_timeout_s": 2.5,       # per-page bound (6 sequential pages max)
        "cache_max": 1024,           # per-process cache entry cap
        "window_minutes": 15,        # rolling aggTrades window
        "min_usd": 100000,           # print >= this counts as a whale print
        "bias_threshold": 0.20,      # |net|/whale $ >= this for a directional bias
        "max_pages": 6,              # pagination cap on the window walk
    },
    "news_catalyst": {
        "ttl_sec": 300,              # GDELT/RSS cache; news moves fast
        "http_timeout_s": 3.0,       # per-request bound (2 parallel GDELT calls)
        "surge_breaking_x": 2.5,     # latest coverage bin >= 2.5x baseline = breaking
        "surge_elevated_x": 1.5,     # >= 1.5x baseline = elevated coverage
        "timespan": "1h",            # GDELT query timespan
        "max_records": 30,           # ArtList maxrecords / headline cap
        "rss_limit": 25,             # rss_headlines headline cap
        "fetch_max_workers": 2,      # parallel GDELT ArtList+TimelineVol pool
        "cb_fail_threshold": 5,      # consecutive failures before opening breaker
        "cb_open_cooldown_s": 300.0,  # pause requests for this long, then one probe
    },
    "whale_index": {
        "min_volume_usd": 1000000,       # smart_money_concentration 24h-vol floor
        "funding_confidence_scale": 0.0001,  # |funding|/this = concentration confidence
        "oi_vol_ratio_min": 10,          # OI/($M vol) above this = high-OI flag
        "oi_vol_confidence_norm": 50,    # ratio/this = high-OI confidence
        "min_oi_usd": 5000000,           # OI notional floor for anomaly/surge
        "max_funding_threshold": -0.00001,  # funding must be below this
        "funding_norm": 0.00008,         # |funding| mapping to ~full confidence
        "flat_price_pct": 10,            # |24h price move| below this = flat
        "min_oi_growth_pct": 8.0,        # OI surge since last snapshot
        "max_price_move_pct": 4.0,       # price-still-flat gate for surge
        "surge_norm_pct": 25.0,          # OI growth mapping to ~full confidence
        "min_confidence": 0.05,          # whale_accumulation_map confidence floor
        "mcp_min_confidence": 0.1,       # get_whale_signals (MCP) confidence floor
        "mcp_top_n": 10,                 # get_whale_signals result cap
    },
    # 动量回补（趋势回归重新入场）
    "momentum_reentry": {
        "enabled": False,
        "reclaim_pct": 1.0,
        "min_composite": 30,
    },
    # Runner/Mover 表面扫描（涨幅榜筛选）
    "runner_mover_surface": {
        "enabled": True,
        "min_crypto_24h_pct": 10.0,
        "min_hip3_24h_pct": 8.0,
        "min_volume_usd": 5_000_000,
    },
    # P2-3: in-process memory retention limits for AgentMemory. Previously
    # hardcoded module constants; operators can now resize the JSON cache /
    # event-log rebuild windows without code changes.
    "memory_limits": {
        "max_perceptions": 500,
        "max_analyses": 200,
        "max_trades": 100,
        "max_closes": 500,
        # R9/P3-4: age-based retention in days for the time-bounded lists.
        # 0 disables age eviction (trades are an audit record — count-capped
        # only). Records without a usable timestamp are never age-evicted.
        "max_age_days": {
            "perceptions": 30,
            "analyses": 30,
            "trades": 0,
        },
    },
    # R13-B11: memory equity quality-gate knobs (memory.py). Seven leaves:
    # the track_daily_pnl partial-dex degraded-read filter (implausible move
    # fraction, immediate-accept crash fraction, the same-tick filter window,
    # and the re-confirm streak), the avg_exit_slip_bps lookback window in
    # days and its minimum-sample bar, and the non-forced flush throttle in
    # seconds. They were previously a bare `_IMPLAUSIBLE_PCT = 0.25` literal
    # and `< 180` / `streak < 2` literals in track_daily_pnl, the
    # `days=30.0` / `min_samples=3` signature defaults of avg_exit_slip_bps,
    # and module-load os.environ.get reads
    # (HERMES_EQUITY_CRASH_DOWN_PCT / HERMES_MEMORY_FLUSH_THROTTLE_S) — never
    # in CANONICAL_DEFAULTS. memory._memory_quality_params() keeps the two
    # legacy env vars as the top-priority override, then falls through to
    # this block via cfg_get (HERMES_CFG_MEMORY_QUALITY__* env +
    # agent-config); the literals remain as the final fallback. Defaults
    # mirror the memory.py literals verbatim; behaviour unchanged.
    "memory_quality": {
        "implausible_pct": 0.25,
        "crash_down_pct": 0.40,
        "filter_window_sec": 180,
        "reconfirm_streak": 2,
        "slip_window_days": 30.0,
        "slip_min_samples": 3,
        "flush_throttle_s": 0.2,
    },
    # R13-B11: dashboard equity read-side quality-gate knobs (dashboard.py).
    # Four leaves: the equity-curve dip flag ratio and trailing reference
    # window (_equity_curve_payload partial-dex degraded-point flagging), the
    # summary heartbeat staleness threshold in seconds (_summary_payload
    # "scanning"/"stale" status), and the closed-trades cross-source
    # de-duplication window in ms (_closed_trades_payload). They were
    # previously module-load os.environ.get reads
    # (HERMES_EQUITY_DIP_RATIO / HERMES_EQUITY_DIP_WINDOW /
    # HERMES_CLOSED_TRADES_DEDUP_MS) and a bare `> 180` literal — never in
    # CANONICAL_DEFAULTS. dashboard._dashboard_equity_params() keeps every
    # legacy env var as the top-priority override, then falls through to this
    # block via cfg_get (HERMES_CFG_DASHBOARD_EQUITY__* env + agent-config);
    # the module-level _EQUITY_DIP_RATIO / _EQUITY_DIP_WINDOW attributes stay
    # (tests monkeypatch.setattr them) and are read live as the literal
    # fallback layer. Defaults mirror the dashboard.py literals verbatim;
    # behaviour unchanged.
    "dashboard_equity": {
        "dip_ratio": 0.7,
        "dip_window": 15,
        "stale_tick_age_s": 180,
        "dedup_window_ms": 5000,
    },
    # R13-B12: HTTP-edge cache TTLs (dashboard.py / public.py / server.py).
    # Five keys cover the three public JSON poll endpoints, the per-coin
    # research verdict cache, and the TTL-cache singleflight waiter timeout.
    # Legacy HERMES_SUMMARY_TTL_S / HERMES_EQUITY_CURVE_TTL_S /
    # HERMES_CLOSED_TRADES_TTL_S / HERMES_RESEARCH_HTTP_CACHE_S env vars
    # remain the top-priority channel; ttl_load_wait_s was a bare literal.
    "http_cache": {
        "summary_ttl_s": 2.0,
        "equity_curve_ttl_s": 30.0,
        "closed_trades_ttl_s": 10.0,
        "research_cache_ttl_s": 30.0,
        "ttl_load_wait_s": 60.0,
    },
    # R13-B13: Hyperliquid client-layer knobs (client/exchange.py /
    # hl_client.py / ws_client.py). Twelve leaves cover the SDK HTTP timeout,
    # the cross-margin fallback leverage, the IOC slippage caps, the meta /
    # ATR / candle / funding cache TTLs+sizes, and the WebSocket staleness /
    # heartbeat / sequence tolerances. Legacy HERMES_HL_SDK_TIMEOUT_S /
    # HERMES_DEFAULT_LEVERAGE / HERMES_MAX_SLIPPAGE_PCT /
    # HERMES_MAX_SLIPPAGE_CLOSE_PCT / HERMES_META_TTL_S / HERMES_ATR_TTL_S /
    # HERMES_CANDLE_CACHE_TTL_S / HERMES_CANDLE_CACHE_MAX /
    # HERMES_FUNDING_CACHE_TTL_S / HERMES_WS_MAX_STALE_SECONDS /
    # HERMES_WS_HEARTBEAT_S / HERMES_WS_SEQ_MAX_BACKWARD env vars remain the
    # top-priority channel. default_leverage (5) is the cross-margin fallback
    # only — NOT the top-level trading `leverage` (10); the two never merged.
    "hl_client_io": {
        "sdk_timeout_s": 30.0,
        "default_leverage": 5,
        "max_slippage_pct": 1.5,
        "max_slippage_close_pct": 5.0,
        "meta_ttl_s": 3600.0,
        "atr_ttl_s": 60.0,
        "candle_cache_ttl_s": 90.0,
        "candle_cache_max": 512,
        "funding_cache_ttl_s": 300.0,
        "ws_max_stale_s": 30,
        "ws_heartbeat_s": 10.0,
        "ws_seq_max_backward": 1024,
        "ws_max_tick_jump_frac": 0.25,
        # Audit 2026-09-06 (F6, engineering hygiene): explicit per-call timeout
        # for L2 order-book snapshots (the pre-trade spread gate) and a short
        # positive-result cache. l2_timeout_s bounds a hung l2Book request
        # independently of sdk_timeout_s; l2_cache_ttl_s lets the diagnostic
        # fetch and the pre-trade gate share one snapshot within a cycle.
        "l2_timeout_s": 5.0,
        "l2_cache_ttl_s": 2.0,
    },
    # R13-B13: Hyperliquid rate-limiter knobs (client/rate_limit.py +
    # hl_client.py call sites). Seven leaves cover token-bucket refill /
    # capacity, the trading-path max budget wait, the 429 retry count, the
    # opportunistic (observability) budget wait, and the two gate switches
    # (cross-process shared bucket; in-process per-endpoint serialization).
    # Legacy HERMES_HL_RATE_REFILL_PER_SEC / HERMES_HL_RATE_CAPACITY /
    # HERMES_HL_RATE_MAX_WAIT_S / HERMES_HL_429_RETRIES /
    # HERMES_HL_RATE_OPPORTUNISTIC_WAIT_S / HERMES_HL_RATE_SHARED /
    # HERMES_HL_RATE_PER_ENDPOINT_GATE env vars remain the top-priority
    # channel (the gate switch keeps its historical call-time env read so a
    # post-import toggle still takes effect). The shared-bucket state FILE
    # path stays an env-only deployment knob.
    "hl_rate_limit": {
        "rate_refill_per_sec": 20.0,
        "rate_capacity": 600,
        "rate_max_wait_s": 30.0,
        "rate_429_retries": 2,
        "rate_opportunistic_wait_s": 2.0,
        "rate_shared": True,
        "rate_per_endpoint_gate": True,
    },
    # Audit 2026-09-06 (F6, engineering hygiene): Binance second-source price
    # crosscheck knobs (client/price_crosscheck.py). The timeout was a hardcoded
    # 2.5s module literal and the other three leaves were env-only
    # (HERMES_PRICE_CROSSCHECK_*); they now resolve through the canonical block
    # with the legacy env vars kept as the top-priority channel in the module.
    "price_crosscheck": {
        "http_timeout_s": 2.5,       # Binance ticker GET per-call bound
        "ttl_s": 10.0,               # short per-symbol price cache
        "warn_bps": 30.0,            # 0.30% divergence -> alert
        "block_bps": 100.0,          # 1.00% divergence -> block entry
    },
    # P2-3: bps the exchange backup stop sits behind the DSL floor (executor
    # SL ratchet coordination); and the funding-rate history lookback window
    # in hours (research display / against-funding context).
    "sl_buffer_bps": 10.0,
    # H4 (deep audit 2026-08-29): assumed maintenance-margin rate (PERCENT)
    # for the pre-trade liquidation-price estimate. The gate requires
    # liq_distance_pct (= 100/leverage - this rate) > stop_distance_pct +
    # sl_buffer. HL's actual maintenance margin is tier-dependent; 1.0% is a
    # conservative flat assumption for small perps. Set to 0 to disable.
    "liquidation_maint_margin_pct": 1.0,
    "funding_lookback_hours": 24,
    # R13-B2: dynamic exchange-SL mover (executor.py L304-311) tunables.
    # Two knobs control how aggressively the trailing SL follows the DSL
    # floor in Phase 2:
    #   * min_interval_sec — per-coin throttle on batchModify (avoid
    #     spamming HL cancel+replace every tick / respect rate limit)
    #   * min_bps — minimum relative-to-entry move (in bps) that
    #     justifies a cancel+replace (filters micro-ratchets)
    # Were hardcoded module-level constants (_SL_MOVE_MIN_INTERVAL_SEC=30.0,
    # _SL_MOVE_MIN_BPS=15.0) at executor.py L304/307; perception / R12
    # audit flagged them as unobservable + not env-overridable + not
    # dashboard-dumpable. sl_buffer_bps above is already plumbed via
    # cfg_get at executor L2389; this block brings the other two knobs
    # into parity. Defaults match the existing literals verbatim.
    "sl_move": {
        "min_interval_sec": 30.0,
        "min_bps": 15.0,
    },
    # R9/P2-3: news gate freshness window (days) and the short-TTL Brave
    # headline cache (seconds). Were hardcoded module constants in research.py.
    "news_freshness_days": 2,
    "news_cache_ttl_s": 120,
    # Audit 2026-09-06 (F6, engineering hygiene): per-request bound for the
    # Brave Search news call in research.py. Was a hardcoded timeout=10.0
    # literal invisible to the config dump / dashboard overrides.
    "news_http_timeout_s": 10.0,
    # P3-2: research-path LLM circuit breaker. After fail_threshold consecutive
    # hard failures (non-success HTTP / network error) the breaker opens for
    # cooldown_s and _call_openrouter short-circuits to "" so a dead upstream
    # can't pile up 60s-timeout calls across every coin each tick; callers
    # already degrade gracefully on empty. Mirrors the dashboard chat breaker.
    "llm_circuit_breaker": {
        "fail_threshold": 3,
        "cooldown_s": 300,
    },
    # Per-coin parameter overrides; deep-merged on top of the base config by
    # with_coin_overrides() / executor. Empty by default.
    "coin_overrides": {},
    # R12-C1: layered trading circuit breakers (executor post-close path).
    # A single coin's realized spot loss >= single_coin_loss_pct halts new
    # entries in that coin for single_coin_halt_min; cumulative daily PnL
    # loss >= daily_loss_pct of start-of-day equity halts ALL entries for
    # daily_halt_min. Thresholds were implicit cfg_get(..., default=) values
    # in executor.py and invisible to operators / config audit. Set a halt
    # duration to 0 to disable that layer.
    "circuit_breaker": {
        "single_coin_loss_pct": 3.0,
        "single_coin_halt_min": 60.0,
        "daily_loss_pct": 5.0,
        "daily_halt_min": 120.0,
        # B-F2 (deep audit 2026-08-28): consecutive losing closes on one coin
        # before new entries on that coin are blocked. The streak was recorded
        # in memory (record_loss_outcome) but no gate ever read it. <=0 disables.
        "consecutive_loss_limit": 3,
        # B-F6: per-coin CUMULATIVE daily realized loss, as % of start-of-day
        # equity. Backs the per_coin_daily_loss_gate; complements the per-trade
        # single_coin_loss_pct (one large spot loss) with the many-small-loss
        # accumulation case. <=0 disables.
        "coin_daily_loss_pct": 5.0,
        # B-F7: account-wide max drawdown from the all-time equity high-water
        # mark, as a %. Blocks ALL new entries beyond it (a kill-switch the
        # daily-loss gate cannot cover: a slow multi-day grind down trips no
        # single day's limit but still blows the account). <=0 disables.
        "max_drawdown_pct": 15.0,
        # H6/C-M3 (deep audit 2026-08-28): after an order placement whose
        # response was LOST (408/read-timeout/SSL drop), the executor polls
        # userFills to decide filled vs not-filled. When the exchange itself
        # is unreachable the outcome stays unknown (a possible orphan); N
        # consecutive unresolvable outcomes trigger a global auto-entry halt
        # for halt_min minutes so we stop spraying orders at a deaf exchange.
        # halt_n <= 0 disables the halt (rehydrate still runs each time).
        "resp_unknown_halt_n": 3,
        "resp_unknown_halt_min": 60.0,
        # Audit 2026-09-04 P1-10: drawdown-freeze rolling window & cooldown were
        # read via caller-default literals (14 days / 24 hours) in memory.py and
        # never registered here, so they couldn't be tuned via config/panel/CLI
        # and were invisible to config diffs. Registered now (hot-reloadable).
        "drawdown_peak_window_days": 14.0,
        "drawdown_cooldown_hours": 24.0,
    },
    # market_circuit (roadmap §3, 2026-09-04): MARKET-level tail-risk breaker.
    # circuit_breaker above halts after THIS bot loses money (per-coin/daily
    # PnL); this block adds non-PnL market triggers that fire BEFORE the loss
    # flows through our own fills — an index flash crash (BTC/ETH short-window
    # drop), a correlated cluster of our own DSL stops, and (opt-in) extreme
    # index funding. A trip arms the EXISTING global halt (set_global_halt),
    # which global_halt_gate enforces and bm11_breaker_flatten turns into a
    # hard flatten when auto_flatten_on_global_halt is on (default on).
    # mode mirrors ta_late_entry's gray-release: "off" = absent;
    # "shadow" = verdict/metrics/JSONL recorded but halt NEVER armed (DEFAULT —
    # audit 2026-09-04 P1-12: was "off", leaving index-crash/stop-cluster/
    # funding tail protection completely inert; shadow first to observe trip
    # frequency, then arm to "enforce" after review); "enforce" = arms the
    # global halt. False trips flatten a healthy book, so the defaults are
    # deliberately conservative and every trigger is independently
    # disable-able.
    "market_circuit": {
        "mode": "shadow",
        # --- Trigger 1: index short-window crash (BTC/ETH) ---
        "index_crash_enabled": True,
        # Candle timeframe for the crash leg ("1m"/"5m"/"15m"). The watched
        # proxies are fixed to BTC/ETH in market_circuit.py (DEFAULT_INDEX_COINS)
        # — the whole crypto book correlates to BTC and ETH is the #2 macro
        # asset; no per-trader knob is warranted for a tail circuit.
        "index_crash_interval": "5m",
        # Peak(high)-to-last-close drop, in %, over the last N CLOSED bars
        # that trips (e.g. 2.0 = a >=2% fall within ~15min on 5m bars).
        "index_crash_pct": 2.0,
        "index_crash_window_bars": 3,
        # --- Trigger 2: correlated DSL-stop cluster (our own book) ---
        "stop_cluster_enabled": True,
        # Distinct coins whose DSL stops fire within this window that trip.
        "stop_cluster_window_s": 180.0,
        "stop_cluster_min_coins": 3,
        # --- Trigger 3: extreme index funding (off by default; opt-in) ---
        "funding_enabled": False,
        # Funding rate as a FRACTION per interval (0.005 = 0.5% = ~50bp).
        "funding_extreme_frac": 0.005,
        # --- Disposal / pacing ---
        # Global-halt duration armed on an enforce trip (minutes).
        "halt_minutes": 60.0,
        # While a halt with at least this many minutes remaining is already
        # armed, don't re-arm / re-alert (sustained-crash dedup).
        "cooldown_minutes": 60.0,
        # Candle fetch sizing (per index coin; cache is shared/90s).
        "fetch_bars": 20,
        # --- shadow verdict log (JSONL); empty = container default path ---
        "shadow_log_path": "",
    },
    # Audit 2026-09-06 (D4): TA pre-filter volume-surge confirmation
    # (ta_filter._check_volume_confirm). Previously the 1.2x / 20-bar numbers
    # were hardcoded literals in the pure function; registering them here makes
    # the confirmation gate configurable / env-overridable without touching
    # code. Defaults mirror the literals verbatim (last closed bar must be >=
    # min_ratio x the prior lookback-bar average volume), so behaviour is
    # unchanged with no config file.
    "volume_confirm": {
        "min_ratio": 1.2,
        "lookback": 20,
    },
    # confidence_decay (roadmap §2, 2026-09-04): AI-conviction freshness
    # decay. The debate cache replays the same LONG/SHORT verdict for minutes
    # after the entry window has passed, so an aged conviction is multiplied
    # by exp(-age/halflife) — the same time-decay math as the trigger
    # age-decay below, applied to the AI's confidence instead of the TA
    # composite score. mode off|shadow|enforce; off = absent (DEFAULT —
    # production flips to shadow via HERMES_CONFIDENCE_DECAY_MODE; no env →
    # zero behavior change). halflife_s is the verdict half-life in seconds
    # (900 = 15 min). Structural PASS→LONG overrides are never decayed.
    "confidence_decay": {
        "mode": "off",
        "halflife_s": 900.0,
        # --- shadow verdict log (JSONL); empty = container default path ---
        "shadow_log_path": "",
    },
    # signal_age_decay (roadmap §2, 2026-09-04): setup-age decay for the
    # perception composite score. Formation triggers keep firing at high
    # score on every bar after the breakout matures (the FARTCOIN top-tick
    # late-chase); each trigger's FIRST-fire bar per coin is tracked across
    # cycles and its weight decayed by exp(-onset_age/halflife). Pulse
    # triggers self-extinguish as velocity normalizes, so their halflife is
    # 0 = never decay (an extra age factor would double-penalize them).
    # halflife_s maps camelCase trigger names → seconds; onset_ttl_s prunes
    # stale onset state (6h). mode off|shadow|enforce, default off.
    "signal_age_decay": {
        "mode": "off",
        "halflife_s": {
            # 5m formation triggers: edge is the breakout bar itself.
            "breakout": 900.0,
            "trendStrength": 1800.0,
            "rangeCompression": 0.0,        # only feeds breakout coupling
            "trendFlip1h": 7200.0,          # 1h formation setups
            "higherLows1h": 7200.0,
            "volumeBuildup1h": 7200.0,
            "momentumContinuation1h": 7200.0,
            # Pulse triggers self-extinguish — never decay.
            "pctMoveSpike": 0.0,
            "volumeSpike": 0.0,
            "momentumBurst": 0.0,
        },
        # Stale-onset prune window, seconds (a setup quiet past this age
        # restarts its clock on the next fire).
        "onset_ttl_s": 21600.0,
        # --- shadow verdict log (JSONL); empty = container default path ---
        "shadow_log_path": "",
    },
    # atr_regime_calibration (roadmap §1, 2026-09-04): continuous ATR-volatility
    # regime factor for stop WIDTH sizing (sizing.atr_regime_calibration).
    # When current ATR% is far below its recent mean (compressed vol, ratio <
    # low_ratio), stops are tightened (factor down to min_mult); when far
    # above (expansion, ratio > high_ratio), stops are widened (up to
    # max_mult). Replaces the binary legacy ATR spike breaker in enforce mode.
    # mode off|shadow|enforce, default off (shadow via
    # HERMES_ATR_REGIME_CALIB_MODE in production).
    "atr_regime_calibration": {
        "mode": "off",
        # Regime thresholds: ratio = current ATR% / historical mean ATR%.
        "low_ratio": 0.6,
        "high_ratio": 1.6,
        # Factor at / beyond each threshold (clamped to [min_mult, max_mult]).
        "low_mult": 0.85,
        "high_mult": 1.20,
        "min_mult": 0.75,
        "max_mult": 1.35,
        # --- shadow verdict log (JSONL); empty = container default path ---
        "shadow_log_path": "",
    },
    # trend_filter_200ma (Wave D, 2026-09-06, ported from Pathia): daily-200
    # SMA trend filter for LONGS. A long is only allowed when the latest daily
    # close is at/above the daily SMA(`period`); shorts are never filtered (the
    # filter is a long-only macro-trend gate, matching Pathia). Fail semantics:
    #   * an ESTABLISHED coin whose daily candles cannot be fetched / computed
    #     FAILS OPEN with a WARNING when block_unknown=false (Pathia default) —
    #     a transient API error must not veto every long on the book;
    #   * a genuinely NEW listing (daily bars < min(period, min_history_bars
    #     floor)) FAILS CLOSED — a coin with no 200d history has no established
    #     trend and longing it is the unfilterable chase this gate exists to
    #     stop. block_unknown=true would also make the fetch-failure fail
    #     closed (opt-in hardening).
    # Bypass: a daily-mover long may bypass the filter ONLY when its 24h move
    # sits inside [daily_mover_min_ext_pct, daily_mover_max_ext_pct] — a strong
    # but not parabolic breakout may legitimately start above the 200MA before
    # price has reverted to it; a move past daily_mover_max_ext_pct is too
    # extended to bypass (the D2 extension ceiling independently refuses it).
    # mode off|shadow|enforce. 生产已 enforce（shadow 数据证明只拦历史不足新币，
    # 成熟币均放行）；canonical 对齐 enforce，使配置丢键深合并时不会静默关闭这个
    # 已生效的过滤器（第五轮复核新危险方向收口）。新部署因此默认启用——该保护
    # 已验证，且只对无 200d 趋势依据的新币 fail-closed，风险低。
    "trend_filter_200ma": {
        "mode": "enforce",
        "period": 200,
        # Fetch slightly more than `period` daily bars so the forming bar / a
        # short shortfall does not starve the SMA.
        "fetch_bars": 210,
        "block_unknown": False,
        "allow_daily_mover_long_bypass": True,
        "daily_mover_min_ext_pct": 10.0,
        "daily_mover_max_ext_pct": 30.0,
        # --- shadow verdict log (JSONL); empty = container default path ---
        "shadow_log_path": "",
    },
    # daily_extension_cap (Wave D / D2, Audit 2026-09-06, ported from Pathia
    # override_max_daily_extension_pct=30.0): hard 24h price-extension ceiling
    # for LONGS (anti-chase). The threshold itself is the root scalar
    # override_max_daily_extension_pct; this block only carries the gray-release
    # switch and the shadow-log path. Default mode SHADOW (unlike D1/D3 which
    # default off): Pathia ships the cap live, so Hermes probes would-blocks
    # while the shadow log collects data before flipping to enforce. env
    # override: HERMES_DAILY_EXTENSION_CAP_MODE.
    "daily_extension_cap": {
        "mode": "shadow",
        # Audit 2026-09-21 (#4): on a daily-change data miss, fail CLOSED.
        "fail_closed": True,
        # --- shadow verdict log (JSONL); empty = container default path ---
        "shadow_log_path": "",
    },
    # reentry_cap (Wave D, 2026-09-06, ported from Pathia): per-coin rolling
    # ENTRY counter. Blocks a new entry on a coin that has already had
    # `max_per_coin` entries (opens) within the last `window_hours`. This caps
    # stop-out-and-rebuy churn on a single name (the coin_circuit breaker only
    # arms on a large realized loss; this caps raw frequency regardless of PnL).
    # Counts OPENINGS only (record_trade), both sides. A memory read failure
    # FAILS OPEN (shared breaker convention). max_per_coin <= 0 disables.
    # mode off|shadow|enforce, default off (gray-release; shadow via
    # HERMES_REENTRY_CAP_MODE).
    "reentry_cap": {
        "mode": "off",
        "max_per_coin": 2,
        "window_hours": 24.0,
        # --- shadow verdict log (JSONL); empty = container default path ---
        "shadow_log_path": "",
    },
    # Audit 2026-09-07 (E6): xs_reversal oversold-bounce LONG shadow arm.
    # The M1 offline backtest (archive/scripts/backtest_xs_reversal.py)
    # falsified the original chop/neutral gate: regime_strength_score is
    # direction-agnostic and ~90% of extreme 3d drawdowns score TREND. The
    # validated edge is a downtrend oversold bounce (EMA8<EMA21 +
    # RSI[15,35)); the probe records every xs+awake trigger with the full
    # snapshot and flags is_candidate for that cell. Default off (inert);
    # shadow via HERMES_XS_REVERSAL_MODE. enforce is record-only in M2.
    #
    # E-4 键名规范（2026-09-20，W4）：此臂用三态 ``mode``（off|shadow|enforce，
    # env fail-closed，被 schema 与 test_xs_reversal_shadow 固化），它是其它臂
    # ``enabled`` 布尔 + ``shadow_mode`` 布尔两键组合的**严格超集**——mode 单键
    # 即可表达「关 / 只观测 / 执行」三态且不可误配成半开状态。故刻意不引入冗余
    # ``enabled`` 键；规范上认定 mode 为本臂（及一切需 enforce 门的影子臂）的
    # 权威启用键。纯布尔影子臂继续用 enabled/shadow_mode。
    "xs_reversal": {
        "mode": "off",             # off | shadow | enforce (enforce=record-only); 权威启用键（E-4）
        "shadow_log_path": "",     # empty = ~/.hermes-trading/xs_reversal_shadow.jsonl
        "lookback_d": 3,           # rolling highest-high window (days of 1h bars)
        "top_pct": 85,             # ext_pct bottom-tail percentile (M1: 90+ halves sample)
        "awake_bars": 7,           # activity window length
        "awake_min_frac": 0.67,    # min active-bar fraction (5 of 7)
        "rsi_long": 35.0,          # oversold confirmation ceiling (M1 sweet spot)
        "rsi_floor": 15.0,         # free-fall guardrail (non-binding in M1 sample)
    },
    # regime_risk_overlay (Wave E / E1, Audit 2026-09-06, Q2): book-level
    # AUTO DE-RISK switch for choppy/range markets. The macro regime comes
    # from detect_regime_with_score (BTC / equity proxy, EMA20/30 + ADX,
    # TTL-cached). After `hysteresis_bars` CONSECUTIVE non-trending samples
    # (chop/neutral) the book drops to the `chop` profile; after the same run
    # of trending samples (up/down) it restores to the `trend` profile. The
    # overlay can only TIGHTEN the operator's base knobs (min/AND), never
    # loosen them. Disabled by default (ships SHADOW-first: set enabled=true
    # while shadow_mode=true to record counterfactuals before enforcing).
    "regime_risk_overlay": {
        "enabled": False,
        # shadow_mode: posture flips and would-derisk is logged, but no knob
        # is actually changed (applied stays False). Set false to enforce.
        "shadow_mode": True,
        # Consecutive same-class macro samples required to flip posture
        # (debounces ADX wobbling around its chop threshold).
        "hysteresis_bars": 3,
        # Minimum seconds between state-machine steps (one step per fresh
        # macro sample; defaults to ~ the regime cache TTL so a single scan
        # over many coins does not advance the counter N times). 0 = step on
        # every call (tests).
        "sample_interval_s": 300.0,
        # De-risk profile (applied while in a confirmed chop/non-trend run).
        "chop": {
            "max_concurrent": 1,
            "allow_shorts": False,
            "equity_fraction_mult": 0.5,
            "pullback_long_enabled": False,
        },
        # Restored posture reference values (only the tightening direction
        # vs the operator base ever binds).
        "trend": {
            "max_concurrent": 4,
            "allow_shorts": True,
            "equity_fraction_mult": 1.0,
        },
        # --- shadow/transition verdict log (JSONL); empty = container default
        # path (~/.hermes-trading/regime_overlay_shadow.jsonl); env override
        # HERMES_REGIME_OVERLAY_SHADOW_FILE ---
        "shadow_log_path": "",
    },
    # ta_late_entry (deep audit 高危项, 2026-08-30): late-entry hard gate.
    # The same late_entry_check() pure function (agents/ta_filter.py) runs in
    # three places with ONE source of truth for thresholds:
    #   1. analyze_perception() — pre-filter before the paid LLM debate
    #   2. ta_late_entry_gate() — hard pre-trade gate in eval_all_gates()
    #   3. scripts/backtest.py — backtest entry path (rule parity)
    # mode controls the pre-trade GATE only (the pre-filter veto is always
    # active): "off" = gate absent; "enforce" = blocks orders (the DEFAULT).
    # SHADOW/LIVE PARITY (2026-09-04): the former "shadow" gray-release value
    # (record but never block) was removed — it made SHADOW silently skip the
    # gate while LIVE blocked, breaking 1:1 parity. The gate now enforces
    # identically in both modes; a stale "shadow" value is normalised to
    # "enforce" at read time. The verdict JSONL/metrics remain additive audit
    # output.
    # Thresholds: 4h RSI / extension-in-ATR veto is OR semantics; when 4h ADX
    # >= adx_trend_threshold and the EMA trend aligns with the trade side the
    # relaxed limits apply (trend exception); on a 4h veto with 15m RSI not
    # yet extreme the trade passes (multi-timeframe continuation override).
    # Per-trader tuning is via env HERMES_CFG_TA_LATE_ENTRY__<KEY>; no global
    # hard-coded numbers outside this block.
    "ta_late_entry": {
        "mode": "enforce",
        # Audit 2026-09-21 (#4): when 4h candle fetch/compute fails or data is
        # insufficient, fail CLOSED (block the trade) instead of the historical
        # fail-open. Set false to restore the old "blind gate passes" behaviour.
        "fail_closed": True,
        # --- 4h hard veto thresholds (normal regime) ---
        "rsi_ob": 75,
        "rsi_os": 25,
        "ext_ob": 2.5,
        "ext_os": -2.5,
        # --- trend exception: relax limits in a strong aligned trend ---
        "trend_relax_enabled": True,
        "adx_trend_threshold": 35,
        "rsi_ob_relaxed": 82,
        "rsi_os_relaxed": 18,
        "ext_ob_relaxed": 3.5,
        "ext_os_relaxed": -3.5,
        # --- relax_tier SHADOW probe (2026-09-11; observation only) ---
        # Tighter trend-strength counterfactuals scored into the gate shadow
        # log, never fed to the live veto. Enable toggle and thresholds so the
        # probe is env-tunable without code changes.
        "relax_tier_probe_enabled": True,
        "rt_relax_adx": 45,        # stricter relax floor (vs today's 35)
        "rt_weak_adx": 35,         # weak-trend ceiling for the high-RSI probe
        "rt_weak_rsi_long": 70,    # long high-RSI floor inside a weak trend
        "rt_weak_rsi_short": 30,   # short mirror
        "rt_no_trend_adx": 20,     # no-trend chase ceiling
        # --- multi-timeframe: 15m RSI continuation override ---
        # Phase 0 (deep audit R3, 2026-08-30): DEFAULT OFF. The 15m fetch is
        # the only cold candle HTTP in the gate path (the screen never warms
        # that key), and a small frame "veto of the veto" inverts the gate's
        # HTF-tail-filter semantics. Opt back in per-trader via env once
        # shadow evidence supports it.
        "mtf_enabled": False,
        "rsi15m_ob": 72,
        "rsi15m_os": 28,
        # --- data requirements / fetch sizing ---
        "min_bars_4h": 30,
        "min_bars_15m": 20,
        "fetch_bars": 100,
        # --- shadow verdict log (JSONL); empty = container default path ---
        "shadow_log_path": "",
        # Audit 2026-09-12 (#7): high-quality confirmed breakout exemption.
        # A fired breakout with RVOL >= require_rvol that trips the late-entry
        # veto is downgraded instead of prefilter-REJECTed. shadow_mode records
        # the would-downgrade but still REJECTs; enforce passes through (the
        # order-time ta_late_entry_gate still applies). Default SHADOW.
        "breakout_exemption": {
            "enabled": True,
            "shadow_mode": True,
            "require_rvol": 4.0,
        },
    },
    # R13-A1: perception scan-tick block (TRIGGER_CONFIG["scan"], perception.py
    # L216-269). Previously implicit: the keys lived only in the module-level
    # TRIGGER_CONFIG dict and perception read them via `config["scan"][key]`
    # without ever consulting read_agent_config() / cfg_get. The MCP server
    # therefore had to hard-code its own defaults (180s / 20) which silently
    # drifted from production (5m / 54). Registering the block here makes the
    # values configurable, env-overridable, dashboard-dumpable, and — most
    # importantly — gives the MCP server a single source of truth to read
    # from. Defaults mirror TRIGGER_CONFIG verbatim; behaviour is unchanged.
    "scan": {
        "minCompositeScore": 54,
        "candleInterval": "5m",
        "candleCount": 100,
        "cacheTtlMs": 50_000,
        "cacheTtlMs1h": 600_000,
        "evaluateClosedBarsOnly": True,
        "postCloseForceRefreshMs": 15_000,
    },
    # R13-B8: trigger composite-score weights (TRIGGER_CONFIG["weights"],
    # perception.py L316/323 -> triggers.composite_score). The 12 weights
    # previously lived only in the module-level TRIGGER_CONFIG dict and had no
    # env / dashboard / set-channel — an operator could not tune or even read
    # them at runtime. Leaf names are snake_case (canonical convention);
    # config.py.trigger_weights_params() maps them to the camelCase runtime
    # keys that composite_score indexes by trigger name. Six weights are
    # intentionally 0.0 (net-negative / surfacing-only triggers), hence the
    # >= 0 guard. Defaults mirror TRIGGER_CONFIG verbatim; behaviour unchanged.
    "trigger_weights": {
        # 近1年事件研究（scripts/audit_triggers_1y.py）：trend_strength
        # 触发率43%且负期望，原0.55权重过高 → 降到0.30。
        "trend_strength": 0.30,
        "pct_move_spike": 0.40,
        "breakout": 0.30,
        "volume_spike": 0.25,
        "momentum_burst": 0.20,
        "volume_buildup_1h": 0.15,
        "higher_lows_1h": 0.0,
        "trend_flip_1h": 0.0,
        "range_compression": 0.0,
        # 唯一 train/test 两段稳定正EV的trigger（虽小），从仅surfacing
        # 转为评分项，给0.20小权重。
        "uptrend_momentum": 0.20,
        "downtrend_momentum": 0.0,
        "daily_mover": 0.0,
    },
    # R13-B8: trigger thresholds (TRIGGER_CONFIG["thresholds"], perception.py
    # L272-300). Same registration gap as trigger_weights. Includes the D6
    # fix: trend_momentum_pct is 5.0 here and in TRIGGER_CONFIG — the
    # perception dict.get fallback (L298/300) and the
    # uptrend_momentum/downtrend_momentum signature defaults (triggers.py
    # L397/419) used to silently say 3.0 (the value that over-surfaced 22
    # triggers/scan at ~4.5x AI cost); they are dead fallbacks (runtime is
    # always 5.0 via get_config) but a missing thresholds key would silently
    # resurrect 3.0, so all four now agree. Leaf names snake_case; mapped to
    # camelCase runtime keys by trigger_thresholds_params(). Six keys are
    # floats (> 0 guard), ten are ints (>= 1 guard). Defaults verbatim.
    "trigger_thresholds": {
        "sigma_threshold": 2.0,
        "trend_momentum_lookback": 72,
        "trend_momentum_pct": 5.0,
        "breakout_lookback": 48,
        "breakout_min_rvol": 1.8,
        "breakout_rvol_window": 20,
        "breakout_atr_score_mult": 3.0,
        "breakout_confirm_bars": 3,
        "bb_length": 20,
        "bb_std_dev": 2,
        "adx_period": 14,
        # 1年事件研究：2bar/4% 使 momentumBurst 近1年只触发9次，放宽到
        # 3bar/3%（与 TRIGGER_CONFIG 同步）。
        "momentum_lookback": 3,
        "momentum_pct": 3.0,
        "vol_buildup_ratio": 2.5,
        "trend_flip_bars": 3,
        "higher_lows_required": 4,
    },
    # R13-B9: perception scan_budget knobs (perception.py scan_once). Twelve
    # leaves covering the candle-cache size, the per-scan market budget split
    # (total / HIP-3 reservation / movers slots / USD volume floors), the
    # rotating universe sweep, batch rate-limit pacing, thread-pool width, the
    # movers |24h%| cut-off, and the per-future timeout. They were previously
    # read only via os.environ.get(HERMES_*, <literal>) at scan time and never
    # appeared in CANONICAL_DEFAULTS — invisible to dashboard dump /
    # validate_config_updates and un-tunable via the canonical config channel.
    # perception.scan_budget_params() keeps every legacy HERMES_* env var as
    # the top-priority override (MCP server writes HERMES_MAX_MARKETS; existing
    # test/operator knobs keep working), then falls through to this block via
    # cfg_get (HERMES_CFG_SCAN_BUDGET__* env + agent-config). Zero is a legal
    # "reserved disabled" value for budget slots / sweep / sleep; cache size,
    # batch size, the movers % cut-off and the timeout must be >= 1 / > 0.
    # Defaults mirror the perception literals verbatim; behaviour unchanged.
    "scan_budget": {
        "cache_max": 512,
        "max_markets": 60,
        "max_markets_hip3": 25,
        "max_markets_movers": 10,
        "movers_vol_floor_usd": 300_000.0,
        "hip3_movers_floor_usd": 50_000.0,
        "universe_sweep": 0,
        "batch_size": 20,
        "batch_sleep_sec": 0.3,
        "parallel_workers": 32,
        "movers_min_pct": 1.0,
        "future_timeout_sec": 60,
    },
    # R13-B10: research-path LLM call knobs (research.py _call_openrouter /
    # _debate_direct). Eleven leaves covering the gateway model/base URL, the
    # sampling temperature, the normal-path vs debate-path response token
    # budgets, the read/connect httpx timeouts, the 429/5xx retry budget with
    # its exponential backoff base/cap, and the finish_reason=length
    # continuation turn budget. They were previously either local literals
    # inside _call_openrouter (temperature / timeouts / retries / backoff /
    # continuations) or read only via OPENROUTER_* env vars, and never appeared
    # in CANONICAL_DEFAULTS — invisible to dashboard dump /
    # validate_config_updates and un-tunable via the canonical config channel.
    # research.research_llm_params() keeps OPENROUTER_MODEL /
    # OPENROUTER_BASE_URL as the top-priority override (operator gateway
    # routing), then falls through to this block via cfg_get
    # (HERMES_CFG_RESEARCH_LLM__* env + agent-config). OPENROUTER_API_KEY stays
    # a bare secret env var and is deliberately NOT registered here. Defaults
    # mirror the research.py literals verbatim; behaviour unchanged.
    "research_llm": {
        "model": "deepseek-v4-flash",
        "base_url": "https://openrouter.ai/api/v1",
        "temperature": 0.1,
        "max_tokens": 500,
        "debate_max_tokens": 350,
        # Audit 2026-09-04 P0-7: unified with debate_research.max_latency_s=25
        # so a debate abort and the LLM per-call timeout line up — no residual
        # 12s of quota burn after the debate gives up.
        # Audit 2026-09-23: real Ark verdicts (long prompt + reasoning tokens)
        # show median ~38s / p85 ~53s; the old 25s cap killed 76% of successful
        # reads. Raised to 55s to cover p85 in one attempt; retries=1.
        "timeout_sec": 55.0,
        "connect_timeout_sec": 5.0,
        "retries": 1,
        "backoff_base_sec": 1.0,
        "backoff_cap_sec": 15.0,
        # Audit 2026-09-04 P1-13: continuations=2 meant an incomplete LLM reply
        # auto-continued up to twice → worst case 25s×3 = 75s per call, well
        # beyond the debate latency budget. Capped at 1.
        "continuations": 1,
        # Audit 2026-09-03 P0-2: hard per-call cap for the single-LLM
        # fallback path (debate failed -> _call_ai). 0 disables the cap
        # (legacy 60s inheritance). Mirrors research.py literal verbatim.
        # Audit 2026-09-04 P0-7: aligned to 25s in sync with research_llm
        # timeout_sec and debate max_latency_s.
        "fallback_timeout_sec": 55.0,
    },
    # 2026-09-23: shadow probe for capping TOTAL output (reasoning + answer)
    # via OpenAI's max_completion_tokens. Unlike max_tokens (which bounds only
    # the answer on this model), max_completion_tokens bounds the chain-of-
    # thought too — the part that actually drives latency. This block is
    # COUNTERFACTUAL ONLY: it never alters the real request. After each call it
    # reads the real usage and records (to a shadow jsonl) whether the proposed
    # cap would have truncated the output, plus the observed token counts /
    # wall time. Promote to "enforce" only after the shadow shows truncation is
    # rare and verdicts still parse. mode: off | shadow | enforce.
    "completion_cap_shadow": {
        "mode": "shadow",
        # Proposed ceiling for reasoning_tokens + answer completion_tokens.
        # Raised 1200→5000: real calls show reasoning 1475–5434, so 1200
        # truncated 100% of samples. 5000 keeps the rare heavy-high tail
        # observable while measuring the residual truncation rate.
        "max_completion_tokens": 5000,
        # Also record the counterfactual per-call timeout a cap would imply
        # (informational; used later to pick a tighter read timeout). 0 skips.
        "implied_timeout_sec": 40.0,
        "log_path": "",   # empty = <data_dir>/completion_cap_shadow.jsonl
        "sample_rate": 1.0,  # fraction of calls to record (0–1)
    },
    # 2026-09-23: canary rollout for OpenAI's reasoning_effort. Controlled
    # testing on real verdicts proved low cuts reasoning tokens ~39% and wall
    # time ~48% vs high, without truncating the final answer. This block is the
    # safe path to production:
    #   off      → never send the parameter (legacy behaviour)
    #   shadow   → don't send, just record the counterfactual (no traffic change)
    #   enforce  → actually attach reasoning_effort=<effort>; sample_rate lets a
    #              small % of traffic go first, the rest stays legacy
    # Verdicts remain valid because low only shrinks the (unseen) chain of
    # thought, not the emitted JSON.
    "reasoning_effort_rollout": {
        "mode": "enforce",
        "effort": "low",          # none | low | high | max
        "sample_rate": 1.0,       # enforce: fraction of requests to apply low
        "log_path": "",           # empty = <data_dir>/reasoning_effort_rollout.jsonl
    },
    # P0-1 (strategy-paradigm optimisation 2026-10-04): cross-signal ranking of
    # the pre-research job queue. Defaults to SHADOW so the ranking/counterfactual
    # is recorded without changing any order flow; promote to enforce only after
    # the ranked selection shows a better expectation.
    #   off / shadow / enforce ; top_k bounds how many gated coins proceed to the
    # paid LLM research when enforce. score_weights mirror signal_rank defaults
    # (relative weights, normalised internally).
    "signal_ranking": {
        "mode": "shadow",
        "top_k": 5,
        "log_path": "",           # empty = <data_dir>/signal_ranking.jsonl
        "score_weights": {
            "composite": 0.55,
            "trigger_quality": 0.20,
            "whale": 0.08,
            "cvd": 0.07,
            "liquidity": 0.05,
            "class_penalty": 0.05,
        },
    },
    # P1-1 (strategy-paradigm optimisation 2026-10-04): active unstucking of
    # underwater positions. Defaults to SHADOW so the would-close is recorded
    # without realising any loss; promote to enforce only after the unstick
    # selection proves it reduces drawdown / frees productive margin. It only
    # activates under drawdown pressure or full stuck-slots, so it never churns
    # normal trades.
    "unstucking": {
        "mode": "shadow",
        "max_peak_drawdown_pct": 12.0,  # equity within 12% of peak triggers
        "max_stuck_slots": 3,           # >=3 stuck slots triggers (0=off)
        "min_urgency": 20.0,            # ignore very weak nudges
        "log_path": "",                 # empty = <data_dir>/unstucking.jsonl
    },
    # P1-2 (strategy-paradigm optimisation 2026-10-04): Forager-inspired
    # continuous ranking of the eligible scan pool, replacing the hard
    # volume/movers buckets. Defaults to SHADOW so the counterfactual top-k is
    # logged against the buckets without changing any markets; promote to
    # enforce only after the continuous selection shows a higher trigger/expectancy
    # hit rate. pre_weights are relative and normalised internally.
    "coin_selection": {
        "mode": "shadow",
        "top_k": 55,
        "log_path": "",                 # empty = <data_dir>/coin_selection.jsonl
        "pre_weights": {
            "turnover": 0.5,
            "momentum": 0.35,
            "funding_oi": 0.15,
        },
    },
    # R13-B10: research-path concurrency / prefetch knobs (research.py
    # _get_pool / _http / _signals_block / _parallel_prefetch). Nine leaves
    # covering the shared ThreadPoolExecutor width, the reused httpx client's
    # keepalive/total connection-pool limits, the inner signals-block future
    # timeout, the per-source prefetch fallback ceiling, and the four
    # per-source fetch ceilings (candles / funding / news / signals). They
    # were previously read only via os.environ.get(HERMES_RESEARCH_*,
    # <literal>) or hardcoded as httpx.Limits(...) literals, and never
    # appeared in CANONICAL_DEFAULTS. research.research_fetch_params() keeps
    # every legacy HERMES_RESEARCH_* env var (including the
    # HERMES_RESEARCH_FETCH_TIMEOUT_<SOURCE> family) as the top-priority
    # override, then falls through to this block via cfg_get
    # (HERMES_CFG_RESEARCH_FETCH__* env + agent-config). Pool width and
    # connection limits are read lazily at pool/client construction, so
    # config changes take effect on the next process (the singletons are
    # built once). Defaults mirror the research.py literals verbatim;
    # behaviour unchanged.
    "research_fetch": {
        "pool_workers": 16,
        "max_connections": 16,
        "max_keepalive_connections": 8,
        "signals_timeout_sec": 40.0,
        "fetch_timeout_default_sec": 45.0,
        "fetch_timeout_candles_sec": 15.0,
        "fetch_timeout_funding_sec": 8.0,
        "fetch_timeout_news_sec": 10.0,
        "fetch_timeout_signals_sec": 12.0,
    },
    # R13-B1: DSL state-file I/O tunables (dsl_exit.py L65/77/86/1061/1063).
    # The five knobs (process-wide save throttle, dashboard force-reload TTL,
    # ExitPolicy cache TTL, save retry attempts, save backoff base) were
    # previously read only via os.environ.get(HERMES_DSL_*, <literal>) at
    # module-load. They never appeared in CANONICAL_DEFAULTS, so MCP
    # server / dashboard dump / validate_config_updates could neither
    # observe nor override them — a real operational blind spot on the
    # hot path (every WS mid tick reads them). Defaults match the existing
    # literals verbatim; behaviour is unchanged. Legacy HERMES_DSL_* env
    # vars continue to take precedence (operator override) and the
    # canonical env route (HERMES_CFG_DSL_STATE_IO__*) also works.
    "dsl_state_io": {
        "save_min_interval_sec": 2.0,
        "force_load_ttl_s": 1.0,
        "policy_cache_ttl_s": 5.0,
        "save_max_attempts": 3,
        "save_backoff_base_sec": 0.1,
        "save_backoff_factor": 3,
    },
    # R13-B3: risk-gate scoring thresholds (risk_gates.py). Eight keys cover
    # the market_regime_gate's counter-trend score bar plus the debate_gate
    # analyst2 / analyst5 thresholds — all of which used to be hardcoded
    # module-level literals. They were therefore not env-overridable, not
    # dashboard-dumpable, and not auditable via the canonical schema. The
    # canonical default values match the existing literals verbatim so the
    # runtime bar is unchanged; only the *path* changes (cfg_get with module
    # default). Subagent audit had flagged the 50.0 / 60.0 "implicit
    # mismatch" at L474/L484 as a drift candidate, but on close reading
    # the two numbers are NOT inconsistent: 50.0 is the *normal*
    # counter-trend bar (L474 default) and 60.0 is the *elevated*
    # against-funding bar (L488 override). The L474 literal was the actual
    # dead default — it now resolves through cfg_get to its canonical twin.
    "analyst_scoring": {
        # market_regime_gate / _counter_trend_decision: composite_score bar
        # for the plain (non-against-funding) counter-trend case.
        "counter_trend_min_score": 50.0,
        # debate_gate analyst2 (confidence-vs-composite alignment): three
        # conditional branches at risk_gates L623-628.
        "analyst2_high_conf": 0.7,
        "analyst2_high_score": 40,
        "analyst2_mid_conf": 0.5,
        "analyst2_mid_score": 60,
        "analyst2_very_high_conf": 0.8,
        "analyst2_very_high_score": 20,
        # debate_gate analyst5 (whale boost): confidence floor that lets a
        # non-whale trade still earn the vote at risk_gates L645.
        # Audit 2026-09-04 P1-9: was 0.75, ABOVE the min_ai_confidence entry
        # gate (0.62). A trade in [0.62, 0.75) cleared the entry gate but
        # systematically lost this 5th vote, slashing debate-gate pass rate —
        # inconsistent with the "0.62 is enough to enter" intent. Aligned to
        # the entry gate; a501b00 lowered that gate 0.62→0.60, so this floor
        # follows to 0.60 (any confidence that can enter can earn the vote).
        "analyst5_whale_or_conf": 0.60,
    },
    # R13-B4: executor execution-path constants (executor.py). Three gaps:
    #   * tp_atr_mult DRIFT FIX — the key was already registered (above) and
    #     server.py / research.py read it, but the executor's actual TP
    #     placement (_place_tp_scale_out and the maybe_execute final_tp) used
    #     the module constant TP_ATR_MULT=1.0 and NEVER read cfg, so an
    #     operator tuning it to 1.2/1.5 made AI advice / backtest / live
    #     order disagree. The hot path now resolves it via cfg_get.
    #   * sl_ceiling_hard_max_pct — the HYPE-43%-incident hard clamp on the
    #     backup-SL width was a function-local literal 15.0 (executor
    #     L2296), not configurable / observable / env-overridable.
    #   * liq_buffer_usd + execution block — the P0-4 liquidation pre-place
    #     gate threshold (env-only HERMES_LIQ_BUFFER_USD=10) and the HL
    #     taker-fee bookkeeping constants (HERMES_TAKER_FEE_PCT=0.025
    #     env-only; round_trip_fills=2 a pure hardcode) were invisible to
    #     the canonical schema / dashboard / config audit.
    # Legacy env vars (HERMES_LIQ_BUFFER_USD / HERMES_TAKER_FEE_PCT) keep
    # precedence for backward compat; canonical defaults match the existing
    # literals verbatim so behaviour is unchanged when nothing is set.
    "sl_ceiling_hard_max_pct": 15.0,
    "liq_buffer_usd": 10.0,
    "execution": {
        # Hyperliquid perp taker fee in PERCENT (HL = 2.5bps = 0.025%), used
        # to model round-trip entry+exit cost in realized-PnL bookkeeping.
        "taker_fee_pct": 0.025,
        # Number of taker fills modeled per round trip (entry + exit = 2).
        "round_trip_fills": 2,
    },
    # SHADOW-mode paper-trading ledger (shadow_book.py). When mode=SHADOW and a
    # decision passes EVERY risk gate, instead of only returning
    # "shadow_mode_would_execute" the engine books a VIRTUAL fill into an
    # isolated paper account, marks it to live mids each loop, runs the SAME
    # DSL exit policy the live engine uses, and books a virtual close +
    # realized PnL when a stop / target / timeout fires. Nothing here touches
    # the real exchange, real orders, or the real .agent-memory ledger — it is
    # a decision-rehearsal book that lets the dashboard show what the strategy
    # WOULD have done with a configurable virtual bankroll.
    "shadow_book": {
        # Book virtual fills while in SHADOW mode. When false the engine keeps
        # the old "log-only" shadow behaviour (no paper positions).
        "enabled": True,
        # Virtual USDC the paper account starts with (operator-configurable).
        "starting_balance": 10000.0,
        # Per-fill taker fee in PERCENT, modeled on close across the round trip
        # (mirrors execution.taker_fee_pct; HL = 0.025% per fill).
        "taker_fee_pct": 0.025,
        # Number of fills per round trip (entry + exit = 2).
        "round_trip_fills": 2,
        # Cap on concurrent virtual positions. SHADOW/LIVE PARITY: the paper
        # book MUST admit exactly the same number of concurrent positions a
        # live book would — a higher cap would let SHADOW book entries the
        # live max_concurrent gate blocks, skewing backtest stats. This key is
        # therefore OMITTED from the canonical default: shadow_book
        # _max_positions() falls back to the global ``max_concurrent`` when
        # unset, so the two caps can never drift apart. Operators may set it
        # explicitly (production pins both to 2); a mismatch DRIFT-warns.
    },
    # R12-C1: optional lower confidence floor for regime-aligned entries
    # (LONG in up-trend / SHORT in down-trend). None = feature off (the
    # global min_ai_confidence applies uniformly). Was implicit via
    # config.get("aligned_min_conf") in risk_gates.
    # Audit 2026-09-04 P0-5: production set this to null (feature off), which
    # silently disabled the aligned-trend relaxation with no warning. The code
    # comment documents 0.78 as the LONG-side calibration. Enable it at 0.60 —
    # below the production min_ai_confidence=0.62 — so a WITH-TREND entry gets
    # a genuine lower bar while neutral/counter-trend keep the default 0.62.
    "aligned_min_conf": 0.60,
    # P1-6: trading_loop runtime knobs (scripts/trading_loop.py). Eighteen
    # leaves covering the loop log file path, the surge-postmortem notify
    # threshold, the self-heal watchdog timeout, the intra-cycle exit
    # checkpoint throttle, the meta-cache prewarm bound, the universe
    # refresh TTL, the HL-budget startup grace, the base scan cadence, the
    # P0-1 dynamic-cadence trio (on / fresh window / fast / slow), the P0-2
    # WS fill-wake switch, the P0-3 ws_status event switch + hold/fresh
    # windows, and the P0-4 research-parallel switch + pool width. They
    # were previously read only via os.environ.get(HERMES_*, <literal>) at
    # scripts/trading_loop.py module load and never appeared in
    # CANONICAL_DEFAULTS — invisible to dashboard dump /
    # validate_config_updates and un-tunable via the canonical config
    # channel. loop_runtime.loop_runtime_params() keeps every legacy
    # HERMES_* env var as the top-priority override (operator / compose /
    # k8s-configmap knobs keep working), then falls through to this block
    # via cfg_get (HERMES_CFG_LOOP_RUNTIME__* env + agent-config). The four
    # boolean switches are coerced with the same 1/true/yes/on truth-set the
    # loop used inline; the loop_log_path stays a string. Defaults mirror
    # the trading_loop.py literals verbatim; behaviour unchanged — this is
    # a startup-tuning block, not a strategy block.
    "loop_runtime": {
        "loop_log_path": "/data/trading-loop.log",
        "surge_min_score": 40.0,
        "watchdog_timeout_s": 600,
        "exit_checkpoint_min_interval_s": 5.0,
        "meta_prewarm_timeout_s": 3.0,
        "universe_refresh_s": 1800,
        "startup_grace_s": 12.0,
        "scan_interval": 15,
        "scan_dynamic": False,
        "scan_fresh_s": 10.0,
        "scan_interval_fast": 8,
        "scan_interval_slow": 20,
        "ws_fill_wake": False,
        "ws_status_event": False,
        "ws_status_hold_s": 30.0,
        "ws_status_fresh_s": 10.0,
        "research_parallel": True,
        "research_parallel_workers": 4,
        # 2026-09-04: per-scan jobs backpressure cap (mirrors
        # loop_runtime.LOOP_RUNTIME_DEFAULTS); 0 disables the cap.
        "research_max_jobs_per_scan": 8,
        # 2026-09-23: hard wall-clock cap for the whole parallel research
        # batch. The batch blocks the main loop on the slowest coin
        # (fut.result() has no per-future timeout); this bounds the total wait
        # so a stall can never stretch a scan toward the 600s watchdog. Any
        # coin still unfinished when the budget expires is dropped and routed
        # as a conservative PASS. 0 disables the cap (legacy behaviour).
        # Tightened 240→180 for a more aggressive scan cadence.
        "research_batch_timeout_s": 180.0,
    },
    # Audit 2026-09-10 (risk-tuning shadow): volatility/score de-leverage
    # arm read by executor.py (`_lev_tier`). shadow_mode=false keeps it an
    # inert would-deleverage probe; when the block is present with
    # shadow_mode=false the executor may still ENFORCE the tier (its own
    # convention), so the canonical default mirrors the production block.
    # The values below match the production .agent-config.json verbatim.
    "leverage_tier_shadow": {
        "shadow_mode": False,
        "atr_pct_max": 3.5,
        "min_composite": 40,
        "low_leverage": 5,
    },
    # 坑1 (2026-09-15): own-4h gap demote threshold (%) read via cfg_get
    # in risk_gates.market_regime_gate. 0.0 = overlay disabled (the
    # revert switch); production runs 15, aligned as canonical default.
    "own_gap_demote_pct": 15.0,
    # 配置文件注释字段（不参与交易逻辑）
    "_comment": "",
}

