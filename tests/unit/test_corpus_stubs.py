"""Unit tests for the corpus stub modules (filled later by W2, W3 and W4)."""

from pathlib import Path

import pytest

from tests.helpers import corpus_docs, corpus_llm, corpus_state


def test_stubs_are_wired(tmp_path: Path) -> None:
    # corpus_docs was filled by W2, so it is no longer a stub; corpus_llm and
    # corpus_state are still owned by W3 and W4 and must keep failing loudly.
    with pytest.raises(NotImplementedError, match="owned by W3"):
        corpus_llm.fenced()
    with pytest.raises(NotImplementedError, match="owned by W4"):
        corpus_state.corrupt_yaml(tmp_path)


def test_corpus_docs_is_implemented(tmp_path: Path) -> None:
    # W2 filled corpus_docs: each generator writes a real file and returns it.
    zero = corpus_docs.zero_byte(tmp_path)
    assert zero.exists()
    assert zero.suffix == ".pdf"
    assert zero.stat().st_size == 0
