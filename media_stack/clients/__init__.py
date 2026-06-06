"""External service clients (Arr, qBittorrent, Bazarr, Telegram).

Each submodule wraps a single service's HTTP API with consistent
timeout, auth, and error handling so the operational scripts
(nuke_stalled.py, bazarr-profile-audit.py, healthcheck.sh) don't
each re-implement the same login + cookie boilerplate.  No
retry/backoff is implemented — transient failures surface as `None`
returns; callers decide whether to retry or alert.
"""
