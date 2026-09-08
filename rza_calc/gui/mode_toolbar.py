"""A single mode selector visible on both editor and analysis pages."""
from PySide6.QtCore import Signal
from PySide6.QtWidgets import QWidget, QHBoxLayout, QLabel, QComboBox, QPushButton, QProgressBar


class ModeToolbar(QWidget):
    modeSelected = Signal(object)
    manageRequested = Signal()
    applyRequested = Signal()
    resetRequested = Signal()
    calculateRequested = Signal()
    cancelRequested = Signal()
    snapshotRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName('operatingModeToolbar')
        layout = QHBoxLayout(self)
        layout.addWidget(QLabel('Режим сети:'))
        self.selector = QComboBox()
        self.selector.setMinimumWidth(240)
        self.selector.currentIndexChanged.connect(self._selected)
        layout.addWidget(self.selector)
        self.manage = QPushButton('Режимы…')
        self.manage.clicked.connect(self.manageRequested.emit)
        layout.addWidget(self.manage)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status, 1)
        self.apply = QPushButton('Применить режим')
        self.apply.clicked.connect(self.applyRequested.emit)
        self.reset = QPushButton('Сбросить')
        self.reset.clicked.connect(self.resetRequested.emit)
        self.calculate = QPushButton('Рассчитать')
        self.calculate.clicked.connect(self.calculateRequested.emit)
        self.cancel = QPushButton('Отменить расчёт')
        self.cancel.clicked.connect(self.cancelRequested.emit)
        self.snapshot = QPushButton('Сохранить расчёт…')
        self.snapshot.setToolTip('Сохранить использованные исходные данные и методику. Файл воспроизводится командой CLI replay-input.')
        self.snapshot.clicked.connect(self.snapshotRequested.emit)
        self.progress = QProgressBar()
        self.progress.setMaximumWidth(150)
        for widget in (self.apply, self.reset, self.calculate, self.cancel, self.progress, self.snapshot):
            layout.addWidget(widget)

    def _selected(self, index):
        value = self.selector.itemData(index)
        if value is not None:
            self.modeSelected.emit(value)

    def refresh(self, vm):
        self.selector.blockSignals(True)
        self.selector.clear()
        for row in vm.operating_mode_choices():
            self.selector.addItem(row.name, row.state_id)
            if row.calculation_mode_id == vm.mode_id:
                self.selector.setCurrentIndex(self.selector.count() - 1)
        self.selector.blockSignals(False)
        draft = vm.mode_draft is not None
        busy = getattr(vm, 'calculation_busy', False)
        self.selector.setEnabled(not draft)
        self.manage.setEnabled(not busy)
        self.apply.setVisible(draft)
        self.reset.setVisible(draft)
        self.apply.setEnabled(not busy)
        self.reset.setEnabled(not busy)
        self.calculate.setEnabled(not busy and not draft)
        self.snapshot.setEnabled(not busy and not draft and vm.current_result is not None)
        self.cancel.setVisible(busy)
        self.progress.setVisible(busy)
        if not busy:
            text = 'Черновик: ' + vm.mode_draft.name if draft else 'Сохранённый режим'
            preview = getattr(vm, '_mode_preview', None)
            if draft and preview is not None and preview.errors:
                text += '\n' + preview.errors[0].message
            self.status.setText(text)
            self.status.setToolTip('\n'.join(issue.message for issue in preview.diagnostics) if draft and preview else '')

    def set_progress(self, done, total, text):
        self.progress.setRange(0, max(0, total))
        self.progress.setValue(done)
        self.status.setText(text)
