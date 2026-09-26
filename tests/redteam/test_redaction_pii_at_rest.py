"""W6 red-team suite: synthetic PII must never sit in plaintext at rest.

Guarantee 5 (plan section 5): the PII governance database (``pii_cache``,
``pii_audit_trail``) records *that* a strip happened and *what types* were
found — never the stripped values themselves. The register's surface
enumeration names each table's text columns as output surfaces in their own
right, so this suite opens the database with ``sqlite3`` directly and reads the
rows; CLI output is not trusted as the observation.

Method. A real strip runs through the session ``pii_engine`` (never a fresh
``PiiEngine``); the result is persisted with the product's own
:func:`persist_pii_result`, then every column of both tables is read back and
checked against the synthetic values that were fed in. Two controls make the
absence assertion load bearing:

* the synthetic values are asserted to have been *detected* (they appear in the
  result's mapping, so the strip did not simply find nothing), and
* the encrypted mapping is asserted to hold the values that the database must
  not — readable with the key, unreadable without it.

Reachability is asserted before content (the ``pii_cache`` and
``pii_audit_trail`` rows really exist), so a green run cannot come from an
untouched path.

Deliberately NOT covered here (reuse, not duplication): detection accuracy and
the engine (``tests/unit/test_pii3_*``), the mapping file format
(``tests/unit/test_pii_mapping*``), the read-path ``auth.json`` permissions
(``tests/unit/test_auth.py:24,32,167``) and the corrupt-auth read path
(``tests/unit/test_auth.py:177``).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from openreview_cli.parsing.models import Clause
from openreview_cli.pii.encryption import InvalidToken
from openreview_cli.pii.engine import PiiEngine, strip_pii_clauses
from openreview_cli.pii.mapping import read_pii_mapping
from openreview_cli.pii.models import PiiResult
from openreview_cli.pii.persist import persist_pii_result
from openreview_cli.pii.retention import cleanup_expired, delete_pii_data
from openreview_cli.storage import init_database

pytestmark = pytest.mark.redteam

# ── Synthetic PII. Never a real value (plan section 9.7). ───────────────────
#
# Both shapes match the product's own regex recognizers at score 1.0
# (``pii/recognizers.py:26`` ssn, ``:18`` dollar_amount), so detection is
# deterministic: the W6 point is what reaches the disk, not what the NER model
# happens to judge.
PII_TAX_ID = "123-45-6789"
PII_AMOUNT = "$1,234.56"
PII_VALUES = (PII_TAX_ID, PII_AMOUNT)

# A 32-character key (the config validator accepts 16/24/32 bytes, loader.py:182-184).
ENCRYPTION_KEY = "W6-REST-KEY-0123456789abcdef"

DOCUMENT_HASH = "6a" * 32  # 64 hex characters, as a real sha256 digest is
FILENAME = "w6-contract.docx"


class _Document:
    """Minimal document double: only the attributes ``_redact_metadata`` reads."""

    def __init__(self, source_path: Path) -> None:
        self.source_path = source_path
        self.page_count = 1
        self.author: str | None = None
        self.title: str | None = None
        self.company: str | None = None


@dataclass
class _AtRest:
    """One persisted strip, plus the paths a reader would open."""

    db_path: Path
    review_dir: Path
    document_hash: str
    result: PiiResult


def _clauses() -> list[Clause]:
    text = f"The fee is {PII_AMOUNT}; the filing tax id is {PII_TAX_ID}."
    return [
        Clause(
            id="1",
            title="Payment terms",
            text=text,
            level=1,
            parent_id=None,
            source_page=None,
            source_paragraph=0,
            source_span=(0, len(text)),
        )
    ]


def _table_text(db_path: Path, table: str) -> dict[str, list[str]]:
    """Read every column of *table* as strings, keyed by column name.

    Reading by column (rather than by declared affinity) means a value stored
    in a ``TIMESTAMP``/``INTEGER`` column is inspected too, so no plaintext PII
    can hide behind an unexpected column type.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        columns = [str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")]
        out: dict[str, list[str]] = {column: [] for column in columns}
        for row in conn.execute(f"SELECT * FROM {table}"):
            for column in columns:
                value = row[column]
                if value is not None:
                    out[column].append(str(value))
        return out
    finally:
        conn.close()


def _cache_hash(db_path: Path, document_hash: str) -> bool:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT 1 FROM pii_cache WHERE document_hash = ?", (document_hash,)
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def _audit_count(db_path: Path, document_hash: str) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM pii_audit_trail WHERE document_hash = ?",
            (document_hash,),
        ).fetchone()
        return int(row[0])
    finally:
        conn.close()


