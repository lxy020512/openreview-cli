"""Egress summary builder for the pre-flight modal (Phase 4).

``openreview_cli.tui.domain.egress`` describes what a review will send to
external providers.  It must stay litellm-free so the TUI import graph never
pulls the ~160 MB gateway router.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from openreview_cli.gateway.models import ProviderInfo

# Deterministic registry: classification must never read the developer's
# ~/.config/openreview/models.json. Only the providers these tests declare.
_REGISTRY = {
    "ollama": ProviderInfo(name="ollama", is_local=True, base_url="http://localhost:11434/v1"),
    "openai": ProviderInfo(name="openai", base_url="https://api.openai.com/v1"),
    "anthropic": ProviderInfo(name="anthropic", base_url="https://api.anthropic.com"),
}


@pytest.fixture(autouse=True)
def _deterministic_registry(monkeypatch) -> None:
    from openreview_cli.tui.domain import egress

    monkeypatch.setattr(egress, "load_registry", lambda: dict(_REGISTRY))


# ── Summary construction ──────────────────────────────────────────────


def test_egress_summary_flags_local_only(monkeypatch) -> None:
    from openreview_cli.tui.domain import egress

    monkeypatch.setattr(egress, "read_privacy_tier", lambda: "maximum")
    monkeypatch.setattr(
        egress,
        "get_slot_configs",
        lambda: {"extraction": {"provider": "ollama", "model": "qwen3:8b", "configured": True}},
    )
    summary = egress.build_egress_summary(disable_pii=False, extraction_model="extraction")
    assert summary.privacy_tier == "maximum"
    assert summary.pii_stripped is True
    assert summary.cloud_warning is False
    assert any("ollama" in d for d in summary.destinations)


def test_egress_summary_warns_when_pii_off_with_cloud(monkeypatch) -> None:
    from openreview_cli.tui.domain import egress

    monkeypatch.setattr(egress, "read_privacy_tier", lambda: "performance")
    monkeypatch.setattr(
        egress,
        "get_slot_configs",
        lambda: {"extraction": {"provider": "openai", "model": "gpt-4o-mini", "configured": True}},
    )
    summary = egress.build_egress_summary(disable_pii=True, extraction_model="extraction")
    assert summary.pii_stripped is False
    assert summary.cloud_warning is True
    assert any("openai" in d for d in summary.destinations)
    # The warning appears in the rendered lines.
    assert any("uploaded" in line.lower() or "no pii" in line.lower() for line in summary.lines())


def test_egress_summary_no_cloud_warning_when_pii_stripped(monkeypatch) -> None:
    """A cloud destination with PII stripping ON is not a warning."""
    from openreview_cli.tui.domain import egress

    monkeypatch.setattr(egress, "read_privacy_tier", lambda: "balanced")
    monkeypatch.setattr(
        egress,
        "get_slot_configs",
        lambda: {"extraction": {"provider": "openai", "model": "gpt-4o-mini", "configured": True}},
    )
    summary = egress.build_egress_summary(disable_pii=False, extraction_model="extraction")
    assert summary.pii_stripped is True
    assert summary.cloud_warning is False
    assert not any("uploaded" in line.lower() for line in summary.lines())


def test_egress_summary_lists_distinct_slots_in_order(monkeypatch) -> None:
    """Extraction then QA, de-duplicated, in a deterministic order."""
    from openreview_cli.tui.domain import egress

    monkeypatch.setattr(egress, "read_privacy_tier", lambda: "performance")
    monkeypatch.setattr(
        egress,
        "get_slot_configs",
        lambda: {
            "extraction": {"provider": "openai", "model": "gpt-4o-mini", "configured": True},
            "qa": {"provider": "anthropic", "model": "claude-3", "configured": True},
        },
    )
    summary = egress.build_egress_summary(
        disable_pii=True, extraction_model="extraction", qa_model="qa"
    )
    assert summary.destinations == ("openai/gpt-4o-mini", "anthropic/claude-3")
    assert summary.cloud_warning is True


def test_egress_summary_deduplicates_shared_slot(monkeypatch) -> None:
    """Same model in extraction and QA must appear once."""
    from openreview_cli.tui.domain import egress

    monkeypatch.setattr(egress, "read_privacy_tier", lambda: "maximum")
    monkeypatch.setattr(
        egress,
        "get_slot_configs",
        lambda: {"extraction": {"provider": "ollama", "model": "qwen3:8b", "configured": True}},
    )
    summary = egress.build_egress_summary(
        disable_pii=False, extraction_model="extraction", qa_model="extraction"
    )
    assert summary.destinations == ("ollama/qwen3:8b",)


def test_egress_summary_handles_unconfigured_gateway(monkeypatch) -> None:
    """No slot config at all must not crash and must report no destinations."""
    from openreview_cli.tui.domain import egress

    monkeypatch.setattr(egress, "read_privacy_tier", lambda: "—")
    monkeypatch.setattr(egress, "get_slot_configs", lambda: {})
    summary = egress.build_egress_summary(disable_pii=False, extraction_model="extraction")
    assert summary.privacy_tier == "—"
    assert summary.destinations == ()
    assert summary.cloud_warning is False
    assert any("none configured" in line for line in summary.lines())


def test_egress_summary_skips_slot_without_provider(monkeypatch) -> None:
    """A configured-but-empty primary must not produce a blank destination."""
    from openreview_cli.tui.domain import egress

    monkeypatch.setattr(egress, "read_privacy_tier", lambda: "maximum")
    monkeypatch.setattr(
        egress,
        "get_slot_configs",
        lambda: {"extraction": {"provider": "", "model": "", "configured": False}},
    )
    summary = egress.build_egress_summary(disable_pii=False, extraction_model="extraction")
    assert summary.destinations == ()


# ── Rendering ─────────────────────────────────────────────────────────


def test_egress_summary_lines_report_privacy_state(monkeypatch) -> None:
    """The rendered lines state the tier, the PII state and the destinations."""
    from openreview_cli.tui.domain import egress

    monkeypatch.setattr(egress, "read_privacy_tier", lambda: "maximum")
    monkeypatch.setattr(
        egress,
        "get_slot_configs",
        lambda: {"extraction": {"provider": "ollama", "model": "qwen3:8b", "configured": True}},
    )
    lines = egress.build_egress_summary(disable_pii=False, extraction_model="extraction").lines()
    joined = "\n".join(lines)
    assert "maximum" in joined
    assert "PII stripped before egress: Yes" in joined
    assert "ollama/qwen3:8b" in joined


def test_egress_summary_lines_flag_disabled_pii(monkeypatch) -> None:
    from openreview_cli.tui.domain import egress

    monkeypatch.setattr(egress, "read_privacy_tier", lambda: "maximum")
    monkeypatch.setattr(
        egress,
        "get_slot_configs",
        lambda: {"extraction": {"provider": "ollama", "model": "qwen3:8b", "configured": True}},
    )
    lines = egress.build_egress_summary(disable_pii=True, extraction_model="extraction").lines()
    assert any("PII stripped before egress: NO" in line for line in lines)


# ── Registry-driven classification ────────────────────────────────────


def test_is_cloud_classifies_from_an_injected_registry() -> None:
    """#145/C7: registry membership and base_url decide, not the provider name."""
    from openreview_cli.tui.domain import egress

    registry = {
        "custom-local": ProviderInfo(name="custom-local", base_url="http://localhost:1234/v1"),
        "remote-named-local": ProviderInfo(
            name="remote-named-local", base_url="https://api.example.com/v1"
        ),
        "bedrock-like": ProviderInfo(name="bedrock-like", is_local=False, base_url=None),
    }
    assert egress._is_cloud("custom-local", registry) is False
    assert egress._is_cloud("remote-named-local", registry) is True
    assert egress._is_cloud("bedrock-like", registry) is True  # ValueError -> cloud
    assert egress._is_cloud("undeclared", {}) is True
    assert egress._is_cloud("", registry) is False


