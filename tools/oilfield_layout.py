# -*- coding: utf-8 -*-
"""Deterministic facility sheets for the separate oilfield demonstration.

Every sheet refers to the same electrical model. Repeated incoming/outgoing
views never introduce another conductor, switch, or electrical connection.
"""
from __future__ import annotations

from collections import defaultdict

from rza_calc.domain.diagram import (
    DiagramDocument, DiagramPage, DiagramRoute, DiagramRouteId, DiagramRouteKind,
    GraphicalRepresentation, GraphicalRepresentationId, PageId,
    RepresentationTargetKind, RouteAnchorKind, RouteEndpointAnchor,
    RouteWaypoint, RouteWaypointId,
)
from rza_calc.domain.electrical import deterministic_id
from rza_calc.editor.orientation import rotated_port_layout
from rza_calc.editor.symbols import canonical_key, default_size
from tools.autolayout import legacy_id, orthogonal, symbol_key_for


class Sheet:
    def __init__(self, project, facility, order, network):
        self.project = project
        self.model = project.electrical_model
        self.network = network
        self.facility = facility
        self.page = DiagramPage(
            deterministic_id(PageId, 'oilfield-demo', facility['id']),
            f"{order + 1:02d} · {facility['name']}", order=order,
        )
        self.nodes = {legacy_id(n): n for n in self.model.electrical_nodes.values()}
        self.equipment = {legacy_id(e): e for e in self.model.equipment.values()}
        self.connections = defaultdict(list)
        for connection in self.model.connections.values():
            self.connections[self.model.ports[connection.port_id].equipment_id].append(connection)
        self.reps = {}
        self.node_reps = {}
        self.eq_reps = {}
        self.routes = {}
        self.lines = set()
        self.sizes = {}

    def sid(self, cls, *parts):
        return deterministic_id(cls, 'oilfield-demo', self.facility['id'], *parts)

    def node(self, nid, x, y, *, bus=False, label=None, linked_page=None, bus_width=1000.):
        if nid in self.node_reps:
            return self.node_reps[nid]
        obj = self.nodes[nid]
        width, height = (bus_width, 12.) if bus else (4., 4.)
        graphics = {
            'width': width, 'height': height, 'label_visible': bus or label is not None,
            'label_x': -width / 2 if bus else 14.,
            'label_y': -38. if bus else -14., 'line_width': 2.4 if bus else 1.,
            'label_manual': False,
        }
        ext = {'stage3_graphics': graphics}
        if linked_page:
            ext['linked_page_id'] = deterministic_id(PageId, 'oilfield-demo', linked_page).value
        rep = GraphicalRepresentation(
            self.sid(GraphicalRepresentationId, 'node', nid), self.page.id,
            RepresentationTargetKind.ELECTRICAL_NODE, electrical_node_id=obj.id,
            x=x, y=y, symbol_key='busbar' if bus else 'connection_point',
            label=obj.name if label is None else label, extensions=ext,
        )
        self.reps[rep.id] = rep
        self.node_reps[nid] = rep
        self.sizes[rep.id] = (width, height)
        return rep

    def equipment_rep(self, eid, x, y, *, rotation=90, label=None, label_offset=(40.,-14.)):
        if eid in self.eq_reps:
            return self.eq_reps[eid]
        obj = self.equipment[eid]
        definition = self.model.equipment_types[(obj.type_id, obj.type_version)]
        key = canonical_key(symbol_key_for(definition), definition.behavior_key)
        width, height = default_size(key)
        if key in {'generator', 'load', 'source'}:
            rotation = 0
        if label is None:
            # Full equipment names remain in the model/inspector.
            label = obj.name
        gx,gy=label_offset
        label_x,label_y={0:(gx,gy),90:(gy,-gx),180:(-gx,-gy),270:(-gy,gx)}[rotation]
        rep = GraphicalRepresentation(
            self.sid(GraphicalRepresentationId, 'equipment', eid), self.page.id,
            RepresentationTargetKind.EQUIPMENT, equipment_id=obj.id,
            x=x, y=y, rotation_deg=rotation, label=label,
            extensions={'stage3_graphics': {
                'width': width, 'height': height, 'label_visible': True,
                'label_x': label_x, 'label_y': label_y, 'line_width': 2., 'label_manual':False,
            }},
        )
        self.reps[rep.id] = rep
        self.eq_reps[eid] = rep
        self.sizes[rep.id] = (width, height)
        return rep

    def chain(self, branch_ids, x, start_y, end_y, *, start_label=None, end_label=None,
              start_page=None, end_page=None):
        branches = [self.network.branches[eid] for eid in branch_ids]
        assert branches
        nids = [branches[0].node_from, *(b.node_to for b in branches)]
        assert all(a.node_to == b.node_from for a, b in zip(branches, branches[1:])), branch_ids
        step = (end_y - start_y) / len(branches)
        for i, nid in enumerate(nids):
            label = start_label if i == 0 else end_label if i == len(branches) else None
            link = start_page if i == 0 else end_page if i == len(branches) else None
            self.node(nid, x, start_y + i * step, label=label, linked_page=link)
        for i, branch in enumerate(branches):
            if branch.kind == 'line':
                self.lines.add(branch.id)
            else:
                self.equipment_rep(branch.id, x, start_y + (i + .5) * step,
                                   rotation=90 if step > 0 else 270,
                                   label=short_label(branch.name))

    def _anchor_geometry(self, rep, port):
        equipment = self.model.equipment[rep.equipment_id]
        definition = self.model.equipment_types[(equipment.type_id, equipment.type_version)]
        w, h = self.sizes[rep.id]
        return next(p for p in rotated_port_layout(
            equipment, definition, width=w, height=h, rotation=rep.rotation_deg,
            center_x=rep.x, center_y=rep.y,
        ) if p.port_id == port)

    def _route(self, key, kind, start, end, points, *, eid=None, nid=None):
        points = orthogonal(points)
        if len(points) < 2:
            raise ValueError(f'Zero length demo route: {key}')
        rid = self.sid(DiagramRouteId, key)
        route = DiagramRoute(
            rid, self.page.id, kind, start, end, equipment_id=eid,
            electrical_node_id=nid,
            waypoints=tuple(RouteWaypoint(self.sid(RouteWaypointId, key, str(i)), x, y)
                            for i, (x, y) in enumerate(points)),
        )
        self.routes[rid] = route

    def finish(self):
        node_by_id = {obj.id: nid for nid, obj in self.nodes.items()}
        for eid, rep in self.eq_reps.items():
            equipment = self.equipment[eid]
            for c in self.connections[equipment.id]:
                node_rep = self.node_reps.get(node_by_id[c.electrical_node_id])
                if node_rep is None:
                    raise ValueError(f'Missing sheet endpoint for {eid}: {c.electrical_node_id}')
                anchor = self._anchor_geometry(rep, c.port_id)
                width = self.sizes[node_rep.id][0]
                if width > 40:
                    if abs(anchor.y-node_rep.y) < 1e-6:
                        tx = node_rep.x + (width/2 if anchor.x > node_rep.x else -width/2)
                    else:
                        tx = max(node_rep.x-width/2+24, min(anchor.x, node_rep.x+width/2-24))
                else:
                    tx = node_rep.x
                target = (tx, node_rep.y)
                if str(anchor.direction) in {'left','right'}:
                    points = [(anchor.x,anchor.y),(tx,anchor.y),target]
                else:
                    points = [(anchor.x,anchor.y),(anchor.x,node_rep.y),target]
                self._route('connection:'+c.id.value, DiagramRouteKind.NODE_CONNECTION,
                    RouteEndpointAnchor(RouteAnchorKind.EQUIPMENT_PORT, rep.id,
                                        c.electrical_node_id, target_port_id=c.port_id),
                    RouteEndpointAnchor(RouteAnchorKind.BUS if width>40 else RouteAnchorKind.ELECTRICAL_NODE,
                                        node_rep.id,c.electrical_node_id),
                    points,nid=c.electrical_node_id)
        for eid in sorted(self.lines):
            equipment = self.equipment[eid]
            connections = {self.model.port_definition(c.port_id).role: c
                           for c in self.connections[equipment.id]}
            ends=[]
            for role in ('from','to'):
                c=connections[role]
                rep=self.node_reps[node_by_id[c.electrical_node_id]]
                ends.append((rep,c))
            a,b=ends
            self._route('line:'+eid,DiagramRouteKind.EQUIPMENT_BRANCH,
                RouteEndpointAnchor(RouteAnchorKind.ELECTRICAL_NODE,a[0].id,a[1].electrical_node_id,
                                    branch_port_id=a[1].port_id),
                RouteEndpointAnchor(RouteAnchorKind.ELECTRICAL_NODE,b[0].id,b[1].electrical_node_id,
                                    branch_port_id=b[1].port_id),
                [(a[0].x,a[0].y),(a[0].x,b[0].y),(b[0].x,b[0].y)],eid=equipment.id)


