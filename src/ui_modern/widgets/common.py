"""Shared Windows Settings-style building blocks for modern pages."""
from ..i18n import tr
from PySide6.QtCore import Property, QEasingCurve, QEvent, QPropertyAnimation, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QFrame, QWidget, QVBoxLayout, QHBoxLayout
from PySide6.QtSvgWidgets import QSvgWidget
from qfluentwidgets import (SettingCard, StrongBodyLabel, SegmentedWidget, PushButton,
    FluentIcon, FluentStyleSheet, ToolTipFilter, InfoBar, InfoBarIcon, InfoBarPosition)
from qfluentwidgets.components.settings.expand_setting_card import (
    ExpandBorderWidget, GroupSeparator, GroupWidget, HeaderSettingCard)
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
        # The scroll area can relinquish space a layout pass after a row shrinks.
        # Put that space below the rows so their geometry stays stable.
        outer.addStretch()

    def addRow(self, row):
        self.box.addWidget(row)
        return row

    def row(self, title, control, helper=None):
        return self.addRow(SettingRow(title, helper, control, self))


class ExpandSettingRow(QWidget):
    """Keep the body at its natural size and reveal it by clipping the card."""
    animatedHeight = Property(int, QWidget.height, QWidget.setFixedHeight)

    def __init__(self, title, caption=None, parent=None, *, icon=FluentIcon.SETTING):
        super().__init__(parent)
        self.isExpand = False
        self.card = HeaderSettingCard(icon, setting_title(title), tr(caption or ""), self)
        self.card.expandButton.setAccessibleName(self.card.titleLabel.text())
        self.card.expandButton.setFocusPolicy(Qt.StrongFocus)
        self.card.expandButton.clicked.connect(lambda: self.setExpand(not self.isExpand))
        self.view = QFrame(self)
        self.view.setObjectName("view")
        self.viewLayout = QVBoxLayout(self.view)
        self.viewLayout.setContentsMargins(0, 0, 0, 0)
        self.viewLayout.setSpacing(0)
        self.view.hide()
        self.border = ExpandBorderWidget(self)
        FluentStyleSheet.EXPAND_SETTING_CARD.apply(self)
        self.setFixedHeight(self.card.height())
        self._animation = QPropertyAnimation(self, b"animatedHeight", self)
        self._animation.setDuration(200)
        self._animation.setEasingCurve(QEasingCurve.OutQuad)
        self._animation.finished.connect(lambda: self.view.setVisible(self.isExpand))
        self.view.installEventFilter(self)

    def add_body_widget(self, widget):
        self.viewLayout.addWidget(widget)

    def add_setting(self, title, control):
        """Return a single visibility handle for a setting and its divider."""
        row = QWidget(self.view)
        layout = QVBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        if self.viewLayout.count():
            layout.addWidget(GroupSeparator(row))
        layout.addWidget(GroupWidget(QIcon(), tr(title), "", control, parent=row))
        self.add_body_widget(row)
        return row

    def setTitle(self, title):
        self.card.setTitle(title)
        self.card.expandButton.setAccessibleName(title)

    def setExpand(self, expanded):
        if self.isExpand == expanded:
            return
        self.isExpand = expanded
        if expanded:
            self.view.show()
        elif self.view.isAncestorOf(QApplication.focusWidget()):
            self.card.expandButton.setFocus()
        self.card.expandButton.setExpand(expanded)
        self.card.update()
        self._sync_height(animate=True)

    def _sync_height(self, animate):
        header = self.card.height()
        height = (self.viewLayout.totalHeightForWidth(self.width()) if self.viewLayout.hasHeightForWidth()
                  else self.viewLayout.sizeHint().height())
        self.view.setGeometry(0, header, self.width(), height)
        target = header + height if self.isExpand else header
        running = self._animation.state() == QPropertyAnimation.Running
        if running and target == self._animation.endValue():
            return
        self._animation.stop()
        if (animate or running) and self.height() != target:
            self._animation.setStartValue(self.height())
            self._animation.setEndValue(target)
            self._animation.start()
        else:
            self.setFixedHeight(target)
            self.view.setVisible(self.isExpand)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if event.size().width() != event.oldSize().width():
            self.card.resize(self.width(), self.card.height())
            self._sync_height(animate=False)

    def eventFilter(self, watched, event):
        if event.type() == QEvent.LayoutRequest and watched is self.view:
            self._sync_height(animate=False)
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
