# -*- coding: utf-8 -*-
"""Приёмочная матрица 17 обязательных требований редактора Этапа 3.

Тесты команд не зависят от Qt. Единственные GUI-проверки запускают настоящий
``EditorWorkspaceWidget`` через offscreen-платформу. Проверка 1000 объектов
намеренно не содержит жёсткого ограничения времени: устойчивые регрессионные
условия проверяют число объектов, ревизии моделей, одну общую команду
перемещения и сохранение электрической топологии. Численные метрики собирает
``benchmark_stage3_editor.py``.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from rza_calc.calculation import CalculationProjectionBuilder  # noqa: E402
from rza_calc.domain.catalog_snapshot import ProjectCatalogSnapshots  # noqa: E402
from rza_calc.domain.diagram import (  # noqa: E402
    DiagramDocument,
    DiagramDocumentId,
    DiagramPage,
    GraphicalRepresentation,
    GraphicalRepresentationId,
    PageId,
    RepresentationTargetKind,
)
from rza_calc.domain.electrical import (  # noqa: E402
    ElectricalModel,
    ElectricalNode,
    ElectricalNodeId,
    EquipmentId,
    OperatingState,
    OperatingStateId,
    SwitchPosition,
    VoltageClassId,
)
from rza_calc.editor import (  # noqa: E402
    EditorCommandError,
    EditorMode,
    ProjectEditorController,
    RU_STRINGS,
    RepresentationGraphics,
)
from rza_calc.gui.strings import (  # noqa: E402
    DEFAULT_LOCALE,
    PROPERTY_LABELS,
    RU,
)
from rza_calc.io.diagram import diagram_to_dict  # noqa: E402
from rza_calc.io.electrical_model import electrical_model_to_dict  # noqa: E402
from rza_calc.io.project import load_project, save_project  # noqa: E402
from rza_calc.topology import TopologyEngine  # noqa: E402


U10 = VoltageClassId("builtin.voltage.ac.10kv")
EXAMPLE = (
    Path(__file__).resolve().parent.parent
    / "tests" / "fixtures" / "legacy_projects"
    / "gtes_sever.json"
)


@dataclass
class _Project:
    electrical_model: ElectricalModel
    diagram: DiagramDocument
    catalog_snapshots: ProjectCatalogSnapshots


def _project(token: str = "requirements", *, with_page: bool = True) -> _Project:
    page = DiagramPage(PageId(f"page.stage3.{token}"), "Основная схема")
    return _Project(
        ElectricalModel.with_builtins("Приёмка редактора"),
        DiagramDocument.create(
            "Однолинейная схема",
            (page,) if with_page else (),
            document_id=DiagramDocumentId(f"diagram.stage3.{token}"),
        ),
        ProjectCatalogSnapshots(),
    )


def _controller(token: str = "requirements") -> tuple[_Project, ProjectEditorController]:
    project = _project(token)
    return project, ProjectEditorController(project)


def _place_equipment(
    project: _Project,
    equipment_id: EquipmentId,
    token: str,
    *,
    x: float,
    y: float,
) -> GraphicalRepresentationId:
    representation = GraphicalRepresentation(
        GraphicalRepresentationId(f"representation.stage3.{token}"),
        next(iter(project.diagram.pages)),
        RepresentationTargetKind.EQUIPMENT,
        equipment_id=equipment_id,
        x=x,
        y=y,
        label=project.electrical_model.equipment[equipment_id].name,
    )
    project.diagram = DiagramDocument.create(
        project.diagram.name,
        project.diagram.pages.values(),
        (*project.diagram.representations.values(), representation),
        document_id=project.diagram.id,
        extensions=project.diagram.extensions,
    )
    return representation.id


def _connected_pair(token: str) -> tuple[
    _Project,
    ProjectEditorController,
    EquipmentId,
    EquipmentId,
    GraphicalRepresentationId,
    GraphicalRepresentationId,
]:
    project = _project(token)
    first, _ = project.electrical_model.create_equipment(
        "builtin.circuit_breaker",
        "QF-1",
        normal_position=SwitchPosition.CLOSED,
    )
    second, _ = project.electrical_model.create_equipment(
        "builtin.load",
        "Нагрузка-1",
    )
    upstream = ElectricalNode(
        ElectricalNodeId(f"node.stage3.{token}.upstream"),
        "Вводной узел",
    )
    project.electrical_model.add_node(upstream)
    project.electrical_model.connect_port(first.port_ids[0], upstream.id)
    project.electrical_model.connect_ports(first.port_ids[1], second.port_ids[0])
    first_representation = _place_equipment(
        project, first.id, token + ".first", x=20.0, y=20.0
    )
    second_representation = _place_equipment(
        project, second.id, token + ".second", x=120.0, y=20.0
    )
    return (
        project,
        ProjectEditorController(project),
        first.id,
        second.id,
        first_representation,
        second_representation,
    )


def _electrical_content(model: ElectricalModel) -> dict[str, Any]:
    value = electrical_model_to_dict(model)
    value.pop("revision", None)
    return value


def _diagram_content(document: DiagramDocument) -> dict[str, Any]:
    value = diagram_to_dict(document)
    value.pop("revision", None)
    return value


def _large_controller(count: int = 1024) -> tuple[
    _Project, ProjectEditorController, tuple[GraphicalRepresentationId, ...]
]:
    if count < 1 or count & (count - 1):
        raise ValueError("Размер большой схемы должен быть степенью двойки.")
    project, controller = _controller("large")
    first = controller.add_equipment(
        "builtin.load",
        "Нагрузка 1",
        x=0.0,
        y=0.0,
    )
    ids = [first.representation_id]
    while len(ids) < count:
        controller.copy(ids)
        result = controller.paste(
            offset_x=40.0 + len(ids),
            offset_y=20.0,
        )
        ids.extend(result.representation_ids)
    return project, controller, tuple(ids)


# 1. Добавление элемента создаёт объект модели и графическое представление.
def test_01_add_creates_model_object_ports_representation_and_history() -> None:
    project, controller = _controller("add")
    before_equipment = len(project.electrical_model.equipment)
    before_representations = len(project.diagram.representations)

    added = controller.add_equipment(
        "builtin.circuit_breaker",
        "QF-1",
        x=13.0,
        y=27.0,
    )

    equipment = project.electrical_model.equipment[added.equipment_id]
    representation = project.diagram.representations[added.representation_id]
    assert len(project.electrical_model.equipment) == before_equipment + 1
    assert len(project.diagram.representations) == before_representations + 1
    assert equipment.port_ids == added.port_ids
    assert all(
        project.electrical_model.ports[item].equipment_id == equipment.id
        for item in equipment.port_ids
    )
    assert representation.equipment_id == equipment.id
    assert representation.page_id == added.page_id
    assert len(controller.journal) == 1
    assert controller.can_undo


# 2. Перемещение изменяет только графические координаты.
def test_02_move_changes_only_graphical_coordinates() -> None:
    project, controller = _controller("move_coordinates")
    controller.set_snap(False)
    added = controller.add_equipment("builtin.load", "Нагрузка", x=10.0, y=30.0)
    electrical_before = _electrical_content(project.electrical_model)
    row_before = project.diagram.representations[added.representation_id]

    controller.move_representations((added.representation_id,), 17.5, -8.0)

    row_after = project.diagram.representations[added.representation_id]
    assert (row_after.x, row_after.y) == (27.5, 22.0)
    assert row_after.equipment_id == row_before.equipment_id
    assert _electrical_content(project.electrical_model) == electrical_before


# 3. Перемещение не меняет электрические соединения.
def test_03_move_preserves_connections_topology_and_calculation_projection() -> None:
    project, controller, _, _, first_representation, second_representation = (
        _connected_pair("move_topology")
    )
    controller.set_snap(False)
    connectivity = project.electrical_model.connectivity_signature()
    topology = TopologyEngine().compile(project.electrical_model).semantic_signature()
    projection = (
        CalculationProjectionBuilder()
        .build(project.electrical_model)
        .semantic_fingerprint()
    )
    connections = dict(project.electrical_model.connections)

    controller.move_representations(
        (first_representation, second_representation), 55.0, 35.0
    )

    assert dict(project.electrical_model.connections) == connections
    assert project.electrical_model.connectivity_signature() == connectivity
    assert TopologyEngine().compile(project.electrical_model).semantic_signature() == topology
    assert (
        CalculationProjectionBuilder()
        .build(project.electrical_model)
        .semantic_fingerprint()
        == projection
    )


# 4. Копирование создаёт новые ID.
def test_04_copy_and_paste_create_new_stable_ids() -> None:
    project, controller = _controller("copy_ids")
    original = controller.add_equipment("builtin.recloser", "REC-1")
    original_equipment = project.electrical_model.equipment[original.equipment_id]
    controller.copy((original.representation_id,))

    result = controller.paste(offset_x=40.0, offset_y=20.0)

    copied_equipment = project.electrical_model.equipment[result.equipment_ids[0]]
    assert set(result.equipment_ids).isdisjoint({original.equipment_id})
    assert set(result.representation_ids).isdisjoint({original.representation_id})
    assert set(copied_equipment.port_ids).isdisjoint(original_equipment.port_ids)
    all_new = {
        *(item.value for item in result.equipment_ids),
        *(item.value for item in result.representation_ids),
        *(item.value for item in copied_equipment.port_ids),
    }
    assert len(all_new) == 2 + len(copied_equipment.port_ids)


# 5. Внутренние связи копируемой группы сохраняются.
def test_05_group_copy_preserves_internal_connections() -> None:
    project, controller, _, _, first_representation, second_representation = (
        _connected_pair("copy_internal")
    )

    result = controller.duplicate((first_representation, second_representation))

    assert len(result.equipment_ids) == 2
    assert len(result.node_ids) == 1
    assert len(result.connection_ids) == 2
    copied_connections = tuple(
        project.electrical_model.connections[item] for item in result.connection_ids
    )
    assert {item.electrical_node_id for item in copied_connections} == set(
        result.node_ids
    )
    assert len({item.port_id for item in copied_connections}) == 2
    copied_node = result.node_ids[0]
    assert sum(
        item.electrical_node_id == copied_node
        for item in project.electrical_model.connections.values()
    ) == 2


# 6. Вставленная группа не ссылается на порты оригинала.
def test_06_pasted_group_has_no_original_port_references() -> None:
    project, controller, first_id, second_id, first_representation, second_representation = (
        _connected_pair("copy_ports")
    )
    original_ports = {
        *project.electrical_model.equipment[first_id].port_ids,
        *project.electrical_model.equipment[second_id].port_ids,
    }
    original_nodes = {
        item.electrical_node_id
        for item in project.electrical_model.connections.values()
        if item.port_id in original_ports
    }

    controller.copy((first_representation, second_representation))
    result = controller.paste()

    copied_ports = {
        port_id
        for equipment_id in result.equipment_ids
        for port_id in project.electrical_model.equipment[equipment_id].port_ids
    }
    copied_connections = tuple(
        project.electrical_model.connections[item] for item in result.connection_ids
    )
    assert copied_ports.isdisjoint(original_ports)
    assert all(item.port_id in copied_ports for item in copied_connections)
    assert all(item.electrical_node_id in result.node_ids for item in copied_connections)
    assert set(result.node_ids).isdisjoint(original_nodes)


# 7. Удаление не оставляет осиротевшие ссылки.
def test_07_delete_leaves_no_orphan_references() -> None:
    project, controller, first_id, _, first_representation, _ = _connected_pair(
        "delete_integrity"
    )
    owned_ports = set(project.electrical_model.equipment[first_id].port_ids)

    result = controller.delete_from_project((first_representation,))

    assert result.equipment_ids == (first_id,)
    assert first_id not in project.electrical_model.equipment
    assert owned_ports.isdisjoint(project.electrical_model.ports)
    assert all(
        item.port_id not in owned_ports
        for item in project.electrical_model.connections.values()
    )
    assert project.electrical_model.validate_integrity() == []
    assert project.diagram.validate_targets(project.electrical_model) == ()
    assert project.catalog_snapshots.validate_targets(project.electrical_model) == ()


# 8. Undo восстанавливает объект и его связи.
def test_08_undo_restores_object_ports_connections_and_representation() -> None:
    project, controller, _, _, first_representation, _ = _connected_pair(
        "undo_delete"
    )
    electrical_before = _electrical_content(project.electrical_model)
    diagram_before = _diagram_content(project.diagram)
    connections_before = project.electrical_model.connectivity_signature()

    controller.delete_from_project((first_representation,))
    controller.undo()

    assert _electrical_content(project.electrical_model) == electrical_before
    assert _diagram_content(project.diagram) == diagram_before
    assert project.electrical_model.connectivity_signature() == connections_before
    assert first_representation in project.diagram.representations


# 9. Redo повторно удаляет объект.
def test_09_redo_repeats_the_exact_delete_result() -> None:
    project, controller, first_id, _, first_representation, _ = _connected_pair(
        "redo_delete"
    )
    controller.delete_from_project((first_representation,))
    electrical_deleted = _electrical_content(project.electrical_model)
    diagram_deleted = _diagram_content(project.diagram)

    controller.undo()
    controller.redo()

    assert first_id not in project.electrical_model.equipment
    assert first_representation not in project.diagram.representations
    assert _electrical_content(project.electrical_model) == electrical_deleted
    assert _diagram_content(project.diagram) == diagram_deleted


# 10. Сохранение и загрузка сохраняют координаты.
def test_10_save_load_preserve_coordinates_graphics_labels_and_view(tmp_path: Path) -> None:
    project = load_project(EXAMPLE)
    controller = ProjectEditorController(project)
    controller.set_mode(EditorMode.EDIT)
    page = controller.ensure_default_page()
    controller.set_snap(False)
    added = controller.add_electrical_node(
        "Контрольная шина",
        page_id=page,
        x=123.25,
        y=-456.75,
        voltage_class_id=U10,
        symbol_key="busbar",
        width=280.0,
        height=16.0,
    )
    controller.rotate_representation(added.representation_id, 90.0)
    controller.set_label(
        added.representation_id,
        text="Шина 10 кВ",
        label_x=18.0,
        label_y=-42.0,
        visible=False,
    )
    controller.set_grid(False, grid_size=25.0)
    controller.set_view(1.75, 12_500.0, -8_250.0)
    expected = project.diagram.representations[added.representation_id]
    expected_workspace = controller.workspace_state
    target = tmp_path / "stage3-coordinates.json"

    save_project(target, project)
    reopened = load_project(target)

    assert reopened.diagram.representations[added.representation_id] == expected
    restored_controller = ProjectEditorController(reopened)
    assert restored_controller.workspace_state == expected_workspace


# 11. Сохранение и загрузка сохраняют электрическую топологию.
def test_11_save_load_preserve_electrical_topology(tmp_path: Path) -> None:
    project = load_project(EXAMPLE)
    controller = ProjectEditorController(project)
    controller.set_mode(EditorMode.EDIT)
    page = controller.ensure_default_page()
    existing_node = next(iter(project.electrical_model.electrical_nodes))
    controller.place_existing_node(existing_node, page_id=page, x=80.0, y=120.0)
    state_ids = tuple(sorted(project.electrical_model.operating_states, key=lambda item: item.value))
    engine = TopologyEngine()
    signatures_before = tuple(
        engine.compile(project.electrical_model, state_id).semantic_signature()
        for state_id in (state_ids or (None,))
    )
    connectivity_before = project.electrical_model.connectivity_signature()
    target = tmp_path / "stage3-topology.json"

    save_project(target, project)
    reopened = load_project(target)

    signatures_after = tuple(
        TopologyEngine().compile(reopened.electrical_model, state_id).semantic_signature()
        for state_id in (state_ids or (None,))
    )
    assert reopened.electrical_model.connectivity_signature() == connectivity_before
    assert signatures_after == signatures_before


# 12. Аналитический режим запрещает перемещение.
def test_12_analysis_mode_rejects_move_without_side_effects() -> None:
    project, controller = _controller("analysis")
    added = controller.add_electrical_node("Узел", x=40.0, y=60.0)
    controller.set_mode(EditorMode.ANALYSIS)
    electrical_before = _electrical_content(project.electrical_model)
    diagram_before = _diagram_content(project.diagram)
    journal_before = controller.journal

    with pytest.raises(EditorCommandError):
        controller.move_representations((added.representation_id,), 20.0, 20.0)

    assert _electrical_content(project.electrical_model) == electrical_before
    assert _diagram_content(project.diagram) == diagram_before
    assert controller.journal == journal_before


# 13. Переключение реклоузера не изменяет его координаты.
def test_13_recloser_switching_changes_path_but_not_coordinates() -> None:
    project = _project("recloser")
    model = project.electrical_model
    first = ElectricalNode(ElectricalNodeId("node.stage3.recloser.a"), "Шина А", declared_voltage_class_id=U10)
    second = ElectricalNode(ElectricalNodeId("node.stage3.recloser.b"), "Шина Б", declared_voltage_class_id=U10)
    model.add_node(first)
    model.add_node(second)
    recloser, _ = model.create_equipment(
        "builtin.recloser",
        "REC-1",
        properties={"rated_current_a": 630.0, "rated_voltage_v": 10_000},
        voltage_class_by_group={"main": U10},
        normal_position=SwitchPosition.CLOSED,
    )
    model.connect_port(model.port_by_role(recloser.id, "a").id, first.id)
    model.connect_port(model.port_by_role(recloser.id, "b").id, second.id)
    state = OperatingState(
        OperatingStateId("state.stage3.recloser"),
        "Нормальный режим",
        {recloser.id: SwitchPosition.CLOSED},
    )
    model.add_operating_state(state)
    representation_id = _place_equipment(
        project, recloser.id, "recloser", x=345.0, y=-210.0
    )
    controller = ProjectEditorController(project)
    controller.set_mode(EditorMode.ANALYSIS)
    before = project.diagram.representations[representation_id]
    snapshot_closed = TopologyEngine().compile(model, state.id)
    assert snapshot_closed.has_path(first.id, second.id)

    controller.set_switch_position(
        state.id,
        recloser.id,
        SwitchPosition.OPEN,
        confirmed=True,
    )

    after = project.diagram.representations[representation_id]
    snapshot_open = TopologyEngine().compile(model, state.id)
    assert not snapshot_open.has_path(first.id, second.id)
    assert (after.x, after.y, after.rotation_deg) == (
        before.x,
        before.y,
        before.rotation_deg,
    )
    assert after.id == before.id


# 14. Изменение длины шины не создаёт новый электрический узел.
def test_14_busbar_length_is_graphics_only_and_keeps_one_node() -> None:
    project, controller = _controller("busbar")
    added = controller.add_electrical_node(
        "Секция шин",
        symbol_key="busbar",
        width=220.0,
        height=14.0,
        voltage_class_id=U10,
    )
    node_ids = set(project.electrical_model.electrical_nodes)
    electrical_revision = project.electrical_model.revision

    controller.resize_representation(added.representation_id, 480.0, 14.0)

    row = project.diagram.representations[added.representation_id]
    assert RepresentationGraphics.from_representation(row).width == 480.0
    assert set(project.electrical_model.electrical_nodes) == node_ids
    assert len(node_ids) == 1
    assert project.electrical_model.revision == electrical_revision


# 15. Положение надписи не влияет на электрическую модель.
def test_15_label_position_visibility_and_text_do_not_change_electrical_model() -> None:
    project, controller, _, _, first_representation, _ = _connected_pair(
        "label"
    )
    electrical_before = _electrical_content(project.electrical_model)
    topology_before = TopologyEngine().compile(project.electrical_model).semantic_signature()

    controller.set_label(
        first_representation,
        text="Вводной выключатель",
        label_x=95.0,
        label_y=-65.0,
        visible=False,
    )

    row = project.diagram.representations[first_representation]
    graphics = RepresentationGraphics.from_representation(row)
    assert row.label == "Вводной выключатель"
    assert (graphics.label_x, graphics.label_y, graphics.label_visible) == (
        95.0,
        -65.0,
        False,
    )
    assert _electrical_content(project.electrical_model) == electrical_before
    assert TopologyEngine().compile(project.electrical_model).semantic_signature() == topology_before


def _has_russian_letter(value: str) -> bool:
    return any("а" <= item.casefold() <= "я" or item in "ёЁ" for item in value)


def _visible_widget_texts(widget: Any) -> tuple[str, ...]:
    from PySide6.QtGui import QAction
    from PySide6.QtWidgets import (
        QAbstractButton,
        QComboBox,
        QGroupBox,
        QLabel,
        QLineEdit,
        QTabWidget,
        QTableWidget,
        QTreeWidget,
        QWidget,
    )

    values: list[str] = []
    widgets = (widget, *widget.findChildren(QWidget))
    for item in widgets:
        if isinstance(item, (QLabel, QAbstractButton)):
            values.append(item.text())
        if isinstance(item, QGroupBox):
            values.append(item.title())
        if isinstance(item, QLineEdit):
            values.append(item.placeholderText())
        tooltip = item.toolTip()
        if tooltip:
            values.append(tooltip)
        if isinstance(item, QTabWidget):
            values.extend(item.tabText(index) for index in range(item.count()))
        if isinstance(item, QComboBox):
            values.extend(item.itemText(index) for index in range(item.count()))
        if isinstance(item, (QTreeWidget, QTableWidget)):
            header = item.headerItem()
            if header is not None:
                values.extend(header.text(index) for index in range(header.columnCount()))
    values.extend(action.text() for action in widget.findChildren(QAction))
    return tuple(value.strip() for value in values if value and value.strip())


# 16. Все видимые пользователю строки интерфейса русские.
def test_16_visible_user_interface_strings_are_russian_and_centralized() -> None:
    assert DEFAULT_LOCALE == "ru-RU"
    assert RU
    assert RU_STRINGS
    required_properties = {
        "name", "description", "note", "type", "voltage_class",
        "normal_position", "x", "y", "rotation_deg", "width", "height",
        "label", "label_visible", "label_offset_x", "label_offset_y",
        "equipment_id", "representation_id", "electrical_node_id", "page_id",
        "ports", "connection_count", "logical_line_id",
    }
    assert required_properties <= set(PROPERTY_LABELS)
    assert all(_has_russian_letter(value) for value in RU.values())
    assert all(_has_russian_letter(value) for value in RU_STRINGS.values())

    from PySide6.QtWidgets import QApplication
    from rza_calc.gui.editor_panels import EditorWorkspaceWidget

    app = QApplication.instance() or QApplication(["stage3-offscreen-test"])
    _, controller = _controller("localization")
    workspace = EditorWorkspaceWidget(controller)
    workspace.resize(1400, 900)
    app.processEvents()
    texts = _visible_widget_texts(workspace)
    assert len(texts) >= 30
    forbidden = {
        "on", "off", "open", "closed", "ok", "close", "yes", "no",
        "max", "min", "file", "save", "undo", "redo", "copy", "paste",
        "delete", "project", "equipment", "analysis", "edit",
    }
    violations = {
        value
        for value in texts
        if forbidden & {item.casefold() for item in re.findall(r"[A-Za-z]+", value)}
    }
    workspace.close()
    workspace.deleteLater()
    app.processEvents()
    assert not violations, "В интерфейсе найдены английские строки: " + repr(violations)


# 17. Проект с 1000 объектами открывается и остаётся управляемым.
def test_17_project_with_1000_objects_opens_and_remains_manageable() -> None:
    from PySide6.QtWidgets import QApplication
    from rza_calc.gui.editor_panels import EditorWorkspaceWidget

    app = QApplication.instance() or QApplication(["stage3-large-offscreen-test"])
    project, controller, ids = _large_controller(1024)
    assert len(ids) == 1024
    assert len(project.diagram.representations) == 1024
    assert len(project.electrical_model.equipment) == 1024
    electrical_revision = project.electrical_model.revision
    connectivity = project.electrical_model.connectivity_signature()
    representations_before = dict(project.diagram.representations)

    workspace = EditorWorkspaceWidget(controller)
    workspace.resize(1500, 900)
    workspace.show()
    app.processEvents()
    assert len(workspace.scene._items_by_id) == 1024
    workspace.scene.select_representations(ids)
    app.processEvents()
    assert len(workspace.scene.selected_representation_ids()) == 1024

    journal_before = len(controller.journal)
    controller.set_snap(False)
    controller.move_representations(ids, 20.0, 40.0)
    workspace.refresh()
    app.processEvents()
    assert len(controller.journal) == journal_before + 1
    assert project.electrical_model.revision == electrical_revision
    assert project.electrical_model.connectivity_signature() == connectivity
    for representation_id in ids:
        before = representations_before[representation_id]
        after = project.diagram.representations[representation_id]
        assert (after.x - before.x, after.y - before.y) == (20.0, 40.0)

    electrical_after_move = project.electrical_model.revision
    representations_after_move = dict(project.diagram.representations)
    for _ in range(25):
        workspace.view.zoom_in()
        workspace.view.zoom_out()
    workspace.view.centerOn(40_000.0, -30_000.0)
    controller.set_view(
        workspace.view.zoom_factor,
        40_000.0,
        -30_000.0,
    )
    app.processEvents()
    assert project.electrical_model.revision == electrical_after_move
    assert dict(project.diagram.representations) == representations_after_move
    assert project.electrical_model.connectivity_signature() == connectivity

    workspace.close()
    workspace.deleteLater()
    app.processEvents()