def short_label(name):
    """Keep apparatus designations readable; properties retain full names."""
    return name if len(name) <= 38 else name[:35].rstrip() + '…'


def build_layout(project, manifest):
    """Build one detailed sheet per facility, with physical incoming circuits."""
    sheets=[]
    network=project.network
    for order, facility in enumerate(manifest['facilities']):
        sheet=Sheet(project,facility,order,network)
        _place_facility(sheet,facility,manifest)
        sheet.finish()
        sheets.append(sheet)
    first=sheets[0].page.id
    result=DiagramDocument(
        project.diagram.id,'Нефтепромысел · ГТЭС и распределительная сеть',
        pages={s.page.id:s.page for s in sheets},
        representations={key:value for s in sheets for key,value in s.reps.items()},
        routes={key:value for s in sheets for key,value in s.routes.items()},
        revision=project.diagram.revision+1,
        extensions={'stage3_workspace':{
            'mode':'edit','grid_visible':True,'snap_enabled':True,'grid_size':20.,
            'zoom':.75,'view_x':-660.,'view_y':-60.,'active_page_id':first.value,
            'open_panels':['project','properties','issues'],
            'developer_diagnostics':False,'confirm_switching':True,
        }},
    )
    problems=result.validate_targets(project.electrical_model)
    if problems:
        raise ValueError('Invalid oilfield diagram: '+ '; '.join(problems))
    return result


