"""W5 red-team suite: ``gateway setup`` and ``gateway test``.

Attacks two guarantees inside the two commands that own key material
(plan section 5, Track B):

* guarantee 4 — ``auth.json`` contents never reach an output surface, and a
  corrupt ``auth.json`` produces a clear error rather than a raw traceback;
* guarantee 2, as far as these two commands are concerned — a provider failure
  must not turn an API key into user-visible text.

Every case runs the real Typer app through a one-shot ``CliRunner`` with all
four XDG roots redirected at ``tmp_path``, so the developer's own ``auth.json``
is never read. The planted value is a synthetic canary; ``assert_detects`` is
the positive control that the capture harness can see a leak at all. No
sockets, no network, no paid providers.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import Result

from openreview_cli.errors import EXIT_CONFIG
from tests.helpers import faults, leak_capture
from tests.redteam import _w5_probe as _w5

pytestmark = pytest.mark.redteam

TRACEBACK_MARKER = "Traceback (most recent call last)"


@pytest.fixture(autouse=True)
def _detach_cli_log_handlers() -> Iterator[None]:
    """Drop the handlers ``_init`` installs, so none holds this test's buffer.

    ``_init`` attaches a FileHandler and a StreamHandler (``app.py:246-252``)
    bound to the streams that exist at invoke time — under ``leak_capture``
    that is a StringIO which dies with the test. A later emit into the stale
    buffer prints a ``--- Logging error ---`` block into an unrelated test's
    captured output.
    """
    yield
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_openreview_owned", False):
            root.removeHandler(handler)
            handler.close()


class _FakePrompt:
    """A scripted ``questionary`` prompt: ``.ask()`` always returns *value*."""

    def __init__(self, value: Any) -> None:
        self._value = value

    def ask(self) -> Any:
        return self._value


def script_wizard(monkeypatch: pytest.MonkeyPatch) -> None:
    """Answer every wizard prompt, so ``gateway setup`` runs unattended."""
    import questionary

    monkeypatch.setattr(questionary, "select", lambda *a, **k: _FakePrompt("openai"))
    monkeypatch.setattr(questionary, "text", lambda *a, **k: _FakePrompt("gpt-4o"))
    monkeypatch.setattr(questionary, "password", lambda *a, **k: _FakePrompt(_w5.CANARY_KEY))


def assert_clean_failure(result: Result, allowed: frozenset[int], token: str | None = None) -> None:
    """Assert *result* is a clean failure: an ``errors.py`` code, no raw traceback."""
    assert result.exit_code in allowed, (
        f"expected exit in {sorted(allowed)}, got {result.exit_code}; "
        f"exception={result.exception!r}; output={result.output!r}"
    )
    assert isinstance(result.exception, SystemExit), (
        f"uncaught {type(result.exception).__name__}: {result.exception!r}"
    )
    assert TRACEBACK_MARKER not in result.output, result.output
    if token is not None:
        assert token in result.output, f"{token!r} not in {result.output!r}"


def canary_hits(captured: leak_capture.Captured) -> dict[str, int]:
    """Every surface carrying either the whole canary or its un-redacted body."""
    hits = captured.find_all(_w5.CANARY_KEY)
    for surface, count in captured.find_all(_w5.CANARY_BODY).items():
        hits[surface] = hits.get(surface, 0) + count
    return hits


def _plant_canary_in_db(db_path: Path) -> None:
    """Plant the canary in a real TEXT-affinity column (``clients.name``)."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO clients (id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
            ("canary-client", _w5.CANARY_KEY, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
        )
        conn.commit()
    finally:
        conn.close()


# ── Negative control: the leak oracle must be able to fail ──────────────────


