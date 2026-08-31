# -*- coding: utf-8 -*-
"""Регрессионные проверки топологии и свежести расчётного view.

Сценарии из аудита 4.4 теперь подтверждают, что cache и GUI используют
фактическое содержимое модели, а не только повторяемый номер ревизии.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from rza_calc.adapters import adapt_to_calculation
from rza_calc.calculation import CalculationProjectionBuilder
from rza_calc.core.methodology import Methodology
from rza_calc.core.model import Network
from rza_calc.domain import ProjectStructure
from rza_calc.domain.catalog_snapshot import ProjectCatalogSnapshots
from rza_calc.domain.diagram import (
    DiagramDocument,
    DiagramDocumentId,
    DiagramPage,
    GraphicalRepresentation,
    GraphicalRepresentationId,
    PageId,
    RepresentationTargetKind,
)
from rza_calc.domain.electrical import (
    ConnectionId,
    ElectricalModel,
    ElectricalNode,
    ElectricalNodeId,
    EquipmentId,
    OperatingState,
    OperatingStateId,
    PortId,
    SwitchPosition,
    VoltageClassId,
)
from rza_calc.editor import ProjectEditorController
from rza_calc.editor.history import ProjectCommandHistory
from rza_calc.gui.view_model import ProjectViewModel
from rza_calc.io.project import FORMAT_VERSION, ProjectData
from rza_calc.topology import (
    BehaviorKind,
    TopologyBehaviorHandler,
    TopologyBehaviorRegistry,
    TopologyEngine,
    TopologyCompatibilityError,
    builtin_topology_handlers,
)


U10 = VoltageClassId("builtin.voltage.ac.10kv")


@dataclass(slots=True)
class _EditableProject:
    electrical_model: ElectricalModel
    diagram: DiagramDocument
    catalog_snapshots: ProjectCatalogSnapshots


def _empty_diagram() -> DiagramDocument:
    return DiagramDocument.create(
        "Аудит",
        (),
        document_id=DiagramDocumentId("diagram.audit.topology"),
    )


def _node(model: ElectricalModel, token: str) -> ElectricalNode:
    node = ElectricalNode(
        ElectricalNodeId(f"node.audit.{token}"),
        token,
        declared_voltage_class_id=U10,
    )
    model.add_node(node)
    return node


def _connect(
    model: ElectricalModel,
    equipment_id: EquipmentId,
    role: str,
    node_id: ElectricalNodeId,
) -> None:
    model.connect_port(
        model.port_by_role(equipment_id, role).id,
        node_id,
        connection_id=ConnectionId(
            f"connection.audit.{equipment_id.value}.{role}.{node_id.value}"
        ),
    )


def _source(
    model: ElectricalModel,
    token: str,
    node: ElectricalNode,
) -> EquipmentId:
    equipment_id = EquipmentId(f"equipment.audit.source.{token}")
    model.create_equipment(
        "builtin.external_grid",
        f"Источник {token}",
        equipment_id=equipment_id,
        port_ids_by_role={"terminal": PortId(f"port.audit.source.{token}")},
        properties={
            "i_kz_max": 10.0,
            "i_kz_min": 6.0,
            "x_r_ratio": 10.0,
        },
        voltage_class_by_group={"main": U10},
    )
    _connect(model, equipment_id, "terminal", node.id)
    return equipment_id


def _two_terminal(
    model: ElectricalModel,
    token: str,
    first: ElectricalNode,
    second: ElectricalNode,
    *,
    type_id: str = "builtin.line",
    roles: tuple[str, str] = ("from", "to"),
    position: SwitchPosition | None = None,
) -> EquipmentId:
    equipment_id = EquipmentId(f"equipment.audit.{token}")
    properties = None
    if type_id == "builtin.recloser":
        properties = {
            "manufacturer": "Audit",
            "model": "R-10",
            "rated_voltage_v": 10_000,
            "rated_current_a": 630,
        }
    model.create_equipment(
        type_id,
        token,
        equipment_id=equipment_id,
        port_ids_by_role={
            roles[0]: PortId(f"port.audit.{token}.{roles[0]}"),
            roles[1]: PortId(f"port.audit.{token}.{roles[1]}"),
        },
        properties=properties,
        voltage_class_by_group={"main": U10},
        normal_position=position,
    )
    _connect(model, equipment_id, roles[0], first.id)
    _connect(model, equipment_id, roles[1], second.id)
    return equipment_id


def _state(
    model: ElectricalModel,
    token: str,
    positions: dict[EquipmentId, SwitchPosition] | None = None,
) -> OperatingStateId:
    state_id = OperatingStateId(f"state.audit.{token}")
    model.add_operating_state(OperatingState(state_id, token, positions or {}))
    return state_id


def _branching_project() -> tuple[
    _EditableProject,
    ProjectCommandHistory,
    EquipmentId,
    PortId,
    ElectricalNodeId,
    ElectricalNodeId,
]:
    model = ElectricalModel.with_builtins("Аудит ревизий")
    first = _node(model, "revision.a")
    second = _node(model, "revision.b")
    third = _node(model, "revision.c")
    equipment_id = EquipmentId("equipment.audit.revision.recloser")
    port_a = PortId("port.audit.revision.a")
    port_b = PortId("port.audit.revision.b")
    model.create_equipment(
        "builtin.recloser",
        "REC-AUDIT",
        equipment_id=equipment_id,
        port_ids_by_role={"a": port_a, "b": port_b},
        properties={
            "manufacturer": "Audit",
            "model": "R-10",
            "rated_voltage_v": 10_000,
            "rated_current_a": 630,
        },
        voltage_class_by_group={"main": U10},
        normal_position=SwitchPosition.CLOSED,
    )
    model.connect_port(
        port_a,
        first.id,
        connection_id=ConnectionId("connection.audit.revision.a"),
    )
    project = _EditableProject(model, _empty_diagram(), ProjectCatalogSnapshots())
    return (
        project,
        ProjectCommandHistory(project),
        equipment_id,
        port_b,
        second.id,
        third.id,
    )


def _execute_connect(
    history: ProjectCommandHistory,
    port_id: PortId,
    node_id: ElectricalNodeId,
    token: str,
) -> None:
    def command(draft) -> None:
        draft.electrical_model.connect_port(
            port_id,
            node_id,
            connection_id=ConnectionId(f"connection.audit.revision.{token}"),
        )

    history.execute(f"Подключить к {token}", command)


def test_full_graph_keeps_parallel_links_ring_and_two_real_sources() -> None:
    model = ElectricalModel.with_builtins("Граф аудита")
    first = _node(model, "graph.a")
    second = _node(model, "graph.b")
    third = _node(model, "graph.c")
    source_a = _source(model, "graph.a", first)
    source_b = _source(model, "graph.b", second)
    recloser = _two_terminal(
        model,
        "graph.recloser",
        first,
        second,
        type_id="builtin.recloser",
        roles=("a", "b"),
        position=SwitchPosition.CLOSED,
    )
    line_ac_1 = _two_terminal(model, "graph.line_ac_1", first, third)
    line_ac_2 = _two_terminal(model, "graph.line_ac_2", first, third)
    line_cb = _two_terminal(model, "graph.line_cb", third, second)
    opened_id = _state(model, "graph.open", {recloser: SwitchPosition.OPEN})
    closed_id = _state(model, "graph.closed", {recloser: SwitchPosition.CLOSED})

    engine = TopologyEngine()
    opened = engine.compile(model, opened_id)
    closed = engine.compile(model, closed_id)

    assert opened.is_valid and closed.is_valid
    assert {
        item.equipment_id for item in opened.links_between(first.id, third.id)
    } == {line_ac_1, line_ac_2}
    assert set(opened.sources_for(second.id)) == {source_a, source_b}
    assert opened.find_path(first.id, second.id)
    assert next(
        item for item in opened.links.values() if item.equipment_id == recloser
    ).active is False
    assert next(
        item for item in closed.links.values() if item.equipment_id == recloser
    ).active is True
    assert closed.components[closed.component_by_node[first.id]].is_meshed
    assert {
        item.equipment_id for item in opened.neighbors(third.id)
    } == {line_ac_1, line_ac_2, line_cb}


def test_recloser_state_reaches_projection_adapter_and_core_network() -> None:
    model = ElectricalModel.with_builtins("Сквозной реклоузер")
    first = _node(model, "flow.a")
    second = _node(model, "flow.b")
    _source(model, "flow", first)
    recloser = _two_terminal(
        model,
        "flow.recloser",
        first,
        second,
        type_id="builtin.recloser",
        roles=("a", "b"),
        position=SwitchPosition.CLOSED,
    )
    opened_id = _state(model, "flow.open", {recloser: SwitchPosition.OPEN})
    closed_id = _state(model, "flow.closed", {recloser: SwitchPosition.CLOSED})
    engine = TopologyEngine()

    opened_projection = CalculationProjectionBuilder().build(model, opened_id)
    closed_projection = CalculationProjectionBuilder().build(model, closed_id)
    assert opened_projection.branches_for_equipment(recloser)[0].active is False
    assert closed_projection.branches_for_equipment(recloser)[0].active is True

    adapted = adapt_to_calculation(model, engine.compile(model, closed_id))
    branch_id = adapted.trace.domain_equipment_to_legacy[recloser.value][0]
    modes = {item.name: item for item in adapted.network.modes.values()}
    branch = adapted.network.branches[branch_id]
    assert adapted.network.branch_conducting(branch, modes["flow.open"]) is False
    assert adapted.network.branch_conducting(branch, modes["flow.closed"]) is True


def test_graphical_move_does_not_change_domain_or_topology() -> None:
    model = ElectricalModel.with_builtins("Графическая изоляция")
    node = _node(model, "diagram")
    source_id = _source(model, "diagram", node)
    page = DiagramPage(PageId("page.audit.diagram"), "Схема")
    representation_id = GraphicalRepresentationId("representation.audit.diagram")
    representation = GraphicalRepresentation(
        representation_id,
        page.id,
        RepresentationTargetKind.EQUIPMENT,
        equipment_id=source_id,
        x=20.0,
        y=30.0,
    )
    project = _EditableProject(
        model,
        DiagramDocument.create(
            "Графическая изоляция",
            (page,),
            (representation,),
            document_id=DiagramDocumentId("diagram.audit.geometry"),
        ),
        ProjectCatalogSnapshots(),
    )
    engine = TopologyEngine()
    revision_before = model.revision
    fingerprint_before = engine.compile(model).topology_fingerprint

    ProjectEditorController(project).move_representations(
        (representation_id,), 80.0, 50.0
    )

    assert model.revision == revision_before
    assert engine.compile(model).topology_fingerprint == fingerprint_before
    assert project.diagram.representations[representation_id].x == 100.0
    assert project.diagram.representations[representation_id].y == 80.0


def test_aud_top_001_topology_cache_must_not_alias_divergent_history() -> None:
    project, history, equipment_id, port_b, node_b, node_c = _branching_project()
    engine = TopologyEngine()

    _execute_connect(history, port_b, node_b, "b")
    first = engine.compile(project.electrical_model)
    history.undo()
    _execute_connect(history, port_b, node_c, "c")
    cached = engine.compile(project.electrical_model)
    engine.clear_cache(project.electrical_model)
    fresh = engine.compile(project.electrical_model)

    cached_link = next(
        item for item in cached.links.values() if item.equipment_id == equipment_id
    )
    fresh_link = next(
        item for item in fresh.links.values() if item.equipment_id == equipment_id
    )
    assert cached_link.node_ids == fresh_link.node_ids
    assert node_c in cached_link.node_ids


def test_aud_top_002_project_data_must_refresh_same_revision_new_content() -> None:
    editable, history, _equipment_id, port_b, node_b, node_c = _branching_project()
    project = ProjectData(
        Network(editable.electrical_model.name),
        Methodology.load(),
        {"name": editable.electrical_model.name},
        ProjectStructure(),
        FORMAT_VERSION,
        editable.electrical_model,
        diagram=editable.diagram,
        catalog_snapshots=editable.catalog_snapshots,
    )
    # История должна работать с тем же ProjectData, который владеет derived view.
    history = ProjectCommandHistory(project)

    _execute_connect(history, port_b, node_b, "b")
    network_for_b = project.network
    history.undo()
    _execute_connect(history, port_b, node_c, "c")
    network_without_force = project.network
    network_for_c = project.refresh_calculation_view(force=True)

    def endpoints(network: Network) -> frozenset[str]:
        branch = next(
            item for item in network.branches.values() if item.name == "REC-AUDIT"
        )
        return frozenset((branch.node_from, branch.node_to))

    assert network_without_force is not network_for_b
    assert endpoints(network_without_force) == endpoints(network_for_c)


def test_aud_top_003_view_model_result_must_use_current_project_network() -> None:
    model = ElectricalModel.with_builtins("Свежесть результата")
    first = _node(model, "result.a")
    second = _node(model, "result.b")
    _source(model, "result", first)
    recloser = _two_terminal(
        model,
        "result.recloser",
        first,
        second,
        type_id="builtin.recloser",
        roles=("a", "b"),
        position=SwitchPosition.CLOSED,
    )
    state_id = _state(model, "result", {recloser: SwitchPosition.CLOSED})
    adapted = adapt_to_calculation(model, TopologyEngine().compile(model, state_id))
    project = ProjectData(
        adapted.network,
        Methodology.load(),
        {"name": model.name},
        ProjectStructure(),
        FORMAT_VERSION,
        model,
        adapter_diagnostics=adapted.diagnostics,
    )
    vm = ProjectViewModel(project, Path("audit-project.json"))
    assert vm.result is not None

    model.set_switch_position(state_id, recloser, SwitchPosition.OPEN)
    current_network = project.network
    assert current_network is vm.net
    assert vm.recalculate() is True
    assert vm.result is not None
    assert vm.result.ctx.net is current_network


def test_aud_top_004_projection_must_reject_foreign_topology_registry() -> None:
    model = ElectricalModel.with_builtins("Registry boundary")
    first = _node(model, "registry.a")
    second = _node(model, "registry.b")
    _two_terminal(model, "registry.line", first, second)

    handlers = tuple(
        TopologyBehaviorHandler(
            item.behavior_key,
            BehaviorKind.PASSIVE if item.behavior_key == "line" else item.kind,
            item.roles,
            item.same_voltage,
            item.compatibility,
        )
        for item in builtin_topology_handlers()
    )
    foreign_snapshot = TopologyEngine(
        TopologyBehaviorRegistry(handlers)
    ).compile(model)
    assert not foreign_snapshot.links

    with pytest.raises(TopologyCompatibilityError, match="behavior registry"):
        CalculationProjectionBuilder().build(
            model, topology_snapshot=foreign_snapshot
        )
