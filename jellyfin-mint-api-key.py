#!/usr/bin/env python3
"""Mint a new Jellyfin API key with safety verifications.

Adds a new row to Jellyfin's ApiKeys table via the documented HTTP API
(NOT direct SQL writes — the API does the right thing with internal
references; raw SQL can corrupt the DB).  Runs a pre-flight snapshot
of existing keys + a post-flight verification that:

  1. The new key works.
  2. EVERY pre-existing key STILL works (no accidental clobber).
  3. A known admin user can still log in (no policy disruption).

If any verification fails, the script EXITS NON-ZERO but does NOT
auto-rollback — manual revocation via Jellyfin admin UI is more
reliable than a script unwinding a partial state.  The pre-flight
snapshot is printed so the operator can compare to the live state
and revoke anything unexpected.

Usage:
    JELLYFIN_API_KEY=<existing-admin-key> \
    JELLYFIN_VERIFY_USER=<admin-username> \
    JELLYFIN_VERIFY_PW=<that-user's-password> \
        python3 jellyfin-mint-api-key.py [--dry-run] <new-key-name>

Safer invocation (codex round 4 #3 — keeps secrets out of shell
history + `ps`-style argv inspection; prompts read no-echo):

    read -rsp 'JELLYFIN_API_KEY: '     JELLYFIN_API_KEY     && echo
    read -rsp 'JELLYFIN_VERIFY_USER: ' JELLYFIN_VERIFY_USER && echo
    read -rsp 'JELLYFIN_VERIFY_PW: '   JELLYFIN_VERIFY_PW   && echo
    export JELLYFIN_API_KEY JELLYFIN_VERIFY_USER JELLYFIN_VERIFY_PW
    python3 jellyfin-mint-api-key.py [--dry-run] <new-key-name>
    unset JELLYFIN_API_KEY JELLYFIN_VERIFY_USER JELLYFIN_VERIFY_PW

Env contract:
    JELLYFIN_API_KEY        existing admin-scoped API key used to auth
                            this script's calls.  REQUIRED.
    JELLYFIN_URL            base URL (default: http://10.0.0.10:8096)
    JELLYFIN_VERIFY_USER    username to round-trip a login test against
                            after minting.  REQUIRED unless --skip-login-test.
    JELLYFIN_VERIFY_PW      that user's password.  REQUIRED unless
                            --skip-login-test.

Exit codes:
    0   key minted + all verifications passed
    1   pre-flight failed (existing key invalid, server unreachable)
    2   mint failed (server returned non-2xx)
    3   post-flight verification failed — the new key MAY have landed
        but something else is wrong; check the printed before/after
        snapshot and manually reconcile
    4   bad usage (missing env, bad argv)

The new key is printed ONLY at the end on success, ONCE, prefixed
with a clear marker.  Capture immediately; the key is NOT retrievable
later (Jellyfin only stores the value when minted).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
from pathlib import Path

# media_stack lives next to this script; make it importable whether run
# directly, via the Jellyfin wire-in, or loaded by the pytest harness.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from media_stack.clients.jellyfin import JellyfinClient, NetworkError


# Default base URL derives from the shared host identity helper so this
# script follows the same .env contract as the rest of the stack
# (codex round 2 #6: previous hardcoded 10.0.0.10 would drift if
# MEDIA_LAN_IP ever changed).  JELLYFIN_URL env var still overrides.
def _default_url() -> str:
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from media_stack.paths import MEDIA_LAN_IP
        return f"http://{MEDIA_LAN_IP}:8096"
    except Exception:
        # Fallback: same default the rest of the stack uses if for some
        # reason media_stack isn't importable (running from a stripped-
        # down copy).  Same literal as media_stack.paths' fallback.
        return "http://10.0.0.10:8096"


DEFAULT_URL = _default_url()


# --- thin compat shims over media_stack.clients.jellyfin.JellyfinClient ---
# The HTTP + response-shape validation now lives in the adapter; these
# one-line wrappers preserve this script's long-verified call surface (and
# its unit tests), so the safety-critical mint flow in main() is unchanged.
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
    """Show only the last 4 chars of a token in logs/snapshots so we
    can spot the SAME key across snapshots without ever printing the
    full value.

    Short tokens (len ≤ 4) get a fixed marker instead — codex round 2
    #5 caught that `f"…{token[-4:]}"` for a 3-char token surfaced
    the whole value with just an ellipsis prefix.  Even short/test
    tokens are still secrets; never reveal them in full.
    """
    if not token:
        return ""
    if len(token) <= 4:
        return "…<short-redacted>"
    return f"…{token[-4:]}"


def mint_key(base: str, key: str, name: str) -> None:
    JellyfinClient(base).create_key(key, name)


class _UsageError(Exception):
    """Raised by `_UsageParser.error` so main() can map argparse
    failures to the documented rc=4 instead of argparse's default 2
    (codex round 3 #4)."""


class _UsageParser(argparse.ArgumentParser):
    """ArgumentParser that raises instead of `sys.exit(2)`-ing on
    usage errors, so main() controls the exit code."""
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
    # Codex round 4 #5: normalize ONCE here.  Previously checked
    # `.strip()` for emptiness but used the original (untrimmed)
    # `args.name` for duplicate detection + minting, so `" Jellyseerr "`
    # would bypass dup-check and create a confusing near-duplicate.
    args.name = args.name.strip()
    if not args.name:
        print("FATAL: key name must be non-empty", file=sys.stderr)
        return 4

    # --- pre-flight ---
    print(f"[pre-flight] base URL: {base}", file=sys.stderr)
    print("[pre-flight] verifying bootstrap key works...", file=sys.stderr)
    # Codex round 1 #2 + round 3 #1: catch NetworkError (transport),
    # ValueError (json.loads on non-JSON body), AND RuntimeError
    # (list_keys non-200 or unexpected shape) so EVERY pre-flight
    # failure surfaces as the documented rc=1 with a FATAL line
    # instead of an uncontrolled traceback.
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
        # RuntimeError: list_keys saw a non-200 OR an unexpected shape.
        # ValueError: list_keys's json.loads on a non-JSON body.
        # Either way the bootstrap key path is broken — refuse to
        # proceed; minting would then fail to verify anyway.
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

    # --- mint ---
    # Codex round 2 #2 + round 5 #1: catch NetworkError, RuntimeError
    # AND ValueError around the mint POST + post-mint list.  Round 2
    # added the first two but missed ValueError (json.loads on a
    # truncated/non-JSON post-list response would still escape).
    # All three map to rc=2 + the "MAY have landed" diagnostic because
    # at this point the POST has either run or it hasn't, and we
    # can't tell from the error site — operator must check UI.
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

    # --- post-flight verifications ---
    # Same NetworkError handling — but a verify failure at this point
    # means the mint DID land (we proved it via post_keys above), so
    # network failure during verify maps to rc=3 not rc=2.
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
        # Codex round 2 #3: a user who can log in but lost admin
        # privileges would have passed a status-only check.  The stated
        # purpose is "admin auth still works"; enforce it.
        # Codex round 6 #2: use strict `is True` instead of `bool(...)`.
        # Any non-empty string ("false", "0", "no") is truthy in Python
        # so the bool() coercion would silently accept a spoofed or
        # malformed Policy that doesn't actually grant admin.  Only the
        # JSON literal `true` (which json.loads → Python `True`) counts.
        try:
            auth_data = json.loads(auth_body)
            policy = (auth_data.get("User") or {}).get("Policy") or {}
            is_admin = policy.get("IsAdministrator") is True
        except (ValueError, AttributeError) as e:
            print(f"FATAL: could not parse auth response to check admin "
                  f"policy: {type(e).__name__}: {e}", file=sys.stderr)
            return 3
        if not is_admin:
            # Codex round-debloat #1: do NOT echo the raw value.
            # IsAdministrator is response-derived; a malicious or
            # malformed proxy response could plant token-like content
            # there and we'd leak it via stderr → alert wrapper →
            # Telegram.  Report only structural metadata (type name +
            # present-or-missing flag) — that's enough for the
            # operator to diagnose.
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

    # --- success: print the new key ONCE at the end ---
    print("", file=sys.stderr)
    print(f"==== new Jellyfin API key (name={args.name!r}) — capture now ====")
    print(new_token)
    print("===================================================================")
    return 0


if __name__ == "__main__":
    sys.exit(main())
