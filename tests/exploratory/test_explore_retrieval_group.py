"""Exploratory probes: the retrieval group ``ingest``, ``retrieve``,
``index-status`` and ``index-clear``.

Covers the argument-validation failure modes and records the surprise that the
documented retrieval exit codes 40 to 43 (``errors.py:22-25``) are never emitted
by the CLI.  Findings are ``RT-NNN`` rows in the register.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import Result

EXIT_USER_ERROR = 1
EXIT_USAGE = 2
EXIT_RETRIEVAL_INDEX_NOT_FOUND = 40


def _text(result: Result) -> str:
    raw = (getattr(result, "output", "") or "") + (getattr(result, "stderr", "") or "")
    return " ".join(raw.split())


def _write_ndax(path: Path, chunks: list[object]) -> Path:
    path.write_text(json.dumps(chunks), encoding="utf-8")
    return path


_DOC_ID = "doc-" + "a" * 40
_CHUNK: dict[str, object] = {
    "document_id": _DOC_ID,
    "chunk_id": "c1",
    "text": "The parties shall keep information confidential.",
    "clause_heading": "Confidentiality",
    "clause_level": 1,
}


# ── ingest ────────────────────────────────────────────────────────────────


@pytest.mark.fast
def test_ingest_missing_file_is_user_error(
    isolated_dirs: Path, invoke: Callable[[list[str]], Result], tmp_path: Path
) -> None:
    result = invoke(["ingest", str(tmp_path / "missing.ndax")])
    assert result.exit_code == EXIT_USER_ERROR, (result.exit_code, _text(result))
    assert "File not found" in _text(result)


@pytest.mark.fast
def test_ingest_malformed_json_is_user_error(
    isolated_dirs: Path, invoke: Callable[[list[str]], Result], tmp_path: Path
) -> None:
    bad = tmp_path / "bad.ndax"
    bad.write_text("not json at all", encoding="utf-8")
    result = invoke(["ingest", str(bad)])
    assert result.exit_code == EXIT_USER_ERROR, (result.exit_code, _text(result))
    assert "not a valid .ndax JSON file" in _text(result)


@pytest.mark.fast
def test_ingest_empty_chunk_list_is_user_error(
    isolated_dirs: Path, invoke: Callable[[list[str]], Result], tmp_path: Path
) -> None:
    empty = _write_ndax(tmp_path / "empty.ndax", [])
    result = invoke(["ingest", str(empty)])
    assert result.exit_code == EXIT_USER_ERROR, (result.exit_code, _text(result))
    assert "No chunks found" in _text(result)


@pytest.mark.fast
def test_ingest_chunk_without_id_is_a_clean_error(
    isolated_dirs: Path, invoke: Callable[[list[str]], Result], tmp_path: Path
) -> None:
    """RT-006 (fixed): a well-formed JSON list whose chunk dicts lack the
    required ``id`` key is now a clean usage error (exit 2) naming the missing
    key, instead of an uncaught ``KeyError('id')`` (raw traceback).
    """
    ndax = _write_ndax(tmp_path / "noid.ndax", [{"document_id": _DOC_ID, "text": "x"}])
    result = invoke(["ingest", str(ndax)])
    assert result.exit_code == EXIT_USAGE, (result.exit_code, _text(result))
    assert "id" in _text(result).lower()


@pytest.mark.fast
def test_ingest_non_object_chunk_after_the_first_is_a_clean_error(
    isolated_dirs: Path, invoke: Callable[[list[str]], Result], tmp_path: Path
) -> None:
    """Follow-up to RT-006: only ``data[0]`` used to be validated, so a non-dict
    element after index 0 escaped as a raw ``AttributeError:(...)'get'`` — exit 1
    with empty stdout. The loader now rejects every element up front (exit 1,
    same file-level message), so nothing reaches chunk normalization.
    """
    ndax = _write_ndax(tmp_path / "nonobj.ndax", [_CHUNK, "not an object"])
    result = invoke(["ingest", str(ndax)])

    assert result.exit_code == EXIT_USER_ERROR, (result.exit_code, _text(result))
    text = _text(result)
    assert "not a valid .ndax JSON file" in text, text
    assert "Traceback" not in text, text
    assert "AttributeError" not in text, text


# ── retrieve ──────────────────────────────────────────────────────────────


@pytest.mark.fast
def test_retrieve_without_document_or_index_is_usage_error(
    isolated_dirs: Path, invoke: Callable[[list[str]], Result]
) -> None:
    result = invoke(["retrieve", "confidentiality"])
    assert result.exit_code == EXIT_USAGE, (result.exit_code, _text(result))
    assert "no previously indexed document" in _text(result).lower()


@pytest.mark.fast
def test_retrieve_missing_file_is_user_error(
    isolated_dirs: Path, invoke: Callable[[list[str]], Result], tmp_path: Path
) -> None:
    result = invoke(["retrieve", "q", str(tmp_path / "missing.ndax")])
    assert result.exit_code == EXIT_USER_ERROR, (result.exit_code, _text(result))
    assert "File not found" in _text(result)


@pytest.mark.fast
def test_retrieve_unindexed_document_is_usage_error(
    isolated_dirs: Path, invoke: Callable[[list[str]], Result], tmp_path: Path
) -> None:
    """An existing document that was never ingested exits 2 (see RT-003)."""
    ndax = _write_ndax(tmp_path / "doc.ndax", [_CHUNK])
    result = invoke(["retrieve", "confidentiality", str(ndax)])
    assert result.exit_code == EXIT_USAGE, (result.exit_code, _text(result))
    assert "Document not indexed" in _text(result)


@pytest.mark.fast
@pytest.mark.xfail(strict=True, reason="RT-003")
def test_retrieve_unindexed_document_uses_the_registry_code(
    isolated_dirs: Path, invoke: Callable[[list[str]], Result], tmp_path: Path
) -> None:
    """RT-003: the registry names ``EXIT_RETRIEVAL_INDEX_NOT_FOUND = 40`` for a
    missing index (``errors.py:22``) and ``retrieval_error`` defaults to it, but
    ``retrieve`` maps ``IndexNotFoundError`` to exit 2 (``app.py:2322-2324``).

    Intended: the documented retrieval code 40.
    """
    ndax = _write_ndax(tmp_path / "doc.ndax", [_CHUNK])
    result = invoke(["retrieve", "confidentiality", str(ndax)])
    assert result.exit_code == EXIT_RETRIEVAL_INDEX_NOT_FOUND, (result.exit_code, _text(result))


@pytest.mark.fast
def test_retrieval_registry_codes_are_unreachable_from_the_cli() -> None:
    """Reachability evidence for RT-003.

    The only producer of codes 40 to 43 is ``errors.retrieval_error``.  Neither
    it nor any ``EXIT_RETRIEVAL_*`` constant is referenced by ``app.py`` (the
    module that registers every subcommand), so no CLI path can emit them.
    """
    import openreview_cli.app as app_module

    source = inspect.getsource(app_module)
    assert "retrieval_error" not in source
    for attribute in (
        "EXIT_RETRIEVAL_INDEX_NOT_FOUND",
        "EXIT_RETRIEVAL_INDEX_CORRUPT",
        "EXIT_RETRIEVAL_INDEX_OUTDATED",
        "EXIT_RETRIEVAL_DIM_MISMATCH",
    ):
        assert attribute not in source, attribute


# ── index-status ──────────────────────────────────────────────────────────


@pytest.mark.fast
def test_index_status_requires_a_file(
    isolated_dirs: Path, invoke: Callable[[list[str]], Result]
) -> None:
    result = invoke(["index-status"])
    assert result.exit_code == EXIT_USER_ERROR, (result.exit_code, _text(result))
    assert "FILE argument required" in _text(result)


@pytest.mark.fast
def test_index_status_missing_file_is_user_error(
    isolated_dirs: Path, invoke: Callable[[list[str]], Result], tmp_path: Path
) -> None:
    result = invoke(["index-status", str(tmp_path / "missing.ndax")])
    assert result.exit_code == EXIT_USER_ERROR, (result.exit_code, _text(result))
    assert "File not found" in _text(result)


@pytest.mark.fast
def test_index_status_unindexed_document_is_usage_error(
    isolated_dirs: Path, invoke: Callable[[list[str]], Result], tmp_path: Path
) -> None:
    ndax = _write_ndax(tmp_path / "doc.ndax", [_CHUNK])
    result = invoke(["index-status", str(ndax)])
    assert result.exit_code == EXIT_USAGE, (result.exit_code, _text(result))
    assert "Document not indexed" in _text(result)


# ── index-clear ───────────────────────────────────────────────────────────


@pytest.mark.fast
def test_index_clear_requires_a_file_unless_all(
    isolated_dirs: Path, invoke: Callable[[list[str]], Result]
) -> None:
    no_arg = invoke(["index-clear"])
    assert no_arg.exit_code == EXIT_USER_ERROR, (no_arg.exit_code, _text(no_arg))
    assert "FILE argument required" in _text(no_arg)

    clear_all = invoke(["index-clear", "--all"])
    assert clear_all.exit_code == 0, (clear_all.exit_code, _text(clear_all))
    assert "Cleared 0 index database(s)" in _text(clear_all)


@pytest.mark.fast
def test_index_clear_unindexed_document_is_usage_error(
    isolated_dirs: Path, invoke: Callable[[list[str]], Result], tmp_path: Path
) -> None:
    ndax = _write_ndax(tmp_path / "doc.ndax", [_CHUNK])
    result = invoke(["index-clear", str(ndax)])
    assert result.exit_code == EXIT_USAGE, (result.exit_code, _text(result))
    assert "Document not indexed" in _text(result)


# ── negative control ──────────────────────────────────────────────────────


@pytest.mark.fast
def test_negative_control_oracle_rejects_a_wrong_exit_code(
    isolated_dirs: Path, invoke: Callable[[list[str]], Result]
) -> None:
    """Negative control: the oracle discriminates. Flipping the inner assertion
    (or removing ``pytest.raises``) makes this case go red."""
    result = invoke(["index-clear"])
    assert result.exit_code == EXIT_USER_ERROR, (result.exit_code, _text(result))
    with pytest.raises(AssertionError):
        assert result.exit_code == 0, "negative control: exit 1 is not exit 0"
