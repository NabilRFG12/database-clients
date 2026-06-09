# Multi-Tenant Self-Hosted Supabase

Run isolated Supabase backends for many clients on **one VPS**: one shared
Caddy reverse proxy, one trimmed Supabase stack per client with hard Docker
resource caps, and a `tenantctl` CLI that onboards a new client in about a
minute.

Design background and decisions: [`../MULTI_TENANT_ARCHITECTURE.md`](../MULTI_TENANT_ARCHITECTURE.md).

```
                       ┌───────────────── VPS ─────────────────┐
 acme.api.you.com ────►│  Caddy (TLS + host routing)           │
 beta.api.you.com ────►│    │                                  │
 studio.acme.api... ──►│    │ (basic auth, opt-in per tenant)  │
                       │    ▼                                  │
                       │  tenant "acme": db + auth + rest      │
                       │                 (+ realtime/storage/  │
                       │                  studio, opt-in)      │
                       │  tenant "beta": db + auth + rest      │
                       │  ...                                  │
                       │  tenantctl ← create/suspend/backup/...│
                       └───────────────────────────────────────┘
```

## What each tenant gets

| Piece                          | Isolation                                                               |
| ------------------------------ | ----------------------------------------------------------------------- |
| Postgres container             | Own database, own password, own RAM/CPU caps                            |
| GoTrue (auth)                  | Own `auth` schema and users                                             |
| PostgREST                      | Own REST API at `https://<name>.<base-domain>/rest/v1/`                 |
| JWT secret + anon/service keys | Unique per tenant — a leaked key opens one client only                  |
| Realtime / Storage             | Optional, only if that client's app uses them                           |
| Studio + postgres-meta         | Optional, behind per-tenant basic auth at `studio.<name>.<base-domain>` |

Dropped relative to the official single-project stack: Kong (replaced by the
shared Caddy), Logflare/analytics, Vector, imgproxy (image transformation is
off), edge functions, Supavisor. A minimal tenant idles around 350–500 MB.

## Prerequisites

- Linux server with Docker Engine + Compose v2, ports 80/443 free
- A wildcard DNS record: `*.api.example.com -> <server IP>`
- `openssl` (for secret/key generation)

## Deploying a fresh VPS (recommended path)

Buy an Ubuntu 24.04 VPS (16 GB RAM / 4 vCPU / 100 GB+ disk is comfortable
for ~11 light clients), SSH in as root, then:

```bash
# Lean clone — downloads only the files this platform needs, not the whole monorepo
git clone --depth 1 --filter=blob:none --sparse \
  -b claude/epic-allen-zz92as https://github.com/NabilRFG12/database-clients.git
cd database-clients
git sparse-checkout set multi-tenant docker/volumes/db
cd multi-tenant

bash bootstrap-vps.sh --base-domain api.example.com --email you@example.com
```

`bootstrap-vps.sh` hardens first, deploys second: firewall (only SSH/80/443
open), fail2ban, automatic security updates, then Docker, `tenantctl init`,
and a nightly 03:00 backup cron (`tenantctl backup-all`). Pass
`--ssh-pubkey "ssh-ed25519 AAAA..."` to also install your key and disable
password login in the same run.

It finishes by printing the wildcard DNS record to add
(`*.api.example.com -> <server IP>`) and the smoke-test command. Off-site
copying of `backups/` is the one thing it can't do for you — set that up
before onboarding real clients.

## Quickstart

```bash
cd multi-tenant

# 1. One-time setup: config, mt-proxy network, shared Caddy
./tenantctl init --base-domain api.example.com --email you@example.com

# 2. Onboard a client
./tenantctl create acme --ram 2g --cpus 2 --services storage

#    -> prints SUPABASE_URL / SUPABASE_ANON_KEY / SUPABASE_SERVICE_ROLE_KEY
#       ready to paste into the client app's config
```

## Commands

