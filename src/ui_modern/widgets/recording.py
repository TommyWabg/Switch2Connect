"""Keyboard/mouse/controller input recorder shared by mapping and IR settings."""
from ..widgets.changes import apply_change
from PySide6.QtCore import Qt, QEvent, Signal
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QApplication, QWidget
from qfluentwidgets import PushButton, BodyLabel, CaptionLabel, FlowLayout, isDarkTheme
from .common import SettingsGroup, Segments
from ..qt_bridge import call_core
from .support import SettingsDialog
from .display import input_display

_ALIASES = {
    **{k: "VK_CONTROL" for k in ("VK_CONTROL_L", "VK_CONTROL_R", "VK_LCONTROL", "VK_RCONTROL")},
    **{k: "VK_SHIFT" for k in ("VK_SHIFT_L", "VK_SHIFT_R", "VK_LSHIFT", "VK_RSHIFT")},
    **{k: "VK_MENU" for k in ("VK_ALT", "VK_ALT_L", "VK_ALT_R", "VK_LMENU", "VK_RMENU")},
    **{k: "VK_LWIN" for k in ("VK_WIN", "VK_WIN_L", "VK_WIN_R", "VK_RWIN")},
}
_MODIFIERS = ("VK_CONTROL", "VK_SHIFT", "VK_MENU", "VK_LWIN")


def canonical_tokens(tokens):
    unique = list(dict.fromkeys(_ALIASES.get(token, token) for token in tokens))
    return [x for x in _MODIFIERS if x in unique] + [x for x in unique if x not in _MODIFIERS]


def key_token(event):
    special = {Qt.Key_Control: "CONTROL", Qt.Key_Shift: "SHIFT", Qt.Key_Alt: "MENU",
               Qt.Key_Meta: "LWIN", Qt.Key_Return: "RETURN", Qt.Key_Enter: "RETURN",
               Qt.Key_Backspace: "BACK", Qt.Key_Delete: "DELETE", Qt.Key_Insert: "INSERT",
               Qt.Key_PageUp: "PRIOR", Qt.Key_PageDown: "NEXT", Qt.Key_Space: "SPACE",
               Qt.Key_Tab: "TAB", Qt.Key_Backtab: "TAB", Qt.Key_CapsLock: "CAPITAL", Qt.Key_NumLock: "NUM_LOCK",
               Qt.Key_ScrollLock: "SCROLL_LOCK", Qt.Key_Print: "PRINT",
               Qt.Key_Minus: "MINUS", Qt.Key_Equal: "EQUAL", Qt.Key_BracketLeft: "BRACKETLEFT",
               Qt.Key_BracketRight: "BRACKETRIGHT", Qt.Key_Backslash: "BACKSLASH",
               Qt.Key_Semicolon: "SEMICOLON", Qt.Key_Apostrophe: "APOSTROPHE",
               Qt.Key_QuoteLeft: "GRAVE", Qt.Key_Comma: "COMMA", Qt.Key_Period: "PERIOD", Qt.Key_Slash: "SLASH"}
    if event.modifiers() & Qt.KeypadModifier:
        if Qt.Key_0 <= event.key() <= Qt.Key_9:
            return f"VK_KP_{event.key() - Qt.Key_0}"
        keypad = {Qt.Key_Plus: "ADD", Qt.Key_Minus: "SUBTRACT", Qt.Key_Asterisk: "MULTIPLY",
                  Qt.Key_Slash: "DIVIDE", Qt.Key_Period: "DECIMAL"}
        if event.key() in keypad:
            return "VK_" + keypad[event.key()]
    name = special.get(event.key())
    if name is None:
        name = QKeySequence(event.key()).toString().upper()
    return "VK_" + name if name else None


