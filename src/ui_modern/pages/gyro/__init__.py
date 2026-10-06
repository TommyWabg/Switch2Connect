"""Gyro settings page. No configuration or hardware imports."""
from functools import partial
from ...widgets.changes import apply_change, bind_numeric
from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QVBoxLayout
from qfluentwidgets import ScrollArea, TitleLabel, PushButton
from app_core import events
from ...qt_bridge import BusBridge
from ...widgets.common import SettingsGroup, SettingRow
from ...widgets.support import FluentSurface
from ...widgets.controls import Choice, BooleanChoice, Numeric, local_toast

TUTORIAL_URL = "https://youtu.be/J_cZnPcW-Yw?si=ID2vdzURiOph8x77&t=6"
PASS = ("gyro_passthrough_9axis_enabled", "steam_roll_compensation",
        "virtual_gyro_soft_deadzone", "gyro_passthrough_mode", "cemuhook_sensitivity")
APP = ("gyro_control_mode", "gyro_sensitivity", "gyro_mode", "mode_shift_enabled",
       "in_app_gyro_soft_deadzone", "stick_mouse_sensitivity")
DUAL = ("djg_enabled", "djg_mode", "djg_dominant_side", "djg_activation")
LABELS = {"gyro_passthrough_mode": "Destination", "gyro_mode": "Axes",
          "gyro_control_mode": "Control mode", "djg_enabled": "Enable",
          "steam_roll_compensation": "Horizon lock", "djg_mode": "Mode",
          "djg_dominant_side": "Dominant side", "djg_activation": "Activation",
          "gyro_passthrough_9axis_enabled": "9-axis assist", "mode_shift_enabled": "Mode shift",
          "stick_mouse_sensitivity": "Stick assist", "virtual_gyro_soft_deadzone": "Deadzone",
          "in_app_gyro_soft_deadzone": "Deadzone"}
CAPTIONS = {"gyro_passthrough_9axis_enabled": "Use magnetometer data to reduce gyro drift.",
    "steam_roll_compensation": "Keep the horizon level while aiming.",
    "virtual_gyro_soft_deadzone": "Ignore small gyro movements near rest.",
    "in_app_gyro_soft_deadzone": "Ignore small gyro movements near rest.",
    "gyro_passthrough_mode": "Choose where controller gyro data is sent.",
    "stick_mouse_sensitivity": "Use the stick to help move the gyro pointer.",
    "djg_mode": "Choose how the two Joy-Con gyros work together."}


