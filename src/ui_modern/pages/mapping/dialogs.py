from ...i18n import tr
from ...widgets.changes import apply_change, setting_writer
from ...widgets.display import setting_title
import sys
from PySide6.QtCore import QSignalBlocker
from qfluentwidgets import SwitchButton, DoubleSpinBox
from ...widgets.common import SettingsGroup, ExpandSettingRow, Segments
from ...widgets.support import SettingsDialog, BatchSlider
from .rows import MappingRow
from ...widgets.pickers import SwitchInputPanel


class ChangeProfileDialog(SettingsDialog):
    def __init__(self, core, toast, parent=None):
        super().__init__(core, "Change profile", toast, parent)
        group = SettingsGroup("Profile switching", self)
        self.box.addWidget(group)
        self.mode = Segments(self)
        group.row("Selection mode", self.mode)
        self.mode.currentItemChanged.connect(setting_writer(self, core.settings.set_change_profile_mode, read=lambda: self.previous))
        self.start_settings()
        self.footer()

    def apply_settings(self, state):
        self.previous = state.app.change_profile_mode
        self.mode.sync(("Auto", "Manual"), self.previous)


class DeadzoneDialog(SettingsDialog):
    def __init__(self, core, toast, parent=None):
        super().__init__(core, "Joystick deadzone settings", toast, parent)
        self.sliders, self.links = {}, {}
        for row in core.settings.state().deadzones.rows:
            group = SettingsGroup({"pro_controller": "Pro", "joycon": "Joy-Con", "nso_gamecube_controller": "GameCube"}.get(row.family, row.family), self)
            self.box.addWidget(group)
            link = SwitchButton(self)
            self.links[row.family] = link
            link.setChecked(row.linked)
            group.row("Link left and right", link)
            link.checkedChanged.connect(lambda value, family=row.family: self.set_link(family, value))
            for side in ("left", "right"):
                slider = BatchSlider(core, 0, 100, 1, getattr(row, side),
                    lambda value, family=row.family, side=side: apply_change(self, core.settings.set_deadzone, family, side, int(value), save=not self.isVisible()), self,
                    toast=toast, reader=lambda family=row.family, side=side: getattr(self.read_row(family), side))
                slider.slider.setAccessibleName(f"{row.family} {side} deadzone percent")
                self.sliders[row.family, side] = slider
                group.row(side.title() + " %", slider)
        self.start_settings()
        self.footer()
        self.finished.connect(self.flush)

    def read_row(self, family):
        return next(row for row in self._settings_state.deadzones.rows if row.family == family)

    def set_link(self, family, value):
        old = self.read_row(family).linked
        apply_change(self, self.core.settings.set_deadzone_linked, family, value,
            undo=lambda: apply_change(self, self.core.settings.set_deadzone_linked, family, old, save=True))


    def apply_settings(self, state):
        for row in state.deadzones.rows:
            for side in ("left", "right"):
                self.sliders[row.family, side].set_value(getattr(row, side))
            link = self.links[row.family]
            link.blockSignals(True)
            link.setChecked(row.linked)
            link.blockSignals(False)

    def flush(self, *_):
        for slider in self.sliders.values():
            slider.finish()
        apply_change(self, self.core.settings.commit_deadzones)


class JoystickDialog(SettingsDialog):
    def __init__(self, core, key, scope, mode, toast, parent=None):
        super().__init__(core, tr("Left joystick" if key.startswith("l_") else "Right joystick") + " · " + setting_title(mode), toast, parent)
        self.key, self.scope, self.mode = key, scope, mode
        group = SettingsGroup(mode, self)
        self.box.addWidget(group)
        view = core.settings.joystick(key, scope)
        self.rows = {}
        self.sensitivity = None
        if mode == "Mouse":
            self.sensitivity = BatchSlider(core, 0, 10, .2, view.mouse_sensitivity,
                setting_writer(self, core.settings.set_joystick_setting, key, "mouse_sensitivity", suffix=(scope,)), self, toast=toast,
                reader=lambda: self.view.mouse_sensitivity)
            group.row("Sensitivity", self.sensitivity)
        elif mode == "Scroll Wheel":
            self.scroll = Segments(self)
            self.activation = Segments(self)
            group.row("Direction", self.scroll)
            group.row("Activation", self.activation)
            self.scroll.currentItemChanged.connect(setting_writer(self, core.settings.set_joystick_setting, key, "scroll_mode", suffix=(scope,), read=lambda: self.view.scroll_mode))
            self.activation.currentItemChanged.connect(setting_writer(self, core.settings.set_joystick_setting, key, "scroll_activation", suffix=(scope,), read=lambda: self.view.scroll_activation))
        elif mode == "Custom":
            for direction in ("up", "down", "left", "right", "click"):
                model = next(row for row in view.mapping_rows if row.key == key + "_" + direction)
                widget = MappingRow(self, model, scope, self)
                self.rows[direction] = widget
                group.addRow(widget)
        self.start_settings()
        self.footer()
        self.finished.connect(lambda _: self.sensitivity.finish() if self.sensitivity else None)


    def apply_settings(self, state):
        view = next(v for v in state.joysticks if v.key == self.key and v.scope == self.scope)
        self.view = view
        if self.mode == "Mouse":
            self.sensitivity.set_value(view.mouse_sensitivity)
        elif self.mode == "Scroll Wheel":
            self.scroll.sync(("Up/Down", "Up/Down/Left/Right"), view.scroll_mode)
            self.activation.sync(("Hold", "Tap"), view.scroll_activation)
        elif self.mode == "Custom":
            for direction, row in self.rows.items():
                row.update_snapshot(next(r for r in view.mapping_rows if r.key == self.key + "_" + direction))


    def configure_mapping(self, key, scope, value):
        selected = next(row.snapshot.selected_option for row in self.rows.values() if row.snapshot.key == key)
        dialog = ActivationDialog(self.core, key, self.toast, self) if selected == "In-app Gyro" else ChangeProfileDialog(self.core, self.toast, self)
        self.child_dialog = dialog
        dialog.show()


