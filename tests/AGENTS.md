# tests/ — pytest suite

Unit tests for the pure helpers in the media stack.  Designed to stay
fast (<3s total, dominated by the multiprocessing-backed flock tests),
isolated (no real ffmpeg / mkvmerge / Docker / network), and
deterministic (no temp file lingering, no cwd dependence).

## Layout

Authoritative test list comes from `python3 -m pytest tests/ --collect-only -q`.
The groups below are a fast-lookup map for "where do tests for X live?":

- `test_media_lang.py` — `canonical_lang()` family aliases + CLI mode;
  regional/script variants (`en-US`, `eng-GB`, `ja-JP`, `ko-KR`, ...)
  collapsing to their base family, and NOT guessing English from a bare
  `-US`/`und-US` with no real base subtag; `is_english_tag()` (same
  regional recognition, but untagged/`und` is NOT English — the
  stricter subtitle-side rule)
- `test_audio_selection.py` — `get_audio_lang_pref()` / `select_keep_audio()`
  multi_keep gating, dedup, primary-first ordering; regressions for
  every codex audio-selection finding (rounds 1-4); the `original_language=`
  kwarg (DEFECT 2 fix — Radarr/Sonarr signal beats the Matroska `default`
  disposition, which release groups routinely set on a dub; `None` falls
  back to the old disposition rule, sharpened so `default` alone can
  neither promote a dub to primary NOR cause the foreign track to be
  DROPPED — both are kept, eng primary/default, `multi_keep=True` —
  including a simulated Arr-fetch-failure case on a Japanese-original-
  style file; and codex round 2's ordering fix — `original` is checked
  for BOTH jpn and kor before either's `default` is considered, so a
  jpn `default` can never outrank a kor `original`); codex round 3 #1 —
  `select_keep_audio`'s in-spec/scoring test is FAMILY-based
  (`canonical_lang`), not a literal raw-string match: `eng + ja-JP` with
  `original_language="jpn"` keeps BOTH (a literal match would wrongly
  keep only eng), and `en-US + jpn` on an anime-keyword path still
  behaves exactly as before; `confident_default_audio()` — keyword/
  known-original_language/original-disposition confidence sources
  (never `default`, never a guess), explicit-tag requirement (regional
  variants count, untagged/`und` doesn't), commentary exclusion,
  best-scored-candidate selection, and the Arr-outage-toggle
  thrash-avoidance case (confident "jpn" -> unconfident "default-only"
  None -> confident "jpn" again, never alternating)
- `test_arrlang.py` — `original_language_for()`: movie/series path →
  folder → `originalLanguage` mapping (mocked `ArrClient`, no real
  network), None on client failure/missing key/no match/non-media path/
  unmapped language name (anything other than English/Japanese/Korean —
  the code never invents a non-ISO-639-2 code), per-process cache (one
  fetch per kind); codex round 5 — the root check: a same-basename
  record under an UNRELATED root (not `ARR_MOVIES_ROOT`/`ARR_TV_ROOT`,
  default `/movies`/`/tv`) is ignored entirely (None, safe fallback); a
  record under the configured/default root is used, trailing slashes
  on either side included; two ACCEPTED records sharing a basename are
  AMBIGUOUS -> None, without shadowing an unrelated, unambiguous
  basename in the same fetch; a record rejected for the wrong root
  does NOT itself trigger false ambiguity against an accepted one; the
  root override works via process env AND via `.env` (same precedence
  as the API keys, process-env still winning over a conflicting `.env`
  entry), for both Radarr and Sonarr
- `test_mux.py` — `media_stack.mux.remux()` mkvmerge argv, mocked
  `subprocess.run`: the full consolidate-subs path (external English
  srt + optional forced srt, `cleanup_flags` omitted) is pinned
  byte-for-byte across every cleanup-mode addition; the `eng_srt=None`
  cleanup mode (`--subtitle-tracks` for `keep_subtitle_indices`, or
  `--no-subtitles` when none given, `forced_srt` ignored); codex round
  4 #2 — `cleanup_flags={track_id: {"default": bool, "forced": bool}}`
  emits an EXPLICIT `--default-track` for every kept video/audio/
  subtitle id and an explicit `--forced-track` for every kept subtitle
  id, by source track ID: a source with no default flags anywhere
  produces explicit `default=no` for every one of its tracks (mkvmerge
  auto-picks a default per type when the source has none at all for
  it, so omitting the override would NOT mean "unchanged"); a confident
  audio pick changes ONLY the audio defaults (video/subtitle flags
  mirror source exactly); subtitle forced flags are re-emitted as in
  the source independent of the audio/video decisions; a kept id
  missing from the map defaults conservatively to `no`/`no`
- `test_consolidate_cleanup.py` — the DEFECT 1 fix: the AUDIO-AND-
  SUBTITLE CLEANUP REMUX run from `_process_locked`'s FOUR "no main
  English subtitle yet" call sites (`_cleanup_no_main_english`) —
  NEEDS_BAZARR "no English candidates", FAIL "only forced English",
  NEEDS_BAZARR "only image subs (subliminal fetch failed)", and FAIL
  "all extracts failed" (status unchanged in every case) — exercised
  through the real gate wherever possible. Governing principle under
  test throughout (codex rounds 2-4): never delete a track that might
  be wanted, never silently change a flag that wasn't meant to change.
  - **Video (round 4 #1, tightened from round 3):** NOT touched at
    all — a cover-art mjpeg stream survives right alongside real video,
    and its mere presence (nothing else wrong) does NOT trigger a
    remux (contrast with round 3, where dropping cover art alone used
    to count as "something to clean").
  - **Audio:** NEVER dropped and NEVER physically reordered — a
    confidently-decided change only sets the `default` flag by source
    track ID (via `cleanup_flags`), list order untouched, including
    when the confident pick is physically second or nothing is flagged
    default at all; the narrow "already correct" guard (pick already
    the SOLE default-flagged stream) skips the remux entirely; no
    confidence signal at all (Arr down, no keyword, no disposition)
    leaves every audio track's OWN source default flag preserved
    exactly (round 4 #2 — re-verified per-track, not just "untouched"
    in aggregate), including a French-original/Spanish-dub file with
    no English track to anchor on.
  - **Subtitles:** only POSITIVELY foreign tags are dropped under the
    ASCII-only rule (round 4 #3) — `mul`/`und-Latn`/`zxx`/`qaa`
    (special/private-use codes), untagged/unrecognized shapes, AND
    non-ASCII "letters" like an accented `éé` or `日本語` all survive;
    `fre`/`spa`/`jpn`/`fr-CA` (well-formed ASCII non-English codes) are
    dropped; regional English (`en-US`) is recognized as a candidate
    upstream and never reaches the foreign-check at all; every kept
    subtitle's forced flag is re-emitted from its own source value.
  - **Normalization:** `_normalize_audio_inline` is NEVER called from
    this path (codex round 3 #4), even on a successful swap.
  - No `CONSOLIDATED_SUBS` tag write; an already-clean file skips the
    remux entirely (idempotency); `--dry-run` reports without mutating;
    English image-subtitle tracks are never dropped; the post-remux
    verify step (round 4 #4, strengthened) checks BOTH track counts
    (audio/video expected = the ORIGINAL counts, since neither is ever
    dropped) AND the per-track default-flag PATTERN for audio and
    video — either kind of mismatch deletes the tmp output and FAILs
    without touching the original; a full match lets the swap proceed,
    still without normalizing.
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
  7 early-return branches (round-5 #4 mitigation); the "no main English
  subtitle yet" branches also cover the DEFECT 1 cleanup-remux trigger
  vs. the already-clean no-op (see `test_consolidate_cleanup.py` for the
  dedicated coverage of that mechanism)
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
- `test_unaired_guard.py` — `unaired-guard.py`, the reaper for Sonarr
  queue items whose episode has not aired yet (fake releases for
  unaired episodes are a malware vector).  Weighted toward FALSE
  POSITIVES rather than coverage of the happy path, because a wrong
  reap deletes and blocklists a legitimate in-progress download:
  unknown/TBA/malformed air dates, episodes missing from the series
  fetch, the grace window in both directions, mixed vs fully-unaired
  season packs, pending records with no `downloadId` (codex round-1
  #1), bool-vs-int `episodeId` (#3), invalid grace falling back to the
  default rather than to zero (#4), already-gone rows on a pack reap
  (#5), and blind-vs-clean exit codes (#2).
- `test_search_missing.py` — `search-missing.py`, the weekly missing-item
  search.  Weighted toward the movie PICK rather than the posting, because a
  wrong pick grabs pre-release junk for a film still in cinemas: unreleased,
  `isAvailable` absent or merely truthy, `isAvailable` true but `status` still
  inCinemas/announced (codex round-1 #1), released by Radarr's 90-day cinema
  fallback with no home-release date, or a future/malformed one (round-2 #1),
  unknown `hasFile`, already queued, bool ids in either list, a null title
  (round-1 #3).  Also: Radarr's bulk `MissingMoviesSearch` is never
  used, an unreadable movie list or queue searches no movies, and a failure or
  missing key on one app still lets the other search.

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
