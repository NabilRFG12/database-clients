# Multi-Tenant Self-Hosted Supabase — Decisions & Research Log

> Status: **Pre-PRD working document** — records what we learned and decided before writing the final PRD.
> Date: 2026-06-09

## 1. The Problem

We build apps/software for client companies (currently 11). Each client today gets:

- A dedicated VPS
- A full self-hosted Supabase deployment (used as the backend only — clients never get Studio or direct database access)

This means 11 VPSes to pay for, patch, back up, and upgrade, while most of them sit idle. The goal: **one powerful VPS running all clients with real isolation between them**, plus fast onboarding when client #12 arrives.

## 2. Constraints & Non-Negotiables

| #   | Requirement                                                                                                                                                                             |
| --- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | Hard isolation between clients: separate database, separate auth users, separate JWT secret + anon/service keys per client. A leaked key for client A must be useless against client B. |
| 2   | Hard per-client resource caps (RAM/CPU) — "your plan includes 4 GB" must map to a real limit.                                                                                           |
| 3   | Only we access Studio/admin tooling; clients only ever hit their API endpoints.                                                                                                         |
| 4   | New client onboarding in minutes via a form/script, with credentials generated automatically.                                                                                           |
| 5   | No forking of Supabase data-plane services (GoTrue, PostgREST, Storage) — upstream security patches must remain a simple image-tag bump.                                                |

## 3. Decisions Made

### D1 — Rejected: single shared database with schema-per-client + RLS

One GoTrue/`auth` schema would mix all clients' users; one JWT secret and one service key would span every client. Weak isolation for separate paying companies. **Rejected.**

### D2 — Rejected: forking Supabase services to be internally multi-tenant

The code is writable, but we would permanently diverge from upstream and own every future auth CVE merge ourselves. Supabase keeps GoTrue/PostgREST single-tenant by design; Realtime and Supavisor are the only natively multi-tenant components. **Rejected** — multi-tenancy lives in the orchestration layer, not inside the services.

### D3 — Accepted: one VPS, one trimmed stack per client, Postgres-per-client

- **One VPS** (32 GB is comfortable for 11 light clients; 16 GB viable).
- **Per client:** own Postgres container + GoTrue + PostgREST, plus Realtime/Storage _only if that client's app uses them_. Own JWT secret, own anon/service keys, own database password.
- **Postgres per client (not one shared cluster):** Postgres has no per-database RAM/CPU limits, so a shared cluster only allows soft controls (`statement_timeout`, per-role `work_mem`, connection caps). Per-client Postgres containers give **hard Docker caps** (`memory: 4g`, `cpus: "2.0"`), at the cost of ~300–500 MB baseline overhead each (≈ +5 GB for 11 clients — acceptable).
- **Dropped per client:** Kong (replaced by one shared Caddy), Studio (one shared instance for us), Logflare/analytics, Vector, imgproxy. These are most of the famous "Supabase needs 2 GB" footprint.
- **Shared layer:** one Caddy/Traefik doing host-based routing (`clienta.api.ourdomain.com`) with automatic TLS via a single wildcard DNS record; one Studio + postgres-meta for us, behind auth/VPN; optionally one multi-tenant Supavisor for pooling.

**Idle RAM budget (measured estimates):** minimal tenant (Postgres + Auth + REST) ≈ 350–500 MB; + Storage ≈ 500–650 MB; + Realtime ≈ 700–900 MB. Eleven light clients ≈ **5–7 GB idle**, plus 1–2 GB shared layer. Docker limits are ceilings, not reservations — caps can safely oversubscribe physical RAM.

### D4 — Accepted: build a thin control plane ("tenantctl" + admin panel)

This is the layer Supabase Cloud has and never open-sourced. Two pieces:

