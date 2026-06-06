# media_stack/ — pipeline helper package

Importable Python package extracted from the monolithic CLI entrypoints
(`consolidate-subs.py`, `normalize-audio.py`) during DEFERRED #1.  The
package owns the **pure, reusable helpers** (probe, lang, audio, sub,
mux, loudness, tags, state, locking, sweeps, service clients) so they
can be unit-tested without subprocess shims and consumed from multiple
call sites.

What stays in the CLI scripts (NOT in this package):

- argparse + logging setup
- the per-file orchestration glue (`_process_locked` in
  `consolidate-subs.py`, `_process_file_inner_locked` in
  `normalize-audio.py`) — these tie together state file, lock
  acquisition, telemetry, error reporting, CLI flags
- the `walk` / `--scan` dispatch loop

That glue is deliberately not extracted because there is currently
only one caller for each.  Moving it into the package would create a
new file purely to satisfy an architectural claim — see the system
guidance against speculative abstractions.  If a second caller
appears (webhook trigger, alt scanner mode, programmatic API), at
that point extract `media_stack/consolidate.py` /
`media_stack/normalize.py` with a clean entry function and keep the
CLI scripts as `argparse + dispatch` only.

## Module map

| Module | Responsibility | Side-effects allowed |
| :--- | :--- | :--- |
| `config.py` | Constants — `PIPELINE_VERSION`, codec prefs, regex, keyword sets. | none (pure data) |
| `paths.py` | Host identity + `var/` paths.  Loads `.env` at import time.  Exposes `ensure_var_dirs()` for writer scripts to call BEFORE first write. | reads `.env` once; **import itself is read-only** — fs writes only when a writer calls `ensure_var_dirs()` |
| `lang.py` | `canonical_lang()` family normalizer.  Also exposes a CLI mode (`python3 -m media_stack.lang ...`). | argv parsing only |
| `probe.py` | ffprobe wrappers, idempotency-tag checks, primary-stream picker. | spawns `ffprobe` |
| `tags.py` | mkvpropedit-driven global tag writes (`CONSOLIDATED_SUBS`, `NORMALIZED_AUDIO`). | spawns `mkvpropedit`/`mkvextract` |
| `state.py` | flock-protected `state.json` upserts (`load_state`/`update_state_entry`/`save_state`). | reads/writes the state JSON; takes fcntl flock |
| `locking.py` | `acquire_file_lock(media, inherit_from=None)` — per-file cross-pipeline mutex. | creates/locks `.consolidate-<name>.lock` next to the media file (MUST be same filesystem) |
| `audio.py` | Track-selection policy: language preference, commentary/dub detection, multi-keep gating. | none (pure on dicts) |
| `subtitles.py` | Sub scoring, cleaning, classification, sidecar discovery, subliminal fallback. | spawns `ffmpeg`/`ffsubsync`/`subliminal` when called; pure scoring otherwise |
| `mux.py` | mkvmerge orchestration (`remux`, `mkvmerge_remux_simple`). | spawns `mkvmerge` |
| `loudness.py` | Two-pass EBU R128 ffmpeg loudnorm + Atmos 7.1.2 downmix. | spawns `ffmpeg` |
| `sweeps.py` | Orphan `.tmp.mkv` / workdir cleanup. | fs scan + unlink |
| `dedupe.py` | Pure keeper-selection for duplicate movie files (resolution → source → processed-tag → size; corrupt-demotion).  Used by `movie-dedupe.py`. | none (pure on dicts) |
| `health.py` | Aggregate health monitor (containers/VPN/API/policy/perimeter/drift probes) with exit codes; `healthcheck.sh` is a thin wrapper.  Byte-identical port of the former shell script (AUDIT A12). | spawns `docker`/`curl`/`ip`/`systemctl`/`find`/`pgrep`/`ops/audit.sh`; network via `clients/` |
| `cli.py` | Shared CLI dispatcher for shell scripts: `python3 -m media_stack.cli <subcommand>`. | argv parsing; subcommand-dependent |
| `clients/` | External service adapters — see `clients/AGENTS.md`. | network |

