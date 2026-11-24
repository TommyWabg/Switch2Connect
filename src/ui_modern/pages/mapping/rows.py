from ...i18n import tr
from ...widgets.changes import apply_change
from PySide6.QtCore import Qt, QEvent
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget, QSizePolicy
from qfluentwidgets import DropDownPushButton, TransparentToolButton, TransparentPushButton, FluentIcon, BodyLabel, setCustomStyleSheet
from ...widgets.common import Segments
from ...widgets.recording import RecordDialog
from ...widgets.pickers import OptionPicker
from ...widgets.display import input_display, setting_title


class MappingRow(QWidget):
    def __init__(self, page, snapshot, scope=None, parent=None, *, writer=None, configure=None):
        super().__init__(parent)
        self.setMaximumWidth(264)
        outer = QVBoxLayout(self); outer.setContentsMargins(0, 0, 0, 0); outer.setSpacing(4)
        self.titleLabel = BodyLabel(snapshot.label.rstrip(":"), self)
        controls = QWidget(self)
        box = QHBoxLayout(controls); box.setContentsMargins(0, 0, 0, 0); box.setSpacing(4)
        self.value = DropDownPushButton(controls)
        value_style = "DropDownPushButton { text-align: left; padding-left: 12px; }"
        setCustomStyleSheet(self.value, value_style, value_style)
        self.value.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.value.setMinimumHeight(32)
        self.value.installEventFilter(self)
        self.reset = TransparentToolButton(FluentIcon.ROTATE, controls)
        self.reset.setFixedSize(32, 32)
        self.reset.setAccessibleName(tr("Reset {title}", title=setting_title(snapshot.label.rstrip(":")))); self.reset.setToolTip(tr("Restore Default"))
        box.addWidget(self.value, 1)
        box.addWidget(self.reset)
        outer.addWidget(self.titleLabel); outer.addWidget(controls)
        self.page, self.scope, self.writer, self.configure_callback = page, scope, writer, configure
        self.value.clicked.connect(self.pick)
        self.reset.clicked.connect(self.restore_default)
        self.update_snapshot(snapshot)

    def update_snapshot(self, snapshot):
        self.snapshot = snapshot
        self.titleLabel.setText(setting_title(snapshot.label.rstrip(":")))
        display = input_display(snapshot.display_text)
        self.value.setToolTip(display)
        self._fit_value_text()
        self.value.setAccessibleName(self.titleLabel.text() + " " + display)
        self.can_configure = bool(self.configure_callback) or (not self.writer and snapshot.configurable)
        self.reset.setVisible(not snapshot.is_default)
        self.setVisible(snapshot.visible)

    def _fit_value_text(self):
        width = max(0, self.value.width() - 40)
        self.value.setText(self.value.fontMetrics().elidedText(self.value.toolTip(), Qt.ElideRight, width))

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Resize and watched is self.value:
            self._fit_value_text()
        return super().eventFilter(watched, event)

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
        if hasattr(self, "picker"):
            self.picker.flyout.close()
            self.picker.flyout.deleteLater()
        self.picker = OptionPicker(self.snapshot, self.choose, self.record, self)
        if self.snapshot.mode is not None:
            activation = Segments(self.picker); activation.sync(("Hold", "Tap"), self.snapshot.mode)
            activation.currentItemChanged.connect(self.set_mode); self.picker.box.addWidget(activation)
            self.picker.activation = activation
        if self.can_configure:
            configure = TransparentPushButton(FluentIcon.SETTING, tr("Configure…"), self.picker)
            configure.clicked.connect(lambda: (self.picker.dismiss(), self.configure()))
            self.picker.box.addWidget(configure)
            self.picker.configure_button = configure
        self.picker.show_at(self.value)

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