1. **`tenantctl`** (the engine, CLI): `create | suspend | resume | resize | backup | upgrade | delete`. Creating a tenant = generate secrets → render docker-compose from a template → `docker compose up` → write Caddy route → register tenant in a small registry → print the app `.env` block. Works over SSH even if the panel is down.
2. **Admin panel** (thin UI over `tenantctl`): form with client name, subdomain (auto-suggested), RAM/CPU cap, service toggles → click Create → credentials on screen in ~30–60 s. Lists tenants with live RAM/CPU, suspend/resize/backup/delete buttons. **Never exposed publicly** — password + VPN/IP allowlist.

### D5 — Accepted: Studio access model

- Phase 1: one small Studio container per tenant + a launcher page at `studio.ourdomain.com` listing all clients (dropdown-like UX, ~95% of supabase.com feel). Studio containers can be started on demand to save RAM.
- Phase 2 (optional, only if the launcher annoys us): use this Studio fork to add a true in-app project switcher. Real work; deferred.
- **Client-facing Studio (opt-in per tenant):** some clients (~3–5) may be granted Studio access to their own backend. Supported naturally by the per-tenant design — each Studio container is wired to one tenant's stack only, so isolation holds. `tenantctl` exposes it via a flag (`--studio-access on`) that publishes `studio.<client>.api.ourdomain.com` behind per-client credentials (Caddy basic auth to start; upgrade path: shared Authelia/Authentik forward-auth for real logins/2FA/revocation). **Caveat:** self-hosted Studio has no roles — access means full admin power over that tenant (arbitrary SQL, service key visible, can drop tables). Mitigations: contract terms + nightly backups; optionally point a "viewer" client's Studio at a restricted Postgres role (read-only / no DDL) accepting some broken Studio features. RAM cost ≈ 150–300 MB per always-on client Studio.

### D6 — Accepted: operational model

- **Backups:** per-tenant `pg_dump` on cron, shipped off-site. One pipeline instead of 11.
- **Upgrades:** image tags templated in one place; `tenantctl upgrade` rolls tenants one at a time.
- **Suspend:** non-paying/dormant clients drop to zero RAM (containers stopped, volumes kept).
- **Blast radius:** the single VPS is a shared point of failure — mitigated by off-site backups and the option to move a critical client to its own box later (the per-tenant compose file makes a tenant portable by design).

## 4. Prior Art — Internet Research (2026-06-09)

### Direct equivalents (open source) — people have built this

