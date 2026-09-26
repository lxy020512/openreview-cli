"""W4 fuzz suite: the playbook YAML and prompt-store boundaries.

The plan calls this boundary out as "the same class as config.yml" and already
corruption-prone (commit ``1a02d26`` kept the playbook corruption scan off the TUI
render path). Two loaders are covered:

* ``review/playbook.py:load_playbook`` parses a YAML file and emits
  ``PlaybookLoadError`` for bad input. It guards a YAML parse error and a
  non-mapping top level, but ``_parse_category`` (``playbook.py:123``) calls
  ``dict(raw)`` on each category without checking it is a mapping, so a
  ``categories: [1, 2]`` playbook raises a raw ``TypeError``.
* ``tui/domain/playbooks.py:corrupt_playbook_ids_via_tui`` is the corruption scan;
  it catches only ``(PlaybookLoadError, ValueError)`` (``:70``), so the same raw
  ``TypeError`` escapes the scanner that exists to classify corrupt playbooks.

``PromptStore`` (``prompts/store.py``) is covered with a real round trip on a
migrated and on a freshly initialised database.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from openreview_cli.prompts.store import PromptStore
from openreview_cli.review import playbook as playbook_module
from openreview_cli.storage.playbooks import import_playbook_yaml
from openreview_cli.tui.domain import playbooks as tui_playbooks
from tests.fuzz import _state_probe
from tests.helpers import corpus_state

if TYPE_CHECKING:
    from collections.abc import Callable

pytestmark = pytest.mark.fuzz

# A JSON-encoded playbook payload (the storage format load_playbook_from_db reads)
# whose categories are scalars, not mappings.
_BAD_CATEGORIES_PAYLOAD = json.dumps(
    {
        "id": "badcat",
        "mode": "precheck",
        "metadata": {"version": 1, "description": "d", "author": "a"},
        "categories": [1, 2],
    }
)


def _capture_load(path: Path) -> tuple[bool, BaseException | None]:
    """Load *path*, returning ``(accepted, exception)`` without raising."""
    try:
        playbook_module.load_playbook(path)
    except Exception as exc:  # the oracle classifies every exception
        return False, exc
    return True, None


def _assert_load_rejected(outcome: tuple[bool, BaseException | None]) -> None:
    """Oracle: a malformed playbook must be rejected with ``PlaybookLoadError``."""
    accepted, exc = outcome
    assert not accepted, "a malformed playbook was accepted"
    assert isinstance(exc, playbook_module.PlaybookLoadError), (
        f"uncaught {type(exc).__name__}: {exc!r}"
    )


# ── load_playbook on a malformed YAML file ─────────────────────────────────


def test_load_playbook_corrupt_yaml_is_a_playbook_error(tmp_path: Path) -> None:
    """A YAML syntax error is already mapped to ``PlaybookLoadError`` (handled)."""
    path = corpus_state.corrupt_playbook_yaml(tmp_path)
    with _state_probe.count_calls(playbook_module, "load_playbook") as hits:
        outcome = _capture_load(path)
    assert hits[0] >= 1
    _assert_load_rejected(outcome)


@pytest.mark.xfail(
    strict=True,
    reason="RT-024: a playbook whose categories are scalars makes _parse_category "
    "(review/playbook.py:123, dict(raw)) raise a raw TypeError instead of PlaybookLoadError",
)
def test_load_playbook_wrong_typed_categories_is_a_playbook_error(tmp_path: Path) -> None:
    path = corpus_state.wrong_typed_playbook_yaml(tmp_path)
    with _state_probe.count_calls(playbook_module, "_parse_category") as hits:
        outcome = _capture_load(path)
    assert hits[0] >= 1
    _assert_load_rejected(outcome)


# ── the corruption scan must classify, not crash ───────────────────────────


@pytest.mark.xfail(
    strict=True,
    reason="RT-024: corrupt_playbook_ids_via_tui (tui/domain/playbooks.py:70) catches only "
    "PlaybookLoadError/ValueError, so a scalar-category playbook raises a raw TypeError out of "
    "the scan instead of being classified corrupt",
)
def test_corrupt_playbook_scan_classifies_a_scalar_categories_playbook(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state = _state_probe.prepare_state(monkeypatch, tmp_path)
    import_playbook_yaml(state.db_path, "badcat", _BAD_CATEGORIES_PAYLOAD)
    with _state_probe.count_calls(tui_playbooks, "load_playbook_from_db") as hits:
        caught: BaseException | None = None
        result: set[str] | None = None
        try:
            result = tui_playbooks.corrupt_playbook_ids_via_tui(["badcat"])
        except Exception as exc:  # the scan must not raise
            caught = exc
    assert hits[0] >= 1, "the corruption scan never reached the loader"
    assert caught is None, f"the scan raised {type(caught).__name__}: {caught!r}"
    assert result == {"badcat"}


def test_corrupt_playbook_scan_classifies_non_json_and_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The documented corruption shapes (bad JSON, absent id) are classified."""
    state = _state_probe.prepare_state(monkeypatch, tmp_path)
    import_playbook_yaml(state.db_path, "notjson", "this is not JSON")
    with _state_probe.count_calls(tui_playbooks, "load_playbook_from_db") as hits:
        result = tui_playbooks.corrupt_playbook_ids_via_tui(["notjson", "absent"])
    assert hits[0] >= 1
    assert result == {"notjson", "absent"}


# ── PromptStore on real / hostile databases ────────────────────────────────


def test_prompt_store_round_trips_on_a_migrated_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    state = _state_probe.prepare_state(monkeypatch, tmp_path)
    store = PromptStore(state.db_path)
    with _state_probe.count_calls(PromptStore, "init") as hits:
        store.init()
    assert hits[0] >= 1
    store.create("probe", "hello world", tags=["t"])
    fetched = store.get_latest("probe")
    assert fetched.content == "hello world"
    assert [p.name for p in store.list()] == ["probe"]


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(corpus_state.zero_byte_sqlite, id="zero-byte"),
        pytest.param(corpus_state.missing_table_sqlite, id="missing-table"),
    ],
)
def test_prompt_store_initialises_a_hostile_database(
    build: Callable[[Path], Path], tmp_path: Path
) -> None:
    path = build(tmp_path)
    store = PromptStore(path)
    with _state_probe.count_calls(PromptStore, "init") as hits:
        store.init()
    assert hits[0] >= 1
    store.create("probe", "content")
    assert store.get_latest("probe").content == "content"


# ── Negative control: the oracle must be able to fail ───────────────────────


def test_negative_control_playbook_oracle_rejects_a_valid_playbook() -> None:
    """A valid playbook is NOT a playbook error; the oracle must reject it.

    ``load_bundled`` loads a shipped, valid playbook. ``_assert_load_rejected``
    must raise when the load succeeds, or a green suite would certify nothing
    (plan section 9.3).
    """
    valid = playbook_module.BUNDLED_PLAYBOOKS["precheck"]
    with _state_probe.count_calls(playbook_module, "load_playbook") as hits:
        outcome = _capture_load(valid)
    assert hits[0] >= 1
    assert outcome[0] is True, "the bundled playbook must load"
    with pytest.raises(AssertionError):
        _assert_load_rejected(outcome)