def pressed_keyboard_tokens():
    """Recover keys swallowed by OS shortcuts on focus loss, as the classic UI did."""
    import sys
    if sys.platform != "win32":
        return ()
    import ctypes
    names = {8: "BACK", 9: "TAB", 13: "RETURN", 16: "SHIFT", 17: "CONTROL", 18: "MENU",
             19: "PAUSE", 20: "CAPITAL", 27: "ESCAPE", 32: "SPACE", 33: "PRIOR", 34: "NEXT",
             35: "END", 36: "HOME", 37: "LEFT", 38: "UP", 39: "RIGHT", 40: "DOWN",
             44: "SNAPSHOT", 45: "INSERT", 46: "DELETE", 91: "LWIN", 92: "RWIN",
             144: "NUM_LOCK", 145: "SCROLL_LOCK", 160: "LSHIFT", 161: "RSHIFT",
             162: "LCONTROL", 163: "RCONTROL", 164: "LMENU", 165: "RMENU"}
    names.update({key: chr(key) for key in (*range(48, 58), *range(65, 91))})
    names.update({key: f"F{key - 111}" for key in range(112, 136)})
    names.update({key: f"KP_{key - 96}" for key in range(96, 106)})
    return canonical_tokens(["VK_" + name for key, name in names.items()
                             if ctypes.windll.user32.GetAsyncKeyState(key) & 0x8000])


