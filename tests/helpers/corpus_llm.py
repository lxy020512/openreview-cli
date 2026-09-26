"""Hostile LLM-response corpus generators.

Owned by W3 (fuzzing, LLM response and JSON ingestion). W0 created every
function stubbed to raise ``NotImplementedError`` so a workstream that starts
early fails loudly instead of silently sharing the file; W3 fills the bodies.

Every generator returns a deliberately hostile "model reply" for the review
JSON ingestion boundary: a payload a provider could plausibly emit that the
fence strippers and JSON parsers must survive. Only ``non_utf8_bytes`` returns
``bytes`` (a transport-level payload); every other generator returns ``str``,
which is what the gateway boundary hands to the parsers.
"""

from __future__ import annotations

import json

# Depth of the nested-object reply. Deep enough to exercise the JSON parser's
# recursion path, shallow enough to stay instant and memory-safe.
_NESTED_DEPTH = 200

# Item count of the enormous array: a shape test, not a stress test.
_ENORMOUS_ITEMS = 3000


def fenced() -> str:
    """A JSON object wrapped in a ```json fence."""
    body = json.dumps(
        {
            "position": "preferred",
            "confidence": 0.9,
            "citation": "The parties shall keep information confidential.",
            "category_match": True,
        }
    )
    return f"```json\n{body}\n```"


def fenced_without_newline() -> str:
    """A fenced block whose opening fence line has no trailing newline.

    ``strip_fences`` only trims the opening fence when it contains a newline
    (``llm_json.py:28-33``), so this reply keeps its leading ``\\`\\`\\`json`` and
    every caller silently falls back (plan sharp edge 12, register RT-017).
    """
    return (
        '```json{"position": "preferred", "confidence": 0.9, '
        '"citation": "The parties shall keep information confidential.", '
        '"category_match": true}```'
    )


def truncated() -> str:
    """Valid JSON cut off mid-token."""
    return '{"position": "preferred", "confidence": 0.9, "citation": "The parties shal'


def deeply_nested() -> str:
    """JSON nested deeply enough to stress the parser."""
    return '{"a":' * _NESTED_DEPTH + "1" + "}" * _NESTED_DEPTH


def non_utf8_bytes() -> bytes:
    """A response payload that is not valid UTF-8."""
    return b'\xff\xfe{"position": "\x80\x81preferred", "confidence": 0.9}'


def control_chars() -> str:
    """A JSON string carrying raw control characters."""
    return '{"position": "preferred", "citation": "line1\u0000line2\u001b[31mred"}'


def enormous_array() -> str:
    """A JSON array with an enormous number of elements."""
    return json.dumps(list(range(_ENORMOUS_ITEMS)))


def json_string_not_object() -> str:
    """A JSON string where an object is expected."""
    return '"position: preferred"'


def json_null() -> str:
    """The literal JSON ``null``."""
    return "null"


def json_empty() -> str:
    """An empty response string."""
    return ""


def embedded_backticks() -> str:
    """A response containing embedded backtick runs."""
    return (
        "```json\n"
        '{"position": "preferred", "confidence": 0.9, '
        '"citation": "wrap the value in ```code``` fences", "category_match": false}\n'
        "```"
    )
