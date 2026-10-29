from PySide6.QtCore import Qt, QEvent, QPoint, Signal
from PySide6.QtGui import QFont, QColor
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QGridLayout,
                             QWidget, QDialogButtonBox, QListWidgetItem)
from qfluentwidgets import (PushButton, SearchLineEdit, ListWidget, FluentStyleSheet,
                            BodyLabel, TransparentTogglePushButton, TransparentPushButton,
                            HorizontalSeparator, FluentIcon)
from qfluentwidgets.components.widgets.flyout import (
    Flyout, FlyoutViewBase, FlyoutAnimationType, FlyoutAnimationManager)
from .display import input_display, setting_title


class OptionPicker(FlyoutViewBase):
    """Searchable catalogue with the classic column-based category bands."""
    chosen = Signal(object)
    closed = Signal()

    def __init__(self, snapshot, choose=None, record=None, parent=None):
        super().__init__(parent)
        self.setObjectName("mappingPicker")
        self.snapshot, self.choose, self.record = snapshot, choose, record
        self.options = tuple(getattr(snapshot, "options", snapshot))
        self.selected_option = getattr(snapshot, "selected_option", None)
        self.joystick = getattr(snapshot, "key", None) in ("l_joystick", "r_joystick")
        self.value = None
        self.content = QVBoxLayout(self)
        self.content.setContentsMargins(12, 12, 12, 8)
        self.content.setSpacing(5)
        self.search = SearchLineEdit(self)
        self.search.setPlaceholderText("Search mappings")
        self.search.installEventFilter(self)
        self.content.addWidget(self.search)
        self.buttons, self.blocks = {}, {}
        self._entries = []
        font = QFont(self.font())
        font.setPixelSize(12)
        known = (("General", "In-app Gyro", "PS Input", "Windows", "Mouse Click"),
                 ("Switch Input", "Media Keys"))
        titles = {title for band in known for title in band}
        bands = [[cat for title in band for cat in self.options if cat.title == title]
                 for band in known]
        bands.append([cat for cat in self.options if cat.title not in titles])
        for categories in bands:
            if not categories:
                continue
            band_widget = QWidget(self)
            band = QHBoxLayout(band_widget)
            band.setContentsMargins(0, 0, 0, 0)
            band.setSpacing(5)
            for cat in categories:
                block = QWidget(band_widget)
                layout = QVBoxLayout(block)
                layout.setContentsMargins(0, 0, 0, 0)
                layout.setSpacing(5)
                header = BodyLabel(setting_title(cat.title), block)
                bold = QFont(font)
                bold.setBold(True)
                header.setFont(bold)
                layout.addWidget(header)
                grid = QGridLayout()
                grid.setSpacing(5)
                layout.addLayout(grid)
                entries = []
                for column, options in enumerate(cat.rows):
                    for row, option in enumerate(options):
                        button = TransparentTogglePushButton(input_display(option.label), block)
                        button.setFont(font)
                        button.setToolTip(str(option.label))
                        button.setFixedHeight(28)
                        button.setChecked(option.value == self.selected_option)
                        button.clicked.connect(lambda checked=False, value=option.value: self.select(value))
                        button.installEventFilter(self)
                        grid.addWidget(button, row, column)
                        self.buttons[option.value] = button
                        entries.append((option, button))
                self._entries.append((header, entries))
                self.blocks[cat.title] = block
                band.addWidget(block, 0, Qt.AlignTop)
            band.addStretch()
            self.content.addWidget(band_widget)
        width = max((button.fontMetrics().horizontalAdvance(button.text()) + 24
                     for button in self.buttons.values()), default=120)
        for button in self.buttons.values():
            button.setFixedWidth(width)
        self.content.addWidget(HorizontalSeparator(self))
        # Callers append activation/configuration actions to this shared footer.
        self.box = QHBoxLayout()
        self.box.setSpacing(8)
        self.content.addLayout(self.box)
        self.record_button = TransparentPushButton(FluentIcon.EDIT, "Record keys…", self)
        self.record_button.setVisible(not self.joystick)
        self.box.addWidget(self.record_button)
        self.box.addStretch()
        self.record_button.clicked.connect(self.open_record)
        self.search.textChanged.connect(self.filter)
        self.filter()
        self.installEventFilter(self)

    def visible_buttons(self):
        return [button for _, entries in self._entries for _, button in entries
                if button.isEnabled() and not button.isHidden()]

    def filter(self, *_):
        query = self.search.text().casefold()
        for header, entries in self._entries:
            for option, button in entries:
                button.setEnabled(query in (str(option.label) + " " + str(option.value)).casefold())
            enabled = any(button.isEnabled() for _, button in entries)
            header.setEnabled(enabled)
            header.setTextColor(QColor(0, 0, 0, 255 if enabled else 92),
                                QColor(255, 255, 255, 255 if enabled else 92))

    def show_at(self, target):
        if hasattr(self, "flyout"):
            self.flyout.close()
            self.flyout.deleteLater()
        self.value = None
        self.flyout = Flyout(self, target.window(), isDeleteOnClose=False)
        self.flyout.closed.connect(self.closed)
        self.flyout.installEventFilter(self)
        self.flyout.adjustSize()
        self.flyout.setFixedSize(self.flyout.sizeHint())
        margins = self.flyout.layout().contentsMargins()
        area = target.screen().availableGeometry()
        anchor = target.mapToGlobal(QPoint())
        x = anchor.x() - margins.left()
        y = anchor.y() + target.height() - margins.top()
        animation = FlyoutAnimationType.DROP_DOWN
        if y + self.flyout.height() > area.bottom() + 1:
            y = anchor.y() - self.flyout.height() + margins.bottom()
            animation = FlyoutAnimationType.PULL_UP
        pos = QPoint(max(area.left(), min(x, area.right() + 1 - self.flyout.width())),
                     max(area.top(), min(y, area.bottom() + 1 - self.flyout.height())))
        self.flyout.exec(pos, animation)
        # Native managers clamp to the cursor's screen and add a teaching-tip offset.
        # Keep their timing/easing, but anchor the card to this button's screen.
        manager: FlyoutAnimationManager = self.flyout.aniManager
        offset = QPoint(0, -8 if animation == FlyoutAnimationType.DROP_DOWN else 8)
        manager.slideAni.setStartValue(QPoint(pos.x(), max(area.top(), min(
            pos.y() + offset.y(), area.bottom() + 1 - self.flyout.height()))))
        manager.slideAni.setEndValue(pos)
        self.search.setFocus()
        return self.flyout

    def dismiss(self):
        self.flyout.aniManager.aniGroup.stop()
        self.flyout.fadeOut()

    def eventFilter(self, watched, event):
        if event.type() == QEvent.KeyPress:
            key = event.key()
            buttons = self.visible_buttons()
            if key == Qt.Key_Escape:
                self.dismiss()
                return True
            if watched is self.search:
                if key in (Qt.Key_Return, Qt.Key_Enter):
                    if buttons:
                        buttons[0].click()
                    return True
                if key == Qt.Key_Down:
                    if buttons:
                        buttons[0].setFocus()
                    return True
            elif watched in buttons:
                if key in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
                    watched.click()
                    return True
                directions = {Qt.Key_Left: (-1, 0), Qt.Key_Right: (1, 0),
                              Qt.Key_Up: (0, -1), Qt.Key_Down: (0, 1)}
                if key in directions:
                    dx, dy = directions[key]
                    origin = watched.mapTo(self, watched.rect().center())
                    candidates = []
                    for button in buttons:
                        delta = button.mapTo(self, button.rect().center()) - origin
                        if delta.x() * dx + delta.y() * dy > 0:
                            candidates.append((delta.x() ** 2 + delta.y() ** 2, button))
                    if candidates:
                        min(candidates, key=lambda item: item[0])[1].setFocus()
                    return True
        return super().eventFilter(watched, event)

    def select(self, value):
        self.value = value
        self.dismiss()
        self.chosen.emit(value)
        if value == "Custom" and self.record and not self.joystick:
            self.record()
        elif self.choose:
            self.choose(value)

    def open_record(self):
        if not self.joystick:
            self.select("Custom")


