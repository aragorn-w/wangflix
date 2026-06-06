# tests/ — pytest suite

Unit tests for the pure helpers in the media stack.  Designed to stay
fast (<3s total, dominated by the multiprocessing-backed flock tests),
isolated (no real ffmpeg / mkvmerge / Docker / network), and
deterministic (no temp file lingering, no cwd dependence).

## Layout

Authoritative test list comes from `python3 -m pytest tests/ --collect-only -q`.
The groups below are a fast-lookup map for "where do tests for X live?":

- `test_media_lang.py` — `canonical_lang()` family aliases + CLI mode
- `test_audio_selection.py` — `get_audio_lang_pref()` / `select_keep_audio()`
  multi_keep gating, dedup, primary-first ordering; regressions for
  every codex audio-selection finding (rounds 1-4)
- `test_subtitles.py` — sub scoring + cleaning + recasing, sidecar
  discovery (bare + .en + .en.sdh + .en.forced + foreign-excluded),
  classification (forced/SDH/regular)
- `test_probe.py` — `file_key`, `already_processed`/`already_normalized`,
  `primary_audio_stream`, `probe` failure modes (timeout, non-zero rc)
- `test_state.py` — flock-protected RMW upserts, 4-worker
  multiprocessing lost-update regression (round-1 lost-update bug)
- `test_locking.py` — `acquire_file_lock` happy path + competing-holder
  skip + inherit-bypass + same-basename/different-dir REJECTION (round-4
  #2) + relative-vs-absolute same-file BYPASS (round-9 #3 sharpened so
  the test catches the bug, not just exercises it)
- `test_sweeps.py` — orphan `.tmp.mkv` + workdir cleanup
- `test_clients.py` — `ArrClient` / `BazarrClient` / `QBitClient` /
  `JellyfinClient` / Telegram adapter.  Includes pagination regressions
  (round-2 short-page miss + round-7 totalRecords-vs-short-page edge),
  tri-state remove_by_download_id, hardlinks tri-state (round-9 + round-11
  #5 field-missing-is-None defense), Telegram URL/argv hygiene,
  JellyfinClient request/list_keys-shape-validation/create_key (the
  jellyfin-mint script's own end-to-end tests live in
  `test_jellyfin_mint_api_key.py`)
- `test_paths.py` — `load_env_file` (quoting, first-`=`-only, missing
  file, no-trailing-newline); `ensure_var_dirs` fail-fast (round-6 #4)
- `test_cli.py` — subcommand dispatch (canonicalize, expand,
  has_eng_sdh, dual_audio, telegram_send) with mocked ffprobe + adapter
- `test_orchestration.py` — mock-based coverage of `_process_locked`'s
  7 early-return branches (round-5 #4 mitigation)
- `test_env_parser_no_eval.sh` + `test_env_parser_no_eval.py` —
  shell regression for round-10 #1 (lib/paths.sh must not eval
  values).  The .sh holds the actual assertion logic; the .py is
  a thin subprocess wrapper so `python3 -m pytest tests/ -q`
  picks it up (codex round-12 #1: the canonical test command
  would otherwise miss the shell-only file).
- `test_jellyfin_mint_api_key.py` — safety-verified API key
  minting helper.  Network-error handling, bare-list-vs-Items
  shape, AccessToken validation, _redact short-token leak
  prevention, error-message-doesn't-leak-tokens, every documented
  exit code (1 = pre-flight network/parse fail; 2 = mint failed
  or unverifiable; 3 = post-flight verify fail / admin policy
  disruption; 4 = bad usage / duplicate-name refusal), and the
  happy-path single-stdout-token-print invariant.
- `test_vpn_country.py` — VPN consensus + ISO-2/long-name
  normalization helper (round-cleanup-3 #1+#4; alias-equivalent
  provider consensus added in round-cleanup-4 #6).

## Run them

```bash
python3 -m pytest tests/ -q
```

All cases must pass on every commit per the `/review-loop` mandate.
Don't hardcode the count in docs — `pytest --collect-only -q | tail -1`
is the authoritative number.

## Conventions for new tests

- **Import the production code under test, don't shell out.**  As of
  DEFERRED #1 (landed 2026-05-19), pure helpers live in the
  `media_stack/` package and are importable directly:
  `from media_stack.audio import select_keep_audio`.  The hyphenated
  entrypoint scripts (`consolidate-subs.py`, `normalize-audio.py`)
  still need `importlib.util.spec_from_file_location` if you must
  test their orchestration directly — `test_audio_selection.py` keeps
  the shim around for that, but most new tests should import via the
  package.
- **Use a `_stream(...)` helper** when you need synthetic audio stream
  dicts.  See `test_audio_selection.py:23` — keeps test bodies focused on
  the assertion, not on JSON construction.
- **Name each test by the BEHAVIOR it verifies**, not the implementation
  detail.  `test_anime_path_single_jpn` is good; `test_get_audio_lang_pref`
  is too generic.
- **Regression tests for codex findings** must link to the round and
  finding number in the docstring (e.g. "codex round-3 #4 alias
  handling"); makes it easier to map test → why-it-exists.
