"""Common navigation overview, built only from explicit project membership."""
from __future__ import annotations

from types import SimpleNamespace
from dataclasses import replace
from math import isfinite
from collections.abc import Mapping

from PySide6.QtCore import QRectF, QSize, Qt, Signal, QTimer
from PySide6.QtGui import QColor, QPen, QPainter
from PySide6.QtWidgets import (QComboBox, QFrame, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QPushButton, QSplitter, QStyledItemDelegate, QTabWidget,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from ..application.network_overview import build_network_overview
from ..domain.electrical import thaw_json
from .diagram_preview import DiagramPreviewWidget, PreviewTarget
from .preview_annotations import make_preview_annotations
from .editor_scene import is_switch_control
from .outline_icons import outline_icon
from .overview_canvas import OverviewCanvas, OverviewCard, OverviewWire, STATUS_COLORS, voltage_color
from .overview_results import OverviewResultsWidget, OverviewResultsSnapshot, SummaryCard, OverviewTab


KEY_ROLE = int(Qt.ItemDataRole.UserRole)
STATUS_ROLE = KEY_ROLE + 1
VOLTAGE_ROLE = KEY_ROLE + 2
REPAIR_ROLE = KEY_ROLE + 3
OVERVIEW_LAYOUT_KEY = 'network_overview_layout_v1'


def _facility_icon(kind):
    return {'gtes': 'power_station', 'power_plant': 'power_station', 'cp': 'substation', 'central': 'substation', 'ps': 'substation',
            'central_substation': 'substation'}.get(kind, kind)


def _icon_kind(target, model):
    if target.role == 'busbar' and not target.equipment_ids:
        return 'bus_system'
    if target.kind == 'bay':
        return {'bus_coupler': 'bus_section_breaker', 'generator': 'generator',
                'transformer': 'transformer_2w'}.get(target.role, 'feeder')
    direct = {'voltage_level': 'switchyard', 'level': 'switchyard',
              'section': 'bus_system', 'bus_section': 'bus_system',
              'outgoing': 'feeder', 'incoming': 'feeder', 'bay': 'feeder'}
    if target.kind in direct:
        return direct[target.kind]
    if target.kind == 'equipment' and target.equipment_ids:
        item = model.equipment.get(target.equipment_ids[0])
        definition = model.equipment_type(item.type_id, item.type_version) if item else None
        if definition is not None:
            if is_switch_control(model, item.id):
                return 'circuit_breaker'
            symbol = str(definition.extensions.get('diagram_symbol_key') or definition.behavior_key.removeprefix('legacy.'))
            properties = model.effective_equipment_properties(item.id)
            line_type = properties.get('line_type', '')
            if definition.behavior_key == 'legacy.line':
                line_type = properties.get('legacy_payload', {}).get('line_type', '')
            if item.id in model.line_sections:
                line_type = model.logical_line_for_section(item.id).line_kind.value
            return {'line': {'cable': 'cable_line', 'overhead': 'overhead_line'}.get(line_type, 'feeder'),
                    'line_section': {'cable': 'cable_line', 'overhead': 'overhead_line'}.get(line_type, 'feeder'),
                    'transformer': 'transformer_2w', 'recloser': 'circuit_breaker'}.get(symbol, symbol)
    return target.kind


class _StatusDelegate(QStyledItemDelegate):
    """Voltage and state use a trailing column; the glyph stays monochrome."""
    def paint(self, painter, option, index):
        if index.column() != 1:
            super().paint(painter, option, index)
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = option.rect
        voltage = index.data(VOLTAGE_ROLE)
        if voltage is not None:
            text = f'{float(voltage):g}'.replace('.', ',') + ' кВ'
            painter.setPen(QColor(voltage_color(voltage)))
            painter.drawText(rect.adjusted(1, 0, -23, 0), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, text)
        state = index.data(STATUS_ROLE) or 'unknown'
        center = rect.center()
        center.setX(rect.right() - 10)
        painter.setPen(QPen(QColor(STATUS_COLORS.get(state, '#94A0B0')), 1.2))
        painter.setBrush(Qt.BrushStyle.NoBrush if state == 'unknown' else QColor(STATUS_COLORS.get(state, '#94A0B0')))
        painter.drawEllipse(center, 3, 3)
        if index.data(REPAIR_ROLE):
            painter.setPen(QPen(QColor('#9D764A'), 1.2))
            painter.drawRect(QRectF(rect.right() - 24, center.y() - 4, 7, 7))
        painter.restore()


class NetworkOverviewWorkspace(QWidget):
    openRequested = Signal(object)
    pathChanged = Signal(str)
    message = Signal(str)

    def __init__(self, vm, controller, source_scene, parent=None):
        super().__init__(parent)
        self.vm, self.controller, self.source_scene = vm, controller, source_scene
        self.setObjectName('networkOverviewWorkspace')
        self._source_key = None
        self.projection = None
        self._selected_key = ''
        self._tree_items = {}
        self._parents = {}
        self._target_states = {}
        self._point_result = None
        self._dirty = True
        self._positions = {}
        self._model = None
        self._topology = None
        self._facility_by_target = {}
        self._displayed_point = None
        self._selected_pages = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(8)
        title = QHBoxLayout()
        heading = QLabel('Обзор объекта')
        heading.setObjectName('inspectorTitle')
        title.addWidget(heading)
        title.addSpacing(15)
        self.mode_label = QLabel()
        self.mode_label.setObjectName('muted')
        title.addWidget(self.mode_label, 1)
        fit = QPushButton('Показать всё')
        fit.clicked.connect(lambda: self.canvas.fit_all())
        title.addWidget(fit)
        self.collapse_button = QPushButton('Свернуть итоги')
        self.collapse_button.setCheckable(True)
        self.collapse_button.toggled.connect(self._collapse_results)
        title.addWidget(self.collapse_button)
        layout.addLayout(title)
        self.vertical = QSplitter(Qt.Orientation.Vertical)
        self.horizontal = QSplitter(Qt.Orientation.Horizontal)
        tree_card = QFrame()
        tree_card.setObjectName('overviewTreeCard')
        tl = QVBoxLayout(tree_card)
        tl.setContentsMargins(10, 12, 8, 8)
        label = QLabel('Структура сети')
        label.setObjectName('sectionTitle')
        tl.addWidget(label)
        self.search = QLineEdit()
        self.search.setPlaceholderText('Найти объект или оборудование…')
        self.search.setClearButtonEnabled(True)
        tl.addWidget(self.search)
        self.type_filter = QComboBox()
        self.type_filter.addItem('Все типы оборудования', '')
        for caption, kind in [('Генераторы', 'generator'), ('Трансформаторы', 'transformer_2w'),
                              ('Выключатели', 'circuit_breaker'), ('КЛ / ВЛ', 'lines'), ('Нагрузки', 'load')]:
            self.type_filter.addItem(caption, kind)
        tl.addWidget(self.type_filter)
        self.tree = QTreeWidget()
        self.tree.setObjectName('overviewNetworkTree')
        self.tree.setColumnCount(2)
        self.tree.setHeaderHidden(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setIconSize(QSize(16, 16))
        self.tree.setIndentation(14)
        self.tree.setItemDelegate(_StatusDelegate(self.tree))
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self.tree.setColumnWidth(1, 78)
        self.tree.itemClicked.connect(lambda item, column: self.select_target(item.data(0, KEY_ROLE)))
        self.tree.itemDoubleClicked.connect(lambda item, column: self.open_target(item.data(0, KEY_ROLE)))
        self.search.textChanged.connect(self._filter_tree)
        self.type_filter.currentIndexChanged.connect(self._filter_tree)
        tl.addWidget(self.tree, 1)
        hint = QLabel('● питание   ○ не определено   □ ремонт')
        hint.setObjectName('muted')
        hint.setWordWrap(True)
        tl.addWidget(hint)
        self.horizontal.addWidget(tree_card)
        center = QFrame()
        center.setObjectName('overviewCanvasCard')
        cl = QVBoxLayout(center)
        cl.setContentsMargins(0, 0, 0, 6)
        self.canvas = OverviewCanvas()
        self._fit_timer = QTimer(self)
        self._fit_timer.setSingleShot(True)
        self._fit_timer.timeout.connect(self.canvas.fit_all)
        self.canvas.previewRequested.connect(self.select_target)
        self.canvas.openRequested.connect(self.open_target)
        self.canvas.positionsChanged.connect(self._remember_positions)
        cl.addWidget(self.canvas, 1)
        hint = QLabel('  Щелчок — мини-схема  ·  Двойной щелчок — открыть  ·  Колесо — масштаб')
        hint.setObjectName('muted')
        hint.setWordWrap(True)
        cl.addWidget(hint)
        self.horizontal.addWidget(center)
        right = QFrame()
        right.setObjectName('overviewInspectorCard')
        rl = QVBoxLayout(right)
        rl.setContentsMargins(10, 10, 10, 10)
        self.preview = DiagramPreviewWidget()
        self.preview.openRequested.connect(self._open_preview)
        rl.addWidget(self.preview, 3)
        self.pages = QComboBox()
        self.pages.currentIndexChanged.connect(self._select_preview_page)
        rl.addWidget(self.pages)
        self.voltage_filter = QComboBox()
        self.voltage_filter.setAccessibleName('Часть объекта в мини-схеме')
        self.voltage_filter.currentIndexChanged.connect(self._select_preview_page)
        rl.addWidget(self.voltage_filter)
        self.summary = QLabel('Выберите ГТЭС, подстанцию или КТП.')
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(Qt.TextFormat.PlainText)
        rl.addWidget(self.summary)
        self.inspector_tabs = QTabWidget()
        self.inspector_tabs.setObjectName('overviewInspectorTabs')
        self.details = {}
        for name in ('Параметры', 'КЗ', 'РЗА', 'Фидеры'):
            text = QLabel()
            text.setTextFormat(Qt.TextFormat.PlainText)
            text.setWordWrap(True)
            text.setAlignment(Qt.AlignmentFlag.AlignTop)
            box = QWidget()
            bl = QVBoxLayout(box)
            bl.setContentsMargins(10, 12, 10, 8)
            bl.addWidget(text)
            self.inspector_tabs.addTab(box, name)
            self.details[name] = text
        rl.addWidget(self.inspector_tabs, 1)
        self.horizontal.addWidget(right)
        tree_card.setMinimumWidth(240)
        right.setMinimumWidth(330)
        self.horizontal.setSizes([280, 740, 420])
        self.horizontal.setStretchFactor(1, 1)
        self.vertical.addWidget(self.horizontal)
        self.results = OverviewResultsWidget()
        self.vertical.addWidget(self.results)
        self.vertical.setSizes([650, 235])
        self.vertical.setStretchFactor(0, 1)
        layout.addWidget(self.vertical, 1)
        self.setStyleSheet('''
            QWidget#networkOverviewWorkspace { background: #F3F7FB; }
            QFrame#overviewTreeCard, QFrame#overviewCanvasCard, QFrame#overviewInspectorCard {
                background: white; border: 1px solid #DFE7EF; border-radius: 10px; }
            QTreeWidget#overviewNetworkTree { border: none; background: white; }
            QTreeWidget#overviewNetworkTree::item { height: 27px; border-radius: 5px; }
            QTreeWidget#overviewNetworkTree::item:selected { background: #E6F0FB; color: #294F77; }
            QTreeWidget#overviewNetworkTree::item:hover { background: #F1F6FC; }
            QTabWidget#overviewInspectorTabs::pane { border: 1px solid #E2E9F0; border-radius: 7px; background: #FAFCFE; }
        ''')

    def showEvent(self, event):
        super().showEvent(event)
        self.ensure_current()

    def invalidate(self):
        self._dirty = True
        if self.isVisible():
            self.ensure_current()

    def ensure_current(self):
        document = self.controller.diagram
        model = self.source_scene._model or self.controller.model
        state_id = self.source_scene._operating_state_id
        key = (id(document), id(model), model.revision, state_id)
        if not self._dirty and key == self._source_key:
            return
        existed = self.projection is not None
        if existed:
            self._fit_timer.stop()
        viewport_center = self.canvas.mapToScene(self.canvas.viewport().rect().center())
        viewport_transform = self.canvas.transform()
        self._source_key, self._dirty = key, False
        self._model = model
        self._topology = self.source_scene.topology_snapshot
        source = SimpleNamespace(electrical_model=model, diagram=document,
                                 structure=self.vm.project.structure, metadata=self.vm.project.metadata)
        self.projection = build_network_overview(source, operating_state_id=state_id, topology=self._topology)
        layout_data = document.extensions.get(OVERVIEW_LAYOUT_KEY, {})
        raw_positions = layout_data.get('positions', {}) if isinstance(layout_data, Mapping) else {}
        raw_positions = raw_positions if isinstance(raw_positions, Mapping) else {}
        self._positions = {key: tuple(value) for key, value in raw_positions.items()
                           if key in self.projection.targets and isinstance(value, (list, tuple)) and len(value) == 2
                           and all(isinstance(n, (int, float)) and not isinstance(n, bool) and isfinite(n) and abs(n) <= 1e6 for n in value)}
        self.preview.set_project(document, model, operating_state_id=state_id,
                                 topology_state_available=state_id is not None)
        state = model.operating_states.get(state_id)
        self.mode_label.setText(('Черновик режима · ' if self.vm.mode_draft is not None else 'Режим · ') +
                                (state.name if state else 'не выбран') + '  ·  Просмотр без расчёта токов')
        self._facility_by_target = {}
        for facility in self.projection.facilities:
            def attach(target):
                self._facility_by_target[target.key] = facility
                for child in target.children:
                    attach(child)
            attach(facility.target)
        self._build_tree()
        self._build_cards()
        target = self._selected_key if self._selected_key in self.projection.targets else ''
        if target:
            self.select_target(target)
        else:
            self._selected_key = ''
            self.preview.clear()
            self.pages.clear()
            self.pages.hide()
            self.summary.setText('Выберите ГТЭС, подстанцию или КТП.')
            for label in self.details.values():
                label.clear()
            self.pathChanged.emit(self.projection.name)
            self._render_results(None)
        if existed:
            self.canvas.setTransform(viewport_transform)
            self.canvas.centerOn(viewport_center)
        else:
            self._fit_timer.start(0)

    def _status(self, target):
        status = target.status
        code = {'open': 'deenergized', 'connected': 'energized', 'partial': 'mixed'}.get(status.code, status.code)
        reason = status.label
        if target.diagnostics:
            code = 'error'
            reason += '\n' + '; '.join(d.message for d in target.diagnostics)
        return code, reason, status.repair

    def _build_tree(self):
        expanded = {key for key, item in self._tree_items.items() if item.isExpanded()}
        scroll = self.tree.verticalScrollBar().value()
        self.tree.clear()
        self._tree_items, self._parents, self._target_states = {}, {}, {}
        root = QTreeWidgetItem([self.projection.name, ''])
        root.setToolTip(0, self.projection.name)
        self.tree.addTopLevelItem(root)
        root.setExpanded(True)
        def add(target, parent):
            item = QTreeWidgetItem([target.name, ''])
            item.setData(0, KEY_ROLE, target.key)
            facility = self._facility_by_target.get(target.key)
            kind = _facility_icon(facility.kind) if facility and target.key == facility.target.key else _icon_kind(target, self._model)
            item.setIcon(0, outline_icon(kind))
            status, reason, repair = self._status(target)
            self._target_states[target.key] = (status, reason, repair)
            item.setData(1, STATUS_ROLE, status)
            item.setData(1, VOLTAGE_ROLE, target.nominal_kv)
            item.setData(1, REPAIR_ROLE, repair)
            item.setToolTip(0, target.name + '\n' + reason)
            item.setToolTip(1, reason)
            parent.addChild(item)
            self._tree_items[target.key] = item
            self._parents[target.key] = parent.data(0, KEY_ROLE)
            for child in target.children:
                add(child, item)
            item.setExpanded(target.key in expanded or (not expanded and target.kind in {'facility', 'power_plant', 'substation', 'ktp'}))
        for target in self.projection.roots:
            add(target, root)
        if self.projection.unassigned.key not in self._tree_items:
            add(self.projection.unassigned, root)
        self._filter_tree()
        self.tree.verticalScrollBar().setValue(scroll)

    def _build_cards(self):
        cards = []
        for f in self.projection.facilities:
            diagnostics = len(self._diagnostics(f.target))
            working = self._working_feeders(f)
            cards.append(OverviewCard(f.target.key, f.name, _facility_icon(f.kind), f.voltage_levels,
                f'{"≥ " if not f.counts_complete else ""}{f.outgoing_count} фидеров · {f.transformer_count} трансф.',
                f'В работе: {working if working is not None else "—"} · замечаний: {diagnostics}', self._status(f.target)[0],
                f'{f.name}\n{f.status.label}\nДвойной щелчок — открыть сохранённую схему.'))
        keys = {f.id: f.target.key for f in self.projection.facilities}
        wires = []
        pairs = {}
        for row in self.projection.links:
            pair = (row.from_facility_id, row.to_facility_id)
            pairs[pair] = pairs.get(pair, 0) + 1
            voltage = row.nominal_kv
            wires.append(OverviewWire(row.id, keys[row.from_facility_id], keys[row.to_facility_id],
                f'Ввод {pairs[pair]}' + (f' · {voltage:g} кВ' if voltage else ''), voltage,
                row.name + '\n' + row.status.label + '\nСтрелка обозначает связь, не направление мощности.'))
        self.canvas.set_network(cards, wires, positions=self._positions)

    def _filter_tree(self, *args):
        text = self.search.text().casefold().strip()
        kind = self.type_filter.currentData()
        def visit(item):
            key = item.data(0, KEY_ROLE)
            target = self.projection.targets.get(key) if self.projection else None
            icon_kind = _icon_kind(target, self._model) if target else ''
            type_match = (not kind or icon_kind == kind
                or (kind == 'lines' and (icon_kind in {'overhead_line', 'cable_line'}
                    or target is not None and target.role.removeprefix('legacy.') in {'line', 'line_section'}))
                or kind == 'transformer_2w' and icon_kind in {'transformer_3w', 'autotransformer'})
            own = text in item.text(0).casefold() and type_match
            child = False
            for i in range(item.childCount()):
                child = visit(item.child(i)) or child
            item.setHidden(not (own or child))
            if (text or kind) and child:
                item.setExpanded(True)
            return own or child
        for i in range(self.tree.topLevelItemCount()):
            visit(self.tree.topLevelItem(i))

    def path_for(self, key):
        names = []
        while key:
            target = self.projection.targets.get(key)
            if target:
                names.append(target.name)
            key = self._parents.get(key)
        return ' → '.join([self.projection.name, *reversed(names)])

    def select_target(self, key):
        if not self.projection or key not in self.projection.targets:
            return
        self._selected_key = key
        target = self.projection.targets[key]
        facility = self._facility_by_target.get(key)
        self.canvas.select_card(facility.target.key if facility else key)
        item = self._tree_items.get(key)
        if item:
            self.tree.setCurrentItem(item)
        self.pathChanged.emit(self.path_for(key))
        self.pages.blockSignals(True)
        self.pages.clear()
        for page in target.page_ids:
            if page in self.controller.diagram.pages:
                self.pages.addItem(self.controller.diagram.pages[page].name, page)
        preferred = self._selected_pages.get(key, target.preferred_page_id)
        if preferred:
            self.pages.setCurrentIndex(next((i for i in range(self.pages.count())
                if self.pages.itemData(i) == preferred), 0))
        self.pages.blockSignals(False)
        self.pages.setVisible(self.pages.count() > 1)
        current_filter = self.voltage_filter.currentData()
        self.voltage_filter.blockSignals(True)
        self.voltage_filter.clear()
        self.voltage_filter.addItem('Весь объект', '')
        for child in target.children:
            if child.kind == 'voltage_level':
                self.voltage_filter.addItem(child.name, child.key)
        self.voltage_filter.setCurrentIndex(max(0, self.voltage_filter.findData(current_filter)))
        self.voltage_filter.blockSignals(False)
        self.voltage_filter.setVisible(self.voltage_filter.count() > 1)
        self._select_preview_page()
        status, reason, repair = self._target_states.get(key, ('unknown', 'Состояние не определено', False))
        self.summary.setText(target.name + '\n' + reason + ('\n□ Выведено из работы' if repair else ''))
        self.details['Параметры'].setText(f'Аппаратов: {len(set(target.equipment_ids))}\nЛистов: {len(target.page_ids)}\n' +
            ('\n'.join(d.message for d in target.diagnostics) or 'Состав из сохранённых данных проекта.'))
        self.details['РЗА'].setText('Уставки и их проверки доступны в подробной схеме. Статус питания не означает успешную проверку защит.')
        facility = self._facility_by_target.get(key)
        working = self._working_feeders(facility) if facility else None
        self.details['Фидеры'].setText((f'Отходящих присоединений: {"≥ " if not facility.counts_complete else ""}{facility.outgoing_count}\nВ работе по топологии: {working if working is not None else "—"}\nВводов: {facility.incoming_count}\nВводы и секционные выключатели не входят в число фидеров.' if facility else 'Выберите подстанцию для сводки присоединений.'))
        self._render_results(target)

    def _select_preview_page(self, *args):
        if not self.projection or self._selected_key not in self.projection.targets:
            return
        target = self.projection.targets[self._selected_key]
        filter_key = self.voltage_filter.currentData()
        if filter_key:
            target = self.projection.targets.get(filter_key, target)
        page = self.pages.currentData()
        if page is None:
            self.preview.clear('Для объекта не назначен лист. Проверьте его принадлежность в проекте.')
            return
        self._selected_pages[self._selected_key] = page
        # Whole facility previews show the saved sheet. A leaf highlights its exact glyph.
        focus = target.kind == 'equipment' or bool(filter_key)
        reps = tuple(r.id for r in self.controller.diagram.representations.values() if focus and r.page_id == page and
                     (r.equipment_id in target.equipment_ids or r.electrical_node_id in target.node_ids))
        routes = tuple(r.id for r in self.controller.diagram.routes.values() if focus and r.page_id == page and r.equipment_id in target.equipment_ids)
        self.preview.set_page(page, representation_ids=reps, route_ids=routes, title=target.name)
        facility = self._facility_by_target.get(self._selected_key)
        self.preview.set_annotations(make_preview_annotations(self.controller.diagram, self._model, target, page,
                                     numbering_context=facility.target if facility else target))

    def open_target(self, key):
        if not self.projection or key not in self.projection.targets:
            return
        self.select_target(key)
        if self.preview.target is not None and self.preview.open_button.isEnabled():
            self._open_preview(self.preview.target)

    def _open_preview(self, target):
        self.openRequested.emit(target)

    def _remember_positions(self, positions):
        positions = {key: list(value) for key, value in positions.items()}
        def move_cards(draft):
            extensions = thaw_json(draft.diagram.extensions)
            extensions[OVERVIEW_LAYOUT_KEY] = {'positions': positions}
            draft.diagram = replace(draft.diagram, revision=draft.diagram.revision + 1, extensions=extensions)
        self.controller.history.execute('Переместить карточки общего обзора', move_cards)
        self._positions = dict(positions)
        self.message.emit('Расположение карточек изменено. «Сохранить» запишет его в проект; доступна отмена.')

    def _working_feeders(self, facility):
        return facility.outgoing_in_service

    @staticmethod
    def _diagnostics(target):
        found = {}
        def visit(item):
            for row in item.diagnostics:
                found[(row.code, row.message, row.target_key)] = row
            for child in item.children:
                visit(child)
        visit(target)
        return tuple(found.values())

    def _collapse_results(self, collapsed):
        self.results.setVisible(not collapsed)
        self.collapse_button.setText('Показать итоги' if collapsed else 'Свернуть итоги')

    def set_point_result(self, result):
        self._point_result = result
        if self.projection:
            self._render_results(self.projection.targets.get(self._selected_key))

    def _render_results(self, target):
        if self.projection is None:
            return
        facility = self._facility_by_target.get(target.key) if target else None
        title = target.name if target else self.projection.name
        equipments = set(target.equipment_ids) if target else set(self._model.equipment)
        nodes = set(target.node_ids) if target else set(self._model.electrical_nodes)
        result = self._point_result
        point_rows = []
        if result is not None:
            point = result.request.target
            node = point.node_id or (self._topology.port_to_node.get(point.port_id) if self._topology else None)
            if node in nodes:
                from .view_model import FAULT_TYPE_LABELS
                for row in result.outcomes:
                    label = FAULT_TYPE_LABELS.get(row.spec.kind.value, str(row.spec.kind.value))
                    value = f'{max(abs(i) for i in row.fault.iabc_ka):.3f}' if row.available else '—'
                    point_rows.append((result.node_name, str(label), value, row.message or 'Расчёт в выбранной точке'))
        fault_note = 'Нет актуального точечного расчёта для выбранного объекта. Откройте лист и выберите «Рассчитать КЗ здесь…».'
        self.details['КЗ'].setText(('Актуальный точечный расчёт: ' + result.node_name) if point_rows else fault_note)
        state = self._model.operating_states.get(self.projection.state_id)
        cards = (SummaryCard('Выбранный объект', title, 'Обзор сети'),
                 SummaryCard('Оборудование', str(len(equipments)), 'Уникальные аппараты'),
                 SummaryCard('Фидеры', ('≥ ' if not facility.counts_complete else '') + str(facility.outgoing_count) if facility else '—',
                             'Принадлежность требует уточнения' if facility and not facility.counts_complete else 'Все отходящие присоединения'),
                 SummaryCard('КЗ', str(len(point_rows)) if point_rows else 'Нет расчёта', 'Только актуальная выбранная точка'))
        rows = tuple((self._model.equipment[eid].name,
                      'Выключатель' if is_switch_control(self._model, eid) else
                      self._model.equipment_type(self._model.equipment[eid].type_id, self._model.equipment[eid].type_version).display_name.removeprefix('Legacy: '))
                     for eid in sorted(equipments, key=str) if eid in self._model.equipment)
        tabs = (
            OverviewTab('network', 'Сеть', ('Оборудование', 'Тип'), rows, 'Нет оборудования в выбранном объекте.'),
            OverviewTab('modes', 'Режимы', ('Режим', 'Состояние'), ((state.name if state else 'Не выбран', 'Черновик' if self.vm.mode_draft else 'Применён'),), ''),
            OverviewTab('loads', 'Нагрузки', ('Сведения',), (), 'Параметры нагрузок доступны в карточках оборудования. Рабочий режим здесь не рассчитывается.'),
            OverviewTab('fault', 'КЗ', ('Точка', 'Вид КЗ', 'Макс. ток фазы, кА', 'Статус'), tuple(point_rows), fault_note),
            OverviewTab('settings', 'Уставки', ('Защита', 'Статус'), (), 'Для ввода и проверки уставок откройте подробную схему и раздел анализа.'),
            OverviewTab('selectivity', 'Селективность', ('Проверка', 'Результат'), (), 'Обзор не запускает проверки защит. Готовые проверки доступны в анализе проекта.'),
            OverviewTab('report', 'Отчёт', ('Замечание',), tuple((d.message,) for d in (target.diagnostics if target else self.projection.diagnostics)), 'Замечаний к привязке объектов нет. Это не протокол инженерной проверки.'))
        self.results.set_snapshot(OverviewResultsSnapshot(title, cards, tabs))
