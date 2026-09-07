"""Real page/tree navigation preserves electrical identity on large diagrams."""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from rza_calc.domain.electrical import DataConfirmation, LineKind
from rza_calc.domain.fingerprint import electrical_model_fingerprint
from rza_calc.editor.controller import PhysicalLineInput, PortTarget, ProjectEditorController
from rza_calc.gui.analysis_scheme import AnalysisSchemeView
from rza_calc.gui.editor_panels import ProjectEquipmentPanel, QTreeWidgetItemIteratorCompat
from rza_calc.gui.strings import ui_text
from rza_calc.io.electrical_model import electrical_model_to_dict
from rza_calc.io.project import load_project
from test_ui_connection_cleanup_inspector import workspace_factory
from test_ui_direct_connections import U10, _controller


def _network():
    controller = _controller()
    overview = next(iter(controller.diagram.pages))
    detail = controller.create_page("КТП-01 · 10/0,4 кВ")
    first = controller.add_equipment("builtin.circuit_breaker", "QF ввода А", x=0, y=0,
                                     voltage_class_by_group={"main": U10})
    second = controller.add_equipment("builtin.circuit_breaker", "QF ввода Б", x=240, y=0,
                                      voltage_class_by_group={"main": U10})
    result = controller.create_physical_line("КЛ промысла", LineKind.CABLE,
        PortTarget(first.port_ids[-1], first.representation_id),
        PortTarget(second.port_ids[0], second.representation_id),
        physical=PhysicalLineInput(None, DataConfirmation.UNCONFIRMED))
    controller.set_active_page(overview)
    return controller, overview, detail, first, second, result.route_id


def _item(panel, role, value):
    return next(item for item in QTreeWidgetItemIteratorCompat(panel.project_tree)
                if item.data(0, role) == value.value)


def _click(panel, item, modifiers=Qt.KeyboardModifier.NoModifier):
    panel.project_tree.scrollToItem(item)
    QApplication.processEvents()
    QTest.mouseClick(panel.project_tree.viewport(), Qt.MouseButton.LeftButton,
                    modifiers, panel.project_tree.visualItemRect(item).center())
    QApplication.processEvents()


def _state(controller):
    return (electrical_model_to_dict(controller.model), electrical_model_fingerprint(controller.model),
            controller.model.connectivity_signature(), dict(controller.diagram.representations),
            dict(controller.diagram.routes), len(controller.journal))


def test_page_header_click_and_keyboard_navigate_without_model_or_history_changes(workspace_factory):
    controller, overview, detail, *_ = _network()
    workspace = workspace_factory(controller)
    before = _state(controller)
    panel = workspace.side_panel
    _click(panel, _item(panel, panel.PAGE_ROLE, detail))
    assert workspace.canvas.page_id == detail
    assert controller.workspace_state.active_page_id == detail.value
    _click(panel, _item(panel, panel.PAGE_ROLE, overview))
    assert workspace.canvas.page_id == overview
    # Arrow navigation reaches a collapsed page header just like a mouse click.
    _item(panel, panel.PAGE_ROLE, overview).setExpanded(False)
    QTest.keyClick(panel.project_tree, Qt.Key.Key_Down)
    assert workspace.canvas.page_id == detail
    assert _state(controller) == before


def test_physical_route_is_placed_under_correct_page_and_tree_selection_focuses_it(workspace_factory):
    controller, _, detail, _, _, route_id = _network()
    workspace = workspace_factory(controller)
    panel = workspace.side_panel
    route_item = _item(panel, panel.ROUTE_ROLE, route_id)
    assert route_item.text(0) == "КЛ промысла"
    assert route_item.parent().data(0, panel.PAGE_ROLE) == detail.value
    assert not any(item.text(0) == ui_text("panel.unplaced")
                   for item in QTreeWidgetItemIteratorCompat(panel.project_tree))
    before = _state(controller)
    _click(panel, route_item)
    assert workspace.canvas.page_id == detail
    assert workspace.scene.selected_route_ids() == (route_id,)
    assert workspace._selected_route_ids == (route_id,)
    fields = {field.key: field.value for field in workspace._property_fields()[0]}
    assert fields["equipment.name"] == "КЛ промысла"
    assert workspace.view.viewport().rect().intersects(
        workspace.view.mapFromScene(workspace.scene._route_items_by_id[route_id].sceneBoundingRect()).boundingRect())
    assert _state(controller) == before


