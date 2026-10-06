from ...widgets.changes import apply_change
from PySide6.QtWidgets import QHBoxLayout, QWidget
from qfluentwidgets import DropDownPushButton, TransparentToolButton, PushButton, FluentIcon, BodyLabel, themeColor
from ...widgets.common import SettingRow, Segments
from ...widgets.recording import RecordDialog
from ...widgets.pickers import OptionPicker
from ...widgets.display import input_display, setting_title


class MappingRow(SettingRow):
    def __init__(self, page, snapshot, scope=None, parent=None, *, writer=None, configure=None):
        controls = QWidget(parent); controls.setFixedWidth(285)
        box = QHBoxLayout(controls); box.setContentsMargins(0, 0, 0, 0); box.setSpacing(6)
        self.value = DropDownPushButton(controls); self.value.setFixedWidth(240)
        self.reset = TransparentToolButton(FluentIcon.CANCEL, controls)
        self.reset.setAccessibleName("Reset " + snapshot.label); self.reset.setToolTip("Restore Default")
        for widget in (self.value, self.reset):
            box.addWidget(widget)
        box.addStretch()
        super().__init__(snapshot.label.rstrip(":"), control=controls, parent=parent)
        self.marker = BodyLabel("●", self)
        self.marker.setFixedWidth(8)
        self.rowLayout.insertWidget(0, self.marker)
        self.page, self.scope, self.writer, self.configure_callback = page, scope, writer, configure
        self.value.clicked.connect(self.pick)
        self.reset.clicked.connect(self.restore_default)
        self.update_snapshot(snapshot)

    def update_snapshot(self, snapshot):
        self.snapshot = snapshot
        self.marker.setVisible(not snapshot.is_default)
        self.marker.setStyleSheet("color: " + themeColor().name() + ";")
        self.titleLabel.setText(setting_title(snapshot.label.rstrip(":")))
        display = input_display(snapshot.display_text)
        self.value.setText(display)
        self.value.setToolTip(display)
        self.value.setAccessibleName(snapshot.label + " " + display)
        self.can_configure = bool(self.configure_callback) or (not self.writer and snapshot.configurable)
        self.reset.setVisible(not snapshot.is_default)
        self.setVisible(snapshot.visible)

    def choose(self, value):
        previous = self.snapshot.stored_value
        if self.writer:
            self.writer(value)
        else:
            key, scope = self.snapshot.key, self.scope
            apply_change(self.page, self.page.core.settings.set_mapping, key, value, scope,
                undo=lambda: apply_change(self.page, self.page.core.settings.set_mapping, key, previous, scope),
                )

    def restore_default(self):
        if self.writer:
            self.writer("None" if self.snapshot.key.endswith("_in_app_gyro_simul") else "Default")
            return
        key, scope, previous = self.snapshot.key, self.scope, self.snapshot.stored_value
        apply_change(self.page, self.page.core.settings.reset_mapping, key, scope,
            undo=lambda: apply_change(self.page, self.page.core.settings.set_mapping, key, previous, scope))

    def set_mode(self, mode):
        key, scope, previous = self.snapshot.key, self.scope, self.snapshot.mode
        if key in ("l_joystick", "r_joystick") and self.snapshot.stored_value == "Scroll Wheel":
            fn = self.page.core.settings.set_joystick_setting
            apply_change(self.page, fn, key, "scroll_activation", mode, scope,
                undo=lambda: apply_change(self.page, fn, key, "scroll_activation", previous, scope))
        elif self.writer:
            self.writer(dict(self.snapshot.mode_values)[mode])
        else:
            fn = self.page.core.settings.set_mapping_mode
            apply_change(self.page, fn, key, mode, scope, undo=lambda: apply_change(self.page, fn, key, previous, scope))

    def pick(self):
        self.open_picker()

    def open_picker(self):
        self.picker = OptionPicker(self.snapshot, self.choose, self.record, self)
        self.picker.record_button.setVisible(self.snapshot.key not in ("l_joystick", "r_joystick"))
        if self.snapshot.mode is not None:
            activation = Segments(self.picker); activation.sync(("Hold", "Tap"), self.snapshot.mode)
            activation.currentItemChanged.connect(self.set_mode); self.picker.box.addWidget(activation)
            self.picker.activation = activation
        if self.can_configure:
            configure = PushButton("Configure…", self.picker)
            configure.clicked.connect(lambda: (self.picker.accept(), self.configure()))
            self.picker.box.addWidget(configure)
            self.picker.configure_button = configure
        self.picker.move(self.value.mapToGlobal(self.value.rect().bottomLeft()))
        self.picker.show()

    def record(self):
        previous = self.snapshot.stored_value
        key, scope = self.snapshot.key, self.scope
        writer = self.writer or (lambda value: apply_change(self.page, self.page.core.settings.set_mapping, key, value, scope,
            undo=lambda: apply_change(self.page, self.page.core.settings.set_mapping, key, previous, scope)))
        self.dialog = RecordDialog(self.page.core, key, scope, self.page.toast, self.window(),
                                   initial=str(previous), writer=writer)
        self.dialog.show()

    def configure(self):
        if self.configure_callback:
            return self.configure_callback()
        self.page.configure_mapping(self.snapshot.key, self.scope, self.snapshot.stored_value)
