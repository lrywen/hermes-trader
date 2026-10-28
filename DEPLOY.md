# Deploy hermes-trader

> ## ⚠️ 当前生产部署方式：自建主机 Docker Compose（2026-10 起）
>
> 生产环境**不再使用 Fly.io**。下文 Fly 章节仅作历史参考，值班/回滚请以本节为准。
>
> - 部署目录：`/home/ldy/hermes-deploy`（`docker-compose.yml` + 绑定挂载的
>   `.agent-config.json` / `.secrets.json` / `.env.local`）。
> - 镜像：`hermes-deploy-hermes-trader`；容器/服务名：`hermes-trader`；
>   命名卷 `hermes_data:/data`；仅 `expose 8000`（经内网 nginx，不直接发布）。
> - 一键发布（源码仓库内）：`bash scripts/deploy_prod.sh`
>   （预检 → 同步 → `compose build` → `up -d --force-recreate`（保留 /data 卷）
>   → 健康轮询 → `postdeploy_smoke` → `postdeploy_shadow_checks`）。
> - 健康检查：compose healthcheck 调容器内 `/healthcheck.sh`
>   （HTTP `/api/health` + trading_loop 进程存活 + 心跳 ≤300s）。
> - 运行模式安全默认：`mode` 不进 prod_override 快照（重建回 canonical OFF）；
>   生产运行配置当前为 **SHADOW**、`allow_shorts=false`、maker 执行关闭。
>   LIVE 需 env `HERMES_ENABLE_LIVE` + B-13 HMAC 限时准入记录双闸同时满足。
> - 回滚锚点：见每次 `deploy_prod.sh` 结束时打印的 git commit 与回滚步骤；
>   镜像/容器由 compose 管理，可用旧镜像 tag 重建。
>
> ---
>
> # （历史）Fly.io 部署

# Deploy hermes-trader to Fly.io

Single-machine Fly deployment running the dashboard (public) + trading loop (private)
out of one image, sharing one persistent volume for `.dsl-state.json`,
`.agent-memory.json`, `.agent-config.json`, and the session log.

Cost: roughly **$2–4/month** for two `shared-cpu-1x` VMs + 1GB volume.

## One-time setup

```bash
# 1. Install Fly CLI (Mac)
brew install flyctl
flyctl auth signup   # or `flyctl auth login` if you already have an account

# 2. From the repo root, launch the app (skips automatic deploy so we can wire secrets first)
flyctl launch --no-deploy --copy-config

# When prompted:
#   - App name: pick something unique (e.g. hermes-trader-julian)
#   - Region: pick one near you (iad / ord / fra / nrt …)
#   - Postgres / Redis: NO
#   - Deploy now: NO

# 3. Create the persistent volume (one per region; size = 1GB is plenty)
flyctl volumes create hermes_data --size 1 --region iad

# 4. Wire secrets — never put these in fly.toml or the image
flyctl secrets set \
  OPENROUTER_API_KEY="sk-or-..." \
  HYPERLIQUID_WALLET_ADDRESS="0x..." \
  HYPERLIQUID_PRIVATE_KEY="0x..." \
  HERMES_OPERATOR_TOKEN="$(openssl rand -hex 16)"

# Optional secrets
flyctl secrets set BRAVE_API_KEY="BSA..."                    # news in research
flyctl secrets set HYPERLIQUID_MASTER_ADDRESS="0x..."        # agent-wallet setup

# 5. First deploy
flyctl deploy
```

After a successful deploy you'll get a URL like `https://hermes-trader-julian.fly.dev`.
The dashboard and operator console are now Vue SPA pages served at `/web/`
(`/` redirects there). The operator console is at `/web/operator`; unlock it
in-page with the `HERMES_OPERATOR_TOKEN` (sent via the `X-Operator-Token`
header, never in the URL).

> **Note:** this repo's Dockerfile does not build or `COPY` the SPA — the
> `hermes-web/dist` bundle lives in the separate `hermes-web` repo. A bare
> Fly image therefore serves only the JSON API (`/api/*`, `/metrics`); to
> get the UI on Fly, add a build step that bakes `hermes-web/dist` into
> `/app/web-dist`. The docker-compose deployment gets the SPA automatically
> via bind-mount.

## Reading + rotating secrets

```bash
flyctl secrets list                                # names only, never values
flyctl secrets set HERMES_OPERATOR_TOKEN="$(openssl rand -hex 16)"   # rotate
flyctl secrets unset BRAVE_API_KEY                 # remove
```

Setting or unsetting a secret triggers a rolling redeploy.

## Tailing logs

```bash
flyctl logs                          # combined web + loop
flyctl logs -i web                   # dashboard server only
flyctl logs -i loop                  # trading loop only
```

## Pausing the bot without redeploying