def test_negative_control_leak_oracle_rejects_a_planted_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The canary planted on two real surfaces must be found, and named.

    ``assert_detects`` is the positive control (the harness can see a leak at
    all); the ``pytest.raises`` below is the negative control — the same oracle
    that guards every case in this file must raise here. Demonstrated failing
    for real in ``draft/evidence/W5_negative_control_gateway_setup.txt``.
    """
    state = _w5.prepare_state(monkeypatch, tmp_path)
    (state.log_dir / "planted.log").write_text(
        f"2026-01-01 [INFO] planted {_w5.CANARY_KEY}\n", encoding="utf-8"
    )
    _plant_canary_in_db(state.db_path)

    with leak_capture.capture(log_dir=state.log_dir, db_path=state.db_path) as captured:
        sys.stdout.write(f"planted {_w5.CANARY_KEY} on stdout\n")

    leak_capture.assert_detects(captured, _w5.CANARY_KEY)
    assert {Path(surface).name for surface in captured.find_all(_w5.CANARY_KEY)} == {
        "stdout",
        "planted.log",
        "clients.name",
    }

    with pytest.raises(AssertionError, match="found on surface"):
        captured.assert_absent(_w5.CANARY_KEY)


# ── gateway setup ───────────────────────────────────────────────────────────


def test_gateway_setup_on_a_corrupt_auth_json_names_the_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Inverts the pre-fix RT-033 pin (exit 1, empty output): the config error names the file."""
    state = _w5.prepare_state(monkeypatch, tmp_path)
    state.auth_path.write_text("{not json", encoding="utf-8")
    state.auth_path.chmod(0o600)
    script_wizard(monkeypatch)

    result = _w5.run_cli(["gateway", "setup"])

    assert_clean_failure(result, frozenset({EXIT_CONFIG}), "corrupt")
    assert "auth.json" in result.output


def test_gateway_setup_on_a_corrupt_auth_json_is_a_clean_config_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Expected: a documented config error naming the file, not an escaped raise."""
    state = _w5.prepare_state(monkeypatch, tmp_path)
    state.auth_path.write_text("{not json", encoding="utf-8")
    state.auth_path.chmod(0o600)
    script_wizard(monkeypatch)

    result = _w5.run_cli(["gateway", "setup"])

    assert_clean_failure(result, frozenset({EXIT_CONFIG}), "corrupt")


def test_gateway_setup_writes_the_prompted_key_without_printing_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The key typed at the wizard prompt reaches auth.json and no other surface."""
    state = _w5.prepare_state(monkeypatch, tmp_path)
    script_wizard(monkeypatch)

    with leak_capture.capture(log_dir=state.log_dir, db_path=state.db_path) as captured:
        result = _w5.run_cli(["gateway", "setup"])
    captured.add_cli_result(result)

    assert result.exit_code == 0, f"exit={result.exit_code} output={result.output!r}"
    written = json.loads(state.auth_path.read_text(encoding="utf-8"))
    assert written.get("openai") == _w5.CANARY_KEY, (
        "reachability: the wizard never wrote the planted key, so this case proves nothing"
    )
    assert canary_hits(captured) == {}, f"key material on surface(s): {canary_hits(captured)}"


def test_gateway_setup_never_prints_a_key_that_is_already_on_disk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A key loaded from auth.json is rewritten, never echoed."""
    state = _w5.prepare_state(monkeypatch, tmp_path, auth={"openai": _w5.CANARY_KEY})
    script_wizard(monkeypatch)

    with leak_capture.capture(log_dir=state.log_dir, db_path=state.db_path) as captured:
        result = _w5.run_cli(["gateway", "setup"])
    captured.add_cli_result(result)

    assert result.exit_code == 0, f"exit={result.exit_code} output={result.output!r}"
    assert _w5.CANARY_KEY in state.auth_path.read_text(encoding="utf-8")
    assert canary_hits(captured) == {}, f"key material on surface(s): {canary_hits(captured)}"
    # The log surface is a real surface, not a vacuous one: the CLI writes a log.
    assert captured.logs, f"no log file was harvested from {state.log_dir}"


# ── gateway test ────────────────────────────────────────────────────────────


def test_gateway_test_against_a_down_provider_is_a_clean_error_and_prints_no_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A provider-down fault: exit 1, the component named, no key material anywhere."""
    import openreview_cli.gateway.router as router_mod

    state = _w5.prepare_state(
        monkeypatch,
        tmp_path,
        tier="performance",
        primary="openai/gpt-4o",
        auth={"openai": _w5.CANARY_KEY},
    )
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    calls = [0]
    faulted = faults.apply(faults.FAULTS["provider_down"], lambda **kw: None)

    def counted(**kwargs: Any) -> Any:
        calls[0] += 1
        return faulted(**kwargs)

    monkeypatch.setattr(router_mod, "completion", counted)
    router_mod.mark_pii_available()

    with leak_capture.capture(log_dir=state.log_dir, db_path=state.db_path) as captured:
        result = _w5.run_cli(["gateway", "test", "extraction"])
    captured.add_cli_result(result)

    assert calls[0] >= 1, "the dispatch seam was never reached"
    assert_clean_failure(result, frozenset({1}), "provider_down fault injected")
    assert canary_hits(captured) == {}, f"key material on surface(s): {canary_hits(captured)}"


