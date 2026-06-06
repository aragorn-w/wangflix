# ops/ — Versioned orchestration snapshots

This directory tracks live system orchestration (cron, systemd) so the repo
is the source of truth and an outside replicator can stand the same setup
up from scratch.

## Files

- `cron.d/media-stack.crontab` — versioned snapshot of the user crontab.
  Uses `$HOME/media-stack` as a placeholder for the install path; cron's
  default shell expands it at run time, so for hosts where the install
  path matches `$HOME/media-stack` the snapshot is installable as-is.
  For other paths, render via `ops/install.sh`.
- `systemd/*.service` — versioned snapshots of the installed unit files.
  Use `$HOME/media-stack` for the install path and `User=mediauser` /
  `Group=mediauser` as placeholders.  Templates only — DO NOT install
  raw; `mediauser` is not a real account.  Always render through
  `ops/install.sh --apply`, which substitutes the placeholders for this
  operator's MEDIA_STACK_ROOT + invoking user/group.
- `logrotate.d/media-stack` — logrotate snapshot for `var/log/*.log`
  (weekly, rotate 7, compress, `copytruncate`, `su root root`).  Uses the
  `$HOME/media-stack` path placeholder; `ops/install.sh` renders it and
  installs to `/etc/logrotate.d/media-stack`.
- `install.sh` — placeholder renderer + installer (cron + systemd + logrotate).
- `audit.sh` — drift checker.  Diffs the live state (cron, systemd,
  logrotate) against the snapshots here.  Tri-state exit code:
  - `0` clean — every snapshot matched live state
  - `1` drift — live cron or systemd differs from the repo snapshot
  - `2` warning-only — at least one unit body is mode-640 so its drift
    couldn't be checked (e.g. `realtek-fix.service` on this host).
    `healthcheck.sh` maps this to a yellow warning, not a failure.

## Mandatory ritual for changes

If you change anything that runs on cron or under systemd, you must:

1. Update the snapshot file in this directory in the **same commit** as
   the live change.
2. Run `bash ops/audit.sh` afterwards — it must exit 0 or 2 (drift is exit 1).
3. If you can't read a unit's body (mode-640 file), audit.sh emits a
   WARN and exits 2.  That's expected for `realtek-fix.service` — note
   the warning but treat as informational.

Drift between live state and these snapshots is the most common way agents
silently break orchestration.  The audit is the safety net; don't skip it.

## Replication

Order of operations for a fresh install:

1. `cp ../.env.example ../.env`, fill values.
2. `docker compose -f ../docker-compose.yml up -d`.
3. Render + install cron + systemd units via the installer:
   ```bash
   bash ops/install.sh                # dry-run preview
   bash ops/install.sh --apply        # actually install
   ```
   The installer substitutes the `$HOME/media-stack` path placeholder
   for the operator's `MEDIA_STACK_ROOT` and the `mediauser` user/group
   placeholders for the invoking user/group, then `crontab` + `sudo
   install`s the rendered files.  It also skips host-specific units
   (`realtek-fix.service`) unless `INCLUDE_HOST_SPECIFIC=1` is set.
4. `bash ../preflight.sh && bash ../healthcheck.sh` — both should exit 0.
