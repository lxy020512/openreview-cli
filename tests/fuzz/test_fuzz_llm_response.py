"""W3a fuzz suite: the LLM-JSON ingestion boundary.

Every corpus variant from ``tests/helpers/corpus_llm.py`` is fed to
``llm_json.strip_fences`` and to ``review.extraction._parse_response`` and must
satisfy one invariant: the parser returns the documented dict shape
(``extraction.py:172``) and no unhandled exception escapes it. ``_parse_response``
is the boundary that decides whether a model reply is usable, so a reply it
cannot parse must degrade to the safe default dict, never crash.

Coverage read-back (plan W3 first task). ``tests/unit/test_llm_json.py:6-31``
already asserts the ``strip_fences`` output for four fenced/plain shapes. This
suite does not re-assert that; the *gap* it fills is ``_parse_response`` (never
covered before) plus the hostile variants the unit file does not have — see the
register's W3 coverage read-back table.

Known-broken behaviour (RT-014, RT-016, RT-017) is asserted as the CORRECT
behaviour and carries ``xfail(strict=True)`` tied to a register row.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from openreview_cli.llm_json import strip_fences
from openreview_cli.review import extraction as _extraction
from openreview_cli.review.models import Position
from tests.fuzz import _llm_probe
from tests.helpers import corpus_llm

if TYPE_CHECKING:
    from collections.abc import Callable

pytestmark = pytest.mark.fuzz

# Deterministic profile (plan section 13, "Hypothesis determinism"): a fixed
# seed and a low example budget, so a failure is reproducible rather than flaky.
settings.register_profile(
    "w3_fuzz",
    max_examples=25,
    derandomize=True,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
settings.load_profile("w3_fuzz")

_CATEGORY = _llm_probe.make_category()
_CLAUSE = "The parties shall keep information confidential."

# Variants _parse_response must survive by returning the documented dict shape.
# ``parsed`` is True when the reply is well-formed enough to be unwrapped and
# parsed into real values rather than the safe default.
_DICT_VARIANTS: list[tuple[str, Callable[[], str], bool]] = [
    ("fenced", corpus_llm.fenced, True),
    ("fenced-without-newline", corpus_llm.fenced_without_newline, False),
    ("truncated", corpus_llm.truncated, False),
    ("deeply-nested", corpus_llm.deeply_nested, False),
    ("control-chars", corpus_llm.control_chars, False),
    ("empty", corpus_llm.json_empty, False),
    ("embedded-backticks", corpus_llm.embedded_backticks, True),
]

# Valid JSON that is not an object: the reply parses but has no ``.get``.
_NON_OBJECT_VARIANTS: list[tuple[str, Callable[[], str]]] = [
    ("enormous-array", corpus_llm.enormous_array),
    ("json-string-not-object", corpus_llm.json_string_not_object),
    ("json-null", corpus_llm.json_null),
]


@pytest.mark.parametrize(
    ("variant", "build", "parsed"),
    _DICT_VARIANTS,
    ids=[variant for variant, _build, _parsed in _DICT_VARIANTS],
)
def test_hostile_reply_returns_the_documented_dict_shape(
    variant: str,
    build: Callable[[], str],
    parsed: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hostile reply degrades to a ``dict``; reachability is counted."""
    counter = _llm_probe.instrument(monkeypatch, _extraction, "strip_fences")
    result = _extraction._parse_response(build())
    _llm_probe.assert_safe_response(result)
    assert counter.calls >= 1, f"{variant}: the parse boundary was never reached"
    if parsed:
        assert result["position"] == "preferred", f"{variant}: the reply was not unwrapped"
        assert result["confidence"] == 0.9
    else:
        assert result == _llm_probe.SAFE_DEFAULT_RESPONSE, f"{variant}: not the safe default"


def test_valid_object_reply_is_actually_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Control: a well-formed reply parses, so the default is not a constant."""
    counter = _llm_probe.instrument(monkeypatch, _extraction, "strip_fences")
    result = _extraction._parse_response(
        '{"position": "preferred", "confidence": 0.9, "citation": "quote", "category_match": true}'
    )
    assert result == {
        "position": "preferred",
        "confidence": 0.9,
        "citation": "quote",
        "category_match": True,
    }
    assert counter.calls >= 1


def test_non_utf8_payload_decodes_to_the_safe_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-UTF-8 transport payload is decoded leniently and degrades safely."""
    counter = _llm_probe.instrument(monkeypatch, _extraction, "strip_fences")
    payload = corpus_llm.non_utf8_bytes()
    assert isinstance(payload, bytes)
    text = payload.decode("utf-8", errors="replace")
    result = _extraction._parse_response(text)
    assert result == _llm_probe.SAFE_DEFAULT_RESPONSE
    assert counter.calls >= 1


