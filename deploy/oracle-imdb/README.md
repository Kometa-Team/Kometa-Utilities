# imdb-service on Oracle Cloud (Always Free Ampere A1)

Moves imdb-service (app + SQLite) off the shared host onto an Oracle Cloud
Always Free Arm VM, while `https://utilities.kometa.wiki/imdb-service` keeps
working unchanged: Caddy on the shared host proxies to the VM over Tailscale.

**Why:** on the shared host the service is capped at 2 CPUs / 2 GB, the host
has almost no RAM left to cache the 22.7 GiB database, and the nightly import
needs a 23 GiB staging copy on a disk shared with 20 other containers.

**Budget (Always Free, as of June 2026):** 2 OCPU + 12 GB for `VM.Standard.A1.Flex`,
200 GB block storage (boot + block combined), home region only. 2 OCPUs is the
same CPU budget the service has today; the gain is RAM for page cache, I/O,
and disk headroom. The first step is a **trial** to confirm 2 cores keep up.

## 1. VM

1. Create a `VM.Standard.A1.Flex` instance: **2 OCPU, 12 GB**, Ubuntu 24.04
   (aarch64), in your home region. Keep the boot volume at the minimum (~47-50 GB).
   "Out of capacity" is common for A1; retry or try another availability domain.
2. Install Docker (`curl -fsSL https://get.docker.com | sh`) and add your user to
   the `docker` group.
3. **Idle reclamation:** Oracle may reclaim Always Free instances whose 95th
   percentile CPU, network *and* memory are all under 20% for 7 days. Real
   traffic keeps CPU above that, but upgrading the tenancy to Pay As You Go
   (still free within Always Free limits) removes the risk entirely.

## 2. Block volume

Create a block volume of the remaining allowance (~150 GB; on OCI, throughput
scales with size), attach it to the VM, then:

```bash
sudo mkfs.ext4 -L imdb-data /dev/oracleoci/oraclevdb   # check the device path in the console
sudo mkdir -p /mnt/imdb-data
echo 'LABEL=imdb-data /mnt/imdb-data ext4 defaults,noatime,_netdev,nofail 0 2' | sudo tee -a /etc/fstab
sudo mount -a && df -h /mnt/imdb-data
```

The service needs ~50 GB at the peak of an import (DB + staging copy + downloads).

## 3. Tailscale (no public ports)

Install Tailscale on **both** the VM and the shared host (`curl -fsSL
https://tailscale.com/install.sh | sh && sudo tailscale up`). Note the VM's
address with `tailscale ip -4` (100.x.y.z). The OCI security list needs no new
ingress rules; the container listens only on the Tailscale IP.

From the shared host, Caddy runs in a container, so confirm the Caddy container
can reach the VM once the service is up:
`docker exec caddy wget -qO- http://100.x.y.z:8000/health/live`.
(If it can't, the host's Tailscale interface isn't routable from the Docker
network; run Tailscale in a sidecar or use `network_mode: host` for that check.)

## 4. Start the service (trial)

On the VM, from a checkout of this repo:

```bash
cd deploy/oracle-imdb
cp .env.example .env    # fill in BIND_ADDR, IMDB_ADMIN_TOKEN, PARENTAL_PROXY_URLS ...
docker compose up -d
docker logs -f imdb-service
```

With an empty volume it runs a **full import** first (title_basics alone took
~4h17m on the shared host; expect faster with more RAM). `start_period` is 8h
for that reason. Alternatively, copy `imdb.db` and `cache.db` from the current
host's `imdb-data` volume into `/mnt/imdb-data` before starting (stop the old
container first so the copy is consistent).

Check it before sending traffic:

```bash
curl -s http://100.x.y.z:8000/health/ready
curl -s http://100.x.y.z:8000/stats | head -c 400
curl -s -X POST -H 'content-type: application/json' \
  -d '{"imdb_ids":["tt0111161"]}' http://100.x.y.z:8000/ratings
```

Also confirm GraphQL works from Oracle's IP (`POST /search/advanced`, `/keywords/tt0133093`).

## 5. Route traffic (on the shared host)

The `/imdb-service` upstream is in `caddy/imdb-upstream.caddy`, imported by
the Caddyfile. Switch it by copying a variant and reloading (no restart; other
sites unaffected):

```bash
cd ~/Kometa-Utilities
sed 's/ORACLE_VM_TAILNET_IP/100.x.y.z/' deploy/oracle-imdb/caddy/oracle-with-local-fallback.caddy > caddy/imdb-upstream.caddy
docker exec caddy caddy reload --config /etc/caddy/Caddyfile
```

| Variant | Use |
|---|---|
| `local.caddy` | Local container only: the pre-trial setup and the rollback target |
| `oracle-with-local-fallback.caddy` | **Trial:** Oracle first; the local container takes over whenever the VM fails its health check |
| `oracle-only.caddy` | **Cutover:** once the local container is stopped |

Commit the switch to `caddy/imdb-upstream.caddy` like any other config change.

## 6. Trial checklist (1-2 days)

- [ ] `docker stats imdb-service` on the VM: CPU under ~170% at peak; memory well under 9G
- [ ] Caddy access log (`/data/access/imdb-service.log` in the caddy container): p95 `duration` for `/title` and `POST /ratings` no worse than before
- [ ] No fallbacks to the local container (Caddy logs show upstream health failures if any)
- [ ] The VM's 03:00 UTC import completes (`/app/data/import.log`), and `title_fts_ready` is set
- [ ] Uptime Kuma: IMDb monitors stay green

## 7. Cutover

1. Switch to `oracle-only.caddy` and reload Caddy.
2. On the shared host: `docker compose stop imdb-service`, then remove it from
   `docker-compose.yml` (and its `imdb-data` volume once you're sure; it frees
   ~25 GB).
3. Move the Uptime Kuma "IMDB Service - internal" style checks to the Tailscale URL.
4. Back up `cache.db` from the VM (the dataset DB can always be rebuilt from IMDb).

## Rollback

Copy `deploy/oracle-imdb/caddy/local.caddy` to `caddy/imdb-upstream.caddy`
and `caddy reload`. During the trial the local container is still running, so
this is instant. After cutover, start it again with `docker compose up -d imdb-service`
first (its volume still holds the old database unless you deleted it).
