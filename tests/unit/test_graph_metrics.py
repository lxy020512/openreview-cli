from __future__ import annotations

import pytest

from openreview_cli.graph.metrics import (
    compute_broken_ref_count,
    compute_definition_coverage,
    compute_density,
    compute_max_depth,
    compute_metrics,
    compute_orphan_ratio,
)
from openreview_cli.graph.models import ContractGraph, EdgeType, GraphEdge, GraphNode


def _single_node() -> ContractGraph:
    return ContractGraph(nodes={"c1": GraphNode("c1", "1", "text", 0)}, edges=[])


def _two_nodes_one_edge() -> ContractGraph:
    return ContractGraph(
        nodes={
            "c1": GraphNode("c1", "1", "t", 0),
            "c2": GraphNode("c2", "2", "t", 1),
        },
        edges=[GraphEdge("c1", "c2", EdgeType.parent_child)],
    )


class TestDensity:
    def test_single_node_zero(self) -> None:
        assert compute_density(_single_node()) == 0.0

    def test_two_nodes_one_edge_is_0_5(self) -> None:
        density = compute_density(_two_nodes_one_edge())
        assert density == 0.5  # 1 / (2 * 1) = 0.5

    def test_empty_graph_zero(self) -> None:
        assert compute_density(ContractGraph()) == 0.0


class TestMaxDepth:
    def test_single_node_depth_one(self) -> None:
        assert compute_max_depth(_single_node()) == 1

    def test_chain_depth_three(self) -> None:
        graph = ContractGraph(
            nodes={
                "c1": GraphNode("c1", "1", "t", 0),
                "c2": GraphNode("c2", "2", "t", 1),
                "c3": GraphNode("c3", "3", "t", 2),
            },
            edges=[
                GraphEdge("c1", "c2", EdgeType.parent_child),
                GraphEdge("c2", "c3", EdgeType.parent_child),
            ],
        )
        assert compute_max_depth(graph) == 3

    def test_empty_graph_zero(self) -> None:
        assert compute_max_depth(ContractGraph()) == 0


class TestOrphanRatio:
    def test_root_with_child_is_not_orphan(self) -> None:
        """A top-level clause declares no parent, so it is not an orphan."""
        ratio = compute_orphan_ratio(_two_nodes_one_edge())
        assert ratio == 0.0

    def test_declared_parent_present_is_not_orphan(self) -> None:
        graph = ContractGraph(
            nodes={
                "c1": GraphNode("c1", "1", "t", 0),
                "c2": GraphNode("c2", "2", "t", 1, parent_id="c1"),
            },
            edges=[GraphEdge("c1", "c2", EdgeType.parent_child)],
        )
        assert compute_orphan_ratio(graph) == 0.0

    def test_dangling_parent_is_orphan(self) -> None:
        graph = ContractGraph(
            nodes={
                "c1": GraphNode("c1", "1", "t", 0),
                "c2": GraphNode("c2", "2", "t", 1),
                "c3": GraphNode("c3", "3", "t", 2, parent_id="missing"),
            },
            edges=[GraphEdge("c1", "c2", EdgeType.parent_child)],
        )
        # Only c3 names a parent the graph does not contain.
        assert compute_orphan_ratio(graph) == pytest.approx(1.0 / 3.0)


class TestBrokenRefCount:
    def test_no_broken_refs(self) -> None:
        assert compute_broken_ref_count(_two_nodes_one_edge()) == 0

    def test_broken_refs_detected(self) -> None:
        graph = ContractGraph(
            nodes={
                "c1": GraphNode("c1", "1", "t", 0),
                "c2": GraphNode("c2", "2", "t", 1),
            },
            edges=[
                GraphEdge("c1", "c3", EdgeType.cross_ref),
            ],
        )
        assert compute_broken_ref_count(graph) == 1


class TestDefinitionCoverage:
    def test_no_refs_trivially_perfect(self) -> None:
        assert compute_definition_coverage(_single_node()) == 1.0

    def test_half_defined(self) -> None:
        graph = ContractGraph(
            nodes={
                "c1": GraphNode("c1", "1", "t", 0),
                "c2": GraphNode("c2", "2", "t", 1),
            },
            edges=[
                GraphEdge("c1", "c2", EdgeType.def_ref, {"term": "TermA"}),
            ],
        )
        # Both TermA is referenced (via def_ref from c1) and defined (target c2 exists)
        coverage = compute_definition_coverage(graph)
        assert coverage == 1.0


class TestComputeMetrics:
    def test_single_node_all_defaults(self) -> None:
        metrics = compute_metrics(_single_node())
        assert metrics.density == 0.0
        assert metrics.max_depth == 1
        assert metrics.orphan_ratio == 0.0
        assert metrics.broken_ref_count == 0
        assert metrics.definition_coverage == 1.0

    def test_empty_graph_zeros(self) -> None:
        metrics = compute_metrics(ContractGraph())
        assert metrics.density == 0.0
        assert metrics.max_depth == 0
        assert metrics.orphan_ratio == 0.0
        assert metrics.broken_ref_count == 0
        assert metrics.definition_coverage == 1.0
