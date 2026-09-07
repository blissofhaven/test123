"""User-entered line templates participate in one project transaction."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from rza_calc.domain.catalog import CatalogCategoryId, CatalogEntry, CatalogEntryId, CatalogOrigin, UserCatalog
from rza_calc.domain.catalog_snapshot import ProjectCatalogSnapshots
from rza_calc.domain.diagram import DiagramDocument, DiagramPage, PageId
from rza_calc.domain.electrical import ElectricalModel, EquipmentTypeId
from rza_calc.domain.fingerprint import electrical_model_fingerprint
from rza_calc.editor.history import ProjectCommandHistory, ProjectHistoryError


def _entry(key='one'):
    return CatalogEntry(CatalogEntryId('catalog.user.line.'+key), CatalogOrigin.USER,
        CatalogCategoryId('lines'), 'Марка '+key, EquipmentTypeId('builtin.line_section.cable'),1,
        properties={'conductor_mark':'Марка '+key,'r1_ohm_per_km':.2,'x1_ohm_per_km':.08},
        source='Введено пользователем')


def _project(with_catalog=True):
    project=SimpleNamespace(electrical_model=ElectricalModel.with_builtins('Каталог'),
        diagram=DiagramDocument.create('Схема',(DiagramPage(PageId('page.catalog'),'Схема'),)),
        catalog_snapshots=ProjectCatalogSnapshots())
    if with_catalog:project.user_catalog=UserCatalog()
    return project


def test_catalog_edit_undo_redo_is_isolated_from_circuit_and_later_draft_changes():
    project=_project();history=ProjectCommandHistory(project)
    before=electrical_model_fingerprint(project.electrical_model)
    retained=[];entry=_entry()
    def add(draft):
        retained.append(draft.user_catalog)
        draft.user_catalog.add(entry)
    result=history.execute('Добавить марку',add)
    assert result.change.catalog_changed and not result.change.electrical_changed
    assert not result.change.diagram_changed
    assert project.user_catalog.get(entry.id)==entry
    retained[0].add(_entry('later'))
    assert len(project.user_catalog.entries)==1
    history.undo()
    assert not project.user_catalog.entries
    history.redo()
    assert tuple(project.user_catalog.entries)==(entry.id,)
    assert electrical_model_fingerprint(project.electrical_model)==before
    assert len(history.journal)==3


def test_failed_command_does_not_leak_template_or_history():
    project=_project();history=ProjectCommandHistory(project)
    def fail(draft):
        draft.user_catalog.add(_entry())
        raise ValueError('cancel')
    with pytest.raises(ValueError,match='cancel'):history.execute('Отмена',fail)
    assert not project.user_catalog.entries and not history.can_undo
    assert not history.journal


def test_invalid_template_is_rejected_before_any_project_write():
    project=_project();history=ProjectCommandHistory(project)
    invalid=replace(_entry(),equipment_type_id=EquipmentTypeId('missing.line.type'))
    with pytest.raises(ProjectHistoryError,match='справочника'):
        history.execute('Ошибка',lambda draft:draft.user_catalog.add(invalid))
    assert not project.user_catalog.entries and not history.journal


def test_external_in_place_catalog_change_cannot_be_overwritten_by_undo():
    project=_project();history=ProjectCommandHistory(project)
    history.execute('Марка',lambda draft:draft.user_catalog.add(_entry()))
    project.user_catalog.add(_entry('external'))
    with pytest.raises(ProjectHistoryError):history.undo()
    assert len(project.user_catalog.entries)==2 and history.can_undo


def test_projects_without_user_catalog_keep_the_previous_command_contract():
    project=_project(False);history=ProjectCommandHistory(project)
    result=history.execute('Без изменения',lambda draft:None)
    assert not result.change.catalog_changed
    assert not hasattr(project,'user_catalog')
