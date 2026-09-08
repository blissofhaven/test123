"""ID-preserving navigation; no solver, model mutation or hidden inference."""
from __future__ import annotations
from collections import defaultdict
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Mapping
from ..domain.diagram import PageId
from ..domain.electrical import ElectricalNodeId, EquipmentId, OperatingStateId, PortId, EquipmentAvailability, deterministic_id
from ..domain.fingerprint import electrical_model_fingerprint


@dataclass(frozen=True, slots=True)
class OverviewDiagnostic:
    code: str
    message: str
    target_key: str = ''
    equipment_ids: tuple[EquipmentId, ...] = ()


@dataclass(frozen=True, slots=True)
class OverviewStatus:
    code: str = 'unknown'
    label: str = 'Состояние по топологии не определено'
    energized_sections: int = 0
    total_sections: int = 0
    unknown_sections: int = 0
    repair: bool = False


@dataclass(frozen=True, slots=True)
class OverviewTarget:
    key: str
    kind: str
    name: str
    children: tuple[OverviewTarget, ...] = ()
    equipment_ids: tuple[EquipmentId, ...] = ()
    node_ids: tuple[ElectricalNodeId, ...] = ()
    port_ids: tuple[PortId, ...] = ()
    page_ids: tuple[PageId, ...] = ()
    preferred_page_id: PageId | None = None
    nominal_kv: float | None = None
    role: str = ''
    diagnostics: tuple[OverviewDiagnostic, ...] = ()
    status: OverviewStatus = field(default_factory=OverviewStatus)
    anchor_node_ids: tuple[ElectricalNodeId, ...] = ()


@dataclass(frozen=True, slots=True)
class FacilityOverview:
    id: str
    name: str
    kind: str
    parent_id: str | None
    upstream_ids: tuple[str, ...]
    target: OverviewTarget
    transformer_count: int
    outgoing_count: int
    incoming_count: int
    unassigned_count: int
    status: OverviewStatus
    voltage_levels: tuple[float, ...] = ()
    input_sections_distinct: bool | None = None
    counts_complete: bool = True
    outgoing_in_service: int | None = None

    @property
    def page_ids(self):
        return self.target.page_ids

    @property
    def preferred_page_id(self):
        return self.target.preferred_page_id


@dataclass(frozen=True, slots=True)
class InterfacilityLink:
    id: str
    from_facility_id: str
    to_facility_id: str
    name: str
    equipment_ids: tuple[EquipmentId, ...]
    from_node_id: ElectricalNodeId
    to_node_id: ElectricalNodeId
    from_port_id: PortId
    to_port_id: PortId
    status: OverviewStatus = field(default_factory=OverviewStatus)
    from_bus_id: ElectricalNodeId | None = None
    to_bus_id: ElectricalNodeId | None = None
    nominal_kv: float | None = None


@dataclass(frozen=True, slots=True)
class NetworkOverview:
    name: str
    facilities: tuple[FacilityOverview, ...]
    roots: tuple[OverviewTarget, ...]
    links: tuple[InterfacilityLink, ...]
    unassigned: OverviewTarget
    diagnostics: tuple[OverviewDiagnostic, ...]
    state_id: OperatingStateId | None
    model_fingerprint: str
    targets: Mapping[str, OverviewTarget]
    facility_ids_by_equipment: Mapping[EquipmentId, tuple[str, ...]]
    facility_ids_by_node: Mapping[ElectricalNodeId, tuple[str, ...]]
    page_facility_ids: Mapping[PageId, tuple[str, ...]]
    source: str = 'unassigned'

    def __post_init__(self):
        for key in ('targets', 'facility_ids_by_equipment', 'facility_ids_by_node', 'page_facility_ids'):
            object.__setattr__(self, key, MappingProxyType(dict(getattr(self, key))))


def build_network_overview(project, *, operating_state_id=None, topology=None) -> NetworkOverview:
    """No topology is compiled here; absent/stale topology means unknown status."""
    return _Builder(project, operating_state_id, topology).build()


