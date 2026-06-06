# media-stack

Self-hosted Linux DVR + media normalization pipeline. Designed for a small home-server class machine (8-core CPU, 16–32 GB RAM, optional consumer GPU, Ubuntu 22.04). Tailscale for remote access; nothing public-facing.

## What's inside

- **Docker stack** (`docker-compose.yml`): Gluetun (ProtonVPN/WireGuard) + qBittorrent + Radarr + Sonarr + Bazarr + Prowlarr + Jellyfin + Jellyseerr + Tdarr + Watchtower + Unpackerr + flaresolverr.
- **Media pipeline**: `consolidate-subs.py` (subs+audio+container repair) + `normalize-audio.py` (EBU R128 -23 LUFS loudness), driven by `consolidate-watch.service` (inotify) and cron sweeps.
- **Operational scripts**: `nuke_stalled.py` (qBit/Arr stall reaper), `normalize-driver.sh` (autonomous loudness watchdog), `consolidate-status.sh` / `normalize-status.sh` (snapshots), `jellyfin-mint-api-key.py` (safety-verified Jellyfin admin API key minting; see `AGENTS.md` § "Jellyfin admin ops").

For full agent-targeted documentation see `AGENTS.md`.

## First-run setup (fresh install / replication walkthrough)

End-to-end path from a fresh Ubuntu 22.04 box to a running stack.  Each
step is independently verifiable.

1. **Clone the repo.**  Runtime state (`config/`, `data/`, logs, locks)
   is gitignored and will be populated by the services.
2. **Fill the env contract.**
   ```bash
   cp .env.example .env
   $EDITOR .env
   ```
   `.env.example` flags every variable as `[REQUIRED]` or `[OPTIONAL]`.
   At minimum: `WIREGUARD_PRIVATE_KEY`, `WIREGUARD_ADDRESS`,
   `SONARR_API_KEY`, `RADARR_API_KEY`, `QBIT_USER`, `QBIT_PASS`.
   `VPN_COUNTRY` defaults to Switzerland.
3. **Install Python dependencies.**  Do this BEFORE `preflight.sh` —
   the preflight checks that `requests`, `pysubs2`, and `srt` import
   cleanly, so it'll fail on a clean machine otherwise.
   ```bash
   pip3 install --user -r requirements.txt
   ```
4. **Verify host dependencies are present.**
   ```bash
   bash preflight.sh --verbose
   ```
   Required binaries: `ffmpeg`, `ffprobe`, `mkvmerge`, `mkvpropedit`,
   `mkvextract`, `python3`, `docker`, `inotifywait`, `curl`, `flock`,
   `subliminal`, `ffsubsync`.  Optional: `nvidia-smi` for HW transcode.
   Exit 0 → ready; exit 1 → fix the missing piece before continuing.
5. **Bring up the Docker stack.**
   ```bash
   docker compose up -d
   ```
6. **Install systemd units + the user crontab.**  The checked-in
   snapshots under `ops/` use generic placeholders (`$HOME/media-stack`,
   `User=mediauser`, `Group=mediauser`).  The installer is required —
   raw snapshots are templates and won't run as-is because
   `mediauser` is a placeholder, not a real account:
   ```bash
   bash ops/install.sh                # dry-run preview
   bash ops/install.sh --apply        # actually install
   ```
   The installer substitutes `MEDIA_STACK_ROOT` (from `.env`) for the
   `$HOME/media-stack` placeholder and the invoking user/group for the
   `mediauser` placeholder, writes rendered files to a per-run
   `mktemp -d` directory, then `crontab` + `sudo install`s them.
   Re-runnable; same inputs produce identical output.  Both
   `consolidate-watch.service` and `malware-guard.service` are
   stack-mandatory (see `ops/AGENTS.md` for the per-unit install
   ritual).  `realtek-fix.service` is host-specific to one NIC model
   and is skipped at install time unless `INCLUDE_HOST_SPECIFIC=1`.
7. **Verify everything's healthy.**
   ```bash
   bash healthcheck.sh --verbose
   ```
   Exit 0 → all green; exit 1 → real failure listed; exit 2 → warnings
   only.  The hourly cron entry runs this automatically afterward.
8. **Run the test suite.**  (Optional but recommended on first install.)
   ```bash
   python3 -m pytest tests/ -q
   ```

**For replicators on a different host:** uncomment and override these
in `.env` BEFORE step 5 (`docker compose up -d`):
- `MEDIA_STACK_ROOT` if the repo isn't at `$HOME/media-stack`
- `MEDIA_LAN_IP` to your host's LAN IP — used to build host-side
  service URLs (`SONARR_URL`, `RADARR_URL`, etc.) and as the target
  for healthcheck API probes.  It does NOT scope the docker port
  bindings: this stack publishes admin ports on all interfaces by
  design (Tailscale + Tailnet Lock + no public iface).  If your
  replicator host has a public interface, narrow the compose
  `ports:` lines manually or front the services with a proxy.
- `CALIBRE_LIBRARY` to your Calibre library if Kavita is enabled
- `JELLYFIN_PUBLISHED_URL` to your Tailscale MagicDNS / LAN name
- `PUID`/`PGID` if your non-root user isn't UID 1000

Everything else (paths, service URLs) derives automatically.  Both
`media_paths.py` (Python) and `lib/paths.sh` (shell) load these from
`.env`; defaults match this server.

## Operational commands

```bash
docker compose ps                          # health
docker compose logs -f --tail 100 [svc]    # tail
docker compose restart [svc]               # bounce
bash consolidate-status.sh                 # pipeline snapshot
bash normalize-status.sh                   # loudness coverage
python3 nuke_stalled.py                    # one-shot stall reap
```

## Cron orchestration

| When | Job |
| :--- | :--- |
| every minute | `nuke_stalled.py` — reaps stalled qBit/Arr torrents |
| `*/15` | `normalize-driver.sh` — autonomous loudness sweep watchdog |
| `0 *` | `healthcheck.sh` — hourly aggregate health probe |
| `05:00` daily | `consolidate-subs.py --scan` — full library sweep |
| `04:30` daily | `bazarr-profile-audit.py` — fixes silent `profileId=None` items |
| `04:00` daily | `nightly-upgrade.sh` — host package updates |
| Sundays `03:00` | weekly prune (docker image prune + aptitude autoclean + journal vacuum) |

Versioned snapshot of the live crontab lives at `ops/cron.d/media-stack.crontab`.
`clean-subs.py` was retired on 2026-05-15 and deleted in the 2026-06-01
debloat cleanup (v2 logic lives in `consolidate-subs.py`; pre-v2 history
is in git).

## Safety notes

- **Tailscale handles the perimeter.** UFW must stay OFF (locks out SSH).
- **No public ports.** All admin interfaces (qBit, Sonarr, Radarr, Bazarr, Jellyseerr) are LAN/Tailnet-only.
- **VPN kill switch (Gluetun) is active.** Containers in `network_mode: service:gluetun` cannot leak the real IP.
- **`config/` and `data/` are runtime state.** Don't commit them. Don't edit them while services run unless you intend to.

## Layout

See `AGENTS.md` § "Repo Layout".

## Troubleshooting

- Bazarr says "no subs" → check `profileId` is not None (see `bazarr-profile-audit.py`).
- Movies lose JPN audio after import → Radarr Custom Format may be filtering JPN-only releases; manual re-search.
- Jellyfin shows sub but renders nothing on Shield → `accessibility_captioning_enabled` setting.
- mergerfs branch shows 90% full → that's just one branch; pool free space is what matters.

## License / use

Personal home server. Not packaged for general distribution.