## Import expectations

- **No circular imports.**  The dependency arrow points outward from
  `config`/`paths` toward higher-level modules; `cli.py` and the
  top-level CLI scripts (`consolidate-subs.py`, `normalize-audio.py`)
  may import from anywhere.  Conversely, helpers must not import
  from the CLI wrappers.
- **`paths.py` parses `.env` at import time but performs NO fs
  writes.**  The exposed `ensure_var_dirs()` helper is the single
  point that creates `var/log`, `var/run`, `var/state`,
  `var/reviews`; writer scripts (entry points that open log files,
  state JSON, sentinels) call it before their first write.  Read-only
  importers (review tools, dry-run scanners, tests) never trigger
  any filesystem mutation by importing the module (codex round-4
  module-split #7).
- **No I/O at module top level.**  Helpers compute values on call,
  not at import.  `paths.py`'s `.env` read is the only at-import
  side effect, and it is bounded + read-only.

## Subprocess + network rules

- **Allow-listed binaries only:** `ffmpeg`, `ffprobe`, `mkvmerge`,
  `mkvpropedit`, `mkvextract`, `ffsubsync`, `subliminal`.  Any new
  helper that needs a different binary first goes in `preflight.sh`
  (presence check) before being invoked from `media_stack/`.
- **No shell=True.**  Always pass argv as a list to `subprocess.run`
  so filenames with whitespace / metacharacters don't escape.
- **Timeouts on every external call.**  No unbounded `subprocess.run`
  — pick a generous but finite timeout (loudness pass2 timeouts are
  why `normalize-driver.sh` exists in the first place).
- **Network only in `clients/`.**  No other module in `media_stack/`
  should be calling `requests.*` or opening sockets.  **Exception:
  `health.py`** is the aggregate monitor and deliberately shells out to
  infrastructure tools for probing — `docker exec … wget` (VPN egress IP),
  host `curl` (leak check), `ip`/`systemctl`/`find`/`pgrep`,
  `ops/audit.sh`.  Its *service-API* reachability still goes through
  `clients/` (`ArrClient`/`BazarrClient`/`QBitClient.reachable_status`);
  only the host/infra probes are subprocess-based, because they're not
  service clients and there's nothing to centralize.

## Testing expectations

- Every pure helper has a unit test under `tests/test_<module>.py`.
- Tests must NOT shell out to real `ffmpeg`/`mkvmerge` — mock with
  `unittest.mock.patch` on `subprocess.run` / `requests.get`.
- New regression tests for codex findings name the round + finding
  in the docstring (`"codex round-3 #N — ..."`).  Makes it easy to
  trace a test back to why it exists.
- The full suite must stay under ~1s wall clock — no real disk I/O
  beyond `tmp_path`, no real network.

## Common pitfalls when editing

- **Don't break the lock-inheritance protocol.**  `acquire_file_lock`
  accepts `inherit_from=Path(...)` with resolved-absolute-path
  verification (basename-only was unsafe — codex round-4 module-
  split #2; two unrelated files sharing a basename in different
  directories must NOT bypass).  Used by `normalize-audio.py` to
  re-enter under `NORMALIZE_INHERIT_LOCK_PATH` from consolidate's
  inline call.  Any change to the env-var name or comparison
  semantics MUST update both call sites and `tests/test_locking.py`.
- **Don't conflate language families** — `canonical_lang()` collapses
  `eng/en/english` → `eng`, but extending the alias set without a
  test case is a regression magnet.
- **mkvpropedit is Matroska-only.**  Any tag-only shortcut MUST gate
  on `path.suffix.lower() == ".mkv"` first (codex caught us doing
  this for MP4 once; permanent TAG_FAIL).
- **`state.py` upserts take a flock** — do not bypass with a direct
  `json.dump` over the state path.  Concurrent normalizers will
  silently lose updates (codex round-1 lost-update bug, now covered
  by `tests/test_state.py`).
