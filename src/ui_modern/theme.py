"""UI-only preferences and system accent."""
import sys
from PySide6.QtCore import QSettings
from PySide6.QtGui import QFont
from qfluentwidgets import Theme, setTheme, setThemeColor

def preferences():
    return QSettings("Switch2Connect", "ModernUI")

def apply_theme(name=None):
    name = name or preferences().value("theme", "System")
    setTheme({"System": Theme.AUTO, "Light": Theme.LIGHT, "Dark": Theme.DARK}.get(name, Theme.AUTO))

def initialize_theme(app):
    app.setFont(QFont("Segoe UI Variable", 10))
    accent = "#0067C0"
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        color, opaque = wintypes.DWORD(), wintypes.BOOL()
        try:
            ctypes.windll.dwmapi.DwmGetColorizationColor(ctypes.byref(color), ctypes.byref(opaque))
            accent = f"#{color.value & 0xffffff:06x}"
        except OSError:
            pass
    setThemeColor(accent)
    apply_theme()


def current_ui_priority():
    """Capture the GUI thread's original Win32 priority (classic policy)."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.windll.kernel32
    kernel.GetCurrentThread.restype = wintypes.HANDLE
    kernel.GetThreadPriority.argtypes = [wintypes.HANDLE]
    value = int(kernel.GetThreadPriority(kernel.GetCurrentThread()))
    return None if value == 0x7fffffff else value


def set_ui_priority(value):
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.windll.kernel32
        kernel.GetCurrentThread.restype = wintypes.HANDLE
        kernel.SetThreadPriority.argtypes = [wintypes.HANDLE, ctypes.c_int]
        kernel.SetThreadPriority(kernel.GetCurrentThread(), int(value))
