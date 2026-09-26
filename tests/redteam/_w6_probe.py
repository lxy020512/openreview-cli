"""Private W6 red-team harness: the isolated state, the surface table, the fixture.

W6 attacks guarantee 2 (secrets, keys and raw contract text never reach stdout,
stderr, a log file or the database) and guarantee 5 (PII rows carry no
plaintext and expire) across every output surface. The plan keeps the redteam
tree conftest-free (section 7), so this is a private module imported directly,
following the W5 precedent (``tests/redteam/_w5_probe.py``).

Reuse, not duplication: the XDG isolation and the one-shot ``CliRunner`` come
from ``_w5_probe``. W6 adds only what its own suites need:

* the three synthetic secrets and their bodies, so a partial redaction is
  caught as well as a whole-value leak;
* :func:`build_raw_contract_docx`, which materializes the committed text
  fixture into a real DOCX the CLI can parse (no committed binary);
* :func:`absence_table` / :func:`detection_table`, the per-surface PASS/FAIL
  and DETECTED/MISSED tables the register's surface enumeration reports.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

import pytest

from tests.helpers import leak_capture
from tests.redteam import _w5_probe as _w5
from tests.redteam._w5_probe import State

# Re-exported for W6 suites (reuse, not duplication).
__all__ = [
    "ENC_TOKEN",
    "FIXTURES_DIR",
    "LOG_FILENAME",
    "PII_EMAIL",
    "PROVIDER_TOKEN",
    "PROVIDER_TOKEN_BODY",
    "RAW_CONTRACT_TXT",
    "RAW_SENTINEL",
    "REPO_ROOT",
    "SK_BODY",
    "SK_KEY",
    "State",
    "absence_table",
    "build_raw_contract_docx",
    "config_yml",
    "demonstrate_leak_is_visible",
    "detach_owned_handlers",
    "detection_table",
    "failures",
    "plant_canary_in_auth",
    "plant_canary_in_db",
    "prepare_state",
    "raw_contract_text",
    "read_auth",
    "render_table",
    "run_cli",
    "surface_names",
]

REPO_ROOT = _w5.REPO_ROOT
FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures" / "redteam"
RAW_CONTRACT_TXT = FIXTURES_DIR / "raw_contract_sentinel.txt"

# The one-shot Typer CliRunner, reused from W5.
run_cli = _w5.run_cli

# ── Synthetic secret material. Never a real credential (plan section 9.7). ──

# Shaped like the product's own `sk-` keys, so `_KEY_VALUE_RE` can match it.
SK_KEY = "sk-test-W6CANARY-9f2a7"
SK_BODY = "W6CANARY-9f2a7"

# A provider token that does NOT match `_KEY_VALUE_RE` (not sk/pk/rk shaped):
# the filter's literal pattern list is the only backstop for its label.
PROVIDER_TOKEN = "hf_W6TOKEN3c8b1d5e92f4a7c6"
PROVIDER_TOKEN_BODY = "W6TOKEN3c8b1d5e92f4a7c6"

# A secret that lives only in config.yml (`privacy.pii_encryption_key`).
# The field validator requires exactly 16, 24 or 32 bytes (loader.py:182-184).
ENC_TOKEN = "W6-ENCFERNET-55aa91c3d7e2f9a0b1c"

# Planted in the raw contract fixture only; absent from every redacted artifact.
RAW_SENTINEL = "Zarquon-CANARY-7f3a9"
# A PII span in the same fixture, so the strip has something real to remove.
PII_EMAIL = "zq.canary.7f3a9@example-inbound.test"

LOG_FILENAME = "openreview.log"


def config_yml(tier: str, primary: str, enc_key: str | None = None) -> str:
    """The isolated ``config.yml`` body W6 drives the real CLI against."""
    lines = ["privacy:", f"  tier: {tier}"]
    if enc_key is not None:
        lines.append(f"  pii_encryption_key: {enc_key}")
    lines += [
        "gateway:",
        "  models:",
        "    extraction:",
        f"      primary: {primary}",
        "  fallback:",
        "    retries: 2",
        "    retry_delay: 0.0",
        "    timeout: 5",
    ]
    return "\n".join(lines) + "\n"


def prepare_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    tier: str = "balanced",
    primary: str = "openai/gpt-4o",
    auth: dict[str, object] | None = None,
    enc_key: str | None = None,
) -> State:
    """Reuse W5's XDG isolation, then write W6's own config body."""
    state = _w5.prepare_state(monkeypatch, tmp_path, tier=tier, primary=primary, auth=auth)
    state.config_path.write_text(config_yml(tier, primary, enc_key), encoding="utf-8")
    return state


def build_raw_contract_docx(directory: Path, name: str = "raw_contract.docx") -> Path:
    """Materialize the committed text fixture as a DOCX the parser accepts.

    The plan forbids new committed binaries (section 6), so the raw contract is
    assembled at test time from the tracked text fixture.
    """
    from docx import Document

    path = directory / name
    document = Document()
    for line in RAW_CONTRACT_TXT.read_text(encoding="utf-8").splitlines():
        document.add_paragraph(line)
    document.save(str(path))
    return path


def raw_contract_text() -> str:
    """The sentinel-bearing text as the CLI will parse it, for reachability."""
    return RAW_CONTRACT_TXT.read_text(encoding="utf-8")


def plant_canary_in_db(db_path: Path, canary: str) -> None:
    """Plant *canary* in a real TEXT-affinity column (``clients.name``)."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO clients (id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
            ("w6-canary-client", canary, "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
        )
        conn.commit()
    finally:
        conn.close()


