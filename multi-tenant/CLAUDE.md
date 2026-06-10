# multi-tenant/ — Claude instructions

Multi-tenant self-hosted Supabase platform: many isolated client backends on
one VPS. **This directory is independent of the Studio monorepo around it** —
monorepo conventions (pnpm, React, Tailwind) do NOT apply here.

Read first: [`../PROJECT_STATUS.md`](../PROJECT_STATUS.md) (current state,
production facts, gotchas) and [`README.md`](README.md) (operations).
Decisions live in [`../MULTI_TENANT_ARCHITECTURE.md`](../MULTI_TENANT_ARCHITECTURE.md);
per-client migration in [`MIGRATION.md`](MIGRATION.md).

## Architecture in one breath

Per tenant: own Postgres + GoTrue + PostgREST (+ optional Realtime/Storage/Studio
via compose profiles), own JWT secret + keys, hard Docker RAM/CPU caps, tenant
database NEVER on the shared network. Shared: one Caddy (wildcard TLS, host
routing replicating Kong's path map) on the external `mt-proxy` network.
`tenantctl` is the single engine; the panel is a thin UI over it.

## Key files

| File | Role |
| --- | --- |
| `tenantctl` | Bash CLI — ALL tenant operations live here |
| `templates/docker-compose.yml` | Per-tenant stack (copied verbatim; reads tenant `.env`) |
| `templates/tenant.env.tpl`, `templates/caddy-*.tpl` | Rendered with `@@VAR@@` placeholders |
| `shared/` | Shared Caddy compose + Caddyfile (imports `tenants/*.caddy`) |
| `panel/panel.py` | Admin UI — single file, Python stdlib ONLY |
| `bootstrap-vps.sh` | Fresh-server setup: hardening FIRST, then Docker + init |

Runtime state (gitignored, never commit): `tenants/`, `backups/`, `config.env`,
`shared/caddy/tenants/*.caddy`.

## Hard rules

1. **All tenant logic goes in `tenantctl`.** The panel, docs, and cron call it —
   never duplicate its logic elsewhere.
2. **`panel/panel.py` stays zero-dependency** (Python stdlib, embedded HTML/CSS/JS,
   no build step) and **binds to 127.0.0.1 only** — it can control Docker, so it
   must never be publicly exposed or get a ufw rule.
3. **Never fork Supabase services** for multi-tenancy; isolation lives in the
   orchestration layer. Upgrades must stay an image-tag bump (tags are pinned in
   `templates/docker-compose.yml`).
4. **No edge-functions runtime** — deliberate. Client serverless = a FastAPI/Express
   container per tenant, optionally routed at `/functions/v1/*` via a custom Caddy
   snippet.
5. After changing `templates/caddy-site.tpl`, existing tenants need
   `tenantctl rerender <name>` — routes do not regenerate themselves.
6. Caddy routes must mirror Kong's path map (`docker/volumes/api/kong.yml`),
   including transforms — e.g. `/graphql/v1` requires the
   `Content-Profile: graphql_public` header.
7. Inter-service URLs in the compose template use **tenant-prefixed container
   names** (`${TENANT_NAME}-rest`), never bare service names — bare aliases
   collide on the shared network. The Realtime container name must keep its
   `realtime-dev.` prefix (tenant id is parsed from the Host header).
8. Migrations follow `MIGRATION.md` exactly: dump/restore as `supabase_admin`
   (not `postgres`), always run the §2b pg_graphql fixup, `NOTIFY pgrst, 'reload
   schema'` after out-of-band DDL.
9. Secrets never go into git, chat replies, or PR text. Tenant `.env` files are
   chmod 600.
10. Validate changes against a real Docker daemon when possible (`bash -n` +
    `docker compose config` minimum). The full validation history and known
    gotchas are in `PROJECT_STATUS.md` — extend it when you learn something new.

## Production (live system — be careful)

Hetzner `167.233.76.46` (4 GB test tier), base domain `db.backend.stream`
(wildcard DNS, Cloudflare **grey-cloud/DNS-only**). Repo at `~/database-clients`
(sparse checkout), branch `claude/epic-allen-zz92as`; deploy = `git pull` (+
`systemctl restart supabase-mt-panel` for panel changes, `tenantctl rerender`
per tenant for route-template changes). Real client tenants run here — a
server-side agent with SSH executes operations; cloud sessions build tooling
and push to PR #1.
