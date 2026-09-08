"""One immutable preview and one transaction for cards, tables and catalog diffs.

Only the canonical model is edited. The calculation DTO, saved topology and
catalog source entries are never edited as a side effect of parameter input.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal
import math
from types import MappingProxyType
from typing import Any, Mapping

from rza_calc.domain.catalog import CatalogEntry
from rza_calc.domain.catalog_snapshot import CatalogBinding, CatalogEntrySnapshot, ParameterOverride
from rza_calc.domain.electrical import (
    DataConfirmation, DomainInvariantError, EquipmentId, LineConstructionSegmentId,
    LineSection, PortId, SwitchPosition, _freeze_json, thaw_json,
)
from .history import ProjectDraft, ProjectMemento
from .parameter_schema import FieldSpec, schemas_for, validate_field_value, parameter_readiness
from rza_calc.domain.catalog_compatibility import parameter_family

PROVENANCE_KEY = "rza_calc.parameter_provenance"
_ABSENT = object()


@dataclass(frozen=True, slots=True)
class ParameterValue:
    value: Any
    source: str = ""
    confirmation: DataConfirmation = DataConfirmation.UNCONFIRMED
    origin: str = "instance"

    def __post_init__(self):
        object.__setattr__(self, "value", _freeze_json(self.value))
        object.__setattr__(self, "confirmation", DataConfirmation(self.confirmation))
        if not isinstance(self.source, str) or not isinstance(self.origin, str):
            raise TypeError("Источник и происхождение параметра должны быть строками.")


def is_manual_override(value):
    """Explicit local input remains manual even when its source is missing."""
    return value.value is not None and value.origin in {"instance","manual","segment"}


@dataclass(frozen=True, slots=True)
class ParameterRef:
    equipment_id: EquipmentId
    key: str
    segment_id: LineConstructionSegmentId | None = None


@dataclass(frozen=True, slots=True)
class ParameterPatch:
    equipment_id: EquipmentId
    values: Mapping[str, ParameterValue] = field(default_factory=dict)
    clear: frozenset[str] = frozenset()
    segment_id: LineConstructionSegmentId | None = None

    def __post_init__(self):
        if not isinstance(self.equipment_id, EquipmentId):
            raise TypeError("Для изменения параметров нужен постоянный EquipmentId.")
        if any(not isinstance(k, str) or not k or not isinstance(v, ParameterValue)
               for k, v in self.values.items()):
            raise TypeError("values должен содержать именованные ParameterValue.")
        clear = frozenset(self.clear)
        if any(not isinstance(k, str) or not k for k in clear):
            raise TypeError("clear должен содержать имена параметров.")
        if clear & set(self.values):
            raise ValueError("Один параметр нельзя одновременно изменить и удалить.")
        object.__setattr__(self, "values", MappingProxyType(dict(self.values)))
        object.__setattr__(self, "clear", clear)


@dataclass(frozen=True, slots=True)
class ParameterIssue:
    equipment_id: EquipmentId | None
    key: str
    code: str
    message: str
    severity: str = "error"
    fault_types: tuple[str, ...] = ()
    segment_id: LineConstructionSegmentId | None = None

    @property
    def ref(self):
        return ParameterRef(self.equipment_id,self.key,self.segment_id)


@dataclass(frozen=True, slots=True)
class EquipmentSnapshot:
    equipment_id: EquipmentId
    name: str
    type_id: Any
    type_version: int
    fields: Mapping[str, ParameterValue]
    specs: tuple[FieldSpec, ...]
    stamp: Any
    segment_id: LineConstructionSegmentId | None = None
    segments: tuple[LineConstructionSegmentId, ...] = ()
    readiness: tuple = ()
    family: str = ""


@dataclass(frozen=True, slots=True)
class FieldChange:
    equipment_id: EquipmentId
    key: str
    before: ParameterValue
    after: ParameterValue
    segment_id: LineConstructionSegmentId | None = None

    @property
    def ref(self):
        return ParameterRef(self.equipment_id, self.key, self.segment_id)


@dataclass(frozen=True, slots=True)
class _Stamp:
    owner: Any = field(repr=False, compare=False)
    state: ProjectMemento = field(repr=False)


@dataclass(frozen=True, slots=True)
class ParameterPreview:
    valid: bool
    changes: tuple[FieldChange, ...] = ()
    diagnostics: tuple[ParameterIssue, ...] = ()
    readiness: tuple = ()
    stamp: Any = field(default=None, repr=False)
    _candidate: ProjectMemento | None = field(default=None, repr=False, compare=False)

    @property
    def errors(self):
        return tuple(p for p in self.diagnostics if p.severity == "error")


@dataclass(frozen=True, slots=True)
class ParameterBatchResult:
    equipment_ids: tuple[EquipmentId, ...]
    changes: tuple[FieldChange, ...]
    readiness: tuple = ()


@dataclass(frozen=True, slots=True)
class CatalogUpdatePreview:
    changes: tuple[FieldChange, ...]
    incompatible: tuple[ParameterIssue, ...]
    stamp: Any
    entry: CatalogEntry
    equipment_ids: tuple[EquipmentId, ...]
    codecs: Mapping[EquipmentId, str | None] = field(default_factory=dict)

    @property
    def valid(self):
        return not self.incompatible


def _stamp(controller):
    return _Stamp(controller, ProjectMemento.capture(controller._project))


def _require_current(controller, stamp):
    from .controller import EditorCommandError
    if not isinstance(stamp, _Stamp) or stamp.owner is not controller or stamp.state != ProjectMemento.capture(controller._project):
        raise EditorCommandError("Проект изменился после предварительного просмотра. Проверьте изменения заново.")


def _read_path(root, path, default=None):
    current = root
    for part in path:
        if isinstance(current, Mapping):
            if part not in current:
                return default
            current = current[part]
        elif isinstance(current, (tuple, list)) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return default
    return current


def _write_path(root, path, value, *, clear=False):
    if not path:
        raise ValueError("Параметр не имеет явного пути хранения.")
    def write(current, parts):
        key, *tail = parts
        if isinstance(current, list):
            if not key.isdigit():
                raise ValueError("Некорректный индекс составного параметра.")
            index = int(key)
            if index > 16:
                raise ValueError("Недопустимый индекс составного параметра.")
            while len(current) <= index:
                current.append(None)
            if tail:
                if current[index] is None:
                    current[index] = [] if tail[0].isdigit() else {}
                write(current[index], tail)
            else:
                current[index] = None if clear else thaw_json(value)
        elif isinstance(current, dict):
            if tail:
                if current.get(key) is None:
                    current[key] = [] if tail[0].isdigit() else {}
                write(current[key], tail)
            elif clear:
                current.pop(key, None)
            else:
                current[key] = thaw_json(value)
        else:
            raise ValueError("Повреждена структура составного параметра.")
    write(root, list(path))


def _convert_storage(spec,value,*,to_storage):
    multiplier=Decimal(getattr(spec,"storage_multiplier","1"))
    if value is None or multiplier==1:
        return value
    number=Decimal(str(value))
    converted=number*multiplier if to_storage else number/multiplier
    if not to_storage and spec.value_kind=="integer":
        if converted!=converted.to_integral_value():
            nearest=converted.to_integral_value()
            tolerance=Decimal(str(math.ulp(value)))*2/abs(multiplier) if isinstance(value,float) else Decimal(0)
            if tolerance>=Decimal("0.5") or abs(converted-nearest)>tolerance:
                raise ValueError("Сохранённую длину нельзя точно представить целым числом миллиметров.")
            converted=nearest
        return int(converted)
    result=float(converted)
    if Decimal(str(result))!=converted:
        raise ValueError("Значение нельзя сохранить без потери точности единиц измерения.")
    return result


def _specs(model, equipment, segment_id):
    specs = schemas_for(model, equipment)
    section = model.line_sections.get(equipment.id)
    if segment_id is not None:
        if section is None or segment_id not in {s.id for s in section.construction_segments}:
            raise ValueError("Конструктивный участок не принадлежит выбранной ветви.")
        return tuple(replace(s, scope="segment") if s.scope == "equipment" else
                     replace(s, scope="segment_geometry", editable=True) for s in specs
                     if s.scope in {"equipment", "line_section"})
    if section is not None and len(section.construction_segments) != 1:
        specs = tuple(replace(s, editable=False) if s.scope == "line_section" else s for s in specs)
    return tuple(specs)


def _ct_port_value(model,equipment):
    if "rza_calc.ct_port_id" in equipment.extensions:
        return equipment.extensions["rza_calc.ct_port_id"]
    saved=equipment.extensions.get("editor_legacy_ct_ports",{}).get("ct_node",{})
    if isinstance(saved,Mapping) and saved.get("port_id"):
        return saved["port_id"]
    payload=equipment.properties.get("legacy_payload",equipment.properties)
    node_id=payload.get("ct_node") if isinstance(payload,Mapping) else None
    if node_id is None:
        return None
    from .legacy_ct import _node_identity_or_none
    cache={}
    candidates=[pid.value for pid in equipment.port_ids if model.node_for_port(pid) is not None
        and _node_identity_or_none(model,model.node_for_port(pid).id,cache)==node_id]
    return candidates[0] if len(candidates)==1 else None


def _set_ct_port(model,equipment,properties,extensions,value):
    if value is None:
        identity=None
        port=None
    else:
        port=PortId(value)
        if port not in equipment.port_ids:
            raise ValueError("Выбранный вывод ТТ не принадлежит аппарату.")
        node=model.node_for_port(port)
        if node is None:
            raise ValueError("Для задания стороны ТТ сначала подключите выбранный вывод.")
        from .legacy_ct import _node_identity
        identity=_node_identity(model,node.id,{})
    if str(equipment.type_id).startswith("compat.rza_calc."):
        properties["legacy_payload"]["ct_node"]=identity
        slots=extensions.setdefault("editor_legacy_ct_ports",{})
        if port is None:
            slots.pop("ct_node",None)
        else:
            slots["ct_node"]={"port_id":port.value,"role":model.ports[port].role,"node_id":identity}
        if not slots:
            extensions.pop("editor_legacy_ct_ports",None)
    else:
        extensions["rza_calc.ct_port_id"]=port.value if port is not None else None
        properties.pop("ct_node",None)


def _field_provenance(equipment, segment, key, path):
    local = segment.extensions.get(PROVENANCE_KEY, {}) if segment is not None else {}
    if isinstance(local, Mapping) and key in local:
        return local[key]
    # A segment's own value cannot borrow the confirmation of a different
    # value at equipment level. Removing that override restores inheritance.
    if segment is not None and _read_path(segment.properties, path, _ABSENT) is not _ABSENT:
        return {}
    inherited = equipment.extensions.get(PROVENANCE_KEY, {})
    return inherited.get(key, {}) if isinstance(inherited, Mapping) else {}


def _values(model, equipment, specs, segment_id=None):
    effective = model.effective_equipment_properties(equipment.id)
    section = model.line_sections.get(equipment.id)
    segment = next((s for s in section.construction_segments if s.id == segment_id), None) if section else None
    if segment is not None:
        effective = model.effective_line_construction_segment_properties(equipment.id, segment.id)
    values = {}
    for spec in specs:
        path = spec.storage_path or (spec.key,)
        if spec.scope == "identity":
            value = getattr(equipment, path[0], None)
        elif spec.scope == "ct_port":
            value = _ct_port_value(model,equipment)
        elif spec.scope in {"nameplate","ct"}:
            namespace="rza_calc.nameplate" if spec.scope=="nameplate" else "rza_calc.ct_parameters"
            value = _read_path(equipment.extensions.get(namespace,{}),path,_ABSENT)
            if value is _ABSENT:
                value = _read_path(effective,path) if spec.scope=="ct" else None
        elif spec.scope == "line_section":
            value = getattr(section, path[0], None) if section else None
        elif spec.scope == "segment_geometry":
            value = getattr(segment, path[0], None) if segment else None
        else:
            value = _read_path(effective, path)
        if spec.key=="line_type" and section is not None:
            value=model.logical_lines[section.logical_line_id].line_kind.value
        value=_convert_storage(spec,value,to_storage=False)
        info = _field_provenance(equipment, segment, spec.key, path)
        status = info.get("confirmation", DataConfirmation.UNCONFIRMED.value) if isinstance(info, Mapping) else "unconfirmed"
        source = info.get("source", "") if isinstance(info, Mapping) else ""
        path_root=segment.properties if segment is not None else equipment.properties
        definition=model.equipment_type(equipment.type_id,equipment.type_version)
        inferred_origin=("legacy" if definition.behavior_key.startswith("legacy.") else
                         "segment" if segment is not None and _read_path(path_root,path,_ABSENT) is not _ABSENT else
                         "instance" if _read_path(path_root,path,_ABSENT) is not _ABSENT else
                         "line" if section is not None else "type")
        origin = info.get("origin", inferred_origin) if isinstance(info, Mapping) else inferred_origin
        # Existing confirmed physical records keep their explicit status, but
        # no imported nameplate value is automatically considered verified.
        if not info and section is not None:
            segments = (segment,) if segment else section.construction_segments
            if spec.scope in {"line_section", "segment_geometry"}:
                status = "confirmed" if all(s.length_confirmation is DataConfirmation.CONFIRMED for s in segments) else "unconfirmed"
            elif spec.key in {"r1_ohm_per_km", "x1_ohm_per_km"}:
                status = "confirmed" if all(s.impedance_confirmation is DataConfirmation.CONFIRMED for s in segments) else "unconfirmed"
        values[spec.key] = ParameterValue(value, source, status, origin)
    return MappingProxyType(values)


def equipment_snapshot(controller, equipment_id, segment_id=None, *, stamp=None):
    model = controller.model
    equipment = model.equipment[equipment_id]
    specs = _specs(model, equipment, segment_id)
    values = _values(model, equipment, specs, segment_id)
    binding=controller._project.catalog_snapshots.bindings.get(equipment_id)
    if binding is not None and segment_id is None:
        from rza_calc.domain.catalog_compatibility import catalog_values
        baseline=catalog_values(model,equipment,binding.entry)
        provenance=equipment.extensions.get(PROVENANCE_KEY,{})
        values=MappingProxyType({key:replace(value,origin="catalog",source=binding.entry.source)
            if key not in provenance and key not in binding.parameter_overrides
                and value.value==baseline.get(key,_ABSENT) else value for key,value in values.items()})
    section = model.line_sections.get(equipment_id)
    return EquipmentSnapshot(equipment.id, equipment.name, equipment.type_id, equipment.type_version,
        values, specs, _stamp(controller) if stamp is None else stamp, segment_id,
        tuple(s.id for s in section.construction_segments) if section else (),
        _readiness(model, equipment, specs, values, segment_id),
        parameter_family(equipment.type_id))


def equipment_snapshots(controller,equipment_ids):
    """One consistent input capture for a table, including a large project."""
    stamp=_stamp(controller)
    return tuple(equipment_snapshot(controller,eid,stamp=stamp) for eid in equipment_ids)


def _metadata(extensions, key, value, clear):
    result = thaw_json(extensions)
    rows = result.setdefault(PROVENANCE_KEY, {})
    if not isinstance(rows, dict):
        raise ValueError("Повреждены сведения об источниках параметров.")
    if clear:
        rows.pop(key, None)
    else:
        rows[key] = {"source": value.source, "confirmation": value.confirmation.value, "origin": value.origin}
    if not rows:
        result.pop(PROVENANCE_KEY, None)
    return result


def _readiness(model, equipment, specs, values, segment_id=None):
    return tuple(ParameterIssue(equipment.id, issue.key, issue.code, issue.message,
        issue.severity, issue.fault_types, segment_id)
        for issue in parameter_readiness(specs, values, family=parameter_family(equipment.type_id)))


def _apply_patch_records(model, patches):
    equipment_records, section_records, changes, issues = {}, {}, [], []
    seen = set()
    for patch in patches:
        equipment = equipment_records.get(patch.equipment_id, model.equipment.get(patch.equipment_id))
        if equipment is None:
            issues.append(ParameterIssue(patch.equipment_id, "", "equipment_missing", "Оборудование не найдено."))
            continue
        specs = {s.key: s for s in _specs(model, equipment, patch.segment_id)}
        before = _values(model, equipment, tuple(specs.values()), patch.segment_id)
        properties, extensions = thaw_json(equipment.properties), thaw_json(equipment.extensions)
        identity = {}
        section = section_records.get(equipment.id, model.line_sections.get(equipment.id))
        segments = list(section.construction_segments) if section else []
        for key in sorted(set(patch.values) | patch.clear):
            ref = ParameterRef(equipment.id, key, patch.segment_id)
            if ref in seen:
                issues.append(ParameterIssue(equipment.id,key,"duplicate_cell","Параметр встречается в пакете дважды.",segment_id=patch.segment_id));continue
            seen.add(ref)
            spec = specs.get(key)
            value = patch.values.get(key, ParameterValue(None))
            if spec is None or not spec.editable:
                issues.append(ParameterIssue(equipment.id,key,"field_readonly","Параметр не поддерживает редактирование.",segment_id=patch.segment_id));continue
            error = validate_field_value(spec, value.value)
            if value.confirmation is DataConfirmation.CONFIRMED and (value.value is None or not value.source.strip()):
                error = "Для подтверждения нужны значение и источник данных."
            if error:
                issues.append(ParameterIssue(equipment.id,key,"invalid_value",error,segment_id=patch.segment_id));continue
            clear = key in patch.clear
            remove_override = clear and not (
                spec.scope == "ct_port" or (spec.scope == "equipment"
                    and str(equipment.type_id).startswith("compat.rza_calc.")))
            if not clear and before[key]==value:
                continue
            path = spec.storage_path or (key,)
            try:
                stored=_convert_storage(spec,value.value,to_storage=True)
                if not clear and value.value==before[key].value and spec.scope=="equipment":
                    original=_read_path(model.effective_equipment_properties(equipment.id),path,_ABSENT)
                    if original is not _ABSENT:
                        stored=original
            except (ValueError, TypeError, ArithmeticError) as exc:
                issues.append(ParameterIssue(equipment.id,key,"invalid_value",str(exc),segment_id=patch.segment_id))
                continue
            if spec.scope == "identity":
                if path[0] not in {"name", "note"}:
                    raise ValueError("Неизвестное поле идентичности оборудования.")
                identity[path[0]] = value.value if value.value is not None else ""
            elif spec.scope == "ct_port":
                try:
                    _set_ct_port(model,equipment,properties,extensions,value.value)
                except (ValueError, TypeError, RuntimeError) as exc:
                    issues.append(ParameterIssue(equipment.id,key,"invalid_ct_side",str(exc),segment_id=patch.segment_id))
                    continue
            elif spec.scope in {"nameplate","ct"}:
                namespace="rza_calc.nameplate" if spec.scope=="nameplate" else "rza_calc.ct_parameters"
                namespace_values=extensions.setdefault(namespace,{})
                if spec.scope=="ct" and len(path)>1 and path[0] not in namespace_values:
                    old_container=_read_path(model.effective_equipment_properties(equipment.id),(path[0],),_ABSENT)
                    if old_container is not _ABSENT:
                        namespace_values[path[0]]=thaw_json(old_container)
                _write_path(namespace_values,path,stored,clear=clear)
            elif spec.scope in {"line_section", "segment_geometry", "segment"}:
                if section is None:
                    raise ValueError("У аппарата нет физических участков.")
                if patch.segment_id is None and len(segments) != 1:
                    raise ValueError("Длину составной ветви меняют отдельно для каждого участка.")
                index = next((i for i,s in enumerate(segments) if s.id == patch.segment_id), 0)
                segment = segments[index]
                ext = _metadata(segment.extensions,key,value,clear)
                if spec.scope in {"line_section", "segment_geometry"}:
                    if path != ("length_mm",):
                        raise ValueError("Неизвестный физический параметр участка.")
                    segment = replace(segment,length_mm=value.value,length_confirmation=value.confirmation,extensions=ext)
                else:
                    segment_props = thaw_json(segment.properties)
                    _write_path(segment_props,path,stored,clear=clear)
                    segment = replace(segment,properties=segment_props,extensions=ext)
                segments[index] = segment
            elif spec.scope == "equipment":
                _write_path(properties,path,stored,clear=remove_override)
            else:
                raise ValueError("Неизвестная область хранения параметра.")
            if spec.scope not in {"segment", "segment_geometry"}:
                extensions = _metadata(extensions,key,value,remove_override)
            if before[key] != value or clear:
                changes.append(FieldChange(equipment.id,key,before[key],value,patch.segment_id))
        equipment_records[equipment.id] = replace(equipment,properties=properties,extensions=extensions,**identity)
        if section is not None:
            section_records[equipment.id] = LineSection(section.equipment_id,section.logical_line_id,
                extensions=section.extensions,construction_segments=segments)
    if issues:
        return (),tuple(issues)
    # Confirmation follows explicit field provenance. Editing values alone
    # cannot create a confirmed pair, and segment overrides remain independent.
    for eid, section in tuple(section_records.items()):
        equipment = equipment_records[eid]
        line = model.logical_lines[section.logical_line_id]
        rows = []
        for segment in section.construction_segments:
            old = next(s for s in model.line_sections[eid].construction_segments if s.id == segment.id)
            old_values = model.effective_line_construction_segment_properties(eid,segment.id)
            effective = {**line.inherited_properties,**equipment.properties,**segment.properties}
            changed = any(effective.get(k) != old_values.get(k) for k in ("r1_ohm_per_km","x1_ohm_per_km"))
            known = [_field_provenance(equipment, segment, k, (k,))
                     for k in ("r1_ohm_per_km","x1_ohm_per_km")]
            confirmed = all(effective.get(k) is not None for k in ("r1_ohm_per_km","x1_ohm_per_km")) and (
                all(isinstance(p,Mapping) and p.get("confirmation")=="confirmed" and p.get("source","").strip() for p in known)
                if any(known) else not changed and old.impedance_confirmation is DataConfirmation.CONFIRMED)
            rows.append(replace(segment,impedance_confirmation=DataConfirmation.CONFIRMED if confirmed else DataConfirmation.UNCONFIRMED))
        section_records[eid] = LineSection(eid,section.logical_line_id,extensions=section.extensions,construction_segments=rows)
    model.replace_parameter_records(tuple(equipment_records.values()),tuple(section_records.values()))
    # Clear means remove an override, so its resulting value can be inherited
    # from the parent line/type. Show that effective result in the diff.
    resolved=[]
    for change in changes:
        equipment=model.equipment[change.equipment_id]
        specs=_specs(model,equipment,change.segment_id)
        after=_values(model,equipment,specs,change.segment_id)[change.key]
        if change.before!=after:
            resolved.append(replace(change,after=after))
    return tuple(resolved),()


def _sync_bindings(draft, changes):
    now = datetime.now(timezone.utc)
    for eid in dict.fromkeys(c.equipment_id for c in changes):
        binding = draft.catalog_snapshots.bindings.get(eid)
        if binding is None:
            continue
        equipment = draft.electrical_model.equipment[eid]
        from rza_calc.domain.catalog_compatibility import catalog_properties_for_type
        baseline=catalog_properties_for_type(binding.entry,equipment.type_id)
        effective = draft.electrical_model.effective_equipment_properties(eid)
        overrides = {key: thaw_json(effective.get(key)) for key in set(baseline) | set(effective)
                     if effective.get(key,_ABSENT) != baseline.get(key,_ABSENT)}
        audited = dict(binding.parameter_overrides)
        extension_audit=thaw_json(binding.extensions.get("parameter_extension_overrides",{}))
        specs={s.key:s for s in schemas_for(draft.electrical_model,equipment)}
        for change in changes:
            if change.equipment_id!=eid or change.segment_id is not None:
                continue
            spec=specs.get(change.key)
            if spec is not None and spec.scope in {"ct","nameplate"}:
                from rza_calc.domain.catalog_compatibility import catalog_values
                source=catalog_values(draft.electrical_model,equipment,binding.entry).get(change.key)
                if change.after.value==source:
                    extension_audit.pop(change.key,None)
                else:
                    extension_audit[change.key]={"scope":spec.scope,"path":list(spec.storage_path),
                        "source_value":thaw_json(source),"override_value":thaw_json(change.after.value),
                        "reason":"Изменено в карточке параметров", "source":change.after.source or "Явный ввод параметров",
                        "modified_at":now.isoformat()}
                continue
            if spec is None or spec.scope!="equipment":
                continue
            source=_convert_storage(spec,_read_path(baseline,spec.storage_path or (spec.key,)),to_storage=False)
            if change.after.value==source:
                audited.pop(change.key,None)
            else:
                audited[change.key]=ParameterOverride(change.key,source,change.after.value,
                    "Изменено в карточке параметров",change.after.source or "Явный ввод параметров",now)
        extensions={**binding.extensions,"parameter_schema":"v1"}
        if extension_audit:
            extensions["parameter_extension_overrides"]=extension_audit
        else:
            extensions.pop("parameter_extension_overrides",None)
        for namespace in ("rza_calc.ct_parameters","rza_calc.nameplate",PROVENANCE_KEY):
            if namespace in equipment.extensions:
                extensions[namespace]=thaw_json(equipment.extensions[namespace])
            else:
                extensions.pop(namespace,None)
        segment_overrides=thaw_json(extensions.get("segment_parameter_overrides",{}))
        for change in changes:
            if change.equipment_id==eid and change.segment_id is not None:
                segment_overrides.setdefault(change.segment_id.value,{})[change.key]={
                    "value":thaw_json(change.after.value),"source":change.after.source,
                    "confirmation":change.after.confirmation.value}
        if segment_overrides:
            extensions["segment_parameter_overrides"]=segment_overrides
        draft.catalog_snapshots = draft.catalog_snapshots.with_binding(replace(binding,instance_overrides=overrides,
            parameter_overrides=audited,extensions=extensions))


def preview_parameter_patch(controller, patches):
    controller._require_edit()
    stamp = _stamp(controller)
    patches = tuple(patches)
    if any(not isinstance(p,ParameterPatch) for p in patches):
        raise TypeError("Ожидается пакет ParameterPatch.")
    draft = ProjectDraft(stamp.state.electrical.to_model(),stamp.state.diagram,stamp.state.catalog_snapshots,
        stamp.state.user_catalog.to_catalog() if stamp.state.user_catalog else None)
    try:
        changes,issues = _apply_patch_records(draft.electrical_model,patches)
        if issues:
            return ParameterPreview(False,diagnostics=issues,stamp=stamp)
        _sync_bindings(draft,changes)
        readiness = []
        for eid in dict.fromkeys(p.equipment_id for p in patches):
            equipment=draft.electrical_model.equipment[eid]
            specs=_specs(draft.electrical_model,equipment,None)
            readiness.extend(_readiness(draft.electrical_model,equipment,specs,
                _values(draft.electrical_model,equipment,specs)))
        candidate=ProjectMemento.capture_draft(draft)
        problems=draft.catalog_snapshots.validate_targets(draft.electrical_model)
        if problems:
            raise ValueError("; ".join(problems))
        return ParameterPreview(True,changes,readiness=tuple(readiness),stamp=stamp,_candidate=candidate)
    except (ValueError,TypeError,KeyError,RuntimeError) as exc:
        return ParameterPreview(False,diagnostics=(ParameterIssue(None,"","invalid_batch",str(exc)),),stamp=stamp)


def apply_parameter_preview(controller, preview):
    from .controller import EditorCommandError
    controller._require_edit()
    if not isinstance(preview,ParameterPreview) or not preview.valid or preview._candidate is None:
        raise EditorCommandError("Пакет параметров содержит ошибки; изменения не применены.")
    _require_current(controller,preview.stamp)
    def command(draft):
        _require_current(controller,preview.stamp)
        draft.electrical_model=preview._candidate.electrical.to_model()
        draft.catalog_snapshots=preview._candidate.catalog_snapshots
        return ParameterBatchResult(tuple(dict.fromkeys(c.equipment_id for c in preview.changes)),preview.changes,preview.readiness)
    return controller._execute("Изменить параметры оборудования",command)


def preview_catalog_update(controller,equipment_ids,entry):
    if not isinstance(entry,CatalogEntry):
        raise TypeError("Нужна конкретная запись справочника.")
    from rza_calc.domain.catalog_compatibility import catalog_codec, catalog_values
    stamp=_stamp(controller)
    changes,problems,codecs=[],[],{}
    ids=tuple(dict.fromkeys(equipment_ids))
    for eid in ids:
        try:
            snapshot=equipment_snapshot(controller,eid,stamp=stamp)
            equipment=controller.model.equipment[eid]
            codecs[eid]=catalog_codec(controller.model,equipment,entry)
            values=catalog_values(controller.model,equipment,entry)
            for key,value in values.items():
                spec=next((s for s in snapshot.specs if s.key==key),None)
                if spec is None or not spec.editable or spec.scope=="identity":
                    continue
                metadata=entry.extensions.get(PROVENANCE_KEY,{}).get(key,{})
                source=metadata.get("source",entry.source)
                confirmation=(DataConfirmation.CONFIRMED if metadata.get("confirmation")=="confirmed"
                              and source.strip() and value is not None and not spec.instance_only else DataConfirmation.UNCONFIRMED)
                after=ParameterValue(value,source,confirmation,"catalog")
                if after!=snapshot.fields[key]:
                    changes.append(FieldChange(eid,key,snapshot.fields[key],after))
        except (ValueError,TypeError,KeyError,RuntimeError) as exc:
            problems.append(ParameterIssue(eid,"","catalog_incompatible",str(exc)))
    return CatalogUpdatePreview(tuple(changes),tuple(problems),stamp,entry,ids,MappingProxyType(codecs))


def apply_catalog_update(controller,preview,selected,*,preserve_manual=True,inherit_confirmation=True):
    from .controller import EditorCommandError
    if not isinstance(preview,CatalogUpdatePreview) or not preview.valid:
        raise EditorCommandError("Запись справочника несовместима с выбранным оборудованием.")
    _require_current(controller,preview.stamp)
    refs=frozenset(selected)
    offered={change.ref:change for change in preview.changes}
    if refs-set(offered):
        raise EditorCommandError("Выбраны поля, которых нет в просмотренном сравнении.")
    patches=[]
    for change in preview.changes:
        ref=change.ref
        if ref not in refs:
            continue
        if preserve_manual and is_manual_override(change.before):
            continue
        value=change.after
        if not inherit_confirmation:
            value=replace(value,confirmation=DataConfirmation.UNCONFIRMED)
        patches.append(ParameterPatch(ref.equipment_id,{ref.key:value},segment_id=ref.segment_id))
    proposal=preview_parameter_patch(controller,tuple(patches))
    if not proposal.valid:
        raise EditorCommandError("Параметры справочника не прошли проверку: "+"; ".join(p.message for p in proposal.diagnostics))
    candidate=proposal._candidate
    snapshots=candidate.catalog_snapshots
    catalog_id=(preview.stamp.state.user_catalog.id if preview.stamp.state.user_catalog
                and preview.entry.origin.value=="user" else None)
    for eid in dict.fromkeys(p.equipment_id for p in patches):
        binding=CatalogBinding(eid,CatalogEntrySnapshot.from_entry(preview.entry,source_catalog_id=catalog_id),
            extensions={"parameter_schema":"v1",**({"parameter_codec":preview.codecs[eid]} if preview.codecs[eid] else {})})
        snapshots=snapshots.with_binding(binding)
    draft=ProjectDraft(candidate.electrical.to_model(),candidate.diagram,snapshots)
    _sync_bindings(draft,proposal.changes)
    proposal=replace(proposal,_candidate=replace(candidate,catalog_snapshots=draft.catalog_snapshots))
    return apply_parameter_preview(controller,proposal)


def save_parameter_catalog_entry(controller,entry):
    controller._require_edit()
    if not isinstance(entry,CatalogEntry):
        raise TypeError("Нужна запись справочника.")
    def command(draft):
        if draft.user_catalog is None:
            raise ValueError("В проекте отсутствует пользовательский справочник.")
        if entry.id in draft.user_catalog.entries:
            draft.user_catalog.replace(entry)
        else:
            draft.user_catalog.add(entry)
        return entry
    return controller._execute("Сохранить запись справочника",command)
