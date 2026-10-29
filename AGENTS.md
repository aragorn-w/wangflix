# AGENTS.md: Media Stack (DVR)

Canonical agent guidance. Home-server policy is in `$HOME/.claude/CLAUDE.md` (maintainer host only; generated, so `chezmoi edit --apply` it). `$HOME/CLAUDE.md` is retired and intentionally empty; do not delete it.

```bash
docker compose ps  # status
docker compose restart <svc>
docker compose logs -f --tail 100 <svc>
python3 $HOME/media-stack/nuke_stalled.py  # one-shot stall-killer
bash $HOME/media-stack/consolidate-status.sh  # pipeline snapshot
nvidia-smi  # NVIDIA GPU visible?
```

## Core Mandates

- **VPN:** Gluetun (ProtonVPN WireGuard) locked to **Switzerland** (`VPN_COUNTRY` in `.env` is authoritative).
- **Media rules:** Radarr/Sonarr block AV1 and 12-bit HEVC; Tdarr/Bazarr English only. Keep Radarr/Sonarr **hardlinks** ENABLED and Docker DNS forced to 8.8.8.8/Cloudflare.
- **Host-side API calls** use `MEDIA_LAN_IP` (`.env`, default `10.0.0.10`), not `localhost` (Gluetun resets loopback); shell sources `lib/paths.sh`, Python imports `media_stack.paths`.
- **Perimeter:** UFW must stay OFF (locks out SSH); the box listens only on Tailscale + its LAN subnet (`FIREWALL_OUTBOUND_SUBNETS`); no public exposure.
- **Credentials:** never put live secrets in `AGENTS.md`, scripts, code, docs or commits; use `.env` (gitignored; keys listed in `.env.example`) and variable names.

## Services

Ports and auth: `docker-compose.yml`; audit with `docker compose ps --format json | jq '.[] | {Service,Publishers}'`. Keys live in `.env`; Bazarr's: `docker exec bazarr cat /config/config/config.yaml`. Prowlarr, Flaresolverr, Tdarr: no auth, Tailnet/LAN only. No torrent peer-listen host publish, by design: do not re-add static `54321`.

## Repo Layout

`ls` / `git ls-files` is authoritative. Ephemeral files live under `var/` (`log`, `run` for `*.lock`, `state` for `*.done`/JSON, `reviews`, `quarantine/subtitles`; `lib/paths.sh` `$VAR_*` / `media_stack.paths`). Per-file media locks (`.consolidate-<name>.lock`) stay beside the media file (same filesystem, for flock), NOT in `var/run/`. `config/` stays put for the compose bind-mounts.

## Media Normalization Pipeline (v2)

Never-weaken rules; symbols are in `consolidate-subs.py` unless a file is named.

### consolidate-subs.py (subs, audio and container repair despite the name)

