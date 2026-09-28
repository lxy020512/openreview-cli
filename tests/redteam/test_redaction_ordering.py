"""W6 red-team suite: the redaction install ordering must not fail open.

``install_on_root_handlers`` is imported at ``src/openreview_cli/app.py:26`` and
called at ``app.py:324`` — AFTER ``_init`` creates its StreamHandler and
RotatingFileHandler (``app.py:301-322``). It attaches a ``RedactingFilter`` to the
root handlers that exist *at the moment it runs* (``gateway/redaction.py:52-61``),
so a handler added later is never filtered. This is an ordering attack, deliberately
not the end state: the suite drives orderings that bypass the filter and checks
whether the key survives.

Two cases match the plan's brief — the key must stay absent (a) with
``install_on_root_handlers`` deliberately NOT called and (b) with a handler
configured BEFORE it. A third pair demonstrates the ordering property itself:
install-before-handler leaks, handler-before-install redacts. That demonstration
is the RED evidence that the absence assertions can fail.
"""

from __future__ import annotations

import io
import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from tests.helpers import leak_capture
from tests.redteam import _w6_probe as _w6

pytestmark = pytest.mark.redteam


@pytest.fixture(autouse=True)
def _detach_cli_log_handlers() -> Iterator[None]:
    yield
    _w6.detach_owned_handlers()
    os.environ.pop("OPENAI_API_KEY", None)


@contextmanager
def _bare_root() -> Iterator[None]:
    """Temporarily detach every root handler, restoring them on exit.

    ``RedactingFilter.filter`` mutates the ``LogRecord`` in place
    (``redaction.py:44-48``), so an already-filtered handler earlier in the
    dispatch order redacts the record for every later handler too. Isolating the
    root handlers removes that cross-talk and exposes the *ordering* property
    under test rather than the incidental mutation.
    """
    root = logging.getLogger()
    saved = list(root.handlers)
    for handler in saved:
        root.removeHandler(handler)
    try:
        yield
    finally:
        for handler in saved:
            root.addHandler(handler)


def _state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _w6.State:
    state = _w6.prepare_state(
        monkeypatch,
        tmp_path,
        tier="balanced",
        primary="openai/gpt-4o",
        auth={"openai": _w6.SK_KEY},
    )
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    return state


def _log_raw_key_via_product_logger(key: str) -> None:
    """Emit a raw-key record through a real product logger.

    ``openreview_cli.gateway.router`` is the module whose ``_set_env_vars`` logs
    at ``router.py:184``; a record emitted here is filtered only by whatever
    filter the receiving handler carries. That is exactly the guard under test.
    """
    logging.getLogger("openreview_cli.gateway.router").debug("Set OPENAI_API_KEY to %s", key)


# ── The ordering property: install-before-handler leaks, the reverse redacts ─


def test_handler_added_after_install_bypasses_redaction() -> None:
    """RED demonstration of the ordering hole: install runs, THEN a handler is added.

    With the root handlers isolated, a handler added after
    ``install_on_root_handlers`` never receives the ``RedactingFilter``, so a
    raw-key record reaches it unredacted. Asserted present so this file records
    the hole; the product's own ``_init`` avoids it by installing last
    (``app.py:301-324``).
    """
    from openreview_cli.gateway.redaction import install_on_root_handlers

    root = logging.getLogger()
    stream = io.StringIO()
    level = root.level
    with _bare_root():
        install_on_root_handlers()  # nothing to filter yet
        handler = logging.StreamHandler(stream)
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        try:
            _log_raw_key_via_product_logger(_w6.SK_KEY)
        finally:
            root.removeHandler(handler)
            root.setLevel(level)

    rendered = stream.getvalue()
    assert _w6.SK_KEY in rendered, (
        "the post-install handler was filtered after all; the ordering hole is "
        f"not the one described: {rendered!r}"
    )


def test_handler_present_before_install_is_redacted() -> None:
    """The safe order: a handler configured BEFORE install gets the filter."""
    from openreview_cli.gateway.redaction import install_on_root_handlers

    root = logging.getLogger()
    stream = io.StringIO()
    level = root.level
    with _bare_root():
        handler = logging.StreamHandler(stream)
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        try:
            install_on_root_handlers()
            _log_raw_key_via_product_logger(_w6.SK_KEY)
        finally:
            root.removeHandler(handler)
            root.setLevel(level)

    rendered = stream.getvalue()
    assert "OPEN**********" in rendered, (
        f"reachability: the filter never ran on the record: {rendered!r}"
    )
    assert _w6.SK_KEY not in rendered, f"the filter did not redact the key: {rendered!r}"
    assert "***t" in rendered


