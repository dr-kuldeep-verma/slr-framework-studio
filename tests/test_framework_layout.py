"""Where the boxes go and how the lines run between them.

The spec asks for four things at once: straight connectors, aligned boxes, no
line passing through a box, and as few crossings as possible. The first two
are easy to satisfy and easy to check; the third is the one a naive layout
gets wrong, because a straight line between two boxes two columns apart will
happily cross whatever sits between them. These tests assert the geometry
rather than inspecting the drawing, so a renderer change cannot quietly break
the figure.
"""

from __future__ import annotations

import pytest

from slr_studio.framework.layout import (
    Edge,
    Layout,
    Node,
    count_crossings,
    layout_graph,
    segment_hits_box,
)


def graph(edges, nodes=None):
    names = list(nodes) if nodes else []
    for source, target in edges:
        for name in (source, target):
            if name not in names:
                names.append(name)
    return (
        [Node(key=name, label=name) for name in names],
        [Edge(key=f"{s}->{t}", source=s, target=t) for s, t in edges],
    )


class TestSegmentHitsBox:
    """The primitive the no-line-through-a-box rule is built on."""

    def test_a_line_straight_through_the_middle_hits(self):
        assert segment_hits_box((0, 50), (100, 50), (40, 40, 20, 20)) is True

    def test_a_line_passing_above_misses(self):
        assert segment_hits_box((0, 10), (100, 10), (40, 40, 20, 20)) is False

    def test_a_line_stopping_short_misses(self):
        assert segment_hits_box((0, 50), (30, 50), (40, 40, 20, 20)) is False

    def test_a_line_that_merely_touches_the_edge_does_not_count_as_through(self):
        """Connectors land on a box's edge by definition; that is not a hit."""
        assert segment_hits_box((0, 40), (100, 40), (40, 40, 20, 20)) is False

    def test_a_diagonal_clipping_a_corner_hits(self):
        assert segment_hits_box((0, 0), (100, 100), (40, 40, 20, 20)) is True

    def test_a_vertical_line_through_a_box_hits(self):
        assert segment_hits_box((50, 0), (50, 100), (40, 40, 20, 20)) is True


class TestCountCrossings:
    def test_two_parallel_edges_do_not_cross(self):
        nodes, edges = graph([("a", "c"), ("b", "d")])
        placed = Layout(
            nodes={
                "a": (0, 0, 10, 10), "b": (0, 50, 10, 10),
                "c": (100, 0, 10, 10), "d": (100, 50, 10, 10),
            },
            edges={e.key: ((10, 5), (100, 5)) for e in edges},
        )
        placed.edges = {
            "a->c": ((10, 5), (100, 5)),
            "b->d": ((10, 55), (100, 55)),
        }
        assert count_crossings(placed) == 0

    def test_a_swapped_pair_crosses_once(self):
        placed = Layout(
            nodes={},
            edges={
                "a->d": ((10, 5), (100, 55)),
                "b->c": ((10, 55), (100, 5)),
            },
        )
        assert count_crossings(placed) == 1

    def test_edges_sharing_an_endpoint_are_not_a_crossing(self):
        """Two arrows into the same box meet there; that is not a crossing."""
        placed = Layout(
            nodes={},
            edges={
                "a->c": ((10, 5), (100, 30)),
                "b->c": ((10, 55), (100, 30)),
            },
        )
        assert count_crossings(placed) == 0


