from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from openreview_cli.graph.metrics import GraphMetrics

#: Upper bound for depth normalisation. Depth >= this is scored as worst.
MAX_EXPECTED_DEPTH = 10
#: Upper bound for broken-ref count normalisation. Count >= this is worst.
MAX_EXPECTED_BROKEN_REFS = 10

#: Default weights: [density, depth, orphans, broken_refs, coverage].
#: The score measures structural defects only: density and depth are reported
#: but score nothing (a defect-free document scores 100 whether flat or
#: structured). A caller may still weight them via custom weights.
DEFAULT_WEIGHTS: list[float] = [0.0, 0.0, 0.35, 0.40, 0.25]


@dataclass
class HealthScore:
    """A single 0-100 score summarising contract structural quality."""

    score: int
    weights: list[float]


def normalise_weights(weights: list[float]) -> list[float]:
    """Normalise weights to sum to 1.0, or fall back to defaults if all zero.

    Emits a warning on stderr if sum != 1.0.
    """
    total = sum(weights)
    if total <= 0:
        return list(DEFAULT_WEIGHTS)
    if abs(total - 1.0) > 1e-9:
        print(
            f"Warning: weights normalised from {total:.4f} to 1.0",
            file=sys.stderr,
        )
        return [w / total for w in weights]
    return weights


def compute_health(
    metrics: GraphMetrics,
    weights: list[float] | None = None,
) -> HealthScore:
    """Compute a 0-100 health score from graph metrics.

    The default score measures structural defects only: clauses whose declared
    parent is missing (``orphan_ratio``), broken cross-references
    (``broken_ref_count``) and uncovered definitions (``definition_coverage``).
    Density and depth are still accepted and reported but carry zero weight by
    default, so a defect-free document scores 100 whether flat or structured.
    All five components are combined with the configured weights.

    Args:
        metrics: Computed graph metrics.
        weights: Five weights (density, depth, orphans, broken_refs,
            coverage). None = use defaults. Zero-sum weights = use defaults.

    Returns:
        HealthScore with integer 0-100 score and weights used.

    Raises:
        ValueError: If weights has a length other than 5.
    """
    if weights is None:
        resolved_weights = list(DEFAULT_WEIGHTS)
    else:
        if len(weights) != 5:
            raise ValueError(f"Expected 5 weights, got {len(weights)}")
        resolved_weights = normalise_weights(weights)

    # Components (all normalised to [0,1], higher = better)
    c1 = 1.0 - metrics.density  # Low density is good
    c2 = 1.0 - min(metrics.max_depth / MAX_EXPECTED_DEPTH, 1.0)
    c3 = 1.0 - metrics.orphan_ratio
    c4 = 1.0 - min(metrics.broken_ref_count / MAX_EXPECTED_BROKEN_REFS, 1.0)
    c5 = metrics.definition_coverage

    raw = (
        resolved_weights[0] * c1
        + resolved_weights[1] * c2
        + resolved_weights[2] * c3
        + resolved_weights[3] * c4
        + resolved_weights[4] * c5
    )

    score = max(0, min(100, round(raw * 100)))
    return HealthScore(score=score, weights=resolved_weights)


__all__ = [
    "DEFAULT_WEIGHTS",
    "HealthScore",
    "compute_health",
]
