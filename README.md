# media-stack

Self-hosted Linux DVR + media normalization pipeline. Designed for a small home-server class machine (8-core CPU, 16-32 GB RAM, optional consumer GPU, Ubuntu 22.04). Tailscale for remote access; nothing public-facing.

## What's inside

- **Docker stack** (`docker-compose.yml`): Gluetun (ProtonVPN/WireGuard) + qBittorrent + Radarr + Sonarr + Bazarr + Prowlarr + Jellyfin + Jellyseerr + Tdarr + Watchtower + Unpackerr + flaresolverr.
- **Media pipeline**: `consolidate-subs.py` (subs + audio + container repair) + `normalize-audio.py` (EBU R128 -23 LUFS loudness), driven by `consolidate-watch.service` (inotify) and cron sweeps.
- **Operational scripts**: `nuke_stalled.py` (qBit/Arr stall reaper), `consolidate-status.sh` (pipeline snapshot), `jellyfin-mint-api-key.py` (safety-verified Jellyfin admin API key minting).

Agent-targeted documentation, including the pipeline's rules and invariants, is in `AGENTS.md`.

## First-run setup (fresh install / replication walkthrough)

End-to-end path from a fresh Ubuntu 22.04 box to a running stack. Each step is independently verifiable.

1. **Clone the repo.** Runtime state (`config/`, `data/`, logs, locks) is gitignored and populated by the services.
2. **Fill the env contract.**
   ```bash
   cp .env.example .env
   $EDITOR .env
   ```
   `.env.example` flags every variable as `[REQUIRED]` or `[OPTIONAL]`. At minimum: `WIREGUARD_PRIVATE_KEY`, `WIREGUARD_ADDRESS`, `SONARR_API_KEY`, `RADARR_API_KEY`, `QBIT_USER`, `QBIT_PASS`. `VPN_COUNTRY` defaults to Switzerland.
3. **Install Python dependencies** BEFORE `preflight.sh` (it checks that `requests`, `pysubs2` and `srt` import cleanly, so it fails on a clean machine otherwise).
   ```bash
   pip3 install --user -r requirements.txt
   ```
4. **Verify host dependencies.**
   ```bash
   bash preflight.sh --verbose
   ```
   Required binaries: `ffmpeg`, `ffprobe`, `mkvmerge`, `mkvpropedit`, `mkvextract`, `python3`, `docker`, `inotifywait`, `curl`, `flock`, `subliminal`, `ffsubsync`. Optional: `nvidia-smi` for HW transcode. Exit 0 = ready; exit 1 = fix the missing piece first.
5. **Bring up the Docker stack.**
   ```bash
   docker compose up -d
   ```
6. **Install systemd units + the user crontab.** The snapshots under `ops/` use generic placeholders (`$HOME/media-stack`, `User=mediauser`, `Group=mediauser`), so they are templates that won't run as-is (`mediauser` is not a real account); always go through the installer:
   ```bash
   bash ops/install.sh                # dry-run preview
   bash ops/install.sh --apply        # actually install
   ```
   It substitutes `MEDIA_STACK_ROOT` (from `.env`) for the path placeholder and the invoking user/group for `mediauser`, renders into a per-run `mktemp -d` directory, then `crontab` + `sudo install`s the result. Re-runnable; same inputs give identical output. `consolidate-watch.service` and `malware-guard.service` are stack-mandatory; `realtek-fix.service` is specific to one NIC model and is skipped unless `INCLUDE_HOST_SPECIFIC=1`.
7. **Verify everything's healthy.**
   ```bash
   bash healthcheck.sh --verbose
   ```
   Exit 0 = all green; exit 1 = real failure listed; exit 2 = warnings only. The hourly cron entry runs this automatically afterward.
8. **Run the test suite** (optional but recommended on first install).
   ```bash
   python3 -m pytest tests/ -q
   ```

**Replicators on a different host:** uncomment and override these in `.env` BEFORE step 5:

- `MEDIA_STACK_ROOT` if the repo isn't at `$HOME/media-stack`.
- `MEDIA_LAN_IP` to your host's LAN IP. It builds the host-side service URLs (`SONARR_URL`, `RADARR_URL`, ...) and is the target for healthcheck API probes. It does NOT scope the docker port bindings: this stack publishes admin ports on all interfaces by design (Tailscale + Tailnet Lock + no public interface). If your host has a public interface, narrow the compose `ports:` lines manually or front the services with a proxy.
- `CALIBRE_LIBRARY` to your Calibre library if Kavita is enabled.
- `JELLYFIN_PUBLISHED_URL` to your Tailscale MagicDNS / LAN name.
- `PUID`/`PGID` if your non-root user isn't UID 1000.

Everything else (paths, service URLs) derives automatically: `media_stack.paths` (Python) and `lib/paths.sh` (shell) both load these from `.env`; defaults match the reference server.

## Operational commands

```bash
docker compose ps                          # health
docker compose logs -f --tail 100 [svc]    # tail
docker compose restart [svc]               # bounce
bash consolidate-status.sh                 # pipeline snapshot
python3 nuke_stalled.py                    # one-shot stall reap
```

## Cron orchestration

The main jobs; the versioned snapshot `ops/cron.d/media-stack.crontab` is the full, authoritative list.

| When | Job |
| :--- | :--- |
| every minute | `nuke_stalled.py`: reaps stalled qBit/Arr torrents |
| `0 *` | `healthcheck.sh`: hourly aggregate health probe |
| `05:00` daily | `consolidate-subs.py --scan`: full library sweep |
| `04:30` daily | `bazarr-profile-audit.py`: fixes silent `profileId=None` items |
| `04:00` daily | `nightly-upgrade.sh`: host package updates |
| `12:15` daily | `shield-check.py`: report-only NVIDIA Shield configuration drift check |
| Sundays `03:00` | weekly prune (docker image prune + aptitude autoclean + journal vacuum) |

## Shield configuration check

`shield-check.py` keeps NVIDIA Shield TV units configured alike. Once a day it compares each unit over network ADB with the tracked `ops/shield/profile.json` (packages that must be disabled, enabled or absent, a set of system settings, and the enabled accessibility and keyboard components) and sends one Telegram message only when a unit's findings change, or when a unit has been unreachable for three runs in a row (and again when it comes back). It is report-only: it reads state and never changes a device, so fixes stay manual, each finding having a hint line in `var/log/shield-check.log`.

Turn on network debugging on each Shield, then set `SHIELD_DEVICES` in `.env` (see `.env.example`); unset, the check does nothing. Device addresses live only there, never in tracked files.

```bash
python3 shield-check.py --no-notify                  # print findings and hints; send and write nothing
python3 shield-check.py --device living --no-notify  # one unit only
```

## Safety notes

- **Tailscale handles the perimeter.** UFW must stay OFF (it locks out SSH).
- **No public ports.** All admin interfaces (qBit, Sonarr, Radarr, Bazarr, Jellyseerr) are LAN/Tailnet-only.
- **VPN kill switch (Gluetun) is active.** Containers in `network_mode: service:gluetun` cannot leak the real IP.
- **`config/` and `data/` are runtime state.** Don't commit them, and don't edit them while services run unless you intend to.

## Layout

See `AGENTS.md` ("Repo Layout").

## Troubleshooting

- Bazarr says "no subs": check `profileId` is not None (see `bazarr-profile-audit.py`).
- Movies lose JPN audio after import: a Radarr Custom Format may be filtering JPN-only releases; re-search manually.
- Jellyfin shows a sub but renders nothing on Shield: check the `accessibility_captioning_enabled` setting.
- mergerfs branch shows 90% full: that's one branch; pool free space is what matters.

## License / use

Personal home server. Not packaged for general distribution.
