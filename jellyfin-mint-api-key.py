#!/usr/bin/env python3
"""Mint a new Jellyfin API key via the HTTP API (never raw SQL, which can corrupt the DB).

Snapshots the existing keys first, then verifies that the new key works, every pre-existing key
still works, and an admin user can still log in. On a failed verification it exits non-zero
WITHOUT rolling back: revoke manually in the Jellyfin admin UI, comparing against the printed
pre-flight snapshot.

Usage (read secrets without echo, to keep them out of shell history and `ps` argv):

    read -rsp 'JELLYFIN_API_KEY: '     JELLYFIN_API_KEY     && echo
    read -rsp 'JELLYFIN_VERIFY_USER: ' JELLYFIN_VERIFY_USER && echo
    read -rsp 'JELLYFIN_VERIFY_PW: '   JELLYFIN_VERIFY_PW   && echo
    export JELLYFIN_API_KEY JELLYFIN_VERIFY_USER JELLYFIN_VERIFY_PW
    python3 jellyfin-mint-api-key.py [--dry-run] <new-key-name>
    unset JELLYFIN_API_KEY JELLYFIN_VERIFY_USER JELLYFIN_VERIFY_PW

Env:
    JELLYFIN_API_KEY        existing admin-scoped API key. REQUIRED.
    JELLYFIN_URL            base URL (default: http://$MEDIA_LAN_IP:8096)
    JELLYFIN_VERIFY_USER    username for the post-mint login round-trip.
    JELLYFIN_VERIFY_PW      that user's password. Both REQUIRED unless --skip-login-test.

Exit codes:
    0   key minted + all verifications passed
    1   pre-flight failed (existing key invalid, server unreachable)
    2   mint failed (non-2xx); the new key MAY have landed
    3   post-flight verification failed; the new key MAY have landed, so reconcile manually
        from the printed before/after snapshot
    4   bad usage (missing env, bad argv)

The new key is printed once, at the end, on success. Capture it immediately.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from media_stack.clients.jellyfin import JellyfinClient, NetworkError


def _default_url() -> str:
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from media_stack.paths import MEDIA_LAN_IP
        return f"http://{MEDIA_LAN_IP}:8096"
    except Exception:
        # same literal as media_stack.paths' fallback
        return "http://10.0.0.10:8096"


DEFAULT_URL = _default_url()


# Thin shims over JellyfinClient, kept for this script's call surface and tests.
def _api(base: str, path: str, key: str, *, method: str = "GET",
         body: dict | None = None, timeout: int = 10) -> tuple[int, bytes]:
    return JellyfinClient(base, timeout=timeout).request(
        path, key, method=method, body=body)


def _login(base: str, username: str, password: str, *,
           timeout: int = 10) -> tuple[int, bytes]:
    return JellyfinClient(base, timeout=timeout).authenticate(username, password)


def list_keys(base: str, key: str) -> list[dict]:
    return JellyfinClient(base).list_keys(key)


def _redact(token: str) -> str:
    """Last 4 chars only; tokens of 4 or fewer get a fixed marker (`token[-4:]` would reveal them whole)."""
    if not token:
        return ""
    if len(token) <= 4:
        return "…<short-redacted>"
    return f"…{token[-4:]}"


def mint_key(base: str, key: str, name: str) -> None:
    JellyfinClient(base).create_key(key, name)


class _UsageError(Exception):
    """Maps argparse failures to the documented rc=4 instead of argparse's 2."""


class _UsageParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        self.print_usage(sys.stderr)
        raise _UsageError(message)