class _Builder:
    def __init__(self, project, state_id, topology):
        self.project, self.model, self.diagram = project, project.electrical_model, project.diagram
        self.revision = self.model.revision
        self.fingerprint = electrical_model_fingerprint(self.model)
        self.state_id = (OperatingStateId(state_id) if isinstance(state_id, str) else state_id)
        self.diagnostics = []
        self.topology = topology
        if topology is not None:
            if (topology.model_fingerprint != self.fingerprint or
                    self.state_id is not None and topology.operating_state_id != self.state_id):
                self.issue('stale_topology', 'Топология относится к другой модели или режиму; состояния не показаны.')
                self.topology = None
            elif self.state_id is None:
                self.state_id = topology.operating_state_id
        self.by_alias = defaultdict(set)
        self.node_alias = defaultdict(set)
        self.pages_by_equipment, self.pages_by_node = defaultdict(set), defaultdict(set)
        for item in (*self.diagram.representations.values(), *self.diagram.routes.values()):
            if item.equipment_id is not None:
                self.pages_by_equipment[item.equipment_id].add(item.page_id)
            if item.electrical_node_id is not None:
                self.pages_by_node[item.electrical_node_id].add(item.page_id)
        self.node_by_port = {c.port_id: c.electrical_node_id for c in self.model.connections.values()}
        self.behaviors = {e.id: self.model.equipment_type(e.type_id, e.type_version).behavior_key
                          for e in self.model.equipment.values()}
        # The established adapter's ID codec does not build a network or solve.
        from ..adapters.legacy_calculation import _legacy_or_native_id
        for node in self.model.electrical_nodes.values():
            for alias in (node.id.value, _legacy_or_native_id('node', node.id.value, node.extensions)):
                self.node_alias[alias].add(node.id)
        for equipment in self.model.equipment.values():
            behavior = self.behaviors[equipment.id]
            category = ('transformer3w' if behavior in {'transformer_3w', 'legacy.transformer_3w'} else
                        'load' if behavior in {'load', 'legacy.load'} else 'branch')
            alias = _legacy_or_native_id(category, equipment.id.value, equipment.extensions)
            kind = 'load' if category == 'load' else 'branch'
            self.by_alias[(kind, alias)].add(equipment.id)
            self.by_alias[(kind, equipment.id.value)].add(equipment.id)
            if category == 'transformer3w':
                for suffix in ('_mv', '_lv'):
                    self.by_alias[('branch', alias + suffix)].add(equipment.id)
        self.facilities = {}
        self.links = []
        self.link_ids = set()
        self.interfacility_lines = set()
        self.manifest_loads = set()
        self.assigned = set()
        self.port_facilities = defaultdict(set)
        self.source = 'unassigned'
        self.blocked_nodes = set()
        self.blocked_equipment = set()
        if self.topology is not None:
            for diagnostic in self.topology.diagnostics:
                if not diagnostic.is_blocking:
                    continue
                nodes = set(diagnostic.related_node_ids)
                if diagnostic.electrical_node_id is not None:
                    nodes.add(diagnostic.electrical_node_id)
                equipment_ids = {eid for eid in (*diagnostic.related_equipment_ids, diagnostic.equipment_id) if eid is not None}
                ports = set(diagnostic.related_port_ids)
                if diagnostic.port_id is not None:
                    ports.add(diagnostic.port_id)
                for eid in equipment_ids:
                    if eid in self.model.equipment:
                        ports.update(self.model.equipment[eid].port_ids)
                equipment_ids.update(self.model.ports[pid].equipment_id for pid in ports if pid in self.model.ports)
                self.blocked_equipment.update(equipment_ids)
                nodes.update(self.node_by_port[p] for p in ports if p in self.node_by_port)
                if not nodes and not equipment_ids and not ports:
                    self.blocked_nodes.update(self.topology.node_ids)
                for node in nodes:
                    if node in self.topology.component_by_node:
                        self.blocked_nodes.update(self.topology.component_of(node).node_ids)

    def issue(self, code, message, key='', equipment_ids=()):
        row = OverviewDiagnostic(code, message, key, tuple(equipment_ids))
        if row not in self.diagnostics:
            self.diagnostics.append(row)
        return row

    def resolve(self, token, key='', kind='branch'):
        values = self.node_alias.get(str(token), set()) if kind == 'node' else self.by_alias.get((kind, str(token)), set())
        if len(values) != 1:
            self.issue('unresolved_reference', f'Неоднозначная или отсутствующая ссылка {kind}: {token}', key)
            return None
        return next(iter(values))

    def equipment(self, token, key=''):
        values = self.by_alias.get(('branch', str(token)), set()) | self.by_alias.get(('load', str(token)), set())
        if len(values) != 1:
            self.issue('unresolved_reference', f'Неоднозначная или отсутствующая ссылка оборудования: {token}', key)
            return None
        return next(iter(values))

    def ordered_pages(self, pages):
        return tuple(sorted(set(pages), key=lambda pid: (self.diagram.pages[pid].order, pid.value)))

    def target(self, key, kind, name, *, children=(), equipment_ids=(), node_ids=(), port_ids=(),
               preferred_page_id=None, nominal_kv=None, role=''):
        children = tuple(children)
        equipment = set(equipment_ids)
        nodes, ports = set(node_ids), set(port_ids)
        anchor_nodes = tuple(sorted(nodes))
        for child in children:
            equipment.update(child.equipment_ids)
            nodes.update(child.node_ids)
            ports.update(child.port_ids)
        pages = set()
        for eid in equipment:
            pages.update(self.pages_by_equipment[eid])
        for nid in nodes:
            pages.update(self.pages_by_node[nid])
        if preferred_page_id is not None:
            pages.add(preferred_page_id)
        page_ids = self.ordered_pages(pages)
        preferred = preferred_page_id or (page_ids[0] if len(page_ids) == 1 else None)
        diagnostics = tuple(d for d in self.diagnostics if d.target_key == key)
        status = self.topology_status(nodes)
        if kind in {'equipment','bay'} and equipment:
            status = self.link_status(equipment, nodes)
        elif equipment & self.blocked_equipment:
            status = replace(status, code='error', label='Есть ошибка топологии оборудования')
        if kind == 'unassigned' or role == 'unassigned' or any(
                d.code in {'unresolved_section', 'unknown_level_voltage', 'unassigned_section'} for d in diagnostics):
            status = OverviewStatus()
        status = replace(status, repair=self.in_repair(equipment))
        return OverviewTarget(key, kind, name, children,
            tuple(sorted(equipment)), tuple(sorted(nodes)), tuple(sorted(ports)), page_ids, preferred,
            nominal_kv, role, diagnostics, status, anchor_nodes)

    def leaf(self, eid, key, *, port_ids=None):
        equipment = self.model.equipment[eid]
        ports = equipment.port_ids if port_ids is None else tuple(port_ids)
        nodes = tuple(self.node_by_port[p] for p in ports if p in self.node_by_port)
        return self.target(key, 'equipment', equipment.name, equipment_ids=(eid,), node_ids=nodes,
                           port_ids=ports, role=self.behaviors[eid])

    def facility(self, fid, name, kind, parent=None, preferred=None):
        if fid in self.facilities:
            self.issue('duplicate_facility', 'Повторяется ID объекта: ' + fid)
            return None
        value = dict(id=fid, name=name, kind=kind, parent=parent, preferred=preferred,
                     levels={}, equipment=set(), recognized=set(), buses=set(), upstream=set(),
                     outgoing=0, incoming=0, input_buses=[], input_complete=True,
                     unresolved_sections=0, counts_complete=True, outgoing_bays=[], circuit_placements=set())
        self.facilities[fid] = value
        return value

    def level(self, facility, key, name, kv):
        if key not in facility['levels']:
            facility['levels'][key] = dict(key=key, name=name, kv=kv, sections={}, unassigned=[])
        return facility['levels'][key]

    def section(self, level, key, name, nodes=()):
        if key not in level['sections']:
            level['sections'][key] = dict(key=key, name=name, nodes=tuple(nodes), bays=[])
        return level['sections'][key]

    def bay(self, facility, section, key, name, eids, role, *, level=None, port_ids=None, extra_children=(), count_circuit=True):
        eids = tuple(dict.fromkeys(eids))
        if section is None:
            self.issue('unassigned_section', 'Секция присоединения не определена: ' + name, key, eids)
        children = [self.leaf(eid, key + ':equipment:' + eid.value,
                    port_ids=port_ids.get(eid) if port_ids else None) for eid in eids]
        children.extend(extra_children)
        result = self.target(key, 'bay', name, children=children, role=role)
        if role == 'outgoing' and count_circuit:
            facility['outgoing_bays'].append(result)
        facility['equipment'].update(eids)
        facility['recognized'].update(eids)
        self.assigned.update(eids)
        for child in children:
            for pid in child.port_ids:
                self.port_facilities[pid].add(facility['id'])
        if section is None:
            if level is not None:
                level['unassigned'].append(result)
        else:
            section['bays'].append(result)
        return result

    def topology_status(self, nodes, *, unresolved=0):
        nodes = tuple(dict.fromkeys(nodes))
        total = len(nodes) + unresolved
        if self.topology is None or not nodes:
            return OverviewStatus(total_sections=total, unknown_sections=total)
        rows = [self.topology.is_energized(nid)
                if nid in self.topology.component_by_node and nid not in self.blocked_nodes else None
                for nid in nodes] + [None] * unresolved
        live, unknown = rows.count(True), rows.count(None)
        if any(nid in self.blocked_nodes for nid in nodes):
            code, label = 'error', 'Есть ошибка топологии; питание не подтверждено'
        elif unknown:
            code, label = 'unknown', 'Питание части секций не определено'
        elif live == len(rows):
            code, label = 'energized', 'Все секции связаны с источником'
        elif live:
            code, label = 'partial', 'Часть секций без питания'
        else:
            code, label = 'deenergized', 'Все секции без питания'
        return OverviewStatus(code, label, live, len(rows), unknown)

    def in_repair(self, eids):
        return self.topology is not None and any(
            self.topology.operating_state.availability.get(eid) == EquipmentAvailability.OUT_OF_SERVICE
            for eid in eids)

    def link_status(self, eids, nodes):
        if self.topology is None:
            return OverviewStatus()
        if any(nid in self.blocked_nodes for nid in nodes) or set(eids) & self.blocked_equipment:
            return OverviewStatus('error', 'Есть ошибка топологии; состояние цепи не подтверждено', repair=self.in_repair(eids))
        if self.in_repair(eids):
            return OverviewStatus('open', 'Оборудование выведено из работы', repair=True)
        links = [self.topology.links[key] for eid in eids for key in self.topology.link_ids_by_equipment.get(eid, ())]
        non_branch = {'load','legacy.load','source','legacy.source','generator','legacy.generator','bus'}
        if any(not self.topology.link_ids_by_equipment.get(eid) and self.behaviors[eid] not in non_branch for eid in eids):
            return OverviewStatus()
        if any(link.conductivity.value == 'UNKNOWN' for link in links):
            return OverviewStatus()
        if any(not link.active for link in links):
            return OverviewStatus('open', 'Цепь разомкнута или выведена из работы')
        value = self.topology_status(nodes)
        if value.code == 'energized':
            return OverviewStatus('connected', 'Цепь замкнута, концы связаны с источником')
        return value

    def chain(self, row, key):
        raw_branches, raw_nodes = row.get('branch_ids', ()), row.get('node_ids', ())
        if not isinstance(raw_branches, (tuple, list)) or not isinstance(raw_nodes, (tuple, list)):
            self.issue('invalid_chain', 'Повреждено описание цепи.', key)
            return None
        eids = [self.resolve(token, key) for token in raw_branches]
        nids = [self.resolve(token, key, 'node') for token in raw_nodes]
        if not eids or None in eids or None in nids or len(nids) != len(eids) + 1:
            self.issue('invalid_chain', 'Состав цепи не совпадает с её электрическими узлами.', key)
            return None
        for eid, first, last in zip(eids, nids, nids[1:]):
            ports = self.model.equipment[eid].port_ids
            actual = {self.node_by_port.get(pid) for pid in ports}
            if first == last or actual != {first, last} or len(ports) != 2:
                self.issue('changed_chain', 'Электрические концы изменены; прежняя цепь требует уточнения.', key, (eid,))
                return None
        return tuple(eids), tuple(nids)

    def manifest(self, manifest, source):
        if manifest.get('schema_version') != 1 or not isinstance(manifest.get('facilities'), (list, tuple)):
            self.issue('unsupported_manifest', 'Версия или состав описания учебных объектов не поддерживается.')
            return
        rows = manifest['facilities']
        if any(not isinstance(row, Mapping) or not isinstance(row.get('id'), str) for row in rows):
            self.issue('invalid_manifest', 'Некорректные записи объектов учебной схемы.')
            return
        if len({row['id'] for row in rows}) != len(rows):
            self.issue('duplicate_facility', 'ID объектов учебной схемы повторяются.')
            return
        # The registered demo schema is a navigation declaration, not executable
        # input. Malformed containers must not crash opening an arbitrary file.
        for row in rows:
            arrays = ('branch_ids', 'load_ids', 'transformers', 'loads', 'generators', 'incoming')
            maps = ('buses', 'bus_ties')
            valid = (bool(row['id'].strip())
                and all(isinstance(row.get(k, ()), (tuple, list)) for k in arrays)
                and all(isinstance(row.get(k, {}), Mapping) for k in maps)
                and all(isinstance(row.get(k, ''), str) for k in ('name','kind')))
            if valid:
                valid = all(isinstance(row.get('buses', {}).get(side, ()), (tuple, list)) for side in ('hv','lv'))
            for name in ('transformers','loads','generators','incoming'):
                if not valid:
                    break
                entries = row.get(name, ())
                valid = (all(isinstance(entry, Mapping) and isinstance(entry.get('id'), str)
                             and entry['id'].strip() for entry in entries)
                         and len({entry['id'] for entry in entries}) == len(entries))
                if name == 'generators' and valid:
                    valid = all(isinstance(entry.get('node_ids', ()), (tuple, list)) for entry in entries)
                if name == 'incoming' and valid:
                    valid = all(isinstance(entry.get('upstream_id', ''), str) for entry in entries)
            if not valid:
                self.issue('invalid_manifest', 'Некорректные поля описания объекта: ' + row['id'])
                return
        self.source = source
        declared = {row['id']: row for row in rows}
        for row in rows:
            fid, key = row['id'], 'facility:' + row['id']
            page = deterministic_id(PageId, 'oilfield-demo', fid)
            if page not in self.diagram.pages:
                self.issue('missing_facility_page', 'Исходный лист объекта отсутствует; выберите существующее представление.', key)
                page = None
            facility = self.facility(fid, row.get('name', fid), row.get('kind', 'other'), preferred=page)
            for token in (*row.get('branch_ids', ()), *row.get('load_ids', ())):
                eid = self.equipment(token, key)
                if eid is not None:
                    facility['equipment'].add(eid)
            for side in ('hv', 'lv'):
                level = self.level(facility, f'level:{fid}:{side}', 'РУ: напряжение не определено', None)
                for index, token in enumerate(row.get('buses', {}).get(side, ()), 1):
                    nid = self.resolve(token, key, 'node')
                    name = self.model.electrical_nodes[nid].name if nid else f'Секция {index}: не определена'
                    self.section(level, f'section:{fid}:{side}:{index}', name, (nid,) if nid else ())
                    if nid:
                        facility['buses'].add(nid)
                    else:
                        facility['unresolved_sections'] += 1
                        facility['counts_complete'] = False
                kv = self.canonical_level_voltage(level, row.get(side + '_kv'))
                level['kv'] = kv
                level['name'] = f'РУ {kv:g} кВ' if kv is not None else 'РУ: напряжение не определено'
                tie = self.equipment(row.get('bus_ties', {}).get(side), key)
                if tie is not None:
                    buses = {nid for section in level['sections'].values() for nid in section['nodes']}
                    actual = {self.node_by_port.get(p) for p in self.model.equipment[tie].port_ids}
                    common = (self.section(level, f'section:{fid}:{side}:common', 'Межсекционные связи', buses)
                              if len(buses) == 2 and actual == buses else None)
                    self.bay(facility, common, f'bay:{fid}:{side}:coupler', self.model.equipment[tie].name, (tie,), 'bus_coupler', level=level)
                else:
                    facility['counts_complete'] = False
            for transformer in row.get('transformers', ()):
                tid = str(transformer.get('id', ''))
                checked = self.chain(transformer, f'bay:{fid}:transformer:{tid}')
                if checked is None or len(checked[0]) != 3:
                    facility['counts_complete'] = False
                    continue
                eids, nodes = checked
                if self.behaviors[eids[1]] not in {'transformer_2w','legacy.transformer_2w'}:
                    self.issue('invalid_transformer', 'Заявленная цепь не содержит двухобмоточный трансформатор.', key, eids)
                    facility['counts_complete'] = False
                    continue
                for side, pair, endpoint in (('hv', eids[:2], nodes[1]), ('lv', eids[1:], nodes[2])):
                    level, section = self.manifest_section(facility, side, transformer.get('section'))
                    if section is not None and nodes[0 if side == 'hv' else -1] not in section['nodes']:
                        section = None
                    physical = {eids[1]: tuple(p for p in self.model.equipment[eids[1]].port_ids if self.node_by_port.get(p) == endpoint)}
                    self.bay(facility, section, f'bay:{fid}:{side}:transformer:{tid}', self.model.equipment[eids[1]].name, pair, 'transformer', level=level, port_ids=physical)
            for load in row.get('loads', ()):
                key = f"bay:{fid}:load:{load.get('id', '')}"
                checked = self.chain(load, key)
                load_id = self.resolve(load.get('load_id'), key, 'load')
                if checked is None or load_id is None:
                    facility['counts_complete'] = False
                    continue
                if load_id in self.manifest_loads:
                    self.issue('duplicate_feeder', 'Один физический фидер описан повторно; повтор не посчитан.', key, (load_id,))
                    facility['counts_complete'] = False
                    continue
                if {self.node_by_port.get(p) for p in self.model.equipment[load_id].port_ids} != {checked[1][-1]}:
                    self.issue('changed_chain', 'Нагрузка больше не подключена к указанному фидеру.', key, (load_id,))
                    facility['counts_complete'] = False
                    continue
                self.manifest_loads.add(load_id)
                level, section = self.manifest_section(facility, 'lv', load.get('section'))
                if section is not None and checked[1][0] not in section['nodes']:
                    section = None
                self.bay(facility, section, key, self.model.equipment[load_id].name, (*checked[0], load_id), 'outgoing', level=level)
                facility['outgoing'] += 1
            for generator in row.get('generators', ()):
                key = f"bay:{fid}:generator:{generator.get('id', '')}"
                eid = self.resolve(generator.get('id'), key)
                qid = self.resolve(generator.get('breaker'), key)
                nodes = [self.resolve(n, key, 'node') for n in generator.get('node_ids', ())]
                if eid is None or qid is None or None in nodes or len(nodes) != 2:
                    facility['counts_complete'] = False
                    continue
                if ({self.node_by_port.get(p) for p in self.model.equipment[qid].port_ids} != set(nodes)
                        or {self.node_by_port.get(p) for p in self.model.equipment[eid].port_ids} != {nodes[0]}):
                    self.issue('changed_chain', 'Цепь генератора изменена.', key, (eid,qid))
                    facility['counts_complete'] = False
                    continue
                level, section = self.manifest_section(facility, 'lv', generator.get('section'))
                if section is not None and nodes[1] not in section['nodes']:
                    section = None
                self.bay(facility, section, key, self.model.equipment[eid].name, (eid,qid), 'generator', level=level)
        # One incoming declaration creates both physical sides; never two views counted twice.
        for row in rows:
            for incoming in row.get('incoming', ()):
                self.manifest_incoming(row, incoming, declared)

    def canonical_level_voltage(self, level, declared):
        """Display current declared canonical bus classes, never stale metadata."""
        classes = set()
        unknown = False
        for section in level['sections'].values():
            if not section['nodes']:
                unknown = True
            for nid in section['nodes']:
                vid = self.model.electrical_nodes[nid].declared_voltage_class_id
                if vid is None:
                    unknown = True
                else:
                    classes.add(self.model.voltage_classes[vid].nominal_voltage_v / 1000)
        if unknown or len(classes) != 1:
            self.issue('unknown_level_voltage', 'Напряжение РУ не определено однозначно по каноническим секциям.', level['key'])
            return None
        actual = next(iter(classes))
        if declared != actual:
            self.issue('changed_level_voltage', 'Описание РУ отличается от текущего напряжения секций; показано текущее значение.', level['key'])
        return actual

    def connection_voltage(self, nodes):
        """Actual endpoint stage, never a maximum of a facility's other RUs."""
        values = set()
        for nid in nodes:
            vid = self.model.electrical_nodes[nid].declared_voltage_class_id
            if vid is None and self.topology is not None:
                zone = self.topology.voltage_zone_by_node.get(nid)
                if zone is not None:
                    vid = self.topology.voltage_zones[zone].resolution.voltage_class_id
            if vid is None:
                return None
            values.add(self.model.voltage_classes[vid].nominal_voltage_v / 1000)
        return next(iter(values)) if len(values) == 1 else None

    def manifest_section(self, facility, side, number):
        level = facility['levels'][f"level:{facility['id']}:{side}"]
        key = f"section:{facility['id']}:{side}:{number}"
        return level, level['sections'].get(key)

    def manifest_incoming(self, row, chain, declared):
        fid, upstream_id = row['id'], chain.get('upstream_id')
        key = f"link:{chain.get('id', '')}"
        checked = self.chain(chain, key)
        current = self.facilities[fid]
        if upstream_id not in declared or checked is None or len(checked[0]) != 3:
            current['input_complete'] = False
            self.issue('invalid_incoming', 'Ввод не удалось связать с фактической питающей цепью.', key)
            return
        upstream = self.facilities[upstream_id]
        side = 'hv' if declared[upstream_id].get('kind') == 'gtes' else 'lv'
        upl, ups = self.manifest_section(upstream, side, chain.get('section'))
        dstl, dsts = self.manifest_section(current, 'hv', chain.get('section'))
        eids, nodes = checked
        if (ups is None or dsts is None or nodes[0] not in ups['nodes'] or nodes[-1] not in dsts['nodes']
                or self.behaviors[eids[1]] not in {'line','line_section','legacy.line'}):
            current['input_complete'] = False
            self.issue('changed_incoming_section', 'Ввод не соответствует указанным секциям шин.', key, eids)
            return
        if key in self.link_ids or eids[1] in self.interfacility_lines:
            self.issue('duplicate_incoming', 'Ввод объявлен повторно.', key, eids)
            current['input_complete'] = False
            return
        self.link_ids.add(key)
        self.interfacility_lines.add(eids[1])
        line = self.model.equipment[eids[1]]
        from_port = next(p for p in line.port_ids if self.node_by_port[p] == nodes[1])
        to_port = next(p for p in line.port_ids if self.node_by_port[p] == nodes[2])
        self.links.append(InterfacilityLink(key, upstream_id, fid, line.name, eids,
            nodes[1], nodes[2], from_port, to_port, self.link_status(eids, nodes), nodes[0], nodes[-1],
            self.connection_voltage((nodes[1],nodes[2]))))
        self.bay(upstream, ups, f'bay:{upstream_id}:out:{chain["id"]}', 'Линия → ' + current['name'], eids[:2], 'outgoing', port_ids={line.id:(from_port,)})
        self.bay(current, dsts, f'bay:{fid}:in:{chain["id"]}', 'Ввод ← ' + upstream['name'], eids[1:], 'incoming', port_ids={line.id:(to_port,)})
        upstream['outgoing'] += 1
        current['incoming'] += 1
        current['upstream'].add(upstream_id)
        current['input_buses'].append(nodes[0])

    def structure(self, structure):
        problems = structure.validate()
        if problems:
            for problem in problems:
                self.issue('invalid_structure', str(problem))
            return
        self.source = 'structure'
        for row in structure.facilities.values():
            self.facility(row.id, row.name, row.kind, row.parent_id)
        levels, sections = {}, {}
        for row in structure.voltage_levels.values():
            owner = self.facilities[row.facility_id]
            levels[row.id] = self.level(owner, 'level:' + row.id, row.name, row.u_nom)
        for row in structure.bus_sections.values():
            owner = self.facilities[structure.voltage_levels[row.voltage_level_id].facility_id]
            nid = self.resolve(row.calculation_node_id, 'section:' + row.id, 'node') if row.calculation_node_id else None
            if nid is None:
                self.issue('unresolved_section', 'У секции нет однозначного канонического узла: ' + row.name, 'section:' + row.id)
                owner['unresolved_sections'] += 1
                owner['counts_complete'] = False
            else:
                owner['buses'].add(nid)
            sections[row.id] = self.section(levels[row.voltage_level_id], 'section:' + row.id, row.name, (nid,) if nid else ())
        for level in levels.values():
            level['kv'] = self.canonical_level_voltage(level, level['kv'])
        equipment_map = {}
        for row in structure.equipment.values():
            ids, nodes = set(), set()
            for ref in row.calculation_refs:
                if ref.kind == 'node':
                    node = self.resolve(ref.object_id, 'structure-equipment:' + row.id, 'node')
                    if node is not None:
                        nodes.add(node)
                    continue
                if ref.kind not in {'branch','load'}:
                    continue
                eid = self.resolve(ref.object_id, 'structure-equipment:' + row.id, ref.kind)
                if eid is not None:
                    ids.add(eid)
            if not ids and row.id in {e.value for e in self.model.equipment}:
                ids.add(EquipmentId(row.id))
            equipment_map[row.id] = ids, nodes
        placements = defaultdict(list)
        for placement in structure.placements.values():
            placements[placement.bay_id].append(placement)
        for bay in structure.bays.values():
            owner = self.facilities[structure.voltage_levels[bay.voltage_level_id].facility_id]
            ids, physical, node_children = set(), defaultdict(set), []
            for placement in sorted(placements[bay.id], key=lambda p:(p.order,p.id)):
                resolved, nodes = equipment_map[placement.equipment_id]
                if not resolved and not nodes:
                    self.issue('unresolved_placement', 'Оборудование ячейки не сопоставлено: ' + placement.equipment_id, 'bay:' + bay.id)
                    owner['counts_complete'] = False
                if nodes:
                    node_children.append(self.target('placement:' + placement.id, 'equipment',
                        structure.equipment[placement.equipment_id].name, node_ids=nodes, role='busbar'))
                ids.update(resolved)
                for eid in resolved:
                    ports = self.model.equipment[eid].port_ids
                    if placement.terminal:
                        ports = tuple(pid for pid in ports if self.model.ports[pid].role == placement.terminal or pid.value == placement.terminal)
                        if not ports:
                            self.issue('unresolved_terminal', 'Физическая сторона размещения не сопоставлена: ' + placement.terminal, 'bay:' + bay.id, (eid,))
                            owner['counts_complete'] = False
                    physical[eid].update(ports)
            count_circuit = bool(ids)
            if bay.kind in {'incoming', 'outgoing'}:
                signature = (bay.kind, tuple((eid, tuple(sorted(physical[eid]))) for eid in sorted(ids)))
                if not ids:
                    self.issue('unresolved_circuit', 'Физическое оборудование присоединения не определено.', 'bay:' + bay.id)
                    owner['counts_complete'] = False
                elif signature in owner['circuit_placements']:
                    self.issue('duplicate_feeder', 'Те же аппараты и выводы присоединения размещены повторно; повтор не посчитан.', 'bay:' + bay.id, sorted(ids))
                    owner['counts_complete'] = False
                    count_circuit = False
                else:
                    owner['circuit_placements'].add(signature)
            self.bay(owner, sections.get(bay.bus_section_id), 'bay:' + bay.id, bay.name, sorted(ids), bay.kind,
                     level=levels[bay.voltage_level_id], port_ids=physical, extra_children=node_children,
                     count_circuit=count_circuit)
            # Counts are circuit/bay counts, not equipment/representation counts.
            if count_circuit and bay.kind == 'outgoing':
                owner['outgoing'] += 1
            if count_circuit and bay.kind == 'incoming':
                owner['incoming'] += 1

    def discover_links(self):
        """For explicit structure/unassigned additions, resolve real line ends.

        Port placement wins over node membership. Ambiguous sides are visible
        diagnostics, never resolved by name, page position or assumed flow.
        """
        node_owners = defaultdict(set)
        for fid, facility in self.facilities.items():
            for node in facility['buses']:
                node_owners[node].add(fid)
        for pid, owners in self.port_facilities.items():
            if pid in self.node_by_port:
                node_owners[self.node_by_port[pid]].update(owners)
        represented = {eid for link in self.links for eid in link.equipment_ids}
        for eid, behavior in self.behaviors.items():
            if eid in represented or behavior not in {'line','line_section','legacy.line'}:
                continue
            ports = {self.model.ports[pid].role: pid for pid in self.model.equipment[eid].port_ids}
            if 'from' not in ports or 'to' not in ports:
                continue
            first, last = ports['from'], ports['to']
            nodes = self.node_by_port.get(first), self.node_by_port.get(last)
            owners = [self.port_facilities.get(pid) or node_owners.get(nid,set()) for pid,nid in zip((first,last),nodes)]
            if None in nodes:
                continue
            if len(owners[0]) == len(owners[1]) == 1:
                a, b = next(iter(owners[0])), next(iter(owners[1]))
                if a != b:
                    self.links.append(InterfacilityLink('line:' + eid.value, a, b, self.model.equipment[eid].name,
                        (eid,), nodes[0], nodes[1], first, last, self.link_status((eid,), nodes),
                        nominal_kv=self.connection_voltage(nodes)))
            elif owners[0] and owners[1] and (len(owners[0]) > 1 or len(owners[1]) > 1):
                self.issue('ambiguous_line_sides', 'Стороны линии принадлежат нескольким объектам; направление обзорной связи не определено.', equipment_ids=(eid,))
                for fid in owners[0] | owners[1]:
                    self.facilities[fid]['counts_complete'] = False

    def finish_facility(self, f):
        levels = []
        unknown_ids = set(f['equipment'] - f['recognized'])
        for level in f['levels'].values():
            sections = []
            for section in level['sections'].values():
                sections.append(self.target(section['key'], 'section', section['name'], children=section['bays'],
                                            node_ids=section['nodes'], nominal_kv=level['kv']))
            if level['unassigned']:
                sections.append(self.target(level['key'] + ':unassigned', 'section', 'Секция не определена',
                                            children=level['unassigned'], role='unassigned'))
                for bay in level['unassigned']:
                    unknown_ids.update(bay.equipment_ids)
            levels.append(self.target(level['key'], 'voltage_level', level['name'], children=sections, nominal_kv=level['kv']))
        missing_bays = f['equipment'] - f['recognized']
        if missing_bays:
            key = 'facility:' + f['id'] + ':unassigned'
            self.issue('unassigned_equipment', 'Принадлежность этих аппаратов присоединениям требует уточнения.', key, sorted(missing_bays))
            levels.append(self.target(key, 'unassigned', 'Не распределено',
                children=[self.leaf(eid, key + ':' + eid.value) for eid in sorted(missing_bays)]))
            self.assigned.update(missing_bays)
        if f['preferred'] is not None and f['buses'] and not any(
                f['preferred'] in self.pages_by_node[nid] for nid in f['buses']):
            self.issue('changed_facility_page', 'Исходный лист больше не содержит секций объекта; выберите представление.', 'facility:' + f['id'])
            f['preferred'] = None
        target = self.target('facility:' + f['id'], 'facility', f['name'], children=levels,
                             equipment_ids=f['equipment'], node_ids=f['buses'], preferred_page_id=f['preferred'])
        status = replace(self.topology_status(f['buses'], unresolved=f['unresolved_sections']), repair=self.in_repair(target.equipment_ids))
        if set(target.equipment_ids) & self.blocked_equipment:
            status = replace(status, code='error', label='Есть ошибка топологии оборудования')
        target = replace(target, status=status)
        transformers = {eid for eid in target.equipment_ids if self.behaviors[eid] in
            {'transformer_2w','transformer_3w','legacy.transformer_2w','legacy.transformer_3w'}}
        input_distinct = (len(set(f['input_buses'])) == 2 if f['incoming'] == 2 and f['input_complete'] and len(f['input_buses'])==2 else None)
        complete = not unknown_ids and f['input_complete'] and f['counts_complete']
        outgoing_states = [bay.status.code for bay in f['outgoing_bays']]
        in_service = (sum(code == 'connected' for code in outgoing_states)
                      if complete and self.topology is not None and not any(code in {'unknown','error'} for code in outgoing_states) else None)
        return FacilityOverview(f['id'], f['name'], f['kind'], f['parent'], tuple(sorted(f['upstream'])), target,
            len(transformers), f['outgoing'], f['incoming'], len(unknown_ids),
            status,
            tuple(sorted({level['kv'] for level in f['levels'].values() if isinstance(level['kv'], (int,float))},reverse=True)),
            input_distinct, complete, in_service)

    def build(self):
        structure = getattr(self.project, 'structure', None)
        if structure is not None and structure.facilities:
            self.structure(structure)
        else:
            metadata = getattr(self.project, 'metadata', {})
            manifests = [(key, metadata[key]) for key in ('oilfield_demo','training_demo') if key in metadata]
            if len(manifests) == 1 and isinstance(manifests[0][1], Mapping):
                self.manifest(manifests[0][1], manifests[0][0])
            elif manifests:
                self.issue('ambiguous_manifest', 'Описание объектов неоднозначно или повреждено; оборудование оставлено нераспределённым.')
        self.discover_links()
        facilities = tuple(self.finish_facility(f) for f in self.facilities.values())
        remaining = set(self.model.equipment) - self.assigned
        key = 'unassigned'
        if remaining:
            self.issue('unassigned_equipment', 'Для части оборудования не задана проверенная принадлежность объектам.', key, sorted(remaining))
        unassigned = self.target(key, 'unassigned', 'Не распределено',
            children=[self.leaf(eid, key + ':' + eid.value) for eid in sorted(remaining)])
        by_id = {f.id:f for f in facilities}
        def nested(fid):
            target = by_id[fid].target
            children = [nested(f.id) for f in facilities if f.parent_id == fid]
            if not children:
                return target
            return replace(target, children=(*children,*target.children))
        roots = tuple(nested(f.id) for f in facilities if f.parent_id is None)
        if remaining or not roots:
            roots = (*roots, unassigned)
        targets = {}
        def index(target):
            if target.key in targets and targets[target.key] != target:
                raise ValueError('Навигационный ID повторяется с разными целями: ' + target.key)
            targets[target.key] = target
            for child in target.children:
                index(child)
        for root in roots:
            index(root)
        index(unassigned)
        equipment_owners, node_owners, page_owners = defaultdict(set), defaultdict(set), defaultdict(set)
        for facility in facilities:
            for eid in facility.target.equipment_ids:
                equipment_owners[eid].add(facility.id)
            for nid in facility.target.node_ids:
                node_owners[nid].add(facility.id)
            if facility.preferred_page_id is not None:
                page_owners[facility.preferred_page_id].add(facility.id)
        if self.model.revision != self.revision:
            raise ValueError('Электрическая модель изменилась во время построения обзора.')
        freeze = lambda value: {key:tuple(sorted(items)) for key,items in value.items()}
        return NetworkOverview(getattr(self.project, 'metadata', {}).get('name', self.model.name), facilities, roots,
            tuple(self.links), unassigned, tuple(self.diagnostics), self.state_id, self.fingerprint, targets,
            freeze(equipment_owners), freeze(node_owners), freeze(page_owners), self.source)