class TestLayout:
    def test_every_node_is_placed_exactly_once(self):
        nodes, edges = graph([("a", "b"), ("b", "c"), ("a", "c")])
        placed = layout_graph(nodes, edges)
        assert set(placed.nodes) == {"a", "b", "c"}

    def test_boxes_do_not_overlap(self):
        nodes, edges = graph(
            [("a", "d"), ("b", "d"), ("c", "d"), ("d", "e"), ("a", "e")]
        )
        placed = layout_graph(nodes, edges)
        boxes = list(placed.nodes.values())
        for i, first in enumerate(boxes):
            for second in boxes[i + 1 :]:
                assert not _overlap(first, second), (first, second)

    def test_boxes_are_aligned_in_columns(self):
        """"Aligned boxes" means shared x positions, not a scatter."""
        nodes, edges = graph([("a", "b"), ("b", "c"), ("a", "c"), ("x", "c")])
        placed = layout_graph(nodes, edges)
        xs = {round(box[0], 3) for box in placed.nodes.values()}
        assert len(xs) < len(placed.nodes)

    def test_boxes_in_a_column_share_a_width(self):
        nodes, edges = graph([("a", "c"), ("b", "c")])
        placed = layout_graph(nodes, edges)
        by_column: dict[float, set[float]] = {}
        for x, _y, w, _h in placed.nodes.values():
            by_column.setdefault(round(x, 3), set()).add(round(w, 3))
        for widths in by_column.values():
            assert len(widths) == 1

    def test_a_connector_between_neighbours_is_one_straight_segment(self):
        """Nothing can sit between adjacent columns, so nothing may bend."""
        nodes, edges = graph([("a", "b")])
        placed = layout_graph(nodes, edges)
        assert len(placed.edges["a->b"]) == 2

    def test_every_connector_is_made_of_straight_segments(self):
        """"Straight connectors" rules out curves, not the occasional bend.

        A connector spanning three columns, or running backwards against the
        flow, cannot always be a single segment and also miss every box in
        between, so it is allowed to bend. What is never allowed is a curve,
        or a segment through a box.
        """
        nodes, edges = graph(
            [("a", "b"), ("b", "c"), ("a", "c"), ("a", "d"), ("d", "c")]
        )
        placed = layout_graph(nodes, edges)
        for key, points in placed.edges.items():
            assert len(points) >= 2, key
            assert all(len(point) == 2 for point in points), key

    def test_no_connector_passes_through_a_box(self):
        """The rule a naive straight-line layout breaks."""
        nodes, edges = graph(
            [("a", "b"), ("b", "c"), ("a", "c"), ("a", "d"), ("d", "c")]
        )
        placed = layout_graph(nodes, edges)
        for key, points in placed.edges.items():
            source, target = key.split("->")
            for start, end in zip(points, points[1:]):
                for name, box in placed.nodes.items():
                    if name in (source, target):
                        continue
                    assert not segment_hits_box(start, end, box), (
                        f"{key} passes through {name}"
                    )

    def test_a_long_edge_is_routed_round_rather_than_through(self):
        """a -> c must dodge b, which sits between them."""
        nodes, edges = graph([("a", "b"), ("b", "c"), ("a", "c")])
        placed = layout_graph(nodes, edges)
        points = placed.edges["a->c"]
        for start, end in zip(points, points[1:]):
            assert not segment_hits_box(start, end, placed.nodes["b"])

    def test_crossings_are_not_worse_than_the_order_given(self):
        nodes, edges = graph(
            [("a", "y"), ("b", "x"), ("c", "z"), ("a", "x"), ("c", "y")]
        )
        placed = layout_graph(nodes, edges)
        assert count_crossings(placed) <= count_crossings(
            layout_graph(nodes, edges, minimise_crossings=False)
        )

    def test_a_graph_with_no_edges_still_lays_out(self):
        nodes, edges = graph([], nodes=["lonely", "also lonely"])
        placed = layout_graph(nodes, edges)
        assert len(placed.nodes) == 2
        assert placed.edges == {}

    def test_an_empty_graph_is_not_an_error(self):
        placed = layout_graph([], [])
        assert placed.nodes == {} and placed.edges == {}

    def test_a_cycle_does_not_hang(self):
        nodes, edges = graph([("a", "b"), ("b", "c"), ("c", "a")])
        placed = layout_graph(nodes, edges)
        assert len(placed.nodes) == 3

    def test_a_self_link_is_placed_without_a_zero_length_line(self):
        nodes, edges = graph([("a", "a")])
        placed = layout_graph(nodes, edges)
        points = placed.edges["a->a"]
        assert len(set(points)) > 1

    def test_the_same_graph_lays_out_the_same_way_twice(self):
        """A figure in a thesis must not move between runs."""
        nodes, edges = graph([("a", "c"), ("b", "c"), ("c", "d")])
        first = layout_graph(nodes, edges)
        second = layout_graph(nodes, edges)
        assert first.nodes == second.nodes
        assert first.edges == second.edges

    def test_the_canvas_covers_every_box(self):
        nodes, edges = graph([("a", "b"), ("b", "c")])
        placed = layout_graph(nodes, edges)
        for x, y, w, h in placed.nodes.values():
            assert x >= 0 and y >= 0
            assert x + w <= placed.width and y + h <= placed.height

    def test_a_longer_label_gets_a_wider_box(self):
        short = layout_graph([Node(key="a", label="a")], [])
        long = layout_graph(
            [Node(key="a", label="perceived distributive fairness of benefits")], []
        )
        assert long.nodes["a"][2] > short.nodes["a"][2]