class GyroPage(ScrollArea):
    def __init__(self, core, toast=None, parent=None):
        super().__init__(parent)
        self.core, self.toast = core, toast or local_toast(self)
        self.setObjectName("gyro")
        self.setWidgetResizable(True)
        self.enableTransparentBackground()
        self.content = FluentSurface(self); self.setWidget(self.content)
        self.content.setAutoFillBackground(False)
        self.viewport().setAutoFillBackground(False)
        box = QVBoxLayout(self.content); box.setContentsMargins(24, 20, 24, 24); box.setSpacing(12)
        box.addWidget(TitleLabel("Gyro", self.content))
        self.controls = {}; self.rows = {}; self.values = {}; self.keys = {}
        self.groups = {}
        for title in ("Pass-through", "In-app gyro", "Dual Joy-Con gyro"):
            group = SettingsGroup(title, self.content); self.groups[title] = group; box.addWidget(group)
        calibration = SettingsGroup("Calibration", self.content); box.addWidget(calibration)
        self.calibrate = PushButton("Calibrate", calibration)
        self.calibration_row = calibration.addRow(SettingRow("Calibrate gyro",
            "Place controllers on a flat surface and keep them still", self.calibrate, calibration))
        self.calibrate.clicked.connect(lambda: apply_change(self, core.players.start_gyro_calibration))
        tutorial = PushButton("Watch tutorial", calibration)
        tutorial.setToolTip(TUTORIAL_URL)
        tutorial.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(TUTORIAL_URL)))
        calibration.row("Magnetometer calibration", tutorial, "Move the controller in a figure 8 pattern.")
        self.tutorial = tutorial
        box.addStretch()
        self.bridge = BusBridge(core.bus, self)
        self.bridge.event.connect(self.apply_event)
        self.destroyed.connect(self.bridge.close)
        self.refresh()

    def refresh(self):
        self.apply_settings(self.core.settings.state())

    def apply_settings(self, state):
        self._settings_state = state
        specs = {c.key: c for c in state.gyro.controls}
        sensitivity_key = "r_joystick_gyro_sensitivity" if specs.get("gyro_control_mode") and specs["gyro_control_mode"].value == "R Joystick" else "gyro_sensitivity"
        if sensitivity_key in specs:
            specs["gyro_sensitivity"] = specs[sensitivity_key]
        for title, names in (("Pass-through", PASS), ("In-app gyro", APP), ("Dual Joy-Con gyro", DUAL)):
            for name in names:
                c = specs.get(name)
                if c is None:
                    continue
                self.keys[name] = c.key
                self.values[name] = c.value
                if name not in self.controls:
                    widget = BooleanChoice(self) if isinstance(c.value, bool) else Choice(self) if c.options else Numeric(c, self)
                    widget.setAccessibleName(LABELS.get(name, c.label.rstrip(":")))
                    row = self.groups[title].row(LABELS.get(name, c.label.rstrip(":")), widget, CAPTIONS.get(name))
                    row.setToolTip(c.label)
                    self.controls[name], self.rows[name] = widget, row
                    if isinstance(widget, Numeric):
                        bind_numeric(self, widget, lambda val, n=name: self.core.settings.set_gyro_setting(self.keys[n], val),
                            lambda n=name: self.values[n], row,
                            writer_factory=lambda n=name: partial(self.core.settings.set_gyro_setting, self.keys[n]))
                    else:
                        widget.edited.connect(lambda val, n=name: self.change_choice(n, val))
                widget = self.controls[name]
                if isinstance(widget, (Choice, BooleanChoice)):
                    widget.sync(c.options, c.value)
                else:
                    widget.sync(c.value)
                self.rows[name].setVisible(c.visible and (name not in DUAL[1:] or bool(specs["djg_enabled"].value)))
        if "djg_enabled" in specs:
            self.groups["Dual Joy-Con gyro"].setVisible(specs["djg_enabled"].visible)

    def change_choice(self, name, value):
        old = self.values[name]
        # Dedicated service preserves outgoing per-mode sensitivity and axes.
        if name == "gyro_control_mode":
            self.finish_edits()
            fn = self.core.settings.set_gyro_control_mode
            apply_change(self, fn, value, row=self.rows[name], undo=lambda: apply_change(self, fn, old))
        else:
            apply_change(self, self.core.settings.set_gyro_setting, self.keys[name], value,
                         row=self.rows[name], old_args=(self.keys[name], old))

    def finish_edits(self):
        for widget in self.controls.values():
            if isinstance(widget, Numeric):
                widget.finish()

    def apply_event(self, topic, value):
        if topic == events.SETTINGS_CHANGED:
            keys = getattr(value, "keys", None)
            if keys is None or "gyro" in keys or "mode_shift_enabled" in keys or set(self.keys.values()) & set(keys):
                self.refresh()
        elif topic == events.TASK_STATE and value.task_id == "gyro_calibration":
            self.calibrate.setEnabled(not value.active)
            self.calibration_row.captionLabel.setText(value.message)
            self.calibration_row.captionLabel.show()

    def closeEvent(self, event):
        self.finish_edits(); self.bridge.close(); super().closeEvent(event)

    def hideEvent(self, event):
        self.finish_edits(); super().hideEvent(event)


def create_page(core, toast=None, parent=None):
    """Lazy page factory used by MainWindow."""
    return GyroPage(core, toast, parent)