@pytest.mark.parametrize(
    ("variant", "build"),
    _NON_OBJECT_VARIANTS,
    ids=[variant for variant, _build in _NON_OBJECT_VARIANTS],
)
@pytest.mark.xfail(
    strict=True,
    reason="RT-014: a valid-JSON non-object reply makes _parse_response raise AttributeError",
)
def test_non_object_reply_still_returns_a_dict(
    variant: str,
    build: Callable[[], str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid-JSON non-object must also degrade to the documented dict."""
    counter = _llm_probe.instrument(monkeypatch, _extraction, "strip_fences")
    result = _extraction._parse_response(build())
    _llm_probe.assert_safe_response(result)
    assert counter.calls >= 1, f"{variant}: the parse boundary was never reached"


@pytest.mark.xfail(
    strict=True,
    reason="RT-017: a fence with no newline is left in place, so the reply never parses",
)
def test_fence_without_newline_is_unwrapped() -> None:
    """The CORRECT behaviour: a newline-less fence is stripped and the body parses."""
    result = _extraction._parse_response(corpus_llm.fenced_without_newline())
    assert result["position"] == "preferred"


# ── The high-level fallback contract of extract_clause ──────────────────────


def test_malformed_reply_yields_the_safe_default_score(monkeypatch: pytest.MonkeyPatch) -> None:
    """The documented fallback: position uncertain with confidence 0.0."""
    gateway = _llm_probe.install_gateway(monkeypatch, _extraction, "this is not JSON at all")
    assessment = _extraction.extract_clause(
        clause_text=_CLAUSE,
        clause_id="clause-1",
        category=_CATEGORY,
        extraction_model="extraction-slot",
    )
    assert gateway.chat_call_count == 1
    assert assessment.position is Position.UNCERTAIN
    assert assessment.confidence == 0.0
    assert assessment.playbook_category == "confidentiality"


@pytest.mark.xfail(
    strict=True,
    reason="RT-016: a malformed reply yields a plausible default with no error signal",
)
def test_malformed_reply_is_signalled_not_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    """A parse failure must be observable, not indistinguishable from 'uncertain'."""
    _llm_probe.install_gateway(monkeypatch, _extraction, "this is not JSON at all")
    assessment = _extraction.extract_clause(
        clause_text=_CLAUSE,
        clause_id="clause-1",
        category=_CATEGORY,
        extraction_model="extraction-slot",
    )
    assert assessment.error is not None, "a parse failure must be observable on the assessment"


# ── Property-based fuzzing ──────────────────────────────────────────────────


@given(text=st.text())
def test_property_strip_fences_is_total(text: str) -> None:
    """``strip_fences`` is a total function: any text maps to a string."""
    assert isinstance(strip_fences(text), str)


@given(
    payload=st.dictionaries(
        keys=st.text(max_size=8),
        values=st.one_of(st.integers(), st.booleans(), st.none(), st.text(max_size=8)),
        max_size=4,
    ).map(lambda data: {**data, "confidence": 0.5})
)
def test_property_any_json_object_maps_to_the_response_shape(payload: dict[str, object]) -> None:
    """Any well-formed JSON object reply maps to the documented four-key dict."""
    result = _extraction._parse_response(json.dumps(payload))
    _llm_probe.assert_safe_response(result)


# ── Negative control: the oracle must be able to fail ───────────────────────


def test_negative_control_oracle_rejects_a_broken_parser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dict-shape oracle cannot certify a parser it cannot fail.

    ``_parse_response`` is replaced by a stub returning a non-dict — exactly the
    outcome ``assert_safe_response`` exists to reject. The ``pytest.raises``
    below is the assertion that the oracle is not vacuous.
    """

    def broken_parser(_raw: str) -> object:
        return None

    monkeypatch.setattr(_extraction, "_parse_response", broken_parser)
    broken_result = _extraction._parse_response("{}")
    with pytest.raises(AssertionError):
        _llm_probe.assert_safe_response(broken_result)
