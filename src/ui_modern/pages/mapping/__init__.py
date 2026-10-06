"""Mapping page and its public dialog entry points."""
from ...widgets.changes import apply_change
from ...widgets.support import FluentSurface
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QStackedWidget
from PySide6.QtGui import QAction
from qfluentwidgets import ScrollArea, TitleLabel, PushButton, CheckBox, MessageBox, RoundMenu
from app_core import events
from ...widgets.display import input_display
from ...widgets.common import SettingsGroup, Segments
from ...qt_bridge import BusBridge
from .rows import MappingRow, OptionPicker
from ...widgets.recording import RecordDialog
from .dialogs import JoystickDialog, DeadzoneDialog, ChangeProfileDialog, ActivationDialog

SHIFT_SCOPE = "in_app_gyro_mode_mappings"
GROUPS = (("shoulders", "Shoulders & triggers"), ("face", "Face buttons"),
          ("dpad", "D-pad"), ("system", "System buttons"), ("sticks", "Sticks"),
          ("back", "Back buttons (Pro 2)"), ("rail", "Joy-Con rail buttons"),
          ("ir", "Joy-Con IR sensor"), ("gamecube", "GameCube"))


class MappingPage(ScrollArea):
    def __init__(self, core, toast, parent=None):
        super().__init__(parent)
        self.core, self.toast = core, toast
        self.setObjectName("mapping")
        self.setWidgetResizable(True)
        self.enableTransparentBackground()
        content = FluentSurface(self); self.setWidget(content)
        content.setObjectName("mappingContent")
        self.content = content
        content.setAutoFillBackground(False)
        box = QVBoxLayout(content); box.setContentsMargins(24, 20, 24, 24); box.setSpacing(12)
        box.addWidget(TitleLabel("Mapping", self))
        header = QHBoxLayout()
        self.scope_control = Segments(self)
        self.scope_control.sync(("base", "shift"), "base", {"base": "Controller mapping", "shift": "Mode shift"})
        self.changed_only = CheckBox("Show changed only", self)
        header.addWidget(self.scope_control); header.addStretch(); header.addWidget(self.changed_only)
        box.addLayout(header)
        self.scope = None; self.rows = {}; self.groups = {}; self.gc_modes = {}; self.ir_buttons = {}
        self.ir_rows = {}
        self.ir_defaults = {}
        self.stack = QStackedWidget(self); box.addWidget(self.stack)
        self.scope_control.currentItemChanged.connect(self.change_scope)
        self.changed_only.toggled.connect(self.filter_rows)
        self.shift_actions = QWidget(self); actions = QHBoxLayout(self.shift_actions); actions.setContentsMargins(0, 0, 0, 0)
        self.copy = PushButton("Copy from controller mapping", self)
        self.reset_shift = PushButton("Reset mode shift mapping", self)
        self.copy.hide()
        self.reset_shift.hide()
        self.overflow = PushButton("⋯", self.shift_actions); self.overflow.setFixedWidth(36)
        self.overflow.setAccessibleName("Mode shift actions")
        self.overflow.setToolTip("Mode shift actions")
        actions.addWidget(self.overflow); header.addWidget(self.shift_actions)
        policy = self.shift_actions.sizePolicy()
        policy.setRetainSizeWhenHidden(True)
        self.shift_actions.setSizePolicy(policy)
        self.overflow.clicked.connect(self.show_shift_menu)
        self.copy.clicked.connect(lambda: self.confirm_shift("Copy controller mapping?", "This replaces the mode shift mappings with controller mappings.", core.settings.copy_controller_mapping_to_mode_shift))
        self.reset_shift.clicked.connect(lambda: self.confirm_shift("Reset mode shift mapping?", "This clears the mode shift mappings and simultaneous inputs.", core.settings.reset_mode_shift_mapping))
        for scope in (None, SHIFT_SCOPE):
            panel = QWidget(self); layout = QVBoxLayout(panel); layout.setContentsMargins(0, 0, 0, 0); layout.setSpacing(12)
            groups = {}
            for key, title in GROUPS:
                group = SettingsGroup(title, panel); groups[key] = group; layout.addWidget(group)
            self.groups[scope] = groups
            view = core.settings.mapping(scope)
            for snapshot in view.rows:
                row = MappingRow(self, snapshot, scope, groups[snapshot.group])
                groups[snapshot.group].addRow(row)
                self.rows[scope, snapshot.key] = row
            deadzones = PushButton("Deadzones…", self)
            groups["sticks"].row("Joystick deadzones", deadzones)
            deadzones.clicked.connect(lambda: self.show_dialog(DeadzoneDialog(core, toast, self.window())))
            self.gc_modes[scope] = Segments(self)
            groups["gamecube"].row("Trigger mode", self.gc_modes[scope])
            self.gc_modes[scope].currentItemChanged.connect(lambda mode, scope=scope: self.set_gc(mode, scope))
            calibrate = PushButton("Calibrate…", self)
            groups["gamecube"].row("Trigger calibration", calibrate)
            calibrate.clicked.connect(self.calibrate_gc)
            for side in ("left", "right"):
                button = PushButton("Configure…", self); button.setFixedWidth(205)
                self.ir_rows[scope, side] = groups["ir"].row(side.title() + " IR sensor", button)
                button.clicked.connect(lambda _, side=side, scope=scope: self.open_ir(side, scope))
                self.ir_buttons[scope, side] = button
            layout.addStretch(); self.stack.addWidget(panel)
        self.shift_actions.hide()
        self.bridge = BusBridge(core.bus, self)
        self.bridge.event.connect(self.apply_event)
        self.destroyed.connect(lambda: self.bridge.close())
        self.sync_reads()

    def show_dialog(self, dialog):
        self.dialog = dialog; dialog.show()
        return dialog

    def change_scope(self, value):
        self.scope = SHIFT_SCOPE if value == "shift" else None
        self.stack.setCurrentIndex(1 if self.scope else 0); self.shift_actions.setVisible(self.scope is not None)

    def confirm_shift(self, title, text, fn):
        dialog = MessageBox(title, text, self.window())
        dialog.yesButton.setText("Continue")
        dialog.cancelButton.setText("Cancel")
        dialog.accepted.connect(lambda: apply_change(self, fn))
        self.show_dialog(dialog)

    def show_shift_menu(self):
        self.shift_menu = RoundMenu(parent=self)
        for title, button in (("Copy from controller mapping", self.copy), ("Reset mode shift mapping", self.reset_shift)):
            action = QAction(title, self.shift_menu)
            action.triggered.connect(button.click); self.shift_menu.addAction(action)
        self.shift_menu.exec(self.overflow.mapToGlobal(self.overflow.rect().bottomLeft()))

    def set_gc(self, mode, scope):
        old = next(v.mode for v in self.core.settings.state().gc_triggers if v.scope == scope)
        fn = self.core.settings.set_gc_trigger_mode
        apply_change(self, fn, mode, scope, undo=lambda: apply_change(self, fn, old, scope))

    def configure_mapping(self, key, scope, value):
        if key in ("l_joystick", "r_joystick"):
            dialog = JoystickDialog(self.core, key, scope, value, self.toast, self.window())
        elif value == "Change Profile":
            dialog = ChangeProfileDialog(self.core, self.toast, self.window())
        else:
            dialog = ActivationDialog(self.core, key, self.toast, self.window())
        self.show_dialog(dialog)

    def open_ir(self, side, scope):
        from ..ir import IrSensorDialog
        return self.show_dialog(IrSensorDialog(side, scope, core=self.core, toast=self.toast, parent=self.window()))

    def calibrate_gc(self):
        from ...wizards.calibration import open_gc_trigger_wizard
        self.dialog = open_gc_trigger_wizard(self.core, parent=self.window())
        return self.dialog

    def sync_reads(self):
        self.apply_settings(self.core.settings.state())

    def apply_settings(self, state):
        self._settings_state = state
        for view in state.mappings:
            for snapshot in view.rows:
                if (view.scope, snapshot.key) in self.rows:
                    self.rows[view.scope, snapshot.key].update_snapshot(snapshot)
        for view in state.gc_triggers:
            self.gc_modes[view.scope].sync(tuple(o.value for o in view.options), view.mode, {o.value: o.label for o in view.options})
        for view in state.ir_sensors:
            button = self.ir_buttons[view.scope, view.side]
            button.setText(input_display(view.display_text))
            button.setToolTip(button.text())
            self.ir_defaults[view.scope, view.side] = view.function == "Default"
        self.filter_rows()

    def filter_rows(self, *_):
        for (scope, key), row in self.rows.items():
            row.setVisible(row.snapshot.visible and (not self.changed_only.isChecked() or not row.snapshot.is_default))
        for key, row in self.ir_rows.items():
            row.setVisible(not self.changed_only.isChecked() or not self.ir_defaults.get(key, True))
        for scope, groups in self.groups.items():
            for name, group in groups.items():
                if name == "ir":
                    group.setVisible(not self.changed_only.isChecked() or any(not self.ir_defaults.get((scope, side), True) for side in ("left", "right")))
                elif name not in ("sticks", "gamecube"):
                    group.setVisible(any(row.snapshot.visible and (not self.changed_only.isChecked() or not row.snapshot.is_default)
                        for (s, _), row in self.rows.items() if s == scope and row.snapshot.group == name))

    def apply_event(self, topic, payload):
        if topic == events.SETTINGS_CHANGED:
            keys = getattr(payload, "keys", None)
            if keys is None or keys.intersection({"mapping", "joystick", "gc_trigger", "ir_sensor", "gyro", "in_app_gyro"}):
                self.sync_reads()


def create_page(core, toast, parent=None, **kwargs):
    """Lazy page factory used by MainWindow."""
    return MappingPage(core, toast, parent, **kwargs)


__all__ = ["MappingPage", "MappingRow", "OptionPicker", "RecordDialog", "JoystickDialog",
           "DeadzoneDialog", "ActivationDialog", "ChangeProfileDialog", "create_page"]
