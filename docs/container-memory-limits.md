# Container memory limits — research notes

**Date:** 2026-10-02
**Status:** applied 2026-10-02 -- tenant dashboard capped at `512m`
(generator + regenerated tenant file + container recreated). Base
`dashboard` and both `youtube-comments` services are still uncapped.
**Scope:** why `travel_explorer_satya-dashboard` appeared to use 7.65 GB, what it
actually uses, and where to set a real limit so it survives re-provisioning.

Written so this doesn't have to be re-derived. Everything below was measured on
this machine, not estimated.

---

## 1. The premise was a misreading

`docker stats` shows `MEM USAGE / LIMIT`. The number after the slash is the
**limit**, not consumption:

```
social-comment-bot-travel_explorer_satya-dashboard-1   89.05MiB / 7.651GiB   1.14%
                                                       ^^^^^^^^   ^^^^^^^^^
                                                       actual     limit
```

7.651 GiB is exactly this Docker Desktop VM's total memory
(`docker info --format '{{.MemTotal}}'`). A container with **no** memory limit
reports the whole VM as its ceiling, so `7.651GiB` here literally means
"unlimited". Confirmed: `HostConfig.Memory: 0`.

So nothing was wrong — the container was using ~1.1% of the machine.

## 2. What it actually uses

Read from the container's own cgroup v2 files, not from `docker stats`:

```bash
C=social-comment-bot-travel_explorer_satya-dashboard-1
docker exec $C sh -c 'cat /sys/fs/cgroup/memory.current /sys/fs/cgroup/memory.peak'
docker exec $C sh -c 'grep -E "^(anon|file|slab|sock) " /sys/fs/cgroup/memory.stat'
```

| Metric | Value | Notes |
|---|---|---|
| `memory.current` | 99.4 MiB | includes reclaimable page cache |
| `memory.peak` | **107.9 MiB** | high-water mark since start |
| `anon` | 87.8 MiB | real, non-reclaimable working set |
| `file` | 6.9 MiB | page cache — kernel reclaims this under pressure |
| `slab` | 2.3 MiB | |
| uptime at measurement | 4 hours | **peak is a floor, not a lifetime max** |

Workload: gunicorn, **1 worker / 4 threads**, `app.dashboard:create_serving_app()`,
bound to `0.0.0.0:9001`, published on host `127.0.0.1:9101`.

## 3. Decision: `mem_limit: 512m` (applied)

- ≈4.7× observed peak, ≈5.8× the anon working set.
- Still a **93% reduction** from the 7.65 GiB effective ceiling.
- Deliberately *not* `256m` (which would match the `video-stats` sibling): that's
  only 2.4× peak, measured over a mere 4-hour window, on the **user-facing**
  process. An OOM-kill of the dashboard is a worse outcome than a slightly loose
  cap. `256m` is defensible if you first watch `memory.peak` across a few days
  and a real traffic spike.

Because `file` cache is reclaimable, a limit does not hard-fail at
`memory.current` — the kernel evicts cache first. Only `anon` growth can OOM.

### How low could it go?

Probed on 2026-10-02: live-capped to `160m` (`--memory 160m --memory-swap 160m`,
swap denied) and hit with 400 requests at 24-way concurrency against `/login`.
Result: all `200`, `memory.peak` 93.8 MiB, `memory.events` all zero (no `high`,
no `max`, no `oom_kill`). So even 160m held under that load.

That is **not** proof 160m is safe, and the probe does not justify dropping to
it. `/login` is the cheapest route in the app; every data-heavy view
(`/`, `/insights`, `/momentum`, `/status`) is behind auth and was never
exercised, and the container only holds a `DASHBOARD_PASSWORD_HASH_B64`, so
there's no password to log in with. The real per-request cost of a chart-heavy
view across 4 live threads is still unmeasured.

