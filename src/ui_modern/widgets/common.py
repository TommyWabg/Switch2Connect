"""Shared Windows Settings-style building blocks for modern pages."""
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QPainter, QPixmap
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout
from PySide6.QtSvgWidgets import QSvgWidget
from qfluentwidgets import (CardWidget, BodyLabel, CaptionLabel, SegmentedWidget,
    FluentIcon, ToolButton, ToolTipFilter, InfoBar, InfoBarIcon, InfoBarPosition)
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


class SettingRow(CardWidget):
    def __init__(self, title, caption=None, control=None, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(64 if caption else 48)
        self.rowLayout = QHBoxLayout(self)
        self.rowLayout.setContentsMargins(16, 10, 16, 10)
        self.rowLayout.setSpacing(16)
        text = self.textLayout = QVBoxLayout(); text.setSpacing(2); text.setContentsMargins(0, 0, 0, 0)
        self.titleLabel = BodyLabel(setting_title(title), self)
        font = self.titleLabel.font(); font.setPixelSize(14); self.titleLabel.setFont(font)
        text.addWidget(self.titleLabel)
        self.helper_text = caption or ""
        self.captionLabel = CaptionLabel(caption or "", self)
        font = self.captionLabel.font(); font.setPixelSize(12); self.captionLabel.setFont(font)
        self.captionLabel.setTextColor("#606060", "#bfbfbf")
        self.captionLabel.setWordWrap(True); self.captionLabel.setVisible(bool(caption))
        text.addWidget(self.captionLabel)
        self.titleLabel.setToolTip(title)
        self.titleLabel.installEventFilter(ToolTipFilter(self.titleLabel, showDelay=350))
        self.rowLayout.addLayout(text, 1)
        if control is not None and hasattr(control, "setOnText"):
            control.setOnText(""); control.setOffText("")
            control.setFocusPolicy(Qt.StrongFocus)
        self.control = control
        if control is not None:
            self.rowLayout.addWidget(control, 0, Qt.AlignVCenter)
            for widget in (control, *control.findChildren(QWidget)):
                widget.installEventFilter(ToolTipFilter(widget, showDelay=350))


class WarningSettingRow(InfoBar):
    """Persistent inline warning with stacked text and a centred right action."""
    def __init__(self, control, parent=None):
        super().__init__(InfoBarIcon.WARNING, "Driver needs attention", "Virtual output is unavailable.",
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
        self.label = CaptionLabel(title, self)
        font = self.label.font(); font.setPixelSize(14); font.setWeight(QFont.DemiBold)
        self.label.setFont(font)
        self.label.setTextColor("#606060", "#bfbfbf")
        self.heading.addWidget(self.label); self.heading.addStretch()
        outer.addLayout(self.heading); outer.addLayout(self.box)

    def addRow(self, row):
        self.box.addWidget(row)
        return row

    def row(self, title, control, helper=None):
        return self.addRow(SettingRow(title, helper, control, self))


class ExpandSettingRow(QWidget):
    """Closed-by-default setting row with a persistent disclosure body."""
    def __init__(self, title, caption=None, control=None, parent=None):
        super().__init__(parent)
        box = QVBoxLayout(self); box.setContentsMargins(0, 0, 0, 0); box.setSpacing(4)
        self.row = SettingRow(title, caption, control, self)
        self.toggle = ToolButton(FluentIcon.CHEVRON_DOWN_MED, self.row)
        self.toggle.setAccessibleName(f"Expand {title}")
        self.row.rowLayout.addWidget(self.toggle, 0, Qt.AlignVCenter)
        self.body = QWidget(self); self.viewLayout = QVBoxLayout(self.body)
        self.viewLayout.setContentsMargins(16, 8, 16, 8)
        box.addWidget(self.row); box.addWidget(self.body); self.body.hide()
        self.isExpand = False
        self.toggle.clicked.connect(lambda: self.setExpand(not self.isExpand))

    def setExpand(self, expanded):
        self.isExpand = bool(expanded); self.body.setVisible(self.isExpand)
        self.toggle.setIcon(FluentIcon.UP if self.isExpand else FluentIcon.CHEVRON_DOWN_MED)
        self.toggle.setAccessibleName(("Collapse " if self.isExpand else "Expand ") + self.row.titleLabel.text())

    def addWidget(self, widget):
        self.viewLayout.addWidget(widget)


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
