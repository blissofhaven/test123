"""Bus contact graphics must outlive neither their page nor their Qt parent."""
from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPointF
from PySide6.QtWidgets import QApplication
from shiboken6 import isValid

from rza_calc.gui.editor_scene import DiagramGraphicsScene
from test_oilfield_page_navigation import _state, analysis_factory, workspace_factory
from test_bus_connection_spacing import PAGE, U10, _setup, _load, _connect


def _two_bus_pages():
    controller, bus = _setup(width=240)
    _connect(controller, bus, _load(controller, 150), .3)
    _connect(controller, bus, _load(controller, 650), .7)
    other = controller.create_page("Другая площадка")
    second = controller.add_electrical_node("Вторая шина", page_id=other, symbol_key="busbar",
                                            width=240, height=12, x=400, y=0, voltage_class_id=U10)
    for index, x in enumerate((150, 650)):
        load = controller.add_equipment("builtin.load", f"Потребитель {index}", page_id=other,
                                        x=x, y=240, voltage_class_by_group={"main": U10})
        controller.connect_port_to_node(load.port_ids[0], second.node_id, page_id=other,
            source_representation_id=load.representation_id, node_representation_id=second.representation_id,
            target_anchor_key=str(.3 + .4 * index))
    controller.set_active_page(PAGE)
    return controller, other


@pytest.mark.parametrize("surface", ("scene", "editor", "analysis"))
def test_repeated_bus_page_switch_cleans_handles_before_qt_parent_is_removed(workspace_factory, analysis_factory, surface):
    controller, other = _two_bus_pages()
    before = _state(controller)
    if surface == "scene":
        scene = DiagramGraphicsScene()
        show = lambda page: scene.sync_document(controller.diagram, controller.model, page_id=page)
    elif surface == "editor":
        workspace = workspace_factory(controller)
        scene = workspace.scene
        show = workspace._tree_page_selection
    else:
        analysis = analysis_factory(controller)
        scene = analysis.scene
        show = lambda page: analysis.page_selector.setCurrentIndex(analysis.page_selector.findData(page.value))
    for page in (PAGE, other, PAGE, other, PAGE):
        show(page)
        QApplication.processEvents()
        assert scene.page_id == page
        assert len(scene._bus_attachment_handles) == 2
        for (route_id, at_start), handle in scene._bus_attachment_handles.items():
            assert isValid(handle)
            assert handle.scene() is scene
            owner = handle.parentItem()
            route = controller.diagram.routes[route_id]
            anchor = route.start_anchor if at_start else route.end_anchor
            assert owner is scene._items_by_id[anchor.representation_id]
            assert owner.representation.page_id == page
        # A test-held parent wrapper would keep its C++ children alive and
        # hide precisely the deletion ordering exercised by real navigation.
        del owner, handle
    assert _state(controller) == before


def test_page_switch_discards_active_bus_contact_preview_before_parent_cleanup(workspace_factory):
    controller, other = _two_bus_pages()
    workspace = workspace_factory(controller)
    scene = workspace.scene
    before = _state(controller)
    handle = next(iter(scene._bus_attachment_handles.values()))
    start = handle.scenePos()
    assert scene.begin_connected_drag(handle, start)
    scene.update_connected_drag(start + QPointF(20, 0))
    assert scene._connected_drag is not None and scene._connected_drag.preview_routes
    workspace._tree_page_selection(other)
    assert scene._connected_drag is None
    assert scene.mouseGrabberItem() is None
    assert handle.scene() is None
    scene.finish_connected_drag()  # a delayed mouse release has no owner to commit
    workspace._tree_page_selection(PAGE)
    assert _state(controller) == before
