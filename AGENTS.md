# AGENTS.md — Media Stack (DVR)

Canonical agent guidance for this directory. `CLAUDE.md` is a symlink to this file.
Top-level home-server policy lives at `$HOME/.claude/CLAUDE.md`, not in this repo.
The rest of this paragraph is maintainer-host only; replicators can ignore it.
That file is generated, and hand-edits to it are reverted within 30 minutes, so
change it with `chezmoi edit --apply $HOME/.claude/CLAUDE.md`, or
non-interactively by editing `$(chezmoi source-path $HOME/.claude/CLAUDE.md)` and
then running `chezmoi apply $HOME/.claude/CLAUDE.md`. `$HOME/CLAUDE.md` there is
retired and intentionally left empty; do not delete it.

---

## Quickstart

```bash
docker compose ps                          # stack status
docker compose restart [service]           # bounce a single container
docker compose logs -f --tail 100 [svc]    # tail logs
python3 $HOME/media-stack/nuke_stalled.py   # one-shot stall-killer
bash $HOME/media-stack/consolidate-status.sh
nvidia-smi                                 # verify NVIDIA GPU is visible
```

## Core Mandates

- **VPN:** Gluetun (ProtonVPN WireGuard) locked to **Switzerland**. Ports 8090, 8989, 7878 route through tunnel. `VPN_COUNTRY` in `.env` is authoritative; compose default mirrors it.
- **Media Rules:** Radarr/Sonarr strictly block AV1 and 12-bit HEVC (Custom Formats, -10000 score in "Shield Prioritized" profiles). Tdarr/Bazarr locked to English.
- **Hardlinks:** ENABLED in Radarr/Sonarr. Do not disable or disk usage will double.
- **DNS:** Forced to Google/Cloudflare (8.8.8.8) in Docker to fix Trakt API timeouts.
- **API calls from host:** Must use the LAN IP (`MEDIA_LAN_IP` in `.env`, default `10.0.0.10`), not `localhost` (Gluetun resets loopback connections).  Shell scripts source `lib/paths.sh` for the value; Python imports from `media_stack.paths` (new code) or `media_paths` (legacy compat shim re-exporting the same names — don't reach for it in new code).
- **Network perimeter:** Tailscale handles encryption; UFW must stay OFF (locks out SSH). Admin services bind to all interfaces but the box only listens on Tailscale + its LAN subnet (configure via `FIREWALL_OUTBOUND_SUBNETS` in `.env`) — no public exposure.

## Service Map

Every container that publishes a host port is listed below.  Anything not in
this table either binds to the gluetun network namespace (no separate host
port) or is internal-only.

| Service | URL (LAN) | Auth |
| :--- | :--- | :--- |
| qBittorrent | `http://10.0.0.10:8090` | `QBIT_USER` / `QBIT_PASS` in `.env` |
| Sonarr | `http://10.0.0.10:8989` | `SONARR_API_KEY` in `.env` |
| Radarr | `http://10.0.0.10:7878` | `RADARR_API_KEY` in `.env` |
| Prowlarr | `http://10.0.0.10:9696` | no auth (Tailnet/LAN only) |
| Jellyfin | `http://10.0.0.10:8096` | local user/pass (rotate from defaults) |
| Jellyseerr | `http://10.0.0.10:5055` | no auth on bootstrap; pair to Jellyfin |
| Bazarr | `http://10.0.0.10:6767` | apikey inside `docker exec bazarr cat /config/config/config.yaml` |
| Flaresolverr | `http://10.0.0.10:8192` | no auth (LAN/Tailnet only; called from Prowlarr).  Host port 8192 maps to container port 8191 via gluetun's port-publish list. |
| Tdarr server | `http://10.0.0.10:8265` | no auth (UI), `8266` worker API |
| Kavita | `http://10.0.0.10:5000` | local user/pass |
| NFS | `<MEDIA_LAN_IP>:$HOME/media-stack/data/media` | subnet allow (the LAN subnet in `/etc/exports`) |

**Non-HTTP listen ports** (perimeter inventory, not admin UIs):

| Port | Protocol | Purpose |
| :--- | :--- | :--- |
| `54321/tcp` | torrent peer-listen | qBittorrent incoming connections via Gluetun's WireGuard-mapped port-forward.  Published to the LAN so qBit can accept incoming peer connections through the VPN. |
| `54321/udp` | torrent peer-listen | Same purpose, UDP variant (uTP / µTP fallback). |

**Ports NOT exposed to the LAN** (gluetun namespace, no host port published):
`unpackerr`, `watchtower`, `tdarr-node`.

Audit drift between this table and live state with:

```bash
docker compose ps --format json | jq '.[] | {Service:.Service, Publishers:.Publishers}'
```

**Credential policy:** never put live secrets in `AGENTS.md`/`CLAUDE.md`, scripts, or commits. Use `.env` (gitignored) and reference variable names in docs. `.env.example` lists every required key.

## Repo Layout

For the directory tree itself, `find`/`ls` on a checkout is authoritative — it's
not repeated here. The non-obvious part is the runtime-artifact convention:

**Runtime-artifact convention:** as of DEFERRED #5 (landed 2026-05-19),
every stack-level ephemeral file lives under `var/`.  Shell scripts get
the paths from `lib/paths.sh` (`$VAR_LOG`, `$VAR_RUN`, `$VAR_STATE`,
`$VAR_REVIEWS`); Python imports the same names from `media_stack.paths`
(or the `media_paths` shim, for backward compat — prefer the package
in new code).

Conventions:
- Logs (`*.log`, `*.out`, `*.err`) → `var/log/`
- Lock files (`*.lock`) → `var/run/`
- Completion sentinels (`*.done`), JSON state, scan output → `var/state/`
- Codex multi-agent reviews → `var/reviews/`

Per-file media locks (`.consolidate-<name>.lock`, written next to a
specific media file) DO NOT move into `var/run/` — they need to be on
the same filesystem as the media file for flock semantics.  Only
stack-level runtime files live under `var/`.

The `config/` tree (per-Docker-service persistent state — Sonarr DB,
Bazarr settings, etc.) stays where it is so the bind-mounts in
`docker-compose.yml` continue to work.

## Media Normalization Pipeline (v2)

- **Authoritative tool:** `consolidate-subs.py` — handles subs, audio, and container repair despite the name.
  - **Subtitles:** picks highest-scoring English sub (text > image; SDH > non-SDH), runs `ffsubsync`, regex-cleans `\h` / ASS override tags / stray HTML, smart-recases ALL-CAPS, remuxes lone English sub with `default=1` (forced track preserved second).
  - **Audio:** single primary track. Default English; Japanese for anime / `/anime/` paths; Korean for Korean cinema. Drops commentary, audio-description, visual-impaired, non-primary dubs.
  - **Container:** final mux is **mkvmerge** (not ffmpeg) — produces a proper cue index. ffmpeg's matroska muxer wrote zero-cue files, breaking scrubbing + tripping Intro Skipper.
  - **Cover art:** drops still-image "video" tracks (mjpeg/png/jpeg) lacking `attached_pic` disposition — these caused black-screen-no-audio bugs.
  - **Idempotency:** state file at `var/state/consolidate-subs.state.json` + mkv global tag `CONSOLIDATED_SUBS=v2`. Bump `PIPELINE_VERSION` to trigger re-flow.

- **Audio loudness (EBU R128 -23 LUFS):** `normalize-audio.py`. Two-pass `loudnorm`; tags `NORMALIZED_AUDIO=v1` in MKV format_tags. Atmos 7.1.2 inputs (FL+FR+FC+LFE+SL+SR+TFL+TFR) get downmixed to 5.1 before AAC encode (native ffmpeg AAC can't take non-standard 8ch).

- **Driver:** `normalize-driver.sh` runs every 15 min via cron. Idempotent — checks coverage; relaunches sweep with `setsid` at `JOBS=5` if dead and <100%; touches `normalize-driver.done` + Telegram-notifies when complete. JOBS history: 8/6/4 all ≥50% pass2-timeout fail rate; 3 = 0% but slow (~2/hr); 5 = ~18% fail at ~3/hr (winner). Overridable via `JOBS=N` in `.env`.

- **Triggers:**
  - `consolidate-watch.service` — inotify on media root; fires on `close_write`/`moved_to` for `.mkv`/`.mp4` and sidecar `.srt`/`.ass`/`.vtt`.
  - Cron 05:00 daily — full library sweep with `--jobs 2` as backstop.
  - Cron `*/15` — normalize-driver.sh keeps the loudness sweep alive.
  - Cron 04:30 — `bazarr-profile-audit.py` fixes silently-orphaned profileId=None items.
  - Cron 04:45 — `movie-dedupe.py --apply --notify` resolves duplicate movie
    files left behind when Radarr upgrade-imports over a pipeline-renamed old
    file (Jellyfin would otherwise list both).  Auto-resolves only the SAFE
    case (Radarr already tracks the keeper → moves the leftover to the
    recoverable `.dupe-recycle/`); flags the RISKY case (Radarr tracking a
    non-keeper) for manual review (`movie-dedupe.py --apply --force`).
    `consolidate-watch` excludes `.dupe-recycle` so recycled files aren't
    re-normalized.
  - Cron 04:50 — `tv-dedupe.py --apply --notify`, the Sonarr sibling of
    movie-dedupe.py (runs 5 min later so they never overlap). Same
    SAFE/RISKY model, one TV-specific addition: Sonarr's `episodefile` DB
    table can retain an orphan row for the removed leftover even when the
    correct file is tracked (observed live 2026-07-26, Rick and Morty
    S09E01), so `tv-dedupe.py` also deletes that row via the Sonarr API
    after the physical move. Recycles to `.dupe-recycle/tv/`; RISKY cases
    need manual review (`tv-dedupe.py --apply --force`).

- **Bazarr:** English profile (id=1) attached to everything. Sidecar SRTs ingested by the watcher.

- **Legacy retired 2026-06-01:** `clean-subs.py` was deprecated 2026-05-15
  and deleted in the cleanup pass.  All v2 functionality lives in
  `consolidate-subs.py`.  Pre-v2 implementations are in git history
  (`consolidate-subs.py.pre-v2.bak` was also removed — git history is
  the source of truth).

## Jellyfin admin ops

- **API key minting:** see the `jellyfin-mint-api-key` skill (loads on demand).

## Agent Operating Rules

- **No secrets in code or docs.** Always reference `.env` variable names.
- **No `nohup` alone for jobs that must outlive a Claude session** — always wrap with `setsid` (lesson learned 2026-05-14). See `feedback_setsid_long_bg` in user memory.
- **Long sweeps survive Claude bounces** because `normalize-driver.sh` is cron-driven, not session-driven.
- **Don't touch `consolidate-subs.py` mid-sweep** — review-loop gate applies, and live writes corrupt state.
- **Don't change the R128 -23 LUFS target without approval** — verified against entire library, switch would invalidate all coverage tags.
- **Times reported to the user go in 12-hour AM/PM in the operator's local timezone.** Internal log timestamps follow the host timezone (set via `timedatectl`); cron schedules are likewise interpreted in the host TZ.
- **Verify before claiming UI works** — type-checks and tests verify correctness, not behavior. For UI changes, exercise the feature in a browser before reporting success.

## Common Pitfalls

- Bazarr `profileId: None` items are silently invisible to sub search — has bitten 3x. Daily `bazarr-profile-audit.py` cron is the backstop; manual `bazarr-profile-audit.py` after big imports.
- After NVIDIA driver branch purge: `apt autoremove` will yank the active stack. `apt-mark manual` the active branch first.
- Mergerfs branches: `/mnt/disk{1,2,3}` are pool members; high per-branch `df` is not "data outside the pool".
- Claude session bounce + `nohup ... &` background = SIGKILL (logind/cgroup cleanup). Use `setsid`.
- `pgrep` regex: include `--scan.*movies` not just `normalize-audio.py` (the wire-in path also runs single-file invocations).

## Maintenance

- **Updates:** Host via `aptitude` (nightly 04:00 via `nightly-upgrade.sh`). Containers via Watchtower (24h cycle). See `$HOME/.claude/CLAUDE.md` for the home-server policy.
- **Power:** `restart: unless-stopped` on all containers.
- **Backups:** none configured for media (intentional — re-fetchable). Service config under `config/` should be snapshotted before destructive changes.

### Trade-off: `:latest` images + Watchtower auto-update (explicit)

Container images in `docker-compose.yml` use `:latest` tags with Watchtower
performing 24h auto-update.  This is a **deliberate choice**, not an
oversight, and was the subject of recurring codex findings (round-2 #9 /
round-3 #13).  The decision: freshness > reproducibility on this server.

**Watchtower fork (2026-05-23):** the `watchtower` service uses
`nickfedor/watchtower:latest`, the actively-maintained community fork.
Upstream `containrrr/watchtower` is abandoned — its last image ships a
Docker API client at version 1.25, which Docker daemon 29.x rejects (it
requires minimum 1.44).  After today's nightly upgrade bumped Docker
daemon 27.5.1 → 29.1.3, the upstream image entered a permanent restart
loop with `client version 1.25 is too old`.  The fork ships Watchtower
1.17.1 using API v1.52 — same config interface, same volumes, drop-in
replacement.  Same precedent as the jellyseerr → seerr-team swap in
`reference_jellyseerr_fork.md`.

The auto-update can in principle silently break an integration (a service
changes its API path, container layout, config schema, …).  We accept that
risk because:

1. The "zero-touch" operating mode is the design goal — pinning fights it
2. No incident history (as of 2026-05-19) where Watchtower broke us
3. If something does break, `nightly-upgrade.sh` logs + healthcheck.sh
   exit codes surface it fast; recovery is `docker compose down && up -d
   <service>:<known-good-tag>`

If you replicate this stack and want reproducibility instead, pin tags or
digests in your fork of `docker-compose.yml` and disable Watchtower.  We
don't — the full rationale is the trade-off described above.

## Status / Health Probes

Two layers — human-readable status scripts for narrative output, and the
machine-readable `healthcheck.sh` aggregator that returns exit codes.

- `bash $HOME/media-stack/healthcheck.sh` — aggregated pass/warn/fail
  with exit code (0/1/2).  Wire into monitoring or run pre-deploy.  Add
  `--verbose` for per-check OK lines, `--json` for machine-readable output.
- `bash $HOME/media-stack/consolidate-status.sh` — pipeline snapshot
- `bash $HOME/media-stack/normalize-status.sh` — loudness coverage
- `bash $HOME/media-stack/preflight.sh` — once at install time + after
  host upgrades: verifies every required dependency (ffmpeg, mkvtoolnix,
  ffsubsync, subliminal, inotifywait, docker, …)
- `bash $HOME/media-stack/ops/audit.sh` — diff live cron + systemd
  against the snapshots tracked under `ops/`.  Fails on drift.
- `docker compose ps` — container health
- `docker exec gluetun wget -qO- https://ifconfig.co` — verify VPN egress (must NOT be host IP)
- `tail -20 var/log/stall_killer.log` — qBit reaper activity

## Agent Validation Checklist

Before declaring any code change "done," run the validation appropriate to
the change.  This list is the answer to "which command proves my edit is
acceptable?"

- **Python script edits (`*.py`)**:
  - `python3 -c "import ast; ast.parse(open('FILE').read())"` — syntax
  - `python3 -m pytest tests/ -q` — unit tests (all pytest cases under `tests/` must pass; count grows over time)
  - `ruff check media_stack/ *.py` — lint (config + per-file-ignores in `pyproject.toml`; must stay clean)
  - `mypy` — type-check `media_stack/` (permissive baseline in `pyproject.toml`; must stay clean)
  - Single-file smoke test via `python3 FILE --help` or `--dry-run`
  - Dev tools (`ruff`, `mypy`, `pytest`) come from `requirements-dev.txt`:
    `pip3 install --user -r requirements-dev.txt`
- **Shell script edits (`*.sh`)**:
  - `bash -n FILE` — syntax
  - `shellcheck FILE` if available (not installed by default; flag as
    optional improvement, not blocker)
- **Docker Compose changes (`docker-compose.yml`)**:
  - `docker compose config` — verify it parses
  - `docker compose up -d <changed-service>` — bounce just that service
  - `bash healthcheck.sh` to verify container + VPN + invariants still pass
- **Cron / systemd changes**:
  - Update both the live state AND the snapshot under `ops/`
  - `bash ops/audit.sh` — confirm no drift
- **Media pipeline dry runs**:
  - Before sweeping the library: pick one file, run
    `python3 consolidate-subs.py --dry-run <one-file.mkv>`, eyeball the
    plan, then run for real on that single file
  - Never invoke `consolidate-subs.py --scan` while a sweep is in flight
    (state lock now protects against the most damaging race, but
    overlapping ffmpeg invocations still thrash the HDD)
