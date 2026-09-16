#!/usr/bin/env python3
"""Keep qBittorrent's listen port equal to the port ProtonVPN forwards.

ProtonVPN's forwarded port is DYNAMIC: Proton's own documentation says the
number "usually changes when you reconnect to VPN", and gluetun re-requests the
mapping continuously (the lease is short-lived).  qBittorrent has no idea any of
that happened, so without something reconciling the two, inbound peer
connections silently stop working the first time the port changes.

Why a periodic reconciler rather than gluetun's VPN_PORT_FORWARDING_UP_COMMAND
hook: the hook fires once, at port-forward setup, before qBittorrent is
necessarily up, and a failed hook is logged without being retried.  It also does
nothing when qBittorrent restarts on its own and reverts to its stored port.
Polling converges from any starting state, which is what "unattended" needs.

Reads the port through gluetun's control server (authoritative, and not the
`/tmp/gluetun/forwarded_port` file, which gluetun's own docs mark for removal in
v4.0.0).  The control server binds inside gluetun's network namespace and is not
published, so this reaches it with `docker exec` rather than opening a host port.

Exit codes:
  0  in sync (or nothing to do yet, including "no port allocated")
  1  could not read the forwarded port
  2  could not read or update qBittorrent
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from media_stack.clients.qbit import QBitClient  # noqa: E402
from media_stack.paths import (  # noqa: E402
    MEDIA_STACK_ROOT, QBIT_URL, load_env_file,
)

CONTAINER = "gluetun"
CONTROL_URL = "http://127.0.0.1:8000/v1/portforward"


def log(msg: str) -> None:
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


def forwarded_port() -> int | None:
    """Port gluetun currently holds, or None if it could not be read.

    Returns 0 when gluetun answers but holds no mapping yet, which is a normal
    transient state right after a reconnect rather than a failure.
    """
    try:
        r = subprocess.run(
            ["docker", "exec", CONTAINER, "wget", "-qO-", "--timeout=10", CONTROL_URL],
            capture_output=True, text=True, timeout=60,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        log(f"ERROR: querying {CONTAINER}: {type(exc).__name__}: {exc}")
        return None
    if r.returncode != 0:
        log(f"ERROR: control server query failed rc={r.returncode}: "
            f"{(r.stderr or r.stdout).strip()[:200]}")
        return None
    try:
        data = json.loads(r.stdout)
    except ValueError:
        log(f"ERROR: control server returned non-JSON: {r.stdout.strip()[:200]}")
        return None
    if not isinstance(data, dict):
        log(f"ERROR: control server returned {type(data).__name__}, expected object")
        return None
    # The endpoint has used both a bare int and a list of ports across versions;
    # accept either rather than assuming one shape.  An ABSENT key means "no
    # allocation" and is normal; a key present but explicitly null is a shape
    # gluetun does not produce, so treat it as malformed rather than quietly
    # reading it as zero.
    if data.get("port") is not None:
        port = data["port"]
    elif "ports" in data:
        ports = data["ports"]
        if not isinstance(ports, list):
            log(f"ERROR: 'ports' is {ports!r}, expected a list")
            return None
        port = ports[0] if ports else 0
    elif "port" in data:
        log("ERROR: 'port' is null and no 'ports' list was returned")
        return None
    else:
        port = 0
    # Validate rather than trust: a bad value here would be written straight into
    # qBittorrent's listen port.
    if isinstance(port, bool) or not isinstance(port, int):
        log(f"ERROR: port is {port!r}, expected an integer")
        return None
    if port and not (1 <= port <= 65535):
        log(f"ERROR: port {port} out of range")
        return None
    return port


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change; write nothing")
    args = ap.parse_args()

    # Process env wins over .env, matching nuke_stalled.py.
    env = {**load_env_file(MEDIA_STACK_ROOT / ".env"), **os.environ}
    port = forwarded_port()
    if port is None:
        return 1
    if port <= 0:
        log("no forwarded port allocated yet; nothing to do")
        return 0

    qb = QBitClient(QBIT_URL, env.get("QBIT_USER", ""),
                    env.get("QBIT_PASS", ""))
    if not qb.login():
        log("ERROR: qBittorrent login failed")
        return 2
    try:
        current = int(qb.preferences().get("listen_port", 0))
    except Exception as exc:
        log(f"ERROR: reading qBittorrent preferences: {type(exc).__name__}: {exc}")
        return 2

    if current == port:
        log(f"in sync: qBittorrent listen_port={current} == forwarded port")
        return 0
    if args.dry_run:
        log(f"DRY RUN: would change listen_port {current} -> {port}")
        return 0

    try:
        qb.set_preferences({"listen_port": port})
    except Exception as exc:
        log(f"ERROR: setPreferences failed: {type(exc).__name__}: {exc}")
        return 2
    # setPreferences returns 200 even when it ignores the body, so confirm.
    try:
        after = int(qb.preferences().get("listen_port", 0))
    except Exception as exc:
        log(f"ERROR: re-reading preferences: {type(exc).__name__}: {exc}")
        return 2
    if after != port:
        log(f"ERROR: listen_port still {after} after setting it to {port}")
        return 2
    log(f"updated qBittorrent listen_port {current} -> {port}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
