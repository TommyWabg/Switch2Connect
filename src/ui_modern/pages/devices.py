from ..widgets.changes import apply_change, apply_setting
from PySide6.QtWidgets import QWidget, QVBoxLayout, QGridLayout, QHBoxLayout, QStackedWidget
from PySide6.QtCore import Qt
from qfluentwidgets import ScrollArea, TitleLabel, BodyLabel, PushButton, IndeterminateProgressRing, CaptionLabel, SubtitleLabel
from app_core.models import SlotInfo
from .rumble.support import int_spin
from ..widgets.common import SettingsGroup, SettingRow, Segments, FitImage, WarningSettingRow
from ..widgets.player_card import PlayerCard, CARD_HEIGHT, _ASSETS
from ..widgets.support import FluentSurface

EMPTY_HEIGHT = 340

class DevicesPage(ScrollArea):
    def __init__(self, core, toast, parent=None):
        super().__init__(parent)
        self.core, self.toast = core, toast
        self.setObjectName("devices")
        self.setWidgetResizable(True)
        self.enableTransparentBackground()
        self.content = FluentSurface(self); self.setWidget(self.content)
        box = QVBoxLayout(self.content); box.setContentsMargins(24, 20, 24, 24); box.setSpacing(12)
        box.addWidget(TitleLabel("Devices", self))
        self.controller_stack = QStackedWidget(self.content)
        self.controller_stack.setFixedHeight(EMPTY_HEIGHT)
        self.empty = QWidget(self.controller_stack)
        empty_box = QVBoxLayout(self.empty); empty_box.setContentsMargins(0, 8, 0, 8); empty_box.setSpacing(8)
        empty_box.addStretch()
        # Classic's pre-rendered pairing hint (pair.svg uses clip paths QtSvg cannot draw).
        self.pair_image = FitImage(_ASSETS / "pairing_hint.png", self.empty)
        self.pair_image.setFixedHeight(200)
        empty_box.addWidget(self.pair_image)
        empty_box.addWidget(SubtitleLabel("No controllers connected", self.empty), 0, Qt.AlignCenter)
        body = BodyLabel("Press a button on a paired controller, or hold the sync button to pair a new one.", self.empty)
        body.setWordWrap(True)
        body.setAlignment(Qt.AlignCenter)
        empty_box.addWidget(body)
        self.connection_route = CaptionLabel("", self.empty)
        empty_box.addWidget(self.connection_route, 0, Qt.AlignCenter); empty_box.addStretch()
        self.card_panel = QWidget(self.controller_stack)
        self.grid = QGridLayout(self.card_panel); self.grid.setContentsMargins(0, 0, 0, 0); self.grid.setSpacing(12)
        self.controller_stack.addWidget(self.empty); self.controller_stack.addWidget(self.card_panel)
        box.addWidget(self.controller_stack)
        self.cards = {i: PlayerCard(self.player_action, self.card_panel) for i in range(4)}
        self._columns = 0
        self._arrange()
        output = SettingsGroup("Output", self.content); self.output_group = output; box.addWidget(output)
        self.driver_action = PushButton("Install", output)
        self.attention = WarningSettingRow(self.driver_action, output)
        self.attention.hide()
        output.addRow(self.attention)
        self.driver_action.clicked.connect(lambda: apply_change(self, core.drivers.on_driver_btn_clicked))
        self.output_progress = IndeterminateProgressRing(output)
        self.output_progress.setFixedSize(18, 18)
        self.output_progress.hide()
        output.heading.addWidget(self.output_progress)
        self.driver = Segments(output)
        self.emulate = Segments(output)
        self.layout_control = Segments(output)
        output.row("Driver", self.driver)
        output.row("Emulate as", self.emulate)
        output.row("Button layout", self.layout_control, "Match face-button positions to Xbox or Switch game prompts.")
        self.driver.currentItemChanged.connect(lambda val: apply_setting(self, core.drivers, "switch_driver", "driver_type", val, self._driver_state))
        self.emulate.currentItemChanged.connect(lambda val: apply_setting(self, core.drivers, "set_simulation_mode", "simulation_mode", val, self._driver_state))
        self.layout_control.currentItemChanged.connect(lambda val: apply_setting(self, core.drivers, "set_layout", "abxy_mode", val, self._driver_state))
        auto = SettingsGroup("Auto disconnect", self.content); box.addWidget(auto)
        self.auto_mode = Segments(auto)
        auto.row("Disconnect controllers", self.auto_mode)
        self.auto_mode.currentItemChanged.connect(lambda value: apply_setting(self, core.settings, "set_auto_disconnect_mode", "mode", value, self._settings_state.auto_disconnect))
        duration = QWidget(auto); row = QHBoxLayout(duration); row.setContentsMargins(0, 0, 0, 0)
        self.duration_spins = []
        for label in ("days", "hours", "minutes"):
            spin = int_spin(duration)
            row.addWidget(spin); row.addWidget(BodyLabel(label, duration)); self.duration_spins.append(spin)
            spin.valueChanged.connect(self.change_duration)
        self.duration_row = auto.row("Duration", duration)
        gyro = SettingsGroup("Calibration", self.content); box.addWidget(gyro)
        self.gyro_button = PushButton("Calibrate", gyro)
        self.gyro_row = gyro.addRow(SettingRow("Calibrate gyro",
            "Place controllers on a flat surface and keep them still", self.gyro_button, gyro))
        self.gyro_status = self.gyro_row.captionLabel
        self.gyro_button.clicked.connect(lambda: apply_change(self, core.players.start_gyro_calibration))
        box.addStretch()
    def _arrange(self):
        columns = 2 if self.viewport().width() < 900 else 4
        if columns == self._columns:
            return
        self._columns = columns
        self._fit_stack()
        for col in range(4):
            self.grid.setColumnStretch(col, 1 if col < columns else 0)
        for i, card in self.cards.items():
            self.grid.addWidget(card, i // columns, i % columns)
    def _fit_stack(self):
        if self.controller_stack.currentIndex() == 0:
            height = EMPTY_HEIGHT
        else:
            height = CARD_HEIGHT * (2 if self._columns == 2 else 1) + (12 if self._columns == 2 else 0)
        self.controller_stack.setFixedHeight(height)
    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "cards"):
            self._arrange()
    def player_action(self, method, token, *args):
        fn = self.core.calibration.start_joystick if method == "calibrate_sticks" else getattr(self.core.players, method)
        apply_change(self, fn, token, *args)
    def change_duration(self):
        old = self._settings_state.auto_disconnect
        fn = self.core.settings.set_auto_disconnect_time
        apply_change(self, fn, *(spin.value() for spin in self.duration_spins), undo=lambda: apply_change(self, fn, old.days, old.hours, old.minutes))
    def apply_slots(self, slots):
        by_index = {slot.index: slot for slot in slots}
        first = next((i for i in range(4) if i not in by_index or by_index[i].is_empty), None)
        for i, card in self.cards.items():
            card.update_snapshot(by_index.get(i, SlotInfo(i, i+1)), i == first)
        occupied = any(not card.snapshot.is_empty for card in self.cards.values())
        self.controller_stack.setCurrentIndex(1 if occupied else 0)
        self._fit_stack()
    def apply_driver(self, state):
        self._driver_state = state
        self.driver.sync(state.driver_options, state.driver_type)
        self.emulate.sync(dict(state.mode_options).get(state.driver_type, ()), state.simulation_mode)
        self.layout_control.sync(("Xbox", "Switch"), state.abxy_mode)
        self.attention.setVisible(state.needs_attention)
        repair = state.action_kind == "repair" or "repair" in state.action_label.lower()
        self.driver_action.setText("Repair" if repair else "Install")
        self.attention.setMessage(f"{state.driver_type} needs repair" if repair else f"{state.driver_type} driver isn't installed",
            "Virtual controller output is unavailable until the driver is " + ("repaired." if repair else "installed."))
        self.output_progress.setVisible(state.busy)
        self.driver_action.setEnabled(not state.busy)
        self.driver.setEnabled(not state.busy)
        self.emulate.setEnabled(not state.busy)
    def apply_settings(self, state):
        self._settings_state = state
        view = state.auto_disconnect
        self.auto_mode.sync(view.options, view.mode, {"OFF": "Never", "Inactive": "When idle", "Absolute": "After time"})
        self.duration_row.setVisible(view.mode != "OFF")
        for spin, value in zip(self.duration_spins, (view.days, view.hours, view.minutes)):
            spin.blockSignals(True); spin.setValue(value); spin.blockSignals(False)
    def task(self, task):
        if "gyro" in task.task_id:
            self.gyro_status.setText(task.message if task.active else "Place controllers on a flat surface and keep them still")
            self.gyro_button.setEnabled(not task.active)

    def apply_status(self, status):
        self.connection_route.setText(status.connection_route)
