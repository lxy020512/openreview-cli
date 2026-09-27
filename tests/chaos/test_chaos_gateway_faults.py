"""W7a/W7b chaos suite: gateway transport faults and honest degradation.

W7a (in-process, no socket) drives the five plan fault classes through the real
``Gateway.chat`` retry loop over an isolated database:

    delay, timeout, rate_limit (429), five_hundred (500), five_hundred_three (503)

W7b drives the four stream faults that need a real socket — malformed body,
truncated SSE, mid-stream disconnect and a provider that is always down — against a
``ThreadingTCPServer`` bound on ``127.0.0.1:0`` (``@pytest.mark.enable_socket``,
because pytest-socket blocks AF_INET by default even for loopback).

The invariant under test is the plan's Track D invariant (section 5): after the
fault the process must either succeed, or fail with an allowed exit code, a message
that names the failing component, no traceback, no logged cost and no counter
increment it did not earn.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from openreview_cli.gateway.errors import (
    ConnectionError as GatewayConnectionError,
)
from openreview_cli.gateway.errors import (
    GatewayError,
    RateLimitError,
    UnclassifiedProviderError,
)
from openreview_cli.gateway.models import get_total_cloud_calls
from openreview_cli.gateway.router import Gateway
from tests.chaos import _w7_probe as _w7
from tests.chaos.conftest import FlakyGatewayFactory, StreamServerFactory

pytestmark = pytest.mark.chaos

PROVIDER = "openai"

# The four fault classes whose honest outcome is a TYPED gateway error, and the type.
TYPED_FAULT_MATRIX: dict[str, type[Exception]] = {
    "timeout": UnclassifiedProviderError,
    "rate_limit": RateLimitError,
    "five_hundred": UnclassifiedProviderError,
    "five_hundred_three": UnclassifiedProviderError,
}

# Every in-process fault class the plan enumerates for W7a.
IN_PROCESS_FAULTS: tuple[str, ...] = (
    "delay",
    "timeout",
    "rate_limit",
    "five_hundred",
    "five_hundred_three",
)


def assert_typed_gateway_error(
    exc: BaseException, expected: type[Exception], provider: str
) -> None:
    """The matrix oracle: a typed ``GatewayError`` that names the provider.

    Module-level and deliberately strict so the negative control can prove it can
    fail (plan section 9.3 rule 1).
    """
    assert isinstance(exc, GatewayError), (
        f"{type(exc).__name__} is not a typed gateway error: {exc!r}"
    )
    assert isinstance(exc, expected), f"{type(exc).__name__} is not {expected.__name__}: {exc!r}"
    assert provider in str(exc), f"the error does not name the failing provider: {exc!r}"


def assert_stream_degraded_honestly(outcome: _w7.StreamOutcome, provider: str) -> None:
    """The W7b oracle: a stream either completes with its ``done`` marker, or fails typed."""
    if outcome.caught is not None:
        assert_typed_gateway_error(outcome.caught, GatewayError, provider)
        return
    assert outcome.completed, (
        f"the stream ended without a completion marker: events={outcome.events} "
        f"chunks={outcome.chunks}"
    )


# ── W7a: the in-process fault matrix ────────────────────────────────────────


@pytest.mark.parametrize(("fault_name", "expected"), sorted(TYPED_FAULT_MATRIX.items()))
def test_in_process_fault_is_a_typed_gateway_error(
    flaky_gateway: FlakyGatewayFactory, fault_name: str, expected: type[Exception]
) -> None:
    """Each transport fault must surface as a typed error naming the provider."""
    flaky = flaky_gateway(fault_name)

    with pytest.raises(Exception) as exc_info:
        flaky.chat()

    # Reachability: the retry loop really entered the dispatch seam.
    assert flaky.seam.attempts >= 1, "the dispatch seam was never reached"
    assert_typed_gateway_error(exc_info.value, expected, PROVIDER)
    # Cost and counter invariants: a failed call logs no cost, and earns only the
    # dispatch attempts that actually reached the provider.
    assert _w7.cost_rows(flaky.state.db_path) == [], "a faulted call logged a cost"
    assert flaky.gw._cloud_calls_made == flaky.seam.attempts, (
        "the counter must count every dispatch attempt"
    )
    assert get_total_cloud_calls() == flaky.seam.attempts, (
        "the process counter must count every dispatch attempt"
    )


def test_in_process_matrix_records_an_outcome_per_fault_class(
    flaky_gateway: FlakyGatewayFactory,
) -> None:
    """Every fault class has a test AND a recorded result (plan W7 done-when)."""
    outcomes: dict[str, str] = {}
    for fault_name in IN_PROCESS_FAULTS:
        flaky = flaky_gateway(fault_name)
        try:
            flaky.chat()
        except Exception as exc:
            outcomes[fault_name] = f"{type(exc).__name__}: {exc}"
        else:
            outcomes[fault_name] = "ok"
        assert flaky.seam.attempts >= 1, f"{fault_name}: the seam was never reached"

    assert set(outcomes) == set(IN_PROCESS_FAULTS), outcomes
    assert all(record for record in outcomes.values()), outcomes
    # The four transport faults degrade to a typed, provider-naming error.
    for fault_name, expected in TYPED_FAULT_MATRIX.items():
        assert expected.__name__ in outcomes[fault_name], outcomes[fault_name]


def test_retries_are_exactly_three_attempts_for_a_transient_fault(
    flaky_gateway: FlakyGatewayFactory,
) -> None:
    """Sharp edge 9 half: ``retries: 2`` really means 3 dispatches, none of them jittered."""
    flaky = flaky_gateway("timeout")

    with pytest.raises(UnclassifiedProviderError):
        flaky.chat()

    assert flaky.seam.attempts == 3, f"expected 3 attempts, saw {flaky.seam.attempts}"
    # No fallback model is configured for the slot, so no fourth dispatch.
    assert flaky.seam.models == ["openai/gpt-4o"] * 3, flaky.seam.models


# ── RT-038: a no-usable-choice reply must fail typed, with no cost row ──────
#
# ``delay`` (the W0 record that delivers no response) and ``empty_choices`` (the
# real litellm shape for a filtered/tool-only reply) both reach ``chat`` with a
# response that carries no usable choice. The reply is validated BEFORE the cost
# row, so the call raises a typed ``UnclassifiedProviderError`` naming the
# provider and leaves no ``cost_logs`` row. The dispatch is still counted
# (#147/C2), so the counter is not what makes the dispatch observable.

NO_CHOICE_CASES: tuple[str, ...] = ("delay", "empty_choices")


@pytest.mark.parametrize("case", sorted(NO_CHOICE_CASES))
def test_no_choice_response_is_a_typed_error_and_logs_no_cost(
    flaky_gateway: FlakyGatewayFactory, case: str
) -> None:
    """Expected: a typed gateway error naming the provider, and no cost row."""
    flaky = _build_no_choice(flaky_gateway, case)

    with pytest.raises(Exception) as exc_info:
        flaky.chat()

    assert_typed_gateway_error(exc_info.value, GatewayError, PROVIDER)
    assert _w7.cost_rows(flaky.state.db_path) == [], "a call with no usable reply logged a cost"


@pytest.mark.parametrize("case", sorted(NO_CHOICE_CASES))
def test_no_choice_response_is_typed_and_leaves_no_cost_row(
    flaky_gateway: FlakyGatewayFactory, case: str
) -> None:
    """RT-038/RT-039 fixed: a no-choice reply is typed and books no cost.

    ``delay`` delivers no response object at all and ``empty_choices`` the real
    litellm shape for a filtered/tool-only reply; both now raise the typed
    ``UnclassifiedProviderError`` naming the provider, without writing the
    ``cost_logs`` row the old path wrote before it dereferenced the reply. The
    dispatch is still counted (#147/C2).
    """
    flaky = _build_no_choice(flaky_gateway, case)

    with pytest.raises(UnclassifiedProviderError):
        flaky.chat()

    assert _w7.cost_rows(flaky.state.db_path) == [], "a call with no usable reply logged a cost"
    assert flaky.gw._cloud_calls_made == 1, "the counter did count the dispatch"


def _build_no_choice(flaky_gateway: FlakyGatewayFactory, case: str) -> _w7.FlakyGateway:
    """Build the fault-injected gateway for one no-choice case."""
    if case == "empty_choices":
        return flaky_gateway("empty_choices", response=_w7.EmptyChoicesResponse())
    return flaky_gateway("delay")


# ── W7a: the CLI oracle (exit code, component name, no traceback) ───────────


@pytest.mark.parametrize("fault_name", sorted(TYPED_FAULT_MATRIX))
def test_gateway_test_command_with_a_fault_is_a_clean_exit_1(
    flaky_gateway: FlakyGatewayFactory, fault_name: str
) -> None:
    """End to end: the CLI exits 1, names the provider and prints no traceback."""
    flaky = flaky_gateway(fault_name)

    result = _w7.run_cli(["gateway", "test", "extraction"])

    _w7.assert_clean_failure(result, frozenset({1}), PROVIDER)
    assert isinstance(result.exception, SystemExit), "a clean exit, not a raw traceback"
    assert flaky.seam.attempts >= 1, "the CLI never reached the dispatch seam"
    assert _w7.cost_rows(flaky.state.db_path) == [], "a faulted CLI call logged a cost"


# ── Negative control (anti-vacuity, plan section 9.3) ───────────────────────


def test_negative_control_oracle_rejects_a_raw_exception() -> None:
    """The matrix oracle must be able to fail, or a green matrix certifies nothing."""
    with pytest.raises(AssertionError, match="is not a typed gateway error"):
        assert_typed_gateway_error(RuntimeError("raw upstream failure"), GatewayError, PROVIDER)
    with pytest.raises(AssertionError, match="does not name the failing provider"):
        assert_typed_gateway_error(
            UnclassifiedProviderError("[other] boom"), GatewayError, PROVIDER
        )


# ── W7b: the same faults over a REAL local socket ───────────────────────────
#
# A ThreadingTCPServer bound on 127.0.0.1:0 (never a fixed port) serves the stream
# shapes the plan lists. pytest-socket blocks AF_INET by default even for loopback,
# so every case here carries @pytest.mark.enable_socket. The `stream_server`
# fixture's teardown asserts each socket is really gone.

STREAM_PROVIDER = "openai"


def test_negative_control_stream_oracle_rejects_a_raw_escape_and_a_silent_end() -> None:
    """The W7b oracle must reject both dishonest stream outcomes (plan 9.3 rule 1)."""
    raw = _w7.StreamOutcome(events=[], chunks=[], caught=RuntimeError("raw upstream failure"))
    with pytest.raises(AssertionError, match="is not a typed gateway error"):
        assert_stream_degraded_honestly(raw, STREAM_PROVIDER)

    silent = _w7.StreamOutcome(events=["chunk"], chunks=["hi"], caught=None)
    with pytest.raises(AssertionError, match="without a completion marker"):
        assert_stream_degraded_honestly(silent, STREAM_PROVIDER)


@pytest.fixture
def stream_case(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stream_server: StreamServerFactory
) -> Callable[[str], tuple[_w7.LocalSSEServer, Gateway]]:
    """Build (server, gateway) for one stream mode; provider_down leaves nothing listening."""

    def _build(mode: str) -> tuple[_w7.LocalSSEServer, Gateway]:
        server = stream_server(mode)
        if mode == "provider_down":
            server.stop()  # the port is now free: a provider that is always down
        gw = _w7.streaming_gateway(monkeypatch, tmp_path, server)
        return server, gw

    return _build


@pytest.mark.enable_socket
def test_healthy_local_stream_completes_with_chunks_and_done(
    stream_case: Callable[[str], tuple[_w7.LocalSSEServer, Gateway]],
) -> None:
    """Positive control: the local-server harness can deliver a whole, honest stream.

    Without this, an all-xfail W7b would certify nothing: it proves the socket path
    is live, that ``done`` is really emitted when the provider behaves, and that the
    suite's completion oracle can succeed.
    """
    server, gw = stream_case(_w7.HEALTHY_MODE)

    outcome = _w7.collect_stream(gw)

    assert outcome.caught is None, f"a healthy stream raised {outcome.caught!r}"
    assert outcome.events == ["chunk", "chunk", "done"], outcome.events
    assert outcome.chunks == ["hi", "!"], outcome.chunks
    assert _w7.request_hits(server) >= 1, "no HTTP request reached the local server"
    assert gw._cloud_calls_made == 0, "a loopback provider must not count as cloud"


@pytest.mark.enable_socket
@pytest.mark.parametrize("mode", ["malformed_body", "provider_down"])
def test_stream_dispatch_failure_is_a_typed_gateway_error(
    stream_case: Callable[[str], tuple[_w7.LocalSSEServer, Gateway]], mode: str
) -> None:
    """Expected: a stream failure surfaces as a typed gateway error naming the provider."""
    _server, gw = stream_case(mode)

    outcome = _w7.collect_stream(gw)

    assert outcome.caught is not None, "the stream fault did not surface at all"
    assert_stream_degraded_honestly(outcome, STREAM_PROVIDER)


@pytest.mark.enable_socket
@pytest.mark.parametrize("mode", ["malformed_body", "provider_down"])
def test_stream_dispatch_failure_is_a_typed_gateway_error_and_logs_no_cost(
    stream_case: Callable[[str], tuple[_w7.LocalSSEServer, Gateway]], mode: str
) -> None:
    """Characterisation pinning RT-040 fixed: the raw litellm type no longer escapes.

    The two faults surface at DIFFERENT times: ``provider_down`` fails eagerly inside
    ``completion(stream=True)`` (so the dispatch seam, ``_call_with_fallback``, must
    classify it), while ``malformed_body`` raises on the first ``next()`` (so
    ``_iter_stream`` must classify it). Both halves are why this task fixes both.
    """
    server, gw = stream_case(mode)

    outcome = _w7.collect_stream(gw)

    assert outcome.caught is not None, "the stream fault did not surface at all"
    assert isinstance(outcome.caught, GatewayError), outcome.caught
    assert gw._cloud_calls_made == 0, "a failed dispatch must not earn the counter"
    if mode == "provider_down":
        assert _w7.port_is_closed(server.port), "provider_down must have nothing listening"
        assert _w7.request_hits(server) == 0
        assert _w7.cost_rows(gw._data_path) == [], "a never-connected stream logged a cost"


@pytest.mark.enable_socket
@pytest.mark.parametrize("mode", ["truncated_stream", "mid_stream_disconnect"])
def test_stream_without_a_completion_marker_is_not_reported_as_done(
    stream_case: Callable[[str], tuple[_w7.LocalSSEServer, Gateway]], mode: str
) -> None:
    """Expected: an unterminated SSE stream must not look like a completed reply.

    Neither stream carries ``data: [DONE]`` nor a chunk with ``finish_reason``.
    """
    _server, gw = stream_case(mode)

    outcome = _w7.collect_stream(gw)

    assert not outcome.completed, (
        f"a truncated stream was reported as a successful completion: "
        f"events={outcome.events} chunks={outcome.chunks}"
    )


@pytest.mark.enable_socket
@pytest.mark.parametrize("mode", ["truncated_stream", "mid_stream_disconnect"])
def test_stream_without_a_completion_marker_is_reported_as_truncated(
    stream_case: Callable[[str], tuple[_w7.LocalSSEServer, Gateway]], mode: str
) -> None:
    """Characterisation pinning RT-041/RT-039 fixed: truncation is now a typed error.

    The provider never sent a finish reason, so the gateway must raise the typed
    connection error naming the provider, leave no ``done`` event and (RT-039)
    write no ``cost_logs`` row.
    """
    server, gw = stream_case(mode)

    outcome = _w7.collect_stream(gw)

    assert _w7.request_hits(server) >= 1, "no HTTP request reached the local server"
    assert isinstance(outcome.caught, GatewayConnectionError), outcome.caught
    assert STREAM_PROVIDER in str(outcome.caught)
    assert not outcome.completed
    assert "done" not in outcome.events
    assert _w7.cost_rows(gw._data_path) == [], "a truncated stream must not book a cost"
    assert gw._cloud_calls_made == 0, "a loopback provider must not count as cloud"
