#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

RCLONE_REMOTE="${RCLONE_REMOTE:-kometa-backups-crypt:}"
RCLONE_PATH="${RCLONE_PATH:-daily}"

if [[ $# -ne 2 ]]; then
    echo "Usage: $0 <backup-filename> <destination-directory>" >&2
    exit 2
fi

backup_name="$1"
destination="$2"
mkdir -p "${destination}"
archive="${destination}/${backup_name}"

rclone copyto "${RCLONE_REMOTE}${RCLONE_PATH}/${backup_name}" "${archive}"
zstd -q -t "${archive}"
tar --zstd -tf "${archive}" >/dev/null

echo "Verified encrypted backup download: ${archive}"
echo "Extract with: tar --zstd -xf '${archive}' -C '${destination}'"
