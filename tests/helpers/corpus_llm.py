"""Hostile LLM-response corpus generators.

Owned by W3 (fuzzing, LLM response and JSON ingestion). W0 created every
function stubbed to raise ``NotImplementedError`` so a workstream that starts
early fails loudly instead of silently sharing the file; W3 fills the bodies.
"""

from __future__ import annotations


def fenced() -> str:
    """A JSON object wrapped in a ```json fence."""
    raise NotImplementedError("owned by W3")


def fenced_without_newline() -> str:
    """A fenced block whose opening fence line has no trailing newline."""
    raise NotImplementedError("owned by W3")


def truncated() -> str:
    """Valid JSON cut off mid-token."""
    raise NotImplementedError("owned by W3")


def deeply_nested() -> str:
    """JSON nested deeply enough to stress the parser."""
    raise NotImplementedError("owned by W3")


def non_utf8_bytes() -> bytes:
    """A response payload that is not valid UTF-8."""
    raise NotImplementedError("owned by W3")


def control_chars() -> str:
    """A JSON string carrying raw control characters."""
    raise NotImplementedError("owned by W3")


def enormous_array() -> str:
    """A JSON array with an enormous number of elements."""
    raise NotImplementedError("owned by W3")


def json_string_not_object() -> str:
    """A JSON string where an object is expected."""
    raise NotImplementedError("owned by W3")


def json_null() -> str:
    """The literal JSON ``null``."""
    raise NotImplementedError("owned by W3")


def json_empty() -> str:
    """An empty response string."""
    raise NotImplementedError("owned by W3")


def embedded_backticks() -> str:
    """A response containing embedded backtick runs."""
    raise NotImplementedError("owned by W3")
