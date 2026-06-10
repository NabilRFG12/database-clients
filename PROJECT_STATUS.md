# Project Status — Multi-Tenant Self-Hosted Supabase

> Last updated: 2026-06-10. Companion docs: [`MULTI_TENANT_ARCHITECTURE.md`](MULTI_TENANT_ARCHITECTURE.md) (decisions & research), [`multi-tenant/README.md`](multi-tenant/README.md) (operations), [`multi-tenant/MIGRATION.md`](multi-tenant/MIGRATION.md) (per-client migration runbook).

## What this project is

Consolidate ~11 client backends (previously one VPS + one self-hosted Supabase each) onto **one VPS** with hard isolation per client: own Postgres + GoTrue + PostgREST (+ optional Realtime/Storage/Studio), own JWT secret and API keys, hard Docker RAM/CPU caps, one shared Caddy with wildcard TLS, all driven by a `tenantctl` CLI and a dark admin panel.

## Where we are

**The platform is LIVE in production** and the first real client migration is queued.

| Milestone                                                       | Status                                                            |
| --------------------------------------------------------------- | ----------------------------------------------------------------- |
| Architecture decided + prior-art research                       | ✅ documented in `MULTI_TENANT_ARCHITECTURE.md`                   |
| `tenantctl` engine + per-tenant compose template + shared Caddy | ✅ built, validated in sandbox AND production                     |
| `bootstrap-vps.sh` (harden-first server setup)                  | ✅ used for the production deploy                                 |
| Admin panel (shadcn-style dark UI, zero-dependency Python)      | ✅ live, incl. VPS KPIs (CPU/RAM/disk + tenant headroom)          |
| Migration tooling (identity import, `add-domain`, runbook)      | ✅ built + full rehearsal passed in sandbox                       |
| First real client migration (Comarbois Validateur)              | 🔜 prompt issued to the local (SSH) agent; awaiting execution     |
| Remaining ~10 client migrations                                 | ⏳ after the first one proves out                                 |
| Off-site backup shipping                                        | ❌ NOT DONE — nightly dumps stay on the VPS only                  |
| Scale to bigger VPS (16 GB)                                     | ⏳ when client count requires; tenant dirs are portable by design |

## Production environment

- **Server**: Hetzner VPS `167.233.76.46`, Ubuntu 26.04, 2 vCPU / 4 GB (test-tier; fits ~3–4 full or 5–6 light tenants). Hardened: ufw (22/80/443 only), fail2ban, unattended upgrades, SSH key-only.
- **Base domain**: `db.backend.stream` — wildcard `*.db.backend.stream` → server IP, via Cloudflare (**must stay DNS-only/grey cloud**; CF free does not proxy wildcards; beware the duplicate-zone trap that bit us once).
- **Repo on server**: `~/database-clients`, sparse checkout (`multi-tenant` + `docker/volumes/db`), branch `claude/epic-allen-zz92as`. Deploys = `git pull`.
- **Tenants live**: `demo` (test), `comarboisagent` (real, full services + studio).
- **Admin panel**: systemd `supabase-mt-panel`, bound to `127.0.0.1:8800` ONLY. Access from the owner's Mac: `ssh supabase-panel` (alias with port-forward) → `http://localhost:8800`, user `admin`, password in server's `multi-tenant/config.env`.
- **Backups**: nightly 03:00 cron → `multi-tenant/backups/` (on-box only — see gaps).
- A local (laptop) Claude Code agent has SSH access and executes server-side work; this cloud session builds the tooling and pushes to the PR: https://github.com/NabilRFG12/database-clients/pull/1

## What was validated (evidence, not hope)

- **Sandbox (real Docker daemon)**: tenant create/suspend/resume/backup/delete; auth signup returning sessions; REST + GraphQL round-trips; **cross-tenant isolation** (tenant A's service_role key rejected by tenant B with PGRST301); Studio behind basic auth; panel API full lifecycle; idle RAM ≈ 107 MiB per minimal tenant.
- **Production**: full bootstrap, real Let's Encrypt certs (API + Studio), signup/REST/Studio all green; tenant ≈ 410–750 MiB with full services.
- **Migration rehearsal (sandbox)**: dumped tenant A, restored into a new tenant created with A's imported identity → **old keys worked against the migrated tenant** (REST and GraphQL), auth users carried over. Caught two real bugs (see gotchas).

## Things that must be noted (gotchas & lessons learned)

1. **Dump/restore as `supabase_admin`, never `postgres`** — postgres role yields ~600 ownership errors on auth/storage schemas; supabase_admin yields ~4 benign ones.
2. **Always run the post-restore fixup** (MIGRATION.md §2b) — pg_graphql loses its `graphql_public.graphql` wrapper + grants on restore.
3. **PostgREST schema cache** — after any out-of-band DDL: `NOTIFY pgrst, 'reload schema';` or new tables 404.
4. **After editing `templates/caddy-site.tpl`** run `tenantctl rerender <name>` per existing tenant — routes don't regenerate themselves. (The `/graphql/v1` route needs the `Content-Profile: graphql_public` header — already fixed; this is how we found the rule.)
5. **Registry rate limits** — Docker Hub anonymous pulls throttle on datacenter IPs; mirrors: `public.ecr.aws/supabase/<image>`, `mirror.gcr.io/library/<image>`, then `docker tag`.
6. **Run `bootstrap-vps.sh` detached** (tmux/setsid) — it restarts sshd mid-run. Verify key login from a second session before anything disables password auth. Hetzner forces a password change on first login.
7. **Secrets pasted into chats are exposed** — it happened with the demo tenant creds and panel password; rotation was advised (demo: delete/recreate; panel: new PANEL_PASSWORD + service restart — verify these were actually done). Credentials flow: tenantctl/panel → app config, never through conversations.
8. **No edge functions in this platform** (deliberate). Client serverless needs → small FastAPI/Express container per tenant; the same `/functions/v1/*` path can be routed to it via a custom Caddy snippet so deployed apps don't change.
9. **Suspended tenants** keep their Caddy route → visitors get 502, not a friendly page.
10. **Studio access = full admin of that tenant** (no roles in self-hosted Studio) — grant deliberately; restricted-Postgres-role trick exists for "viewer" clients.

## Next actions (in order)

1. Execute the **Comarbois Validateur** migration (prompt already written; no edge functions confirmed; container prefix on OLD is `comarbois-validateur-*`; SMTP creds on OLD are empty — fix during migration; owner flips DNS for `validateur-api.backends.space`).
2. **Off-site backups** — ship `backups/` to S3/B2 or a second box nightly (owner to choose target; then a small cron script).
3. Per-tenant **SMTP** + `ENABLE_EMAIL_AUTOCONFIRM=false` for production auth flows.
4. Migrate remaining clients one at a time (smallest first); move to a 16 GB VPS when headroom KPI demands.
5. Decommission old VPSes only after each client's rollback window passes.