- **Ranking:** `_SubCandidate.rank` = (non-generated, non-SDH, score), text > image. The operator does NOT want hearing-impaired subs: SDH/CC/HI only as the sole English candidate; detection matches whole tokens only (`is_sdh_track`).
- **Audio:** English primary; Japanese for anime or `/anime/` paths; Korean for Korean cinema; drop commentary, audio-description, non-primary dubs. `DUAL_AUDIO_KEYWORDS` is checked BEFORE the anime rule. `select_keep_audio` membership is FAMILY-based (`canonical_lang`), never raw alias strings.
- **Original language** (`media_stack/arrlang.py`): `eng`/`jpn`/`kor` else `None`, never a made-up code; beats the Matroska `default` flag. With `None`: `original` before EITHER's `default` for BOTH jpn and kor; `default` alone neither promotes a language nor drops the foreign track. Index an Arr record only when its path's PARENT is `ARR_MOVIES_ROOT`/`ARR_TV_ROOT`; 2+ records sharing a basename are ambiguous -> `None`.
- **Container:** final mux is mkvmerge, never ffmpeg (zero-cue files). Cover-art removal is by CODEC NAME, so genuine MJPEG motion video is dropped too (deliberate; silent beside a real video stream).
- **Duration guard:** `_expose_tag_normalize` (the one swap, shared by the full, cleanup and upgrade passes) compares container durations via `probe.duration_shortfall` right before `os.replace`: output < source - max(`OUTPUT_DURATION_SLACK_S`, `OUTPUT_DURATION_SLACK_FRAC` x source) is FAIL "duration verify: ...", tmp deleted, original untouched. A readable source with an unknown or zero duration skips (logged); an unprobeable source or output, or an output with no duration, fails. Two episodes were once truncated by a short remux.
- **Idempotency:** `var/state/consolidate-subs.state.json` + mkv tag `CONSOLIDATED_SUBS=v2`; bump `PIPELINE_VERSION` to re-flow.
- **File identity includes `st_ctime_ns`** at every trust boundary; a persisted three-field placement record counts as changed. Three-field `[ino, size, mtime_ns]` stays DELIBERATELY for `needs-english.json` (`identity_matches(..., allow_legacy=True)`) and `audio-lang-check.py` (ctime moves on Arr hardlink changes).
- **Working files** only via `_workfile(workdir, role, suffix)` (`mkstemp`), never names derived from the video, a sidecar or another working file.
- **Sidecar ownership** (`find_sidecar_subs`): exact stem boundary; a LONGER-stemmed sibling owns a sidecar only if it is a regular FILE with a video extension that would accept it; English tag = any tag starting `en`.

#### Content gate

- **A tag or filename claiming English is not proof:** the FULL path decodes and scores the TEXT of every candidate (embedded, main/forced sidecar, subliminal download) with `sublang.classify_candidate` BEFORE ranking. Reject only on real evidence (a wrong rejection costs more than a wrong acceptance). `sublang._clean` strips HI conventions cue-scoped; ASS is detected by structure, only `Dialogue:` text scores.
- **Unclassifiable** (`_Unclassifiable`: over 8 MiB, unstable, not text, unreadable) is NOT a candidate: logged, never copied, ranked or muxed on tag/name; an extraction failure; a sidecar stays and is retried; an embedded track isn't proven foreign; a FORCED one is untouched and out of every decision. All candidates content-rejected -> NEEDS_BAZARR; ANY extract/read failure with none accepted -> FAIL "all extracts failed".
- ASS/SSA (`config.ASS_CODECS`) is extracted as `.ass`, never straight to SRT; convert only accepted tracks, from the decoded copy; a forced one that fails conversion is never remuxed unconverted. Working copies are UTF-8 no BOM in their own `_workfile`; `_sidecar_as_srt` gets decoded text, never copies the original.
- A rejected EMBEDDED track is never chosen and is dropped on the NEEDS_BAZARR cleanup. A rejected SIDECAR is QUARANTINED, never deleted, and out of every decision even if the move fails.
- **Quarantine** (`_quarantine_sidecar`): copy the SAME bytes that were classified (`_read_snapshot`, bounded 8 MiB) into `var/quarantine/subtitles/` with `O_EXCL`; fsync the copy and the dir chain up to and including `var/` on EVERY move; never rename across filesystems; unlink the original only if its identity is unchanged (a failing fsync keeps it and removes the copy). Failures never change the result. The dir is deliberately NOT in `ensure_var_dirs()`.
- **Embedded forced English track** (`_gate_embedded_forced`): gated UP FRONT, before the "no main English candidate" decision; rejected = proven foreign; unreadable = extraction failure, out of every decision and KEPT (stream-copied via `remux(kept_source_subs=...)` with exact flags unless an accepted forced sidecar replaces it; verify before swap, else FAIL `forced track verify`). FIXED drops every embedded subtitle not re-attached.
- FIXED removes only sidecars read and accepted in that run, while the file still has the identity it was read with (inode, size, mtime_ns, ctime_ns); never sweep every file starting with the stem. `--dry-run` extracts and moves nothing.
- **Accepted forced sidecars survive unless actually embedded** (a usable embedded forced track in use, or a conversion/embed failure, leaves them in place; only the ONE embedded is consumed; never in an upgrade).

