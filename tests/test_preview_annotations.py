"""Readable mini labels are display-only and retain exact canonical anchors."""
from dataclasses import replace
from pathlib import Path
import hashlib
import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QHelpEvent
from PySide6.QtWidgets import QApplication, QToolTip

from rza_calc.application.network_overview import build_network_overview
from rza_calc.domain.fingerprint import electrical_model_fingerprint
from rza_calc.gui.diagram_preview import DiagramPreviewWidget
from rza_calc.gui.preview_annotations import PreviewAnnotation, make_preview_annotations
from rza_calc.io.project import load_project

EXAMPLE = Path(__file__).resolve().parents[1] / 'rza_calc/examples/compact_training.json'


@pytest.fixture(scope='module', autouse=True)
def app():
    yield QApplication.instance() or QApplication([])


@pytest.fixture
def sample():
    source = EXAMPLE.read_bytes()
    project = load_project(EXAMPLE)
    overview = build_network_overview(project)
    yield project, overview
    assert hashlib.sha256(EXAMPLE.read_bytes()).digest() == hashlib.sha256(source).digest()


def test_actual_transformer_units_feeder_number_only_and_section_identity(sample):
    project, overview = sample
    model, document = project.electrical_model, project.diagram
    before = electrical_model_fingerprint(model)
    facility = overview.facilities[-1]
    rows = make_preview_annotations(document, model, facility.target, facility.preferred_page_id)
    transformers = [row for row in rows if row.key.startswith('transformer:')]
    assert len(transformers) == 2
    assert {row.text for row in transformers} == {'Т1 · 1 МВА\n10/0,4 кВ', 'Т2 · 1 МВА\n10/0,4 кВ'}
    assert all('Сохранённые исходные данные' in row.tooltip for row in transformers)
    assert all('учебное допущение' in row.tooltip for row in transformers)
    sections = [row for row in rows if row.key.startswith('section:')]
    assert {row.text for row in sections} == {'СШ 1 · 10 кВ', 'СШ 2 · 10 кВ', 'СШ 1 · 0,4 кВ', 'СШ 2 · 0,4 кВ'}
    feeders = [row for row in rows if row.key.startswith('feeder:')]
    assert len(feeders) == facility.outgoing_count == 2
    assert {row.text for row in feeders} == {'Ф1', 'Ф2'}
    assert all('номер в предпросмотре' in row.tooltip and 'нагрузка' in row.tooltip for row in feeders)
    assert all('СШ' in row.tooltip for row in feeders)
    assert electrical_model_fingerprint(model) == before
    assert project.diagram is document


def test_section_names_do_not_choose_electrical_anchors_and_filter_keeps_feeder_numbers(sample):
    project, overview = sample
    target = overview.facilities[0].target
    ru = target.children[1]
    section = ru.children[0]
    renamed = replace(section, name='Произвольное название секции')
    original = make_preview_annotations(project.diagram, project.electrical_model, section, target.preferred_page_id)
    changed = make_preview_annotations(project.diagram, project.electrical_model, renamed, target.preferred_page_id)
    a = next(row for row in original if row.key.startswith('section:'))
    b = next(row for row in changed if row.key.startswith('section:'))
    assert a.anchor == b.anchor and b.text.startswith('Произвольное название')
    # Losing the explicit anchor must not fallback to a matching node name.
    assert not any(row.key.startswith('section:') for row in make_preview_annotations(
        project.diagram, project.electrical_model, replace(section, anchor_node_ids=()), target.preferred_page_id))
    whole = make_preview_annotations(project.diagram, project.electrical_model, target, target.preferred_page_id)
    filtered = make_preview_annotations(project.diagram, project.electrical_model, ru, target.preferred_page_id)
    whole_numbers = {row.key: row.text for row in whole if row.key.startswith('feeder:')}
    assert all(whole_numbers[row.key] == row.text for row in filtered if row.key.startswith('feeder:'))
    scopes = (ru.children[1], *(bay for bay in ru.children[1].children if bay.role == 'outgoing'))
    for scope in scopes:
        focused = make_preview_annotations(project.diagram, project.electrical_model, scope,
            target.preferred_page_id, numbering_context=target)
        assert any(row.key.startswith('feeder:') for row in focused)
        assert all(whole_numbers[row.key] == row.text for row in focused if row.key.startswith('feeder:'))


def test_extreme_numeric_input_is_unknown_instead_of_crashing():
    from rza_calc.gui.preview_annotations import _number
    assert _number(10**400) is None
    assert _number(float('inf')) is None
    assert _number(True) is None


