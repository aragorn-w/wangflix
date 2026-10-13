# AGENTS.md — Media Stack (DVR)

Canonical agent guidance for this directory. `CLAUDE.md` is a symlink to this file.
Top-level home-server policy lives at `$HOME/.claude/CLAUDE.md`, not in this repo.
The rest of this paragraph is maintainer-host only; replicators can ignore it.
That file is generated, and hand-edits to it are reverted within 30 minutes, so
change it with `chezmoi edit --apply $HOME/.claude/CLAUDE.md`, or
non-interactively by editing `$(chezmoi source-path $HOME/.claude/CLAUDE.md)` and
then running `chezmoi apply $HOME/.claude/CLAUDE.md`. `$HOME/CLAUDE.md` there is
retired and intentionally left empty; do not delete it.

---

## Quickstart

```bash
docker compose ps                          # stack status
docker compose restart [service]           # bounce a single container
docker compose logs -f --tail 100 [svc]    # tail logs
python3 $HOME/media-stack/nuke_stalled.py   # one-shot stall-killer
bash $HOME/media-stack/consolidate-status.sh
nvidia-smi                                 # verify NVIDIA GPU is visible
```

## Core Mandates

- **VPN:** Gluetun (ProtonVPN WireGuard) locked to **Switzerland**. Ports 8090, 8989, 7878 route through tunnel. `VPN_COUNTRY` in `.env` is authoritative; compose default mirrors it.
- **Media Rules:** Radarr/Sonarr strictly block AV1 and 12-bit HEVC (Custom Formats, -10000 score in "Shield Prioritized" profiles). Tdarr/Bazarr locked to English.
- **Hardlinks:** ENABLED in Radarr/Sonarr. Do not disable or disk usage will double.
- **DNS:** Forced to Google/Cloudflare (8.8.8.8) in Docker to fix Trakt API timeouts.
- **API calls from host:** Must use the LAN IP (`MEDIA_LAN_IP` in `.env`, default `10.0.0.10`), not `localhost` (Gluetun resets loopback connections).  Shell scripts source `lib/paths.sh` for the value; Python imports from `media_stack.paths` (new code) or `media_paths` (legacy compat shim re-exporting the same names — don't reach for it in new code).
- **Network perimeter:** Tailscale handles encryption; UFW must stay OFF (locks out SSH). Admin services bind to all interfaces but the box only listens on Tailscale + its LAN subnet (configure via `FIREWALL_OUTBOUND_SUBNETS` in `.env`) — no public exposure.

## Service Map

Every container that publishes a host port is listed below.  Anything not in
this table either binds to the gluetun network namespace (no separate host
port) or is internal-only.

| Service | URL (LAN) | Auth |
| :--- | :--- | :--- |
| qBittorrent | `http://10.0.0.10:8090` | `QBIT_USER` / `QBIT_PASS` in `.env` |
| Sonarr | `http://10.0.0.10:8989` | `SONARR_API_KEY` in `.env` |
| Radarr | `http://10.0.0.10:7878` | `RADARR_API_KEY` in `.env` |
| Prowlarr | `http://10.0.0.10:9696` | no auth (Tailnet/LAN only) |
| Jellyfin | `http://10.0.0.10:8096` | local user/pass (rotate from defaults) |
| Jellyseerr | `http://10.0.0.10:5055` | no auth on bootstrap; pair to Jellyfin |
| Bazarr | `http://10.0.0.10:6767` | apikey inside `docker exec bazarr cat /config/config/config.yaml` |
| Flaresolverr | `http://10.0.0.10:8192` | no auth (LAN/Tailnet only; called from Prowlarr).  Host port 8192 maps to container port 8191 via gluetun's port-publish list. |
| Tdarr server | `http://10.0.0.10:8265` | no auth (UI), `8266` worker API |
| Kavita | `http://10.0.0.10:5000` | local user/pass |
| NFS | `<MEDIA_LAN_IP>:$HOME/media-stack/data/media` | subnet allow (the LAN subnet in `/etc/exports`) |

**Non-HTTP listen ports** (perimeter inventory, not admin UIs):

| Port | Protocol | Purpose |
| :--- | :--- | :--- |
| _(none)_ | torrent peer-listen | **No host publish, by design.** ProtonVPN allocates a DYNAMIC forwarded port; gluetun opens it inside its own firewall, so inbound peer traffic never traverses a host port mapping. The static `54321/tcp+udp` publishes that used to sit here were removed 2026-09-16: Proton was never forwarding anything to them, so they documented an inbound path that did not exist. |

**Ports NOT exposed to the LAN** (gluetun namespace, no host port published):
`unpackerr`, `watchtower`, `tdarr-node`.

Audit drift between this table and live state with:

```bash
docker compose ps --format json | jq '.[] | {Service:.Service, Publishers:.Publishers}'
```

**Credential policy:** never put live secrets in `AGENTS.md`/`CLAUDE.md`, scripts, or commits. Use `.env` (gitignored) and reference variable names in docs. `.env.example` lists every required key.

## Repo Layout

For the directory tree itself, `find`/`ls` on a checkout is authoritative — it's
not repeated here. The non-obvious part is the runtime-artifact convention:

**Runtime-artifact convention:** as of DEFERRED #5 (landed 2026-05-19),
every stack-level ephemeral file lives under `var/`.  Shell scripts get
the paths from `lib/paths.sh` (`$VAR_LOG`, `$VAR_RUN`, `$VAR_STATE`,
`$VAR_REVIEWS`); Python imports the same names from `media_stack.paths`
(or the `media_paths` shim, for backward compat — prefer the package
in new code).

Conventions:
- Logs (`*.log`, `*.out`, `*.err`) → `var/log/`
- Lock files (`*.lock`) → `var/run/`
- Completion sentinels (`*.done`), JSON state, scan output → `var/state/`
- Codex multi-agent reviews → `var/reviews/`

Per-file media locks (`.consolidate-<name>.lock`, written next to a
specific media file) DO NOT move into `var/run/` — they need to be on
the same filesystem as the media file for flock semantics.  Only
stack-level runtime files live under `var/`.

The `config/` tree (per-Docker-service persistent state — Sonarr DB,
Bazarr settings, etc.) stays where it is so the bind-mounts in
`docker-compose.yml` continue to work.

## Media Normalization Pipeline (v2)

- **Authoritative tool:** `consolidate-subs.py` — handles subs, audio, and container repair despite the name.
  - **Subtitles:** picks highest-scoring English sub (text > image; SDH > non-SDH), runs `ffsubsync`, regex-cleans `\h` / ASS override tags / stray HTML, smart-recases ALL-CAPS, remuxes lone English sub with `default=1` (forced track preserved second).
  - **Audio:** single primary track by default. English; Japanese for anime / `/anime/` paths; Korean for Korean cinema. Drops commentary, audio-description, visual-impaired, non-primary dubs. `DUAL_AUDIO_KEYWORDS` is the per-title opt-in that keeps one track per language family instead (original + English dub), and it is checked BEFORE the anime rule precisely so a title can be in both sets: an anime whose own name matches `JAPANESE_KEYWORDS` would otherwise have its dub pruned and could never hold a dual-audio file. `select_keep_audio`'s in-spec/scoring test is FAMILY-based (codex round 3 #1): a stream's raw tag is canonicalized (`canonical_lang`, regional/script-variant aware) and compared against the canonical families `allowed_langs` maps to, not literally against the raw alias strings — a literal match missed e.g. a `ja-JP` stream even when the jpn family was allowed.
    - **Original-language signal:** `media_stack/arrlang.py` maps a library file back to its Radarr/Sonarr record (by matching `movies/<Folder>/...` / `tv/<Folder>/...` against `movie["path"]` / `series["path"]`'s last component) and returns `originalLanguage.name` mapped to an ISO 639-2 code — `eng`/`jpn`/`kor` only; any other name (or anything it can't determine at all) returns `None`, never a made-up code. `get_audio_lang_pref` takes this as `original_language=` and trusts it over the Matroska `default` disposition: release groups routinely flag a DUB `default` (an English film with a Japanese dub flagged default was previously processed as Japanese-original and ended up with the dub as the default audio — fixed). When arrlang can't determine the language (API unreachable, no match, etc.) it returns `None` and the old disposition-based promotion still applies, but sharpened: for BOTH jpn and kor, `original` disposition is checked before EITHER's `default` is considered at all (so a jpn `default` can never outrank a kor `original`, or vice versa); `original` still promotes to that language's primary; `default` alone (no `original`) no longer promotes to primary EITHER, but — critically — also no longer causes the foreign track to be dropped: both eng + jpn/kor are kept, with eng as the (safe, reversible) default. A wrong default flag can be fixed on a later pass; a deleted track cannot.
      - **Root check (codex round 5):** matching by folder BASENAME alone is not enough — a Radarr/Sonarr record at an UNRELATED root (e.g. `/unrelated-library/Shared`) could otherwise supply the signal for a host file under `movies/Shared` purely because the two folder names collide. A record is only indexed when its path's PARENT directory matches the expected Arr root for its kind: `ARR_MOVIES_ROOT` (default `/movies`) for Radarr, `ARR_TV_ROOT` (default `/tv`) for Sonarr — same process-env-over-`.env` precedence as `RADARR_API_KEY`/`SONARR_API_KEY`, compared with trailing slashes stripped (posix semantics). If two or more records that DO pass that check still share a basename, the basename is AMBIGUOUS and maps to `None` rather than guessing which one a host file with that folder name actually is.
  - **Container:** final mux is **mkvmerge** (not ffmpeg) — produces a proper cue index. ffmpeg's matroska muxer wrote zero-cue files, breaking scrubbing + tripping Intro Skipper.
  - **Cover art:** drops still-image "video" tracks (mjpeg/png/jpeg) lacking `attached_pic` disposition — these caused black-screen-no-audio bugs. **Known limitation, deliberate:** the test is the codec name, so a genuine MJPEG motion video would also be dropped. Two attempts to infer motion from container metadata were both broken by counterexamples built with real ffmpeg, and the metadata is not trustworthy for this: `avg_frame_rate` can read `25/1` on a one-frame thumbnail, Matroska's `DURATION` tag records the END timestamp (a 1-frame thumbnail starting at t=1s reads as 26 frames), `nb_frames` can be a placeholder (`1073741824` observed on streamed AVI), and `r_frame_rate` (ffmpeg's *guessed base* frame rate, not the time base) came back as `90000/1` on every cover-art stream scanned here. Only an actual decode (`-count_frames` / `nb_read_frames`) settles it. The library was scanned as of 2026-09-16: it contains **no** genuine still-image-codec motion video, every such stream being `avg_frame_rate=0/0` + `attached_pic=1` cover art, so the codec rule costs nothing here and the cover-art bug it prevents is real and user-visible. **The failure is only loud when the MJPEG is the ONLY video stream** (pass 2 then raises "no real video stream to map" and leaves the file alone). Alongside a real HEVC/h264 stream it is dropped SILENTLY, and `_check_stream_regression` will not catch it because that guard uses this same classifier; an already-on-target MJPEG-only MKV skips pass 2 entirely via `TAG_ONLY`. Fix by counting frames, never by trusting rate or duration.
  - **Idempotency:** state file at `var/state/consolidate-subs.state.json` + mkv global tag `CONSOLIDATED_SUBS=v2`. Bump `PIPELINE_VERSION` to trigger re-flow.
  - **No English subtitle yet (NEEDS_BAZARR cleanup):** a file with no English subtitle candidate (embedded, sidecar, or image) used to be left completely untouched until Bazarr found one — which could be weeks, during which a MULTI release's foreign default audio track and every foreign subtitle track just sat there. `_process_locked`'s FOUR call sites for this situation — `NEEDS_BAZARR "no English candidates"`, `FAIL "only forced English"`, `NEEDS_BAZARR "only image subs (subliminal fetch failed)"` (the file DOES have an English image sub, but OCR-on-demand also came up empty), and `FAIL "all extracts failed"` (a text/sidecar candidate existed but extraction failed for all of them, no image fallback either) — all run the same AUDIO-AND-SUBTITLE CLEANUP REMUX (`_cleanup_no_main_english`) first, if there's anything to clean.
    - **Governing principle: never delete a track that might be wanted.** A wrong default flag is recoverable (a later pass, or a manual remux, fixes it); a deleted track is not. Every rule below is built to keep when unsure.
    - **Video is NOT TOUCHED AT ALL by this cleanup** (codex round 4 #1, tightened from round 3): not dropped, not reduced, cover art included — cover-art removal stays the FULL path's job (this cleanup runs long before there's an English subtitle to consolidate onto, so it has no business making container-repair decisions). Dropping cover art is no longer "something to clean" for this path either. `video_streams` (every video-type stream, raw/unfiltered) is passed straight through.
    - **Audio is NEVER dropped, and NEVER physically reordered, by this cleanup** (codex round 2 superseded the original "select_keep_audio minus a no-match guard" design, which could still drop a wanted track). The only audio action available is changing WHICH audio track ID carries the `default` flag, and only when `media_stack.audio.confident_default_audio` identifies that stream with actual confidence: a keyword rule (`DUAL_AUDIO_KEYWORDS` / anime / `KOREAN_KEYWORDS`), a KNOWN Radarr/Sonarr `original_language` in `(eng, jpn, kor)`, or an explicit `original` disposition (for jpn OR kor, `original` checked for both before either's `default` is ever considered — never a bare `default`, never a guess). `None` means "leave audio exactly as it is" — the thrash guard: an Arr outage yields `None` instead of flipping the default back and forth on every retry. mkvmerge's `--audio-tracks` is a SELECTOR, not a reorder instruction (`--track-order` would be required for that, and isn't used here), so there's no "reorder" to begin with — only the `default` flag moves, targeted by source track ID. The "already correct" guard is narrow and literal: the pick is already the ONLY default-flagged stream in the source — nothing about physical position matters.
    - **EXACT flag preservation (codex round 4 #2):** mkvmerge does NOT leave a track TYPE's `default` flag alone when the source provides none for that type at all — it auto-picks one itself. So omitting `--default-track` overrides does NOT mean "unchanged"; it means "mkvmerge decides". Every kept track of every type (video, audio, subtitle) now gets an EXPLICIT `--default-track`, and every kept subtitle ALSO an explicit `--forced-track`, built from the probed SOURCE dispositions (`media_stack/mux.py`'s `remux(cleanup_flags=...)`, a `{track_id: {"default": bool, "forced": bool}}` map) — the ONLY intentional change anywhere in that map is the confident audio pick, if any; everything else mirrors the source exactly. `cleanup_flags=None` is the full path's only mode and is unchanged: every video index gets `default=yes`, only the FIRST audio index gets `default=yes`, subtitles go through the `eng_srt`/`forced_srt` external-attach path — the full-path argv stays byte-for-byte identical.
    - **Subtitles (codex round 3, sharpened in round 4 #3 to ASCII-only):** a subtitle is droppable as "positively foreign" ONLY when its base subtag (before the first `-`, lowercased) matches `^[a-z]{2,3}$` (ASCII only — Python's `str.isalpha()` is Unicode-aware and would wrongly accept a non-ASCII "letter" sequence like an accented "éé" as a well-formed code) and names a language that ISN'T English (via `media_stack.lang.is_english_tag`, which also recognizes regional/script variants like `en-US`/`eng-GB`) and isn't one of the special non-language codes `und`/`mul`/`mis`/`zxx` or in the `qaa`-`qtz` private-use range. Everything else — empty, `und-*`, `mul`, `zxx`, non-ASCII letters, an unrecognized shape — is ambiguous and is always kept, same as the forced-English text track and any English image subtitle (the latter is a later subliminal OCR source — never dropped).
    - **No loudness normalization from this path, ever** (codex round 3): `_expose_tag_normalize` gained a `normalize=False` option that the cleanup always uses alongside `tag=False` — `normalize-audio.py` can re-encode the primary audio track and ffmpeg auto-marks the first stream `default` when none is flagged, either of which would re-flag audio outside `confident_default_audio`'s gate above. Normalization stays a FULL-pass-only step, exactly as before this cleanup existed.
    - **Verify before swap (strengthened round 4 #4):** after a successful cleanup remux, the output is re-probed and (a) its audio/subtitle/video track counts, AND (b) its audio/video per-track default-flag PATTERN, are checked against what was INTENDED before the file is swapped into place — the audio count must equal the file's ORIGINAL audio count (never fewer, per the no-drop rule above) and the video count must equal the file's ORIGINAL video count (never touched at all, per the no-touch rule above). Either kind of mismatch deletes the tmp output and returns `FAIL` ("cleanup verify: ...") without touching the original — belt-and-braces against mkvmerge (or the argv construction) silently dropping or misflagging something that was meant to survive unchanged.
    - This does **not** stamp `CONSOLIDATED_SUBS` and does **not** touch sidecars, so the file still gets the full pass once an English subtitle shows up; it's idempotent (nothing left to clean on the next run) and reported status is unchanged (same FAIL/NEEDS_BAZARR, same exit-code mapping in `main()`) except on an actual remux/verify failure — only `detail` gains a `cleaned: audio_default_change=... dropped_subs=...` / `nothing to clean` summary.

- **Audio loudness (EBU R128 -23 LUFS):** `normalize-audio.py`. Two-pass `loudnorm`; tags `NORMALIZED_AUDIO=v1` in MKV format_tags. Pass 2 maps real motion video only, via the shared `probe.real_video_streams` classifier — a bare `-map 0:v` carried over any still-image cover-art track still present in its input, so a file normalized without having been through consolidate-subs' strip kept the malformed mjpeg/png track and stalled Jellyfin Android TV at 0ms. (Normalization cannot re-add a track consolidation already removed; consolidation replaces the file before calling it.) Pass 2 preserves EVERY audio track, re-encoding only the primary through loudnorm and stream-copying the rest, using per-stream ffmpeg specifiers (`-c:a:N` / `-filter:a:N` / `-ac:a:N`); a bare `-c:a`/`-af`/`-ac` would hit the copied tracks too. It previously mapped only `0:a:<primary>`, which silently deleted the second track of every dual-audio title moments after consolidation had kept it, and `_check_stream_regression` did not notice because it counted subtitles and video but not audio. It counts audio now. Atmos 7.1.2 inputs (FL+FR+FC+LFE+SL+SR+TFL+TFR) get downmixed to 5.1 before AAC encode (native ffmpeg AAC can't take non-standard 8ch).

- **Driver:** `normalize-driver.sh` runs every 15 min via cron. Idempotent — checks coverage; relaunches sweep with `setsid` at `JOBS=5` if dead and <100%; touches `normalize-driver.done` + Telegram-notifies when complete. JOBS history: 8/6/4 all ≥50% pass2-timeout fail rate; 3 = 0% but slow (~2/hr); 5 = ~18% fail at ~3/hr (winner). Overridable via `JOBS=N` in `.env`.

- **Triggers:**
  - `consolidate-watch.service` — inotify on media root; fires on `close_write`/`moved_to` for `.mkv`/`.mp4` and sidecar `.srt`/`.ass`/`.vtt`.
  - Cron 05:00 daily — full library sweep with `--jobs 2` as backstop.
  - Cron `*/15` — normalize-driver.sh keeps the loudness sweep alive.
  - Cron 04:30 — `bazarr-profile-audit.py` fixes silently-orphaned profileId=None items.
  - Cron 04:45 — `movie-dedupe.py --apply --notify` resolves duplicate movie
    files left behind when Radarr upgrade-imports over a pipeline-renamed old
    file (Jellyfin would otherwise list both).  Auto-resolves only the SAFE
    case (Radarr already tracks the keeper → moves the leftover to the
    recoverable `.dupe-recycle/`); flags the RISKY case (Radarr tracking a
    non-keeper) for manual review (`movie-dedupe.py --apply --force`).
    `consolidate-watch` excludes `.dupe-recycle` so recycled files aren't
    re-normalized.
  - Cron `*/5` — `sync-forwarded-port.py` keeps qBittorrent's listen port equal
    to the port ProtonVPN currently forwards.  `VPN_PORT_FORWARDING=on` plus
    `PORT_FORWARD_ONLY=on`: Proton only forwards on its P2P servers and country
    selection alone does not prefer them, so without the filter gluetun **can**
    land on a healthy server that will never grant a port.  The allocation is
    dynamic, so no static port is configured for it: gluetun opens the allocated
    port in its own firewall, and the reconciler polls `GET /v1/portforward` on
    gluetun's control server via `docker exec`.  (qBittorrent's stored
    `listen_port` holds the last allocation the reconciler wrote; before the
    first successful allocation it was still the old static 54321.)  Control-server routes are all
    private by default in this version; `ops/gluetun/auth-config.toml` grants
    that ONE read-only route, and `HTTP_CONTROL_SERVER_ADDRESS=127.0.0.1:8000`
    pins the listener to loopback because gluetun otherwise binds `:::8000`,
    which is reachable from the host on the container IP.
  - **PREREQUISITE (met 2026-09-16 MDT / 2026-09-17 UTC):** Proton hands the port out over NAT-PMP,
    so the WireGuard key must be generated with **NAT-PMP (port forwarding)
    enabled** in Proton's dashboard, and with **Moderate NAT off** (Proton's own
    docs: you cannot use port forwarding and moderate/Type 2 NAT together) on a
    **Standard, not Secure Core** server (of 2096 servers in gluetun's list, 964
    do port forwarding and 876 are Secure Core, and **zero** are both).  The
    original key predated this and failed with `10.2.0.1:5351: recvfrom:
    connection refused`; it was replaced.  Regenerating the key is NOT
    self-healing: gluetun never retries a failed initial allocation, so a new key
    requires recreating gluetun and its seven netns dependents.
  - **Verified working end to end 2026-09-16 MDT (2026-09-17 UTC in container logs):** allocation succeeded (port
    37109), the reconciler moved qBittorrent's `listen_port` 54321 -> 37109, and
    inbound was proven from OUTSIDE the tunnel by dialling the VPN exit IP from
    the host, which egresses over its own ISP link: the forwarded port accepted
    the connection while a control dial to the non-forwarded 54321 timed out.
    Re-verify that way rather than trusting qBittorrent's `connection_status`,
    which reports "connected" even when nothing can reach it.
  - Cron 04:50 — `tv-dedupe.py --apply --notify`, the Sonarr sibling of
    movie-dedupe.py (runs 5 min later so they never overlap). Same
    SAFE/RISKY model, one TV-specific addition: Sonarr's `episodefile` DB
    table can retain an orphan row for the removed leftover even when the
    correct file is tracked (observed live 2026-07-26, Rick and Morty
    S09E01), so `tv-dedupe.py` also deletes that row via the Sonarr API
    after the physical move. Recycles to `.dupe-recycle/tv/`; RISKY cases
    need manual review (`tv-dedupe.py --apply --force`).
  - Cron Saturday 03:15 (weekly) — `search-missing.py`, the search Sonarr and
    Radarr never repeat on their own: each searches an item once, when it is
    added, then only watches RSS for new uploads, so a request whose first
    search found nothing (or whose download was reaped) stayed "processing" in
    Jellyseerr indefinitely (12 released movies and 282 aired episodes on
    2026-09-26).  Sonarr gets its own `MissingEpisodeSearch` (aired episodes
    only).  Radarr does NOT get `MissingMoviesSearch`: the script sends
    `MoviesSearch` for an explicit list of movies that are monitored, fileless,
    released and not already queued, because the quality profile approves
    pre-release junk for films still in cinemas and a manual search does not
    reject it.  "Released" is stricter than Radarr's word for it: `status ==
    "released"`, `isAvailable`, AND a past `digitalRelease` or
    `physicalRelease`.  `isAvailable` alone follows each movie's
    `minimumAvailability` ("In Cinemas" reports available at the theatrical
    date), and both Radarr flags fall back to "90 days after cinemas" when no
    home-release date is known.  269 of 270 released movies had one.  `--dry-run` shows the picks without queueing anything.

- **Bazarr:** English profile (id=1) attached to everything. Sidecar SRTs ingested by the watcher.

- **Legacy retired 2026-06-01:** `clean-subs.py` was deprecated 2026-05-15
  and deleted in the cleanup pass.  All v2 functionality lives in
  `consolidate-subs.py`.  Pre-v2 implementations are in git history
  (`consolidate-subs.py.pre-v2.bak` was also removed — git history is
  the source of truth).

## Jellyfin admin ops

- **API key minting:** see the `jellyfin-mint-api-key` skill (loads on demand).

## Agent Operating Rules

- **No secrets in code or docs.** Always reference `.env` variable names.
- **No `nohup` alone for jobs that must outlive a Claude session** — always wrap with `setsid` (lesson learned 2026-05-14). See `feedback_setsid_long_bg` in user memory.
- **Long sweeps survive Claude bounces** because `normalize-driver.sh` is cron-driven, not session-driven.
- **Don't touch `consolidate-subs.py` mid-sweep** — review-loop gate applies, and live writes corrupt state.
- **Don't change the R128 -23 LUFS target without approval** — verified against entire library, switch would invalidate all coverage tags.
- **Times reported to the user go in 12-hour AM/PM in the operator's local timezone.** Internal log timestamps follow the host timezone (set via `timedatectl`); cron schedules are likewise interpreted in the host TZ.
- **Verify before claiming UI works** — type-checks and tests verify correctness, not behavior. For UI changes, exercise the feature in a browser before reporting success.

## Common Pitfalls

- Bazarr `profileId: None` items are silently invisible to sub search — has bitten 3x. Daily `bazarr-profile-audit.py` cron is the backstop; manual `bazarr-profile-audit.py` after big imports.
- After NVIDIA driver branch purge: `apt autoremove` will yank the active stack. `apt-mark manual` the active branch first.
- Mergerfs branches: `/mnt/disk{1,2,3}` are pool members; high per-branch `df` is not "data outside the pool".
- Claude session bounce + `nohup ... &` background = SIGKILL (logind/cgroup cleanup). Use `setsid`.
- `pgrep` regex: include `--scan.*movies` not just `normalize-audio.py` (the wire-in path also runs single-file invocations).

## Maintenance

- **Updates:** Host via `aptitude` (nightly 04:00 via `nightly-upgrade.sh`). Containers via Watchtower (24h cycle). See `$HOME/.claude/CLAUDE.md` for the home-server policy.
- **Power:** `restart: unless-stopped` on all containers.
- **Backups:** none configured for media (intentional — re-fetchable). Service config under `config/` should be snapshotted before destructive changes.

### Trade-off: `:latest` images + Watchtower auto-update (explicit)

Container images in `docker-compose.yml` use `:latest` tags with Watchtower
performing 24h auto-update.  This is a **deliberate choice**, not an
oversight, and was the subject of recurring codex findings (round-2 #9 /
round-3 #13).  The decision: freshness > reproducibility on this server.

**Watchtower fork (2026-05-23):** the `watchtower` service uses
`nickfedor/watchtower:latest`, the actively-maintained community fork.
Upstream `containrrr/watchtower` is abandoned — its last image ships a
Docker API client at version 1.25, which Docker daemon 29.x rejects (it
requires minimum 1.44).  After today's nightly upgrade bumped Docker
daemon 27.5.1 → 29.1.3, the upstream image entered a permanent restart
loop with `client version 1.25 is too old`.  The fork ships Watchtower
1.17.1 using API v1.52 — same config interface, same volumes, drop-in
replacement.  Same precedent as the jellyseerr → seerr-team swap in
`reference_jellyseerr_fork.md`.

The auto-update can in principle silently break an integration (a service
changes its API path, container layout, config schema, …).  We accept that
risk because:

1. The "zero-touch" operating mode is the design goal — pinning fights it
2. No incident history (as of 2026-05-19) where Watchtower broke us
3. If something does break, `nightly-upgrade.sh` logs + healthcheck.sh
   exit codes surface it fast; recovery is `docker compose down && up -d
   <service>:<known-good-tag>`

If you replicate this stack and want reproducibility instead, pin tags or
digests in your fork of `docker-compose.yml` and disable Watchtower.  We
don't — the full rationale is the trade-off described above.

## Status / Health Probes

Two layers — human-readable status scripts for narrative output, and the
machine-readable `healthcheck.sh` aggregator that returns exit codes.

- `bash $HOME/media-stack/healthcheck.sh` — aggregated pass/warn/fail
  with exit code (0/1/2).  Wire into monitoring or run pre-deploy.  Add
  `--verbose` for per-check OK lines, `--json` for machine-readable output.
- `bash $HOME/media-stack/consolidate-status.sh` — pipeline snapshot
- `bash $HOME/media-stack/normalize-status.sh` — loudness coverage
- `bash $HOME/media-stack/preflight.sh` — once at install time + after
  host upgrades: verifies every required dependency (ffmpeg, mkvtoolnix,
  ffsubsync, subliminal, inotifywait, docker, …)
- `bash $HOME/media-stack/ops/audit.sh` — diff live cron + systemd
  against the snapshots tracked under `ops/`.  Fails on drift.
- `docker compose ps` — container health
- `docker exec gluetun wget -qO- https://ifconfig.co` — verify VPN egress (must NOT be host IP)
- `tail -20 var/log/stall_killer.log` — qBit reaper activity

## Agent Validation Checklist

Before declaring any code change "done," run the validation appropriate to
the change.  This list is the answer to "which command proves my edit is
acceptable?"

- **Python script edits (`*.py`)**:
  - `python3 -c "import ast; ast.parse(open('FILE').read())"` — syntax
  - `python3 -m pytest tests/ -q` — unit tests (all pytest cases under `tests/` must pass; count grows over time)
  - `ruff check media_stack/ *.py` — lint (config + per-file-ignores in `pyproject.toml`; must stay clean)
  - `mypy` — type-check `media_stack/` (permissive baseline in `pyproject.toml`; must stay clean)
  - Single-file smoke test via `python3 FILE --help` or `--dry-run`
  - Dev tools (`ruff`, `mypy`, `pytest`) come from `requirements-dev.txt`:
    `pip3 install --user -r requirements-dev.txt`
- **Shell script edits (`*.sh`)**:
  - `bash -n FILE` — syntax
  - `shellcheck FILE` if available (not installed by default; flag as
    optional improvement, not blocker)
- **Docker Compose changes (`docker-compose.yml`)**:
  - `docker compose config` — verify it parses
  - `docker compose up -d <changed-service>` — bounce just that service
  - `bash healthcheck.sh` to verify container + VPN + invariants still pass
- **Cron / systemd changes**:
  - Update both the live state AND the snapshot under `ops/`
  - `bash ops/audit.sh` — confirm no drift
- **Media pipeline dry runs**:
  - Before sweeping the library: pick one file, run
    `python3 consolidate-subs.py --dry-run <one-file.mkv>`, eyeball the
    plan, then run for real on that single file
  - Never invoke `consolidate-subs.py --scan` while a sweep is in flight
    (state lock now protects against the most damaging race, but
    overlapping ffmpeg invocations still thrash the HDD)
