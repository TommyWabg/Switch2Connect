"""Live, asynchronous views over CalibrationService sessions."""
from ..widgets.changes import apply_change
from PySide6.QtCore import Qt, QTimer, QPointF
from PySide6.QtGui import QPainter, QColor, QPen
from PySide6.QtWidgets import QDialog, QWidget, QVBoxLayout, QHBoxLayout
from qfluentwidgets import (TitleLabel, BodyLabel, CaptionLabel, PushButton,
                           PrimaryPushButton, ProgressBar, ToolTipFilter, isDarkTheme, qconfig)
from ..qt_bridge import BusBridge, call_core
from ..widgets.common import SettingsGroup


class StickPlot(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent); self.setFixedSize(150, 150)
        self.raw = (0, 0); self.trace = []
        self.setAccessibleName("Live stick position and observed envelope")

    def sample(self, raw):
        self.raw = raw
        self.trace.append(raw); self.trace = self.trace[-400:]; self.update()

    def paintEvent(self, event):
        painter = QPainter(self); painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(QColor("#888888"), 1))
        painter.drawEllipse(10, 10, 130, 130)
        painter.drawLine(75, 10, 75, 140); painter.drawLine(10, 75, 140, 75)
        # Calibration raw sticks use native 12-bit values, centred at 2048.
        def point(raw):
            return QPointF(75 + max(-1, min(1, (raw[0] - 2048) / 2048)) * 65,
                           75 - max(-1, min(1, (raw[1] - 2048) / 2048)) * 65)
        painter.setPen(QPen(QColor("#609dd5"), 2))
        for a, b in zip(self.trace, self.trace[1:]):
            painter.drawLine(point(a), point(b))
        painter.setBrush(QColor("#0067c0")); painter.drawEllipse(point(self.raw), 5, 5)


class SessionDialog(QDialog):
    kind = ""
    def __init__(self, core, session_id=None, parent=None):
        super().__init__(parent)
        self.core, self.session_id = core, session_id
        self.session = None; self._closed = False
        self.setWindowTitle("Calibration"); self.setMinimumWidth(560)
        self.setWindowModality(Qt.WindowModal)
        box = QVBoxLayout(self); box.setContentsMargins(24, 24, 24, 24); box.setSpacing(16)
        self.title = TitleLabel("Starting calibration…", self)
        self.phase = BodyLabel("Waiting for the controller", self); self.phase.setWordWrap(True)
        self.error = CaptionLabel("", self); self.error.setWordWrap(True)
        for label in (self.title, self.phase, self.error):
            label.installEventFilter(ToolTipFilter(label, showDelay=350))
        box.addWidget(self.title); box.addWidget(self.phase)
        self.body = QVBoxLayout(); box.addLayout(self.body)
        box.addWidget(self.error)
        buttons = QHBoxLayout(); buttons.addStretch()
        self.cancel_button = PushButton("Cancel", self)
        self.primary = PrimaryPushButton("Done", self); self.primary.setDefault(True)
        buttons.addWidget(self.cancel_button); buttons.addWidget(self.primary); box.addLayout(buttons)
        self.cancel_button.clicked.connect(self.reject)
        self.primary.clicked.connect(self.advance)
        self.bridge = BusBridge(core.bus, self)
        self.bridge.calibration_session.connect(self.apply_session)
        self.finished.connect(self._finish)
        self.auto_close = QTimer(self); self.auto_close.setSingleShot(True); self.auto_close.setInterval(3000)
        self.auto_close.timeout.connect(self.accept)
        qconfig.themeChangedFinished.connect(self.refresh_theme); self.refresh_theme()

    def refresh_theme(self):
        self.setStyleSheet("QDialog { background: " + ("#202020" if isDarkTheme() else "#f3f3f3") + "; }")

    def attach(self, session_id):
        if self._closed:
            if session_id:
                call_core(self.core.calibration.cancel, session_id)
            return
        if not session_id:
            self.error.setText("Calibration could not start. Check the connected controller.")
            return
        self.session_id = session_id
        for session in self.core.calibration.state().sessions:
            self.apply_session(session)

    def apply_session(self, session):
        if self._closed:
            return
        if session.session_id != self.session_id:
            # BusBridge coalesces the topic; another session's last event can
            # replace ours. Recover our immutable snapshot on every delivery.
            session = next((item for item in self.core.calibration.state().sessions
                            if item.session_id == self.session_id), None)
            if session is None:
                return
        self.session = session
        title = "Joystick calibration" if self.kind == "joystick" else "GameCube trigger calibration"
        self.title.setText(title); self.setWindowTitle(title)
        self.title.setToolTip(title)
        self.phase.setText(session.message); self.phase.setToolTip(session.message)
        self.render(session)
        self.cancel_button.setEnabled(not session.completed)
        if not session.active:
            if session.completed:
                self.accept()
            else:
                self._closed = True; super().reject()
        elif session.completed and not self.auto_close.isActive():
            self.auto_close.start()


    def advance(self):
        if self.session and self.session.completed:
            self.accept()

    def reject(self):
        if not self._closed and self.session_id and (self.session is None or self.session.active and not self.session.completed):
            apply_change(self, self.core.calibration.cancel, self.session_id)
        super().reject()

    def closeEvent(self, event):
        self.reject(); event.accept()

    def _finish(self, result):
        self._closed = True; self.auto_close.stop(); self.bridge.close()