def test_unknown_power_and_partial_voltage_are_explicit_and_extension_provenance_wins(sample):
    project, overview = sample
    facility = overview.facilities[-1]
    model = project.electrical_model
    equipment = next(e for e in model.equipment.values() if e.id in facility.target.equipment_ids
        and model.equipment_type(e.type_id,e.type_version).behavior_key == 'legacy.transformer_2w')
    payload = dict(equipment.properties['legacy_payload'])
    payload.update(s_nom=None, u_lv=None)
    model._equipment[equipment.id] = replace(equipment, properties={'legacy_payload':payload},
        extensions={**equipment.extensions, 'rza_calc.parameter_provenance': {
            'u_hv': {'confirmation':'unconfirmed', 'source':'Проверить реальный паспорт'}}})
    row = next(row for row in make_preview_annotations(project.diagram, model, facility.target, facility.preferred_page_id)
               if row.key == 'transformer:' + equipment.id.value)
    assert 'мощность не задана' in row.text and 'Напряжения: не все заданы' in row.text
    assert '10/? кВ' in row.tooltip
    assert 'u_hv: не подтверждено' in row.tooltip and 'Проверить реальный паспорт' in row.tooltip


def test_effective_properties_are_used_for_inherited_transformer_data(sample, monkeypatch):
    project, overview = sample
    model = project.electrical_model
    original = model.effective_equipment_properties
    calls = []
    def inherited(eid):
        calls.append(eid)
        result = dict(original(eid))
        if 'legacy_payload' in result:
            result['legacy_payload'] = {**result['legacy_payload'], 's_nom': 630}
        return result
    monkeypatch.setattr(model, 'effective_equipment_properties', inherited)
    facility = overview.facilities[-1]
    rows = make_preview_annotations(project.diagram,model,facility.target,facility.preferred_page_id)
    assert len(calls) == 2
    assert all('630 кВА' in row.text for row in rows if row.key.startswith('transformer:'))


def test_fixed_screen_collision_layout_tooltip_escape_and_readonly_refresh(sample, monkeypatch):
    project, overview = sample
    facility = overview.facilities[-1]
    widget = DiagramPreviewWidget()
    widget.resize(440, 360)
    widget.set_project(project.diagram,project.electrical_model)
    widget.show()
    widget.set_page(facility.preferred_page_id)
    rows = make_preview_annotations(project.diagram,project.electrical_model,facility.target,facility.preferred_page_id)
    rows = (replace(rows[0],tooltip='<img src=x> & реальное имя\nВторая строка'), *rows[1:])
    before = electrical_model_fingerprint(project.electrical_model)
    document = project.diagram
    try:
        widget.set_annotations(rows)
        QApplication.processEvents()
        assert widget.view.annotation_font.pixelSize() == 11
        layout = widget.view.annotation_layout()
        assert len(layout) == len(rows)
        assert all(widget.view.viewport().rect().contains(box.toAlignedRect()) for _,box in layout)
        assert not any(a.intersects(b) for i,(_,a) in enumerate(layout) for _,b in layout[i+1:])
        sizes = {row.key: box.size() for row,box in layout}
        painted = widget.grab().toImage()
        assert not painted.isNull()
        calls = []
        monkeypatch.setattr(QToolTip, 'showText', lambda *args: calls.append(args))
        row,box = next((row,box) for row,box in layout if row.key == rows[0].key)
        point = box.center().toPoint()
        assert widget.view.annotation_at(point) is row
        event = QHelpEvent(QEvent.Type.ToolTip,point,widget.view.viewport().mapToGlobal(point))
        QApplication.sendEvent(widget.view.viewport(),event)
        assert calls and '&lt;img src=x&gt; &amp;' in calls[0][1]
        assert '<img' not in calls[0][1] and '<br>' in calls[0][1]
        assert widget.view.isInteractive() is False
        widget.resize(540,440)
        QApplication.processEvents()
        assert {row.key:box.size() for row,box in widget.view.annotation_layout()} == sizes
        assert electrical_model_fingerprint(project.electrical_model) == before and project.diagram is document
        widget.clear()
        assert widget.view.annotation_layout() == ()
    finally:
        widget.close()
        widget.deleteLater()
        QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)