```bash
flyctl machines stop --process-group loop                # halt trading loop
flyctl machines start --process-group loop               # resume
```

The web process keeps serving the dashboard either way — operators can read state
without the loop running, useful for incident response.

You can also flip the mode in the operator console (set mode `OFF`) — the loop
stays alive but stops opening new positions; existing positions still get DSL-managed.

## Deploying new code

```bash
git push          # main branch; doesn't trigger deploy
flyctl deploy     # builds image + rolls both processes
```

For zero-downtime deploys, Fly does the web rolling restart for free. The loop
process briefly drops; the next scan tick (~60s later) resumes.

## Post-deploy verification

`scripts/deploy_prod.sh` already runs `scripts/postdeploy_smoke.py` inside the
image and fails the deploy on a Traceback in the first ~3 minutes. The scripts
below are the **shadow-evidence** checks added for the 2026-10-27 maker-v2 /
panic-gate / dual-gate work. They are all read-only and open no orders; run them
inside the container (`cd /app`) after enough scan cycles have elapsed. They
write nothing and exit non-zero only where noted.

### 0. One-command shadow health (panic gate + maker v2)

```bash
python scripts/postdeploy_shadow_checks.py            # auto-detects v2 from the book
# after explicitly enabling maker v2, force the expectation:
python scripts/postdeploy_shadow_checks.py --expect-v2 on --min-v2-decided 20
echo $?   # 0 OK (incl. pre-warm WARN), 2 FAIL (wiring defect), 3 MISSING
```

It reads the real `ShadowBook` (`adverse_selection.v2`) and
`xs_reversal_shadow.jsonl` and catches the two "enabled-but-silent" failures:
- maker v2 returns `FAIL` if the book shows >=20 touched cancels with 0 fills
  (`zero_fill_warning`) or a sub-1% fill rate after an adequate sample;
- panic gate returns `FAIL` if trigger rows exist but none carry `panic_gate_*`
  fields (deployed image predates the record-only gate). When the arm hasn't
  triggered yet it falls back to the xs heartbeat (`/data/.xs-reversal.state`)
  for liveness. Use `--json` for machine output. The detailed checks below give
  the underlying counters.

### 1. maker v2 fill model (only if `shadow_book.maker_fill_model="v2"`)

The v2 deterministic wiring must NOT produce a silent empty ledger (the 2026-10-27
defect: the old `p>=1` rule never filled at base=0.30). After the shadow book has
decided >=20 maker orders, inspect the maker shadow summary (`adverse_selection.v2`):

- `zero_fill_warning` must be `false`. `true` means >=20 orders were touched but
  0 filled — the probability wiring regressed; do not trust v2 evidence.
- `fill_rate_pct` should sit near the calibrated ~9–30% band (1m real-data
  offline replay ~9%; long-run deterministic expectation tracks base 0.30), never
  0. v1 (default) is unchanged and does not emit a v2 block.

### 2. WF-C scan coverage

Evidence comes from the already-enabled `coin_selection` shadow log
(`mode=shadow, top_k=55`); no extra instrumentation is needed.

```bash
# static structure from a universe snapshot (works immediately, no waiting):
python scripts/wfc_scan_coverage_counterfactual.py \
  --meta ~/.hermes/universe_cache/meta.json
# measured multi-cycle coverage once /data/coin_selection.jsonl has >=30 cycles:
python scripts/wfc_scan_coverage_from_log.py --file /data/coin_selection.jsonl
```

Decision criteria (measured, not assumed):
- `union_coverage_pct` should approach ~100% of the **sweep ring** (markets with
  24h notional >= `scan_budget.movers_vol_floor_usd`, 101 of 234 in the 2026-10-20
  snapshot; the other 133 are intentionally-excluded pico-caps). Static expectation
  is full ring coverage within ~7 cycles.
- Sub-$300k markets being absent is by design; do not widen `max_markets` on the
  basis of single-cycle coverage (the 60-cap already covers ~98.6% of notional).
- Act only if `score_only_coins_never_bucket_scanned` repeatedly contains
  **liquid ($1–4M) oversold** coins with positive forward returns in the
  `selection_attribution` output — then consider a dedicated oversold entry
  channel, not a blanket cap increase (HL ~1200 weight/min budget).

### 3. WF-C min_composite 40/45 dual gate (record-only)

Every runner-gate verdict now logs `passes40`/`passes45` to
`runner_gate_shadow.jsonl`. The live gate is unchanged.

```bash
python scripts/wfc_dual_gate_counts.py --file /data/runner_gate_shadow.jsonl --days 14
```

Do NOT change `min_composite` from 45 unless the `[40,45)` band, over enough
verdicts, stays **positive end-to-end** in an AI-gated A/B. The counters only
start accumulating after the deploy that includes this field.

### 4. Error-rate alerting

