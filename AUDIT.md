# Code Audit

Tracker for content-level cleanups (smaller scope than structural refactors). Address items opportunistically when a touched file comes up for other work. Items are tagged `A<n>` so each has a stable handle. Add new items when you spot slop during other work (cheaper than fixing immediately, and the trigger context keeps it pragmatic). Close an item by moving it from "Open" to "Closed" with a one-line marker; full history is in git.

---

## Open

### A13. Shared safe-replacement helper for the pipeline swap logic (deferred)

- **Files**: `consolidate-subs.py`, `normalize-audio.py`
- **Issue**: both entrypoints duplicate the high-risk file-swap choreography (destination locking, sibling `.mkv` collision check, `os.replace`, idempotency tagging, rollback, cleanup). The duplication also sits outside `mypy` (which only covers `media_stack/`).
- **Deferred deliberately**: this is the lossy-replacement core (same risk profile as A12); a wrong extraction can silently corrupt media on the next sweep. Extract a typed `media_stack/replacement.py` (lock -> collision -> swap -> rollback -> cleanup, tagging policy via callback) only with before/after regression proof on both pipelines.
- **Trigger**: opportunistic, next time either swap helper is edited for other reasons; gate on `tests/test_orchestration.py` + `tests/test_normalize_audio.py` staying green plus new shared-helper tests.

### A14. `jq` dependency undocumented / not preflighted (low)

- **File**: `consolidate-status.sh` (uses `jq` for the state-count readout)
- **Issue**: `preflight.sh` + README don't list `jq`, so on a fresh host the status output silently degrades to `?` for the entry count.
- **Fix**: add `jq` as an optional preflight check + README note, OR swap that one call for `python3 -c` JSON (the Python dep is already required).

### A15. `.env` contract not centrally validated (low)

- **Files**: `.env.example`, `README.md`, `nuke_stalled.py`, `preflight.sh`
- **Issue**: env-var rules are scattered and not validated as a unit. E.g. `nuke_stalled.py` treats a non-empty `QBIT_USER` as "auth configured" and only fails later if `QBIT_PASS` is missing; `.env.example` leaves `QBIT_PASS` blank with no `[REQUIRED]` marker while the README lists it as minimum setup.
- **Fix**: a single env-contract validator (used by `preflight.sh` + `healthcheck.sh`) encoding the conditional rules ("if `QBIT_USER` set then `QBIT_PASS` required", path/URL/CIDR/VPN-country shape checks), and align the `.env.example` labels to it.

---

## Closed

One-line markers; the resolution detail is in git history.

- **A1** `godzilla-add-sdh.sh` outlived its purpose: script, cron, log and sentinels deleted.
- **A2** `clean-subs.py` deprecation stub: deleted (v2 logic lives in `consolidate-subs.py`).
- **A3** inline Python in bash heredocs: migrated to `python3 -m media_stack.cli` subcommands.
- **A4** verbose fix-history docstrings: trimmed.
- **A5** status scripts stale vs v2 pipeline: `consolidate-status.sh` reports the real `jq length` entry count; disk-free uses `$MEDIA_ROOT`, not hardcoded `/mnt/disk{1,3}`.
- **A6** README replication walkthrough: added (8 steps).
- **A7** `healthcheck.sh` missed media-policy invariants: perimeter, hardlinks, Bazarr equality and custom-format scores are all machine-enforced via `media_stack/clients/*`.
- **A8** pending Gluetun recreate for compose port-list changes: force-recreated, orphan ports gone.
- **A9** healthcheck failures had no alert path: `ops/healthcheck-alert.sh` wraps `healthcheck.sh`; logrotate for `var/log` is wired (`ops/logrotate.d/media-stack` + install/audit). The operator page is OPT-IN (`HEALTHCHECK_ALERT_NOTIFY`, default off): non-zero results record to `var/log/healthcheck-issues.log` and the self-healing crons + agent handle resolution; Telegram pages only when `HEALTHCHECK_ALERT_NOTIFY=1`.
- **A10** Docker admin ports bind to all interfaces: closed as accepted (trade-off documented; trusted-LAN-only perimeter).
- **A11** live VPN routed through the wrong country: gluetun force-recreated; `healthcheck` uses 2-source ISO-2 consensus + alias normalization, so stale geo no longer false-fails.
- **A12** `healthcheck.sh` extracted into the typed `media_stack/health.py`; `healthcheck.sh` is a thin `exec python3 -m media_stack.health` wrapper, proven byte-identical (plain stdout+stderr, `--verbose`, `--json`, all three exit codes against the same live state). `tests/test_health.py` drives every probe's FAIL/WARN/OK branch with mocked subprocess/HTTP/clients; ruff + mypy clean. Preserved intentionally: qBit no-auth reachability semantics, the credential `.env` whitelist `lib/paths.sh` deliberately omits, and process-env > `.env` precedence with is-set semantics.