def test_dense_annotations_have_explicit_overflow_without_font_shrink(sample):
    project, overview = sample
    facility = overview.facilities[0]
    widget = DiagramPreviewWidget()
    widget.resize(250,260)
    widget.set_project(project.diagram,project.electrical_model)
    widget.show()
    widget.set_page(facility.preferred_page_id)
    try:
        QApplication.processEvents()
        center = widget.view.mapToScene(widget.view.viewport().rect().center())
        rows = tuple(PreviewAnnotation(str(i),(center.x(),center.y()),'Секция '+str(i)+' · 110 кВ','Длинное имя '+str(i)) for i in range(30))
        widget.set_annotations(rows)
        layout = widget.view.annotation_layout()
        assert any(row.key=='overflow' and 'выберите РУ' in row.text for row,_ in layout)
        assert not any(a.intersects(b) for i,(_,a) in enumerate(layout) for _,b in layout[i+1:])
        assert widget.view.annotation_font.pixelSize()==11
        overflow = next(row for row,_ in layout if row.key=='overflow')
        visible = {row.key for row,_ in layout if row.key!='overflow'}
        assert all(('Длинное имя '+row.key) in overflow.tooltip for row in rows if row.key not in visible)
    finally:
        widget.close()
        widget.deleteLater()
        QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)


@pytest.mark.parametrize('windings', [2,3])
def test_magnified_transformer_uses_native_windings_exact_anchor_and_saved_rotation(windings):
    from rza_calc.application.network_overview import OverviewTarget
    from test_stage4_editor_interaction import _controller
    controller = _controller()
    edit = controller.add_equipment(f'builtin.transformer_{windings}w', 'Т1',x=160,y=280)
    model,document = controller.model,controller.diagram
    rep=document.representations[edit.representation_id]
    target=OverviewTarget('transformer','equipment','Т1',equipment_ids=(edit.equipment_id,))
    before=electrical_model_fingerprint(model)
    widget=DiagramPreviewWidget()
    widget.resize(330,330)
    widget.set_project(document,model)
    widget.show()
    widget.set_page(rep.page_id)
    try:
        rows=make_preview_annotations(document,model,target,rep.page_id)
        row=rows[0]
        assert row.glyph==f'transformer_{windings}w'
        assert row.anchor==(rep.x,rep.y) and row.representation_id==rep.id
        widget.set_annotations(rows)
        QApplication.processEvents()
        path=widget.view.annotation_glyph_path(row)
        assert max(path.boundingRect().width(),path.boundingRect().height())==pytest.approx(32)
        assert path.boundingRect().center()==QPointF(16,16)
        actual=widget.scene._items_by_id[rep.id]
        assert sum(p.kind=='circle' for p in actual.symbol_geometry().primitives)==windings
        layout=widget.view.annotation_layout()
        box=next(box for item,box in layout if item.key==row.key)
        assert box.width()>=75 and box.height()>=40
        # Changing only the saved rotation refreshes the native silhouette.
        rotated=replace(document,representations={**document.representations,
            rep.id:replace(rep,rotation_deg=rep.rotation_deg+90)})
        widget.set_project(rotated,model)
        updated=widget.view.annotation_glyph_path(row)
        assert updated.boundingRect().width()==pytest.approx(path.boundingRect().height())
        assert updated.boundingRect().height()==pytest.approx(path.boundingRect().width())
        assert model is controller.model and controller.diagram is document
        assert electrical_model_fingerprint(model)==before
    finally:
        widget.close()
        widget.deleteLater()
        QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)


@pytest.mark.parametrize('facility_index', [2,3])
def test_whole_ps_ktp_at_330_pixels_keeps_both_transformers_visible(sample,facility_index):
    project,overview=sample
    facility=overview.facilities[facility_index]
    widget=DiagramPreviewWidget()
    widget.resize(330,280)
    widget.set_project(project.diagram,project.electrical_model)
    widget.show()
    widget.set_page(facility.preferred_page_id)
    rows=make_preview_annotations(project.diagram,project.electrical_model,facility.target,facility.preferred_page_id)
    try:
        widget.set_annotations(rows)
        QApplication.processEvents()
        layout=widget.view.annotation_layout()
        actual={row.key for row,box in layout if row.glyph}
        expected={row.key for row in rows if row.glyph}
        assert len(expected)==2 and actual==expected
        assert not any(a.intersects(b) for i,(_,a) in enumerate(layout) for _,b in layout[i+1:])
        for row,box in layout:
            if row.glyph:
                assert max(widget.view.annotation_glyph_path(row).boundingRect().width(),
                           widget.view.annotation_glyph_path(row).boundingRect().height())==pytest.approx(32)
                rep=project.diagram.representations[row.representation_id]
                assert row.anchor==(rep.x,rep.y)
                assert widget.view.annotation_at(box.center()) is row
    finally:
        widget.close()
        widget.deleteLater()
        QCoreApplication.sendPostedEvents(None,QEvent.Type.DeferredDelete)
