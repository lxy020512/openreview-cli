"""W6 red-team suite: secret and raw-contract leakage across every surface.

Attacks guarantee 2 as a whole (plan section 5, Track B): secrets, keys and raw
contract text must not reach stdout, stderr, a log file or a database text
column. W5 opened RT-034 for one command path (``gateway test`` echoing an
upstream error body); W6 does not refile it. W6 owns the cross-surface
enumeration, the raw-contract sentinel, and guarantee 4's WRITE half.

Method. Two synthetic secrets and one raw-contract sentinel are injected through
the real paths — the isolated ``auth.json``/``config.yml`` and the environment —
never a mock. Real CLI commands then run under ``leak_capture``, which harvests
every surface: stdout, stderr, each log file and each populated TEXT-affinity DB
column. Absence is asserted per surface, and the redacted form is asserted
present in the log so the absence cannot be vacuous.

The positive control (``leak_capture.assert_detects``) and the deliberate-failure
demonstration are load bearing: without them an absence assertion proves nothing
(plan section 9.3 rule 1).
"""

from __future__ import annotations

import os
import platform
import stat
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.helpers import leak_capture
from tests.redteam import _w6_probe as _w6

pytestmark = pytest.mark.redteam


@pytest.fixture(autouse=True)
def _detach_cli_log_handlers() -> Iterator[None]:
    """Drop owned handlers and any credential ``_set_env_vars`` seeded.

    ``Gateway._set_env_vars`` writes the resolved key into ``os.environ`` and
    never clears it on this path, so it must be removed or it would leak into
    later tests (the same hazard the e2e redaction test guards against).
    """
    yield
    _w6.detach_owned_handlers()
    os.environ.pop("OPENAI_API_KEY", None)


def _real_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _w6.State:
    """A state whose secrets arrived through the real config/env path.

    ``SK_KEY`` is seeded in ``auth.json`` and its env var is cleared so the
    product's own ``_set_env_vars`` debug log fires (router.py:184).
    ``PROVIDER_TOKEN`` is seeded in ``auth.json`` AND the environment, and
    ``ENC_TOKEN`` lives only in ``config.yml``.
    """
    state = _w6.prepare_state(
        monkeypatch,
        tmp_path,
        tier="balanced",
        primary="openai/gpt-4o",
        auth={"openai": _w6.SK_KEY, "huggingface": _w6.PROVIDER_TOKEN},
        enc_key=_w6.ENC_TOKEN,
    )
    monkeypatch.setenv("HUGGINGFACE_API_KEY", _w6.PROVIDER_TOKEN)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    return state


# ── Positive control: the gate harness must see a leak on every surface ─────


def test_positive_control_canary_is_seen_on_every_enumerated_surface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plant the canary on stdout, stderr, a log file and a DB text column.

    The per-surface table must read PASS (detected) everywhere. This is the
    positive control that makes every absence assertion below meaningful.
    """
    state = _w6.prepare_state(monkeypatch, tmp_path)
    (state.log_dir / "planted.log").write_text(
        f"2026-01-01 [INFO] planted {_w6.SK_KEY}\n", encoding="utf-8"
    )
    _w6.plant_canary_in_db(state.db_path, _w6.SK_KEY)

    with leak_capture.capture(log_dir=state.log_dir, db_path=state.db_path) as captured:
        sys.stdout.write(f"planted {_w6.SK_KEY} on stdout\n")
        sys.stderr.write(f"planted {_w6.SK_KEY} on stderr\n")

    expected = {"stdout", "stderr", "planted.log", "clients.name"}
    found = set(captured.find_all(_w6.SK_KEY))
    assert {Path(name).name for name in found} == expected, (
        f"planted surface set mismatch: found {sorted(found)}"
    )
    # Per-surface PASS/FAIL over the four enumerated surfaces (log keyed by path).
    enumerated = {"stdout", "stderr", *captured.logs, "clients.name"}
    table = _w6.detection_table(captured, _w6.SK_KEY, enumerated)
    assert not _w6.failures(table), f"surface missed the planted canary:\n{_w6.render_table(table)}"
    leak_capture.assert_detects(captured, _w6.SK_KEY)


def test_negative_control_absent_oracle_rejects_a_planted_canary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``assert_absent`` must raise, naming every surface, when a canary is present."""
    state = _w6.prepare_state(monkeypatch, tmp_path)
    _w6.plant_canary_in_db(state.db_path, _w6.SK_KEY)

    with leak_capture.capture(log_dir=state.log_dir, db_path=state.db_path) as captured:
        sys.stdout.write(f"planted {_w6.SK_KEY}\n")

    assert captured.find_all(_w6.SK_KEY), "the oracle saw nothing; the control is vacuous"
    with pytest.raises(AssertionError, match="found on surface"):
        captured.assert_absent(_w6.SK_KEY)


def test_negative_control_log_surface_can_carry_a_raw_key() -> None:
    """The deliberate RED: with no handler filter, the log surface leaks a raw key.

    This is the planted failure that proves the log surface is real and that the
    absence assertions in the cases below can fail. Captured verbatim in
    ``draft/evidence/W6_negative_control_secret_leakage.txt``.
    """
    rendered = _w6.demonstrate_leak_is_visible(_w6.SK_KEY)
    assert _w6.SK_KEY in rendered, (
        "the log surface did not carry the raw key, so the demonstration proves nothing"
    )
    assert _w6.SK_BODY in rendered