class RecordDialog(SettingsDialog):
    """Save emits a stored value; Cancel/Escape/close invalidate all queued capture work.

    writer(value) optionally supplies the IR dialog's origin-aware service call. The default
    writes set_custom_mapping(key, tokens, mode, scope). Controller initial-release
    gating is owned by profiles.begin_controller_combo_capture.
    """
    controller_tokens = Signal(int, object)

    def __init__(self, core, key=None, scope=None, toast=lambda *a, **k: None,
                 parent=None, *, initial="", writer=None, on_saved=None, mode=None,
                 keyboard_snapshot=None):
        super().__init__(core, "Record keys", toast, parent)
        self.key, self.scope, self.writer, self.on_saved = key, scope, writer, on_saved
        self.keyboard_snapshot = keyboard_snapshot or pressed_keyboard_tokens
        self.setProperty("recording_active", True)
        self.active = True; self.generation = 0; self.tokens = []
        mode = mode or ("Tap" if initial.startswith("Custom[Tap]:") else "Hold")
        if initial.startswith("Custom") and ":" in initial:
            self.tokens = canonical_tokens(initial.split(":", 1)[1].split("+"))
        group = SettingsGroup("Custom mapping", self); self.box.addWidget(group)
        self.capture = PushButton("Click here, then press keys, mouse buttons or controller buttons", self)
        self.capture.setMinimumHeight(64); self.capture.setFocusPolicy(Qt.StrongFocus)
        self.capture.setAccessibleName("Recording area")
        group.row("Capture input", self.capture)
        self.chips = QWidget(self); self.chip_layout = FlowLayout(self.chips, needAni=False)
        self.chip_layout.setContentsMargins(0, 0, 0, 0); self.chip_layout.setHorizontalSpacing(6)
        group.row("Recorded keys", self.chips)
        self.mode = Segments(self); self.mode.sync(("Hold", "Tap"), mode)
        group.row("Activation", self.mode)
        self.clear = PushButton("Clear", self); group.row("Remove all recorded keys", self.clear)
        self.clear.clicked.connect(self.clear_tokens)
        self.add_tab = PushButton("Add Tab", self); self.add_tab.clicked.connect(lambda: self.add_tokens(["VK_TAB"]))
        group.row("Record a navigation key", self.add_tab)
        self.box.addWidget(CaptionLabel("Escape cancels. Tab moves between controls. Empty recordings restore Default.", self))
        self.footer(self.save_recording)
        self.render_tokens()
        self.controller_tokens.connect(self.receive_controller, Qt.QueuedConnection)
        self.finished.connect(self.stop)
        QApplication.instance().installEventFilter(self)
        self.start_controller()
        self.capture.setFocus()

    def start_controller(self):
        if not self.active:
            return
        generation = self.generation
        call_core(self.core.profiles.begin_controller_combo_capture,
                  lambda tokens: self.controller_tokens.emit(generation, tokens),
                  on_error=lambda e: self.toast(str(e), "warning") if self.active else None)

    def receive_controller(self, generation, tokens):
        if self.active and generation == self.generation:
            self.add_tokens(tokens)
            self.start_controller()

    def clear_tokens(self):
        self.generation += 1; self.tokens.clear(); self.render_tokens()
        call_core(self.core.profiles.end_controller_combo_capture)
        self.start_controller()

    def add_tokens(self, tokens):
        if self.active:
            self.tokens = canonical_tokens([*self.tokens, *tokens]); self.render_tokens()

    def render_tokens(self):
        for index in range(self.chip_layout.count()):
            chip = self.chip_layout.itemAt(index).widget()
            chip.hide(); chip.deleteLater()
        self.chip_layout.removeAllWidgets()
        for token in self.tokens or ["No keys recorded"]:
            chip = BodyLabel(input_display(token), self.chips)
            chip.setStyleSheet("background: rgba(128,128,128,30); padding: 4px 8px; border-radius: 6px; color: " +
                              ("white;" if isDarkTheme() else "black;"))
            chip.setToolTip(input_display(token)); self.chip_layout.addWidget(chip)
        self.chips.setToolTip(input_display("+".join(self.tokens)))

    @property
    def value(self):
        tokens = canonical_tokens(self.tokens)
        return f"Custom[{self.mode.currentRouteKey()}]:" + "+".join(tokens) if tokens else "Default"

    def eventFilter(self, watched, event):
        if not self.active or not self.isVisible():
            return False
        if watched is self and event.type() == QEvent.WindowDeactivate:
            self.add_tokens(self.keyboard_snapshot())
            return False
        if event.type() == QEvent.KeyPress and self.isActiveWindow() and event.key() == Qt.Key_Escape:
            self.reject(); return True
        if watched is not self.capture:
            return False
        if event.type() in (QEvent.KeyPress, QEvent.KeyRelease):
            if event.key() in (Qt.Key_Tab, Qt.Key_Backtab) and not event.modifiers() & (Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier):
                return False
            if event.type() == QEvent.KeyPress and not event.isAutoRepeat():
                tokens = []
                for flag, token in ((Qt.ControlModifier, "VK_CONTROL"), (Qt.ShiftModifier, "VK_SHIFT"),
                                    (Qt.AltModifier, "VK_MENU"), (Qt.MetaModifier, "VK_LWIN")):
                    if event.modifiers() & flag:
                        tokens.append(token)
                token = key_token(event)
                if token:
                    self.add_tokens([*tokens, token])
            return True
        if event.type() == QEvent.MouseButtonPress:
            button = {Qt.LeftButton: 1, Qt.MiddleButton: 2, Qt.RightButton: 3,
                      Qt.BackButton: 4, Qt.ForwardButton: 5}.get(event.button())
            if button:
                self.add_tokens([f"MB_{button}"])
            return True
        if event.type() == QEvent.Wheel:
            self.add_tokens(["MW_UP" if event.angleDelta().y() > 0 else "MW_DOWN"])
            return True
        return False

    def stop(self, *_):
        if self.active:
            self.setProperty("recording_active", False)
            self.active = False; self.generation += 1
            QApplication.instance().removeEventFilter(self)
            call_core(self.core.profiles.end_controller_combo_capture)

    def save_recording(self):
        if not self.active:
            return
        tokens = canonical_tokens(self.tokens)
        mode = self.mode.currentRouteKey()
        value = f"Custom[{mode}]:" + "+".join(tokens) if tokens else "Default"
        self.stop()
        if self.writer:
            self.writer(value)
        elif self.key is not None:
            apply_change(self, self.core.settings.set_custom_mapping, self.key, tokens, mode, self.scope)
        if self.on_saved:
            self.on_saved(value)
        self.accept()
