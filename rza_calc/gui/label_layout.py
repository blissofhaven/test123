"""Read-only, deterministic label layout; never routes an electrical wire.

Clearance and zoom are fixed before acceptance measurements (B3-control-point).
Saved manual anchors are obstacles, not suggestions. No layout is written back
to the document: only an explicit editor command changes its manual/auto mode.
"""
from __future__ import annotations

import math
import weakref
from dataclasses import dataclass
from collections import defaultdict
from functools import lru_cache

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QFont, QFontMetricsF, QGuiApplication, QPainterPath, QPainterPathStroker

from ..editor.labels import label_is_manual

LABEL_CLEARANCE = 4.0
MIN_LABEL_ZOOM = 0.6
LABEL_WRAP_WIDTH = 220.0

_font_database_application = None
_font_database_generation = 0


def _font_database_changed():
    global _font_database_generation
    _font_database_generation += 1


def _font_generation(application):
    global _font_database_application
    if application is not None and (
        _font_database_application is None
        or _font_database_application() is not application
    ):
        _font_database_application = weakref.ref(application)
        application.fontDatabaseChanged.connect(_font_database_changed)
        _font_database_changed()
    return _font_database_generation


@dataclass(frozen=True)
class _WrappedLabelText:
    text: str
    font: QFont
    application_font: QFont
    dpi: float
    width: float
    font_generation: int
    wrapped: str


def wrapped_label_text(label, text):
    """Reuse only the last wrapping result owned by this live text item.

    Geometry and collision layout are deliberately absent: they still update
    for each preview. Font values are copied rather than keyed by their mutable
    identity or a partial string serialization. No Qt metrics survive a call.
    """
    text = str(text)
    font = label.font()
    application = QGuiApplication.instance()
    application_font = application.font() if application is not None else QFont()
    generation = _font_generation(application)
    dpi = QFontMetricsF(font).fontDpi()
    previous = getattr(label, "_rza_wrapped_label_text", None)
    if previous is not None and (
        previous.text == text and previous.font == font
        and previous.application_font == application_font and previous.dpi == dpi
        and previous.width == LABEL_WRAP_WIDTH and previous.font_generation == generation
    ):
        return previous.wrapped
    wrapped = wrap_text(text, font)
    label._rza_wrapped_label_text = _WrappedLabelText(
        text, QFont(font), QFont(application_font), dpi, LABEL_WRAP_WIDTH, generation, wrapped,
    )
    return wrapped


def wrap_text(text, font):
    """Wrap without discarding any name or parameter characters."""
    metrics = QFontMetricsF(font)
    lines = []
    for paragraph in str(text).split("\n"):
        line = ""
        for word in paragraph.split():
            candidate = f"{line} {word}" if line else word
            if line and metrics.horizontalAdvance(candidate) > LABEL_WRAP_WIDTH:
                lines.append(line)
                line = ""
            for char in ((" " if line else "") + word):
                if line and metrics.horizontalAdvance(line + char) > LABEL_WRAP_WIDTH:
                    lines.append(line)
                    line = ""
                line += char
        lines.append(line)
    return "\n".join(lines)


@dataclass(frozen=True)
class LabelConflict:
    representation_id: object
    obstacle_id: str
    kind: str
    manual: bool


class _Obstacles:
    """Spatial buckets avoid scanning the whole diagram per candidate."""
    def __init__(self):
        self.rows = []
        self.cells = defaultdict(list)

    @staticmethod
    def _keys(rect):
        for x in range(math.floor(rect.left() / 160), math.floor(rect.right() / 160) + 1):
            for y in range(math.floor(rect.top() / 160), math.floor(rect.bottom() / 160) + 1):
                yield x, y

    def add(self, key, kind, rect, path=None):
        index = len(self.rows)
        self.rows.append((key, kind, QRectF(rect), path))
        for cell in self._keys(rect):
            self.cells[cell].append(index)

    def collisions(self, rect):
        expanded = rect.adjusted(-LABEL_CLEARANCE, -LABEL_CLEARANCE,
                                 LABEL_CLEARANCE, LABEL_CLEARANCE)
        indices = {index for cell in self._keys(expanded) for index in self.cells.get(cell, ())}
        return [(key, kind) for index in sorted(indices)
                for key, kind, bounds, path in (self.rows[index],)
                if bounds.intersects(expanded) and (path is None or path.intersects(expanded))]

    def has_collision(self, rect):
        """Candidate rejection needs one obstacle, not a full diagnostic list."""
        expanded = rect.adjusted(-LABEL_CLEARANCE, -LABEL_CLEARANCE,
                                 LABEL_CLEARANCE, LABEL_CLEARANCE)
        seen = set()
        for cell in self._keys(expanded):
            for index in self.cells.get(cell, ()):
                if index in seen:
                    continue
                seen.add(index)
                _, _, bounds, path = self.rows[index]
                if bounds.intersects(expanded) and (path is None or path.intersects(expanded)):
                    return True
        return False


@lru_cache(maxsize=17)
def _ring_offsets(radius):
    offsets = [(0, 0)] if radius == 0 else [
        (dx * 24.0, dy * 24.0)
        for dx in range(-radius, radius + 1)
        for dy in range(-radius, radius + 1)
        if max(abs(dx), abs(dy)) == radius
    ]
    return tuple(sorted(offsets, key=lambda p: (p[0] ** 2 + p[1] ** 2, p[1], p[0])))


