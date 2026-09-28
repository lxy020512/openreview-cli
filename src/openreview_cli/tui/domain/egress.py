"""Egress summary — what a review will send to external providers (Phase 4).

litellm-free: reads slot config, privacy tier and the provider registry, so the
TUI can build the pre-flight view without importing the gateway router.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from openreview_cli.gateway.models import ProviderInfo, classify_provider
from openreview_cli.gateway.registry import load_registry
from openreview_cli.tui.domain.gateway import get_slot_configs
from openreview_cli.tui.domain.privacy import read_privacy_tier


@dataclass(frozen=True)
class EgressSummary:
    """A human-readable description of a review's outbound data boundary."""

    privacy_tier: str
    pii_stripped: bool
    destinations: tuple[str, ...]
    cloud_warning: bool

    def lines(self) -> list[str]:
        """Render the summary as plain lines for the modal."""
        out = [
            f"Privacy tier: {self.privacy_tier}",
            f"PII stripped before egress: {'Yes' if self.pii_stripped else 'NO'}",
            "",
            "Destinations that may receive document text:",
        ]
        if self.destinations:
            out.extend(f"  • {d}" for d in self.destinations)
        else:
            out.append("  (none configured — local only)")
        if self.cloud_warning:
            out += [
                "",
                "⚠ PII stripping is OFF and a cloud provider is configured — "
                "raw document text will be uploaded.",
            ]
        return out


def _is_cloud(provider: str, registry: Mapping[str, ProviderInfo] | None = None) -> bool:
    """Return True when a provider is not known to run on this machine.

    Classification comes from the registry (``base_url`` / ``is_local``), the same
    source the gateway's tier gate uses. An unresolvable provider is cloud: a name
    nobody declared cannot be asserted to be local, and the gateway fails closed on
    the same state. An empty name is not a destination.
    """
    if not provider:
        return False
    if registry is None:
        registry = load_registry()
    info = registry.get(provider)
    if info is None:
        return True
    try:
        return classify_provider(info) != "local"
    except ValueError:
        return True


def _slot_names(extraction_model: str, qa_model: str | None) -> tuple[str, ...]:
    """Resolve the slots to report, preserving order and de-duplicating."""
    names: list[str] = []
    for name in (extraction_model, qa_model):
        if name and name not in names:
            names.append(name)
    return tuple(names)


def build_egress_summary(
    *,
    disable_pii: bool,
    extraction_model: str,
    qa_model: str | None = None,
) -> EgressSummary:
    """Build the egress summary for a review about to start."""
    slots = get_slot_configs()
    dests: list[str] = []
    for slot in _slot_names(extraction_model, qa_model):
        cfg = slots.get(slot, {})
        provider = str(cfg.get("provider", ""))
        model = str(cfg.get("model", ""))
        if provider:
            dest = f"{provider}/{model}" if model else provider
            if dest not in dests:
                dests.append(dest)

    registry = load_registry()
    has_cloud = any(_is_cloud(d.split("/", 1)[0], registry) for d in dests)
    return EgressSummary(
        privacy_tier=read_privacy_tier(),
        pii_stripped=not disable_pii,
        destinations=tuple(dests),
        cloud_warning=disable_pii and has_cloud,
    )