def _overlap(first, second) -> bool:
    ax, ay, aw, ah = first
    bx, by, bw, bh = second
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


class TestTheInvariantHoldsGenerally:
    """The hand-written cases above are the ones that occurred to me.

    This one does not care what occurred to me. The first version of the
    router passed every test above and still put a line through a box on
    about one graph in five, almost always a link running backwards against
    the flow or between two constructs in the same column.
    """

    def _random_graph(self, rng):
        import itertools

        names = [chr(ord("a") + i) for i in range(rng.randint(2, 9))]
        nodes = [Node(key=n, label=n * rng.randint(1, 12)) for n in names]
        pairs = list(itertools.permutations(names, 2))
        rng.shuffle(pairs)
        how_many = rng.randint(1, min(12, len(pairs)))
        edges = [
            Edge(key=f"{s}->{t}", source=s, target=t)
            for s, t in pairs[:how_many]
        ]
        return nodes, edges

    def test_no_connector_passes_through_a_box_on_any_graph(self):
        import random

        rng = random.Random(7)
        for trial in range(200):
            nodes, edges = self._random_graph(rng)
            placed = layout_graph(nodes, edges)
            for key, points in placed.edges.items():
                source, target = key.split("->")
                for start, end in zip(points, points[1:]):
                    for name, box in placed.nodes.items():
                        if name in (source, target):
                            continue
                        assert not segment_hits_box(start, end, box), (
                            f"trial {trial}: {key} through {name}"
                        )

    def test_boxes_never_overlap_on_any_graph(self):
        import random

        rng = random.Random(11)
        for _ in range(200):
            nodes, edges = self._random_graph(rng)
            placed = layout_graph(nodes, edges)
            boxes = list(placed.nodes.values())
            for i, first in enumerate(boxes):
                for second in boxes[i + 1 :]:
                    assert not _overlap(first, second)

    def test_most_connectors_are_still_a_single_segment(self):
        """Detours are the exception; a figure of elbows would be unreadable."""
        import random

        rng = random.Random(7)
        straight = total = 0
        for _ in range(200):
            nodes, edges = self._random_graph(rng)
            placed = layout_graph(nodes, edges)
            for points in placed.edges.values():
                total += 1
                straight += len(points) == 2
        assert straight / total > 0.6


class TestDetoursDoNotOverlap:
    """Two connectors taking the same way round must not sit on top of each
    other: a reader cannot follow either, and the figure looks like one line.
    """

    def _collinear_overlap(self, first, second) -> bool:
        # Two connectors into the same box share that anchor, and the last
        # stretch into it. That is how an arrowhead works, not a fault.
        shared = set(first) & set(second)
        for a, b in zip(first, first[1:]):
            if a in shared or b in shared:
                continue
            for c, d in zip(second, second[1:]):
                if c in shared or d in shared:
                    continue
                horizontal = a[1] == b[1] == c[1] == d[1]
                vertical = a[0] == b[0] == c[0] == d[0]
                if not (horizontal or vertical):
                    continue
                axis = 0 if horizontal else 1
                lo1, hi1 = sorted((a[axis], b[axis]))
                lo2, hi2 = sorted((c[axis], d[axis]))
                if min(hi1, hi2) - max(lo1, lo2) > 1.0:
                    return True
        return False

    def test_two_connectors_round_the_back_take_different_lanes(self):
        nodes, edges = graph(
            [("a", "b"), ("b", "c"), ("c", "d"), ("d", "e"),
             ("e", "a"), ("d", "a"), ("e", "b")]
        )
        placed = layout_graph(nodes, edges)
        detours = [p for p in placed.edges.values() if len(p) > 3]
        assert len(detours) >= 2, "this graph is meant to force two detours"
        for i, first in enumerate(detours):
            for second in detours[i + 1 :]:
                assert not self._collinear_overlap(first, second)
