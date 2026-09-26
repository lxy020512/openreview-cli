"""Unit tests for tests.helpers.faults."""

import pytest

from tests.helpers.faults import FAULTS, Fault, apply


def test_fault_fires_on_configured_call() -> None:
    calls: list[int] = []

    def fn() -> str:
        calls.append(1)
        return "through"

    wrapped = apply(Fault("timeout", error=TimeoutError, at_call=2), fn)

    assert wrapped() == "through"
    assert wrapped() == "through"
    with pytest.raises(TimeoutError):
        wrapped()
    assert len(calls) == 2  # only the two pass-through calls reached fn


def test_at_call_zero_fires_on_first_call() -> None:
    calls: list[int] = []

    def fn() -> str:
        calls.append(1)
        return "through"

    wrapped = apply(Fault("provider_down", error=ConnectionError), fn)

    with pytest.raises(ConnectionError):
        wrapped()
    assert calls == []  # fn never ran


def test_body_returned_when_error_is_none() -> None:
    calls: list[int] = []

    def fn() -> str:
        calls.append(1)
        return "through"

    wrapped = apply(FAULTS["malformed_body"], fn)

    assert wrapped() == FAULTS["malformed_body"].body
    assert calls == []  # the body fault replaces the call, it does not call through


def test_wrapper_state_is_independent_per_apply_call() -> None:
    def fn() -> str:
        return "through"

    fault = Fault("timeout", error=TimeoutError, at_call=1)
    first = apply(fault, fn)
    second = apply(fault, fn)

    assert first() == "through"
    with pytest.raises(TimeoutError):
        first()
    # The second wrapper keeps its own fresh counter, unaffected by the first.
    assert second() == "through"
    with pytest.raises(TimeoutError):
        second()


def test_args_and_kwargs_pass_through() -> None:
    def fn(a: int, b: int = 0) -> int:
        return a + b

    wrapped = apply(Fault("timeout", error=TimeoutError, at_call=5), fn)

    assert wrapped(2, b=3) == 5


def test_faults_has_exactly_ten_rows_named_by_key() -> None:
    assert len(FAULTS) == 10
    assert set(FAULTS) == {
        "delay",
        "timeout",
        "rate_limit",
        "five_hundred",
        "five_hundred_three",
        "malformed_body",
        "truncated_stream",
        "mid_stream_disconnect",
        "provider_down",
        "failing_write",
    }
    for key, fault in FAULTS.items():
        assert fault.name == key


def test_delay_carries_no_error_and_no_body() -> None:
    delay = FAULTS["delay"]
    assert delay.error is None
    assert delay.body is None


def test_failing_write_raises_oserror() -> None:
    def fn() -> str:
        return "through"

    wrapped = apply(FAULTS["failing_write"], fn)
    with pytest.raises(OSError):
        wrapped()