def main() -> int:
    p = _UsageParser(description=__doc__,
                     formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("name", help="Name for the new API key (e.g. 'claude-ops')")
    p.add_argument("--dry-run", action="store_true",
                   help="Run pre-flight + verifications without minting")
    p.add_argument("--skip-login-test", action="store_true",
                   help="Skip the post-mint admin login round-trip")
    try:
        args = p.parse_args()
    except _UsageError as e:
        print(f"FATAL: usage: {e}", file=sys.stderr)
        return 4

    base = os.environ.get("JELLYFIN_URL", DEFAULT_URL).rstrip("/")
    bootstrap_key = os.environ.get("JELLYFIN_API_KEY", "")
    verify_user = os.environ.get("JELLYFIN_VERIFY_USER", "")
    verify_pw = os.environ.get("JELLYFIN_VERIFY_PW", "")
    if not bootstrap_key:
        print("FATAL: JELLYFIN_API_KEY env var required", file=sys.stderr)
        return 4
    if not args.skip_login_test and (not verify_user or not verify_pw):
        print("FATAL: JELLYFIN_VERIFY_USER + JELLYFIN_VERIFY_PW required "
              "(or pass --skip-login-test)", file=sys.stderr)
        return 4
    # strip once: an untrimmed `" Jellyseerr "` would bypass the duplicate check
    args.name = args.name.strip()
    if not args.name:
        print("FATAL: key name must be non-empty", file=sys.stderr)
        return 4

    print(f"[pre-flight] base URL: {base}", file=sys.stderr)
    print("[pre-flight] verifying bootstrap key works...", file=sys.stderr)
    # every pre-flight failure must surface as rc=1 with a FATAL line, not a traceback
    try:
        status, raw = _api(base, "/System/Info", bootstrap_key)
    except NetworkError as e:
        print(f"FATAL: cannot reach Jellyfin: {e}", file=sys.stderr)
        return 1
    if status != 200:
        print(f"FATAL: bootstrap key check failed ({status}): {raw[:200]!r}",
              file=sys.stderr)
        return 1
    try:
        info = json.loads(raw)
    except ValueError as e:
        print(f"FATAL: /System/Info returned non-JSON: {type(e).__name__}: "
              f"{e} ({raw[:200]!r})", file=sys.stderr)
        return 1
    print(f"[pre-flight] connected to {info.get('ServerName')} v{info.get('Version')}",
          file=sys.stderr)

    print("[pre-flight] snapshotting current ApiKeys table...", file=sys.stderr)
    try:
        pre_keys = list_keys(base, bootstrap_key)
    except NetworkError as e:
        print(f"FATAL: cannot reach Jellyfin during snapshot: {e}", file=sys.stderr)
        return 1
    except (RuntimeError, ValueError) as e:
        print(f"FATAL: pre-flight snapshot failed: {type(e).__name__}: {e}",
              file=sys.stderr)
        return 1
    print(f"[pre-flight] found {len(pre_keys)} existing key(s):", file=sys.stderr)
    for k in pre_keys:
        print(f"           - Name={k.get('AppName')!r} token={_redact(k.get('AccessToken',''))} "
              f"created={k.get('DateCreated')}", file=sys.stderr)
    pre_names = {k.get("AppName") for k in pre_keys}
    if args.name in pre_names:
        print(f"FATAL: a key named {args.name!r} already exists — refusing to "
              f"add a duplicate (would shadow the existing one)", file=sys.stderr)
        return 4
    pre_tokens = {k.get("AccessToken") for k in pre_keys}

    if args.dry_run:
        print("[dry-run] would POST /Auth/Keys?App="
              + urllib.parse.quote(args.name, safe=""), file=sys.stderr)
        print("[dry-run] would verify: new key works + all pre-existing keys "
              "still work + admin login still works", file=sys.stderr)
        return 0

    # all three map to rc=2 "MAY have landed": the error site can't tell whether the POST ran
    print(f"[mint] POST /Auth/Keys?App={args.name!r}...", file=sys.stderr)
    try:
        mint_key(base, bootstrap_key, args.name)
        post_keys = list_keys(base, bootstrap_key)
    except (NetworkError, RuntimeError, ValueError) as e:
        print(f"FATAL: mint/post-list failed: {type(e).__name__}: {e}",
              file=sys.stderr)
        print("       The mint MAY have landed — check Jellyfin admin UI "
              "(Dashboard → API Keys) to confirm + revoke if unexpected.",
              file=sys.stderr)
        return 2
    new_keys = [k for k in post_keys
                if k.get("AccessToken") not in pre_tokens]
    if len(new_keys) != 1 or new_keys[0].get("AppName") != args.name:
        print(f"FATAL: expected exactly 1 new key named {args.name!r}, got "
              f"{len(new_keys)}: {[(k.get('AppName'), _redact(k.get('AccessToken',''))) for k in new_keys]}",
              file=sys.stderr)
        return 2
    new_token = new_keys[0]["AccessToken"]
    print(f"[mint] new key created: Name={args.name!r} token={_redact(new_token)}",
          file=sys.stderr)

    # the mint is proven to have landed, so failures here are rc=3, not rc=2
    print("[verify] confirming new key works against /System/Info...",
          file=sys.stderr)
    try:
        status, _ = _api(base, "/System/Info", new_token)
    except NetworkError as e:
        print(f"FATAL: verify of new key failed (network): {e}", file=sys.stderr)
        return 3
    if status != 200:
        print(f"FATAL: new key failed verification: HTTP {status}", file=sys.stderr)
        return 3
    print("[verify]   new key: OK", file=sys.stderr)

    print(f"[verify] confirming all {len(pre_keys)} pre-existing key(s) still work...",
          file=sys.stderr)
    for k in pre_keys:
        tok = k.get("AccessToken", "")
        name = k.get("AppName", "?")
        try:
            status, _ = _api(base, "/System/Info", tok)
        except NetworkError as e:
            print(f"FATAL: verify of pre-existing key {name!r} "
                  f"({_redact(tok)}) failed (network): {e}", file=sys.stderr)
            return 3
        if status != 200:
            print(f"FATAL: pre-existing key {name!r} ({_redact(tok)}) STOPPED "
                  f"working: HTTP {status}", file=sys.stderr)
            return 3
        print(f"[verify]   {name!r} ({_redact(tok)}): OK", file=sys.stderr)

    if not args.skip_login_test:
        print(f"[verify] confirming login as {verify_user!r} still works "
              f"AND retains IsAdministrator...", file=sys.stderr)
        try:
            code, auth_body = _login(base, verify_user, verify_pw)
        except NetworkError as e:
            print(f"FATAL: login test failed (network): {e}", file=sys.stderr)
            return 3
        if code != 200:
            print(f"FATAL: login as {verify_user!r} returned HTTP {code} "
                  f"(expected 200) — admin auth may be broken", file=sys.stderr)
            return 3
        # strict `is True`: a truthy string ("false", "0") must not pass as admin
        try:
            auth_data = json.loads(auth_body)
            policy = (auth_data.get("User") or {}).get("Policy") or {}
            is_admin = policy.get("IsAdministrator") is True
        except (ValueError, AttributeError) as e:
            print(f"FATAL: could not parse auth response to check admin "
                  f"policy: {type(e).__name__}: {e}", file=sys.stderr)
            return 3
        if not is_admin:
            # never echo the raw value: response-derived, could leak token-like content into the
            # Telegram alert wrapper
            policy_present = (auth_data.get("User") or {}).get("Policy") is not None
            ia_present = "IsAdministrator" in policy
            ia_type = type(policy.get("IsAdministrator")).__name__
            print(f"FATAL: login as {verify_user!r} succeeded but "
                  f"IsAdministrator is not exactly True — admin policy "
                  f"was disrupted (Policy present={policy_present}, "
                  f"IsAdministrator present={ia_present}, type={ia_type})",
                  file=sys.stderr)
            return 3
        print(f"[verify]   {verify_user!r}: OK (admin=True)", file=sys.stderr)

    print("", file=sys.stderr)
    print(f"==== new Jellyfin API key (name={args.name!r}) — capture now ====")
    print(new_token)
    print("===================================================================")
    return 0


if __name__ == "__main__":
    sys.exit(main())
