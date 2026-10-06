"""Non-activating desktop views; no controller or profile mutation."""
from PySide6.QtCore import Qt, QTimer, QPropertyAnimation, QEasingCurve
from PySide6.QtGui import QCursor, QGuiApplication
from PySide6.QtWidgets import QWidget, QVBoxLayout
from qfluentwidgets import BodyLabel, CaptionLabel, TitleLabel, ToolTipFilter, isDarkTheme, qconfig


class NotificationOverlay(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent, Qt.Tool | Qt.FramelessWindowHint |
                         Qt.WindowStaysOnTopHint | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setFocusPolicy(Qt.NoFocus)
        self.setFixedSize(420, 160)
        outer = QVBoxLayout(self); outer.setContentsMargins(0, 0, 0, 0)
        self.surface = QWidget(self); self.surface.setObjectName(f"overlaySurface_{id(self)}")
        outer.addWidget(self.surface)
        self.content = QVBoxLayout(self.surface); self.content.setContentsMargins(22, 18, 22, 18)
        self.title = TitleLabel("", self.surface)
        self.message = BodyLabel("", self.surface); self.message.setWordWrap(True)
        for label in (self.title, self.message):
            label.installEventFilter(ToolTipFilter(label, showDelay=350))
        self.content.addWidget(self.title); self.content.addWidget(self.message)
        self.kind = "info"
        self.dismiss_timer = QTimer(self); self.dismiss_timer.setSingleShot(True)
        self.dismiss_timer.timeout.connect(self.dismiss)
        self.fade = QPropertyAnimation(self, b"windowOpacity", self)
        self.fade.setDuration(150); self.fade.setEasingCurve(QEasingCurve.InOutQuad)
        self.fade.finished.connect(self._fade_finished)
        self._dismissing = False
        qconfig.themeChangedFinished.connect(self.refresh_theme)
        self.refresh_theme()

    def refresh_theme(self):
        colors = {"info": "#0067c0", "success": "#0f7b0f", "warning": "#9d5d00", "error": "#c42b1c"}
        bg = "#292929" if isDarkTheme() else "#fafafa"
        self.surface.setStyleSheet(f"QWidget#{self.surface.objectName()} {{background: {bg}; border: 1px solid #888888; border-left: 4px solid {colors.get(self.kind, colors['info'])}; border-radius: 10px;}}")
        self.surface.style().unpolish(self.surface)
        self.surface.style().polish(self.surface)
        self.surface.update()

    def place(self):
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        self.adjustSize()
        self.move(area.right() - self.width() - 19, area.bottom() - self.height() - 19)

    def present(self, payload):
        self.dismiss_timer.stop(); self.fade.stop(); self._dismissing = False
        self.kind = payload.kind
        self.title.setText(payload.title or "Switch 2 Connect")
        self.title.setToolTip(self.title.text())
        self.message.setText(payload.message); self.message.setToolTip(payload.message)
        self.refresh_theme(); self.place()
        self.setWindowOpacity(0); self.show()
        self.fade.setStartValue(0.); self.fade.setEndValue(1.); self.fade.start()
        seconds = payload.auto_close_seconds
        if seconds is not None:
            self.dismiss_timer.start(max(0, round(seconds * 1000)))

    def dismiss(self):
        self.dismiss_timer.stop(); self.fade.stop(); self._dismissing = True
        self.fade.setStartValue(self.windowOpacity()); self.fade.setEndValue(0.); self.fade.start()

    def _fade_finished(self):
        if self._dismissing:
            self.hide()

    def closeEvent(self, event):
        self.dismiss_timer.stop(); self.fade.stop()
        super().closeEvent(event)


class ProfileSelectionOverlay(NotificationOverlay):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(290)
        self.previous = CaptionLabel("", self.surface)
        self.current = TitleLabel("", self.surface)
        self.next = CaptionLabel("", self.surface)
        self.hints = CaptionLabel("", self.surface)
        for label in (self.previous, self.current, self.next, self.hints):
            label.setWordWrap(False); self.content.addWidget(label)
            label.installEventFilter(ToolTipFilter(label, showDelay=350))
        self.manual_active = False

    def apply_event(self, event):
        if event.action == "request":
            return
        if event.action in ("cancel", "confirm"):
            self.manual_active = False; self.dismiss(); return
        if event.action == "notification":
            return
        self.manual_active = event.manual
        self.previous.setText(f"↑  {event.previous or '—'}")
        self.current.setText(event.current or "—")
        self.next.setText(f"↓  {event.next or '—'}")
        for label, value in ((self.previous, event.previous), (self.current, event.current), (self.next, event.next)):
            label.setToolTip(value or "")
        confirm, cancel = ("A", "B") if event.layout == "Switch" else ("B", "A")
        self.hints.setText(f"{event.layout or 'Xbox'} layout · {confirm} Confirm · {cancel} Cancel" if event.manual else f"{event.layout or 'Xbox'} layout · Auto selection")
        from app_core.events import Toast
        seconds = event.auto_close_seconds
        if seconds is None and event.auto_close_ms is not None:
            seconds = event.auto_close_ms / 1000
        self.present(Toast(event.message, event.kind, event.title or "Select profile", seconds))
