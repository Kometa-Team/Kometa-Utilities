# IMDb Searches Performed by Kometa

This document inventories every IMDb search (`imdb_search`) that Kometa performs on its own behalf —
i.e. searches baked into Kometa's code or its bundled **Defaults** YAML — as opposed to searches a user
writes in their own config.

## Summary

- **Kometa's Python code performs no hardcoded IMDb searches of its own.** The `imdb_search` builder is
  only *dispatched* by the code (`modules/imdb.py`, `modules/builder.py`); the actual search definitions
  all live in Defaults YAML.
- **All built-in `imdb_search` usage lives in the `defaults/` directory**, across three files:
  1. `defaults/both/based.yml` — "Based on..." collections
  2. `defaults/movie/seasonal.yml` — Seasonal collections
  3. `defaults/templates.yml` — the shared `based` template consumed by (1)

Note: many of the seasonal entries use IMDb **list** references (`list.any: ls...`) via `imdb_search`
rather than keyword/attribute searches; those are included below for completeness since they are issued
through the `imdb_search` builder.

---

## 1. `defaults/both/based.yml` — "Based on..." collections

Uses the shared `based` template (see §3). Each collection issues an `imdb_search` with `keyword.any`.

| Collection key | `imdb_search` keywords (`keyword.any`) |
|----------------|----------------------------------------|
| `books`        | `based on book`, `based on novel`      |
| `comics`       | `based on comic`, `based on comic book`|
| `true_story`   | `based on true story`                  |
| `video_games`  | `based on video game`                  |

Effective search per collection (from the template): `type` resolved by library type (movie →
`movie, tv_movie`; show → `tv_series, tv_mini_series`), `sort_by: popularity.asc`, `limit: 200`.

---

## 2. `defaults/movie/seasonal.yml` — Seasonal collections

Each seasonal key defines an `imdb_search`. Two flavors: **keyword searches** and **IMDb list
references** (both run through `imdb_search`). All use `limit: 500`.

### Keyword-based searches

| Season key      | `imdb_search` attributes |
|-----------------|--------------------------|
| `disabilities`  | `keyword.any: disability` |
| `mother`        | `keyword.any: motherhood, pregnancy` |
| `father`        | `keyword.any: fatherhood` |
| `thanksgiving`  | `keyword.any: thanksgiving` |
| `women`         | `type: movie`, `keyword.any: women in film, women's rights, women's suffrage, womens rights, womens suffrage`, `sort_by: rating.desc` |

### IMDb list references (via `imdb_search` `list.any`)

| Season key      | IMDb list(s) (`list.any`) |
|-----------------|---------------------------|
| `years`         | `ls066838460` |
| `black_history` | `ls023525790` |
| `valentine`     | `ls032692441`, `ls000094398`, `ls057783436`, `ls064427905` |
| `patrick`       | `ls067580975` |
| `easter`        | `ls062665509`, `ls051733651` |
| `memorial`      | `ls526590990` |
| `independence`  | `ls561449172`, `ls541213215`, `ls068664510`, `ls080925875` |
| `labor`         | `ls002014923` |
| `lgbtq`         | `ls080580859` |
| `halloween`     | `ls546214737`, `ls023118929`, `ls000099714` |
| `veteran`       | `ls526590990`, `ls565595526` |

---

## 3. `defaults/templates.yml` — the `based` template

This is the reusable template that `defaults/both/based.yml` (§1) instantiates. It is the definition of
the search, not a separate search itself:

```yaml
based:
  default:
    limit: 200
    sort_by: release.desc
    sort_by_<<key>>: <<sort_by>>
  imdb_search:
    keyword.any: <<keywords>>
    limit: <<limit>>
    type: <<type>>
    sort_by: popularity.asc
  conditionals:
    type:
      default: bob
      conditions:
        - library_type: movie
          value: movie, tv_movie
        - library_type: show
          value: tv_series, tv_mini_series
  smart_label: <<sort_by_<<key>>>>
```

`seasonal.yml` also parameterizes `imdb_search` per key via template variables
(`imdb_search_<<key>>: <<imdb_search>>`), which is what allows the per-season definitions in §2.

---

## Prefetching the built-in constraint searches

The following commands warm every constraint-cache key used by Kometa's bundled Defaults. They run
sequentially to avoid sending a burst of GraphQL requests to IMDb.

`type` and `sort_by` are intentionally omitted. The IMDb service applies those as local database
operations, so they do not change the `keyword.any` or `list.any` constraint-cache keys. Likewise,
`limit=1` only limits the final response; the service still fetches and caches the complete constraint
result (up to its 10,000-title ceiling).

The commands are packaged in `prefetch-default-searches.sh`. Run them manually from the repository
root with:

```bash
./imdb-service/prefetch-default-searches.sh
```

Set `IMDB_SERVICE_URL` to target another deployment or `PREFETCH_DELAY_SECONDS` to change the default
two-second pause between searches.

A matching crontab is provided in `imdb-prefetch.crontab`. On this deployment it is installed with:

```bash
crontab imdb-service/imdb-prefetch.crontab
crontab -l
```

It runs daily at 10:00 UTC and logs under the `kometa-imdb-prefetch` syslog tag. Inspect recent entries
with:

```bash
journalctl -t kometa-imdb-prefetch --since today
```

Installing the supplied crontab replaces the current user's crontab. Merge its schedule into the
existing output of `crontab -l` instead when other user cron jobs are already configured.

Each invocation seeds one exact cache entry. In particular, a combined `list.any` or `keyword.any`
search does not seed separate entries for its individual values. Re-running the commands while the
entries are valid reads from the cache rather than fetching them from IMDb again. Keyword entries have
a 7-day TTL; list entries have a 1-day TTL.

---

## Attributes used by built-in searches

Across all built-in `imdb_search` usage, only these attributes appear:

- `keyword.any` — keyword matching (Based on..., and several seasonal keys)
- `list.any` — IMDb list references (most seasonal keys)
- `type` — title type (movie/show resolution)
- `sort_by` — `popularity.asc`, `release.desc`, or `rating.desc`
- `limit` — `200` (Based on...) or `500` (Seasonal)

Notably, **none of Kometa's built-in searches use the `interests` attribute**, so the interests-catalog
work (INTERESTS.json / `interest_options`) does not affect any bundled Default.
