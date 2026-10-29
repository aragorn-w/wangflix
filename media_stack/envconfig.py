"""Bounded numeric env-var parsing shared by the cron scripts.

Every numeric setting must be FINITE, POSITIVE and BOUNDED, not just parseable: `float()` accepts
"nan"/"inf"/1e308, and a nan budget never times out (`elapsed >= nan` is always False) while a huge
one overflows. The caller's default is not re-checked against `max_value`, so it must respect any
runtime-computed bound itself.
"""

from __future__ import annotations

import math
from typing import Callable

LogFn = Callable[[str], None]


def env_float(
    env: dict[str, str], key: str, default: float, max_value: float,
    *, log: LogFn = print,
) -> float:
    """Unset or empty returns `default` silently; an invalid value warns via `log` and returns it."""
    raw = env.get(key)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        log(f"WARNING: {key}={raw!r} is not a valid number; using default {default:g}")
        return default
    if not math.isfinite(value) or value <= 0 or value > max_value:
        log(f"WARNING: {key}={raw!r} must be a finite, positive number <= "
            f"{max_value:g}; using default {default:g}")
        return default
    return value


def env_int(
    env: dict[str, str], key: str, default: int, max_value: int,
    *, log: LogFn = print,
) -> int:
    """`env_float` for a positive integer <= `max_value`."""
    raw = env.get(key)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        log(f"WARNING: {key}={raw!r} is not a valid integer; using default {default}")
        return default
    if value <= 0 or value > max_value:
        log(f"WARNING: {key}={raw!r} must be a positive integer <= {max_value}; "
            f"using default {default}")
        return default
    return value
