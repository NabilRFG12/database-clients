# Migration Runbook — old self-hosted Supabase → tenant

Moves one client from a dedicated single-project Supabase VPS into a tenant
on the multi-tenant server, **without breaking deployed apps or logging out
users**. The trick: the tenant inherits the old project's identity
(JWT secret + API keys) and, optionally, its old domain.

Run one client at a time. Budget ~30–60 minutes per client, most of it
waiting on copies. Commands below assume:

- `OLD` = the client's current VPS (single-project docker-compose Supabase)
- `NEW` = the multi-tenant server, repo at `~/database-clients`
- client tenant name: `clientx`

## 0. Pre-flight (on OLD)

```bash
# Postgres major version — must match the tenant image (15.x). If OLD is
# on 14 or older, STOP and plan a pg_upgrade path first.
docker exec supabase-db psql -U postgres -tAc "show server_version;"

# Which optional services does the app actually use?
#  - realtime: does the app subscribe to changes?  - storage: any buckets?
docker exec supabase-db psql -U postgres -tAc "select count(*) from storage.buckets;"

# Collect the identity (from OLD's docker/.env)
grep -E '^(JWT_SECRET|ANON_KEY|SERVICE_ROLE_KEY)=' ~/supabase/docker/.env
```

Also note the public URL the deployed apps call (e.g. `https://api.clientx.com`)
and its DNS TTL — lower the TTL to 300s now so the final cutover is fast.

## 1. Create the tenant with the OLD identity (on NEW)

```bash
cd ~/database-clients/multi-tenant
./tenantctl create clientx --ram 1g --services storage \
  --jwt-secret '<JWT_SECRET from OLD>' \
  --anon-key '<ANON_KEY from OLD>' \
  --service-key '<SERVICE_ROLE_KEY from OLD>'
```

Because the secret and keys are identical, every key already shipped inside
the client's apps remains valid here.

## 2. Copy the database

On OLD — take the dump during a quiet window. For a strict cutover with zero
lost writes, stop the API first (`docker stop supabase-rest supabase-auth`),
which takes the app offline until DNS cutover completes:

```bash
# IMPORTANT: dump AND restore as supabase_admin (the real superuser), NOT
# postgres — the auth/storage schemas are owned by other roles, and restoring
# as postgres produces hundreds of ownership errors (validated in rehearsal:
# postgres -> ~600 errors, supabase_admin -> 4 benign ones).
docker exec supabase-db pg_dump --clean --if-exists -U supabase_admin postgres \
  | gzip > /root/clientx-migration.sql.gz
scp /root/clientx-migration.sql.gz root@NEW:/root/
```

On NEW — restore into the tenant (services stopped so nothing writes during
restore, db kept up):

```bash
cd ~/database-clients/multi-tenant
docker stop clientx-auth clientx-rest clientx-storage 2>/dev/null
gunzip -c /root/clientx-migration.sql.gz | docker exec -i clientx-db psql -U supabase_admin -d postgres
```

Expected: **a few dozen benign errors** — the count varies with which
services the tenant runs. From a real migration (~30 errors, all benign):

- `role "supabase_realtime_admin" does not exist` (×15) — the tenant was
  created without the realtime service, so that role/schema isn't
  bootstrapped. Harmless when the client doesn't use realtime.
- `schema/type/relation ... already exists` and `cannot drop ... because
other objects depend on it` (auth/storage objects, `users_pkey`, ...) —
  from restoring over the already-bootstrapped auth/storage schemas.
- `graphql_public.graphql ... does not exist` (×4) — drop ordering; fixed
  in the next step.

**The error count is NOT the verification.** Prove the restore by comparing
row counts OLD vs NEW — they must be identical for every table:

```bash
# Run on BOTH old and new db containers and diff the output:
docker exec clientx-db psql -U supabase_admin -d postgres -tAc \
  "select schemaname||'.'||relname||' '||n_live_tup
   from pg_stat_user_tables order by 1;"
```

This restore brings the app schema **plus `auth` users, refresh tokens, and
`storage` metadata** — that's why sessions survive.

### 2b. Post-restore fixup (always run this)

The dump/restore cycle recreates the pg_graphql extension, which loses the
GraphQL wrapper function and its grants. This block is idempotent — run it
even if the client doesn't use GraphQL:

```bash
docker exec clientx-db psql -U supabase_admin -d postgres <<'SQL'
create or replace function graphql_public.graphql(
    "operationName" text default null, query text default null,
    variables jsonb default null, extensions jsonb default null)
returns jsonb language sql
as $$
  select graphql.resolve(
      query := query, variables := coalesce(variables, '{}'),
      "operationName" := "operationName", extensions := extensions);
$$;
grant usage on schema graphql_public to anon, authenticated, service_role;
grant execute on function graphql_public.graphql to anon, authenticated, service_role;
grant usage on schema graphql to postgres, anon, authenticated, service_role;
grant execute on all functions in schema graphql to postgres, anon, authenticated, service_role;
notify pgrst, 'reload schema';
SQL
```

