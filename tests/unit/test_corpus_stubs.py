"""Unit tests for the corpus stub modules (filled later by W2, W3 and W4)."""

from pathlib import Path

import pytest

from tests.helpers import corpus_docs, corpus_llm, corpus_state


def test_stubs_are_wired(tmp_path: Path) -> None:
    with pytest.raises(NotImplementedError, match="owned by W2"):
        corpus_docs.zero_byte(tmp_path)
    with pytest.raises(NotImplementedError, match="owned by W3"):
        corpus_llm.fenced()
    with pytest.raises(NotImplementedError, match="owned by W4"):
        corpus_state.corrupt_yaml(tmp_path)
