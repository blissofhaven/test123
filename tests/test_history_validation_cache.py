# -*- coding: utf-8 -*-
"""Регрессии оптимизированной проверки общей истории проекта."""
from __future__ import annotations

from dataclasses import dataclass, replace

import pytest

from rza_calc.domain import fingerprint as fingerprint_module
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
    Connection,
    ConnectionId,
    DomainInvariantError,
    ElectricalModel,
    ElectricalNode,
    ElectricalNodeId,
    EquipmentId,
    PortId,
)
from rza_calc.editor.history import (
    ProjectCommandHistory,
    ProjectHistoryError,
    ProjectMemento,
)


@dataclass
class _Project:
    electrical_model: ElectricalModel
    diagram: DiagramDocument
    catalog_snapshots: ProjectCatalogSnapshots


def _project() -> tuple[_Project, GraphicalRepresentationId]:
    model = ElectricalModel.with_builtins("Проверка истории")
    equipment, _ = model.create_equipment(
        "builtin.load",
        "Нагрузка для графической команды",
    )
    page = DiagramPage(PageId("page.history.cache"), "Основная схема")
    representation_id = GraphicalRepresentationId(
        "representation.history.cache"
    )
    representation = GraphicalRepresentation(
        representation_id,
        page.id,
        RepresentationTargetKind.EQUIPMENT,
        equipment_id=equipment.id,
        x=10.0,
        y=20.0,
    )
    return (
        _Project(
            model,
            DiagramDocument.create(
                "Однолинейная схема",
                (page,),
                (representation,),
                document_id=DiagramDocumentId("diagram.history.cache"),
            ),
            ProjectCatalogSnapshots(),
        ),
        representation_id,
    )


def _move(
    representation_id: GraphicalRepresentationId,
    x: float,
    y: float,
):
    def command(draft) -> None:
        draft.diagram = draft.diagram.moved_representation(
            representation_id,
            x,
            y,
        )

    return command


def test_graphical_execute_validates_once_and_undo_redo_keep_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, representation_id = _project()
    history = ProjectCommandHistory(project)
    calls = {
        "electrical": 0,
        "diagram": 0,
        "catalog": 0,
        "fingerprint": 0,
    }
    original_electrical = ElectricalModel.validate_integrity
    original_diagram = DiagramDocument.require_valid_targets
    original_catalog = ProjectCatalogSnapshots.validate_targets
    original_fingerprint = fingerprint_module.electrical_model_fingerprint

    def validate_electrical(model):
        calls["electrical"] += 1
        return original_electrical(model)

    def validate_diagram(document, model):
        calls["diagram"] += 1
        return original_diagram(document, model)

    def validate_catalog(snapshots, model):
        calls["catalog"] += 1
        return original_catalog(snapshots, model)

    def fingerprint(model):
        calls["fingerprint"] += 1
        return original_fingerprint(model)

    monkeypatch.setattr(
        ElectricalModel,
        "validate_integrity",
        validate_electrical,
    )
    monkeypatch.setattr(
        DiagramDocument,
        "require_valid_targets",
        validate_diagram,
    )
    monkeypatch.setattr(
        ProjectCatalogSnapshots,
        "validate_targets",
        validate_catalog,
    )
    monkeypatch.setattr(
        fingerprint_module,
        "electrical_model_fingerprint",
        fingerprint,
    )

    history.execute(
        "Переместить объект",
        _move(representation_id, 100.0, 200.0),
    )

    # execute проверяет целевой ProjectMemento ровно один раз. Повторный
    # fingerprint не нужен, потому что электрическая часть не менялась.
    assert calls == {
        "electrical": 1,
        "diagram": 1,
        "catalog": 1,
        "fingerprint": 1,
    }
    assert project.diagram.representations[representation_id].x == 100.0
    assert history.journal[-1].electrical_fingerprint_before == (
        history.journal[-1].electrical_fingerprint_after
    )

    history.undo()
    assert project.diagram.representations[representation_id].x == 10.0
    assert calls == {
        "electrical": 1,
        "diagram": 2,
        "catalog": 2,
        "fingerprint": 2,
    }
    assert history.journal[-1].electrical_fingerprint_before == (
        history.journal[-1].electrical_fingerprint_after
    )

    history.redo()
    assert project.diagram.representations[representation_id].x == 100.0
    assert calls == {
        "electrical": 1,
        "diagram": 3,
        "catalog": 3,
        "fingerprint": 3,
    }
    assert history.journal[-1].electrical_fingerprint_before == (
        history.journal[-1].electrical_fingerprint_after
    )


