"""Non-activating desktop views; no controller or profile mutation."""
from ..i18n import tr
from PySide6.QtCore import Qt, QTimer, QPropertyAnimation, QEasingCurve
from PySide6.QtGui import QCursor, QGuiApplication
from PySide6.QtWidgets import QWidget, QVBoxLayout
from qfluentwidgets import BodyLabel, CaptionLabel, SubtitleLabel, ToolTipFilter, isDarkTheme, qconfig


class NotificationOverlay(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent, Qt.Tool | Qt.FramelessWindowHint |
                         Qt.WindowStaysOnTopHint | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setFocusPolicy(Qt.NoFocus)
        self.setMaximumWidth(340)
        outer = QVBoxLayout(self); outer.setContentsMargins(0, 0, 0, 0)
        self.surface = QWidget(self); self.surface.setObjectName(f"overlaySurface_{id(self)}")
        outer.addWidget(self.surface)
        self.content = QVBoxLayout(self.surface); self.content.setContentsMargins(14, 9, 14, 9)
        self.content.setSpacing(3)
        self.content.setAlignment(Qt.AlignVCenter)
        self.title = SubtitleLabel("", self.surface)
        self.title.setWordWrap(True)
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
        if isDarkTheme():
            bg, border = "#2c2c2c", "#494949"
            colors = {"info": "#60cdff", "success": "#6ccb5f", "warning": "#fce100", "error": "#ff99a4"}
        else:
            bg, border = "#fafafa", "#d6d6d6"
            colors = {"info": "#0067c0", "success": "#0f7b0f", "warning": "#9d5d00", "error": "#c42b1c"}
        self.surface.setStyleSheet(f"QWidget#{self.surface.objectName()} {{background: {bg}; border: 1px solid {border}; border-left: 4px solid {colors.get(self.kind, colors['info'])}; border-radius: 8px;}}")
        self.surface.style().unpolish(self.surface)
        self.surface.style().polish(self.surface)
        self.surface.update()

    def place(self):
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        width = min(self.sizeHint().width(), self.maximumWidth(), area.width() - 40)
        height = self.layout().totalHeightForWidth(width)
        self.resize(width, min(height, area.height() - 40))
        self.move(area.right() - self.width() - 19, area.bottom() - self.height() - 19)

    def present(self, payload):
        self.dismiss_timer.stop(); self.fade.stop(); self._dismissing = False
        self.kind = payload.kind
        self.title.setText(tr(payload.title or "Switch 2 Connect"))
        self.title.setToolTip(self.title.text())
        self.message.setText(tr(payload.message)); self.message.setToolTip(self.message.text())
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
        self.setFixedSize(420, 290)
        self.content.setContentsMargins(20, 16, 20, 16)
        self.content.setSpacing(8)
        self.previous = CaptionLabel("", self.surface)
        self.current = SubtitleLabel("", self.surface)
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
        layout = event.layout or "Xbox"
        if event.manual:
            hint = tr("{layout} layout · {confirm} Confirm · {cancel} Cancel", layout=layout, confirm=confirm, cancel=cancel)
        else:
            hint = tr("{layout} layout · Auto selection", layout=layout)
        self.hints.setText(hint)
        from app_core.events import Toast
        seconds = event.auto_close_seconds
        if seconds is None and event.auto_close_ms is not None:
            seconds = event.auto_close_ms / 1000
        self.present(Toast(event.message, event.kind, event.title or "Select profile", seconds))
