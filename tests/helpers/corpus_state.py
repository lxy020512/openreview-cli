"""Hostile config, auth, playbook and SQLite state generators.

Owned by W4 (fuzzing, playbook, config and storage). W0 created every function
stubbed to raise ``NotImplementedError`` so a workstream that starts early fails
loudly instead of silently sharing the file; W4 fills the bodies.
"""

from __future__ import annotations

from pathlib import Path


def corrupt_yaml(tmp_path: Path) -> Path:
    """A ``config.yml`` that does not parse as YAML."""
    raise NotImplementedError("owned by W4")


def deeply_nested_yaml(tmp_path: Path) -> Path:
    """A ``config.yml`` nested deeply enough to stress the loader."""
    raise NotImplementedError("owned by W4")


def billion_laughs_yaml(tmp_path: Path) -> Path:
    """A ``config.yml`` built from a self-referential YAML anchor."""
    raise NotImplementedError("owned by W4")


def wrong_typed_yaml(tmp_path: Path) -> Path:
    """A ``config.yml`` with values of the wrong type (raw pydantic error)."""
    raise NotImplementedError("owned by W4")


def list_not_map_yaml(tmp_path: Path) -> Path:
    """A ``config.yml`` whose top level is a list, not a mapping."""
    raise NotImplementedError("owned by W4")


def auth_json_invalid(tmp_path: Path) -> Path:
    """An ``auth.json`` that is not valid JSON."""
    raise NotImplementedError("owned by W4")


def auth_json_wrong_mode(tmp_path: Path) -> Path:
    """An ``auth.json`` written with the wrong file mode."""
    raise NotImplementedError("owned by W4")


def corrupt_sqlite(tmp_path: Path) -> Path:
    """A SQLite file whose header/body is corrupt."""
    raise NotImplementedError("owned by W4")


def zero_byte_sqlite(tmp_path: Path) -> Path:
    """A 0-byte SQLite file."""
    raise NotImplementedError("owned by W4")


def missing_table_sqlite(tmp_path: Path) -> Path:
    """A valid SQLite database missing an expected table."""
    raise NotImplementedError("owned by W4")


def interrupted_migration_sqlite(tmp_path: Path) -> Path:
    """A SQLite database left mid-migration (stale ``user_version``/schema)."""
    raise NotImplementedError("owned by W4")


def corrupt_playbook_yaml(tmp_path: Path) -> Path:
    """A playbook YAML that does not parse."""
    raise NotImplementedError("owned by W4")


def wrong_typed_playbook_yaml(tmp_path: Path) -> Path:
    """A playbook YAML with wrongly typed values."""
    raise NotImplementedError("owned by W4")
