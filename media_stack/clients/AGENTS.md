# media_stack/clients/: external service adapters

Thin wrappers over the in-stack HTTP APIs (Arr, Bazarr, qBittorrent, Jellyfin, Telegram) that centralize auth, pagination and timeout for host-side scripts. No retry/backoff: transient failures surface as `None` (or a status string) and callers decide whether to retry or alert.

## Adapter notes

- **Auth:** Arr `X-Api-Key`; Bazarr `X-API-KEY` (`apikey_from_container()` reads it by `docker exec`); qBittorrent session cookie (`login()` is a no-op when `username == ""`).
- **Bazarr writes** (`assign_movie_profile` / `assign_series_profile` / `trigger_task`) take a form-encoded body, NOT JSON; the daily profile audit must go through the client, not raw urllib.
- **Jellyfin:** `Authorization: MediaBrowser Token="<key>"`, passed **per call** (NOT `X-Emby-Token`, NOT `?api_key=`: this Jellyfin answers both with 401). `JellyfinClient` binds only the base URL (the key minter verifies MANY keys per run); `jellyfin-mint-api-key.py` keeps thin compat shims that delegate here. `list_keys()` carries the security-critical shape validation (require `Items` on dict responses; reject non-string/empty AccessToken+AppName; never echo response bodies in errors).
- **`JellyfinClient.now_playing_sessions(key)`** (the playback gate of `sub-fallback.py` / `audio-lang-check.py`) never raises and FAILS CLOSED: `None` on ANY failure (unreachable, bad key, non-2xx, non-list body, any non-dict element such as `[null]`, a `NowPlayingItem` that is PRESENT but not a non-empty dict), `[]` only for a CONFIRMED empty list. Callers MUST check `None` explicitly (`None` and `[]` are both falsy) and treat it as "someone is watching".
- **Telegram:** `send(token, chat_id, text)` only (one-way notifications; keep it minimal). The CALLER loads the bot token from `$HOME/.claude/channels/telegram/.env`, INTENTIONALLY outside this repo so the live token never sits in the stack `.env` or git.

## Rules

- **One adapter per service.** Don't duplicate auth across files; scripts needing the same endpoint share the client.
- **Tri-state returns where ambiguity matters.** `ArrClient.remove_by_download_id` returns `"removed" | "not_found" | "queue_error" | "delete_failed"` so callers can tell "nothing matched, fine" from "matched but DELETE failed: do NOT fall through to a direct qBit delete or the next grab re-imports the same torrent". Boolean returns are a regression risk.
- **Pagination is mandatory** for any v3 endpoint that supports it (`ArrClient.get_queue`, `pageSize=1000`: a single-page read misses page-2+ torrents and lets `nuke_stalled` skip blocklisting). Stop in priority order: (1) `totalRecords` known and `len(records) >= totalRecords`; (2) `totalRecords` known and still ahead: KEEP paginating even on a short page (Arr can server-cap `pageSize`); (3) `totalRecords` absent and short page; (4) empty page; (5) 100k safety cap. A 200 with no `records` list or non-object records is malformed, not empty, and raises, as does an empty page while `totalRecords` says more remain or a present non-integer `totalRecords` (null included); only an ABSENT key falls back to rule 3.
- **Timeouts on every request** (default 15 s on Arr/Bazarr; qBit inherits it). No unbounded `requests.get`.
- **Swallow network errors at the boundary** (e.g. `BazarrClient.movies()` returns `None`, `reachable_status()` returns `"000"`): never propagate raw `requests` exceptions.
- **Never log API keys.** Keys come from `.env` or `docker exec bazarr cat /config/config/config.yaml`; never echo them to stdout, `print(headers)`, or exception messages. Shell callers pass keys to `curl` via a `0600` curl config file, not an `-H` argv (pattern: `consolidate-status.sh`).
- **Tests** live in `tests/test_clients.py`: mock `requests.get`/`post`/`delete`, no real network; pagination regressions get explicit fixtures (a `page1` with exactly `pageSize` records and a higher `totalRecords`, then `page2`).
- **New adapter only when** 2+ host-side scripts call the service, its auth is non-trivial, or its endpoint has pagination/retry semantics; an unauthenticated JSON probe can inline `requests.get`.
