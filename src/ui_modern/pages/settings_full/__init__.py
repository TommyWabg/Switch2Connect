from ...widgets.changes import apply_change, apply_setting, apply_toggle
from PySide6.QtGui import QDesktopServices
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import QWidget, QHBoxLayout
from qfluentwidgets import PushButton, SwitchButton, ComboBox, BodyLabel, MessageBox
from app_core import events
from ...widgets.display import setting_title
from ...qt_bridge import call_core
from ...widgets.common import SettingsGroup, ExpandSettingRow, SettingRow, Segments
from ... import theme as ui_theme
from ..rumble.support import ServicePage, sync
from .firmware import FirmwareManagerDialog


class SettingsPage(ServicePage):
    def __init__(self, core, toast, request_restart=lambda: None, apply_priority=lambda policy: None,
                 parent=None, *, hide_to_tray=None, version='v2.9.1'):
        super().__init__('Settings', core, toast, parent)
        self.request_restart, self.apply_priority, self.hide_to_tray = request_restart, apply_priority, hide_to_tray
        self.driver_rows = {}
        drivers = SettingsGroup('Drivers', self.content); self.box.addWidget(drivers)
        for name in ('WinUHid', 'ViGEmBus', 'USBIP', 'HidHide'):
            holder = QWidget(drivers); line = QHBoxLayout(holder); line.setContentsMargins(0, 0, 0, 0)
            badge = BodyLabel('Unknown', holder)
            button = PushButton('Check status', holder)
            line.addWidget(badge); line.addWidget(button)
            row = drivers.row(name, holder)
            self.driver_rows[name] = row, badge, button
            button.clicked.connect(lambda checked=False, n=name: self.driver_action(n))
        self.restart_notice = drivers.row('Restart required', BodyLabel('Restart Windows before reinstalling USBIP.', drivers))
        self.keyboard = PushButton('Win32 API', drivers)
        self.keyboard_row = drivers.row('Keyboard output backend', self.keyboard)
        self.mouse = PushButton('Win32 API', drivers)
        self.mouse_row = drivers.row('Mouse output backend', self.mouse)
        self.keyboard.clicked.connect(lambda: apply_toggle(self, core.drivers.toggle_keyboard_output, lambda: self.driver_state.keyboard_backend))
        self.mouse.clicked.connect(lambda: apply_toggle(self, core.drivers.toggle_mouse_output, lambda: self.driver_state.mouse_backend))
        hardware = SettingsGroup('Hardware', self.content); self.box.addWidget(hardware)
        self.firmware = PushButton('Manage firmware…', hardware)
        self.esp_row = hardware.row('ESP32-S3 bridge', self.firmware, 'Checking…')
        self.firmware.clicked.connect(self.manage_firmware)
        self.wired = ExpandSettingRow('Wired controller', parent=hardware)
        hardware.addRow(self.wired)
        self.auto_scan = SwitchButton(self.wired)
        self.manual_scan = PushButton('Manual scan', self.wired)
        self.hidhide = SwitchButton(self.wired)
        self.hidhide_uninstall = PushButton('Uninstall HidHide', self.wired)
        for title, control in [('Auto scan', self.auto_scan), ('Manual scan', self.manual_scan), ('HidHide active', self.hidhide), ('HidHide driver', self.hidhide_uninstall)]:
            row = SettingRow(title, control=control, parent=self.wired); self.wired.addWidget(row)
            if control is self.hidhide: self.hidhide_row = row
            if control is self.hidhide_uninstall: self.hidhide_uninstall_row = row
        self.auto_scan.checkedChanged.connect(lambda _: apply_toggle(self, core.drivers.toggle_wired_auto_scan, lambda: self.driver_state.wired_auto_scan_enabled))
        self.manual_scan.clicked.connect(lambda: apply_change(self, core.drivers.manual_wired_scan))
        self.hidhide.checkedChanged.connect(lambda _: apply_toggle(self, core.drivers.toggle_hidhide_active, lambda: self.driver_state.hidhide_active))
        self.hidhide_uninstall.clicked.connect(lambda: apply_change(self, core.drivers.on_hidhide_button))
        app = SettingsGroup('App', self.content); self.box.addWidget(app)
        self.startup = SwitchButton(app)
        self.minimized = SwitchButton(app)
        self.power = Segments(app)
        app.row('Run at startup', self.startup)
        app.row('Start minimized', self.minimized)
        app.row('Power saving', self.power)
        self.startup.checkedChanged.connect(lambda val: apply_setting(self, core.settings, 'set_run_at_startup', 'run_at_startup', val, self._app_state))
        self.minimized.checkedChanged.connect(lambda val: apply_setting(self, core.settings, 'set_start_minimized', 'start_minimized', val, self._app_state))
        self.power.currentItemChanged.connect(lambda val: apply_setting(self, core.settings, 'set_power_saving_mode', 'power_saving_mode', val, self._app_state))
        self.tray = PushButton('Hide to system tray', app)
        app.row('System tray', self.tray, 'The close button quits Switch 2 Connect.')
        self.tray.clicked.connect(self.hide_window)
        self.close_behavior = ComboBox(app)
        self.close_behavior.addItems(['Quit', 'Hide to tray'])
        self.close_behavior.setCurrentText(ui_theme.preferences().value('close_behavior', 'Quit'))
        app.row('Close button behavior', self.close_behavior)
        self.close_behavior.currentTextChanged.connect(self.set_close_behavior)
        interface = SettingsGroup('Interface', self.content); self.box.addWidget(interface)
        self.classic = PushButton('Use classic interface', interface)
        interface.row('Switch interface', self.classic, 'Switch 2 Connect will restart.')
        self.classic.clicked.connect(self.switch_classic)
        self.theme = ComboBox(interface)
        self.theme.addItems(['System', 'Light', 'Dark'])
        self.theme.setCurrentText(ui_theme.preferences().value('theme', 'System'))
        interface.row('Theme', self.theme)
        self.theme.currentTextChanged.connect(self.change_theme)
        interface.row('Language', BodyLabel('English', interface), 'Additional languages are not available yet.')
        about = SettingsGroup('About', self.content); self.box.addWidget(about)
        about.row('Switch 2 Connect', BodyLabel(version, about))
        self.kofi = PushButton('Support on Ko-fi', about)
        about.row('Support development', self.kofi)
        self.kofi.clicked.connect(lambda: QDesktopServices.openUrl(QUrl('https://ko-fi.com/tagayama')))
        about.row('License', BodyLabel('GNU General Public License v3', about))
        self.box.addStretch()
        self.bridge.event.connect(self.on_bus_event)
        call_core(core.settings.state, on_result=self.apply_settings)
        call_core(core.drivers.state, on_result=self.apply_driver)
        if hasattr(core, 'esp32'): call_core(core.esp32.state, on_result=self.apply_esp32)

    def on_bus_event(self, topic, payload):
        if topic == events.SETTINGS_CHANGED:
            keys = getattr(payload, 'keys', None)
            if not keys or keys.intersection({'app', 'open_when_startup', 'run_at_startup', 'start_minimized', 'power_saving_mode', 'ui_mode'}):
                call_core(self.core.settings.state, on_result=self.apply_settings)
        elif topic == events.DRIVER_STATE_CHANGED: self.apply_driver(payload)
        elif topic == events.ESP32_STATE_CHANGED: self.apply_esp32(payload)

    def apply_settings(self, state):
        self._settings_state = state
        self._app_state = state.app
        sync(self.startup, 'setChecked', state.app.run_at_startup)
        sync(self.minimized, 'setChecked', state.app.start_minimized)
        self.power.sync(('Off', 'Auto', 'Full'), state.app.power_saving_mode)

    def apply_driver(self, state):
        self.driver_state = state
        statuses = dict(state.install_states)
        for name, (row, badge, button) in self.driver_rows.items():
            health = statuses.get(name, 'healthy' if name == 'HidHide' and state.hidhide_service_state else 'unknown')
            badge.setText(health.title()); badge.setToolTip(next((s.description for s in state.install_status if s.driver == name), health))
            action = 'Uninstall' if health == 'healthy' else 'Repair' if health == 'partial' else 'Install'
            button.setText(action + ' ' + name + (' driver' if name != 'HidHide' else ''))
            button.setEnabled(not state.busy and not state.pending_check)
            if name == 'WinUHid': button.setVisible(not state.packaged_build or state.packaged_winuhid_available)
            if name == 'USBIP' and state.usbip_restart_pending:
                button.setText('Restart Required'); button.setEnabled(not state.busy)
        self.restart_notice.setVisible(state.usbip_restart_pending)
        self.keyboard.setText(state.keyboard_backend); self.mouse.setText(state.mouse_backend)
        self.keyboard_row.setVisible(state.keyboard_available); self.mouse_row.setVisible(state.mouse_available)
        self.keyboard.setEnabled(not state.busy); self.mouse.setEnabled(not state.busy)
        self.wired.row.titleLabel.setText(setting_title(state.wired_settings_label)); self.wired.row.titleLabel.setToolTip(state.wired_settings_label)
        sync(self.auto_scan, 'setChecked', state.wired_auto_scan_enabled)
        sync(self.hidhide, 'setChecked', state.hidhide_active)
        self.hidhide_row.setVisible(state.hidhide_service_state is True)
        self.hidhide_uninstall_row.setVisible(state.hidhide_service_state is True)
        for control in (self.auto_scan, self.manual_scan, self.hidhide, self.hidhide_uninstall): control.setEnabled(not state.busy)

    def apply_esp32(self, state):
        self.esp_row.captionLabel.setText(' · '.join(filter(None, (state.status, state.firmware_text))))
        self.esp_row.captionLabel.show()

    def driver_action(self, name):
        methods = {'WinUHid': 'on_winuhid_manager_driver_action', 'ViGEmBus': 'on_vigembus_driver_action',
                   'USBIP': 'on_usbip_btn_clicked', 'HidHide': 'on_hidhide_button'}
        apply_change(self, getattr(self.core.drivers, methods[name]))


    def change_theme(self, name):
        old = ui_theme.preferences().value('theme', 'System')
        def write(value):
            ui_theme.preferences().setValue('theme', value)
            ui_theme.apply_theme(value)
            sync(self.theme, 'setCurrentText', value)
        apply_change(self, write, name, old_args=(old,), label='Theme applied')

    def set_close_behavior(self, name):
        old = ui_theme.preferences().value('close_behavior', 'Quit')
        def write(value):
            ui_theme.preferences().setValue('close_behavior', value)
            sync(self.close_behavior, 'setCurrentText', value)
        apply_change(self, write, name, old_args=(old,))

    def hide_window(self):
        callback = self.hide_to_tray or getattr(self.window(), 'hide_to_tray', None)
        if callback: callback()

    def switch_classic(self):
        dialog = MessageBox('Switch interface', 'Switch to the classic interface? Switch 2 Connect will restart.', self.window())
        dialog.yesButton.setText('Switch')
        dialog.cancelButton.setText('Cancel')
        def finished(accepted):
            if accepted:
                apply_change(self, self.core.settings.set_ui_mode, 'classic', restart=True)
        dialog.finished.connect(finished)
        self.confirmation = dialog
        dialog.show()

    def manage_firmware(self):
        if hasattr(self, 'firmware_dialog') and self.firmware_dialog.isVisible():
            self.firmware_dialog.raise_(); return
        self.firmware_dialog = FirmwareManagerDialog(self.core, self.toast, self.window()); self.firmware_dialog.show()


def create_page(core, toast, request_restart=lambda: None, apply_priority=lambda policy: None, parent=None, **kwargs):
    return SettingsPage(core, toast, request_restart, apply_priority, parent, **kwargs)
