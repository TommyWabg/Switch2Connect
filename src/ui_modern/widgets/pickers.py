from ..i18n import tr
from PySide6.QtCore import Qt, QEvent, QPoint, Signal
from PySide6.QtGui import QAccessible, QAccessibleTableModelChangeEvent
from PySide6.QtWidgets import (QDialogButtonBox, QVBoxLayout, QHBoxLayout, QGridLayout, QTreeWidgetItem,
                             QWidget, QListWidgetItem)
from qfluentwidgets import (PushButton, SearchLineEdit, ListWidget, PrimaryPushButton, SubtitleLabel,
                            BodyLabel, TreeWidget, TransparentPushButton,
                            HorizontalSeparator, FluentIcon)
from qfluentwidgets.components.widgets.flyout import (
    Flyout, FlyoutViewBase, FlyoutAnimationType, FlyoutAnimationManager)
from .support import ThemedDialog
from .display import input_display, setting_title


class OptionPicker(FlyoutViewBase):
    """Searchable mapping catalogue shared by controller and IR selectors."""
    chosen = Signal(object)
    closed = Signal()

    def __init__(self, snapshot, choose=None, record=None, parent=None):
        super().__init__(parent)
        self.setObjectName("mappingPicker")
        self.snapshot, self.choose, self.record = snapshot, choose, record
        self.options = tuple(getattr(snapshot, "options", snapshot))
        self.selected_option = getattr(snapshot, "selected_option", getattr(snapshot, "function", None))
        self.joystick = getattr(snapshot, "key", None) in ("l_joystick", "r_joystick")
        self.value = None
        self.content = QVBoxLayout(self)
        self.content.setContentsMargins(12, 12, 12, 8)
        self.content.setSpacing(8)
        self.search = SearchLineEdit(self)
        self.search.setPlaceholderText(tr("Search mappings"))
        self.search.installEventFilter(self)
        self.content.addWidget(self.search)
        self.tree = TreeWidget(self)
        self.tree.setHeaderHidden(True)
        self.tree.setAccessibleName(tr("Search mappings"))
        self.tree.setMinimumWidth(280)
        self.tree.installEventFilter(self)
        self.content.addWidget(self.tree, 1)
        self.empty = BodyLabel(tr("No matching mappings"), self)
        self.empty.setAlignment(Qt.AlignCenter)
        self.content.addWidget(self.empty, 1)
        self.items = {}
        for category in self.options:
            group = QTreeWidgetItem(self.tree, [setting_title(category.title)])
            group.setFlags(group.flags() & ~Qt.ItemIsSelectable)
            for column in category.rows:
                for option in column:
                    label = input_display(option.label)
                    item = QTreeWidgetItem(group, [label])
                    item.setData(0, Qt.UserRole, option.value)
                    item.setData(0, Qt.UserRole + 1, f"{label} {option.label} {option.value}".casefold())
                    self.items[option.value] = item
                    if option.value == self.selected_option:
                        item.setIcon(0, FluentIcon.ACCEPT.icon())
            group.setExpanded(True)
        self.tree.itemClicked.connect(self.choose_item)
        self.tree.expanded.connect(self.refresh_accessible_rows)
        self.tree.collapsed.connect(self.refresh_accessible_rows)
        self.content.addWidget(HorizontalSeparator(self))
        # Callers append activation/configuration actions to this shared footer.
        self.box = QHBoxLayout()
        self.box.setSpacing(8)
        self.content.addLayout(self.box)
        self.record_button = TransparentPushButton(FluentIcon.EDIT, tr("Record keys…"), self)
        self.record_button.setVisible(not self.joystick)
        self.box.addWidget(self.record_button)
        self.box.addStretch()
        self.record_button.clicked.connect(self.open_record)
        self.search.textChanged.connect(self.filter)
        self.filter()
        self.installEventFilter(self)

    def choose_item(self, item):
        if item is not None and item.parent() is not None and not item.isHidden():
            self.select(item.data(0, Qt.UserRole))

    def refresh_accessible_rows(self):
        # Qt caches tree accessibility children by visible row number. Hiding
        # rows can otherwise make Windows UI Automation loop over old siblings.
        if QAccessible.isActive():
            event = QAccessibleTableModelChangeEvent(
                self.tree, QAccessibleTableModelChangeEvent.ModelChangeType.ModelReset)
            QAccessible.updateAccessibility(event)

    def filter(self, *_):
        query = self.search.text().strip().casefold()
        first = None
        for index in range(self.tree.topLevelItemCount()):
            group = self.tree.topLevelItem(index)
            count = 0
            for row in range(group.childCount()):
                item = group.child(row)
                matches = query in item.data(0, Qt.UserRole + 1)
                item.setHidden(not matches)
                if matches:
                    first = first or item
                    count += 1
            group.setHidden(not count)
            if count:
                group.setExpanded(True)
        self.refresh_accessible_rows()
        self.tree.setVisible(first is not None)
        self.empty.setVisible(first is None)
        selected = self.items.get(self.selected_option)
        self.tree.setCurrentItem(selected if selected and not selected.isHidden() else first)

    def show_at(self, target):
        if hasattr(self, "flyout"):
            self.flyout.close()
            self.flyout.deleteLater()
        self.value = None
        self.flyout = Flyout(self, target.window(), isDeleteOnClose=False)
        self.flyout.closed.connect(self.closed)
        self.flyout.installEventFilter(self)
        self.flyout.adjustSize()
        area = target.screen().availableGeometry()
        self.flyout.setFixedSize(min(area.width(), max(380, self.flyout.sizeHint().width())),
                                 min(area.height(), 440))
        margins = self.flyout.layout().contentsMargins()
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
            if key == Qt.Key_Escape:
                self.dismiss()
                return True
            if watched is self.search and key == Qt.Key_Down:
                if self.tree.currentItem():
                    self.tree.setFocus()
                return True
            if watched in (self.tree, self.search) and key in (Qt.Key_Return, Qt.Key_Enter):
                current = self.tree.currentItem()
                if current is not None and current.parent() is None:
                    current.setExpanded(not current.isExpanded())
                else:
                    self.choose_item(current)
                return True
            if watched is self.tree and key == Qt.Key_Space:
                self.choose_item(self.tree.currentItem())
                return True
        return super().eventFilter(watched, event)

    def select(self, value):
        if self.value is not None:
            return
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


