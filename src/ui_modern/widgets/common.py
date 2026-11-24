"""Shared Windows Settings-style building blocks for modern pages."""
from ..i18n import tr
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout
from PySide6.QtSvgWidgets import QSvgWidget
from qfluentwidgets import (SettingCard, StrongBodyLabel, SegmentedWidget, PushButton,
    FluentIcon, SimpleExpandGroupSettingCard, ToolTipFilter, InfoBar, InfoBarIcon, InfoBarPosition)
from .display import setting_title
from pathlib import Path


class SvgImage(QSvgWidget):
    """Original vector art (same palette as the classic UI), scaled without distortion."""
    def __init__(self, path=None, parent=None):
        super().__init__(parent)
        self.path = None
        if path:
            self.load(path)

    def load(self, path):
        self.path = Path(path)
        super().load(str(self.path))
        self.renderer().setAspectRatioMode(Qt.KeepAspectRatio)


class FitImage(QWidget):
    """Raster art scaled to fit and centred, keeping its aspect ratio."""
    def __init__(self, path, parent=None):
        super().__init__(parent)
        self.pixmap = QPixmap(str(path))

    def paintEvent(self, event):
        scaled = self.pixmap.size().scaled(self.size(), Qt.KeepAspectRatio)
        x, y = (self.width() - scaled.width()) // 2, (self.height() - scaled.height()) // 2
        painter = QPainter(self)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        painter.drawPixmap(x, y, scaled.width(), scaled.height(), self.pixmap)


class SettingRow(SettingCard):
    def __init__(self, title, caption=None, control=None, parent=None):
        self.helper_text = tr(caption or "")
        super().__init__(QIcon(), setting_title(title), self.helper_text, parent)
        self.iconLabel.hide()
        self.hBoxLayout.takeAt(1)  # remove the spacer reserved for a leading icon
        self.hBoxLayout.takeAt(self.hBoxLayout.count() - 1)  # give spare width to the text column
        self.hBoxLayout.setStretch(self.hBoxLayout.indexOf(self.vBoxLayout), 1)
        self.hBoxLayout.setContentsMargins(16, 8, 16, 8)
        self.setMaximumHeight(16777215)
        self.setMinimumHeight(64 if caption else 48)
        self.textLayout = self.vBoxLayout
        self.captionLabel = self.contentLabel
        self.titleLabel.setWordWrap(True)
        self.captionLabel.setWordWrap(True)
        self.textLayout.setSpacing(2)
        self.textLayout.setAlignment(self.captionLabel, Qt.AlignVCenter)
        self.titleLabel.setToolTip(setting_title(title))
        self.titleLabel.installEventFilter(ToolTipFilter(self.titleLabel, showDelay=350))
        if control is not None and hasattr(control, "setOnText"):
            control.setOnText(""); control.setOffText("")
            control.setFocusPolicy(Qt.StrongFocus)
        self.control = control
        if control is not None:
            if not control.accessibleName() and not isinstance(control, PushButton):
                control.setAccessibleName(self.titleLabel.text())
            if isinstance(control, PushButton):
                control.setMinimumWidth(max(control.minimumWidth(), 96))
            self.hBoxLayout.addWidget(control, 0, Qt.AlignVCenter)
            for widget in (control, *control.findChildren(QWidget)):
                widget.installEventFilter(ToolTipFilter(widget, showDelay=350))


class WarningSettingRow(InfoBar):
    """Persistent inline warning with stacked text and a centred right action."""
    def __init__(self, control, parent=None):
        super().__init__(InfoBarIcon.WARNING, tr("Driver needs attention"), tr("Virtual output is unavailable."),
                         orient=Qt.Vertical, isClosable=False, duration=-1,
                         position=InfoBarPosition.NONE, parent=parent)
        self.setMinimumHeight(64)
        self.textLayout.removeItem(self.widgetLayout)
        self.hBoxLayout.insertLayout(2, self.widgetLayout)
        self.hBoxLayout.setStretch(1, 1)
        self.hBoxLayout.setAlignment(self.iconWidget, Qt.AlignVCenter)
        self.widgetLayout.addWidget(control, 0, Qt.AlignVCenter)
        for label, size in ((self.titleLabel, 14), (self.contentLabel, 12)):
            font = label.font(); font.setPixelSize(size); label.setFont(font)
            label.setWordWrap(True)
        self.hBoxLayout.setContentsMargins(8, 4, 16, 4)

    def _adjustText(self):
        self.titleLabel.setText(self.title)
        self.contentLabel.setText(self.content)
        self.updateGeometry()

    def setMessage(self, title, caption):
        self.title, self.content = title, caption
        self._adjustText()


class SettingsGroup(QWidget):
    def __init__(self, title, parent=None):
        super().__init__(parent)
        outer = QVBoxLayout(self); outer.setContentsMargins(0, 0, 0, 0); outer.setSpacing(8)
        self.box = QVBoxLayout(); self.box.setContentsMargins(0, 0, 0, 0); self.box.setSpacing(4)
        self.heading = QHBoxLayout()
        self.label = StrongBodyLabel(tr(title), self)
        self.heading.addWidget(self.label); self.heading.addStretch()
        outer.addLayout(self.heading); outer.addLayout(self.box)

    def addRow(self, row):
        self.box.addWidget(row)
        return row

    def row(self, title, control, helper=None):
        return self.addRow(SettingRow(title, helper, control, self))


class ExpandSettingRow(SimpleExpandGroupSettingCard):
    """Fluent disclosure card that follows conditional settings in its body."""
    def __init__(self, title, caption=None, parent=None, *, icon=FluentIcon.SETTING):
        super().__init__(icon, setting_title(title), tr(caption or ""), parent)
        self.card.expandButton.setAccessibleName(self.card.titleLabel.text())
        self.card.expandButton.setFocusPolicy(Qt.StrongFocus)
        self.view.installEventFilter(self)

    def add_body_widget(self, widget):
        self.viewLayout.addWidget(widget)

    def eventFilter(self, watched, event):
        if (event.type() == QEvent.LayoutRequest and watched is getattr(self, "view", None)
                and self.isExpand and self.expandAni.state() != self.expandAni.State.Running):
            self._adjustViewSize()
        return super().eventFilter(watched, event)


# Compatibility for existing page callers.
Group = SettingsGroup

class Segments(SegmentedWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.options = ()
    def sync(self, options, value, labels=None):
        options = tuple(options)
        self.blockSignals(True)
        if options != self.options:
            self.clear()
            for key in options:
                self.addItem(key, setting_title((labels or {}).get(key, key)))
            self.options = options
        if value in options:
            self.setCurrentItem(value)
        self.blockSignals(False)
