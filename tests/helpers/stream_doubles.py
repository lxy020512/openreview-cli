"""Shared litellm stream doubles: the one definition used by every stream test.

The terminal signal lives on the **stream** object
(``TerminatedStream.received_finish_reason``), never on a chunk: litellm's
CustomStreamWrapper fabricates a chunk ``finish_reason="stop"`` on EOF, so a
chunk cannot discriminate a truncated stream. A terminated stream carries the
``received_finish_reason`` attribute litellm sets once the provider signalled a
finish; a truncated stream omits it entirely, which is exactly the shape the
terminal-marker predicate (§0.3 D4) discriminates on.
"""

from __future__ import annotations

import types
from collections.abc import Iterator


class StreamChunk:
    """One chunk: ``choices[0].delta.content`` is where ``_iter_stream`` reads text."""

    def __init__(self, content: str | None) -> None:
        self.choices = [types.SimpleNamespace(delta=types.SimpleNamespace(content=content))]


class TerminatedStream:
    """What litellm returns for a stream that ended with a provider finish_reason."""

    def __init__(self, chunks: list[StreamChunk], terminal: str = "stop") -> None:
        self.chunks = chunks
        self.received_finish_reason: str | None = terminal

    def __iter__(self) -> Iterator[StreamChunk]:
        return iter(self.chunks)


class TruncatedStream:
    """A stream whose provider never sent a finish_reason (no terminal attribute)."""

    def __init__(self, chunks: list[StreamChunk]) -> None:
        self.chunks = chunks

    def __iter__(self) -> Iterator[StreamChunk]:
        return iter(self.chunks)
