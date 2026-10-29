"""Profile combo capture retains classic release-to-save and duplicate retry behavior."""
from PySide6.QtCore import Qt, QEvent, Signal, QTimer
from PySide6.QtWidgets import QApplication, QDialog
from qfluentwidgets import MessageBoxBase, BodyLabel
from ..qt_bridge import call_core
from .display import input_display
from .recording import key_token


class ComboRecorder(MessageBoxBase):
    captured = Signal(object)

    def done(self, code):
        # A quick released combo can finish during the Fluent fade-in animation.
        QDialog.done(self, code)

    def __init__(self, core, save, parent=None, on_saved=None):
        super().__init__(parent)
        self.core, self.save = core, save
        self.on_saved = on_saved
        self.tokens, self.pressed = [], set()
        self.setProperty("recording_active", True)
        self.saving = False; self.closed = False
        self.viewLayout.addWidget(BodyLabel('Record profile switching combo', self))
        self.help = BodyLabel('Press and release keys, mouse buttons, wheel or controller buttons. Escape cancels.', self)
        self.help.setWordWrap(True); self.viewLayout.addWidget(self.help)
        self.value = BodyLabel('Listening…', self); self.value.setWordWrap(True); self.viewLayout.addWidget(self.value)
        self.yesButton.setText('Save'); self.cancelButton.setText('Cancel')
        self.yesButton.clicked.disconnect(); self.yesButton.clicked.connect(self.submit)
        self.captured.connect(self.controller_tokens, Qt.QueuedConnection)
        QApplication.instance().installEventFilter(self)
        self.finished.connect(self.cleanup)
        self.widget.setMinimumWidth(460)
        call_core(core.profiles.begin_controller_combo_capture, self.captured.emit)

    def add_tokens(self, tokens):
        for token in tokens:
            if token not in self.tokens:
                self.tokens.append(token)
        self.value.setText(input_display('+'.join(self.tokens)) if self.tokens else 'Listening…')

    def controller_tokens(self, tokens):
        if not self.closed and not self.saving:
            self.add_tokens(tokens.split('+') if isinstance(tokens, str) else tokens)
            if not self.pressed:
                self.submit()

    @staticmethod
    def key_token(event):
        return key_token(event)

    def eventFilter(self, obj, event):
        if getattr(self, 'closed', True) or getattr(self, 'saving', True) or not self.isVisible():
            return super().eventFilter(obj, event)
        kind = event.type()
        if kind in (QEvent.KeyPress, QEvent.KeyRelease):
            if event.key() == Qt.Key_Escape:
                self.reject(); return True
            if event.isAutoRepeat():
                return True
            token = self.key_token(event)
            if kind == QEvent.KeyPress:
                self.pressed.add(token); self.add_tokens([token])
            else:
                self.pressed.discard(token)
                if not self.pressed and self.tokens:
                    self.submit()
            return True
        if kind in (QEvent.MouseButtonPress, QEvent.MouseButtonRelease):
            if obj in (self.yesButton, self.cancelButton):
                return False
            token = {Qt.LeftButton: 'MB_1', Qt.RightButton: 'MB_3', Qt.MiddleButton: 'MB_2',
                     Qt.BackButton: 'MB_4', Qt.ForwardButton: 'MB_5'}.get(event.button())
            if token:
                if kind == QEvent.MouseButtonPress:
                    self.pressed.add(token); self.add_tokens([token])
                else:
                    self.pressed.discard(token)
                    if not self.pressed:
                        self.submit()
                return True
        if kind == QEvent.Wheel:
            self.add_tokens(['MW_UP' if event.angleDelta().y() > 0 else 'MW_DOWN'])
            if not self.pressed:
                self.submit()
            return True
        return super().eventFilter(obj, event)

    def submit(self):
        if self.saving:
            return
        self.saving = True
        self.yesButton.setEnabled(False); self.cancelButton.setEnabled(False)
        call_core(self.core.profiles.end_controller_combo_capture)
        def done(result):
            self.saving = False
            self.yesButton.setEnabled(True); self.cancelButton.setEnabled(True)
            if getattr(result, 'restart_recording', False):
                self.help.setText('This combo is assigned to another profile. Record a different combo.')
                self.tokens.clear(); self.pressed.clear(); self.value.setText('Listening…')
                QTimer.singleShot(100, lambda: call_core(self.core.profiles.begin_controller_combo_capture, self.captured.emit) if not self.closed else None)
            elif getattr(result, 'ok', True):
                if self.on_saved: self.on_saved('+'.join(self.tokens))
                self.accept()
            else:
                self.help.setText(result.reason or 'Could not save combo.')
                call_core(self.core.profiles.begin_controller_combo_capture, self.captured.emit)
        call_core(self.save, '+'.join(self.tokens), on_result=done, on_error=self.capture_error)

    def capture_error(self, exc):
        self.saving = False; self.help.setText(str(exc))
        self.yesButton.setEnabled(True); self.cancelButton.setEnabled(True)
        call_core(self.core.profiles.begin_controller_combo_capture, self.captured.emit)

    def cleanup(self, accepted):
        self.setProperty("recording_active", False)
        self.closed = True
        QApplication.instance().removeEventFilter(self)
        call_core(self.core.profiles.end_controller_combo_capture)
        if not accepted:
            call_core(self.save, '')  # classic cancellation clears the stored combo
