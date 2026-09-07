# -*- coding: utf-8 -*-
"""Временное состояние интерактивного построения соединения.

Состояние существует только пока пользователь ведёт курсор. Оно не содержит
ссылки на проект и не может создать узел, порт, трассу или запись undo.
Фиксируемый :class:`ConnectionDraft` передаётся одной команде контроллера.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Iterable

from .orthogonal_routing import (
    RouteDirection,
    RouteVertex,
    RouteVertexSource,
    RoutingObstacle,
    RoutingRequest,
    build_orthogonal_route,
)


class ConnectionToolMode(StrEnum):
    IDLE = "idle"
    CREATE = "create"
    RECONNECT = "reconnect"


class ConnectionTargetKind(StrEnum):
    FREE = "free"
    EQUIPMENT_PORT = "equipment_port"
    ELECTRICAL_NODE = "electrical_node"
    BUS = "bus"
    NODE_CONNECTION = "node_connection"
    PHYSICAL_LINE = "physical_line"


class ConnectionTargetFeedback(StrEnum):
    NEUTRAL = "neutral"
    COMPATIBLE = "compatible"
    INCOMPATIBLE = "incompatible"


@dataclass(frozen=True, slots=True)
class ConnectionTarget:
    kind: ConnectionTargetKind
    x: float
    y: float
    target_id: str = ""
    representation_id: str = ""
    direction: RouteDirection | None = None
    feedback: ConnectionTargetFeedback = ConnectionTargetFeedback.NEUTRAL
    message: str = ""
    anchor_key: str = ""
    route_id: str = ""
    route_fraction: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ConnectionTargetKind):
            object.__setattr__(self, "kind", ConnectionTargetKind(self.kind))
        if not isinstance(self.feedback, ConnectionTargetFeedback):
            object.__setattr__(
                self, "feedback", ConnectionTargetFeedback(self.feedback)
            )
        if self.route_fraction is not None and not 0.0 <= float(self.route_fraction) <= 1.0:
            raise ValueError("Доля положения на графической трассе должна быть от 0 до 1.")


@dataclass(frozen=True, slots=True)
class ConnectionDraft:
    source_port_id: str
    source_representation_id: str
    mode: ConnectionToolMode
    target: ConnectionTarget
    vertices: tuple[RouteVertex, ...]
    manual_vertices: tuple[RouteVertex, ...]
    source_anchor_key: str = ""
    #  Жест, начатый с маркера конца уже существующей ВЛ/КЛ. Только он является
    #  переносом конца линии; протяжка от вывода аппарата им не является, даже
    #  если этот вывод уже к чему-то подключён.
    from_route_endpoint: bool = False
    source_target: ConnectionTarget | None = None


@dataclass(frozen=True, slots=True)
class PhysicalLineDraft:
    name: str
    line_kind: str
    source: ConnectionTarget
    target: ConnectionTarget
    vertices: tuple[RouteVertex, ...]
    manual_vertices: tuple[RouteVertex, ...]


@dataclass(slots=True)
class PhysicalLineToolState:
    """Временный двухточечный инструмент ВЛ/КЛ без электрических мутаций."""

    name: str = ""
    line_kind: str = ""
    source: ConnectionTarget | None = None
    target: ConnectionTarget | None = None
    source_direction: RouteDirection | None = None
    cursor_vertex: RouteVertex | None = None
    manual_vertices: list[RouteVertex] = field(default_factory=list)
    preview_vertices: tuple[RouteVertex, ...] = ()

    @property
    def active(self) -> bool:
        return self.source is not None

    def begin(
        self,
        *,
        name: str,
        line_kind: str,
        source: ConnectionTarget,
    ) -> None:
        if not line_kind:
            raise ValueError("Не задан тип физической линии.")
        self.cancel()
        self.name = str(name).strip()
        self.line_kind = str(line_kind)
        self.source = source
        self.source_direction = source.direction
        self.cursor_vertex = RouteVertex(source.x, source.y)
        self.preview_vertices = (self.cursor_vertex,)

    def update(
        self,
        x: float,
        y: float,
        *,
        target: ConnectionTarget | None = None,
        obstacles: Iterable[RoutingObstacle] = (),
    ) -> tuple[RouteVertex, ...]:
        if self.source is None:
            return ()
        self.cursor_vertex = RouteVertex(x, y)
        self.target = target
        end = RouteVertex(target.x, target.y) if target is not None else self.cursor_vertex
        self.preview_vertices = build_orthogonal_route(
            RoutingRequest(
                RouteVertex(self.source.x, self.source.y),
                end,
                self.source_direction,
                target.direction if target is not None else None,
                tuple(self.manual_vertices),
                tuple(obstacles),
            )
        )
        return self.preview_vertices

    def add_manual_vertex(self, x: float, y: float) -> None:
        if self.source is None:
            return
        vertex = RouteVertex(x, y, RouteVertexSource.USER, pinned=True)
        if self.manual_vertices and self.manual_vertices[-1].point == vertex.point:
            return
        self.manual_vertices.append(vertex)
        cursor = self.cursor_vertex or vertex
        self.update(cursor.x, cursor.y, target=self.target)

    def remove_last_manual_vertex(self) -> bool:
        if not self.manual_vertices:
            return False
        self.manual_vertices.pop()
        if self.cursor_vertex is not None:
            self.update(
                self.cursor_vertex.x,
                self.cursor_vertex.y,
                target=self.target,
            )
        return True

    def draft(self, *, free_target: bool = False) -> PhysicalLineDraft:
        if self.source is None or self.cursor_vertex is None:
            raise RuntimeError("Построение физической линии не начато.")
        target = self.target
        if free_target:
            target = ConnectionTarget(
                ConnectionTargetKind.FREE,
                self.cursor_vertex.x,
                self.cursor_vertex.y,
                feedback=ConnectionTargetFeedback.COMPATIBLE,
                message="Создать конечный электрический узел",
            )
        if target is None:
            raise RuntimeError("Не выбрана цель физической линии.")
        if target.feedback is ConnectionTargetFeedback.INCOMPATIBLE:
            raise RuntimeError(target.message or "Выбранная цель несовместима.")
        return PhysicalLineDraft(
            self.name,
            self.line_kind,
            self.source,
            target,
            tuple(self.preview_vertices),
            tuple(self.manual_vertices),
        )

    def cancel(self) -> None:
        self.name = ""
        self.line_kind = ""
        self.source = None
        self.target = None
        self.source_direction = None
        self.cursor_vertex = None
        self.manual_vertices.clear()
        self.preview_vertices = ()


@dataclass(slots=True)
class ConnectionToolState:
    mode: ConnectionToolMode = ConnectionToolMode.IDLE
    source_port_id: str = ""
    source_representation_id: str = ""
    source_anchor_key: str = ""
    from_route_endpoint: bool = False
    source_target: ConnectionTarget | None = None
    source_vertex: RouteVertex | None = None
    source_direction: RouteDirection | None = None
    cursor_vertex: RouteVertex | None = None
    target: ConnectionTarget | None = None
    manual_vertices: list[RouteVertex] = field(default_factory=list)
    preview_vertices: tuple[RouteVertex, ...] = ()

    @property
    def active(self) -> bool:
        return self.mode is not ConnectionToolMode.IDLE

    def begin_from_target(self, source: ConnectionTarget) -> None:
        if source.kind not in {ConnectionTargetKind.BUS,
                               ConnectionTargetKind.ELECTRICAL_NODE,
                               ConnectionTargetKind.NODE_CONNECTION}:
            raise ValueError("Начало соединения должно быть узлом или проводником.")
        self.cancel()
        self.mode = ConnectionToolMode.CREATE
        self.source_target = source
        self.source_representation_id = source.representation_id
        self.source_anchor_key = source.anchor_key
        self.source_vertex = RouteVertex(source.x, source.y)
        self.source_direction = source.direction
        self.cursor_vertex = self.source_vertex
        self.preview_vertices = (self.source_vertex,)

    def begin(
        self,
        *,
        source_port_id: str,
        source_representation_id: str,
        x: float,
        y: float,
        direction: RouteDirection | None,
        anchor_key: str = "",
        reconnect: bool = False,
        from_route_endpoint: bool = False,
    ) -> None:
        if not source_port_id or not source_representation_id:
            raise ValueError(
                "Для начала соединения нужны идентификаторы порта и представления."
            )
        self.cancel()
        self.mode = (
            ConnectionToolMode.RECONNECT if reconnect else ConnectionToolMode.CREATE
        )
        self.source_port_id = str(source_port_id)
        self.source_representation_id = str(source_representation_id)
        self.source_anchor_key = str(anchor_key)
        self.from_route_endpoint = bool(from_route_endpoint)
        self.source_vertex = RouteVertex(x, y)
        self.source_direction = direction
        self.cursor_vertex = self.source_vertex
        self.preview_vertices = (self.source_vertex,)

    def update(
        self,
        x: float,
        y: float,
        *,
        target: ConnectionTarget | None = None,
        obstacles: Iterable[RoutingObstacle] = (),
    ) -> tuple[RouteVertex, ...]:
        if not self.active or self.source_vertex is None:
            return ()
        self.cursor_vertex = RouteVertex(x, y)
        self.target = target
        end = (
            RouteVertex(target.x, target.y)
            if target is not None
            else self.cursor_vertex
        )
        self.preview_vertices = build_orthogonal_route(
            RoutingRequest(
                self.source_vertex,
                end,
                self.source_direction,
                target.direction if target is not None else None,
                tuple(self.manual_vertices),
                tuple(obstacles),
            )
        )
        return self.preview_vertices

    def add_manual_vertex(self, x: float | None = None, y: float | None = None) -> None:
        if not self.active:
            return
        if x is None or y is None:
            if self.cursor_vertex is None:
                return
            x, y = self.cursor_vertex.x, self.cursor_vertex.y
        vertex = RouteVertex(
            x,
            y,
            RouteVertexSource.USER,
            pinned=True,
        )
        if self.manual_vertices and self.manual_vertices[-1].point == vertex.point:
            return
        self.manual_vertices.append(vertex)
        self.update(
            self.cursor_vertex.x if self.cursor_vertex else vertex.x,
            self.cursor_vertex.y if self.cursor_vertex else vertex.y,
            target=self.target,
        )

    def remove_last_manual_vertex(self) -> bool:
        if not self.manual_vertices:
            return False
        self.manual_vertices.pop()
        if self.cursor_vertex is not None:
            self.update(
                self.cursor_vertex.x,
                self.cursor_vertex.y,
                target=self.target,
            )
        return True

    def draft(self, *, free_target: bool = False) -> ConnectionDraft:
        if not self.active or self.source_vertex is None or self.cursor_vertex is None:
            raise RuntimeError("Построение соединения не начато.")
        target = self.target
        if free_target:
            target = ConnectionTarget(
                ConnectionTargetKind.FREE,
                self.cursor_vertex.x,
                self.cursor_vertex.y,
                feedback=ConnectionTargetFeedback.COMPATIBLE,
                message="Создать электрический узел",
            )
        if target is None:
            raise RuntimeError("Не выбрана цель соединения.")
        if target.feedback is ConnectionTargetFeedback.INCOMPATIBLE:
            raise RuntimeError(target.message or "Выбранная цель несовместима.")
        return ConnectionDraft(
            self.source_port_id,
            self.source_representation_id,
            self.mode,
            target,
            tuple(self.preview_vertices),
            tuple(self.manual_vertices),
            self.source_anchor_key,
            self.from_route_endpoint,
            self.source_target,
        )

    def cancel(self) -> None:
        self.mode = ConnectionToolMode.IDLE
        self.source_port_id = ""
        self.source_representation_id = ""
        self.source_anchor_key = ""
        self.from_route_endpoint = False
        self.source_target = None
        self.source_vertex = None
        self.source_direction = None
        self.cursor_vertex = None
        self.target = None
        self.manual_vertices.clear()
        self.preview_vertices = ()


__all__ = [
    "ConnectionDraft",
    "ConnectionTarget",
    "ConnectionTargetFeedback",
    "ConnectionTargetKind",
    "ConnectionToolMode",
    "ConnectionToolState",
    "PhysicalLineDraft",
    "PhysicalLineToolState",
]