def test_tree_mixed_multiselect_scene_sync_and_refresh_preserve_selection(workspace_factory):
    controller, _, detail, first, second, route_id = _network()
    controller.set_active_page(detail)
    workspace = workspace_factory(controller)
    panel = workspace.side_panel
    _click(panel, _item(panel, panel.REPRESENTATION_ROLE, first.representation_id))
    _click(panel, _item(panel, panel.REPRESENTATION_ROLE, second.representation_id), Qt.KeyboardModifier.ControlModifier)
    _click(panel, _item(panel, panel.ROUTE_ROLE, route_id), Qt.KeyboardModifier.ControlModifier)
    assert set(workspace.scene.selected_representation_ids()) == {first.representation_id, second.representation_id}
    assert workspace.scene.selected_route_ids() == (route_id,)
    workspace.refresh()
    assert set(workspace.scene.selected_representation_ids()) == {first.representation_id, second.representation_id}
    assert workspace.scene.selected_route_ids() == (route_id,)
    assert _item(panel, panel.ROUTE_ROLE, route_id).isSelected()
    workspace.scene.clearSelection()
    assert not panel.project_tree.selectedItems()


@pytest.mark.parametrize("count", (2, 34))
def test_page_expansion_survives_refresh_and_search_without_opening_all_large_pages(workspace_factory, count):
    controller = _controller()
    pages = [next(iter(controller.diagram.pages))]
    pages += [controller.create_page(f"Площадка {index:02}") for index in range(1, count)]
    active = pages[-1]
    workspace = workspace_factory(controller)
    panel = workspace.side_panel
    expanded = {page for page in pages if _item(panel, panel.PAGE_ROLE, page).isExpanded()}
    assert expanded == (set(pages) if count == 2 else {active})
    _item(panel, panel.PAGE_ROLE, pages[0]).setExpanded(True)
    _item(panel, panel.PAGE_ROLE, active).setExpanded(False)
    expected = {page: _item(panel, panel.PAGE_ROLE, page).isExpanded() for page in pages}
    panel.refresh(controller)
    assert {page: _item(panel, panel.PAGE_ROLE, page).isExpanded() for page in pages} == expected
    panel.search.setText("Площадка")
    panel.refresh(controller)
    panel.search.clear()
    assert {page: _item(panel, panel.PAGE_ROLE, page).isExpanded() for page in pages} == expected


def test_search_finds_physical_line_and_preserves_page_expansion_after_clear(workspace_factory):
    controller, overview, detail, _, _, route_id = _network()
    workspace = workspace_factory(controller)
    panel = workspace.side_panel
    _item(panel, panel.PAGE_ROLE, detail).setExpanded(False)
    panel.search.setText("кл ПРОМЫСЛА")
    assert not _item(panel, panel.ROUTE_ROLE, route_id).isHidden()
    assert _item(panel, panel.PAGE_ROLE, detail).isExpanded()
    assert _item(panel, panel.PAGE_ROLE, overview).isHidden()
    panel.refresh(controller)
    panel.search.clear()
    assert not _item(panel, panel.PAGE_ROLE, detail).isExpanded()
    assert not _item(panel, panel.PAGE_ROLE, overview).isHidden()


def test_tree_search_finds_full_equipment_name_behind_short_graphical_label(workspace_factory):
    controller = _controller()
    equipment = controller.add_equipment("builtin.load", "Кустовая насосная станция · резервный ввод северной площадки",
                                         label="КНС · резерв")
    workspace = workspace_factory(controller)
    panel = workspace.side_panel
    panel.search.setText("северной площадки")
    child = _item(panel, panel.REPRESENTATION_ROLE, equipment.representation_id)
    assert child.text(0) == "КНС · резерв"
    assert not child.isHidden()
    assert "северной площадки" in child.toolTip(0)


def _vm(controller):
    return SimpleNamespace(project=SimpleNamespace(electrical_model=controller.model, diagram=controller.diagram),
                           net=SimpleNamespace(loads={}), mode_id="")


@pytest.fixture
def analysis_factory(workspace_factory):
    # The shared Qt fixture also catches exceptions from signal callbacks.
    del workspace_factory
    widgets = []
    def create(controller):
        view = AnalysisSchemeView(_vm(controller))
        view.resize(900, 700)
        view.show()
        QApplication.processEvents()
        widgets.append(view)
        return view
    yield create
    for widget in widgets:
        widget.close()


def test_analysis_starts_on_saved_page_and_combo_reaches_every_page_without_model_changes(analysis_factory):
    controller, overview, detail, *_ = _network()
    controller.set_active_page(detail)
    before = _state(controller)
    analysis = analysis_factory(controller)
    assert analysis.scene.page_id == detail
    assert analysis.page_selector.currentData() == detail.value
    analysis.page_selector.setCurrentIndex(analysis.page_selector.findData(overview.value))
    assert analysis.scene.page_id == overview
    analysis.refresh(_vm(controller))
    assert analysis.scene.page_id == overview  # an ordinary refresh preserves browsing
    analysis.page_selector.setCurrentIndex(analysis.page_selector.findData(detail.value))
    assert analysis.scene.page_id == detail
    assert _state(controller) == before