## 3. Copy storage files (skip if no buckets)

The file backend's on-disk path is
`<GLOBAL_S3_BUCKET>/<TENANT_ID>/<bucket>/<object-name>/<version>` (confirmed
empirically by uploading a probe object). Both segments are env-driven:

- OLD single-project self-host: `stub/stub/...` (both default to `stub`)
- Our tenants: `stub/<tenant-name>/...` (`TENANT_ID` is the tenant name)

So the fix is renaming the **second**-level directory — the top-level `stub`
stays:

```bash
# On OLD: the files live in docker/volumes/storage
rsync -a ~/supabase/docker/volumes/storage/ root@NEW:~/database-clients/multi-tenant/tenants/clientx/storage-data/

# On NEW: re-namespace from the old tenant id to ours
cd ~/database-clients/multi-tenant/tenants/clientx/storage-data
mv stub/stub stub/clientx
```

### 3b. Rebuild xattrs (always run this if files were copied)

Newer storage-api versions (the tenant runs v1.60.x) read each object's
metadata from **POSIX extended attributes** on the file. Files written by an
older storage-api (e.g. v1.28.x) never had them, and `tar`/`rsync` don't copy
xattrs by default — so every download returns **HTTP 500 with
`{"code":"ENODATA"}`** ("The extended attribute does not exist").

Rebuild them from the already-restored `storage.objects` rows (non-destructive
and idempotent — DB rows are untouched, so owner/created_at/version survive):

```bash
./tenantctl fix-storage-xattrs clientx
# -> xattrs: N fixed, M already correct, 0 missing on disk
```

Any "MISSING on disk" lines mean files that exist in `storage.objects` but
weren't copied — re-check the rsync before going live. Then spot-check a real
download:

```bash
curl -sI "https://clientx.db.backend.stream/storage/v1/object/authenticated/<bucket>/<name>" \
  -H "Authorization: Bearer $OLDSERVICE"   # expect 200 + correct Content-Type
```

## 4. Start and smoke-test with the OLD keys

```bash
./tenantctl resume clientx
# GoTrue may run a few of its own migrations on first boot if OLD was on an
# older auth version — watch: docker logs clientx-auth | tail

OLDANON='<ANON_KEY from OLD>'
curl -s https://clientx.db.backend.stream/auth/v1/health
curl -s "https://clientx.db.backend.stream/rest/v1/<some-table>?select=*&limit=1" \
  -H "apikey: $OLDANON" -H "Authorization: Bearer $OLDANON"
# Log in as a real (test) user of the app if possible — the strongest signal.
```

The old key working against the new server is the proof the identity import
succeeded.

## 5. Cutover — keep the old domain (zero app changes)

```bash
./tenantctl add-domain clientx api.clientx.com
# Edit tenants/clientx/.env:  API_EXTERNAL_URL=https://api.clientx.com
#                             SITE_URL=<the client app's URL>
./tenantctl resume clientx
```

Then flip DNS: `api.clientx.com  A  -> <NEW server IP>`.

**After DNS resolves to NEW, nudge Caddy:**

```bash
./tenantctl rerender clientx   # graceful Caddy reload -> immediate cert issuance
```

This is needed because `add-domain` triggers eager certificate issuance while
DNS still points at OLD — the ACME HTTP-01 challenge hits the old server,
404s, and Caddy enters a retry **backoff** (it would eventually recover, but
"eventually" can be a long gap). A graceful reload re-triggers issuance
immediately; verified in the field — the cert issued right after the reload.

The deployed apps notice nothing: same URL, same keys, new server.

(Alternative if you control the app build: skip add-domain, point the app's
`SUPABASE_URL` at `clientx.db.backend.stream`, redeploy the app.)

## 6. Verify, then rollback window

- Watch `docker stats` and the panel for the first hours.
- **Keep OLD running but stopped-API (or firewalled) for at least a week** —
  rollback is just flipping DNS back. Do not destroy the old VPS until the
  client has gone a full billing cycle without issues.
- The nightly `backup-all` cron covers the new tenant automatically.

## Gotchas

- **PostgREST schema cache**: after the restore, run
  `docker exec clientx-db psql -U supabase_admin -c "NOTIFY pgrst, 'reload schema';"`
  if REST returns 404 for tables that exist.
- **Custom Postgres roles**: `pg_dump` of a database does not include
  cluster-level roles. If the app created extra roles (rare), recreate them:
  `pg_dumpall --roles-only` on OLD, filter, apply on NEW.
- **Edge functions**: the trimmed tenant stack has no edge runtime. If a
  client uses edge functions, port them (usually to the app's own backend)
  before migrating that client.
- **SMTP**: copy the `SMTP_*` values from OLD's `.env` into the tenant's
  `.env` and set `ENABLE_EMAIL_AUTOCONFIRM=false`, or password resets will
  silently not send.
- **Realtime**: subscriptions reconnect on their own after cutover; nothing
  to migrate (realtime state is ephemeral).
