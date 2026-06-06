# media_stack/clients/ — external service adapters

Thin wrappers over the HTTP APIs of the in-stack services.  Centralizes
auth, pagination, and timeout so the host-side scripts (`nuke_stalled.py`,
`bazarr-profile-audit.py`, `healthcheck.sh`) don't re-implement them.
No retry/backoff is implemented — transient failures surface as `None`
return values; callers decide whether to retry or alert (codex round-16
#3: previous docs claimed retry handling but the code was timeout-only).

## Adapters

| File | Wraps | Auth | Notes |
| :--- | :--- | :--- | :--- |
| `arr.py` | Sonarr + Radarr `/api/v3` | `X-Api-Key` header | Single client class; Sonarr/Radarr share the v3 surface.  `get_queue()` paginates with `pageSize=1000` (codex round-module-split-2 #2 — single-page query silently missed page-2+ torrents and let `nuke_stalled` fall through to a direct qBit delete that skips blocklisting). |
| `bazarr.py` | Bazarr `/api/*` | `X-API-KEY` header | `unprofiled_count()` returns `(movies, series)` counts of `profileId=None` items.  `assign_movie_profile` / `assign_series_profile` / `trigger_task` cover the writes the daily profile audit needs (form-encoded body, NOT JSON; codex round-4 module-split #3 — the audit was bypassing the client with raw urllib).  Helper `apikey_from_container()` extracts the key by `docker exec`-ing into the Bazarr container. |
| `qbit.py` | qBittorrent WebUI | session cookie (`POST /api/v2/auth/login`) | `login()` is a no-op when `username == ""` (config-allowed bypass).  Cookie persists on the `requests.Session` so subsequent calls reuse it. |
| `telegram.py` | Telegram Bot API | bot token loaded by the CALLER from `$HOME/.claude/channels/telegram/.env` (managed by the Claude telegram plugin — INTENTIONALLY outside this repo so the live token never sits in the stack `.env`) | Single `send(token, chat_id, text)` helper — keep it minimal; this is for normalize-driver / healthcheck-alert notifications, not bidirectional bot logic.  Token never persists in stack git or `.env`. |
| `jellyfin.py` | Jellyfin `/System/Info`, `/Auth/Keys`, `/Users/AuthenticateByName` | `X-Emby-Token` header, passed **per call** | `JellyfinClient` binds only the base URL — the API-key minter (`jellyfin-mint-api-key.py`) verifies MANY distinct keys in one run (bootstrap, new, each pre-existing), so the key is a per-method arg, not bound in `__init__`.  `urllib`-based (no `requests`).  `list_keys()` carries the security-critical shape validation (require `Items` on dict responses; reject non-string/empty AccessToken+AppName; never echo response bodies in errors).  The minter keeps thin `_api`/`_login`/`list_keys`/`mint_key` compat shims that delegate here, preserving its long-verified test surface. |

## Operating rules for adapters

- **One adapter per service.**  Don't duplicate auth across files.  If
  two scripts need the same endpoint, both go through the same client.
- **Tri-state returns where ambiguity matters.**  `ArrClient.remove_by_download_id`
  returns `"removed" | "not_found" | "queue_error" | "delete_failed"`
  so the caller can distinguish "nothing matched, fine" from "matched
  but DELETE failed, do NOT fall through to a direct qBit delete or
  the next grab will re-import the same torrent" (codex round-module-
  split #3).  Boolean returns are a regression risk — be explicit.
- **Pagination is mandatory** for any v3 endpoint that supports it.
  Default `pageSize=1000`.  Stop conditions in priority order:
    1. `totalRecords` known AND `len(records) >= totalRecords` → stop.
    2. `totalRecords` known AND still ahead → KEEP paginating even
       on a short page (Arr can server-cap pageSize below the request
       — codex round-7 #2 caught the previous early-stop bug).
    3. `totalRecords` absent AND short page (< pageSize) returns →
       stop (this is the fallback for servers that omit totalRecords).
    4. Empty page → stop.
    5. 100k safety cap → stop.
  See `ArrClient.get_queue` for the canonical implementation +
  regression tests `test_arr_get_queue_keeps_paginating_on_short_page_when_total_is_known`
  and `test_arr_get_queue_short_page_fallback_when_no_total`.
- **Timeouts on every request.**  Default 15 s on Arr/Bazarr; qBit
  inherits the same.  No unbounded `requests.get`.
- **Swallow network errors at the boundary.**  `system_status()` etc.
  return `None` on any exception so callers can treat reachability as
  a boolean.  Don't propagate raw `requests` exceptions out — the
  callers already have to handle the missing-data case.
- **Never log API keys.**  Keys come from `.env` or
  `docker exec bazarr cat /config/config/config.yaml` — never echo to
  stdout, never `print(headers)`, never include them in exception
  messages.  Same rule applies to passing keys to `curl` from shell
  callers — use a `0600` curl config file, not an `-H` argv (codex
  round-3 #6, see `consolidate-status.sh` for the pattern).

## Testing expectations

- Tests live in `tests/test_clients.py`.  Mock `requests.get` /
  `requests.post` / `requests.delete` with `unittest.mock.patch` —
  no real network from the test suite.
- Pagination regressions get explicit fixtures: build a `page1`
  response with exactly `pageSize` records and `totalRecords` set
  higher, then a `page2` with the rest.  The 2026-05-19 pagination
  miss had no test; now it has two.

## When to add a new adapter

- A new in-stack service needs to be called from 2+ host-side
  scripts.  Single-caller boilerplate doesn't need an adapter yet.
- The auth pattern is non-trivial (cookies, OAuth, signed headers).
  Inlining `requests.get` is fine for an unauth'd JSON probe.
- The endpoint has pagination, retry semantics, or other concerns
  the call site shouldn't have to know.
