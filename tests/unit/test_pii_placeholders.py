"""Unit tests for deterministic placeholder assignment."""

from openreview_cli.pii.models import PiiEntity
from openreview_cli.pii.placeholders import assign_placeholders


def _make_entity(entity_type: str, value: str, source: str = "nlp") -> PiiEntity:
    return PiiEntity(
        entity_type=entity_type,
        original_value=value,
        start=0,
        end=len(value),
        score=0.95,
        placeholder="[TEMP_0]",
        source=source,
    )


def test_empty_entities() -> None:
    mapping, entities = assign_placeholders([])
    assert mapping == {}
    assert entities == []


def test_single_party_assigns_letter() -> None:
    entity = _make_entity("ORGANIZATION", "ABC Corp.")
    mapping, entities = assign_placeholders([entity])
    assert mapping["PARTY_A"] == "ABC Corp."
    assert entities[0].placeholder == "[PARTY_A]"


def test_two_parties_assigns_sequential_letters() -> None:
    a = _make_entity("ORGANIZATION", "ABC Corp.")
    b = _make_entity("ORGANIZATION", "XYZ Inc.")
    mapping, entities = assign_placeholders([a, b])
    assert mapping["PARTY_A"] == "ABC Corp."
    assert mapping["PARTY_B"] == "XYZ Inc."


def test_party_sorting_is_alphabetical() -> None:
    a = _make_entity("ORGANIZATION", "XYZ Inc.")
    b = _make_entity("ORGANIZATION", "ABC Corp.")
    mapping, _ = assign_placeholders([a, b])
    assert mapping["PARTY_A"] == "ABC Corp."
    assert mapping["PARTY_B"] == "XYZ Inc."


def test_non_party_uses_numeric() -> None:
    a = _make_entity("PERSON", "Alice")
    b = _make_entity("PERSON", "Bob")
    mapping, _ = assign_placeholders([a, b])
    assert mapping["NAME_1"] == "Alice"
    assert mapping["NAME_2"] == "Bob"


def test_repeated_value_gets_same_placeholder() -> None:
    a = _make_entity("EMAIL_ADDRESS", "a@b.com")
    b = _make_entity("EMAIL_ADDRESS", "a@b.com")
    mapping, entities = assign_placeholders([a, b])
    assert mapping["EMAIL_1"] == "a@b.com"
    assert entities[0].placeholder == "[EMAIL_1]"
    assert entities[1].placeholder == "[EMAIL_1]"


def test_different_types_separate_numbering() -> None:
    name = _make_entity("PERSON", "Alice")
    email = _make_entity("EMAIL_ADDRESS", "a@b.com")
    mapping, _ = assign_placeholders([name, email])
    assert mapping["NAME_1"] == "Alice"
    assert mapping["EMAIL_1"] == "a@b.com"


def test_metadata_entities_included() -> None:
    meta = _make_entity("FILENAME", "report.pdf")
    mapping, _ = assign_placeholders([], metadata_entities=[meta])
    assert mapping["FILENAME_1"] == "report.pdf"


def test_case_insensitive_sorting() -> None:
    a = _make_entity("PERSON", "bob")
    b = _make_entity("PERSON", "Alice")
    mapping, _ = assign_placeholders([a, b])
    assert mapping["NAME_1"] == "Alice"
    assert mapping["NAME_2"] == "bob"


def test_overlapping_types_emit_one_placeholder_for_the_value() -> None:
    """Two recognizers on the same characters must produce one placeholder (#115)."""
    tax = _make_entity("TAX_ID", "11-7654320", source="regex")
    date = _make_entity("DATE_TIME", "11-7654320")
    mapping, entities = assign_placeholders([tax, date])
    assert mapping == {"TAX_ID_1": "11-7654320"}
    assert entities[0].placeholder == "[TAX_ID_1]"
    assert entities[1].placeholder == "[TAX_ID_1]"


def test_losing_type_reuses_the_winning_placeholder() -> None:
    """The losing entity stays in the list and points at the winner's placeholder."""
    reg = _make_entity("REG_NUMBER", "REG-100001", source="regex")
    party = _make_entity("ORGANIZATION", "REG-100001")
    mapping, entities = assign_placeholders([reg, party])
    assert mapping == {"REG_1": "REG-100001"}
    assert len(entities) == 2
    assert all(e.placeholder == "[REG_1]" for e in entities)


def test_more_specific_type_wins_over_party() -> None:
    party = _make_entity("ORGANIZATION", "ACME")
    date = _make_entity("DATE_TIME", "ACME")
    mapping, entities = assign_placeholders([date, party])
    assert mapping == {"PARTY_A": "ACME"}
    assert all(e.placeholder == "[PARTY_A]" for e in entities)


def test_value_claimed_by_a_more_specific_type_loses_its_own_number() -> None:
    tax = _make_entity("TAX_ID", "11-7654320")
    date = _make_entity("DATE_TIME", "11-7654320")
    other_date = _make_entity("DATE_TIME", "1971-05-02")
    mapping, _ = assign_placeholders([tax, date, other_date])
    assert mapping["TAX_ID_1"] == "11-7654320"
    assert "DATE_1" not in mapping  # the tax id is no longer a DATE
    assert "1971-05-02" in mapping.values()
    assert sum(1 for value in mapping.values() if value == "11-7654320") == 1