def test_electrical_change_invalidates_cache_but_graphics_do_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, representation_id = _project()
    history = ProjectCommandHistory(project)
    calls = {"electrical": 0, "diagram": 0, "catalog": 0}
    original_electrical = ElectricalModel.validate_integrity
    original_diagram = DiagramDocument.require_valid_targets
    original_catalog = ProjectCatalogSnapshots.validate_targets

    def validate_electrical(model):
        calls["electrical"] += 1
        return original_electrical(model)

    def validate_diagram(document, model):
        calls["diagram"] += 1
        return original_diagram(document, model)

    def validate_catalog(snapshots, model):
        calls["catalog"] += 1
        return original_catalog(snapshots, model)

    monkeypatch.setattr(
        ElectricalModel,
        "validate_integrity",
        validate_electrical,
    )
    monkeypatch.setattr(
        DiagramDocument,
        "require_valid_targets",
        validate_diagram,
    )
    monkeypatch.setattr(
        ProjectCatalogSnapshots,
        "validate_targets",
        validate_catalog,
    )

    history.execute("Переместить объект 1", _move(representation_id, 30, 40))
    history.execute("Переместить объект 2", _move(representation_id, 50, 60))
    assert calls == {"electrical": 1, "diagram": 2, "catalog": 2}

    node_id = ElectricalNodeId("node.history.cache.new")

    def add_node(draft) -> None:
        original_revision = draft.electrical_model.revision
        draft.electrical_model.add_node(
            ElectricalNode(node_id, "Новый электрический узел")
        )
        # Кэш обязан сравнивать полный memento, а не только revision.
        draft.electrical_model._revision = original_revision

    history.execute("Добавить электрический узел", add_node)
    assert node_id in project.electrical_model.electrical_nodes
    assert calls == {"electrical": 2, "diagram": 3, "catalog": 3}

    history.undo()
    assert node_id not in project.electrical_model.electrical_nodes
    history.redo()
    assert node_id in project.electrical_model.electrical_nodes
    assert calls == {"electrical": 4, "diagram": 5, "catalog": 5}


def test_invalid_electrical_target_is_rejected_atomically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, representation_id = _project()
    history = ProjectCommandHistory(project)
    before = ProjectMemento.capture(project)
    calls = {"electrical": 0}
    original_electrical = ElectricalModel.validate_integrity

    def validate_electrical(model):
        calls["electrical"] += 1
        return original_electrical(model)

    monkeypatch.setattr(
        ElectricalModel,
        "validate_integrity",
        validate_electrical,
    )

    def corrupt(draft) -> None:
        connection_id = ConnectionId("connection.history.missing")
        draft.electrical_model._connections[connection_id] = Connection(
            connection_id,
            PortId("port.history.missing"),
            ElectricalNodeId("node.history.missing"),
        )

    with pytest.raises(
        ProjectHistoryError,
        match="некорректную электрическую модель",
    ):
        history.execute("Создать повреждённую связь", corrupt)

    assert ProjectMemento.capture(project) == before
    assert history.journal == ()
    assert not history.can_undo
    assert not history.can_redo
    assert calls["electrical"] == 1

    history.execute(
        "Переместить после отказа",
        _move(representation_id, 70, 80),
    )
    # Отклонённый target не попал в кэш успешно проверенной модели.
    assert calls["electrical"] == 2


def test_failed_diagram_validation_does_not_seed_electrical_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project, representation_id = _project()
    history = ProjectCommandHistory(project)
    before = ProjectMemento.capture(project)
    calls = {"electrical": 0}
    original_electrical = ElectricalModel.validate_integrity
    added_node_id = ElectricalNodeId("node.history.before.bad.diagram")

    def validate_electrical(model):
        calls["electrical"] += 1
        return original_electrical(model)

    monkeypatch.setattr(
        ElectricalModel,
        "validate_integrity",
        validate_electrical,
    )

    def break_diagram(draft) -> None:
        draft.electrical_model.add_node(
            ElectricalNode(
                added_node_id,
                "Не должен частично попасть в проект",
            )
        )
        representations = dict(draft.diagram.representations)
        representations[representation_id] = replace(
            representations[representation_id],
            equipment_id=EquipmentId("equipment.history.missing"),
        )
        draft.diagram = replace(
            draft.diagram,
            representations=representations,
            revision=draft.diagram.revision + 1,
        )

    with pytest.raises(DomainInvariantError, match="повреждённые ссылки"):
        history.execute("Повредить графическую ссылку", break_diagram)

    assert ProjectMemento.capture(project) == before
    assert added_node_id not in project.electrical_model.electrical_nodes
    assert history.project_revision == 0
    assert history.journal == ()
    assert not history.can_undo
    assert not history.can_redo
    assert calls["electrical"] == 1
    history.execute(
        "Переместить после отказа диаграммы",
        _move(representation_id, 90, 100),
    )
    assert calls["electrical"] == 2
