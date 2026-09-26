"""Fault-injection doubles for the chaos suite.

Owned by W0. W7 and W8 consume this; ``tests/chaos/conftest.py`` only exposes
fixtures that import from here.

The plan's section 5 fault matrix is implemented as ONE ``Fault`` record plus a
table, not one class per fault. The plan calls for "9 rows"; the matrix needs
**10** once ``failing_write`` is included, and W8b explicitly consumes
``failing_write``, so this table has 10 rows. (Correction to plan section 10.)

Exception types are builtins where a builtin fits. ``gateway`` imports
``litellm`` (~4.5s), so this module must NOT import gateway internals at import
time. Two HTTP-shaped faults need a name richer than a builtin, so they get
tiny local ``Exception`` subclasses. (The brief suggested a ``_GatewayFault``
base, but ruff's N818 requires an ``Error`` suffix and the ruff config is
off-limits, so the subclasses sit directly on ``Exception``.)
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


class RateLimitError(Exception):
    """Injected HTTP 429 rate-limit fault."""


class ServerError(Exception):
    """Injected HTTP 5xx provider fault."""


@dataclass(frozen=True)
class Fault:
    """A single injectable fault.

    ``at_call`` is the number of calls allowed through before the fault starts
    firing; ``0`` means every call fires. When it fires, ``error`` is raised if
    set, otherwise ``body`` is returned.
    """

    name: str
    error: type[Exception] | None = None
    body: str | None = None
    at_call: int = 0


@dataclass
class _FaultState:
    """Mutable per-wrapper call counter (a fresh one per ``apply`` call)."""

    calls: int = 0


FAULTS: dict[str, Fault] = {
    # delay: no error and no body (per the W0 contract). Firing it simply stops
    # the call from completing, which is how a latency/stall fault is
    # represented in this record.
    "delay": Fault("delay"),
    "timeout": Fault("timeout", error=TimeoutError),
    "rate_limit": Fault("rate_limit", error=RateLimitError),
    "five_hundred": Fault("five_hundred", error=ServerError),
    "five_hundred_three": Fault("five_hundred_three", error=ServerError),
    # A malformed or truncated body is delivered as a payload, not raised.
    "malformed_body": Fault("malformed_body", body="<malformed body>"),
    "truncated_stream": Fault("truncated_stream", body="<truncated stream"),
    "mid_stream_disconnect": Fault("mid_stream_disconnect", error=ConnectionError),
    "provider_down": Fault("provider_down", error=ConnectionError),
    "failing_write": Fault("failing_write", error=OSError),
}


def apply(fault: Fault, fn: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap ``fn`` so ``fault`` fires once its allowed call budget is spent.

    Calls 1..``fault.at_call`` pass through to ``fn``; call ``at_call + 1`` and
    beyond raise ``fault.error`` (when set) or return ``fault.body``. Each call
    to ``apply`` allocates its own counter, so wrappers never share state.
    """
    state = _FaultState()

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        state.calls += 1
        if state.calls > fault.at_call:
            if fault.error is not None:
                raise fault.error(f"{fault.name} fault injected")
            return fault.body
        return fn(*args, **kwargs)

    return wrapper
