"""Mapping page and its public dialog entry points."""
from ...i18n import tr
from ...widgets.changes import apply_change
from ...widgets.support import FluentSurface
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QStackedWidget
from PySide6.QtGui import QAction
from ...widgets.controls import ComboBox
from qfluentwidgets import (ScrollArea, PushButton, TransparentToolButton, FluentIcon, CheckBox,
                            MessageBox, RoundMenu, SearchLineEdit, AdaptiveFlowLayout, SimpleCardWidget, BodyLabel)
from app_core import events
from ...widgets.display import input_display, setting_title
from ...widgets.common import SettingsGroup, Segments
from ...qt_bridge import BusBridge
from .rows import MappingRow, OptionPicker
from ...widgets.recording import RecordDialog
from .dialogs import JoystickDialog, DeadzoneDialog, ChangeProfileDialog, ActivationDialog

SHIFT_SCOPE = "in_app_gyro_mode_mappings"
GROUPS = (("shared", "Shared buttons & joysticks"),
          ("back", "Pro Controller back buttons"), ("rail", "Joy-Con rail buttons"),
          ("ir", "Joy-Con IR sensor"), ("gamecube", "GameCube"))
BANDS = (("zl", "l", "zr", "r"), ("minus", "plus", "capt", "home", "c"),
         ("l_joystick", "l_stk", "r_joystick", "r_stk"),
         ("a", "b", "x", "y"), ("up", "down", "left", "right"))


