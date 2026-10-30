# ops/: versioned orchestration snapshots

Tracks live system orchestration (cron, systemd, logrotate) so the repo is the source of truth and a replicator can stand the setup up from scratch.

## Files

- `cron.d/media-stack.crontab`: user crontab snapshot, with `$HOME/media-stack` as the install-path placeholder. Templates only: its first line puts `$HOME/media-stack/.venv/bin` on `PATH` so every `python3` resolves into the uv venv, and cron does not expand variables in an assignment, so DO NOT install raw; render via `ops/install.sh --apply` (`consolidate-watch.service` gets the same `Environment=PATH=` line).
- `systemd/*.service`: installed-unit snapshots with `$HOME/media-stack` and `User=mediauser` / `Group=mediauser` placeholders. Templates only: DO NOT install raw (`mediauser` is not a real account); always render through `ops/install.sh --apply`, which substitutes `MEDIA_STACK_ROOT` and the invoking user/group.
- `logrotate.d/media-stack`: `var/log/*.log` rotation, rendered by `install.sh` into `/etc/logrotate.d/media-stack`.
- `shield/profile.json`: desired state read by `shield-check.py` (report-only; nothing applies it). No addresses (those come from `SHIELD_DEVICES` in `.env`); device-specific settings are deliberately left out so the check never flags them.
- `gluetun/auth-config.toml`: grants the one read-only control-server route `sync-forwarded-port.py` uses.
- `install.sh`: placeholder renderer + installer; skips host-specific units (`realtek-fix.service`) unless `INCLUDE_HOST_SPECIFIC=1`.
- `audit.sh`: diffs live cron, systemd and logrotate against these snapshots. Exit `0` clean; `1` drift; `2` warning-only (a mode-640 unit body can't be read, e.g. `realtek-fix.service`: expected, informational; `healthcheck.sh` shows it as a warning).

## Mandatory ritual for changes

Drift between live state and these snapshots is the most common way agents silently break orchestration; don't skip the audit. When changing anything that runs on cron or under systemd: update the snapshot here in the **same commit** as the live change, then run `bash ops/audit.sh` (must exit 0 or 2; 1 is drift).

## Replication

1. `cp ../.env.example ../.env`, fill values.
2. `docker compose -f ../docker-compose.yml up -d`, and `uv sync` in the repo root (creates `.venv`, which the rendered crontab and unit put first on `PATH`; do this BEFORE step 3).
3. `bash ops/install.sh` (dry-run preview), then `bash ops/install.sh --apply` (renders the placeholders, then `crontab` + `sudo install`s cron, systemd and logrotate).
4. `bash ../preflight.sh && bash ../healthcheck.sh`: both should exit 0.