def plant_canary_in_auth(state: _w5.State, canary: str) -> None:
    """Write *canary* through the product's real auth writer (guarantee 4 path)."""
    from openreview_cli.config.auth import write_auth

    write_auth(state.auth_path, {"openai": canary})


# ── Per-surface tables (the register's fixed observation domain) ────────────


def surface_names(captured: leak_capture.Captured) -> list[str]:
    """Every captured surface, sorted, so the table is deterministic."""
    return sorted(captured.surfaces())


def absence_table(captured: leak_capture.Captured, needle: str) -> dict[str, str]:
    """Surface -> PASS when *needle* is absent, FAIL when it is present."""
    return {
        name: ("FAIL" if needle in text else "PASS") for name, text in captured.surfaces().items()
    }


def detection_table(
    captured: leak_capture.Captured, needle: str, expected: set[str]
) -> dict[str, str]:
    """Surface -> PASS when the planted *needle* is seen there, FAIL when missed.

    Only the *expected* surfaces (the ones a case planted on) are reported, so
    the table is a per-surface PASS/FAIL over the planted domain rather than a
    MISSED verdict for every unrelated column.
    """
    found = set(captured.find_all(needle))
    return {name: ("PASS" if name in found else "FAIL") for name in sorted(expected)}


def render_table(table: dict[str, str]) -> str:
    """A stable, readable rendering of a per-surface PASS/FAIL table."""
    width = max((len(name) for name in table), default=0)
    return "\n".join(f"  {name.ljust(width)}  {verdict}" for name, verdict in table.items())


def failures(table: dict[str, str]) -> dict[str, str]:
    """Only the surfaces whose verdict is FAIL."""
    return {name: verdict for name, verdict in table.items() if verdict == "FAIL"}


# ── The RED demonstration: the absence oracle must be able to fail ──────────


def demonstrate_leak_is_visible(canary: str) -> str:
    """Log a raw key through a product logger with the filter deliberately off.

    Returns the rendered stream. Used by each suite's negative control to show
    that the log surface CAN carry the material, so a green absence assertion
    is not vacuous (plan section 9.3 rule 1).
    """
    import io

    root = logging.getLogger()
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    root.addHandler(handler)
    level = root.level
    root.setLevel(logging.DEBUG)
    try:
        logging.getLogger("openreview_cli.gateway.router").debug("Set OPENAI_API_KEY to %s", canary)
    finally:
        root.removeHandler(handler)
        root.setLevel(level)
    return stream.getvalue()


# ── Shared teardown: never leave this test's handler behind ────────────────


def detach_owned_handlers() -> None:
    """Drop the handlers ``_init`` installs, so none holds this test's buffer.

    ``_init`` attaches a FileHandler and a StreamHandler (``app.py:246-252``)
    bound to the streams that exist at invoke time — under ``leak_capture`` that
    is a StringIO which dies with the test. A later emit into the stale buffer
    prints a ``--- Logging error ---`` block into an unrelated test's output.
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_openreview_owned", False):
            root.removeHandler(handler)
            handler.close()


def read_auth(path: Path) -> dict[str, object]:
    """Parse an auth.json written by the product."""
    data: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
    return data