| Command                                                                                | What it does                                                                                |
| -------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------- |
| `tenantctl init --base-domain D --email E`                                             | Write `config.env`, create the `mt-proxy` network, start Caddy                              |
| `tenantctl create NAME [--ram 1g] [--cpus 1] [--services realtime,storage] [--studio]` | Generate secrets, render the stack, start it, wire the Caddy route, print credentials       |
| `tenantctl list`                                                                       | All tenants with running/suspended state                                                    |
| `tenantctl info NAME`                                                                  | Re-print a tenant's credentials                                                             |
| `tenantctl suspend NAME` / `resume NAME`                                               | Stop/start containers; suspended tenants use zero RAM, data kept                            |
| `tenantctl backup NAME`                                                                | `pg_dump` to `backups/NAME-<timestamp>.sql.gz`                                              |
| `tenantctl backup-all`                                                                 | Back up every running tenant (used by the nightly cron)                                     |
| `tenantctl upgrade NAME`                                                               | Pull current image tags and recreate (edit tags in the tenant's `docker-compose.yml` first) |
| `tenantctl studio NAME on\|off`                                                        | Grant/revoke client Studio access (adds the route + basic-auth credentials)                 |
| `tenantctl delete NAME`                                                                | Destroy a tenant **including data** — asks for confirmation; back up first                  |

## Layout

```
multi-tenant/
├── tenantctl                  # the CLI (the engine — a future admin panel calls this)
├── templates/
│   ├── docker-compose.yml     # per-tenant stack (profiles: realtime, storage, studio)
│   ├── tenant.env.tpl         # per-tenant secrets/settings
│   ├── caddy-site.tpl         # per-tenant API routes
│   └── caddy-studio.tpl       # per-tenant Studio route (basic auth)
├── shared/
│   ├── docker-compose.yml     # the shared Caddy
│   └── caddy/Caddyfile        # imports tenants/*.caddy
├── tenants/<name>/            # runtime: .env, compose file, db data, storage files (gitignored)
├── shared/caddy/tenants/      # runtime: generated routes (gitignored)
└── backups/                   # runtime: pg_dump output (gitignored)
```

## Routing

Caddy replicates the official Kong path mapping per tenant domain:

| Public path          | Upstream                            |
| -------------------- | ----------------------------------- |
| `/auth/v1/*`         | GoTrue `:9999` (prefix stripped)    |
| `/rest/v1/*`         | PostgREST `:3000` (prefix stripped) |
| `/graphql/v1`        | PostgREST `/rpc/graphql`            |
| `/realtime/v1/api/*` | Realtime `/api/*`                   |
| `/realtime/v1/*`     | Realtime `/socket/*` (websocket)    |
| `/storage/v1/*`      | Storage `:5000` (prefix stripped)   |

One intentional difference from Kong: there is no gateway-level `apikey`
check. JWT verification still happens in PostgREST/GoTrue/Storage/Realtime
themselves (supabase-js sends the key as a Bearer token), so authorization is
unchanged — but unauthenticated requests reach the services instead of being
rejected at the edge. If edge filtering matters, add rate limiting or an
apikey matcher to `templates/caddy-site.tpl`.

## Security notes

- Tenant `.env` files hold all secrets (mode 600, gitignored). The printed
  service_role key bypasses RLS — never ship it in client-side code.
- All tenants share the `mt-proxy` Docker network so Caddy can reach them.
  Cross-tenant requests on that network are possible at the network level but
  useless without that tenant's credentials; tenant databases are **not** on
  the shared network at all.
- Client Studio access (`--studio` / `studio NAME on`) is full admin of that
  tenant's backend — arbitrary SQL, visible service key. Hand out the
  generated basic-auth credentials deliberately.
- Email auto-confirm is ON by default so signup works before SMTP exists.
  Configure SMTP in the tenant `.env` and set `ENABLE_EMAIL_AUTOCONFIRM=false`
  for production auth flows.

## Validated

**2026-06-09, production VPS (Hetzner, Ubuntu 26.04, 2 vCPU / 4 GB):** full
deploy via `bootstrap-vps.sh` passed end to end — hardening, Docker install,
platform init, tenant creation, **real Let's Encrypt certificates** (API +
Studio domains), signup, REST round-trip, and Studio basic auth. Demo tenant
with Studio idles at ≈ 410 MiB; host at ≈ 1 GiB used with ~2.7 GiB free.

Field notes from that deploy:

- **Run bootstrap detached** (`tmux`, `screen`, or `setsid ... > log`) — it
  restarts sshd partway through, and a dropped SSH channel mid-run could
  leave the server half-configured.
- **Verify key-only login works from a second session** before letting
  `--ssh-pubkey` disable password auth. Use a passphraseless deploy key (or
  an agent-loaded one) for non-interactive automation.
- **Cloudflare free plan does not proxy wildcard records** — set the
  `*.<base-domain>` record to _DNS only_ (grey cloud) or you get NXDOMAIN.
  Also make sure you're editing the Cloudflare zone your registrar actually
  delegates to.
- Some providers (e.g. Hetzner) force a root password change on first
  login — clear that before running automation against the box.

**2026-06-09, sandbox with real Docker daemon:** two tenants (`acme` with
Studio, `beta` minimal) booted with `tenantctl` and passed end-to-end checks
through Caddy with TLS:

- Auth health, user signup (returned a session JWT), REST OpenAPI, and a
  full insert/select round-trip with the generated keys
- **Cross-tenant isolation:** each tenant's service_role key is rejected by
  the other tenant's API (`PGRST301`); auth users and tables fully separate
- Studio behind basic auth: 401 without credentials, full UI with them
- `list`, `suspend`, `resume`, `backup`, `studio on/off` lifecycle
- Idle RAM, measured: minimal tenant ≈ **107 MiB** (db 83 + auth 10 + rest 15);
  with Studio + meta ≈ 380 MiB; Caddy 15 MiB

Gotchas found during validation:

- **PostgREST schema cache** — after creating tables via psql/migrations, run
  `NOTIFY pgrst, 'reload schema';` (or restart the rest container) or new
  tables 404. DDL run through Studio's SQL editor still needs this too.
- **Registry rate limits** — anonymous Docker Hub pulls can be throttled on
  shared/datacenter IPs. Supabase images are mirrored at
  `public.ecr.aws/supabase/<image>` and Hub library images at
  `mirror.gcr.io/library/<image>`; pull from there and `docker tag` to the
  compose names, or `docker login` with a free account.

## Known gaps (v1)

- Load behavior under real client traffic not yet measured (idle numbers
  validated in sandbox + production).
- Off-site backup shipping not included — bootstrap installs the nightly cron, but copying
  `backups/` to another machine/bucket is still manual.
- No edge functions runtime, no Supavisor pooling, no log aggregation.
- Suspended tenants keep their Caddy route and return 502 instead of a
  friendly "suspended" page.
