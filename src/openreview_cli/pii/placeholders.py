"""Deterministic placeholder assignment for PII entities."""

import string
from collections import defaultdict
from typing import Any

PRESIDIO_TO_PREFIX = {
    "ORGANIZATION": "PARTY",
    "PERSON": "NAME",
    "EMAIL_ADDRESS": "EMAIL",
    "PHONE_NUMBER": "PHONE",
    "LOCATION": "ADDRESS",
    "DATE_TIME": "DATE",
    "AMOUNT": "AMOUNT",
    "TAX_ID": "TAX_ID",
    "IBAN_CODE": "ACCT",
    "ACCT": "ACCT",
    "ID_DOCUMENT": "ID",
    "REG_NUMBER": "REG",
    "CREDIT_CARD": "CC",
    "IP_ADDRESS": "IP",
}

PARTY_PREFIXES = {"PARTY"}

#: Which entity-type prefix owns a value when two recognizers claim the same characters.
#  Lower wins; unknown prefixes sort last; ties break deterministically by prefix name.
_PREFIX_PRIORITY: dict[str, int] = {
    "TAX_ID": 0,
    "REG": 1,
    "ACCT": 2,
    "ID": 3,
    "AMOUNT": 4,
    "PHONE": 5,
    "EMAIL": 6,
    "CC": 7,
    "IP": 8,
    "PARTY": 9,
    "NAME": 10,
    "ADDRESS": 11,
    "DATE": 12,
}
_UNKNOWN_PREFIX_RANK = 100


def assign_placeholders(  # noqa: PLR0912
    entities: list[Any], metadata_entities: list[Any] | None = None
) -> tuple[dict[str, str], list[Any]]:
    """Assign deterministic placeholders to entities.

    Args:
        entities: list of PiiEntity-like objects (dict or obj with entity_type, original_value attrs)
        metadata_entities: optional list of metadata PiiEntity objects

    Returns:
        mapping: dict[str, str] of {placeholder_key: original_value}
        entities: list with placeholder field set
    """
    all_entities = list(entities) + list(metadata_entities or [])
    if not all_entities:
        return {}, []

    # Group by prefix
    groups = defaultdict(list)
    for entity in all_entities:
        prefix = _get_prefix(entity)
        groups[prefix].append(entity)

    mapping = {}
    for prefix, group in sorted(groups.items()):
        # Unique values sorted alphabetically
        unique = sorted({e.original_value for e in group}, key=str.lower)

        if prefix in PARTY_PREFIXES:
            if len(unique) <= 26:
                labels = string.ascii_uppercase[: len(unique)]
                for val, lbl in zip(unique, labels, strict=True):
                    placeholder = f"[{prefix}_{lbl}]"
                    mapping[placeholder.replace("[", "").replace("]", "")] = val
                    for entity in group:
                        if entity.original_value == val:
                            entity.placeholder = placeholder
            else:
                for i, val in enumerate(unique, 1):
                    placeholder = f"[{prefix}_{i}]"
                    mapping[placeholder.replace("[", "").replace("]", "")] = val
                    for entity in group:
                        if entity.original_value == val:
                            entity.placeholder = placeholder
        else:
            for i, val in enumerate(unique, 1):
                placeholder = f"[{prefix}_{i}]"
                mapping[placeholder.replace("[", "").replace("]", "")] = val
                for entity in group:
                    if entity.original_value == val:
                        entity.placeholder = placeholder

    # One value, one placeholder: when several recognizers claim the same characters with
    # different types, the most specific type keeps the value and the others reuse its
    # placeholder. strip_pii replaces every occurrence of a value globally, so one
    # placeholder still redacts them all, and no entity leaves the list.
    winner = _value_winners(groups)
    mapping = {
        key: value for key, value in mapping.items() if winner[value] == key.rsplit("_", 1)[0]
    }
    placeholders = {value: f"[{key}]" for key, value in mapping.items()}
    for entity in all_entities:
        entity.placeholder = placeholders[entity.original_value]

    return mapping, all_entities


def _value_winners(groups: dict[str, list[Any]]) -> dict[str, str]:
    """Return the prefix that owns each value when more than one claims it.

    Candidates are ordered by the evidence the entity already carries: a
    regex/pattern match outranks an open-vocabulary NER inference on the same
    value regardless of the table, so an unenumerated structured type cannot
    lose the span. Among same-source entities the ``_PREFIX_PRIORITY`` table
    still picks the more specific type, with the score as the final tie-break.
    """
    best: dict[str, tuple[tuple[Any, ...], str]] = {}
    for prefix, group in groups.items():
        for entity in group:
            value = entity.original_value
            rank = (
                entity.source != "regex",
                _prefix_rank(_get_prefix(entity)),
                -float(entity.score or 0.0),
                _get_prefix(entity),
            )
            current = best.get(value)
            if current is None or rank < current[0]:
                best[value] = (rank, prefix)
    return {value: prefix for value, (_, prefix) in best.items()}


def _prefix_rank(prefix: str) -> tuple[int, str]:
    return (_PREFIX_PRIORITY.get(prefix, _UNKNOWN_PREFIX_RANK), prefix)


def _get_prefix(entity: Any) -> str:
    """Map Presidio entity type to placeholder prefix."""
    return PRESIDIO_TO_PREFIX.get(entity.entity_type, entity.entity_type)  # type: ignore[no-any-return]


__all__ = ["PRESIDIO_TO_PREFIX", "assign_placeholders"]
