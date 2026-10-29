"""Shared model-driven choices and throttled numeric controls."""
from PySide6.QtCore import Qt, QTimer, Signal
import sys
from PySide6.QtWidgets import QWidget, QHBoxLayout
from qfluentwidgets import ComboBox, Slider, InfoBar, InfoBarPosition, PushButton, DoubleSpinBox, SwitchButton
from .display import setting_title


def local_toast(parent):
    def toast(message, kind="info", title="", seconds=4, undo=None):
        old = getattr(parent, "_toast", None)
        if old:
            old.close()
        bar = getattr(InfoBar, kind, InfoBar.info)(title, message, duration=4000,
            position=InfoBarPosition.TOP, parent=parent)
        parent._toast = bar
        if undo:
            button = PushButton("Undo", bar)
            button.clicked.connect(lambda: (bar.close(), undo()))
            bar.addWidget(button)
    return toast


class Choice(ComboBox):
    edited = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(170)
        self._options = None
        self.currentIndexChanged.connect(lambda i: self.edited.emit(self.itemData(i)))

    def sync(self, options, value):
        items = [(getattr(o, "value", o), setting_title(str(getattr(o, "label", o)))) for o in options]
        # Keep old imported values visible without making them a new normal choice.
        if value not in [v for v, _ in items]:
            items.append((value, setting_title(f"{value} (legacy)")))
        self.blockSignals(True)
        if items != self._options:
            self.clear()
            for val, label in items:
                self.addItem(label, userData=val)
            self._options = items
        self.setCurrentIndex([v for v, _ in items].index(value))
        self.setToolTip(self.currentText())
        self.blockSignals(False)


class BooleanChoice(SwitchButton):
    edited = Signal(object)
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setOnText(""); self.setOffText("")
        self.checkedChanged.connect(self.edited.emit)
    def sync(self, options, value):
        self.blockSignals(True); self.setChecked(bool(value)); self.blockSignals(False)


class Numeric(QWidget):
    """A fixed-width slider/spin pair, throttled and save-batched on all input paths."""
    edited = Signal(object)
    started = Signal()
    finished = Signal()

    def __init__(self, control, parent=None):
        super().__init__(parent)
        self.lo, self.hi, self.step = control.minimum or 0, control.maximum, control.step or 1
        self._active = False
        self._pending = None
        box = QHBoxLayout(self); box.setContentsMargins(0, 0, 0, 0)
        self.spin = DoubleSpinBox(self)
        self.spin.setDecimals(1 if self.step < 1 or control.key == "deadzone_amount" else 0)
        self.spin.setRange(self.lo, self.hi if self.hi is not None else sys.float_info.max)
        self.spin.setSingleStep(self.step); self.spin.setFixedWidth(140)
        self.spin.setKeyboardTracking(self.hi is None)
        self.slider = None
        if self.hi is not None:
            self.slider = Slider(Qt.Horizontal, self)
            self.slider.setRange(0, round((self.hi - self.lo) / self.step))
            self.slider.setFixedWidth(150)
            box.addWidget(self.slider)
            self.slider.sliderPressed.connect(self.begin)
            self.slider.sliderReleased.connect(self.finish)
            self.slider.valueChanged.connect(lambda v: self.change(self.lo + v * self.step))
        box.addWidget(self.spin)
        self.spin.valueChanged.connect(self.change)
        self.throttle = QTimer(self); self.throttle.setInterval(60); self.throttle.setSingleShot(True)
        self.throttle.timeout.connect(self.flush)
        self.idle = QTimer(self); self.idle.setInterval(180); self.idle.setSingleShot(True)
        self.idle.timeout.connect(self.finish)
        self.sync(control.value)

    def sync(self, value):
        if self._active:
            return
        # Display legacy snapshots faithfully; never write just because we rendered.
        self.spin.blockSignals(True)
        self.spin.setMinimum(min(self.lo, float(value)))
        self.spin.setValue(float(value)); self.spin.blockSignals(False)
        if self.slider:
            self.slider.blockSignals(True)
            self.slider.setValue(round((float(value) - self.lo) / self.step))
            self.slider.blockSignals(False)

    def begin(self):
        if not self._active:
            self._active = True; self.started.emit()

    def change(self, value):
        self.begin()
        value = max(self.lo, float(value))
        self._pending = value
        self.spin.blockSignals(True); self.spin.setValue(value); self.spin.blockSignals(False)
        if self.slider:
            self.slider.blockSignals(True)
            self.slider.setValue(round((value - self.lo) / self.step))
            self.slider.blockSignals(False)
        if not self.throttle.isActive():
            self.throttle.start()
        if not self.slider or not self.slider.isSliderDown():
            self.idle.start()

    def flush(self):
        self.throttle.stop()
        if self._pending is not None:
            value, self._pending = self._pending, None
            self.edited.emit(int(value) if self.spin.decimals() == 0 else value)

    def finish(self):
        self.idle.stop(); self.flush()
        if self._active:
            self._active = False; self.finished.emit()