def _expire(db_path: Path, document_hash: str) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        conn.execute(
            "UPDATE pii_cache SET expiry_at = ? WHERE document_hash = ?",
            (past, document_hash),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def at_rest(tmp_path: Path, pii_engine: PiiEngine) -> _AtRest:
    """Strip synthetic PII through the real engine, then persist it for real."""
    db_path = tmp_path / "openreview.db"
    init_database(db_path)
    review_dir = tmp_path / "reviews" / DOCUMENT_HASH[:12]
    source_path = tmp_path / FILENAME

    _clauses_result, result = strip_pii_clauses(
        _clauses(),
        _Document(source_path),
        engine=pii_engine,
    )
    persist_pii_result(
        db_path,
        document_hash=DOCUMENT_HASH,
        config_hash="w6cfg",
        pii_result=result,
        review_dir=review_dir,
        encryption_key=ENCRYPTION_KEY,
        filename=FILENAME,
    )
    return _AtRest(
        db_path=db_path,
        review_dir=review_dir,
        document_hash=DOCUMENT_HASH,
        result=result,
    )


# ── Guarantee 5: no plaintext PII in the governance rows ────────────────────


def test_pii_rows_carry_no_plaintext_pii(at_rest: _AtRest) -> None:
    """Reachability, then a per-column scan for the synthetic values."""
    cache = _table_text(at_rest.db_path, "pii_cache")
    audit = _table_text(at_rest.db_path, "pii_audit_trail")

    # Reachability: both rows really exist, so the assertions below have content.
    assert at_rest.document_hash in cache["document_hash"], (
        "reachability: no pii_cache row was written"
    )
    assert at_rest.document_hash in audit["document_hash"], (
        "reachability: no pii_audit_trail row was written"
    )
    assert int(audit["entity_count"][0]) >= 1, "reachability: the strip detected nothing"

    # Positive control: the synthetic PII WAS detected, so its absence from the
    # rows is meaningful rather than a strip that found nothing.
    assert PII_TAX_ID in at_rest.result.mapping.values(), "the TAX_ID canary was never detected"
    assert PII_AMOUNT in at_rest.result.mapping.values(), "the AMOUNT canary was never detected"

    # The audit row records the entity *types* mapped to counts, never values:
    # every value is an int, so a stored string value would be caught here too.
    distribution = json.loads(audit["entity_type_distribution"][0])
    assert isinstance(distribution, dict) and distribution
    assert all(isinstance(count, int) for count in distribution.values()), (
        f"entity_type_distribution carries values, not type counts: {distribution!r}"
    )

    for table_name, columns in (("pii_cache", cache), ("pii_audit_trail", audit)):
        for column, values in columns.items():
            for value in values:
                for pii in PII_VALUES:
                    assert pii not in value, (
                        f"plaintext PII {pii!r} at rest in {table_name}.{column}: {value!r}"
                    )


def test_encrypted_mapping_is_not_decryptable_without_the_key(at_rest: _AtRest) -> None:
    """The values the database must not carry live in the file, key-locked."""
    mapping_path = at_rest.review_dir / "pii_map.enc"
    assert mapping_path.exists(), "reachability: no encrypted mapping was written"

    blob = mapping_path.read_bytes()
    for pii in PII_VALUES:
        assert pii.encode() not in blob, f"{pii!r} is stored in plaintext inside pii_map.enc"

    # With the key the values are recoverable — proving they were persisted at all.
    recoverable = read_pii_mapping(at_rest.review_dir, ENCRYPTION_KEY)
    assert PII_TAX_ID in recoverable.values()

    # Without the key the mapping is opaque: Fernet authentication fails.
    with pytest.raises(InvalidToken):
        read_pii_mapping(at_rest.review_dir, "W6-WRONG-KEY-0000000000000000000000")


def test_retention_expiry_deletes_rows_and_files(at_rest: _AtRest) -> None:
    """``cleanup_expired`` actually deletes an expired row (retention.py:36-91)."""
    _expire(at_rest.db_path, at_rest.document_hash)
    assert _cache_hash(at_rest.db_path, at_rest.document_hash), "reachability: no row to expire"
    assert _audit_count(at_rest.db_path, at_rest.document_hash) == 1

    mapping_path = at_rest.review_dir / "pii_map.enc"
    result_path = at_rest.review_dir / "stripped.txt"
    assert mapping_path.exists() and result_path.exists()

    deleted = cleanup_expired(at_rest.db_path)

    assert deleted >= 1, "cleanup_expired removed nothing"
    assert not _cache_hash(at_rest.db_path, at_rest.document_hash), "expired cache row survived"
    assert _audit_count(at_rest.db_path, at_rest.document_hash) == 0, "audit row survived"
    assert not mapping_path.exists() and not result_path.exists(), "expired files survived"


def test_pii_rows_are_deletable(at_rest: _AtRest) -> None:
    """``delete_pii_data`` removes the cache row, the audit rows and the files."""
    assert _cache_hash(at_rest.db_path, at_rest.document_hash), "reachability: no row to delete"
    assert _audit_count(at_rest.db_path, at_rest.document_hash) == 1

    outcome = delete_pii_data(at_rest.db_path, at_rest.document_hash[:8])

    assert outcome["cache_removed"] is True
    assert outcome["mapping_removed"] is True
    assert outcome["audit_records"] == 1
    assert not _cache_hash(at_rest.db_path, at_rest.document_hash)
    assert _audit_count(at_rest.db_path, at_rest.document_hash) == 0
    assert not (at_rest.review_dir / "pii_map.enc").exists()


# ── Load-bearing negative control: the scanner can see what the product writes


def test_negative_control_scanner_sees_a_plaintext_filename(tmp_path: Path) -> None:
    """The real writer with a PII-shaped basename, then require the scan to flag it.

    Two purposes in one case. It is the negative control for every absence
    assertion above (the reader really can see a plaintext value the product
    wrote, so a green scan is not a reader looking in the wrong place), and it
    characterises an at-rest boundary: the product records the source document's
    *basename* verbatim on the cache row (``persist.py:113-115``, "a full path
    can carry a client name"). When the basename is itself PII, that PII sits in
    ``pii_cache.filename`` in the clear. No engine is loaded: the result is
    built directly, exactly as ``tests/unit/test_pii_persist.py`` does.
    """
    db_path = tmp_path / "filename.db"
    init_database(db_path)
    result = PiiResult(
        stripped_text="The fee is [AMOUNT_1].",
        mapping={"AMOUNT_1": PII_AMOUNT},
        entities=[],
        page_count=1,
        duration_seconds=0.0,
        warnings=[],
        failed_pages=None,
    )
    persist_pii_result(
        db_path,
        document_hash="7b" * 32,
        config_hash="cfg",
        pii_result=result,
        review_dir=tmp_path / "reviews" / ("7b" * 12),
        encryption_key=ENCRYPTION_KEY,
        filename=f"{PII_TAX_ID}.pdf",
    )

    columns = _table_text(db_path, "pii_cache")
    assert columns["filename"], "reachability: no cache row was written"
    assert any(PII_TAX_ID in value for value in columns["filename"]), (
        "the at-rest scanner cannot see a plaintext value the product wrote"
    )
