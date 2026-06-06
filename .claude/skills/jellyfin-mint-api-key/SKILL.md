---
name: jellyfin-mint-api-key
description: Mint a new Jellyfin admin API key safely (with pre/post-flight verification). Use when the user asks to create, mint, or generate a new Jellyfin API key.
---

# Minting a Jellyfin admin API key

Use `jellyfin-mint-api-key.py` for any new admin API key — don't INSERT
directly into the SQLite `ApiKeys` table.

The helper runs pre/post-flight verifications: snapshots existing keys,
rejects duplicate names, confirms every pre-existing key STILL works after
the mint, and confirms a known admin user can still log in WITH
`IsAdministrator=true`.

**Required env:**
- `JELLYFIN_API_KEY` — an existing admin key for auth.
- `JELLYFIN_VERIFY_USER` + `JELLYFIN_VERIFY_PW` — for the admin-login round-trip.

**Optional env:**
- `JELLYFIN_URL` — defaults to `http://{MEDIA_LAN_IP}:8096` from `media_stack.paths`.

Exit codes are documented in the script header. A non-zero exit never
auto-rolls-back — manual revocation via Dashboard → API Keys is more
reliable than a scripted unwind.

Unit tests: `tests/test_jellyfin_mint_api_key.py`.
