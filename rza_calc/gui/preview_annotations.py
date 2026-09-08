"""Small display labels anchored to saved geometry, never electrical inference.

Hierarchy/ownership comes from OverviewTarget. A locally numbered feeder is
explicitly described as such; the model's names and identifiers stay intact.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
import re
from collections.abc import Mapping


@dataclass(frozen=True, slots=True)
class PreviewAnnotation:
    key: str
    anchor: tuple[float, float]
    text: str
    tooltip: str = ''
    priority: int = 10
    glyph: str = ''
    representation_id: object | None = None


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    try:
        return f'{value:g}'.replace('.', ',') if isfinite(value) else None
    except (OverflowError, ValueError):
        return None


def _walk(target):
    yield target
    for child in target.children:
        yield from _walk(child)


def _short_section(name):
    match = re.search(r'(?<!\w)(\d+)\s*СШ\b', name, re.IGNORECASE)
    if match:
        return 'СШ ' + match.group(1)
    match = re.search(r'\b(?:секция|СШ)\s*[№#]?\s*(\d+)\b', name, re.IGNORECASE)
    return 'Секция ' + match.group(1) if match else name


def make_preview_annotations(document, model, target, page_id, *, numbering_context=None) -> tuple[PreviewAnnotation, ...]:
    """Use explicit tree membership and real parameters on this exact page.

    Missing bus representations are omitted rather than assigned to a guessed
    section. Values here are saved input data, not calculations.
    """
    if target is None or page_id not in document.pages:
        return ()
    reps = sorted((r for r in document.representations.values() if r.page_id == page_id),
                  key=lambda r: r.id.value)
    by_equipment = {}
    by_node = {}
    for rep in reps:
        if rep.equipment_id is not None:
            by_equipment.setdefault(rep.equipment_id, []).append(rep)
        if rep.electrical_node_id is not None and rep.symbol_key in {'busbar', 'bus_system', 'bus_section'}:
            by_node.setdefault(rep.electrical_node_id, []).append(rep)
    targets = tuple(_walk(target))
    rows, used_sections = [], set()
    for section in (row for row in targets if row.kind == 'section' and row.role != 'unassigned'):
        candidates = [(nid, rep) for nid in getattr(section, 'anchor_node_ids', ()) for rep in by_node.get(nid, ())]
        if len(candidates) != 1:
            continue
        nid, rep = candidates[0]
        if rep.id in used_sections:
            continue
        used_sections.add(rep.id)
        voltage = _number(section.nominal_kv)
        text = _short_section(section.name)
        if voltage:
            text += ' · ' + voltage + ' кВ'
        rows.append(PreviewAnnotation('section:' + section.key, (rep.x, rep.y), text,
            section.name + ('\nНоминальное напряжение: ' + voltage + ' кВ' if voltage else ''), 1))

    for eid in sorted(set(target.equipment_ids), key=lambda value: value.value):
        equipment = model.equipment.get(eid)
        if equipment is None or eid not in by_equipment:
            continue
        definition = model.equipment_type(equipment.type_id, equipment.type_version)
        if definition.behavior_key not in {'transformer_2w', 'transformer_3w', 'legacy.transformer_2w', 'legacy.transformer_3w'}:
            continue
        values = model.effective_equipment_properties(eid)
        if definition.behavior_key.startswith('legacy.'):
            values = values.get('legacy_payload', {})
        if not isinstance(values, Mapping):
            values = {}
        match = re.search(r'(?<!\w)(?:Т|T|АТ|AT)\s*\d+\b', equipment.name, re.IGNORECASE)
        short = re.sub(r'\s+', '', match.group()) if match else 'Т'
        snom = values.get('s_nom')  # shared parameter schema stores s_nom in kVA
        power = _number(snom)
        if power:
            power = (_number(snom / 1000) + ' МВА') if snom >= 1000 else (power + ' кВА')
        text = short + (' · ' + power if power else ' · мощность не задана')
        voltage_values = [values.get('u_hv')]
        if definition.behavior_key.endswith('transformer_3w'):
            voltage_values.append(values.get('u_mv'))
        voltage_values.append(values.get('u_lv'))
        volts = [_number(v) for v in voltage_values]
        if all(volts):
            text += '\n' + '/'.join(volts) + ' кВ'
        else:
            text += '\nНапряжения: не все заданы'
        tooltip = equipment.name + '\nСохранённые исходные данные'
        if power:
            tooltip += '\nНоминальная мощность: ' + power
        else:
            tooltip += '\nНоминальная мощность не задана'
        if all(volts):
            tooltip += '\nНапряжения обмоток ВН→НН: ' + '/'.join(volts) + ' кВ'
        else:
            tooltip += '\nНапряжения обмоток ВН→НН: ' + '/'.join(v or '?' for v in volts) + ' кВ'
        provenance = values.get('parameter_provenance', {})
        canonical_provenance = equipment.extensions.get('rza_calc.parameter_provenance', {})
        for key in ('s_nom', 'u_hv', 'u_mv', 'u_lv'):
            meta = canonical_provenance.get(key, {}) if isinstance(canonical_provenance, Mapping) else {}
            if not meta:
                meta = provenance.get(key, {}) if isinstance(provenance, Mapping) else {}
            if isinstance(meta, Mapping) and meta:
                if meta.get('confirmation') != 'confirmed':
                    tooltip += '\n' + key + ': не подтверждено'
                if meta.get('source'):
                    tooltip += '\n' + str(meta['source'])
        rep = by_equipment[eid][0]
        glyph = 'transformer_3w' if definition.behavior_key.endswith('transformer_3w') else 'transformer_2w'
        tooltip += '\nУвеличенное обозначение; пунктир — выноска к аппарату, не провод.'
        rows.append(PreviewAnnotation('transformer:' + eid.value, (rep.x, rep.y), text, tooltip, 0,
                                      glyph=glyph, representation_id=rep.id))

    # Number only outgoing bays. Each RU has its own display sequence, so
    # choosing the RU filter does not renumber its feeders.
    numbering_targets = tuple(_walk(numbering_context or target))
    scopes = [row for row in numbering_targets if row.kind == 'voltage_level'] or [target]
    selected_bays = {row.key for row in targets if row.kind == 'bay'}
    used_bays = set()
    for scope in scopes:
        bays = [row for row in _walk(scope) if row.kind == 'bay' and row.role == 'outgoing']
        formal = {}
        for bay in bays:
            match = re.search(r'(?<!\w)(?:Ф|F|фидер)\s*[№#]?\s*(\d+)\b', bay.name, re.IGNORECASE)
            if match:
                formal[bay.key] = match.group(1)
        reserved = set(formal.values())
        local = 1
        for bay in bays:
            if bay.key in used_bays:
                continue
            used_bays.add(bay.key)
            number = formal.get(bay.key)
            generated = number is None
            if generated:
                while str(local) in reserved:
                    local += 1
                number = str(local)
                reserved.add(number)
                local += 1
            if bay.key not in selected_bays:
                continue
            # Child order is explicit bay order, not a parse of electrical IDs.
            identifiers = [eid for leaf in _walk(bay) if leaf.kind == 'equipment' for eid in leaf.equipment_ids]
            anchor = next((by_equipment[eid][0] for eid in identifiers if eid in by_equipment), None)
            if anchor is None:
                continue
            tooltip = bay.name
            if generated:
                tooltip += '\nФ' + number + ' — номер в предпросмотре; имя в проекте не изменено.'
            if scope.kind == 'voltage_level':
                tooltip += '\n' + scope.name
            section = next((s for s in _walk(scope) if s.kind == 'section' and any(b.key == bay.key for b in s.children)), None)
            if section:
                tooltip += '\n' + section.name
            rows.append(PreviewAnnotation('feeder:' + bay.key, (anchor.x, anchor.y), 'Ф' + number, tooltip, 2))
    return tuple(rows)


__all__ = ['PreviewAnnotation', 'make_preview_annotations']