def test_build_egress_summary_loads_the_registry_once(monkeypatch) -> None:
    """#145/C8: one registry read per summary, however many destinations."""
    from openreview_cli.tui.domain import egress

    calls: list[int] = []

    def counting_load_registry() -> dict[str, ProviderInfo]:
        calls.append(1)
        return dict(_REGISTRY)

    monkeypatch.setattr(egress, "load_registry", counting_load_registry)
    monkeypatch.setattr(egress, "read_privacy_tier", lambda: "performance")
    monkeypatch.setattr(
        egress,
        "get_slot_configs",
        lambda: {
            "extraction": {"provider": "openai", "model": "gpt-4o-mini"},
            "qa": {"provider": "anthropic", "model": "claude-3"},
        },
    )

    summary = egress.build_egress_summary(
        disable_pii=True, extraction_model="extraction", qa_model="qa"
    )

    assert summary.destinations == ("openai/gpt-4o-mini", "anthropic/claude-3")
    assert len(calls) == 1


# ── Litellm-free guard ────────────────────────────────────────────────


def test_egress_module_import_does_not_pull_litellm() -> None:
    """Importing tui.domain.egress must NOT pull litellm into sys.modules."""
    code = (
        "import openreview_cli.tui.domain.egress, sys; "
        "sys.exit(1 if 'litellm' in sys.modules else 0)"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr.decode()
