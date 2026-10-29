# tests/: pytest suite

Fast (<3 s, dominated by the multiprocessing flock tests), isolated (no real ffmpeg / mkvmerge / Docker / network) and deterministic (no lingering temp files, no cwd dependence). Behaviors are specified in the root `AGENTS.md`; tests pin them.

```bash
python3 -m pytest tests/ -q                           # all cases must pass on every commit (/review-loop)
python3 -m pytest tests/ --collect-only -q | tail -1  # authoritative count; `--collect-only -q` is the file list
```

Don't hardcode the test count in docs. `ls tests/` is authoritative: `test_<module>.py` covers `media_stack/<module>.py` (`test_media_lang.py` covers `media_stack/lang.py`), `test_<script>.py` covers a root script.

## Conventions for new tests

- **Import the production code under test; don't shell out.** Pure helpers import from the package (`from media_stack.audio import select_keep_audio`). Hyphenated scripts (`consolidate-subs.py`, `normalize-audio.py`, ...) need `importlib.util.spec_from_file_location` only when their orchestration is under test (`test_audio_selection.py` keeps the shim); prefer package imports.
- **Name each test by the BEHAVIOR it verifies** (`test_anime_path_single_jpn`), not the implementation. Use a `_stream(...)` helper for synthetic stream dicts (`test_audio_selection.py`).
- **Mock at the module boundary:** `subprocess.run` / `Popen`, `requests.*`, `faster_whisper`, ADB/Telegram/Jellyfin. Never run real ffmpeg/mkvmerge, never touch the network.
- **Scripted fakes consume sequences LAZILY.** A `list()` over `itertools.repeat` once ate 29 GB and OOM-killed the host. Run anything heavy under a memory cap (`systemd-run --user --scope -p MemoryMax=4G -p MemorySwapMax=0 ...`).
- **No test may create the real `var/` tree or write outside `tmp_path`.** Tests that load `consolidate-subs.py` stub the import-time `ensure_var_dirs()` first and monkeypatch every `var/` path (`VAR_QUARANTINE_SUBTITLES`, `QUARANTINE_LOG`, `NEEDS_ENGLISH_STATE_FILE`, `LOG_FILE`, `STATE_FILE`) to `tmp_path`; `probe` is stubbed to fail unless a test sets it. `test_audio_lang_check.py` has an autouse fixture mocking `ensure_var_dirs`.
- **Cumulative-budget and playback tests use a `_FakeClock`** (`sleep` advances it), not a hand-counted `time.monotonic()` sequence.
- Ordering-sensitive guarantees are pinned with call-order spies (the quarantine fsync chain runs leaf to root before the unlink; `os.link` publishes complete content), and source audits pin structural rules (see below).

## Non-obvious suites

- `test_consolidate_generated.py`: source audit pins `_workfile` use (no `workdir / <name>`, no `.with_suffix(".srt")` conversion outputs) with adversarial stems. `test_consolidate_watch.py` runs the REAL sidecar-arrival block of `consolidate-watch.sh` under bash (`log`/`trigger` stubbed) plus a parametrized cross-check against the real `find_sidecar_subs`.
- `test_sub_quarantine.py`: content gate and quarantine through the real `_process_locked`; an AST audit pins that every `_sidecar_as_srt` call passes decoded text.
- `test_consolidate_cleanup.py`: the NEEDS_BAZARR cleanup from `_process_locked`'s call sites (principle: never delete a track that might be wanted, never silently change a flag that wasn't meant to change; `_normalize_audio_inline` is NEVER called).
- `test_consolidate_upgrade.py`: the generated -> real upgrade through the real `_process_locked` (ffprobe, window reads, mkvmerge, ffsubsync, tag writers and normalize faked; sidecar discovery, `sublang`, `overlap` math, ranking and the state file REAL). By default the harness neutralizes the late sweep (`Harness(handoff=True)` opts in).
- `test_mux.py`: the full-path `remux()` argv is pinned byte-for-byte; cleanup mode emits explicit `--default-track`/`--forced-track` for every kept track.
- `test_media_lang.py`: regional/script variants (`en-US`, `ja-JP`) collapse to the base family; a bare `-US`/`und-US` is NOT guessed English; `is_english_tag()` treats untagged/`und` as NOT English.
- `test_fileid.py`: a same-size in-place rewrite with the mtime restored changes ONLY ctime; `identity_matches` compares four-field records exactly, a legacy three-field record per `allow_legacy`, and never matches malformed records.
- `test_sub_fallback.py` (mocks `media_stack.subtitles` / `subverify`) and `test_audio_lang_check.py` (its `world` fixture mocks probe/whisper/Jellyfin/Arr/Telegram as named handles and runs `main()` in one environment, all paths in `tmp_path`) carry the state-machine, fail-closed-gate and validation cases; the cron entry for the latter must stay hourly, nice/ionice, with a minute no other crontab entry uses.
- `test_unaired_guard.py` and `test_search_missing.py` are weighted toward FALSE POSITIVES (a wrong reap deletes and blocklists a legitimate download; a wrong movie pick grabs pre-release junk). Radarr's `MissingMoviesSearch` is never used.
- `test_shield_check.py`: adb is a fake runner injected via `main(adb=...)` that emulates only `connect`, `devices` and the read-only shell statements and raises on anything else (plus an audit of every recorded call); Telegram is a recording stub. It also pins the real `ops/shield/profile.json` (valid; no IPv4, MAC or `@`) and that new files hold only RFC 5737 addresses (the repo is public).
- `test_jellyfin_mint_api_key.py`: pins the exit-code contract (1 pre-flight network/parse fail; 2 mint failed or unverifiable; 3 post-flight verify fail / admin policy disruption; 4 bad usage / duplicate-name refusal), `_redact`, no token in error messages, and a single stdout token print.
- `test_env_parser_no_eval.sh` + `.py`: `lib/paths.sh` must not eval values; the `.sh` holds the assertions and the `.py` is a thin wrapper so the canonical pytest command picks it up.