class ActivationDialog(SettingsDialog):
    def __init__(self, core, key, toast, parent=None):
        super().__init__(core, "In-app gyro activation", toast, parent)
        self.key = key
        self.fields = {}
        self.field_rows = {}
        self.selections = {}
        self.numeric_pending = {}
        view = core.settings.in_app_gyro(key)
        controls = {c.key: c for c in view.controls}
        group = SettingsGroup("Activation", self)
        self.box.addWidget(group)
        self.simultaneous = MappingRow(self, view.simultaneous_mapping, None, self,
                                      writer=setting_writer(self, core.settings.set_in_app_gyro_setting, key, "simul", suffix=(None,), read=lambda: self.values()["simul"]))
        group.addRow(self.simultaneous)
        for name, title in (("deadzone_mode", "Trigger deadzone"), ("dampening_mode", "Trigger dampening")):
            disclosure = ExpandSettingRow(title, "Choose controller inputs", parent=self)
            group.addRow(disclosure)
            panel = SwitchInputPanel(controls[name].options, self)
            self.selections[name] = panel.checks
            for checkbox in panel.checks.values():
                checkbox.toggled.connect(lambda _, name=name: self.selection_changed(name))
            disclosure.add_body_widget(panel)
            self.field_rows[name] = disclosure
            numeric_group = SettingsGroup(title + " tuning", self)
            disclosure.add_body_widget(numeric_group)
            prefix = name.removesuffix("mode")
            for control in view.controls:
                if not control.key.startswith(prefix) or control.key == name:
                    continue
                if control.key == "dampening_amount":
                    field = BatchSlider(core, 0, 100, 1, control.value,
                        setting_writer(self, core.settings.set_in_app_gyro_setting, key, "dampening_amount", suffix=(None,)), self,
                        toast=toast, reader=lambda: self.values()["dampening_amount"])
                else:
                    field = DoubleSpinBox(self)
                    field.setDecimals(6 if control.key == "deadzone_amount" else 0)
                    field.setRange(0, sys.float_info.max)
                    field.setKeyboardTracking(False)
                    field.setFixedWidth(155)
                    if control.key.endswith("_ms"):
                        field.setSuffix(" ms")
                    field.setAccessibleName(setting_title(control.label.rstrip(":")))
                    field.valueChanged.connect(lambda value, name=control.key: self.write_number(name, value))
                self.fields[control.key] = field
                self.field_rows[control.key] = numeric_group.row(control.label.rstrip(":"), field)
            self.field_rows[prefix + "group"] = numeric_group
        self.start_settings()
        self.footer()
        self.finished.connect(self.flush)


    def values(self):
        return {c.key: c.value for c in self.view.controls}

    def apply_settings(self, state):
        view = next(v for v in state.in_app_gyro if v.key == self.key)
        self.view = view
        values = {c.key: c.value for c in view.controls}
        self.simultaneous.update_snapshot(view.simultaneous_mapping)
        for name, boxes in self.selections.items():
            selected = values[name]
            for token, checkbox in boxes.items():
                checkbox.blockSignals(True)
                checkbox.setChecked(token in selected)
                checkbox.blockSignals(False)
            self.field_rows[name].card.setContent(", ".join(token for token in boxes if token in selected) or tr("None"))
            self.field_rows[name].setToolTip(", ".join(selected) if isinstance(selected, (list, tuple)) else str(selected))
            self.field_rows[name.removesuffix("mode") + "group"].setVisible(bool([token for token in boxes if token in selected]))
        for name, field in self.fields.items():
            if isinstance(field, BatchSlider):
                field.set_value(values[name])
            elif name not in self.numeric_pending and field.value() != values[name]:
                with QSignalBlocker(field):
                    field.setValue(values[name])

    def selection_changed(self, setting):
        apply_change(self, self.core.settings.set_in_app_gyro_setting, self.key, setting,
                     [token for token, box in self.selections[setting].items() if box.isChecked()],
                     old_args=(self.key, setting, self.values()[setting]))

    def write_number(self, name, value):
        if name != "deadzone_amount":
            value = int(value)
        revision = object()
        self.numeric_pending[name] = revision
        def finished():
            if self.numeric_pending.get(name) is revision:
                self.numeric_pending.pop(name)
                self.render_settings(self.core.settings.state())
        apply_change(self, self.core.settings.set_in_app_gyro_setting, self.key, name, value, None,
                     old_args=(self.key, name, self.values()[name], None), on_finished=finished)

    def flush(self, *_):
        self.fields["dampening_amount"].finish()

    def configure_mapping(self, *_):
        pass
