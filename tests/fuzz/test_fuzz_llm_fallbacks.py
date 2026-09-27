"""W3b fuzz suite: the documented fallback contract of the review fallback modules.

For each of ``review/prompts.py``, ``review/qa.py``, ``review/playbook.py`` and
``review/report.py`` this suite asserts what the module ACTUALLY does with a
malformed/hostile model reply, after reading the code:

* ``prompts._parse_json`` returns the caller's *exact* fallback object on a
  parse failure (``prompts.py:11-17``) — never a partially-populated dict.
* ``qa._parse_qa_response`` returns the exact documented fallback
  (``qa.py:106-119``) and ``qa.verify_assessment`` degrades to
  ``qa_verdict = uncertain`` + Amber with no partial revision copied onto the
  assessment (``qa.py:82-87``).
* ``playbook.py`` has NO model-reply fallback: it fails loud with
  ``PlaybookLoadError`` (``playbook.py:48-70``) — recorded as absent rather
  than invented.
* ``report.py`` has no model-reply fallback either: an unrecognised report
  object is classified ``unknown`` and a malformed export file is skipped with
  a logged warning (``report.py:316-326``, ``:427-432``).

RT-015 is fixed: ``_parse_json`` returns the caller's exact fallback object for
any non-dict reply, so ``qa._parse_qa_response`` inherits the guard.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest

from openreview_cli.review import prompts as _prompts
from openreview_cli.review import qa as _qa
from openreview_cli.review.models import QAVerdict
from openreview_cli.review.playbook import PlaybookLoadError, load_playbook
from openreview_cli.review.report import _detect_report_shape, batch_export_reports
from tests.fuzz import _llm_probe
from tests.helpers import corpus_llm

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

pytestmark = pytest.mark.fuzz

# qa.py:108-116 — the exact documented fallback object.
_QA_FALLBACK: dict[str, object] = {
    "verdict": "uncertain",
    "revised_position": None,
    "rationale": "Unparseable QA response",
    "citation_valid": False,
    "position_valid": False,
    "category_valid": False,
    "confidence_valid": False,
}


# ── prompts._parse_json ─────────────────────────────────────────────────────


def test_prompts_parse_json_returns_the_exact_fallback_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    counter = _llm_probe.instrument(monkeypatch, _prompts, "strip_fences")
    fallback: dict[str, object] = {"verdict": "uncertain"}
    assert _prompts._parse_json("this is not JSON", fallback) is fallback
    assert counter.calls >= 1


def test_prompts_parse_json_round_trips_a_valid_object(monkeypatch: pytest.MonkeyPatch) -> None:
    counter = _llm_probe.instrument(monkeypatch, _prompts, "strip_fences")
    assert _prompts._parse_json('{"a": 1}', {}) == {"a": 1}
    assert counter.calls >= 1


@pytest.mark.parametrize(
    "build",
    [corpus_llm.json_null, corpus_llm.json_string_not_object, corpus_llm.enormous_array],
    ids=["null", "string", "array"],
)
def test_prompts_parse_json_never_returns_a_non_dict(build: Callable[[], str]) -> None:
    """The declared ``-> dict`` contract: any reply maps to a dict."""
    result: object = _prompts._parse_json(build(), {})
    assert isinstance(result, dict)


# ── qa._parse_qa_response / verify_assessment ───────────────────────────────


def test_qa_parse_response_returns_the_documented_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    counter = _llm_probe.instrument(monkeypatch, _qa, "_parse_json")
    assert _qa._parse_qa_response("this is not JSON") == _QA_FALLBACK
    assert counter.calls >= 1


@pytest.mark.parametrize(
    "build",
    [corpus_llm.json_null, corpus_llm.enormous_array, corpus_llm.json_string_not_object],
    ids=["null", "array", "string"],
)
def test_qa_parse_response_never_raises_on_a_non_object(build: Callable[[], str]) -> None:
    result: object = _qa._parse_qa_response(build())
    assert isinstance(result, dict)


def test_qa_verify_assessment_falls_back_cleanly(monkeypatch: pytest.MonkeyPatch) -> None:
    """A malformed QA reply degrades to uncertain + Amber with no partial object."""
    gateway = _llm_probe.install_gateway(monkeypatch, _qa, "this is not JSON")
    assessment = _llm_probe.make_assessment()
    result = _qa.verify_assessment(assessment, _llm_probe.make_category(), "qa-slot")
    assert gateway.chat_call_count == 1
    assert result is assessment, "verify_assessment must not return a partial copy"
    assert result.qa_verdict is QAVerdict.uncertain
    assert result.is_amber is True
    assert result.qa_revised_position is None
    assert result.qa_revised_rationale is None
    assert result.error is None


# ── playbook.py: no silent fallback, fail loud ──────────────────────────────


def test_playbook_load_fails_loud_on_a_hostile_playbook(tmp_path: Path) -> None:
    """``playbook.py`` has no model-reply fallback; a malformed playbook is rejected."""
    hostile = {
        "empty": "",
        "null": "null",
        "string": "just a string",
        "list": "- a\n- b\n",
        "missing-fields": "id: x\nmode: y\n",
    }
    for name, text in hostile.items():
        path = tmp_path / f"{name}.yaml"
        path.write_text(text, encoding="utf-8")
        with pytest.raises(PlaybookLoadError):
            load_playbook(path)


# ── report.py: shape detection + skip-with-warning ──────────────────────────


def test_report_shape_detection_defaults_to_unknown() -> None:
    for obj in ({"foo": "bar"}, {}, {"assessments": 1}, {"clauses": 1}, {"summary": "x"}):
        assert _detect_report_shape(obj) == "unknown"


def test_report_batch_export_skips_a_malformed_file_with_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    malformed = tmp_path / "bad.json"
    malformed.write_text("this is not JSON", encoding="utf-8")
    output_dir = tmp_path / "out"
    with caplog.at_level(logging.WARNING, logger="openreview_cli.review.report"):
        written = batch_export_reports([malformed], "json", output_dir, mode="precheck")
    assert written == [], "a malformed export file must write nothing"
    assert "Skipping" in caplog.text, "the skip must be signalled, not silent"
    assert output_dir.exists()
    assert not any(output_dir.iterdir())


# ── Negative control: the exact-fallback oracle must be able to fail ────────


def _assert_qa_fallback_shape(result: dict[str, object]) -> None:
    assert result == _QA_FALLBACK


def test_negative_control_fallback_oracle_rejects_a_partial_object() -> None:
    """The exact-fallback oracle must reject a partially-populated object."""
    with pytest.raises(AssertionError):
        _assert_qa_fallback_shape({"verdict": "agree"})
    with pytest.raises(AssertionError):
        _assert_qa_fallback_shape({**_QA_FALLBACK, "position_valid": True})
