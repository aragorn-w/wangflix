# media_stack/: pipeline helper package

Pure helpers, unit-testable without subprocess shims; invariants: root `AGENTS.md`. `ls media_stack/` is the module list; non-obvious contracts:

- `paths.py`: host identity and `var/` paths. `ensure_var_dirs()` creates log/run/state/reviews only and is called by writer scripts BEFORE their first write; the quarantine dir is created lazily by `consolidate-subs.py` (an import-time `SystemExit` for an optional dir must not stop the pipeline).
- `lang.py`: `canonical_lang()` normalizes language families (regional/script variants); `is_english_tag()` is stricter (untagged/`und` is not English).
- `probe.py`: `parse_duration()` is finite, positive, never a bool; `audio_stream_duration()` reads audio streams only (callers pass non-commentary indices: a cover-art video or commentary track is never a duration source); `probe_duration()` is the bounded 30 s fallback.
- `audio.py`: `get_audio_lang_pref`/`select_keep_audio` serve the FULL path (can drop tracks; FAMILY-based). `confident_default_audio()` is the delete-free sibling for the cleanup path: the stream that should be `default`, or `None` to leave every flag untouched.
- `arrlang.py`: never raises (`None` on failure). `original_language_lookup()` returns `(code, "known" | "unknown" | "unavailable")`; callers that cache a per-file result must not cache one computed under `unavailable`.
- `subtitles.py`: `is_sdh_track()`/`is_hi_text()` match whole tokens; `extract_track()` writes ASS/SSA natively as `.ass`; `decode_subtitle_bytes()` never raises, `decode_subtitle_text()` returns `None` when the result isn't text (what the content gate uses).
- `sublang.py`: an unlisted language comes back `UNKNOWN` or the best-matching listed language, never `eng`; `classify_candidate(text)` is the pure content-gate rule (NON-LATIN override needs >= 3 English hits).
- `mux.py`: full-path (`cleanup_flags=None`) argv is pinned byte-for-byte. Cleanup mode `cleanup_flags={track_id: {"default": bool, "forced": bool}}` emits an EXPLICIT `--default-track`/`--forced-track` per kept track by source track ID, never by reordering `audio_indices`/`video_indices` (selectors) and never by omission. `kept_source_subs` / `keep_subtitle_indices` stream-copy source subtitle tracks ahead of the external srt .
- `loudness.py`: size-scaled budgets (`pass1_timeout_s`, `pass2_timeout_s`) and `normalize_budget_s()` (their sum plus `mux.remux_timeout_s` and a margin), the outer cap for the inline normalize run.
- `playback.py`: the fail-closed Jellyfin gate shared by `sub-fallback.py` (thin `Config`-binding adapters) and `audio-lang-check.py`: `PlaybackWaitBudget` (ONE per candidate, cumulative), `wait_while_playing`, `call_with_playback_pause` (wires `should_abort`, retries after `ExtractionInterrupted`), `PlaybackWaitTimeout`.
- `envconfig.py` (finite/positive/BOUNDED env parsing), `fileid.py`, `locking.py`, `state.py` (flock-protected `mutate_state`), `sweeps.py`, `dedupe.py`, `vpn_country.py`, `health.py` (behind `healthcheck.sh`; first probe checks the mergerfs pool and each branch are mounted and non-empty; shells out to `docker`/`curl`/`ip`/`systemctl`/`ops/audit.sh`, service APIs via `clients/`), `clients/` (see `clients/AGENTS.md`).

## subverify.py

- `english_audio_stream()` excludes commentary tracks first (`audio.is_commentary_audio`, no second keyword list), then tagged-English preferring `default`, else untagged/`und`, else `None`; it never guesses a foreign or commentary stream is English.
- `detect_language()` needs a MULTILINGUAL model, never `.en`. `asr_window()`/`detect_language()` REQUIRE an explicit `workdir` forwarded to `_extract_wav`: the wav must land in the caller's per-candidate local workdir, never the system temp dir. `transcribe_to_srt()` has none: it runs only in the CLI-mode subprocess, whose `TMPDIR` the parent points at that workdir.
- `overlap()` returns `(word_ratio, bigram_ratio, asr_word_count)`; `MIN_WORD_RATIO`/`MIN_BIGRAM_RATIO`/`MIN_ASR_WORDS` are shared with `sub-fallback.py`. `judge_sidecar_against_windows` returns `(ok, reason, usable, evidence)`; only `evidence >= 2` yields a wrong-episode verdict. Window reads (`ffprobe -read_intervals start%end`, absolute end on purpose) are half-open `[start, end)`; any failure is `None`, never a partial read.
- Every extraction function takes an optional `should_abort`: `_extract_wav` checks it BEFORE spawning ffmpeg, then polls every 5 s, raising `ExtractionInterrupted` (ffmpeg terminated, partial wav deleted). `faster_whisper` is imported LAZILY, so importing the module never requires it.
- CLI mode `python3 -m media_stack.subverify transcribe <video> <audio_index> <model> <threads>` builds its OWN Jellyfin gate from env (`JELLYFIN_URL`, `JELLYFIN_API_KEY`, `SUB_FALLBACK_MAX_WAIT_MIN`, validated like the parent's `env_float`). A missing/empty key exits 3 BEFORE constructing a `JellyfinClient` or touching ffmpeg. The wait report file is written on every exit path; a missing `TMPDIR` is a no-op, never a crash.

## Rules

- **No circular imports:** the arrow points outward from `config`/`paths`; helpers must not import from the CLI wrappers (top-level scripts may import anything).
- **No I/O at module top level, no fs writes on import** (only `paths.py`'s bounded read-only `.env` read): read-only importers never mutate the filesystem.
- **Allow-listed binaries only:** `ffmpeg`, `ffprobe`, `mkvmerge`, `mkvpropedit`, `mkvextract`, `ffsubsync`, `subliminal`; a new one first needs a `preflight.sh` presence check. **No `shell=True`** (argv lists). **Timeouts on every external call** (loudness steps scale with file size). **Network only in `clients/`** (exception: `health.py`).
- **Orchestration glue stays in the CLI scripts** (`_process_locked`, `_process_file_inner_locked`, argparse/logging, `--scan`): one caller each; extract `media_stack/consolidate.py` / `normalize.py` only when a second caller appears.
- **Don't break the lock-inheritance protocol:** `acquire_file_lock(media, inherit_from=Path(...))` verifies the RESOLVED ABSOLUTE path (basename-only is unsafe: unrelated files sharing a basename must NOT bypass). `normalize-audio.py` re-enters under `NORMALIZE_INHERIT_LOCK_PATH` from consolidate's inline call; changing its name or the comparison MUST update both call sites and `tests/test_locking.py`. The lock file `.consolidate-<name>.lock` MUST be on the media file's filesystem.
- **Don't conflate language families:** `canonical_lang()` collapses `eng/en/english` -> `eng`; extending the alias set needs a test case.
- **mkvpropedit is Matroska-only:** any tag-only shortcut MUST gate on `path.suffix.lower() == ".mkv"` first (MP4 gave a permanent `TAG_FAIL`).
- **`state.py` upserts take a flock:** never bypass with a direct `json.dump` over the state path (concurrent normalizers silently lose updates).
- **Tests:** every pure helper has `tests/test_<module>.py`; never shell out to real `ffmpeg`/`mkvmerge` (`tests/AGENTS.md`).
