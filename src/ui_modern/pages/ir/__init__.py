"""Joy-Con IR configuration rendered from captured-origin read models."""
from ...widgets.support import ThemedDialog
from ...widgets.changes import apply_change, bind_numeric
from PySide6.QtWidgets import QDialog, QVBoxLayout, QWidget, QDialogButtonBox
from qfluentwidgets import ScrollArea, PushButton, BodyLabel, TitleLabel
from app_core import events
from ...widgets.display import input_display
from ...qt_bridge import BusBridge, call_core
from ...widgets.common import SettingsGroup, ExpandSettingRow
from ...widgets.controls import Choice, Numeric, local_toast
from ...widgets.pickers import OptionPicker, InputPicker
from ...widgets.recording import RecordDialog


class IrSensorDialog(ThemedDialog):
    """Entry point: IrSensorDialog(side, scope, core=core, parent=page).

    Origin context is frozen at construction. Optional profile/category arguments
    let callers provide an explicit origin obtained from their own read models.
    """
    def __init__(self, side, scope=None, *, core, toast=None, parent=None,
                 profile_name=None, category=None):
        super().__init__(parent)
        self.core, self.toast = core, toast or local_toast(self)
        if side not in ("left", "right"):
            raise ValueError("side must be left or right")
        self.side, self.scope = side, scope
        origin = core.settings.ir_sensor(side, scope)
        self.profile_name = profile_name or origin.profile_name or core.profiles.state().active
        self.category = category or origin.category
        self.context = dict(profile_name=self.profile_name, category=self.category)
        self._closed = False
        self._children = []
        self.setWindowTitle(f"Joy-Con IR sensor · {side.title()}")
        self.resize(780, 680)
        box = QVBoxLayout(self); box.setContentsMargins(20, 16, 20, 16)
        box.addWidget(TitleLabel(f"{side.title()} IR sensor", self))
        category_label = {"xbox": "Xbox", "ps4": "PS4", "ps5_winuhid": "PS5 · WinUHid",
            "ps5_usbip": "PS5 · USBIP", "switch1": "Switch", "switch2": "Switch 2"}.get(self.category, self.category)
        self.origin_label = BodyLabel(f"{self.profile_name} · {category_label} · {'Mode shift' if scope else 'Controller mapping'}", self)
        self.origin_label.setToolTip(self.origin_label.text()); box.addWidget(self.origin_label)
        scroll = ScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.enableTransparentBackground()
        content = QWidget(scroll); scroll.setWidget(content); box.addWidget(scroll)
        content.setAutoFillBackground(False)
        scroll.viewport().setAutoFillBackground(False)
        self.body = QVBoxLayout(content); self.body.setContentsMargins(0, 8, 0, 8); self.body.setSpacing(12)
        self.general = SettingsGroup("Function", content); self.body.addWidget(self.general)
        self.function = PushButton("Default (IR Mouse)", self); self.function.setFixedWidth(260)
        self.function.clicked.connect(self.pick_function)
        self.function_row = self.general.row("IR function", self.function)
        self.mode = Choice(self)
        self.mode.sync(("Hold", "Tap"), "Hold")
        self.mode_row = self.general.row("Activation", self.mode)
        self.mode.edited.connect(self.change_mode)
        reset = PushButton("Reset", self)
        reset.clicked.connect(lambda: self.set_function("Default"))
        self.general.row("Reset function", reset)
        self.reset_button = reset
        self.mouse = ExpandSettingRow("IR mouse settings", parent=content)
        self.body.addWidget(self.mouse)
        self.mouse_group = SettingsGroup("Mouse", self.mouse.body); self.mouse.addWidget(self.mouse_group)
        self.gyro = ExpandSettingRow("In-app gyro settings", parent=content)
        self.body.addWidget(self.gyro)
        self.gyro_group = SettingsGroup("Activation settings", self.gyro.body); self.gyro.addWidget(self.gyro_group)
        self.profile = SettingsGroup("Change profile", content); self.body.addWidget(self.profile)
        self.profile_mode = Choice(self)
        self.profile_mode.sync(("Auto", "Manual"), core.settings.state().app.change_profile_mode)
        self.profile_row = self.profile.row("Mode", self.profile_mode)
        self.profile_mode.edited.connect(self.change_profile_mode)
        self.controls = {}; self.rows = {}; self.values = {}; self.gyro_controls = {}; self.gyro_rows = {}; self.gyro_values = {}
        self.body.addStretch()
        buttons = QDialogButtonBox(QDialogButtonBox.Close, self); box.addWidget(buttons)
        buttons.rejected.connect(self.reject)
        self.finished.connect(self.cleanup)
        self.bridge = BusBridge(core.bus, self)
        self.bridge.event.connect(self.apply_event)
        self.destroyed.connect(self.bridge.close)
        self.refresh()

    def same_origin(self):
        origin = self.core.settings.ir_sensor(self.side, self.scope)
        return (origin.profile_name or self.core.profiles.state().active) == self.profile_name and (
            origin.category) == self.category

    def refresh(self):
        if self._closed:
            return
        if not self.same_origin():
            call_core(self.core.settings.ir_sensor_context, self.side, self.scope, **self.context,
                      on_result=lambda view: self.apply_view(view) if not self._closed else None)
            return
        state = self.core.settings.state()
        view = next((v for v in state.ir_sensors if v.side == self.side and v.scope == self.scope), None)
        if view:
            self.apply_view(view)
        self.profile_mode.sync(("Auto", "Manual"), state.app.change_profile_mode)
    def apply_view(self, view):
        self.view = view
        self.function.setText(input_display(view.display_text))
        self.function.setToolTip(self.function.text())
        self.mode_row.setVisible(view.mode is not None)
        if view.mode:
            self.mode.sync(("Hold", "Tap"), view.mode)
        self.mouse.setVisible(view.function == "Default")
        is_gyro = view.is_gyro
        self.gyro.setVisible(is_gyro)
        self.profile.setVisible(view.function == "Change Profile")
        if is_gyro:
            self.apply_gyro(view.gyro_view)
        for c in view.controls:
            self.values[c.key] = c.value
            if c.key not in self.controls:
                group = self.general if c.key == "activate_threshold" else self.mouse_group
                widget = PushButton("None", self) if c.options else Numeric(c, self)
                widget.setAccessibleName(c.label.rstrip(":"))
                row = group.row(c.label.rstrip(":"), widget)
                self.controls[c.key], self.rows[c.key] = widget, row
                if c.options:
                    widget.setFixedWidth(260)
                    widget.clicked.connect(lambda checked=False, key=c.key: self.pick_inputs(key))
                else:
                    bind_numeric(self, widget, lambda v, key=c.key: self.numeric_write(key, v),
                        lambda key=c.key: self.values[key], row)
            widget = self.controls[c.key]
            if isinstance(widget, Numeric):
                widget.sync(c.value)
            else:
                self.selection_text(widget, c.value)

    @staticmethod
    def selection_text(button, value):
        text = input_display("+".join(map(str, value))) if isinstance(value, (list, tuple)) and value else "None"
        button.setText(text); button.setToolTip(text)

    def numeric_write(self, key, value):
        if key == "activate_threshold":
            return self.core.settings.set_ir_activation_threshold(self.side, value, self.scope, **self.context)
        return self.core.settings.set_ir_mouse_setting(self.side, key, value, self.scope, **self.context)

    def show_child(self, dialog, on_accept):
        self._children.append(dialog)
        def done(result):
            if result == QDialog.Accepted:
                on_accept(dialog.value)
            self._children.remove(dialog); dialog.deleteLater()
        dialog.finished.connect(done); dialog.open()

    def pick_function(self):
        self.show_child(OptionPicker(self.view, parent=self), self.choose_function)

    def choose_function(self, value):
        if value == "Custom":
            self.show_child(RecordDialog(self.core, parent=self, mode=self.view.mode or "Hold"), self.set_function)
        else:
            self.set_function(value)

    def set_function(self, value):
        old = self.view.function
        # Leaving gyro resets its auxiliary values; Undo must restore those too.
        old_gyro = dict(self.view.in_app_gyro)
        apply_change(self, self.core.settings.set_ir_function, self.side, value, self.scope, **self.context,
            row=self.function_row, undo=lambda: apply_change(self, self.core.settings.set_ir_function,
                self.side, old, self.scope, **self.context, gyro_values=old_gyro))

    def change_mode(self, value):
        old = self.view.mode
        apply_change(self, self.core.settings.set_ir_mapping_mode, self.side, value, self.scope,
                     **self.context, row=self.mode_row, old_args=(self.side, old, self.scope))

    def pick_inputs(self, key, gyro=False):
        values = self.gyro_values if gyro else self.values
        if gyro:
            spec = self.gyro_specs[key]
        else:
            spec = next(c for c in self.view.controls if c.key == key)
        old = values[key]
        def accepted(value):
            if gyro:
                apply_change(self, self.core.settings.set_ir_gyro_setting, self.side, key, value, self.scope, **self.context,
                    undo=lambda: apply_change(self, self.core.settings.set_ir_gyro_setting, self.side, key, old, self.scope, **self.context))
            else:
                apply_change(self, self.core.settings.set_ir_mouse_setting, self.side, key, value, self.scope,
                    **self.context, undo=lambda: apply_change(self, self.core.settings.set_ir_mouse_setting,
                        self.side, key, old, self.scope, **self.context))
        self.show_child(InputPicker(spec.options, old, self), accepted)


    def apply_gyro(self, view):
        self.gyro_specs = {c.key: c for c in view.controls}
        for c in view.controls:
            self.gyro_values[c.key] = c.value
            if c.key not in self.gyro_controls:
                widget = PushButton("None", self) if c.options or c.key == "simul" else Numeric(c, self)
                row = self.gyro_group.row(c.label.rstrip(":"), widget)
                widget.setAccessibleName(c.label.rstrip(":"))
                self.gyro_controls[c.key], self.gyro_rows[c.key] = widget, row
                if isinstance(widget, Numeric):
                    bind_numeric(self, widget, lambda v, key=c.key: self.core.settings.set_ir_gyro_setting(self.side, key, v, self.scope, **self.context),
                        lambda key=c.key: self.gyro_values[key], row)
                elif c.key == "simul":
                    widget.setFixedWidth(260)
                    widget.clicked.connect(lambda: self.pick_simultaneous(view.simultaneous_options))
                else:
                    widget.setFixedWidth(260)
                    widget.clicked.connect(lambda checked=False, key=c.key: self.pick_inputs(key, True))
            widget = self.gyro_controls[c.key]
            if isinstance(widget, Numeric):
                widget.sync(c.value)
            elif c.key == "simul":
                widget.setText(input_display(c.value))
                widget.setToolTip(widget.text())
            else:
                self.selection_text(widget, c.value)
            self.gyro_rows[c.key].setVisible(c.visible)


    def pick_simultaneous(self, options):
        def accepted(value):
            if value == "Custom":
                self.show_child(RecordDialog(self.core, parent=self), accepted)
                return
            old = self.gyro_values["simul"]
            apply_change(self, self.core.settings.set_ir_gyro_setting, self.side, "simul", value, self.scope, **self.context,
                undo=lambda: apply_change(self, self.core.settings.set_ir_gyro_setting, self.side, "simul", old, self.scope, **self.context))
        self.show_child(OptionPicker(options, parent=self), accepted)

    def change_profile_mode(self, value):
        old = self.core.settings.state().app.change_profile_mode
        apply_change(self, self.core.settings.set_change_profile_mode, value, row=self.profile_row,
            undo=lambda: apply_change(self, self.core.settings.set_change_profile_mode, old))

    def apply_event(self, topic, value):
        if topic == events.SETTINGS_CHANGED:
            keys = getattr(value, "keys", None)
            if keys is None or set(keys) & {"ir_sensor", "in_app_gyro", "mapping", "change_profile_mode"}:
                self.refresh()
    def cleanup(self, *_):
        for child in tuple(self._children):
            child.reject()
        for widget in (*self.controls.values(), *self.gyro_controls.values()):
            if isinstance(widget, Numeric):
                widget.finish()
        self._closed = True
        self.bridge.close()