def _candidates(body, width, height, preferred):
    """Near anchors first, then bounded rings, not a far-away free label pile."""
    gap = LABEL_CLEARANCE + 2.0
    centers = [
        (preferred.x(), preferred.y()),
        (body.right() + gap, body.center().y() - height / 2),
        (body.left() - gap - width, body.center().y() - height / 2),
        (body.center().x() - width / 2, body.bottom() + gap),
        (body.center().x() - width / 2, body.top() - gap - height),
    ]
    seen = set()
    for radius in range(17):
        # A 24-unit search step is a layout search resolution, not a tolerance.
        for dx, dy in _ring_offsets(radius):
            for center_x, center_y in centers:
                x, y = center_x + dx, center_y + dy
                key = (round(x, 6), round(y, 6))
                if key not in seen:
                    seen.add(key)
                    yield QRectF(x, y, width, height)


def object_label_obstacle_rect(item):
    """Conservative painted bounds, without selection or rotation overlays.

    Cosmetic strokes are widest in scene coordinates at the minimum visible
    label zoom. Primitive scale matters: a bus uses a thicker pen than its
    apparatus's base pen. Display-only bridge arcs can leave the original
    primitive bounds, but never become saved electrical geometry.
    """
    bounds = item.body_scene_rect().united(item.mapRectToScene(item.symbol_ink_rect()))
    if getattr(item, "_label_route_owner", False):
        return bounds

    bridges = getattr(item, "_bridge_paths", {})
    for index, primitive in enumerate(item.symbol_geometry().primitives):
        left, top, right, bottom = primitive.bounds()
        primitive_bounds = QRectF(left, top, right - left, bottom - top)
        bridge = bridges.get(index)
        if bridge is not None:
            primitive_bounds = primitive_bounds.united(bridge.boundingRect())
        margin = item._line_width * primitive.stroke_scale / (2.0 * MIN_LABEL_ZOOM)
        primitive_bounds.adjust(-margin, -margin, margin, margin)
        if not primitive.rotates and item.rotation() and bridge is None:
            # The renderer counter-rotates internal text about its own anchor.
            center = QPointF(*primitive.center)
            primitive_bounds.translate(item.mapToScene(center) - center)
            painted = primitive_bounds
        else:
            painted = item.mapRectToScene(primitive_bounds)
        bounds = bounds.united(painted)

    for point in getattr(item, "_bridge_nodes", ()):
        if getattr(item, "_canonical_key", "") != "busbar":
            bounds = bounds.united(item.mapRectToScene(
                QRectF(point.x() - 3.0, point.y() - 3.0, 6.0, 6.0)
            ))
    bus_radius = 3.3 + 1.25 / (2.0 * MIN_LABEL_ZOOM)
    for point in getattr(item, "_bus_junction_points", ()):
        bounds = bounds.united(item.mapRectToScene(QRectF(
            point.x() - bus_radius, point.y() - bus_radius,
            2.0 * bus_radius, 2.0 * bus_radius,
        )))
    return bounds


def route_label_obstacle_path(route):
    """Use visible bridge ink, not the straight electrical/hit-test path."""
    stroker = QPainterPathStroker()
    # 3.0 covers a selected physical branch (2.1 + 0.9); transient connection
    # feedback, like other editing overlays, is not a document obstacle.
    stroker.setWidth(3.0 / MIN_LABEL_ZOOM)
    stroker.setCapStyle(Qt.PenCapStyle.RoundCap)
    stroker.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    display_path = getattr(route, "_display_path", route._path)
    # The direction decoration is visible ink, but never enters the route's
    # electrical or hit-test path. Stroke both subpaths together; no extra
    # native boolean union is needed for this small chevron.
    scene_path = route.mapToScene(display_path)
    marker = route.direction_marker() if hasattr(route, "direction_marker") else None
    if marker is not None:
        scene_path.moveTo(route.mapToScene(QPointF(*marker.points[0])))
        for point in marker.points[1:]:
            scene_path.lineTo(route.mapToScene(QPointF(*point)))
    ink = stroker.createStroke(scene_path)
    for point in getattr(route, "_bridge_nodes", ()):
        marker = QPainterPath()
        marker.addEllipse(route.mapToScene(point), 2.6, 2.6)
        ink = ink.united(marker)
    return ink


def layout_labels(items, routes):
    """Position eligible text items and return all remaining conflicts."""
    objects = sorted(items, key=lambda item: (item.body_scene_rect().center().y(), item.body_scene_rect().center().x(), item.representation_id.value))
    obstacles = _Obstacles()
    bodies = {}
    for item in objects:
        body = object_label_obstacle_rect(item)
        bodies[item.representation_id] = body
        if not getattr(item, "_label_route_owner", False):
            obstacles.add(item.representation_id.value, "body", body)
    for route in routes:
        path = route_label_obstacle_path(route)
        obstacles.add(route.route_id.value, "wire", path.boundingRect(), path)

    eligible = [item for item in objects if item._label_requested_visible]
    manual = [item for item in eligible if label_is_manual(item.representation)]
    automatic = [item for item in eligible if not label_is_manual(item.representation)]
    conflicts = []
    for item in manual + automatic:
        label = item._label
        is_manual = label_is_manual(item.representation)
        rect = label.sceneBoundingRect()
        if not is_manual:
            preferred = item.mapToScene(item._label_preferred_position)
            for candidate in _candidates(bodies[item.representation_id], rect.width(), rect.height(), preferred):
                if not obstacles.has_collision(candidate):
                    label.setPos(item.mapFromScene(candidate.topLeft()))
                    rect = label.sceneBoundingRect()
                    break
        for obstacle_id, kind in obstacles.collisions(rect):
            conflicts.append(LabelConflict(item.representation_id, obstacle_id, kind, is_manual))
        obstacles.add(item.representation_id.value, "label", rect)
    return tuple(conflicts)
