"""Rumble settings rendered exclusively from RumbleView."""
from ...widgets.changes import apply_change
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QWidget, QHBoxLayout
from qfluentwidgets import Slider, SwitchButton, BodyLabel
from app_core import events
from ...widgets.common import SettingsGroup, SettingRow, ExpandSettingRow, Segments
from ...qt_bridge import call_core
from .support import ServicePage, int_spin, sync


class RumblePage(ServicePage):
    KEYS = {'rumble', 'rumble_mode', 'vibration_strength', 'vibration_frequency',
            'rumble_delay_ms', 'audio_haptics_enabled', 'adaptive_triggers_enabled',
            'impulse_trigger_enabled', 'impulse_trigger_dynamic_frequency',
            'impulse_trigger_strength', 'impulse_trigger_frequency'}

    def __init__(self, core, toast, parent=None):
        super().__init__('Rumble', core, toast, parent)
        self.controls, self.rows, self.pending, self.drag_old = {}, {}, {}, {}
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(100)
        self.timer.timeout.connect(self.flush)
        group = SettingsGroup('Rumble', self.content); self.box.addWidget(group)
        self.mode = Segments(group)
        group.row('Mode', self.mode)
        self.mode.currentItemChanged.connect(lambda value: apply_change(self, core.settings.set_rumble_mode, value, old_args=(self.view.mode,)))
        self.impulse = ExpandSettingRow('Impulse triggers', 'Add vibration to the controller triggers.', parent=self.content)
        self.audio = ExpandSettingRow('Audio haptics', 'Turn audio output into controller vibration.', parent=self.content)
        for key, title, low, high, owner in (
            ('vibration_strength', 'Strength', 0, 10, group),
            ('vibration_frequency', 'Frequency', 1, 10, group),
            ('impulse_trigger_strength', 'Strength', 1, 10, self.impulse),
            ('impulse_trigger_frequency', 'Frequency', 1, 10, self.impulse)):
            holder = QWidget(owner); layout = QHBoxLayout(holder); layout.setContentsMargins(0, 0, 0, 0)
            slider = Slider(Qt.Horizontal, holder)
            slider.setRange(low, high)
            slider.setSingleStep(1)
            slider.setMinimumWidth(180)
            slider.setAccessibleName(title)
            label = BodyLabel('', holder); label.setFixedWidth(28)
            layout.addWidget(slider); layout.addWidget(label)
            self.controls[key] = slider
            row = SettingRow(title, control=holder, parent=owner)
            (owner.addRow if isinstance(owner, SettingsGroup) else owner.addWidget)(row)
            self.rows[key] = row
            slider.valueChanged.connect(lambda value, k=key, l=label: (l.setText(str(value)), self.queue(k, value)))
            slider.sliderPressed.connect(lambda k=key: self.begin_drag(k))
            slider.sliderReleased.connect(lambda k=key: self.end_drag(k))
            slider._value_label = label
        holder = QWidget(group); line = QHBoxLayout(holder); line.setContentsMargins(0, 0, 0, 0)
        delay = int_spin(holder); line.addWidget(delay); line.addWidget(BodyLabel('ms', holder))
        delay.setAccessibleName('Rumble delay in milliseconds')
        self.controls['rumble_delay_ms'] = delay
        group.row('Delay', holder)
        delay.valueChanged.connect(lambda value: apply_change(self, core.settings.set_rumble_setting, 'rumble_delay_ms', value, old_args=('rumble_delay_ms', self.values['rumble_delay_ms'])))
        for key, title, owner in (
            ('impulse_trigger_enabled', 'Impulse triggers', self.impulse),
            ('impulse_trigger_dynamic_frequency', 'Dynamic frequency', self.impulse),
            ('audio_haptics_enabled', 'Audio haptics', self.audio),
            ('adaptive_triggers_enabled', 'Adaptive triggers', self.audio)):
            switch = SwitchButton(owner)
            switch.setAccessibleName(title)
            self.controls[key] = switch
            owner.addWidget(SettingRow(title, control=switch, parent=owner))
            switch.checkedChanged.connect(lambda value, k=key: apply_change(self, core.settings.set_rumble_setting, k, value, old_args=(k, self.values[k])))
        self.box.addWidget(self.impulse); self.box.addWidget(self.audio); self.box.addStretch()
        self.bridge.event.connect(self.on_bus_event)
        call_core(core.settings.state, on_result=self.apply_settings)

    def on_bus_event(self, topic, value):
        if topic == events.SETTINGS_CHANGED and (not getattr(value, 'keys', None) or self.KEYS.intersection(value.keys)):
            call_core(self.core.settings.state, on_result=self.apply_settings)
        elif topic == events.DRIVER_STATE_CHANGED:
            call_core(self.core.settings.state, on_result=self.apply_settings)

    def apply_settings(self, state):
        self._settings_state = state
        self.view = state.rumble
        view = self.view
        self.mode.sync(tuple(o.value for o in view.options), view.mode, {o.value: o.label for o in view.options})
        values = dict(vibration_strength=view.strength, vibration_frequency=view.frequency, rumble_delay_ms=view.delay_ms,
                      **{k: getattr(view, k) for k in self.controls if k.startswith(('audio_', 'adaptive_', 'impulse_'))})
        for key, widget in self.controls.items():
            if key in self.pending or key in self.drag_old:
                continue
            sync(widget, 'setChecked' if isinstance(widget, SwitchButton) else 'setValue', values[key])
            if hasattr(widget, '_value_label'):
                widget._value_label.setText(str(values[key]))
        self.values = values
        self.rows['vibration_frequency'].setVisible(view.frequency_visible)
        self.rows['impulse_trigger_frequency'].setVisible(view.impulse_trigger_enabled and not view.impulse_trigger_dynamic_frequency)
        self.rows["impulse_trigger_strength"].setVisible(view.impulse_trigger_enabled)
        self.controls["impulse_trigger_dynamic_frequency"].parentWidget().setVisible(view.impulse_trigger_enabled)
        self.impulse.setVisible(view.impulse_trigger_available)
        self.audio.setVisible(view.audio_haptics_available)

    def begin_drag(self, key):
        self.drag_old[key] = self.values[key]
        call_core(self.core.settings.begin_batch)

    def queue(self, key, value):
        self.pending[key] = value
        if not self.timer.isActive():
            self.timer.start()

    def flush(self):
        pending, self.pending = self.pending, {}
        for key, value in pending.items():
            if key in self.drag_old:
                apply_change(self, self.core.settings.set_rumble_setting, key, value)
            else:
                apply_change(self, self.core.settings.set_rumble_setting, key, value, old_args=(key, self.values[key]))

    def end_drag(self, key):
        self.timer.stop()
        self.flush()
        old = self.drag_old.pop(key)
        apply_change(self, self.core.settings.end_batch,
                     undo=lambda: apply_change(self, self.core.settings.set_rumble_setting, key, old))

    def finish_edits(self):
        self.timer.stop(); self.flush()
        for key in tuple(self.drag_old):
            self.end_drag(key)

    def hideEvent(self, event):
        self.finish_edits(); super().hideEvent(event)

    def closeEvent(self, event):
        self.finish_edits(); self.bridge.close(); super().closeEvent(event)


def create_page(core, toast, parent=None):
    return RumblePage(core, toast, parent)