# ── The real CLI runs: secrets and raw contract must be absent everywhere ───


@pytest.mark.parametrize("mode", ["default", "--debug", "--verbose"])
def test_secrets_and_raw_contract_absent_from_every_surface(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run the real CLI, then require a per-surface PASS for every value."""
    state = _real_state(tmp_path, monkeypatch)
    docx = _w6.build_raw_contract_docx(tmp_path)
    prefix = [] if mode == "default" else [mode]

    with leak_capture.capture(log_dir=state.log_dir, db_path=state.db_path) as captured:
        status = _w6.run_cli([*prefix, "gateway", "status"])
        precheck = _w6.run_cli(
            [*prefix, "precheck", "-d", str(docx), "--output", str(state.output_dir)]
        )
    captured.add_cli_result(status)
    captured.add_cli_result(precheck)

    assert status.exit_code == 0, f"gateway status failed: {status.output!r}"
    assert precheck.exit_code == 0, f"precheck failed: {precheck.output!r}"

    # Reachability: the raw contract really carried the sentinel and the strip
    # really ran (a row was written), so the absence assertion is not vacuous.
    assert _w6.RAW_SENTINEL in _w6.raw_contract_text()
    assert _w6.PII_EMAIL in _w6.raw_contract_text()
    audit_rows = captured.db_text.get("pii_audit_trail.document_hash")
    assert audit_rows, (
        "no pii_audit_trail row was written, so the PII strip never ran: "
        f"{sorted(captured.db_text)}"
    )

    needles = {
        "sk-key": _w6.SK_KEY,
        "sk-key-body": _w6.SK_BODY,
        "provider-token": _w6.PROVIDER_TOKEN,
        "provider-token-body": _w6.PROVIDER_TOKEN_BODY,
        "config-only-token": _w6.ENC_TOKEN,
        "raw-contract-sentinel": _w6.RAW_SENTINEL,
        "raw-contract-pii-email": _w6.PII_EMAIL,
    }
    for label, needle in needles.items():
        table = _w6.absence_table(captured, needle)
        assert not _w6.failures(table), (
            f"{label} ({needle!r}) leaked on surface(s): {_w6.failures(table)}\n"
            f"{_w6.render_table(table)}"
        )

    if mode == "--debug":
        log_text = "\n".join(captured.logs.values())
        assert "***t" in log_text, (
            "reachability: the redacted key never reached the log, so the "
            f"filter/producer did not fire: {log_text[-500:]!r}"
        )


# ── guarantee 4, the WRITE half: auth.json mode 600 and never logged ────────

# test_auth.py:24,32,167 already assert the read-path creation/repair modes and
# save_key's creation mode; those are NOT re-asserted. The write paths below
# (write_auth on an existing file, save_provider_credentials, the CLI provider
# add) are the genuine gap in the same guarantee.


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_write_auth_repairs_an_existing_permissive_file(tmp_path: Path) -> None:
    """``write_auth`` on a pre-existing 0o644 file must leave it 0o600."""
    if platform.system() == "Windows":
        pytest.skip("Unix-only permission test")
    from openreview_cli.config.auth import write_auth

    auth_path = tmp_path / "auth.json"
    auth_path.write_text("{}", encoding="utf-8")
    auth_path.chmod(0o644)

    write_auth(auth_path, {"openai": _w6.SK_KEY})

    assert _mode(auth_path) == 0o600, f"write left mode {oct(_mode(auth_path))}"


def test_save_provider_credentials_creates_0600(tmp_path: Path) -> None:
    """``save_provider_credentials`` (the CLI provider-add path) creates 0o600."""
    if platform.system() == "Windows":
        pytest.skip("Unix-only permission test")
    from openreview_cli.config.auth import save_provider_credentials

    auth_path = tmp_path / "auth.json"
    save_provider_credentials(auth_path, "w6prov", {"W6PROV_API_KEY": _w6.PROVIDER_TOKEN})

    assert _mode(auth_path) == 0o600, f"write left mode {oct(_mode(auth_path))}"
    assert _w6.PROVIDER_TOKEN in auth_path.read_text(encoding="utf-8")


def test_cli_provider_add_writes_0600_and_never_logs_the_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real CLI write path: mode 600, and the credential reaches no surface."""
    if platform.system() == "Windows":
        pytest.skip("Unix-only permission test")
    state = _w6.prepare_state(monkeypatch, tmp_path)

    with leak_capture.capture(log_dir=state.log_dir, db_path=state.db_path) as captured:
        result = _w6.run_cli(
            [
                "--debug",
                "gateway",
                "provider",
                "add",
                "w6prov",
                "--base-url",
                "http://localhost:9/v1",
                "--cred",
                f"W6PROV_API_KEY={_w6.SK_KEY}",
            ]
        )
    captured.add_cli_result(result)

    assert result.exit_code == 0, f"provider add failed: {result.output!r} {result.exception!r}"
    written = _w6.read_auth(state.auth_path)
    assert written, "reachability: provider add wrote no auth.json"
    assert _mode(state.auth_path) == 0o600, f"mode {oct(_mode(state.auth_path))}"

    for needle in (_w6.SK_KEY, _w6.SK_BODY):
        table = _w6.absence_table(captured, needle)
        assert not _w6.failures(table), (
            f"credential leaked on surface(s): {_w6.failures(table)}\n{_w6.render_table(table)}"
        )
    assert captured.logs, "no log file was harvested, so the log surface is untested"
