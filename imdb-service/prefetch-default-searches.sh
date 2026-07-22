#!/usr/bin/env bash
set -uo pipefail

BASE_URL="${IMDB_SERVICE_URL:-https://utilities.kometa.wiki/imdb-service}"
DELAY_SECONDS="${PREFETCH_DELAY_SECONDS:-2}"
LOCK_FILE="${TMPDIR:-/tmp}/kometa-imdb-prefetch.lock"
failures=0

log() {
    printf '%s\n' "$*"
    logger -t kometa-imdb-prefetch -- "$*" 2>/dev/null || true
}

for command in curl flock logger; do
    if ! command -v "${command}" >/dev/null; then
        log "Required command not found: ${command}"
        exit 1
    fi
done

exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
    log "Another IMDb defaults prefetch is already running; skipping"
    exit 0
fi

prefetch() {
    local collection="$1"
    local constraint="$2"
    local error

    log "Prefetching ${collection}"
    if error="$(curl -fsS -G "${BASE_URL}/search" \
        --connect-timeout 15 \
        --max-time 900 \
        --retry 2 \
        --retry-delay 5 \
        --retry-all-errors \
        --data-urlencode "${constraint}" \
        --data-urlencode "limit=1" \
        -o /dev/null 2>&1)"; then
        log "Prefetched ${collection}"
    else
        log "Failed to prefetch ${collection}: ${error}"
        failures=$((failures + 1))
    fi
    sleep "${DELAY_SECONDS}"
}

if ! curl -fsS --connect-timeout 10 --max-time 15 "${BASE_URL}/health/ready" >/dev/null; then
    log "IMDb service is not ready at ${BASE_URL}"
    exit 1
fi

# defaults/both/based.yml
prefetch "Based on Books" "keyword.any=based on book,based on novel"
prefetch "Based on Comics" "keyword.any=based on comic,based on comic book"
prefetch "Based on a True Story" "keyword.any=based on true story"
prefetch "Based on Video Games" "keyword.any=based on video game"

# defaults/movie/seasonal.yml - keyword searches
prefetch "Disabilities" "keyword.any=disability"
prefetch "Mother's Day" "keyword.any=motherhood,pregnancy"
prefetch "Father's Day" "keyword.any=fatherhood"
prefetch "Thanksgiving" "keyword.any=thanksgiving"
prefetch "Women's History Month" "keyword.any=women in film,women's rights,women's suffrage,womens rights,womens suffrage"

# defaults/movie/seasonal.yml - IMDb list searches
prefetch "Years" "list.any=ls066838460"
prefetch "Black History Month" "list.any=ls023525790"
prefetch "Valentine's Day" "list.any=ls032692441,ls000094398,ls057783436,ls064427905"
prefetch "St. Patrick's Day" "list.any=ls067580975"
prefetch "Easter" "list.any=ls062665509,ls051733651"
prefetch "Memorial Day" "list.any=ls526590990"
prefetch "Independence Day" "list.any=ls561449172,ls541213215,ls068664510,ls080925875"
prefetch "Labor Day" "list.any=ls002014923"
prefetch "LGBTQ+" "list.any=ls080580859"
prefetch "Halloween" "list.any=ls546214737,ls023118929,ls000099714"
prefetch "Veterans Day" "list.any=ls526590990,ls565595526"

if ((failures > 0)); then
    log "IMDb defaults prefetch completed with ${failures} failure(s)"
    exit 1
fi

log "IMDb defaults prefetch completed successfully"