class InputPicker(QDialog):
    def __init__(self, options, selected=(), parent=None):
        super().__init__(parent)
        self.setWindowTitle("Switch input")
        self.resize(360, 440)
        self.setWindowModality(Qt.WindowModal)
        self.setObjectName("switchInputPicker")
        FluentStyleSheet.DIALOG.apply(self)
        self.options = tuple(getattr(o, "value", o) for o in options)
        self.value = list(selected) if isinstance(selected, (tuple, list)) else []
        box = QVBoxLayout(self)
        self.listing = ListWidget(self)
        box.addWidget(self.listing)
        for option in options:
            value = getattr(option, "value", option)
            item = QListWidgetItem(input_display(getattr(option, "label", option)), self.listing)
            item.setData(Qt.UserRole, value)
            item.setToolTip(item.text())
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if value in self.value else Qt.Unchecked)
        clear = PushButton("Clear", self); box.addWidget(clear)
        clear.clicked.connect(lambda: [self.listing.item(i).setCheckState(Qt.Unchecked) for i in range(self.listing.count())])
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel, self)
        box.addWidget(buttons); buttons.accepted.connect(self.save); buttons.rejected.connect(self.reject)

    def save(self):
        self.value = [self.listing.item(i).data(Qt.UserRole) for i in range(self.listing.count())
                      if self.listing.item(i).checkState() == Qt.Checked]
        self.accept()


class SwitchInputPanel(QDialog):
    """Ordered selection body; the owner decides when to persist changes."""
    def __init__(self, options, parent=None):
        from PySide6.QtWidgets import QGridLayout
        from qfluentwidgets import CheckBox
        super().__init__(parent)
        self.setWindowFlags(Qt.Widget)
        grid = QGridLayout(self); grid.setContentsMargins(0, 0, 0, 0)
        self.checks = {}
        for index, option in enumerate(options):
            value = getattr(option, "value", option)
            checkbox = CheckBox(input_display(getattr(option, "label", option)), self)
            grid.addWidget(checkbox, index // 7, index % 7)
            self.checks[value] = checkbox
