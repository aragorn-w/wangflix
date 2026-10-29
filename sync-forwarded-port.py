#!/usr/bin/env python3
"""Keep qBittorrent's listen port equal to the port ProtonVPN forwards.

The forwarded port is dynamic (usually changes on reconnect) and qBittorrent doesn't notice, so
inbound peers silently stop working. This polls instead of using gluetun's
VPN_PORT_FORWARDING_UP_COMMAND hook, which fires once (possibly before qBittorrent is up), is
never retried, and does nothing when qBittorrent restarts and reverts to its stored port.

The port is read from gluetun's control server through `docker exec` (it is not published; the
`/tmp/gluetun/forwarded_port` file is slated for removal in gluetun v4.0.0).

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
    """0 if gluetun has no mapping yet (normal after a reconnect), None if unreadable."""
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
    # The endpoint has used both a bare int and a list of ports; accept either. An absent
    # key means no allocation; an explicit null is malformed, not zero.
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
    # validate: this value is written straight into qBittorrent's listen port
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
    # setPreferences returns 200 even when it ignores the body, so re-read to confirm
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
