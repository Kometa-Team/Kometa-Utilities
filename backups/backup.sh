#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

REPO_ROOT="${REPO_ROOT:-/home/deploy/Kometa-Utilities}"
RCLONE_REMOTE="${RCLONE_REMOTE:-kometa-backups-crypt:}"
RCLONE_PATH="${RCLONE_PATH:-daily}"
RETENTION_DAYS="${RETENTION_DAYS:-30}"
WORK_ROOT="${BACKUP_WORK_ROOT:-/tmp}"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
BACKUP_NAME="kometa-utilities-${TIMESTAMP}.tar.zst"
LOCK_FILE="${WORK_ROOT}/kometa-utilities-backup.lock"

exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
    echo "Another backup is already running" >&2
    exit 1
fi

for command in docker rclone tar zstd sha256sum; do
    command -v "${command}" >/dev/null || {
        echo "Required command not found: ${command}" >&2
        exit 1
    }
done

WORK_DIR="$(mktemp -d "${WORK_ROOT}/kometa-backup.XXXXXX")"
STAGE_DIR="${WORK_DIR}/stage"
UPLOAD_DIR="${WORK_DIR}/upload"
CH_BACKUP_NAME="kometa-${TIMESTAMP}.zip"
mkdir -p "${STAGE_DIR}" "${UPLOAD_DIR}"

cleanup() {
    docker exec plausible_events_db rm -f "/var/lib/clickhouse/backups/${CH_BACKUP_NAME}" \
        >/dev/null 2>&1 || true
    rm -rf "${WORK_DIR}"
}
trap cleanup EXIT

echo "Creating PostgreSQL dumps..."
docker exec fider-db sh -c \
    'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom --no-owner --no-acl' \
    >"${STAGE_DIR}/fider.pgdump"
docker exec plausible_db pg_dump -U postgres -d plausible_db \
    --format=custom --no-owner --no-acl >"${STAGE_DIR}/plausible.pgdump"
docker exec kometa-utilities-database-1 sh -c \
    'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom --no-owner --no-acl' \
    >"${STAGE_DIR}/weblate.pgdump"

sqlite_backup() {
    local container="$1"
    local source_path="$2"
    local output_name="$3"
    local container_backup="/tmp/kometa-${output_name}"

    docker exec "${container}" python -c \
        'import sqlite3, sys; source=sqlite3.connect(sys.argv[1]); target=sqlite3.connect(sys.argv[2]); source.backup(target); target.close(); source.close()' \
        "${source_path}" "${container_backup}"
    docker exec "${container}" cat "${container_backup}" >"${STAGE_DIR}/${output_name}"
    docker exec "${container}" rm -f "${container_backup}"
}

echo "Creating SQLite backups..."
sqlite_backup imdb-service /app/data/cache.db imdb-cache.db
sqlite_backup anidb-mirror /app/database/anidb.db anidb.db
sqlite_backup simkl-service /app/data/simkl.db simkl.db

echo "Creating ClickHouse backup..."
docker exec plausible_events_db clickhouse-client --query \
    "BACKUP DATABASE plausible_events_db TO File('/var/lib/clickhouse/backups/${CH_BACKUP_NAME}')" \
    >/dev/null
docker exec plausible_events_db cat "/var/lib/clickhouse/backups/${CH_BACKUP_NAME}" \
    >"${STAGE_DIR}/plausible-clickhouse.zip"
docker exec plausible_events_db rm -f "/var/lib/clickhouse/backups/${CH_BACKUP_NAME}"

echo "Archiving persistent service files..."
docker exec anidb-mirror tar -C /app/data -cf - . \
    | zstd -q -T0 -3 -o "${STAGE_DIR}/anidb-xml.tar.zst"
docker exec simkl-service tar --exclude=simkl.db -C /app/data -cf - . \
    | zstd -q -T0 -3 -o "${STAGE_DIR}/simkl-data.tar.zst"
docker exec kometa-utilities-weblate-1 tar -C /app -cf - data \
    | zstd -q -T0 -3 -o "${STAGE_DIR}/weblate-data.tar.zst"
docker exec plausible tar -C /var/lib/plausible -cf - . \
    | zstd -q -T0 -3 -o "${STAGE_DIR}/plausible-data.tar.zst"
docker exec caddy tar -C / -cf - data config \
    | zstd -q -T0 -3 -o "${STAGE_DIR}/caddy-data.tar.zst"
docker run --rm --volumes-from transfer --entrypoint tar caddy:latest -C /data -cf - . \
    | zstd -q -T0 -3 -o "${STAGE_DIR}/transfer-data.tar.zst"

echo "Archiving deployment configuration..."
config_files=(
    docker-compose.yml
    docker-compose.override.yml
    Caddyfile
    clickhouse-logs.xml
    .env
)
for env_file in \
    anidb-service/.env \
    plex-oauth/.env \
    trakt-oauth/.env \
    mal-oauth/.env \
    simkl-oauth/.env \
    features-fider/.env; do
    if [[ -f "${REPO_ROOT}/${env_file}" ]]; then
        config_files+=("${env_file}")
    fi
done
tar -C "${REPO_ROOT}" -cf - "${config_files[@]}" \
    | zstd -q -T0 -3 -o "${STAGE_DIR}/configuration.tar.zst"

component_names=()
for component_path in "${STAGE_DIR}"/*; do
    component_names+=("./$(basename "${component_path}")")
done
(cd "${STAGE_DIR}" && sha256sum "${component_names[@]}") \
    >"${STAGE_DIR}/CHECKSUMS.sha256"
cat >"${STAGE_DIR}/MANIFEST.txt" <<EOF
timestamp=${TIMESTAMP}
hostname=$(hostname)
git_commit=$(git -C "${REPO_ROOT}" rev-parse HEAD 2>/dev/null || echo unknown)
scope=critical-state-with-anidb-cache
excluded=imdb.db,reconstructible caches,weblate cache,valkey cache
EOF

echo "Creating final archive..."
tar -C "${STAGE_DIR}" -cf - . \
    | zstd -q -T0 -6 -o "${UPLOAD_DIR}/${BACKUP_NAME}"
zstd -q -t "${UPLOAD_DIR}/${BACKUP_NAME}"
tar --zstd -tf "${UPLOAD_DIR}/${BACKUP_NAME}" >/dev/null

echo "Uploading encrypted backup to ${RCLONE_REMOTE}${RCLONE_PATH}/${BACKUP_NAME}..."
rclone copyto \
    "${UPLOAD_DIR}/${BACKUP_NAME}" \
    "${RCLONE_REMOTE}${RCLONE_PATH}/${BACKUP_NAME}" \
    --retries 3 --low-level-retries 10
rclone cryptcheck \
    "${UPLOAD_DIR}" \
    "${RCLONE_REMOTE}${RCLONE_PATH}" \
    --include "${BACKUP_NAME}" --one-way

echo "Applying ${RETENTION_DAYS}-day retention..."
rclone delete "${RCLONE_REMOTE}${RCLONE_PATH}" \
    --min-age "${RETENTION_DAYS}d" --include 'kometa-utilities-*.tar.zst'

archive_size="$(stat -c %s "${UPLOAD_DIR}/${BACKUP_NAME}")"
echo "Backup completed: ${BACKUP_NAME} (${archive_size} bytes)"
