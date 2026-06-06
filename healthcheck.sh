#!/bin/bash
# healthcheck.sh — thin wrapper around the typed implementation.
#
# The aggregate health monitor was extracted into media_stack/health.py
# (AUDIT A12) so the probe logic is type-checked (mypy), unit-tested
# (tests/test_health.py), and free of inline-Python-in-bash.  This shell
# entrypoint is retained so existing callers keep working unchanged:
# ops/healthcheck-alert.sh, cron, docs, and muscle memory all still run
# `bash healthcheck.sh [--verbose|--json]`.
#
# Behaviour is byte-identical to the former shell implementation (same
# probes/order, same OK/WARN/FAIL strings, same stdout-vs-stderr routing,
# same --json shape, same exit codes 0/1/2).  The full prior shell version
# is in git history if a side-by-side is ever needed.
#
# Exit codes:
#   0  every probe passed
#   1  one or more probes failed (details on stderr)
#   2  warnings only
#
# Usage:
#   bash healthcheck.sh            # quiet — only failed checks print
#   bash healthcheck.sh --verbose  # also print passed checks
#   bash healthcheck.sh --json     # machine-readable JSON summary

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"

# PYTHONPATH makes `-m media_stack.health` importable regardless of cwd —
# the hourly ops/healthcheck-alert.sh cron runs from $HOME, not the repo.
exec env PYTHONPATH="$REPO_ROOT" python3 -m media_stack.health "$@"
