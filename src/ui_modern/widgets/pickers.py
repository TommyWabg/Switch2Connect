from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QVBoxLayout, QDialogButtonBox, QListWidgetItem
from qfluentwidgets import PushButton, SearchLineEdit, ComboBox, ListWidget, FluentStyleSheet
from .display import input_display, setting_title

class OptionPicker(QDialog):
    """Catalogue-backed, searchable picker; arrow keys + Enter select checked items."""
    def __init__(self, snapshot, choose=None, record=None, parent=None):
        super().__init__(parent, Qt.Popup)
        self.setObjectName("mappingPicker")
        FluentStyleSheet.DIALOG.apply(self)
        self.setWindowTitle("Choose mapping")
        self.resize(480, 460)
        self.snapshot, self.choose, self.record = snapshot, choose, record
        self.options = getattr(snapshot, "options", snapshot)
        self.selected_option = getattr(snapshot, "selected_option", None)
        self.value = None
        self.box = QVBoxLayout(self)
        self.search = SearchLineEdit(self)
        self.search.setPlaceholderText("Search mappings")
        self.categories = ComboBox(self)
        self.categories.addItem("All categories")
        self.categories.addItems([setting_title(cat.title) for cat in self.options])
        self.items = ListWidget(self)
        self.items.setAccessibleName("Mapping options")
        self.record_button = PushButton("Record keys…", self)
        for w in (self.search, self.categories, self.items, self.record_button):
            self.box.addWidget(w)
        self.search.textChanged.connect(self.filter)
        self.categories.currentIndexChanged.connect(self.filter)
        self.items.itemActivated.connect(self.select)
        self.items.itemClicked.connect(self.select)
        self.record_button.clicked.connect(self.open_record)
        self.filter()
        self.search.setFocus()

    def filter(self, *_):
        self.items.clear()
        query = self.search.text().casefold()
        current = None
        for index, cat in enumerate(self.options, 1):
            if self.categories.currentIndex() not in (0, index):
                continue
            for row in cat.rows:
                for option in row:
                    if query not in (str(option.label) + " " + str(option.value)).casefold():
                        continue
                    item = QListWidgetItem(f"{setting_title(cat.title)} · {input_display(option.label)}")
                    item.setData(Qt.UserRole, option.value)
                    item.setToolTip(input_display(option.label))
                    item.setCheckState(Qt.Checked if option.value == self.selected_option else Qt.Unchecked)
                    item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                    self.items.addItem(item)
                    if option.value == self.selected_option:
                        current = item
        if current:
            self.items.setCurrentItem(current)
        elif self.items.count():
            self.items.setCurrentRow(0)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter) and self.items.currentItem():
            self.select(self.items.currentItem())
        elif event.key() == Qt.Key_Down and self.search.hasFocus():
            self.items.setFocus()
        else:
            super().keyPressEvent(event)

    def select(self, item):
        value = item.data(Qt.UserRole)
        self.value = value
        self.accept()
        if value == "Custom" and self.record and self.snapshot.key not in ("l_joystick", "r_joystick"):
            self.record()
        elif self.choose:
            self.choose(value)

    def open_record(self):
        self.value = "Custom"
        self.accept()
        if self.record:
            self.record()


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