class InputPicker(ThemedDialog):
    def __init__(self, options, selected=(), parent=None):
        super().__init__(parent)
        self.setWindowTitle(tr("Switch input"))
        self.resize(360, 440)
        self.setObjectName("switchInputPicker")
        self.options = tuple(getattr(o, "value", o) for o in options)
        self.value = list(selected) if isinstance(selected, (tuple, list)) else []
        box = QVBoxLayout(self)
        box.setContentsMargins(24, 20, 24, 20)
        box.setSpacing(12)
        box.addWidget(SubtitleLabel(self.windowTitle(), self))
        self.listing = ListWidget(self)
        box.addWidget(self.listing)
        for option in options:
            value = getattr(option, "value", option)
            item = QListWidgetItem(input_display(getattr(option, "label", option)), self.listing)
            item.setData(Qt.UserRole, value)
            item.setToolTip(item.text())
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if value in self.value else Qt.Unchecked)
        clear = TransparentPushButton(tr("Clear"), self)
        clear.clicked.connect(lambda: [self.listing.item(i).setCheckState(Qt.Unchecked) for i in range(self.listing.count())])
        buttons = QDialogButtonBox(self)
        buttons.addButton(clear, QDialogButtonBox.ResetRole)
        self.yesButton = PrimaryPushButton(tr("Save"), self)
        buttons.addButton(self.yesButton, QDialogButtonBox.AcceptRole)
        buttons.addButton(PushButton(tr("Cancel"), self), QDialogButtonBox.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        box.addWidget(buttons)
        self.yesButton.setDefault(True)
        self.listing.setFocus()

    def accept(self):
        self.value = [self.listing.item(i).data(Qt.UserRole) for i in range(self.listing.count())
                      if self.listing.item(i).checkState() == Qt.Checked]
        super().accept()


class SwitchInputPanel(QWidget):
    """Ordered selection body; the owner decides when to persist changes."""
    def __init__(self, options, parent=None):
        from qfluentwidgets import CheckBox
        super().__init__(parent)
        grid = QGridLayout(self); grid.setContentsMargins(0, 0, 0, 0)
        self.checks = {}
        for index, option in enumerate(options):
            value = getattr(option, "value", option)
            checkbox = CheckBox(input_display(getattr(option, "label", option)), self)
            grid.addWidget(checkbox, index // 7, index % 7)
            self.checks[value] = checkbox