| Project                                                   | What it is                                                                                                                                                                 | Maturity                                 | Notes vs. our needs                                                                                                                                                      |
| --------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| [SupaConsole](https://github.com/sharonpraju/SupaConsole) | Self-hosted dashboard to create/manage multiple Supabase projects via Docker Compose; auto port allocation; team management; Next.js 15 + Prisma, MIT                      | ~90 stars, very few commits, early stage | Clones the official repo and deploys the **full default stack per project** (~2 GB each, incl. Kong/Studio/analytics) — no trimmed stacks, no resource caps, no suspend. |
| [SupaPanel](https://github.com/alanfrigo/SupaPanel)       | Fork of SupaConsole; adds Traefik with automatic HTTPS and custom domain per project; install script; MIT                                                                  | ~9 stars, v1.1.1 (Jan 2026), early stage | Closest to our routing design (reverse proxy + auto TLS). Same full-stack-per-project weight; no caps/suspend/service-toggles.                                           |
| [Multibase](https://github.com/osobh/multibase)           | CLI **and** web dashboard; lifecycle (create/start/stop/restart/delete); health checks, per-instance metrics, alerts, log streaming; Node/Express + dockerode + React, MIT | ~70 stars, no releases yet, active       | Best feature overlap (CLI+UI, metrics, lifecycle ≈ suspend/resume). Still full-stack-per-instance; no RAM/CPU caps per tenant; no trimmed service selection.             |

### Adjacent options

- **[Coolify](https://coolify.io/docs/services/supabase)** — open-source PaaS with one-click Supabase and per-service memory/CPU limits in the dashboard. Generic (not Supabase-aware): no key generation UX, and [known issues running multiple Supabase instances on one server](https://github.com/coollabsio/coolify/issues/5362) (port/log mix-ups; [routing friction](https://github.com/coollabsio/coolify/issues/7458)). Good fallback if we want zero custom code and accept heavier stacks.
- **[supabase-community/supabase-kubernetes](https://github.com/supabase-community/supabase-kubernetes)** — community Helm chart, actively updated through 2026; multi-tenancy = one Helm release per client + ingress routing; pairs with Postgres operators like [StackGres](https://stackgres.io/blog/running-supabase-on-top-of-stackgres/). Architecturally the "grown-up" version of our design, but Kubernetes ops overhead is overkill for one VPS and 11 clients today. Documented as the migration path if we outgrow a single box.
- **Official position:** Supabase confirms self-hosted = one project, no native multi-project support or timeline ([discussion #4907](https://github.com/orgs/supabase/discussions/4907), [discussion #38048](https://github.com/orgs/supabase/discussions/38048)). Community consensus matches D3: separate stacks + reverse proxy. Realtime is the exception — [natively multi-tenant](https://github.com/orgs/supabase/discussions/28668).

### Gap analysis — what none of the existing tools cover

1. **Trimmed per-tenant stacks** (drop Kong/Studio/analytics; toggle Realtime/Storage per client) — all three panels deploy the full ~2 GB stack per project, which would put 11 clients at ~22 GB idle vs. our ~5–7 GB.
2. **Hard per-tenant RAM/CPU caps** as a first-class setting (only Coolify has this, and it isn't Supabase-aware).
3. **Suspend/resume tied to client lifecycle** (Multibase has stop/start, closest).
4. **Agency workflow**: generated `.env` block per client, client-facing API subdomain + owner-only Studio split.

### Verdict

The pattern is **validated** — multiple independent projects converged on "compose-template + reverse proxy + management UI", which is exactly D3/D4. None is mature enough (9–90 stars, pre-release) to bet 11 paying clients on as-is, and none covers gaps 1–2, which are our core requirements. **Decision: build `tenantctl` + panel ourselves (small, ~thin scripts), borrowing proven ideas** — Traefik/Caddy auto-TLS routing from SupaPanel, lifecycle/metrics UX from Multibase. Re-evaluate Multibase periodically; contribute back if alignment grows.

## 5. Open Questions for the PRD

1. VPS sizing & provider; 16 GB vs 32 GB to start.
2. Panel auth: simple password + IP allowlist vs. VPN-only.
3. Backup target (S3-compatible bucket? second cheap VPS?) and retention policy.
4. Migration plan: order and method for moving 11 existing single-VPS clients onto the new box (likely `pg_dump`/restore + storage file copy + DNS cutover, one client at a time).
5. Do any current clients use Realtime/Storage/Edge Functions? (Determines per-tenant templates; Edge Functions runtime was excluded so far.)
6. Monitoring stack: panel-native `docker stats` vs. Grafana + cAdvisor.
7. Naming/branding of subdomains (`<client>.api.ourdomain.com` pattern confirmed?).
8. Client-facing Studio: which clients get it, basic auth vs. Authelia from day one, and full-admin vs. restricted-role access per client.

## 6. Next Steps

1. Answer §5 questions → write the PRD.
2. ~~Scaffold `tenantctl` (compose template + secret generation + Caddy route generation) — the engine.~~ **Done — see [`multi-tenant/`](multi-tenant/README.md).**
3. ~~Stand up a test environment, create dummy tenants, measure real idle/load RAM against §3 estimates.~~ **Done in a Docker sandbox — 2 tenants validated end-to-end (auth, REST, isolation, Studio, lifecycle); minimal tenant idles at ~107 MiB, beating the §3 estimate. See `multi-tenant/README.md` § Validated.** **Production deploy confirmed 2026-06-09 on a Hetzner 4 GB VPS (`bootstrap-vps.sh`): real Let's Encrypt issuance, signup, REST, Studio auth all passed — demo tenant ≈ 410 MiB idle with Studio.** Remaining: load behavior under real client traffic.
4. Build the admin panel on top of `tenantctl`.
5. Migrate one low-risk client end-to-end, then roll the rest.
