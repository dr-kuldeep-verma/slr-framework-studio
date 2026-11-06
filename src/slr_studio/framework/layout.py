"""Where the boxes go and how the connectors run between them.

A layered layout, the same shape as the one every diagramming tool uses: put
the constructs in columns by how far they are from a starting point, order the
rows to untangle the lines, then draw. The spec asks for four properties, and
each is met deliberately rather than hoped for:

*Aligned boxes* -- every box in a column shares an x and a width, so the
figure reads as a grid rather than a scatter.

*Straight connectors* -- a connector is a sequence of straight segments, never
a curve. Between neighbouring columns it is always a single segment. A
connector spanning three or more columns is given one bend where a straight
line would otherwise cross a box, because those two requirements cannot both
hold in general and a line through a box is the worse failure.

*No line through a box* -- enforced by checking every segment against every
box it does not belong to, and routing round what it hits. This is the rule a
naive straight-line layout breaks, and it is checked rather than assumed.

*As few crossings as possible* -- the usual barycentre sweep, keeping the best
ordering found. It is a heuristic, not an optimum; what is guaranteed is that
the result is never worse than the order the nodes arrived in.

Nothing here knows what a construct or a grade is. It takes keys and labels
and returns coordinates, which is what makes it testable as geometry.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "Edge",
    "Layout",
    "Node",
    "count_crossings",
    "layout_graph",
    "segment_hits_box",
]

Point = tuple[float, float]
Box = tuple[float, float, float, float]  # x, y, width, height

#: Geometry, in points. A box is sized from its label; these are the limits
#: and the spacing around them.
_MIN_WIDTH = 110.0
_MAX_WIDTH = 190.0
_CHAR_WIDTH = 6.2
_PADDING = 18.0
_HEIGHT = 46.0
_COLUMN_GAP = 86.0
_ROW_GAP = 34.0
_MARGIN = 28.0

#: How hard to try at untangling. Four sweeps settles these graphs; more makes
#: no difference at the sizes an evidence map reaches.
_SWEEPS = 4

_EPS = 1e-9


@dataclass(frozen=True)
class Node:
    key: str
    label: str


@dataclass(frozen=True)
class Edge:
    key: str
    source: str
    target: str


@dataclass
class Layout:
    """Boxes and connectors in a coordinate space with the origin top left."""

    nodes: dict[str, Box] = field(default_factory=dict)
    #: Each connector is the points of a polyline, source end first.
    edges: dict[str, tuple[Point, ...]] = field(default_factory=dict)
    width: float = 0.0
    height: float = 0.0
    #: Which column each node ended up in, for callers that group by depth.
    columns: dict[str, int] = field(default_factory=dict)


# -- geometry ---------------------------------------------------------------


def segment_hits_box(start: Point, end: Point, box: Box) -> bool:
    """Does the segment pass through the inside of the box?

    The box's edge does not count: every connector lands on some box's edge by
    definition, so counting a touch would make the rule unsatisfiable. Only a
    segment with a stretch of positive length strictly inside the rectangle is
    a hit.

    Liang-Barsky clipping against the open rectangle.
    """
    x0, y0 = start
    x1, y1 = end
    bx, by, bw, bh = box
    dx, dy = x1 - x0, y1 - y0

    t_enter, t_exit = 0.0, 1.0
    for delta, origin, low, high in (
        (-dx, -(bx - x0), None, None),
        (dx, bx + bw - x0, None, None),
        (-dy, -(by - y0), None, None),
        (dy, by + bh - y0, None, None),
    ):
        del low, high
        if abs(delta) < _EPS:
            # Parallel to this pair of edges. For the *open* rectangle the
            # line has to be strictly inside the slab: a connector running
            # exactly along a box's edge is grazing it, not passing through.
            if origin <= _EPS:
                return False
            continue
        t = origin / delta
        if delta < 0:
            if t > t_exit:
                return False
            t_enter = max(t_enter, t)
        else:
            if t < t_enter:
                return False
            t_exit = min(t_exit, t)
    return t_exit - t_enter > 1e-6


def _segments_cross(a: Point, b: Point, c: Point, d: Point) -> bool:
    """Do two segments properly cross, sharing no endpoint?"""
    if a in (c, d) or b in (c, d):
        return False

    def orient(p: Point, q: Point, r: Point) -> float:
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    d1, d2 = orient(c, d, a), orient(c, d, b)
    d3, d4 = orient(a, b, c), orient(a, b, d)
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0))


def count_crossings(layout: Layout) -> int:
    """How many times one connector crosses another.

    Connectors meeting at a shared box are not a crossing; they meet there
    because they describe the same construct.
    """
    polylines = list(layout.edges.values())
    total = 0
    for i, first in enumerate(polylines):
        for second in polylines[i + 1 :]:
            for a, b in zip(first, first[1:]):
                for c, d in zip(second, second[1:]):
                    if _segments_cross(a, b, c, d):
                        total += 1
    return total


# -- layering ---------------------------------------------------------------


def _break_cycles(keys: list[str], edges: list[Edge]) -> set[str]:
    """Edge keys to treat as pointing backwards, so layering terminates.

    A depth-first walk in the order the nodes arrived; any edge reaching a
    node still open on the stack is a back edge. Which edges get chosen
    depends on that order, which is why the order is the caller's and stable.
    """
    out: dict[str, list[Edge]] = {key: [] for key in keys}
    for edge in edges:
        if edge.source in out:
            out[edge.source].append(edge)

    back: set[str] = set()
    state: dict[str, int] = {key: 0 for key in keys}  # 0 new, 1 open, 2 done

    def walk(key: str) -> None:
        state[key] = 1
        for edge in out[key]:
            if edge.target == key:
                back.add(edge.key)          # a self link goes nowhere
            elif state.get(edge.target) == 1:
                back.add(edge.key)
            elif state.get(edge.target) == 0:
                walk(edge.target)
        state[key] = 2

    for key in keys:
        if state[key] == 0:
            walk(key)
    return back


def _assign_columns(
    keys: list[str], edges: list[Edge], back: set[str]
) -> dict[str, int]:
    """Longest path from the sources, so every arrow points rightwards."""
    forward = [e for e in edges if e.key not in back]
    incoming: dict[str, list[Edge]] = {key: [] for key in keys}
    for edge in forward:
        if edge.target in incoming and edge.source in incoming:
            incoming[edge.target].append(edge)

    column = {key: 0 for key in keys}
    # Relaxing |V| times settles any acyclic graph.
    for _ in range(len(keys)):
        changed = False
        for key in keys:
            for edge in incoming[key]:
                if column[edge.source] + 1 > column[key]:
                    column[key] = column[edge.source] + 1
                    changed = True
        if not changed:
            break
    return column


def _order_rows(
    columns: dict[str, int],
    keys: list[str],
    edges: list[Edge],
    back: set[str],
    *,
    minimise: bool,
) -> dict[int, list[str]]:
    """Row order within each column, swept to reduce crossings."""
    by_column: dict[int, list[str]] = {}
    for key in keys:
        by_column.setdefault(columns[key], []).append(key)
    if not minimise:
        return by_column

    neighbours_left: dict[str, list[str]] = {key: [] for key in keys}
    neighbours_right: dict[str, list[str]] = {key: [] for key in keys}
    for edge in edges:
        if edge.key in back or edge.source == edge.target:
            continue
        if edge.source not in columns or edge.target not in columns:
            continue
        neighbours_left[edge.target].append(edge.source)
        neighbours_right[edge.source].append(edge.target)

    def sweep(order: dict[int, list[str]], rightwards: bool) -> dict[int, list[str]]:
        position = {
            key: index
            for keys_in_column in order.values()
            for index, key in enumerate(keys_in_column)
        }
        new = {column: list(rows) for column, rows in order.items()}
        sequence = sorted(order) if rightwards else sorted(order, reverse=True)
        for column in sequence:
            source = neighbours_left if rightwards else neighbours_right

            def barycentre(key: str) -> tuple[float, int]:
                others = [position[o] for o in source[key] if o in position]
                here = position[key]
                return (sum(others) / len(others) if others else here, here)

            new[column] = sorted(new[column], key=barycentre)
            for index, key in enumerate(new[column]):
                position[key] = index
        return new

    best = by_column
    best_score = _crossing_estimate(best, edges, back, columns)
    current = by_column
    for step in range(_SWEEPS):
        current = sweep(current, rightwards=step % 2 == 0)
        score = _crossing_estimate(current, edges, back, columns)
        if score < best_score:
            best, best_score = current, score
    return best


def _crossing_estimate(
    order: dict[int, list[str]],
    edges: list[Edge],
    back: set[str],
    columns: dict[str, int],
) -> int:
    """Crossings counted on row indices, before coordinates exist."""
    position = {
        key: index
        for rows in order.values()
        for index, key in enumerate(rows)
    }
    pairs = [
        (position[e.source], position[e.target])
        for e in edges
        if e.key not in back
        and e.source != e.target
        and e.source in position
        and e.target in position
        and columns[e.target] - columns[e.source] == 1
    ]
    return sum(
        1
        for i, (a1, b1) in enumerate(pairs)
        for (a2, b2) in pairs[i + 1 :]
        if (a1 - a2) * (b1 - b2) < 0
    )


# -- placement --------------------------------------------------------------


def _box_width(label: str) -> float:
    return min(_MAX_WIDTH, max(_MIN_WIDTH, len(label) * _CHAR_WIDTH + _PADDING))


def _place(
    order: dict[int, list[str]], labels: dict[str, str]
) -> tuple[dict[str, Box], float, float]:
    widths = {
        column: max((_box_width(labels[key]) for key in rows), default=_MIN_WIDTH)
        for column, rows in order.items()
    }
    xs: dict[int, float] = {}
    x = _MARGIN
    for column in sorted(order):
        xs[column] = x
        x += widths[column] + _COLUMN_GAP
    total_width = x - _COLUMN_GAP + _MARGIN

    tallest = max((len(rows) for rows in order.values()), default=0)
    band = tallest * _HEIGHT + max(0, tallest - 1) * _ROW_GAP
    total_height = band + 2 * _MARGIN

    boxes: dict[str, Box] = {}
    for column, rows in order.items():
        height = len(rows) * _HEIGHT + max(0, len(rows) - 1) * _ROW_GAP
        top = _MARGIN + (band - height) / 2
        for index, key in enumerate(rows):
            boxes[key] = (
                xs[column],
                top + index * (_HEIGHT + _ROW_GAP),
                widths[column],
                _HEIGHT,
            )
    return boxes, total_width, total_height


# -- routing ----------------------------------------------------------------


def _anchors(source: Box, target: Box) -> tuple[Point, Point, int, int]:
    """Where a connector leaves one box and meets the other.

    Always on a vertical edge, with the direction it points away from the box,
    because a stub leaving sideways lands in the gap between two columns,
    where no box can ever be. A stub leaving the top or bottom would run up
    the middle of its own column and through whatever is stacked there.
    """
    sx, sy, sw, sh = source
    tx, ty, tw, th = target
    if tx >= sx + sw:                       # target to the right
        return (sx + sw, sy + sh / 2), (tx, ty + th / 2), +1, -1
    if tx + tw <= sx:                       # target to the left
        return (sx, sy + sh / 2), (tx + tw, ty + th / 2), -1, +1
    # Same column: out to the right and back in to the right.
    return (sx + sw, sy + sh / 2), (tx + tw, ty + th / 2), +1, +1


def _self_loop(box: Box) -> tuple[Point, ...]:
    x, y, w, h = box
    out = 24.0
    return (
        (x + w, y + h / 3),
        (x + w + out, y + h / 3),
        (x + w + out, y + 2 * h / 3),
        (x + w, y + 2 * h / 3),
    )


def _blocked_by(
    points: tuple[Point, ...], boxes: dict[str, Box], ignore: set[str]
) -> list[str]:
    hit = []
    for name, box in boxes.items():
        if name in ignore:
            continue
        if any(
            segment_hits_box(a, b, box) for a, b in zip(points, points[1:])
        ):
            hit.append(name)
    return hit


def _route(
    edge: Edge, boxes: dict[str, Box], lane: float, *, stub_rank: int = 0
) -> tuple[Point, ...]:
    """A straight connector where one works, and a clear detour where it does not.

    Three attempts, in order of how much the eye has to follow:

    1. A single straight segment. Every connector between neighbouring
       columns takes this, which is most of them.
    2. One bend, round whatever the straight line met.
    3. A stub out into the gap between columns, along a lane outside every
       box, and a stub back in. This one cannot fail: the stubs sit in the
       gutters where no box has any x, and the lane is above or below the
       whole band, so no segment can be inside any box.
    """
    if edge.source == edge.target:
        return _self_loop(boxes[edge.source])

    source_box, target_box = boxes[edge.source], boxes[edge.target]
    start, end, out_from, out_to = _anchors(source_box, target_box)
    ignore = {edge.source, edge.target}

    direct: tuple[Point, ...] = (start, end)
    blocking = _blocked_by(direct, boxes, ignore)
    if not blocking:
        return direct

    obstructions = [boxes[name] for name in blocking]
    midpoint_y = (start[1] + end[1]) / 2
    lanes = sorted(
        (
            min(box[1] for box in obstructions) - _ROW_GAP / 2,
            max(box[1] + box[3] for box in obstructions) + _ROW_GAP / 2,
        ),
        key=lambda y: abs(y - midpoint_y),
    )
    # Several places to put the bend, nearest the middle first. A waypoint in
    # the gap between two columns often clears what the midpoint cannot,
    # which is the difference between one bend and the full detour.
    span = end[0] - start[0]
    offsets = (0.5, 0.25, 0.75, 0.12, 0.88)
    for bend_y in lanes:
        for fraction in offsets:
            bent = (start, (start[0] + span * fraction, bend_y), end)
            if not _blocked_by(bent, boxes, ignore):
                return bent

    # Stagger the stub so two detours leaving the same gutter stay apart.
    stub = _COLUMN_GAP / 3 + (stub_rank % 3) * 7.0
    return (
        start,
        (start[0] + out_from * stub, start[1]),
        (start[0] + out_from * stub, lane),
        (end[0] + out_to * stub, lane),
        (end[0] + out_to * stub, end[1]),
        end,
    )


def layout_graph(
    nodes: list[Node],
    edges: list[Edge],
    *,
    minimise_crossings: bool = True,
) -> Layout:
    """Place the boxes and route the connectors.

    Deterministic: the same graph in the same order lays out identically every
    time, because a figure in a thesis must not move between runs.
    """
    if not nodes:
        return Layout()

    keys = [node.key for node in nodes]
    labels = {node.key: node.label for node in nodes}
    known = set(keys)
    edges = [e for e in edges if e.source in known and e.target in known]

    back = _break_cycles(keys, edges)
    columns = _assign_columns(keys, edges, back)
    order = _order_rows(columns, keys, edges, back, minimise=minimise_crossings)
    boxes, width, height = _place(order, labels)

    # A connector that has to detour runs along a lane below every box. The
    # canvas grows to hold it only if one is actually used, so a tidy figure
    # keeps no empty space at the bottom.
    # Each connector that detours gets a lane of its own, and its own stub
    # offset, so two of them cannot lie on top of each other and read as one
    # line. Lanes are handed out in the order the edges arrived, which keeps
    # the figure stable between runs.
    band_bottom = max((box[1] + box[3] for box in boxes.values()), default=0.0)
    routed: dict[str, tuple[Point, ...]] = {}
    detours = 0
    for edge in edges:
        lane = band_bottom + _ROW_GAP * (detours + 1)
        points = _route(edge, boxes, lane, stub_rank=detours)
        if len(points) > 3:
            detours += 1
        routed[edge.key] = points
    if any(len(points) > 3 for points in routed.values()):
        height = max(height, lane + _MARGIN)
        width = max(
            width,
            max(
                (point[0] for points in routed.values() for point in points),
                default=0.0,
            )
            + _MARGIN,
        )
    return Layout(
        nodes=boxes, edges=routed, width=width, height=height, columns=columns
    )
