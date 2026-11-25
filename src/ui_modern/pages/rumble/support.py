"""Shared page utilities; all mutations cross the existing Qt boundary."""
from PySide6.QtCore import QSignalBlocker
from qfluentwidgets import ScrollArea, SpinBox
from ...qt_bridge import BusBridge
from ...widgets.support import FluentSurface


def sync(widget, method, value):
    with QSignalBlocker(widget):
        getattr(widget, method)(value)


def int_spin(parent=None):
    """Nonnegative integer field that commits on Enter, focus-out or arrow click."""
    spin = SpinBox(parent)
    spin.setRange(0, 2**31 - 1)
    spin.setKeyboardTracking(False)
    # Classic saved unbounded integers; clamp before they reach Qt's 32-bit API.
    spin.setValue = lambda value, set_value=spin.setValue: set_value(min(int(value), 2**31 - 1))
    return spin


class ServicePage(ScrollArea):
    def __init__(self, title, core, toast, parent=None):
        super().__init__(parent)
        self.core, self.toast = core, toast
        self.setObjectName(title.lower())
        self.setWidgetResizable(True)
        self.enableTransparentBackground()
        self.content = FluentSurface(title, self)
        self.content.setObjectName('t10Surface')
        self.setWidget(self.content)
        self.box = self.content.box
        self.bridge = BusBridge(core.bus, self)
        self.destroyed.connect(lambda: self.bridge.close())
