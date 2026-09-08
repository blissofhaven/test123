"""Explicit startup choices; creating a project never overwrites another file."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QFileDialog, QGroupBox, QHBoxLayout, QInputDialog,
    QLabel, QListWidget, QListWidgetItem, QMessageBox, QPushButton, QVBoxLayout,
)

from .project_settings import ProjectSettings


@dataclass(frozen=True)
class TrainingProject:
    title: str
    description: str
    path: Path


def training_projects() -> tuple[TrainingProject, ...]:
    folder = Path(__file__).resolve().parent.parent / "examples"
    candidates = (
        TrainingProject("Компактная учебная схема", "110 / 35 / 10 / 0,4 кВ", folder / "compact_training.json"),
        TrainingProject("Нефтепромысел с ГТЭС", "Большая схема: электростанция и объекты промысла", folder / "oilfield_gtes.json"),
    )
    return tuple(item for item in candidates if item.path.is_file())


def empty_project(name: str):
    """A real blank canonical project: one page, one mode, no invented inputs."""
    from ..adapters.legacy_calculation import adapt_to_calculation
    from ..core.methodology import Methodology
    from ..domain import ProjectStructure
    from ..domain.diagram import DiagramDocument, DiagramDocumentId, DiagramPage, PageId
    from ..domain.electrical import ElectricalModel, OperatingState, OperatingStateId
    from ..editor.state import EditorWorkspaceState, diagram_with_workspace
    from ..io.project import FORMAT_VERSION, ProjectData

    name = name.strip()
    if not name:
        raise ValueError("Укажите название проекта.")
    model = ElectricalModel.with_builtins(name)
    state = OperatingState(OperatingStateId.new(), "Нормальный режим")
    model.add_operating_state(state)
    page = DiagramPage(PageId.new(), "Лист 1")
    diagram = DiagramDocument(DiagramDocumentId.new(), "Однолинейная схема", {page.id: page})
    diagram = diagram_with_workspace(diagram, EditorWorkspaceState(
        active_page_id=page.id.value, active_operating_state_id=state.id.value,
    ))
    return ProjectData(
        network=adapt_to_calculation(model).network, methodology=Methodology.load(),
        metadata={"name": name}, structure=ProjectStructure(),
        source_format_version=FORMAT_VERSION, electrical_model=model, diagram=diagram,
    )


def _save_new_file(project, target: Path) -> Path:
    """Validate through normal persistence, then publish with exclusive creation."""
    from ..io.project import save_project

    target = target.expanduser().resolve()
    if target.exists():
        raise FileExistsError("Этот файл уже существует. Выберите новое имя.")
    # Same parent preserves save_project's relative methodology references.
    with NamedTemporaryFile(prefix=".rza-new-", suffix=".json", dir=target.parent, delete=False) as stream:
        temporary = Path(stream.name)
    try:
        save_project(temporary, project)
        data = temporary.read_bytes()
    finally:
        temporary.unlink(missing_ok=True)
    # Exclusive open also protects a destination created after the first check.
    with target.open("xb") as stream:
        stream.write(data)
    return target


def create_empty_project(name: str, target: str | Path) -> Path:
    return _save_new_file(empty_project(name), Path(target))


def copy_training_project(source: str | Path, target: str | Path) -> Path:
    from ..io.project import load_project
    return _save_new_file(load_project(source), Path(target))


class ProjectChooser(QDialog):
    def __init__(self, settings: ProjectSettings, *, examples=None, notice: str = "", parent=None):
        super().__init__(parent)
        self.settings = settings
        self.project_path: Path | None = None
        self.setWindowTitle("РЗА-Про — выбор проекта")
        self.setMinimumSize(860, 600)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(16)
        title = QLabel("С чего начнём?")
        title.setStyleSheet("font-size: 25px; font-weight: 600;")
        layout.addWidget(title)
        description = QLabel("Откройте свою схему или создайте проект. Расчёт запускается отдельно, когда вы готовы.")
        description.setWordWrap(True)
        layout.addWidget(description)
        self.notice_label = QLabel(notice)
        self.notice_label.setWordWrap(True)
        self.notice_label.setVisible(bool(notice))
        layout.addWidget(self.notice_label)
        body = QHBoxLayout()
        body.setSpacing(22)
        recent_group = QGroupBox("Недавние проекты")
        recent_layout = QVBoxLayout(recent_group)
        self.recent_list = QListWidget()
        self.recent_list.setAccessibleName("Недавние проекты")
        for path in settings.recent_project_paths():
            missing = " · файл недоступен" if not path.is_file() else ""
            item = QListWidgetItem(f"{path.stem}{missing}\n{path}")
            item.setData(Qt.ItemDataRole.UserRole, path)
            item.setToolTip(str(path))
            self.recent_list.addItem(item)
        recent_layout.addWidget(self.recent_list)
        self.empty_recent_label = QLabel("Здесь появятся успешно открытые проекты.")
        self.empty_recent_label.setWordWrap(True)
        self.empty_recent_label.setVisible(self.recent_list.count() == 0)
        recent_layout.addWidget(self.empty_recent_label)
        self.open_recent_button = QPushButton("Открыть выбранный проект")
        self.open_recent_button.setEnabled(False)
        self.open_recent_button.clicked.connect(self._open_recent)
        self.recent_list.itemSelectionChanged.connect(lambda: self.open_recent_button.setEnabled(bool(self.recent_list.selectedItems())))
        self.recent_list.itemDoubleClicked.connect(lambda _item: self._open_recent())
        recent_layout.addWidget(self.open_recent_button)
        body.addWidget(recent_group, 3)
        actions = QVBoxLayout()
        self.open_file_button = QPushButton("Открыть файл…")
        self.new_project_button = QPushButton("Создать пустой проект…")
        for button in (self.open_file_button, self.new_project_button):
            button.setMinimumHeight(42)
            actions.addWidget(button)
        self.open_file_button.clicked.connect(self._open_file)
        self.new_project_button.clicked.connect(self._create_empty)
        training_group = QGroupBox("Учебные проекты")
        training_layout = QVBoxLayout(training_group)
        training_note = QLabel("Создайте рабочую копию примера в своей папке.")
        training_note.setWordWrap(True)
        training_layout.addWidget(training_note)
        self.training_list = QListWidget()
        self.training_list.setAccessibleName("Учебные проекты")
        for example in training_projects() if examples is None else examples:
            item = QListWidgetItem(f"{example.title}\n{example.description}")
            item.setData(Qt.ItemDataRole.UserRole, example)
            item.setToolTip(str(example.path))
            self.training_list.addItem(item)
        training_layout.addWidget(self.training_list)
        self.copy_button = QPushButton("Создать копию и открыть…")
        self.copy_button.setEnabled(False)
        self.training_list.itemSelectionChanged.connect(lambda: self.copy_button.setEnabled(bool(self.training_list.selectedItems())))
        self.copy_button.clicked.connect(self._copy_example)
        training_layout.addWidget(self.copy_button)
        actions.addWidget(training_group, 1)
        body.addLayout(actions, 2)
        layout.addLayout(body, 1)
        self.auto_open_check = QCheckBox("В следующий раз открывать последний проект автоматически")
        self.auto_open_check.setChecked(settings.auto_open_last_project())
        layout.addWidget(self.auto_open_check)
        close_button = QPushButton("Закрыть")
        close_button.clicked.connect(self.reject)
        bottom = QHBoxLayout()
        bottom.addStretch()
        bottom.addWidget(close_button)
        layout.addLayout(bottom)

    def _choose(self, path: Path) -> None:
        if not path.is_file():
            self.notice_label.setText(f"Файл недоступен: {path}. Выберите его новое расположение через «Открыть файл».")
            self.notice_label.show()
            return
        self.project_path = path.expanduser().resolve()
        self.accept()

    def _open_recent(self) -> None:
        item = self.recent_list.currentItem()
        if item is not None:
            self._choose(item.data(Qt.ItemDataRole.UserRole))

    def _open_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Открыть проект", "", "Проекты РЗА (*.json)")
        if path:
            self._choose(Path(path))

    def _new_path(self, filename: str) -> Path | None:
        path, _ = QFileDialog.getSaveFileName(self, "Новый файл проекта", filename, "Проекты РЗА (*.json)")
        if not path:
            return None
        target = Path(path)
        if not target.suffix:
            target = target.with_suffix(".json")
        if target.exists():
            QMessageBox.warning(self, "Файл уже существует", "Выберите новое имя. Существующие проекты не перезаписываются.")
            return None
        return target

    def _create_empty(self) -> None:
        name, accepted = QInputDialog.getText(self, "Новый проект", "Название проекта:")
        if not accepted or not name.strip():
            return
        target = self._new_path("Новый проект.json")
        if target is not None:
            self._create_file(lambda: create_empty_project(name, target))

    def _copy_example(self) -> None:
        item = self.training_list.currentItem()
        if item is None:
            return
        example = item.data(Qt.ItemDataRole.UserRole)
        target = self._new_path(f"{example.title} — копия.json")
        if target is not None:
            self._create_file(lambda: copy_training_project(example.path, target))

    def _create_file(self, action) -> None:
        try:
            path = action()
        except Exception as exc:
            QMessageBox.critical(self, "Не удалось создать проект", str(exc))
            return
        self._choose(path)


def choose_project(settings: ProjectSettings, *, notice: str = "") -> Path | None:
    dialog = ProjectChooser(settings, notice=notice)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    # Selection does not alter last/MRU paths; the launcher records them only
    # after loading the file and showing the real application window.
    try:
        settings.set_auto_open_last_project(dialog.auto_open_check.isChecked())
    except (OSError, RuntimeError):
        pass  # An unavailable settings store must not prevent opening a project.
    return dialog.project_path