class MappingPage(ScrollArea):
    def __init__(self, core, toast, parent=None):
        super().__init__(parent)
        self.core, self.toast = core, toast
        self.setObjectName("mapping")
        self.setWidgetResizable(True)
        self.enableTransparentBackground()
        content = FluentSurface("Mapping", self); self.setWidget(content)
        content.setObjectName("mappingContent")
        self.content = content
        box = content.box
        header = QHBoxLayout()
        header.setSpacing(8)
        self.scope_control = Segments(self)
        self.scope_control.sync(("base", "shift"), "base", {"base": "Controller mapping", "shift": "Mode shift"})
        self.changed_only = CheckBox(tr("Show changed only"), self)
        self.search = SearchLineEdit(self); self.search.setPlaceholderText(tr("Search mappings"))
        self.search.setMinimumWidth(128); self.search.setMaximumWidth(210)
        header.addWidget(self.scope_control); header.addStretch(); header.addWidget(self.search); header.addWidget(self.changed_only)
        box.addLayout(header)
        self.scope = None; self.rows = {}; self.groups = {}; self.gc_modes = {}; self.ir_buttons = {}
        self.ir_rows = {}
        self.ir_defaults = {}
        self.stack = QStackedWidget(self); box.addWidget(self.stack)
        self.scope_control.currentItemChanged.connect(self.change_scope)
        self.changed_only.toggled.connect(self.filter_rows)
        self.search.textChanged.connect(self.filter_rows)
        self.cells = {}; self.empty_labels = {}
        self.shift_actions = QWidget(self); actions = QHBoxLayout(self.shift_actions); actions.setContentsMargins(0, 0, 0, 0)
        self.overflow = TransparentToolButton(FluentIcon.MORE, self.shift_actions)
        self.shift_menu = RoundMenu(parent=self.overflow)
        self.copy = QAction(tr("Copy from controller mapping"), self.shift_menu)
        self.reset_shift = QAction(tr("Reset mode shift mapping"), self.shift_menu)
        self.shift_menu.addActions([self.copy, self.reset_shift])
        self.overflow.setAccessibleName(tr("Mode shift actions"))
        self.overflow.setToolTip(tr("Mode shift actions"))
        actions.addWidget(self.overflow); header.addWidget(self.shift_actions)
        self.overflow.clicked.connect(self.show_shift_menu)
        self.copy.triggered.connect(lambda: self.confirm_shift(tr("Copy controller mapping?"), tr("This replaces the mode shift mappings with controller mappings."), core.settings.copy_controller_mapping_to_mode_shift))
        self.reset_shift.triggered.connect(lambda: self.confirm_shift(tr("Reset mode shift mapping?"), tr("This clears the mode shift mappings and simultaneous inputs."), core.settings.reset_mode_shift_mapping))
        for scope in (None, SHIFT_SCOPE):
            panel = QWidget(self); layout = QVBoxLayout(panel); layout.setContentsMargins(0, 0, 0, 0); layout.setSpacing(20)
            groups = {}
            flows = {}
            for key, title in GROUPS:
                group = SettingsGroup(title, panel); groups[key] = group; layout.addWidget(group)
                card = SimpleCardWidget(group)
                group.addRow(card)
                flow = AdaptiveFlowLayout(card, isTight=True)
                flow.setContentsMargins(16, 12, 16, 16)
                flow.setHorizontalSpacing(16)
                flow.setVerticalSpacing(12)
                flow.setWidgetMinimumWidth(152)
                flow.setWidgetMaximumWidth(264)
                flows[key] = flow
            self.groups[scope] = groups
            view = core.settings.mapping(scope)
            order = [key for band in BANDS for key in band] + ["gl", "gr", "sll", "srl", "slr", "srr", "gc_l_click", "gc_r_click"]
            for snapshot in sorted(view.rows, key=lambda s: order.index(s.key) if s.key in order else len(order)):
                row = MappingRow(self, snapshot, scope, panel)
                group_key = snapshot.group if snapshot.group in groups else "shared"
                flows[group_key].addWidget(row)
                self.rows[scope, snapshot.key] = row
                self.cells[scope, snapshot.key] = (row, group_key)
            def cell(key, title, control, group):
                widget = QWidget(panel)
                cell_box = QVBoxLayout(widget); cell_box.setContentsMargins(0, 0, 0, 0); cell_box.setSpacing(4)
                label = BodyLabel(tr(title), widget)
                control.setAccessibleName(label.text())
                cell_box.addWidget(label); cell_box.addWidget(control)
                control.setMinimumHeight(32)
                flows[group].addWidget(widget)
                self.cells[scope, key] = (widget, group)
                widget.search_title = title + ' ' + label.text(); widget.control = control
                return widget
            deadzones = PushButton(tr("Deadzones…"), self)
            cell("deadzones", "Deadzone settings", deadzones, "shared")
            deadzones.clicked.connect(lambda: self.show_dialog(DeadzoneDialog(core, toast, self.window())))
            self.gc_modes[scope] = ComboBox(self)
            cell("gc_mode", "Analog trigger 100%", self.gc_modes[scope], "gamecube")
            self.gc_modes[scope].currentIndexChanged.connect(lambda index, scope=scope: self.set_gc(self.gc_modes[scope].itemData(index), scope))
            calibrate = PushButton(tr("Calibrate…"), self)
            cell("gc_calibration", "Trigger calibration", calibrate, "gamecube")
            calibrate.clicked.connect(self.calibrate_gc)
            gc_flow = flows["gamecube"]
            gc_flow.removeAllWidgets()
            for key in ("gc_calibration", "gc_mode", "gc_l_click", "gc_r_click"):
                if (scope, key) in self.cells:
                    gc_flow.addWidget(self.cells[scope, key][0])
            for side in ("left", "right"):
                button = PushButton(tr("Configure…"), self)
                self.ir_rows[scope, side] = cell(side + "_ir", side.title() + " IR sensor", button, "ir")
                button.clicked.connect(lambda _, side=side, scope=scope: self.open_ir(side, scope))
                self.ir_buttons[scope, side] = button
            empty = BodyLabel(tr("No matching mappings"), panel)
            empty.setAlignment(Qt.AlignCenter)
            layout.addWidget(empty)
            self.empty_labels[scope] = empty
            layout.addStretch(); self.stack.addWidget(panel)
        self.shift_actions.hide()
        self.bridge = BusBridge(core.bus, self)
        self.bridge.event.connect(self.apply_event)
        self.destroyed.connect(lambda: self.bridge.close())
        self.sync_reads()

    def show_dialog(self, dialog):
        self.dialog = dialog; dialog.show()
        return dialog

    def change_scope(self, value):
        self.scope = SHIFT_SCOPE if value == "shift" else None
        self.stack.setCurrentIndex(1 if self.scope else 0); self.shift_actions.setVisible(self.scope is not None)

    def confirm_shift(self, title, text, fn):
        dialog = MessageBox(title, text, self.window())
        dialog.yesButton.setText(tr("Continue"))
        dialog.cancelButton.setText(tr("Cancel"))
        dialog.accepted.connect(lambda: apply_change(self, fn))
        self.show_dialog(dialog)

    def show_shift_menu(self):
        self.shift_menu.exec(self.overflow.mapToGlobal(self.overflow.rect().bottomLeft()))

    def set_gc(self, mode, scope):
        old = next(v.mode for v in self.core.settings.state().gc_triggers if v.scope == scope)
        fn = self.core.settings.set_gc_trigger_mode
        apply_change(self, fn, mode, scope, undo=lambda: apply_change(self, fn, old, scope))

    def configure_mapping(self, key, scope, value):
        if key in ("l_joystick", "r_joystick"):
            dialog = JoystickDialog(self.core, key, scope, value, self.toast, self.window())
        elif value == "Change Profile":
            dialog = ChangeProfileDialog(self.core, self.toast, self.window())
        else:
            dialog = ActivationDialog(self.core, key, self.toast, self.window())
        self.show_dialog(dialog)

    def open_ir(self, side, scope):
        from ..ir import IrSensorDialog
        return self.show_dialog(IrSensorDialog(side, scope, core=self.core, toast=self.toast, parent=self.window()))

    def calibrate_gc(self):
        from ...wizards.calibration import open_gc_trigger_wizard
        self.dialog = open_gc_trigger_wizard(self.core, parent=self.window())
        return self.dialog

    def sync_reads(self):
        self.apply_settings(self.core.settings.state())

    def apply_settings(self, state):
        self._settings_state = state
        for view in state.mappings:
            for snapshot in view.rows:
                if (view.scope, snapshot.key) in self.rows:
                    self.rows[view.scope, snapshot.key].update_snapshot(snapshot)
        for view in state.gc_triggers:
            combo = self.gc_modes[view.scope]; combo.blockSignals(True); combo.clear()
            for option in view.options:
                combo.addItem(setting_title(option.label), userData=option.value)
            combo.setCurrentIndex(next((i for i, o in enumerate(view.options) if o.value == view.mode), 0)); combo.blockSignals(False)
        for view in state.ir_sensors:
            button = self.ir_buttons[view.scope, view.side]
            display = input_display(view.display_text)
            button.setText(button.fontMetrics().elidedText(display, Qt.ElideRight, 145))
            button.setToolTip(display)
            self.ir_defaults[view.scope, view.side] = view.function == "Default"
        self.filter_rows()

    def filter_rows(self, *_):
        query = self.search.text().casefold().strip()
        for (scope, key), (cell, group) in self.cells.items():
            snapshot = getattr(cell, "snapshot", None)
            text = cell.titleLabel.text() + " " + snapshot.label + " " + input_display(snapshot.display_text) if snapshot else cell.search_title + " " + (cell.control.currentText() if isinstance(cell.control, ComboBox) else cell.control.toolTip() or cell.control.text())
            default = snapshot.is_default if snapshot else self.ir_defaults.get((scope, key.removesuffix("_ir")), False)
            cell.setVisible((not snapshot or snapshot.visible) and (not self.changed_only.isChecked() or not default) and query in text.casefold())
        for scope, groups in self.groups.items():
            for name, group in groups.items():
                group.setVisible(any(not cell.isHidden() for (s, _), (cell, g) in self.cells.items() if s == scope and g == name))
            self.empty_labels[scope].setVisible(all(group.isHidden() for group in groups.values()))

    def apply_event(self, topic, payload):
        if topic == events.SETTINGS_CHANGED:
            keys = getattr(payload, "keys", None)
            if keys is None or keys.intersection({"mapping", "joystick", "gc_trigger", "ir_sensor", "gyro", "in_app_gyro"}):
                self.sync_reads()


def create_page(core, toast, parent=None, **kwargs):
    """Lazy page factory used by MainWindow."""
    return MappingPage(core, toast, parent, **kwargs)


__all__ = ["MappingPage", "MappingRow", "OptionPicker", "RecordDialog", "JoystickDialog",
           "DeadzoneDialog", "ActivationDialog", "ChangeProfileDialog", "create_page"]
