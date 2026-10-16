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