def test_gateway_test_does_not_echo_key_material_from_a_provider_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Expected: an upstream error body is not echoed to the terminal verbatim.

    Providers and proxies do put request credentials into error bodies. The
    gateway already owns the redaction helper for this
    (``gateway/redaction.py:27-42``, applied to log records only), and
    ``_classify_error`` (``router.py:433-490``) hands ``str(exc)`` straight to
    the CLI, which echoes it (``app.py:1673-1675``).
    """
    import openreview_cli.gateway.router as router_mod

    state = _w5.prepare_state(
        monkeypatch,
        tmp_path,
        tier="performance",
        primary="openai/gpt-4o",
        auth={"openai": _w5.CANARY_KEY},
    )
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    calls = [0]

    def echoing_provider_error(**kwargs: Any) -> Any:
        """An upstream error body that echoes the credential the gateway sent."""
        calls[0] += 1
        key = kwargs.get("api_key") or os.environ.get("OPENAI_API_KEY") or "<no key>"
        raise RuntimeError(
            f"401 Unauthorized: invalid api key '{key}' for model {kwargs.get('model')}"
        )

    monkeypatch.setattr(router_mod, "completion", echoing_provider_error)
    router_mod.mark_pii_available()

    with leak_capture.capture(log_dir=state.log_dir, db_path=state.db_path) as captured:
        result = _w5.run_cli(["gateway", "test", "extraction"])
    captured.add_cli_result(result)

    assert calls[0] >= 1, "the dispatch seam was never reached, so this case proves nothing"
    assert result.exit_code == 1
    hits = canary_hits(captured)
    assert hits == {}, f"key material reached surface(s) {hits}: {result.output!r}"


def test_gateway_test_never_logs_the_key_even_at_debug_level(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--debug`` puts the key on the log path; the redaction filter must hold.

    ``router.py:184`` logs the provider key through ``redact_key`` whenever the
    auth entry is a plain string, and ``gateway/redaction.py:30-61`` filters
    every root handler. The log file must therefore carry the redacted form and
    never the key body.
    """
    import openreview_cli.gateway.router as router_mod

    state = _w5.prepare_state(
        monkeypatch,
        tmp_path,
        tier="performance",
        primary="openai/gpt-4o",
        auth={"openai": _w5.CANARY_KEY},
    )
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    faulted = faults.apply(faults.FAULTS["provider_down"], lambda **kw: None)
    monkeypatch.setattr(router_mod, "completion", faulted)
    router_mod.mark_pii_available()

    with leak_capture.capture(log_dir=state.log_dir, db_path=state.db_path) as captured:
        result = _w5.run_cli(["--debug", "gateway", "test", "extraction"])
    captured.add_cli_result(result)

    log_text = "\n".join(captured.logs.values())
    # Reachability: the key DID reach the log handler, in redacted form. The
    # handler renders ``redact_key("sk-test-CANARY-123")`` as ``sk-t`` + mask,
    # which the filter's literal ``sk-`` pattern pass then renders as ``***t``.
    assert "***t" in log_text, (
        "reachability: no redacted key on the log path, so this case proves nothing: "
        f"{log_text[-400:]!r}"
    )
    assert canary_hits(captured) == {}, f"key material on surface(s): {canary_hits(captured)}"
