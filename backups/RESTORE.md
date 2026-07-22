# Backup Restore Runbook

Backups are encrypted by the configured `rclone crypt` remote. Preserve the
crypt password and salt outside this server; the remote data cannot be restored
without them.

## Download And Verify

List available backups:

```bash
rclone lsl kometa-backups-crypt:daily
```

Download and verify one archive:

```bash
./backups/download-and-verify.sh \
  kometa-utilities-YYYYMMDDTHHMMSSZ.tar.zst \
  /tmp/kometa-restore
tar --zstd -xf /tmp/kometa-restore/kometa-utilities-*.tar.zst \
  -C /tmp/kometa-restore/extracted
```

Verify component hashes before restoration:

```bash
cd /tmp/kometa-restore/extracted
sha256sum -c CHECKSUMS.sha256
```

## PostgreSQL

Restore each custom-format dump with `pg_restore`. Stop the dependent
application first and restore into an empty database. Example for Fider:

```bash
docker compose stop features-fider
docker exec fider-db sh -c 'dropdb -U "$POSTGRES_USER" --if-exists "$POSTGRES_DB"'
docker exec fider-db sh -c 'createdb -U "$POSTGRES_USER" "$POSTGRES_DB"'
docker cp fider.pgdump fider-db:/tmp/fider.pgdump
docker exec fider-db sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  --clean --if-exists --no-owner --no-acl /tmp/fider.pgdump'
docker compose start features-fider
```

Use the corresponding database container and credentials for Plausible and
Weblate.

## SQLite

Stop the service, copy the restored database into its volume, then start it:

```bash
docker compose stop anidb-mirror
docker cp anidb.db anidb-mirror:/app/database/anidb.db
docker compose start anidb-mirror
```

Paths:

- IMDb cache: `/app/data/cache.db`
- AniDB: `/app/database/anidb.db`
- SIMKL: `/app/data/simkl.db`

The large IMDb `imdb.db` is intentionally excluded because it is rebuilt from
IMDb datasets.

## ClickHouse

Copy `plausible-clickhouse.zip` into
`/var/lib/clickhouse/backups/`, then restore with:

```bash
docker cp plausible-clickhouse.zip \
  plausible_events_db:/var/lib/clickhouse/backups/plausible-clickhouse.zip
docker exec plausible_events_db clickhouse-client --query \
  "RESTORE DATABASE plausible_events_db FROM File('/var/lib/clickhouse/backups/plausible-clickhouse.zip')"
```

Restore into a clean database or use explicit `structure_only`/`data_only`
options when performing a partial recovery.

## Service Files

The component `.tar.zst` files preserve paths relative to their documented
container locations. Stop the owning service before replacing files. Restore
configuration only after reviewing it against the current deployment version.

After any restore, run:

```bash
docker compose up -d
docker compose ps
```

Confirm all configured health checks become healthy.
