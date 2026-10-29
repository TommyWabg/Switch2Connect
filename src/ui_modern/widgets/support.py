"""Presentation helpers for settings dialogs; all mutations go through core services."""
from .changes import apply_change
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QWidget
from qfluentwidgets import TitleLabel, PushButton, PrimaryPushButton, BodyLabel, FluentStyleSheet, ScrollArea
from ..qt_bridge import call_core, BusBridge
from app_core import events
from app_core.models import Control
from .controls import Numeric


class ThemedDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowModality(Qt.WindowModal)
        FluentStyleSheet.DIALOG.apply(self)


class FluentSurface(QDialog):
    """Opaque embedded surface managed by Fluent's theme stylesheet."""
    def __init__(self, parent=None):
        super().__init__(parent, Qt.Widget)
        FluentStyleSheet.DIALOG.apply(self)


class SettingsDialog(ThemedDialog):
    def __init__(self, core, title, toast, parent=None):
        super().__init__(parent)
        self.core, self.toast = core, toast
        self.setObjectName("mappingDialog")
        self.setWindowTitle(title)
        self.setMinimumWidth(640)
        self.setModal(True)
        self.resize(820, 640)
        self.outer = QVBoxLayout(self)
        self.outer.setContentsMargins(24, 20, 24, 20); self.outer.setSpacing(12)
        self.outer.addWidget(TitleLabel(title, self))
        self.scroll = ScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.enableTransparentBackground()
        self.body = QWidget(self.scroll); self.body.setObjectName("mappingDialogBody"); self.scroll.setWidget(self.body)
        self.box = QVBoxLayout(self.body); self.box.setContentsMargins(0, 0, 0, 0); self.box.setSpacing(12)
        self.box.setAlignment(Qt.AlignTop)
        self.outer.addWidget(self.scroll, 1)
        self.bridge = BusBridge(core.bus, self)
        self.bridge.event.connect(self.event_received)
        self.finished.connect(lambda _: self.bridge.close())
        # A dialog's captured scope must not silently become a different profile.
        self.origin_profile = core.profiles.state().active
        driver = core.drivers.state()
        self.origin_output = (driver.driver_type, driver.simulation_mode)

    def event_received(self, topic, payload):
        switched_profile = topic == events.PROFILES_CHANGED and payload.active != self.origin_profile
        switched_output = topic == events.DRIVER_STATE_CHANGED and (payload.driver_type, payload.simulation_mode) != self.origin_output
        if switched_profile or switched_output:
            # Never flush a pending pointer value into a newly activated profile.
            for slider in self.findChildren(BatchSlider):
                slider.pending = None
                slider.timer.stop()
                slider.toast = None
                slider.finish()
            self.reject()
        elif topic == events.SETTINGS_CHANGED and hasattr(self, "apply_settings"):
            self.render_settings(self.core.settings.state())

    def start_settings(self):
        self.render_settings(self.core.settings.state())

    def render_settings(self, state):
        self._settings_state = state
        self.apply_settings(state)

    def footer(self, save=None):
        row = QHBoxLayout(); row.addStretch()
        self.cancel = PushButton("Cancel" if save else "Close", self)
        self.cancel.clicked.connect(self.reject); row.addWidget(self.cancel)
        if save:
            self.save = PrimaryPushButton("Save", self)
            self.save.setDefault(True); self.save.clicked.connect(save); row.addWidget(self.save)
        self.outer.addLayout(row)


class BatchSlider(Numeric):
    """Slider-only presentation of the shared throttled numeric control."""
    def __init__(self, core, minimum, maximum, step, value, writer, parent=None,
                 toast=None, reader=None):
        super().__init__(Control("value", "Value", value, minimum=minimum, maximum=maximum, step=step), parent)
        self.core, self.writer, self.toast, self.reader = core, writer, toast, reader
        self.initial = value
        self.timer = self.throttle
        self.layout().removeWidget(self.spin)
        self.spin.hide()
        self.slider.setMaximumWidth(16777215)
        self.slider.setMinimumWidth(180)
        self.label = BodyLabel(self)
        self.label.setFixedWidth(48)
        self.layout().addWidget(self.label)
        self.slider.valueChanged.connect(lambda _: self.label.setText(f"{self.spin.value():g}"))
        self.started.connect(self.start_batch)
        self.edited.connect(writer)
        self.finished.connect(self.end_batch)
        self.set_value(value)

    @property
    def pending(self):
        return self._pending

    @pending.setter
    def pending(self, value):
        self._pending = value

    @property
    def batching(self):
        return self._active

    def set_value(self, value):
        if not self._active:
            self.sync(value)
            self.label.setText(f"{value:g}")
            self.slider.setAccessibleName(f"Value {value:g}")

    def start_batch(self):
        self.initial = self.reader() if self.reader else self.spin.value()
        call_core(self.core.settings.begin_batch)

    def end_batch(self):
        old = self.initial
        restore = (lambda: self.writer(old)) if self.toast and self.spin.value() != old else None
        apply_change(self, self.core.settings.end_batch, undo=restore)