class JoystickCalibrationWizard(SessionDialog):
    kind = "joystick"
    def __init__(self, core, session_id=None, parent=None):
        super().__init__(core, session_id, parent)
        self.sticks = {}
        self.group = SettingsGroup("Stick progress", self); self.body.addWidget(self.group)
        self.primary.setEnabled(False)
        if session_id:
            self.attach(session_id)

    def render(self, session):
        self.primary.setEnabled(session.completed)
        for stick in session.sticks:
            if stick.key not in self.sticks:
                panel = QWidget(self); layout = QHBoxLayout(panel); layout.setContentsMargins(0, 0, 0, 0)
                plot = StickPlot(panel); progress = ProgressBar(panel); progress.setFixedWidth(160)
                counter = CaptionLabel("", panel)
                counter.installEventFilter(ToolTipFilter(counter, showDelay=350))
                details = QVBoxLayout(); details.setSpacing(6)
                progress.setFixedHeight(4)
                details.addStretch(); details.addWidget(progress); details.addWidget(counter); details.addStretch()
                layout.addWidget(plot); layout.addLayout(details)
                row = self.group.row(stick.label, panel)
                self.sticks[stick.key] = (plot, progress, counter, row)
            plot, bar, counter, row = self.sticks[stick.key]
            plot.sample(stick.raw)
            rotate = session.phase == "rotate"
            elapsed = stick.rotate_elapsed if rotate else stick.release_elapsed
            bar.setValue(min(100, round(elapsed / (10 if rotate else 3) * 100)))
            if stick.rotate_done and stick.release_done:
                message = "Done"
            elif rotate and not stick.rotate_done:
                message = f"Keep rotating — {max(0, 10 - int(stick.rotate_elapsed))} s left"
            elif stick.phase in ("release", "wait", "waiting", "still"):
                message = "Release the stick" if stick.release_elapsed == 0 else "Hold still…"
            else:
                message = "Hold still…"
            counter.setText(message)
            counter.setToolTip(counter.text())


class GcTriggerWizard(SessionDialog):
    kind = "gc_trigger"
    def __init__(self, core, session_id=None, parent=None):
        super().__init__(core, session_id, parent)
        self.primary.setText("Next"); self.primary.setEnabled(False)
        group = SettingsGroup("Live trigger values", self); self.body.addWidget(group)
        self.bars = []
        for side in ("Left trigger", "Right trigger"):
            bar = ProgressBar(self); bar.setRange(0, 255); bar.setFixedWidth(240)
            bar.installEventFilter(ToolTipFilter(bar, showDelay=350))
            group.row(side, bar); self.bars.append(bar)
        if session_id:
            self.attach(session_id)

    def render(self, session):
        self.primary.setText(session.next_label); self.primary.setEnabled(session.active)
        for bar, value in zip(self.bars, session.trigger_values or (0, 0)):
            bar.setValue(value); bar.setToolTip(str(value))

    def advance(self):
        if self.session_id:
            self.primary.setEnabled(False)
            apply_change(self, self.core.calibration.next, self.session_id,
                         on_finished=lambda: self.primary.setEnabled(True) if not self._closed else None)


def _open(cls, method, core, slot_token, parent):
    dialog = cls(core, parent=parent)
    # Retain non-modal dialogs until finished, including parentless callers.
    _dialogs.add(dialog)
    dialog.finished.connect(lambda _: _dialogs.discard(dialog))
    dialog.show()
    apply_change(dialog, method, slot_token, after=dialog.attach)
    return dialog


_dialogs = set()


def open_joystick_wizard(core, slot_token, parent=None):
    return _open(JoystickCalibrationWizard, core.calibration.start_joystick, core, slot_token, parent)


def open_gc_trigger_wizard(core, slot_token=None, parent=None):
    return _open(GcTriggerWizard, core.calibration.start_gc_trigger, core, slot_token, parent)
