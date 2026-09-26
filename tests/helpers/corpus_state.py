"""Hostile config, auth, playbook and SQLite state generators.

Owned by W4 (fuzzing, playbook, config and storage). W0 created every function
stubbed to raise ``NotImplementedError`` so a workstream that starts early fails
loudly instead of silently sharing the file; W4 fills the bodies.

Every generator writes its malformed file into *tmp_path* and returns the path.
The inputs are small and deterministic (no randomness, no clocks). The
``billion_laughs_yaml`` bomb is a real YAML alias-expansion shape but bounded to
a few thousand nodes when expanded: the point is to detect the absence of a
guard, not to exhaust the machine.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

# ── config.yml (openreview_cli.config.loader) ──────────────────────────────


def corrupt_yaml(tmp_path: Path) -> Path:
    """A ``config.yml`` that does not parse as YAML."""
    path = tmp_path / "config.yml"
    path.write_text("privacy: {tier: [balanced\n", encoding="utf-8")
    return path


def deeply_nested_yaml(tmp_path: Path) -> Path:
    """A ``config.yml`` nested deeply enough to stress the loader.

    Depth 60 flow-style mappings under one unknown key. This is a recursion and
    stack test, not a malformed-YAML test: the file parses and must not crash
    the loader with a raw ``RecursionError``.
    """
    depth = 60
    nested = ("{a: " * depth) + "1" + ("}" * depth)
    path = tmp_path / "config.yml"
    path.write_text(f"privacy:\n  tier: balanced\nx: {nested}\n", encoding="utf-8")
    return path


def billion_laughs_yaml(tmp_path: Path) -> Path:
    """A ``config.yml`` built from a self-referential YAML anchor.

    A 10-deep fan-out that would expand to ~10,000 nodes if an alias were
    copied per reference. Kept bounded so a loader without an alias-expansion
    guard is detected without exhausting memory.
    """
    path = tmp_path / "config.yml"
    path.write_text(
        "version: 1\n"
        'l0: &l0 ["x","x","x","x","x","x","x","x","x","x"]\n'
        "l1: &l1 [*l0,*l0,*l0,*l0,*l0,*l0,*l0,*l0,*l0,*l0]\n"
        "l2: &l2 [*l1,*l1,*l1,*l1,*l1,*l1,*l1,*l1,*l1,*l1]\n"
        "l3: [*l2,*l2,*l2,*l2,*l2,*l2,*l2,*l2,*l2,*l2]\n",
        encoding="utf-8",
    )
    return path


def wrong_typed_yaml(tmp_path: Path) -> Path:
    """A ``config.yml`` with values of the wrong type (raw pydantic error).

    ``tier`` must be one of maximum/balanced/performance and ``pii_threshold``
    must be <= 1.0; both are violated.
    """
    path = tmp_path / "config.yml"
    path.write_text(
        "privacy:\n  tier: 42\n  pii_threshold: 5.0\n",
        encoding="utf-8",
    )
    return path


def list_not_map_yaml(tmp_path: Path) -> Path:
    """A ``config.yml`` whose top level is a list, not a mapping."""
    path = tmp_path / "config.yml"
    path.write_text("- one\n- two\n", encoding="utf-8")
    return path


# ── auth.json (openreview_cli.config.auth) ─────────────────────────────────


def auth_json_invalid(tmp_path: Path) -> Path:
    """An ``auth.json`` that is not valid JSON."""
    path = tmp_path / "auth.json"
    path.write_text('{"openai": "sk-test", ', encoding="utf-8")
    return path


def auth_json_wrong_mode(tmp_path: Path) -> Path:
    """An ``auth.json`` written with the wrong file mode (0o644)."""
    path = tmp_path / "auth.json"
    path.write_text('{"openai": "sk-test"}', encoding="utf-8")
    path.chmod(0o644)
    return path


# ── SQLite (openreview_cli.storage.database / retrieval) ───────────────────


def corrupt_sqlite(tmp_path: Path) -> Path:
    """A SQLite file whose header/body is corrupt.

    A real SQLite magic header followed by bytes that are not a valid page, so
    the header check passes and the first page read fails.
    """
    path = tmp_path / "corrupt.db"
    path.write_bytes(b"SQLite format 3\x00" + b"\xde\xad\xbe\xef" * 512)
    return path


def zero_byte_sqlite(tmp_path: Path) -> Path:
    """A 0-byte SQLite file (SQLite treats it as an empty database)."""
    path = tmp_path / "zero.db"
    path.write_bytes(b"")
    return path


def missing_table_sqlite(tmp_path: Path) -> Path:
    """A valid SQLite database missing every expected table."""
    path = tmp_path / "missing_table.db"
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE unrelated (x INTEGER)")
        conn.commit()
    finally:
        conn.close()
    return path


def interrupted_migration_sqlite(tmp_path: Path) -> Path:
    """A SQLite database left mid-migration (stale ``user_version``/schema).

    A fully migrated database whose ``user_version`` is rewound to 10, while the
    migration-011 column (``review_reports.client_id``) is already present. A
    re-run of migration 011 therefore hits ``duplicate column name`` — the
    idempotent-re-run case ``_exec_migration_safely`` documents it tolerates.
    """
    from openreview_cli.storage.database import get_connection, init_database

    path = tmp_path / "interrupted.db"
    init_database(path)
    conn = get_connection(path)
    try:
        conn.execute("PRAGMA user_version = 10")
        conn.commit()
    finally:
        conn.close()
    return path


# ── playbook YAML (openreview_cli.review.playbook, prompts.store) ──────────


def corrupt_playbook_yaml(tmp_path: Path) -> Path:
    """A playbook YAML that does not parse."""
    path = tmp_path / "playbook.yml"
    path.write_text("id: {unclosed\n", encoding="utf-8")
    return path


def wrong_typed_playbook_yaml(tmp_path: Path) -> Path:
    """A playbook YAML with wrongly typed values.

    ``categories`` is a list of scalars rather than a list of mappings, so the
    per-category parser is handed an ``int``.
    """
    path = tmp_path / "playbook.yml"
    path.write_text(
        "id: x\n"
        "mode: precheck\n"
        "metadata: {version: 1, description: d, author: a}\n"
        "categories:\n"
        "  - 1\n"
        "  - 2\n",
        encoding="utf-8",
    )
    return path
