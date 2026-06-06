# Code Audit

Tracker for content-level cleanups (smaller scope than structural
refactors).  Items here should be addressed opportunistically as each
touched file comes up for other work.

Closed items move to the bottom log so this file stays scannable.

---

## Open

### A13. Shared safe-replacement helper for the pipeline swap logic (deferred)

- **Files**: `consolidate-subs.py`, `normalize-audio.py`
- **Issue**: both entrypoints duplicate the high-risk file-swap choreography
  — destination locking, sibling `.mkv` collision check, `os.replace`,
  idempotency tagging, rollback, cleanup.  The duplication also sits outside
  `mypy` (which only covers `media_stack/`).
- **Deferred deliberately**: same risk profile as the (now-closed) A12 — this
  is the lossy-replacement core; a wrong extraction can silently corrupt media
  on the next sweep.  Extract a typed `media_stack/replacement.py` (lock →
  collision → swap → rollback → cleanup, with tagging policy via callback) only
  with before/after regression proof on both pipelines.
- **Trigger**: opportunistic, next time either swap helper is edited for other
  reasons; gate on the existing `tests/test_orchestration.py` +
  `tests/test_normalize_audio.py` staying green plus new shared-helper tests.

### A14. `jq` dependency undocumented / not preflighted (low)

- **File**: `consolidate-status.sh` (uses `jq` for the state-count readout)
- **Issue**: `preflight.sh` + README don't list `jq`, so on a fresh host the
  status output silently degrades to `?` for the entry count.
- **Fix**: add `jq` as an optional preflight check + README note, OR swap that
  one `jq` call for `python3 -c` JSON (the Python dep is already required).

### A15. `.env` contract not centrally validated (low)

- **Files**: `.env.example`, `README.md`, `nuke_stalled.py`, `preflight.sh`
- **Issue**: env-var rules are scattered + not validated as a unit.  e.g.
  `nuke_stalled.py` treats a non-empty `QBIT_USER` as "auth configured" and
  only fails later if `QBIT_PASS` is missing; `.env.example` leaves `QBIT_PASS`
  blank with no `[REQUIRED]` marker while README lists it as minimum setup.
- **Fix**: a single env-contract validator (used by `preflight.sh` +
  `healthcheck.sh`) encoding conditional rules ("if `QBIT_USER` set then
  `QBIT_PASS` required", path/URL/CIDR/VPN-country shape checks), and align the
  `.env.example` labels to it.  Surfaced repeatedly by Codex holistic reviews.

---

## Closed log

Brief markers only — see git history for the full resolution detail.

- **A1** godzilla-add-sdh.sh outlives its purpose → CLOSED 2026-05-23 (script + cron + log + sentinels deleted with task #48)
- **A2** clean-subs.py deprecation stub → CLOSED 2026-06-01 (deleted in user-requested debloat pass)
- **A3** inline-Python-in-bash heredocs → CLOSED 2026-05-19 (migrated to `python3 -m media_stack.cli` subcommands)
- **A4** verbose fix-history docstrings → CLOSED 2026-06-01 (trimmed the multi-line history blocks in `locking.py`, `arr.get_queue`, `paths.ensure_var_dirs`, `consolidate-subs.py` lock/inherit comments; remaining one-line `(codex round-N)` provenance citations kept on purpose — they aid traceability + match the test-docstring convention)
- **A5** status scripts stale vs v2 pipeline → CLOSED 2026-06-01 (`normalize-status.sh` no longer serial-ffprobes the whole library — it reads the driver's `coverage:` line + `.done` sentinel, dropping the stale South Park special-case + wrong exts; `consolidate-status.sh` reports real `jq length` entry count not `wc -l`; disk-free uses `$MEDIA_ROOT` not hardcoded `/mnt/disk{1,3}`)
- **A6** README replication walkthrough → CLOSED 2026-05-19 (8-step section added)
- **A7** healthcheck.sh misses media-policy invariants → CLOSED across rounds 6+9+13+15 (perimeter + hardlinks + Bazarr equality + custom-format scores all machine-enforced via `media_stack/clients/*`)
- **A8** pending Gluetun recreate for compose port-list changes → CLOSED 2026-05-23 (force-recreated; orphan ports gone)
- **A9** healthcheck failures have no alert path → CLOSED 2026-05-19 (`ops/healthcheck-alert.sh` wraps + Telegrams on non-zero); logrotate piece CLOSED 2026-06-01 (`ops/logrotate.d/media-stack` + install/audit wiring). **Operator-page made OPT-IN 2026-06-08** (`HEALTHCHECK_ALERT_NOTIFY`, default off) per operator request "don't text me with healthcheck issues; just resolve them yourself" — non-zero now records to `var/log/healthcheck-issues.log` and the self-healing crons + agent handle resolution; Telegram page only when `HEALTHCHECK_ALERT_NOTIFY=1`.
- **A10** Docker admin ports bind to all interfaces → CLOSED-as-accepted 2026-05-19 (trade-off documented; trusted-LAN-only perimeter)
- **A11** Live VPN routes through UK → CLOSED 2026-05-23 (force-recreated gluetun; healthcheck now uses 2-source ISO-2 consensus + alias normalization, no longer false-fails on stale geo)
- **A12** healthcheck.sh full extraction into a typed module → CLOSED 2026-06-01 (extracted to `media_stack/health.py`; `healthcheck.sh` is now a thin `exec python3 -m media_stack.health` wrapper.  Proven byte-identical: shell-vs-Python diff matched plain stdout+stderr, `--verbose`, `--json`, and all three exit codes against the same live state; `tests/test_health.py` drives every probe's FAIL/WARN/OK branch with mocked subprocess/HTTP/clients; ruff + mypy clean — the module is now type-checked, closing the holistic finding about orchestration outside mypy coverage.  Intentional behaviors preserved: qBit no-auth reachability semantics, the credential `.env` whitelist `lib/paths.sh` deliberately omits, and process-env>.env precedence with is-set semantics.)

---

## How to use this file

- Items tagged `A<n>` so each has a stable handle to reference.
- Add new items here when you spot slop during other work — cheaper
  than fixing immediately, and the trigger context keeps it pragmatic.
- Close items inline: move the entry from "Open" to "Closed log" with
  a one-line marker.  Full history is in git.