def test_analysis_equipment_and_physical_route_selection_jump_to_their_page(analysis_factory):
    controller, overview, detail, first, _, route_id = _network()
    analysis = analysis_factory(controller)
    before = _state(controller)
    analysis.set_selection("branch", first.equipment_id.value)
    assert analysis.scene.page_id == detail
    assert analysis.scene.selected_representation_ids() == (first.representation_id,)
    analysis.show_page(overview)
    line_id = controller.diagram.routes[route_id].equipment_id
    analysis.set_selection("branch", line_id.value)
    assert analysis.scene.page_id == detail
    assert analysis.page_selector.currentData() == detail.value
    assert analysis.scene.selected_route_ids() == (route_id,)
    assert _state(controller) == before


def test_analysis_repeated_node_selection_keeps_current_page_and_tracks_editor_page_change(analysis_factory):
    controller, overview, detail, *_ = _network()
    node = controller.add_electrical_node("Общая секция", voltage_class_id=U10, page_id=overview)
    duplicate = controller.place_existing_node(node.node_id, page_id=detail, x=600, y=0)
    controller.set_active_page(detail)
    analysis = analysis_factory(controller)
    analysis.set_selection("node", node.node_id.value)
    assert analysis.scene.page_id == detail
    assert analysis.scene.selected_representation_ids() == (duplicate,)
    controller.set_active_page(overview)
    analysis.refresh(_vm(controller))
    assert analysis.scene.page_id == overview
    analysis.set_selection("node", node.node_id.value)
    assert analysis.scene.selected_representation_ids() == (node.representation_id,)


def test_analysis_keeps_current_physical_route_instead_of_switching_to_remote_symbol(analysis_factory):
    controller, overview, detail, _, _, route_id = _network()
    line_id = controller.diagram.routes[route_id].equipment_id
    controller.place_existing_equipment(line_id, page_id=overview, x=0, y=0)
    controller.set_active_page(detail)
    analysis = analysis_factory(controller)
    analysis.set_selection("branch", line_id.value)
    assert analysis.scene.page_id == detail
    assert analysis.scene.selected_route_ids() == (route_id,)


def _legacy_route_project():
    from tools.autolayout import build_layout
    project = load_project(Path(__file__).resolve().parents[1] / "rza_calc/examples/four_fault_types.json")
    project.diagram = build_layout(project, lines_as_routes=True)
    controller = ProjectEditorController(project)
    route = next(row for row in controller.diagram.routes.values() if row.equipment_id is not None)
    return controller, route


def test_legacy_route_only_line_has_readonly_physical_inspector_without_conversion(workspace_factory):
    controller, route = _legacy_route_project()
    assert not controller.diagram.representations_for_equipment(route.equipment_id)
    equipment = controller.model.equipment[route.equipment_id]
    payload = controller.model.effective_equipment_properties(equipment.id)["legacy_payload"]
    workspace = workspace_factory(controller)
    before = _state(controller)
    panel = workspace.side_panel
    _click(panel, _item(panel, panel.ROUTE_ROLE, route.id))
    fields = {field.key: field for field in workspace._property_fields()[0]}
    assert fields["equipment.name"].value == equipment.name
    for key in ("length_km", "r0", "x0", "section_mm2", "material"):
        assert fields["legacy.line." + key].value == payload[key]
        assert not fields["legacy.line." + key].editable
    assert fields["service.route_id"].value == route.id.value
    assert not any(field.editable for field in fields.values())
    assert _state(controller) == before
    assert route.equipment_id not in controller.model.line_sections


def test_analysis_selection_feedback_does_not_reenter_for_legacy_symbols_or_routes(analysis_factory):
    controller, route = _legacy_route_project()
    analysis = analysis_factory(controller)
    requests = []
    def reflect(kind, object_id):
        requests.append((kind, object_id))
        assert len(requests) <= 2, "Selection reflection must not recursively re-emit itself"
        analysis.set_selection(kind, object_id)
    analysis.selectionRequested.connect(reflect)
    before = _state(controller)
    symbol = next(row for row in controller.diagram.representations.values() if row.equipment_id is not None)
    analysis.scene.select_representations((symbol.id,))
    assert len(requests) == 1
    analysis.scene.clearSelection()
    analysis.scene._route_items_by_id[route.id].setSelected(True)
    assert len(requests) == 2
    assert requests[-1] == ("branch", controller.model.equipment[route.equipment_id].extensions["legacy_calculation"]["legacy_id"])
    assert analysis.scene.selected_route_ids() == (route.id,)
    assert _state(controller) == before