def _place_facility(sheet, facility, manifest):
    facilities={f['id']:f for f in manifest['facilities']}
    orders={f['id']:i+1 for i,f in enumerate(manifest['facilities'])}
    departures=[*facility['outgoing'],*facility['loads']]
    gtes=facility['kind']=='gtes'
    counts=[sum(row['section']==section for row in (facility['outgoing'] if gtes else departures))
            for section in (1,2)]
    width=max(1000.,(max(counts,default=1)-1)*260.+180.)
    centers={1:-(width/2+160.),2:width/2+160.}
    bus_y={'hv':600. if gtes else 0.,'lv':0. if gtes else 600.}
    for level in ('hv','lv'):
        for section,nid in enumerate(facility['buses'][level],1):
            sheet.node(nid,centers[section],bus_y[level],bus=True,bus_width=width)
        sheet.equipment_rep(facility['bus_ties'][level],0.,bus_y[level],rotation=0,
                            label=f"СВ {facility[level+'_kv']:g} кВ",label_offset=(-100.,-66.))
    for row in facility['incoming']:
        upstream=facilities[row['upstream_id']]
        section=row['section']
        label=(f"Из {upstream['name']}\n"
               f"{'I' if section==1 else 'II'} секция · лист {orders[upstream['id']]:02d}")
        sheet.chain(row['branch_ids'],centers[section],-660.,0.,
                    start_label=label,start_page=upstream['id'])
    for row in facility['transformers']:
        sheet.chain(row['branch_ids'],centers[row['section']],bus_y['hv'],bus_y['lv'])
    for section in (1,2):
        generators=[r for r in facility.get('generators',[]) if r['section']==section]
        for index,row in enumerate(generators):
            x=centers[section]+(index-(len(generators)-1)/2)*300.
            terminal=row['node_ids'][0]
            sheet.node(terminal,x,-330.)
            sheet.equipment_rep(row['id'],x,-400.,label=sheet.equipment[row['id']].name.split(' 6 ')[0])
            sheet.chain([row['breaker']],x,-330.,0.)
        rows=[r for r in (facility['outgoing'] if gtes else departures) if r['section']==section]
        for index,row in enumerate(rows):
            x=centers[section]+(index-(len(rows)-1)/2)*260.
            # The power station feeds the field at 110 kV. Local station
            # auxiliaries can be attached to its 10 kV bus in the same model.
            start_node=sheet.network.branches[row['branch_ids'][0]].node_from
            y=sheet.node_reps[start_node].y
            end_y=1120.
            if 'target_facility_id' in row:
                target=facilities[row['target_facility_id']]
                label=f"{target['name']}\nВвод {section} · лист {orders[target['id']]:02d}"
                sheet.chain(row['branch_ids'],x,y,end_y,
                            end_label=label,end_page=target['id'])
            else:
                sheet.chain(row['branch_ids'],x,y,end_y-100.)
                sheet.equipment_rep(row['load_id'],x,end_y,
                                    label=short_label(row['name']))
        if gtes:
            # Keep 10 kV auxiliaries outside the 110 kV bus span: a drawn
            # crossing at a bus would misleadingly suggest a connection.
            auxiliaries=[r for r in facility['loads'] if r['section']==section]
            for index,row in enumerate(auxiliaries):
                x=(-1 if section==1 else 1)*(width+160.+220.+index*260.)
                sheet.chain(row['branch_ids'],x,0.,1020.)
                sheet.equipment_rep(row['load_id'],x,1120.,label=short_label(row['name']))