Settled on `512m` (user's call, 2026-10-02). Reopen this only with
`memory.peak` sampled over days *and* an authenticated run through the heavy
views.

## 4. The trap: the tenant compose file is generated

`tenants/travel_explorer_satya.compose.yml` is **machine-generated**. Its own
header says so, and `provision --force` regenerates it — so a hand edit there
works until the next provision, then silently vanishes.

**Durable fix is the template, not the output:**

- `billing/cli.py:119` — `_render_tenant_compose(tenant_key, port)` builds the
  whole tenant file as an f-string.
- `billing/cli.py:243` — where it's written out.
- Add the limit inside the `{tenant_key}-dashboard` service block, mirroring the
  existing `video-stats` pattern at **`billing/cli.py:179-180`**
  (`cpus: "0.35"` / `mem_limit: 256m`).

⚠️ `common_hardening` (defined ~`cli.py:126`) is one shared string interpolated
into **all three** services. Per-service resource limits must **not** go in
there — put them in each service block individually, before the
`{common_hardening}` interpolation.

After editing the generator, regenerate and recreate:

```bash
# regenerate the tenant file via the normal provision path, then:
docker compose up -d travel_explorer_satya-dashboard
```

## 5. Current limit coverage across the project

```bash
for c in $(docker ps --format '{{.Names}}' | grep social-comment-bot); do
  lim=$(docker inspect "$c" --format '{{.HostConfig.Memory}}')
  printf "%-55s %s\n" "$c" "$([ "$lim" = 0 ] && echo UNLIMITED || echo $((lim/1048576))m)"
done
```

| Container | Limit | Defined at |
|---|---|---|
| `travel_explorer_satya-dashboard-1` | 512m | generator, `cli.py:119` |
| `dashboard-1` | UNLIMITED | `docker-compose.yml:2` |
| `youtube-comments-1` | UNLIMITED | `docker-compose.yml:67` |
| `travel_explorer_satya-youtube-comments-1` | UNLIMITED | generator |
| `video-stats-1` | 256m | `docker-compose.yml:57-58` |
| `travel_explorer_satya-video-stats-1` | 256m | generator, `cli.py:179-180` |

`video-stats` (base + tenant) and the tenant dashboard are capped today.
There is exactly **one** tenant compose file right now
(`tenants/travel_explorer_satya.compose.yml`), so the blast radius is small.

## 6. Design intent — read before capping

The `mem_limit`/`cpus` on `video-stats` carries this comment
(`docker-compose.yml:55-56`):

> Stats are eventually consistent. Reserve most of the small VM's CPU for
> Gunicorn and the webhook worker during a refresh cycle.

So the dashboard being uncapped looks **deliberate**: it's the intended priority
process, and the background worker was throttled to protect it. Capping the
dashboard isn't wrong — an unbounded leak can take the whole VM down — but a
*generous* limit respects that intent where a tight one fights it. This is the
main judgment call; everything else here is mechanical.

## 7. Applying a limit to a running container

`mem_limit` cannot be changed in place by Compose — the container must be
recreated (a few seconds of downtime):

```bash
docker compose up -d travel_explorer_satya-dashboard
```

For zero-downtime right now, `docker update` takes effect live:

```bash
docker update --memory 512m --memory-swap 512m \
  social-comment-bot-travel_explorer_satya-dashboard-1
```

…but this does **not** persist across a recreate, and does not touch the
generator. Use it only as a stopgap alongside the real change.

## 8. What was done, and what's left

Done (2026-10-02):

1. `_render_tenant_compose()` in `billing/cli.py` -- `mem_limit: 512m` in the
   `{tenant_key}-dashboard` block, before the `{common_hardening}`
   interpolation (so it stays per-service, not shared).
2. Regenerated `tenants/travel_explorer_satya.compose.yml` by calling
   `_render_tenant_compose('travel_explorer_satya', 9101)` directly --
   deliberately **not** `provision --force`, which would also rewrite
   `tenants/travel_explorer_satya.env` and mint a new admin password.
3. `docker compose ... up -d travel_explorer_satya-dashboard` to recreate.
   Verified: `HostConfig.Memory = 536870912`, `docker stats` now reads
   `77.77MiB / 512MiB`, dashboard answers `302` on `127.0.0.1:9101`.

Note: Compose sets only `Memory`, so Docker defaults `MemorySwap` to 2x
(1 GiB). Add `memswap_limit: 512m` if swap should be denied outright --
`video-stats` doesn't, so this matches the existing pattern.

Left open:

4. Base `dashboard` service (`docker-compose.yml:2`) -- still uncapped, and its
   working set was never measured (see section 9). Measure before copying 512m.
5. Both `youtube-comments` services -- still uncapped, never measured.
6. Re-check `memory.peak` after a day under the 512m cap to confirm headroom.

## 9. Not yet measured

- `youtube-comments` (both instances) — no cgroup readings taken.
- `dashboard-1` (main instance) — `docker stats` returned `0B / 0B` for it at
  measurement time, so its real working set is unknown. Measure it before
  copying the 512m number across; it may differ from the tenant dashboard.
- Peak behaviour under real traffic or a large data view — the 107.9 MiB figure
  comes from a quiet 4-hour window.