# ── The plan's two cases: drive the real CLI in both orderings ──────────────


def test_key_absent_from_log_when_install_is_not_called(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(a) ``install_on_root_handlers`` deliberately a no-op: the key must not leak.

    ``_init`` and ``Gateway.__init__`` both import the symbol, so both call sites
    are patched. The handler filter is off; what remains is the call-site
    redaction at ``router.py:184`` (``redact_key``). The log must carry the
    redacted form and never the key — proving the call site is load bearing in
    its own right, not merely a backstop for the filter.
    """
    state = _state(tmp_path, monkeypatch)
    monkeypatch.setattr("openreview_cli.app.install_on_root_handlers", lambda: None)
    monkeypatch.setattr("openreview_cli.gateway.router.install_on_root_handlers", lambda: None)

    with (
        _bare_root(),
        leak_capture.capture(log_dir=state.log_dir, db_path=state.db_path) as captured,
    ):
        result = _w6.run_cli(["--debug", "gateway", "status"])
    captured.add_cli_result(result)

    assert result.exit_code == 0, f"gateway status failed: {result.output!r}"
    log_text = "\n".join(captured.logs.values())
    assert captured.logs, "no log file was harvested, so this proves nothing"
    # Reachability + call-site redaction: the record fired and was masked before
    # any handler saw it, so the "sk-" prefix is still literal here.
    assert "Set OPENAI_API_KEY to sk-t" in log_text, (
        f"the debug log did not fire through the un-filtered handler: {log_text[-400:]!r}"
    )
    for needle in (_w6.SK_KEY, _w6.SK_BODY):
        table = _w6.absence_table(captured, needle)
        assert not _w6.failures(table), (
            f"{needle!r} leaked with the filter disabled: {_w6.failures(table)}\n"
            f"{_w6.render_table(table)}"
        )


def test_key_absent_from_log_when_a_handler_is_configured_before_the_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(b) A root handler configured BEFORE the CLI: install must still cover it.

    The handler is added before ``_init`` runs; ``install_on_root_handlers`` then
    iterates every root handler and attaches the filter, so this pre-existing
    handler must redact. Drives the real ``--debug`` CLI.
    """
    state = _state(tmp_path, monkeypatch)
    root = logging.getLogger()
    stream = io.StringIO()
    pre_handler = logging.StreamHandler(stream)

    with _bare_root():
        root.addHandler(pre_handler)
        try:
            with leak_capture.capture(log_dir=state.log_dir, db_path=state.db_path) as captured:
                result = _w6.run_cli(["--debug", "gateway", "status"])
            captured.add_cli_result(result)
        finally:
            root.removeHandler(pre_handler)

    assert result.exit_code == 0, f"gateway status failed: {result.output!r}"
    rendered = stream.getvalue()
    assert "OPEN**********" in rendered, (
        f"reachability: the pre-existing handler never received the filtered record: {rendered!r}"
    )
    assert "***t" in rendered, f"the pre-existing handler was not filtered: {rendered!r}"
    assert _w6.SK_KEY not in rendered
    assert _w6.SK_BODY not in rendered
    for needle in (_w6.SK_KEY, _w6.SK_BODY):
        table = _w6.absence_table(captured, needle)
        assert not _w6.failures(table), (
            f"{needle!r} leaked on surface(s): {_w6.failures(table)}\n{_w6.render_table(table)}"
        )


def test_child_logger_with_its_own_handler_bypasses_root_redaction() -> None:
    """Hardening note, asserted so it is not silently believed fixed.

    ``install_on_root_handlers`` covers ROOT handlers only (its own docstring,
    ``redaction.py:53-56``). A logger with ``propagate=False`` and its own
    handler therefore never consults a ``RedactingFilter``. The product attaches
    no handler to a child logger, so this is not reachable today; the case
    records the boundary rather than claiming coverage.
    """
    from openreview_cli.gateway.redaction import install_on_root_handlers

    name = "openreview_cli.w6_ordering_probe"
    child = logging.getLogger(name)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    root = logging.getLogger()
    previous_level = root.level
    previous_propagate = child.propagate
    child.addHandler(handler)
    child.propagate = False
    root.setLevel(logging.DEBUG)
    try:
        install_on_root_handlers()
        child.debug("Set OPENAI_API_KEY to %s", _w6.SK_KEY)
    finally:
        child.removeHandler(handler)
        child.propagate = previous_propagate
        root.setLevel(previous_level)

    rendered = stream.getvalue()
    assert _w6.SK_KEY in rendered, (
        "a child-logger handler WAS filtered; the documented root-handler-only "
        f"scope no longer holds: {rendered!r}"
    )
