"""One GUI draft over canonical operating states; never a second Network."""
from dataclasses import replace

from ..domain.electrical import EquipmentAvailability, SwitchPosition


class ModeStateMixin:
    mode_draft = None
    _mode_preview = None

    @property
    def mode_controller(self):
        if not hasattr(self, '_mode_controller'):
            from ..editor import ProjectEditorController
            self._mode_controller = ProjectEditorController(self.project)
        return self._mode_controller

    def begin_background_calculation(self):
        from ..calculation.input import capture_project_input
        if self.mode_draft is not None:
            raise ValueError('Примените или сбросьте черновик режима перед расчётом.')
        self._discard_presentation_snapshot()
        self._calculation_generation = getattr(self, '_calculation_generation', 0) + 1
        self.result = None
        self.calculation_error = ''
        self.calculation_busy = True
        try:
            captured = capture_project_input(self.project)
        except Exception:
            self.calculation_busy = False
            raise
        return self._calculation_generation, captured

    def publish_background_calculation(self, generation, captured, result=None, error=''):
        if generation != self._calculation_generation:
            return False
        self._discard_presentation_snapshot()
        self.calculation_busy = False
        if error:
            self.calculation_error = error
            return False
        if self.mode_draft is not None or not captured.is_current_for(self.project):
            self.calculation_error = 'Исходные данные изменились во время расчёта. Результат не опубликован.'
            return False
        self.result = result
        self.calculation_error = ''
        return result is not None

    def save_calculation_snapshot(self, path, *, overwrite=False):
        from ..calculation.input import save_calculation_input
        result = self.require_current_result()
        captured = getattr(result, 'calculation_input', None)
        if captured is None:
            raise ValueError('У выполненного расчёта отсутствует воспроизводимый снимок входных данных.')
        save_calculation_input(captured, path, overwrite=overwrite)
        return captured

    def operating_mode_choices(self):
        """Read names/IDs without compiling every mode for a toolbar refresh."""
        return self.mode_controller.operating_mode_choices()

    def selected_mode_choice(self):
        return next((row for row in self.operating_mode_choices() if row.calculation_mode_id == self.mode_id), None)

    def operating_modes(self):
        controller = self.mode_controller
        model = controller.model
        key = (id(model), model.revision, tuple(model.operating_states.values()))
        if getattr(self, '_mode_list_key', None) != key:
            self._mode_list_key = key
            self._mode_list = controller.operating_mode_snapshots()
        return self._mode_list

    def selected_operating_mode(self):
        return next((row for row in self.operating_modes() if row.calculation_mode_id == self.mode_id), None)

    def choose_operating_mode(self, state_id):
        if self.mode_draft is not None:
            raise ValueError('Сначала примените или сбросьте черновик режима.')
        selected = next((row for row in self.operating_mode_choices() if row.state_id == state_id or str(row.state_id) == str(state_id)), None)
        if selected is None:
            raise ValueError('Режим не найден в текущем проекте.')
        self._discard_presentation_snapshot()
        self.mode_controller.select_operating_mode(selected.state_id)
        self.mode_id = selected.calculation_mode_id
        return selected

    def begin_mode_draft(self, *, clone=False, new=False):
        if self.mode_draft is not None:
            return self.mode_draft
        selected = self.selected_mode_choice()
        if clone:
            draft = self.mode_controller.operating_mode_draft(clone_from=selected.state_id if selected else None)
        else:
            draft = self.mode_controller.operating_mode_draft(state_id=None if new or selected is None else selected.state_id)
        return self.update_mode_draft(draft)

    def update_mode_draft(self, draft):
        if getattr(self, 'calculation_busy', False):
            raise ValueError('Расчёт выполняется. Дождитесь завершения или отмените его.')
        self._discard_presentation_snapshot()
        self.mode_draft = draft
        self._mode_preview = None
        return draft

    def preview_mode_draft(self):
        if self.mode_draft is None:
            return None
        if self._mode_preview is None:
            self._mode_preview = self.mode_controller.preview_operating_mode(self.mode_draft)
        return self._mode_preview

    def apply_mode_draft(self):
        if self.mode_draft is None:
            return False
        preview = self.preview_mode_draft()
        if not preview.valid:
            raise ValueError('\n'.join(str(getattr(issue, 'message', issue)) for issue in preview.diagnostics))
        selected = self.mode_controller.apply_operating_mode_preview(preview)
        self.reset_mode_draft()
        self.choose_operating_mode(selected)
        return True

    def reset_mode_draft(self):
        self._discard_presentation_snapshot()
        self.mode_draft = None
        self._mode_preview = None
        self.__dict__.pop('_rendered_mode_preview', None)

    def stage_switch(self, target, position):
        draft = self.begin_mode_draft()
        field = 'extra_positions' if getattr(target, 'legacy_key', None) else 'positions'
        key = getattr(target, 'legacy_key', None) or target.equipment_id
        values = dict(getattr(draft, field))
        values[key] = (SwitchPosition(position) is SwitchPosition.CLOSED) if field == 'extra_positions' else SwitchPosition(position)
        return self.update_mode_draft(replace(draft, **{field: values}))

    def stage_equipment_switch(self, equipment_id, position):
        target = next((row for row in self.mode_controller.operating_mode_targets() if row.equipment_id == equipment_id
                       and row.section == 'positions' and not getattr(row, 'legacy_key', None)), None)
        if target is None:
            raise ValueError('Этот объект не является самостоятельным коммутационным аппаратом.')
        return self.stage_switch(target, position)

    def stage_calculation_switch(self, switch_id):
        target = self.mode_controller.resolve_operating_mode_switch(switch_id)
        old = self.switch_closed(switch_id)
        if self.mode_draft is not None:
            if target.legacy_key and target.legacy_key in self.mode_draft.extra_positions:
                old = self.mode_draft.extra_positions[target.legacy_key]
            elif target.equipment_id in self.mode_draft.positions:
                old = self.mode_draft.positions[target.equipment_id] is SwitchPosition.CLOSED
        self.stage_switch(target, SwitchPosition.OPEN if old else SwitchPosition.CLOSED)
        return not old

    def mode_preview_model(self):
        preview = self.preview_mode_draft()
        if preview is None or not preview.can_display:
            return None
        cached = getattr(self, '_rendered_mode_preview', None)
        if cached is None or cached[0] is not preview:
            cached = (preview, self.mode_controller.operating_mode_preview_model(preview))
            self._rendered_mode_preview = cached
        return cached[1]