#### Late sidecars (state-cache and tag-present SKIPs)

Upgrade check first (an upgrade skips the sweep), then `_late_sidecar_sweep` quarantines non-English sidecars and English ones made REDUNDANT (a main one by a non-forced embedded English text subtitle; a forced one only by an embedded FORCED English track).

- **Never remove a subtitle without positive proof:** the embedded track's CONTENT must score `eng` on a bounded ~10-minute sample (`subverify.embedded_text_window`, never the whole file); the content gate's benefit of the doubt is NOT enough (a forced track may be too short to score). Otherwise KEEP (no embedded English text subtitle, probe failure, unreadable/non-English text; forced-only doesn't count for a main sidecar).
- With a generated-only English track, quarantine a non-generated main sidecar only if the upgrade check REJECTED it as the wrong episode (fresh or remembered `upgrade_rejected`, honoured only while the video's 4-field identity AND the sidecar's fingerprint match; legacy/short records are invalid and transient failures never remembered) or the content gate rejects its text; partial, undecided and transient ones are KEPT. A forced sidecar is retired by a verified embedded forced English track whatever its title.
- Re-check the video's 4-field identity before EACH move (changed: stop; unreadable baseline: move nothing); status stays SKIP.

#### Generated -> real upgrade (`_generated_upgrade_check`)

Runs under the per-file lock BEFORE the state-cache/tag skips, for an already consolidated file whose main English text tracks are all "(generated)" and that has a non-generated main English sidecar.

- The sidecar must classify English AND be the same episode: `subverify.overlap` vs the generated track in three DISJOINT windows (usable at >= 15 distinct words; pass at word ratio >= 0.60 AND bigram ratio >= 0.40, constants shared with `sub-fallback.py`); two passing windows verify. A NEGATIVE verdict (what the late sweep quarantines on) needs `evidence >= 2`. Too little text or any failure = no upgrade, never a guess.
- **Sidecar evidence is disjoint too** (disjoint video windows alone let `overlap()`'s flat 30 s padding reuse a cue): `judge_sidecar_against_windows` pads each window `min(30 s, half the gap to its neighbour)` and gives each sidecar cue to at most ONE window (by start time, half-open); `overlap()` itself is unchanged.
- **What is verified is what is muxed:** read each sidecar ONCE (bounded 8 MiB; bigger is REFUSED, never prefix-read; complete and stable, else retried) and mux those exact bytes. After the swap delete ONLY the muxed sidecar, only if identity AND re-read sha256 still match; never sweep others.
- Only the English subtitle changes: candidates are EXACTLY the verified snapshots (none usable -> SKIP, never re-mux the generated track); all video (no cover-art strip) and audio stay in order with exact source flags (`remux(cleanup_flags=...)`, no audio policy or Arr lookup); EVERY forced English subtitle (text or image) is kept unchanged (`keep_subtitle_indices`; unrecognised codec: SKIP), then the new default main English.
- **Verify before swap** (`_verify_upgrade_output`: counts/flags and subtitle layout from the SOURCE, else FAIL "upgrade verify: ...", original untouched); re-check the video's 4-field identity before the remux and the swap (changed: SKIP). An upgrade NEVER runs loudness normalization, whatever `NORMALIZED_AUDIO` says.

#### No English subtitle yet (NEEDS_BAZARR cleanup)

Four `_process_locked` call sites (NEEDS_BAZARR/FAIL variants, incl. a subliminal download below `SUBLIMINAL_MIN_COVERAGE`) first run `_cleanup_no_main_english`.

- **Never delete a track that might be wanted;** keep when unsure. Video is NOT TOUCHED AT ALL (cover art too).
- **Audio is NEVER dropped or physically reordered.** Only WHICH track ID carries `default` may change, and only when `audio.confident_default_audio` is confident (keyword rule, KNOWN Arr `original_language`, or explicit `original` disposition; never a bare `default` or a guess); `None` leaves audio untouched (an Arr outage must not flip defaults each retry); a pick already the ONLY default-flagged stream is no change (the remux still runs if foreign subtitles must be dropped, else `nothing to clean`).
- **EXACT flag preservation:** omitting `--default-track` means "mkvmerge decides": every kept track gets an explicit `--default-track`, every kept subtitle an explicit `--forced-track`, from the probed SOURCE dispositions (`mux.remux(cleanup_flags=...)`); the confident audio pick is the only intended change. The full path (`cleanup_flags=None`) argv stays byte-identical.
- **Positively foreign** subtitles are the only droppable ones: base subtag `^[a-z]{2,3}$` (ASCII only), non-English (`lang.is_english_tag`), not `und`/`mul`/`mis`/`zxx`/`qaa`-`qtz`. Everything else stays, as do the forced-English text track and English image subs.
- **No loudness normalization from this path, ever** (`_expose_tag_normalize(normalize=False, tag=False)`).
- **Verify before swap:** counts (audio == ORIGINAL, never fewer) and the audio/video default-flag pattern match the INTENDED result, else delete the tmp output, FAIL "cleanup verify: ...", original untouched. Does NOT stamp `CONSOLIDATED_SUBS` or touch sidecars; idempotent; status/exit codes unchanged except on remux/verify failure.
- **`needs-english.json`** is upserted under `mutate_state` whenever `_process_locked` returns NEEDS_BAZARR (`first_seen` preserved, migrated on `.mp4` -> `.mkv`, cleared on FIXED, skipped in `--dry-run`, write failures swallowed). `_utc_now_iso()` must stay MICROSECOND precision (same-second upserts are byte-identical and `drop_stale_entries` would drop the fresh one); `_parse_iso` must keep accepting old whole-second `...Z`. Keep it SEPARATE from `consolidate-subs.state.json` (its `(size, mtime, v)` key doesn't change when a sidecar lands).

### normalize-audio.py (EBU R128 -23 LUFS)

- Two-pass `loudnorm`, tag `NORMALIZED_AUDIO=v1`. Pass 2 maps real motion video only (`probe.real_video_streams`; a bare `-map 0:v` carried cover art and stalled Jellyfin Android TV) and preserves EVERY audio track: only the primary is re-encoded, the rest stream-copied with per-stream specifiers (`-c:a:N`/`-filter:a:N`/`-ac:a:N`, never bare).
- Before the swap `duration_shortfall` (same rule as consolidate's duration guard) compares the remux output with the source: short = `RuntimeError` -> FAIL "duration regressed", temps removed, original untouched.
- Inline run (`_normalize_audio_inline`): the outer cap is `loudness.normalize_budget_s(size)`, never fixed; the child runs in its own session and its whole process group is killed on expiry or interrupt. No separate sweep or cron job: only inline from the full pass.

### Triggers (schedules: `ops/cron.d/media-stack.crontab`)

- `consolidate-watch.service` (inotify) maps a sidecar event to its video by peeling tags from the stem (`generated` peels like `en`/`forced`; a video named EXACTLY like the remaining stem wins), accepting only under `find_sidecar_subs`'s own rule; the 05:00 `--scan` is the backstop.
- `movie-dedupe.py` / `tv-dedupe.py`: only the SAFE case auto-resolves (the arr tracks the keeper; the leftover goes to recoverable `.dupe-recycle/`); RISKY (arr tracks a non-keeper) needs manual review (`--apply --force`).
- `sync-forwarded-port.py` keeps qBittorrent's listen port equal to Proton's forwarded one (`VPN_PORT_FORWARDING=on`, `PORT_FORWARD_ONLY=on`; `ops/gluetun/auth-config.toml` grants the ONE `/v1/portforward` route). The WireGuard key needs NAT-PMP on, Moderate NAT off, a Standard server; a regenerated key is NOT self-healing (recreate gluetun and its seven netns dependents). Verify via the VPN exit IP, not `connection_status`.
- `search-missing.py`: Radarr does NOT get `MissingMoviesSearch` (it approves pre-release junk); it sends `MoviesSearch` only for monitored, fileless, not-queued, RELEASED movies: `status == "released"`, `isAvailable`, AND a past `digitalRelease`/`physicalRelease`.

### sub-fallback.py (hourly ASR fallback)

Mechanics: the module docstring and `media_stack/subverify.py`. `faster_whisper` is optional (don't assume it is installed); workdirs are per-file under `WORKDIR_ROOT` (local `var/`, never the media pool) with fixed per-role names, never derived from a stem; `--dry-run` mutates nothing.

- **Stale tracker entries:** `drop_stale_entries` re-checks each key's CURRENT entry under `mutate_state`'s lock against the SNAPSHOT that judged it stale; never pop from the snapshot unconditionally. `_clear_placement` likewise.
- **Never weaken the verification gates (ratios, word count, language) to speed it up:** a `fetch_via_subliminal` result is accepted ONLY if `sublang` scores it English AND `subverify.overlap` on a short ASR window of the file's own audio passes word ratio >= 0.60 AND ordered-bigram ratio >= 0.40. An untagged/`und` audio stream must first pass whisper language-ID with a MULTILINGUAL model (never `.en`; `english_audio_stream` reuses `audio.is_commentary_audio`), else `AUDIO_LANGUAGE_UNVERIFIED`.
- **Whole-file transcription** runs as `python3 -m media_stack.subverify transcribe ...` under a hard timeout (`SUB_FALLBACK_ASR_TIMEOUT_MIN`) and any result with >= 1 cue is accepted: kill its process group on EVERY exit path; never START it with < 10 min of the run's budget left (checked AT that moment, `DEFERRED_BUDGET`). `subverify._srt_timestamp` must round to TOTAL integer ms before splitting into h/m/s/ms.
- **The `generated` marker is a three-place contract; don't change one place:** a Whisper transcript is `<stem>.en.generated.srt`; `find_sidecar_subs` must keep reading it as a plain non-forced English sidecar; `consolidate-watch.sh`'s stem-peeling regex must keep including `generated` (else `_place_sidecar`'s `close_write` never maps back); `_SubCandidate.rank` must keep generated first, SDH second. Muxed as "English (generated)".
- **Placement** (`_place_sidecar`) is atomic and no-clobber: hidden UNIQUE `mkstemp` temp in the VIDEO's dir (never a fixed name; sweep leftovers at each attempt's START), fsync, `os.link()` onto the destination. **Never replace the hard-link publish with a rename** (`renameat2(RENAME_NOREPLACE)` is EINVAL on mergerfs): `FileExistsError` is `SIDECAR_APPEARED`, any other `OSError` is `PLACE_FAILED`, never a clobbering fallback. Keep the pre-publish re-checks (`ALREADY_CONSOLIDATED`, forced/main sidecar, `_video_identity` = `VIDEO_CHANGED`, sibling collision).
- **Never place a sidecar, or credit a pipeline tag, across a same-stem sibling collision (`Episode.mp4` + `Episode.mkv`)** (`SIBLING_COLLISION`; checked FIRST in `process_candidate` and again in `_place_sidecar`); `wait_for_pipeline` credits `<stem>.mkv` ONLY once the original is gone. Don't loosen one check without the other.
- **A failed post-link `close_write` is never a placement failure:** run `_run_consolidate_subs_directly`, record `event_fallback_used`, still reach `FIXED`, NEVER a plain `ERROR`.
- **ANY forced English sidecar blocks this script entirely** (`FORCED_SIDECAR_PRESENT`: nothing fetched, transcribed, placed, retriggered or recorded): consolidate deletes forced-pattern sidecars, so a main sidecar beside a forced-only file would destroy it. Use `_main_sidecars`, so a forced entry is never counted as the main sidecar.
- **Never retrigger the pipeline onto an existing sidecar without PROOF this script placed and verified it against THIS video:** each publish is recorded (`_record_placement`, `var/state/sub-fallback-placements.json`; a READ failure means don't retrigger); retrigger only with EXACTLY ONE main sidecar AND `_sidecar_matches_our_placement` (path, sha256, video identity); else `SIDECAR_PRESENT`, no action.
- **Playback gate fails closed, CUMULATIVELY per candidate:** `JellyfinClient.now_playing_sessions` returns `None` (not `[]`) on ANY failure and callers treat `None` as "someone is watching"; every ffmpeg audio extraction checks playback BEFORE starting and while running. ONE `PlaybackWaitBudget` (`media_stack.playback`, `SUB_FALLBACK_MAX_WAIT_MIN`) per `process_candidate` must be threaded through EVERY wait, exhaustion checked after EVERY poll; past it the WHOLE run ends cleanly (exit 0). The child gets the REMAINING allowance and reports its wait back (`CHILD_PLAYBACK_WAIT_REPORT_FILENAME`) for deduction on EVERY exit path. A missing `JELLYFIN_API_KEY` is never "idle": child exit 3 -> `MISSING_JELLYFIN_KEY`; a REAL run with no key refuses to start (exit 1); `--dry-run` is exempt.
- **Numeric settings must be validated finite, positive AND bounded, and the DEFAULT must respect its own bound** (`env_float`/`env_int` from `media_stack.envconfig`, shared by `sub-fallback.py`, `audio-lang-check.py` and the child): reject `nan`/`inf`/`1e308`/zero/negative/over-bound with a logged warning and use the default.

### audio-lang-check.py (hourly :37)

Whisper language-ID spot-check of the audio's ACTUAL language; the module docstring has the classification, state schema and alert rules.

- **A foreign TAG never outweighs a confident detection:** decide on ALL sampled windows with RAW probabilities; anything not determined with confidence is `UNVERIFIED` (logged, never alerted). Re-check only when `[ino, size, mtime_ns]` changes: deliberately NOT the four-field `fileid` identity (ctime moves on Arr hardlink changes and would re-run whisper and re-alert).
- Failed analyses are never cached: `PROBE_FAILED`/`ERROR` are counted per file identity in a `failed` record and tried again every run up to `MAX_FAILED_ATTEMPTS`, then skipped until the file changes; `ARR_UNAVAILABLE` records nothing (an outage must not use up attempts); missing `faster_whisper` ends the run (exit 1). A title added to Radarr/Sonarr after its file was checked is NOT revisited until the file changes. ONE state file (`var/state/audio-lang-check.state.json`), each update one locked `mutate_state`, the WHOLE structure validated on load (unreadable or malformed = blinding error, exit 1 before any write); keys of older versions (`streams`, `original_*`, `retry`, whose counter also counted outages) are ignored on load and dropped when the entry is rewritten.
- **Never deletes, blocklists or re-grabs** (Arr won't import a same-quality replacement while the file exists).
- Same fail-closed Jellyfin gate as sub-fallback: no file is read (ffprobe included) before it says idle. Each NEW finding is Telegrammed ONCE (size-bounded messages, oldest first, never for a gone or replaced file); a failed send is logged (stderr and an `ALERT_FAILED` JSONL record) and NEVER retried, and the finding counts as `alerted` either way. `--path FILE` records NOTHING (refused with `--dry-run`); `--dry-run` makes no writes (no `var/` dirs or lock file). Duration comes from a NON-COMMENTARY AUDIO stream only.

### shield-check.py (daily 12:15)

REPORT-ONLY drift check of the Shield TV units against `ops/shield/profile.json`; the module docstring has the alerting rules.

- **Never give it a way to change a Shield** (an automatic "debloat" once disabled packages Play Store needs): only `adb connect`, `adb devices` and ONE shell of `pm list packages [-d]` and `settings get`; remediation hints go to the log, never executed. No apply mode, `pm disable`/`enable`/`install`/`uninstall`, `settings put` or reboot.
- Device addresses live ONLY in `SHIELD_DEVICES` in `.env` (the repo is public): no address, MAC, serial or email in any tracked file (tests use RFC 5737 addresses). Device-specific settings (CEC, IR, volume, ...) stay out of the profile on purpose.
- At most ONE Telegram message per run; alerts compare the CURRENT condition with the last DELIVERED state (no queue: a condition that clears before delivery is never reported) and a unit's state advances only after delivery. Every adb command is wrapped in markers (per-run random nonce plus `$?`): non-zero/missing status or cut-short output = unreachable (an empty list is never taken on faith); `unauthorized` is a finding.

## Agent Operating Rules

- **No bare `nohup` for jobs that must outlive a Claude session** (a bounce kills it): wrap with `setsid`.
- **Don't touch `consolidate-subs.py` mid-sweep** (live writes corrupt state; the review-loop gate applies); never run `--scan` while a sweep is in flight.
- **Don't change the R128 -23 LUFS target without approval** (a switch invalidates all coverage tags).
- **Times reported to the user** are 12-hour AM/PM in the operator's local timezone; logs and cron follow the host timezone.
- **UI changes:** exercise them in a browser before claiming success.
- Bazarr: English profile (id=1) on everything; `profileId: None` items are invisible to sub search: run `bazarr-profile-audit.py` after big imports. Jellyfin key minting: `jellyfin-mint-api-key` skill.
- Pitfalls: `apt autoremove` after an NVIDIA driver purge yanks the active stack (`apt-mark manual` it first); `/mnt/disk{1,2,3}` are mergerfs pool members (per-branch `df` is not "data outside the pool"); a sweep `pgrep` must include `--scan.*movies`.

## Maintenance

- Host updates via `aptitude` (nightly 04:00); containers via Watchtower (24h); `restart: unless-stopped` on all. No media backups (re-fetchable); snapshot `config/` before destructive changes.
- **`:latest` images + Watchtower are deliberate** (freshness over reproducibility; `healthcheck.sh` and the nightly log surface a breaking update; recover by pinning the service's `image:` tag in `docker-compose.yml`, then `docker compose up -d <service>`). `watchtower` is the `nickfedor` fork (upstream `containrrr` is abandoned) and `jellyseerr` runs the community fork image `ghcr.io/seerr-team/seerr`: never swap either back to upstream.

## Validation

- Probes: `bash healthcheck.sh` (exit 0/1/2; `--verbose`, `--json`); `bash preflight.sh` (dependencies; at install and after host upgrades); `bash ops/audit.sh` (live cron + systemd vs `ops/` snapshots; fails on drift); `docker exec gluetun wget -qO- https://ifconfig.co` (VPN egress, must NOT be the host IP); `tail -20 var/log/stall_killer.log`.
- **Python:** syntax `python3 -c "import ast; ast.parse(open('FILE').read())"`; `python3 -m pytest tests/ -q` (all must pass); `ruff check media_stack/ *.py` and `mypy` (`pyproject.toml`; must stay clean); smoke `python3 FILE --help` or `--dry-run`.
- **Shell:** `bash -n FILE`; `shellcheck FILE` if installed. **Compose:** `docker compose config`, `docker compose up -d <svc>`, `bash healthcheck.sh`.
- **Cron / systemd:** update the live state AND the snapshot under `ops/`, then `bash ops/audit.sh`.
- **Pipeline runs:** before sweeping, `python3 consolidate-subs.py --dry-run <one-file.mkv>`, eyeball the plan, then run for real on that one file.
