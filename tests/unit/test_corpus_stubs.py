"""Unit tests for the hostile-input corpus modules."""

from pathlib import Path

from tests.helpers import corpus_docs, corpus_llm, corpus_state


def test_corpus_llm_is_implemented() -> None:
    # W3 filled corpus_llm: each variant returns a hostile model reply.
    assert isinstance(corpus_llm.fenced(), str)
    assert isinstance(corpus_llm.non_utf8_bytes(), bytes)


def test_corpus_state_is_implemented(tmp_path: Path) -> None:
    # W4 filled corpus_state: each generator writes a real file and returns it.
    path = corpus_state.corrupt_yaml(tmp_path)
    assert path.exists()
    assert path.stat().st_size > 0


def test_corpus_docs_is_implemented(tmp_path: Path) -> None:
    # W2 filled corpus_docs: each generator writes a real file and returns it.
    zero = corpus_docs.zero_byte(tmp_path)
    assert zero.exists()
    assert zero.suffix == ".pdf"
    assert zero.stat().st_size == 0
