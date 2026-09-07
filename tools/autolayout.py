# -*- coding: utf-8 -*-
"""Автоматическая раскладка однолинейной схемы по канонической модели.

Скрипт строит ``DiagramDocument`` для проекта, у которого он пуст: расставляет
графические представления всего оборудования и всех электрических узлов и
создаёт ортогональные трассы по существующим соединениям. Электрическая модель
при этом не меняется — добавляется только графика.

Алгоритм — «аккуратное дерево»: от источника строится остовное дерево, глубина
задаёт вертикальный уровень, порядок листьев — горизонтальную координату.
Связи, не вошедшие в дерево (секционные выключатели, резервные линии),
рисуются отдельным ярусом и подключаются ортогональными трассами.

Использование:

    python tools/autolayout.py ПУТЬ_К_ПРОЕКТУ.json
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict, deque
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from rza_calc.domain.diagram import (
    DiagramDocument,
    DiagramPage,
    DiagramRoute,
    DiagramRouteId,
    DiagramRouteKind,
    GraphicalRepresentation,
    GraphicalRepresentationId,
    PageId,
    RepresentationTargetKind,
    RouteAnchorKind,
    RouteEndpointAnchor,
    RouteWaypoint,
    RouteWaypointId,
    RouteWaypointSource,
)
from rza_calc.editor.orientation import rotated_port_layout
from rza_calc.editor.symbols import canonical_key, default_size
from rza_calc.io.project import load_project, save_project

# ── Геометрия раскладки ───────────────────────────────────────────────────
LEAF_PITCH = 300.0        # шаг между соседними «листьями» дерева
LEVEL_PITCH = 190.0       # шаг между уровнями напряжения/глубины
TOP_Y = -1500.0           # верхний уровень (шины источника)
LOOP_LIFT = 110.0         # подъём для связей, не вошедших в дерево
BUS_MARGIN = 120.0        # выступ шины за крайние присоединения
MIN_BUS_WIDTH = 180.0
BAND_GAP = 70.0        # промежуток между полосами классов напряжения

#  Условные обозначения для типов, пришедших из старого расчётного формата:
#  библиотека знает их только по ключу в расширениях типа.
LEGACY_SYMBOLS = {
    "legacy.source": "source",
    "legacy.generator": "generator",
    "legacy.line": "line_section",
    "legacy.branch": "line_section",
    "legacy.transformer_2w": "transformer_2w",
    "legacy.transformer_3w": "transformer_3w",
    "legacy.tie": "circuit_breaker",
    "legacy.load": "load",
}


def symbol_key_for(definition) -> str:
    explicit = str((definition.extensions or {}).get("diagram_symbol_key", "") or "")
    if explicit:
        return explicit
    return LEGACY_SYMBOLS.get(definition.behavior_key, "")


def ensure_symbol_keys(model) -> int:
    """Проставить типам ключ обозначения, если библиотека его не выводит.

    Меняются только описания типов оборудования внутри проекта: расчёт,
    соединения и идентификаторы не затрагиваются.
    """
    patched = 0
    for key, definition in list(model._equipment_types.items()):  # noqa: SLF001
        wanted = LEGACY_SYMBOLS.get(definition.behavior_key)
        if not wanted:
            continue
        if (definition.extensions or {}).get("diagram_symbol_key") == wanted:
            continue
        extensions = dict(definition.extensions or {})
        extensions["diagram_symbol_key"] = wanted
        model._equipment_types[key] = replace(definition, extensions=extensions)  # noqa: SLF001
        patched += 1
    return patched


# ── Вспомогательное ───────────────────────────────────────────────────────
def node_kind(node) -> str:
    payload = ((node.extensions or {}).get("legacy_calculation") or {}).get("payload") or {}
    return str(payload.get("kind") or "bus")


def legacy_id(item) -> str:
    return str(((item.extensions or {}).get("legacy_calculation") or {}).get("legacy_id") or "")


def orthogonal(points):
    """Убрать нулевые и склеить сонаправленные сегменты."""
    cleaned = []
    for point in points:
        if cleaned and abs(cleaned[-1][0] - point[0]) < 1e-9 and abs(cleaned[-1][1] - point[1]) < 1e-9:
            continue
        cleaned.append((float(point[0]), float(point[1])))
    merged = [cleaned[0]] if cleaned else []
    for point in cleaned[1:]:
        if len(merged) >= 2:
            (x0, y0), (x1, y1) = merged[-2], merged[-1]
            if (abs(x0 - x1) < 1e-9 and abs(x1 - point[0]) < 1e-9) or (
                abs(y0 - y1) < 1e-9 and abs(y1 - point[1]) < 1e-9
            ):
                merged[-1] = point
                continue
        merged.append(point)
    for first, second in zip(merged, merged[1:]):
        if abs(first[0] - second[0]) > 1e-9 and abs(first[1] - second[1]) > 1e-9:
            raise AssertionError(f"Неортогональный сегмент: {first} → {second}")
    return merged


def route_from_port(anchor, target):
    """Ортогональная трасса от вывода аппарата к точке узла."""
    px, py, direction = anchor.x, anchor.y, str(anchor.direction)
    tx, ty = target
    if direction in ("left", "right"):
        points = [(px, py), (tx, py), (tx, ty)]
    else:
        points = [(px, py), (px, ty), (tx, ty)]
    result = orthogonal(points)
    if len(result) < 2:
        result = [(px, py), (px, ty if abs(ty - py) > 1e-9 else py + 40.0)]
    return result


def waypoints(points):
    return tuple(
        RouteWaypoint(RouteWaypointId.new(), x, y, RouteWaypointSource.AUTOMATIC)
        for x, y in points
    )


# ── Основная раскладка ────────────────────────────────────────────────────
def is_physical_line(model, eq_id, behavior_key) -> bool:
    """ВЛ или КЛ — то же условие, по которому редактор рисует стрелку.

    Совпадает с `_is_directional_line` в gui/editor_scene.py: важно, чтобы
    линия, нарисованная трассой, получала направление, а не оставалась без
    него из-за расхождения двух списков.
    """
    if behavior_key != "legacy.line":
        return False
    equipment = model.equipment[eq_id]
    legacy = equipment.extensions.get("legacy_calculation") or {}
    payload = (model.effective_equipment_properties(eq_id) or {}).get("legacy_payload") or {}
    return (legacy.get("legacy_class") == "LineBranch"
            and payload.get("line_type") in {"overhead", "cable"})


def build_layout(project, *, lines_as_routes: bool = False) -> DiagramDocument:
    """Разложить схему. При ``lines_as_routes`` ВЛ и КЛ становятся трассами.

    Обычный режим даёт каждой ветви свой прямоугольник — так собран
    «Энергорайон», и его сохранённая графика этим режимом воспроизводится.
    Режим трасс рисует линию так, как её теперь создаёт протяжка: провод от
    вывода до вывода со стрелкой направления, без отдельного объекта в рамке.
    """
    model = project.electrical_model
    ensure_symbol_keys(model)

    ports_by_equipment = {eq.id: tuple(eq.port_ids) for eq in model.equipment.values()}
    node_of_port = {c.port_id: c.electrical_node_id for c in model.connections.values()}
    definition_of = {
        eq.id: model.equipment_types.get((eq.type_id, eq.type_version))
        for eq in model.equipment.values()
    }

    # Оборудование по числу присоединённых узлов
    nodes_of_equipment: dict[object, tuple] = {}
    for eq_id, port_ids in ports_by_equipment.items():
        attached = tuple(
            node_of_port[p] for p in port_ids if p in node_of_port
        )
        nodes_of_equipment[eq_id] = attached

    incident: dict[object, list] = defaultdict(list)
    for eq_id, attached in nodes_of_equipment.items():
        for node_id in set(attached):
            incident[node_id].append(eq_id)

    behavior = {
        eq_id: (definition_of[eq_id].behavior_key if definition_of[eq_id] else "")
        for eq_id in ports_by_equipment
    }
    is_terminal = {
        eq_id: len(set(nodes_of_equipment[eq_id])) < 2 for eq_id in ports_by_equipment
    }

    # ── Корень: узел, к которому подключён источник ───────────────────────
    roots = [
        nodes_of_equipment[eq_id][0]
        for eq_id, key in behavior.items()
        if key in ("source", "legacy.source", "generator", "legacy.generator")
        and nodes_of_equipment[eq_id]
    ]
    if not roots:
        roots = [next(iter(model.electrical_nodes))]

    # ── Остовное дерево обходом в ширину ──────────────────────────────────
    depth: dict[object, int] = {}
    parent_equipment: dict[object, object] = {}
    children: dict[object, list] = defaultdict(list)
    tree_equipment: set = set()
    queue = deque()
    for root in roots:
        if root in depth:
            continue
        depth[root] = 0
        queue.append(root)
    while queue:
        node_id = queue.popleft()
        for eq_id in incident[node_id]:
            if is_terminal[eq_id] or eq_id in tree_equipment:
                continue
            others = [n for n in set(nodes_of_equipment[eq_id]) if n != node_id]
            fresh = [n for n in others if n not in depth]
            if not fresh:
                continue
            tree_equipment.add(eq_id)
            parent_equipment[eq_id] = node_id
            for other in fresh:
                depth[other] = depth[node_id] + 1
                children[node_id].append((eq_id, other))
                queue.append(other)
    for node_id in model.electrical_nodes:
        depth.setdefault(node_id, 0)

    # Оконечное оборудование (нагрузки, источники) — листья своего узла
    terminals_of_node: dict[object, list] = defaultdict(list)
    for eq_id, terminal in is_terminal.items():
        if terminal and nodes_of_equipment[eq_id]:
            terminals_of_node[nodes_of_equipment[eq_id][0]].append(eq_id)

    # ── Горизонтальная координата: порядок листьев ────────────────────────
    x_of_node: dict[object, float] = {}
    counter = [0.0]

    def assign(node_id) -> float:
        kids = children.get(node_id, ())
        if not kids:
            x = counter[0] * LEAF_PITCH
            counter[0] += 1.0
            x_of_node[node_id] = x
            return x
        spots = [assign(child) for _, child in kids]
        x = (min(spots) + max(spots)) / 2.0
        x_of_node[node_id] = x
        return x

    for root in roots:
        assign(root)
    for node_id in model.electrical_nodes:
        if node_id not in x_of_node:
            x_of_node[node_id] = counter[0] * LEAF_PITCH
            counter[0] += 1.0

    # ── Вертикальная координата: полосы по классам напряжения ─────────────
    #  Внутри полосы уровень задаёт глубина, поэтому 35 кВ никогда не
    #  оказывается на одной строке с 10 кВ, даже если до них одинаковое
    #  число элементов от источника.
    def class_value(node) -> float:
        class_id = getattr(node, "declared_voltage_class_id", None)
        entry = model.voltage_classes.get(class_id) if class_id else None
        return float(getattr(entry, "nominal_voltage_v", 0.0) or 0.0)

    by_class: dict[float, list] = defaultdict(list)
    for node_id, node in model.electrical_nodes.items():
        by_class[class_value(node)].append(node_id)

    y_of_node: dict[object, float] = {}
    cursor = TOP_Y
    for value in sorted(by_class, reverse=True):
        members = by_class[value]
        local_min = min(depth[n] for n in members)
        local_max = max(depth[n] for n in members)
        for node_id in members:
            y_of_node[node_id] = cursor + (depth[node_id] - local_min) * LEVEL_PITCH
        cursor += (local_max - local_min + 1) * LEVEL_PITCH + BAND_GAP

    # ── Ширина шин ────────────────────────────────────────────────────────
    span: dict[object, float] = {}
    for node_id in model.electrical_nodes:
        xs = [x_of_node[node_id]]
        for _, child in children.get(node_id, ()):
            xs.append(x_of_node[child])
        for eq_id in incident[node_id]:
            for other in set(nodes_of_equipment[eq_id]):
                if other != node_id and depth.get(other, 0) <= depth[node_id]:
                    continue
        width = (max(xs) - min(xs)) + BUS_MARGIN
        span[node_id] = max(MIN_BUS_WIDTH, width)

    page = DiagramPage(PageId.new(), "Основная схема", order=0)
    representations: dict[object, GraphicalRepresentation] = {}
    rep_of_node: dict[object, GraphicalRepresentation] = {}
    rep_of_equipment: dict[object, GraphicalRepresentation] = {}
    geometry: dict[object, dict] = {}

    line_equipment = {
        eq_id for eq_id in model.equipment
        if lines_as_routes and is_physical_line(model, eq_id, behavior.get(eq_id, ""))
    }

    #  Транзитные точки: узел ровно с двумя соединениями и без собственного
    #  имени на схеме — это место стыка, а не объект. Раньше между
    #  выключателем и нагрузкой рисовались три отрезка и две жирные точки;
    #  на однолинейке там один провод. В МОДЕЛИ узел остаётся: без него
    #  ветвям не к чему присоединяться, и точка КЗ «конец фидера» никуда не
    #  девается. Скрывается только его изображение.
    connections_of_node: dict[object, int] = defaultdict(int)
    for connection in model.connections.values():
        connections_of_node[connection.electrical_node_id] += 1
    pass_through = {
        node_id for node_id, node in model.electrical_nodes.items()
        if lines_as_routes and node_kind(node) == "point"
        and connections_of_node.get(node_id, 0) == 2
    }

    # ── Представления узлов ───────────────────────────────────────────────
    for node_id, node in model.electrical_nodes.items():
        if node_id in pass_through:
            continue          # транзит рисуется сквозным проводом
        bus = node_kind(node) != "point"
        key = "busbar" if bus else "connection_point"
        width, height = default_size(key)
        if bus:
            width = span[node_id]
        rep = GraphicalRepresentation(
            GraphicalRepresentationId.new(),
            page.id,
            RepresentationTargetKind.ELECTRICAL_NODE,
            electrical_node_id=node_id,
            x=x_of_node[node_id],
            y=y_of_node[node_id],
            symbol_key=key,
            label=node.name,
            extensions={
                "stage3_graphics": {
                    "width": width,
                    "height": height,
                    "label_x": -width / 2.0,
                    "label_y": -height / 2.0 - 20.0 if bus else 14.0,
                    "label_visible": bus,
                    "line_width": 2.4 if bus else 1.6,
                }
            },
        )
        representations[rep.id] = rep
        rep_of_node[node_id] = rep
        geometry[rep.id] = {"width": width, "height": height}

    # ── Представления оборудования ────────────────────────────────────────
    for eq_id, equipment in model.equipment.items():
        if eq_id in line_equipment:
            continue          # линия рисуется трассой, а не объектом в рамке
        definition = definition_of[eq_id]
        key = canonical_key(symbol_key_for(definition) if definition else "",
                            behavior.get(eq_id, ""))
        width, height = default_size(key)
        attached = tuple(dict.fromkeys(nodes_of_equipment[eq_id]))
        if is_terminal[eq_id]:
            host = attached[0] if attached else roots[0]
            if behavior.get(eq_id, "").endswith("source") or behavior.get(eq_id, "").endswith("generator"):
                x, y, rotation = x_of_node[host], y_of_node[host] - 120.0, 0.0
            else:                                   # нагрузка — под своим узлом
                x, y, rotation = x_of_node[host], y_of_node[host] + 110.0, 0.0
        elif eq_id in tree_equipment:
            upper = parent_equipment[eq_id]
            lower = [n for n in attached if n != upper]
            x = sum(x_of_node[n] for n in lower) / len(lower)
            y = (y_of_node[upper] + min(y_of_node[n] for n in lower)) / 2.0
            rotation = 90.0
        else:                                        # связь вне дерева
            xs = [x_of_node[n] for n in attached]
            ys = [y_of_node[n] for n in attached]
            same_level = max(ys) - min(ys) < 1e-9
            x = (min(xs) + max(xs)) / 2.0
            y = min(ys) - (0.0 if same_level else LOOP_LIFT)
            rotation = 0.0
        rep = GraphicalRepresentation(
            GraphicalRepresentationId.new(),
            page.id,
            RepresentationTargetKind.EQUIPMENT,
            equipment_id=eq_id,
            x=x,
            y=y,
            rotation_deg=rotation,
            symbol_key="",
            label=equipment.name,
            extensions={
                "stage3_graphics": {
                    "width": width,
                    "height": height,
                    "label_x": width / 2.0 + 10.0,
                    "label_y": -height / 2.0,
                    "label_visible": True,
                    "line_width": 2.0,
                }
            },
        )
        representations[rep.id] = rep
        rep_of_equipment[eq_id] = rep
        geometry[rep.id] = {"width": width, "height": height}

    # ── Трассы по существующим соединениям ────────────────────────────────
    routes: dict[object, DiagramRoute] = {}
    taps: dict[object, list[float]] = defaultdict(list)
    for connection in model.connections.values():
        port = model.ports.get(connection.port_id)
        if port is None:
            continue
        if port.equipment_id in line_equipment:
            continue          # трасса линии строится ниже, целиком
        if connection.electrical_node_id in pass_through:
            continue          # провод пройдёт сквозь эту точку одной линией
        eq_rep = rep_of_equipment.get(port.equipment_id)
        node_rep = rep_of_node.get(connection.electrical_node_id)
        if eq_rep is None or node_rep is None:
            continue
        equipment = model.equipment[port.equipment_id]
        definition = definition_of[port.equipment_id]
        size = geometry[eq_rep.id]
        anchor = None
        for candidate in rotated_port_layout(
            equipment,
            definition,
            width=size["width"],
            height=size["height"],
            rotation=int(eq_rep.rotation_deg),
            center_x=eq_rep.x,
            center_y=eq_rep.y,
        ):
            if candidate.port_id == connection.port_id:
                anchor = candidate
                break
        if anchor is None:
            continue
        node_size = geometry[node_rep.id]
        half = node_size["width"] / 2.0 - 16.0
        target_x = min(max(anchor.x, node_rep.x - half), node_rep.x + half)
        if node_size["width"] <= 40.0:               # точка подключения
            target_x = node_rep.x
        target = (target_x, node_rep.y)
        points = route_from_port(anchor, target)
        taps[connection.electrical_node_id].append(target_x)
        route = DiagramRoute(
            DiagramRouteId.new(),
            page.id,
            DiagramRouteKind.NODE_CONNECTION,
            RouteEndpointAnchor(
                RouteAnchorKind.EQUIPMENT_PORT,
                eq_rep.id,
                connection.electrical_node_id,
                target_port_id=connection.port_id,
            ),
            RouteEndpointAnchor(
                RouteAnchorKind.BUS if node_size["width"] > 40.0 else RouteAnchorKind.ELECTRICAL_NODE,
                node_rep.id,
                connection.electrical_node_id,
            ),
            electrical_node_id=connection.electrical_node_id,
            waypoints=waypoints(points),
        )
        routes[route.id] = route

    # ── Трассы физических линий: провод от вывода до вывода ───────────────
    for eq_id in line_equipment:
        connections = [c for c in model.connections.values()
                       if model.ports[c.port_id].equipment_id == eq_id]
        by_role = {model.port_definition(c.port_id).role: c for c in connections}
        first, second = by_role.get("from"), by_role.get("to")
        if first is None or second is None:
            continue          # неполная линия трассой не рисуется
        ends = []
        for connection in (first, second):
            node_id = connection.electrical_node_id
            if node_id in pass_through:
                #  Транзит: провод продолжается до вывода соседнего аппарата,
                #  а не обрывается на невидимой точке.
                peer = next((c for c in model.connections.values()
                             if c.electrical_node_id == node_id
                             and c.port_id != connection.port_id), None)
                peer_rep = (rep_of_equipment.get(model.ports[peer.port_id].equipment_id)
                            if peer is not None else None)
                if peer_rep is None:
                    ends.append(None)
                    continue
                ends.append(("port", peer_rep, peer))
            else:
                node_rep = rep_of_node.get(node_id)
                ends.append(("node", node_rep, connection) if node_rep is not None else None)
        if any(end is None or end[1] is None for end in ends):
            continue
        start_rep, end_rep = ends[0][1], ends[1][1]
        #  Порядок концов задаёт направление стрелки: от «from» к «to», то же
        #  правило, что у линии, созданной протяжкой.
        points = orthogonal([(start_rep.x, start_rep.y), (end_rep.x, end_rep.y)])
        anchors = []
        for index, (mode_kind, rep, connection) in enumerate(ends):
            size = geometry[rep.id]
            own = first if index == 0 else second
            if mode_kind == "port":
                #  Конец провода — вывод соседнего аппарата: анкер указывает на
                #  его представление и порт, поэтому провод приходит ровно в
                #  нарисованный вывод, а не в пустоту, где была точка.
                kind = RouteAnchorKind.EQUIPMENT_PORT
                anchors.append(RouteEndpointAnchor(
                    kind, rep.id, own.electrical_node_id,
                    branch_port_id=own.port_id,
                    target_port_id=connection.port_id,
                ))
                continue
            kind = (RouteAnchorKind.BUS if size["width"] > 40.0
                    else RouteAnchorKind.ELECTRICAL_NODE)
            anchors.append(RouteEndpointAnchor(
                kind, rep.id, own.electrical_node_id,
                branch_port_id=own.port_id,
            ))
            if size["width"] > 40.0:
                taps[own.electrical_node_id].append(
                    start_rep.x if index == 0 else end_rep.x)
        route = DiagramRoute(
            DiagramRouteId.new(), page.id, DiagramRouteKind.EQUIPMENT_BRANCH,
            anchors[0], anchors[1], equipment_id=eq_id,
            waypoints=waypoints(points),
        )
        routes[route.id] = route

    # Расширить шины до фактических точек подключения
    for node_id, xs in taps.items():
        rep = rep_of_node[node_id]
        size = geometry[rep.id]
        if size["width"] <= 40.0:
            continue
        left, right = min(xs + [rep.x]), max(xs + [rep.x])
        width = max(MIN_BUS_WIDTH, (right - left) + 2 * 32.0)
        center = (left + right) / 2.0
        graphics = dict(rep.extensions["stage3_graphics"])
        graphics["width"] = width
        graphics["label_x"] = -width / 2.0
        updated = replace(
            rep, x=center, extensions={"stage3_graphics": graphics}
        )
        representations[rep.id] = updated
        rep_of_node[node_id] = updated
        geometry[rep.id]["width"] = width

    return DiagramDocument(
        project.diagram.id,
        project.diagram.name or "Однолинейная схема",
        pages={page.id: page},
        representations=representations,
        routes=routes,
        revision=project.diagram.revision + 1,
        extensions={
            "stage3_workspace": {
                "mode": "edit",
                "grid_visible": True,
                "snap_enabled": True,
                "grid_size": 20.0,
                "zoom": 0.25,
                "view_x": 0.0,
                "view_y": 0.0,
                "open_panels": ["project", "properties", "issues"],
                "developer_diagnostics": False,
                "confirm_switching": True,
                "active_page_id": page.id.value,
            }
        },
    )


def main(path: str) -> None:
    target = Path(path)
    project = load_project(target)
    project.diagram = build_layout(project)
    problems = project.diagram.validate_targets(project.electrical_model)
    if problems:
        raise SystemExit("Ошибки ссылок схемы:\n- " + "\n- ".join(problems))
    save_project(target, project)
    print(
        f"схема: страниц 1, представлений {len(project.diagram.representations)}, "
        f"трасс {len(project.diagram.routes)}"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "path",
        help="Проект для раскладки; относительный путь — от текущей папки",
    )
    main(parser.parse_args().path)
