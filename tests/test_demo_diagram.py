# -*- coding: utf-8 -*-
"""Инварианты однолинейной схемы демонстрационного проекта «Энергорайон».

Эти проверки заменяют снятые тесты прежней автосхемы (`test_svg_scheme.py`).
Прежние проверяли HTML второй графической системы; после её удаления те же
свойства — ортогональность трасс, попадание провода в вывод аппарата,
осмысленное условное обозначение у каждого объекта — проверяются там, где они
теперь живут: в сохранённом `DiagramDocument` проекта.

Тесты не требуют Qt: вся геометрия портов и обозначений лежит в
Qt-независимых модулях `rza_calc.editor`.
"""
from pathlib import Path

from rza_calc.domain.diagram import DiagramRouteKind, RepresentationTargetKind
from rza_calc.editor.orientation import rotated_port_layout
from rza_calc.editor.symbols import canonical_key
from rza_calc.io.project import load_project

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "rza_calc" / "examples" / "energoraion.json"

EPS = 1e-6


def demo():
    return load_project(DEMO)


def graphics(representation):
    return dict(representation.extensions.get("stage3_graphics") or {})


def test_demo_project_has_a_placed_scheme():
    project = demo()
    diagram = project.diagram
    assert len(diagram.pages) == 1, "демо-схема размещается на одной странице"
    assert diagram.representations, "схема должна открываться уже размещённой"
    assert diagram.routes, "схема без трасс не читается"


def test_every_object_of_the_model_is_placed_exactly_once():
    project = demo()
    model, diagram = project.electrical_model, project.diagram
    equipment_ids = [
        r.equipment_id for r in diagram.representations.values()
        if r.target_kind is RepresentationTargetKind.EQUIPMENT
    ]
    node_ids = [
        r.electrical_node_id for r in diagram.representations.values()
        if r.target_kind is RepresentationTargetKind.ELECTRICAL_NODE
    ]
    assert len(equipment_ids) == len(set(equipment_ids))
    assert len(node_ids) == len(set(node_ids))
    assert set(equipment_ids) == set(model.equipment)
    assert set(node_ids) == set(model.electrical_nodes)


def test_every_electrical_connection_is_drawn_by_one_route():
    project = demo()
    model, diagram = project.electrical_model, project.diagram
    drawn = [
        route.start_anchor.target_port_id
        for route in diagram.routes.values()
        if route.start_anchor.target_port_id is not None
    ]
    assert len(drawn) == len(set(drawn)), "у порта не может быть двух трасс"
    assert set(drawn) == {c.port_id for c in model.connections.values()}


def test_routes_are_orthogonal_without_zero_length_segments():
    project = demo()
    for route in project.diagram.routes.values():
        points = [(w.x, w.y) for w in route.waypoints]
        assert len(points) >= 2
        for first, second in zip(points, points[1:]):
            dx, dy = abs(first[0] - second[0]), abs(first[1] - second[1])
            assert dx > EPS or dy > EPS, f"нулевой сегмент в трассе {route.id}"
            assert dx <= EPS or dy <= EPS, f"косой сегмент в трассе {route.id}"


def test_wire_starts_exactly_at_the_drawn_terminal():
    """Провод обязан начинаться в выводе символа, а не в углу габарита.

    Это главный инвариант библиотеки обозначений: точка подключения считается
    из той же геометрии, которую рисует сцена, поэтому расхождение здесь
    означало бы «провод в воздухе» на экране.
    """
    project = demo()
    model, diagram = project.electrical_model, project.diagram
    checked = 0
    for route in diagram.routes.values():
        anchor = route.start_anchor
        if anchor.target_port_id is None:
            continue
        representation = diagram.representations[anchor.representation_id]
        equipment = model.equipment[representation.equipment_id]
        definition = model.equipment_types[(equipment.type_id, equipment.type_version)]
        size = graphics(representation)
        port = None
        for candidate in rotated_port_layout(
            equipment,
            definition,
            width=float(size["width"]),
            height=float(size["height"]),
            rotation=int(representation.rotation_deg),
            center_x=representation.x,
            center_y=representation.y,
        ):
            if candidate.port_id == anchor.target_port_id:
                port = candidate
                break
        assert port is not None, f"вывод {anchor.target_port_id} не найден"
        start = route.waypoints[0]
        assert abs(start.x - port.x) < 1e-6 and abs(start.y - port.y) < 1e-6, (
            f"трасса {route.id} начинается не в выводе аппарата"
        )
        checked += 1
    assert checked == len(diagram.routes)


def test_route_ends_on_its_node_symbol():
    """Второй конец трассы обязан попасть в символ своего узла."""
    project = demo()
    diagram = project.diagram
    for route in diagram.routes.values():
        representation = diagram.representations[route.end_anchor.representation_id]
        size = graphics(representation)
        half_w = float(size["width"]) / 2.0
        half_h = float(size["height"]) / 2.0
        end = route.waypoints[-1]
        assert abs(end.y - representation.y) <= half_h + EPS, (
            f"трасса {route.id} не доходит до шины по вертикали"
        )
        assert abs(end.x - representation.x) <= half_w + EPS, (
            f"трасса {route.id} выходит за пределы шины"
        )


def test_every_equipment_type_resolves_to_a_real_symbol():
    """Ни один объект схемы не должен рисоваться безымянным прямоугольником."""
    project = demo()
    model = project.electrical_model
    used = {(e.type_id, e.type_version) for e in model.equipment.values()}
    for key in used:
        definition = model.equipment_types[key]
        symbol = str((definition.extensions or {}).get("diagram_symbol_key", ""))
        resolved = canonical_key(symbol, definition.behavior_key)
        assert resolved != "generic", (
            f"тип «{definition.display_name}» не имеет условного обозначения"
        )


def test_node_connection_routes_keep_one_electrical_node():
    project = demo()
    for route in project.diagram.routes.values():
        assert route.kind is DiagramRouteKind.NODE_CONNECTION
        assert route.start_anchor.electrical_node_id == route.electrical_node_id
        assert route.end_anchor.electrical_node_id == route.electrical_node_id


def test_scheme_references_survive_validation():
    project = demo()
    assert project.diagram.validate_targets(project.electrical_model) == ()


def test_demo_network_has_the_declared_composition():
    """Состав демо-сети: четыре ПС, 24 отходящих фидера, шесть режимов."""
    project = demo()
    net = project.network
    assert len(net.modes) == 6
    feeders = [
        branch for branch in net.branches.values()
        if branch.kind == "line" and branch.name.startswith("Ф-")
        and "продолжение" not in branch.name and "реклоузер" not in branch.name
    ]
    assert len(feeders) == 24, f"ожидалось 24 фидера, найдено {len(feeders)}"
    for station in ("Центральная", "Северная", "Западная", "Южная"):
        assert any(station in node.name for node in net.nodes.values()), station
    assert len(net.loads) == 24


def test_demo_network_calculates_without_unresolved_settings():
    """Демо-проект обязан считаться: «не определено» в нём быть не должно."""
    from rza_calc.core.engine import run

    project = demo()
    result = run(project.network, project.methodology)
    unresolved = [
        (row.branch_name, row.kind)
        for row in result.all_results()
        if str(row.status) == "unresolved"
    ]
    assert unresolved == [], f"незавершённые расчёты: {unresolved}"