```bash
python scripts/wfc_error_rate_alert.py --file /data/session-log.jsonl --warn 100 --days 1
echo $?   # 0 OK, 2 threshold breached, 3 log missing
```

Investigate any day whose ERROR count breaches `--warn` (the 2026-10-23 spike was
~958: max-retries / funding timeout / 429 backoff). Suitable for a cron watch.

## SSH into the running machine

```bash
flyctl ssh console -s
ls /data                              # see the persisted state files
tail -f /data/session-log.jsonl       # live activity from inside the box
```

## Backing up volume state

```bash
flyctl ssh sftp shell
get /data/.dsl-state.json
get /data/.agent-memory.json
get /data/session-log.jsonl
```

Or set up a periodic snapshot:

```bash
flyctl volumes snapshots create hermes_data
flyctl volumes snapshots list hermes_data
```

## Production parameter overrides (rebuild-safe)

There are **two layers of config**, and they must stay separate:

- **`CANONICAL_DEFAULTS`** in `hermes_trader/agents/config_defaults.py` — the
  audited, sentinel-guarded baseline. It mirrors the code-module literals and
  is locked by ~40 "no-drift" tests. **Do not put production tuning here.**
- **`/data/.agent-config.json`** — the live override on the persistent volume,
  deep-merged over the canonical defaults at read time
  (`config_store.read_agent_config`). This is where tuning lives.

`deploy/prod_override.json` is a **versioned snapshot of the production tuning**
(captured from the live host; ~56 strategy leaves over canonical). It lets you
reproduce the live parameters after a volume reset or container rebuild **without
touching the canonical baseline**.

### Apply after a rebuild / onto a fresh volume

```bash
# 1. dry-run first: validates the snapshot + prints every leaf that differs
#    from canonical (writes nothing)
python scripts/apply_prod_override.py

# 2. apply for real (backs up the current file, then writes atomically through
#    config_store)
python scripts/apply_prod_override.py --apply

# custom snapshot / target path
python scripts/apply_prod_override.py \
  --snapshot deploy/prod_override.json \
  --config  /data/.agent-config.json --apply
```

On the docker host (192.168.124.65), the repo lives inside the image, so copy the
snapshot in and run the bundled script there:

```bash
docker cp deploy/prod_override.json hermes-trader:/tmp/prod_override.json
docker exec hermes-trader python3 scripts/apply_prod_override.py \
  --snapshot /tmp/prod_override.json            # dry-run
docker exec hermes-trader python3 scripts/apply_prod_override.py \
  --snapshot /tmp/prod_override.json --apply    # write
```

Config is hot-reloaded by mtime, so no restart is needed; newly opened positions
pick up the new parameters (existing positions keep the bracket locked at entry).

### Runtime `mode` is deliberately NOT in the snapshot

`mode` (`OFF` / `SHADOW` / `LIVE`) is deployment **state**, not strategy tuning.
The snapshot excludes it, and `apply_prod_override.py` **strips any `mode` key by
default**. A rebuilt host therefore inherits the safe canonical default (`OFF`)
and never starts trading just because a snapshot was applied. After applying,
flip the mode explicitly in the operator console (or edit `/data/.agent-config.json`).

To deliberately carry the runtime mode (rare; e.g. cloning an exact environment),
pass `--with-mode` — the script prints a loud warning before writing it.

### Refreshing the snapshot

After intentionally tuning production, re-capture the live override so the repo
copy stays current:

```bash
docker exec -i hermes-trader python3 - > deploy/prod_override.json <<'PY'
import json
cfg = json.load(open("/data/.agent-config.json"))
cfg.pop("mode", None)          # never version the runtime trading mode
print(json.dumps(cfg, ensure_ascii=False, indent=2, sort_keys=True))
PY
```

Then dry-run `apply_prod_override.py` to confirm validation is clean before
committing the updated `deploy/prod_override.json`.

## Switching to a custom domain

```bash
flyctl certs add hermes.yourdomain.com
flyctl certs show hermes.yourdomain.com    # follow the DNS-CNAME instructions
```

## Common gotchas

- **Both processes need the volume mount.** The trading loop writes
  `.dsl-state.json`, the web process reads it. Both `processes = ["web", "loop"]`
  is intentional in `[[mounts]]`.
- **Mode defaults to OFF.** First boot writes `/data/.agent-config.json` with
  `mode: "OFF"`. Open the operator console and flip to `LIVE` once you've
  verified the dashboard.
- **Time on Fly is UTC.** The dashboard converts to your browser's local zone;
  the session log timestamps are epoch ms (timezone-agnostic).
- **The `~/.hermes-trader.pid` file** that the FastAPI start/stop endpoints
  reference is meaningless in a container — Fly handles process supervision.
  Those endpoints will report stopped state inside Fly; ignore them and use
  `flyctl machines stop --process-group loop` instead.
