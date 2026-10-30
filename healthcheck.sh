#!/bin/bash
# Thin entrypoint for media_stack/health.py. Usage: healthcheck.sh [--verbose | --json]
# Exit: 0 every probe passed, 1 any failed (stderr), 2 warnings only.

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
PATH="$REPO_ROOT/.venv/bin:$PATH"  # the stack runs from the repo venv

# PYTHONPATH: importable from any cwd (the hourly cron runs from $HOME).
exec env PYTHONPATH="$REPO_ROOT" python3 -m media_stack.health "$@"
