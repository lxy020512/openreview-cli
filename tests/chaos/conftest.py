"""W7a-owned chaos fixtures (plan section 7: this file is owned by W7a alone).

Two fixtures, both thin: :func:`flaky_gateway` builds a REAL ``Gateway`` over an
isolated XDG tree whose dispatch seam is fault-injected, and :func:`stream_server`
hands out real ``ThreadingTCPServer`` instances on ``127.0.0.1:0`` while asserting
in teardown that every socket it handed out is gone.

The fault doubles themselves are owned by W0 (``tests/helpers/faults.py``); this
module only imports them (plan section 7, ownership rule).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from openreview_cli.gateway.models import ProviderInfo, reset_total_cloud_calls
from openreview_cli.gateway.router import mark_pii_available, reset_pii_available
from tests.chaos import _w7_probe as _w7


@pytest.fixture(autouse=True)
def _isolate_router_globals() -> Iterator[None]:
    """Isolate the two process-wide router globals around every chaos case."""
    reset_total_cloud_calls()
    reset_pii_available()
    yield
    reset_total_cloud_calls()
    reset_pii_available()


class FlakyGatewayFactory:
    """Build one fault-injected real ``Gateway`` per call, inside ``tmp_path``."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self._monkeypatch = monkeypatch
        self._tmp_path = tmp_path
        self._built = 0

    def __call__(
        self,
        fault: str = "timeout",
        *,
        tier: str = "performance",
        primary: str = _w7.CLOUD_PRIMARY,
        retries: int = 2,
        retry_delay: float = 0.0,
        slot_fallback: str | None = None,
        registry: dict[str, ProviderInfo] | None = None,
        response: Any = None,
    ) -> _w7.FlakyGateway:
        self._built += 1
        root = self._tmp_path / f"build{self._built}"
        root.mkdir(parents=True, exist_ok=True)
        state = _w7.prepare_state(
            self._monkeypatch,
            root,
            tier=tier,
            primary=primary,
            retries=retries,
            retry_delay=retry_delay,
            slot_fallback=slot_fallback,
        )
        gw = _w7.make_gateway(
            state,
            primary=primary,
            tier=tier,
            retries=retries,
            retry_delay=retry_delay,
            slot_fallback=slot_fallback,
        )
        seam = _w7.FaultSeam(_w7.injected(fault), response)
        seam.install(self._monkeypatch)
        # Deterministic, offline pricing: litellm's own pricing lookup would need the
        # real model string on the response. The repo's cost tests do the same
        # (tests/unit/test_gateway_cost.py:41).
        self._monkeypatch.setattr(
            "openreview_cli.gateway.cost.completion_cost", lambda *_a, **_k: 0.05
        )
        flaky = _w7.FlakyGateway(gw, seam, state, fault, self._monkeypatch)
        flaky.install_registry(registry if registry is not None else _w7.cloud_registry())
        # performance/balanced require a recorded PII strip before cloud egress
        # (router._enforce_tier); W7 attacks the transport, not the PII gate.
        mark_pii_available()
        return flaky

    @property
    def built(self) -> int:
        """How many gateways this factory has built (reachability sanity)."""
        return self._built


@pytest.fixture
def flaky_gateway(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FlakyGatewayFactory:
    """Return a factory that patches ``router.Gateway`` and ``router.load_registry``.

    Patches ``openreview_cli.gateway.router.Gateway`` (so the real CLI/review paths
    that import it lazily get the fault-injected instance) and
    ``openreview_cli.gateway.router.load_registry`` (a deterministic two-provider
    registry). No socket, no network.
    """
    return FlakyGatewayFactory(monkeypatch, tmp_path)


@pytest.fixture
def stream_server() -> Iterator[StreamServerFactory]:
    """Hand out real loopback SSE servers; assert every one is gone afterwards."""
    factory = StreamServerFactory()
    yield factory
    factory.assert_all_gone()


class StreamServerFactory:
    """Creates :class:`_w7_probe.LocalSSEServer` instances and tracks them."""

    def __init__(self) -> None:
        self._servers: list[_w7.LocalSSEServer] = []

    def __call__(self, mode: str, *, start: bool = True) -> _w7.LocalSSEServer:
        server = _w7.LocalSSEServer(mode)
        if start:
            server.start()
        self._servers.append(server)
        return server

    def assert_all_gone(self) -> None:
        """Teardown assertion (plan W7b): no server thread or socket survives."""
        for server in self._servers:
            server.stop()
            server.assert_gone()
