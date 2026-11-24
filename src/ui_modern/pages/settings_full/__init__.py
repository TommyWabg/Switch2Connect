from ...widgets.changes import apply_change, apply_setting, apply_toggle
from PySide6.QtGui import QIcon
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget, QHBoxLayout
from ...widgets.controls import ComboBox
from qfluentwidgets import (PushButton, HyperlinkButton, SwitchButton, BodyLabel, MessageBox,
                            FluentIcon)
from app_core import events
from app_core.models import WIN32_BACKEND, WINUHID_BACKEND
from ...widgets.display import setting_title
from ...qt_bridge import call_core
from ...widgets.common import ExpandSettingRow, SettingsGroup, Segments
from ... import i18n, theme as ui_theme
from ...i18n import tr
from ..rumble.support import ServicePage, sync
from .firmware import FirmwareManagerDialog


class SettingsPage(ServicePage):
    def __init__(self, core, toast, request_restart=lambda: None, apply_priority=lambda policy: None,
                 parent=None, *, hide_to_tray=None, version='v3.4-modern-preview.7'):
        super().__init__('Settings', core, toast, parent)
        self.request_restart, self.apply_priority, self.hide_to_tray = request_restart, apply_priority, hide_to_tray
        self.driver_rows = {}
        drivers = SettingsGroup('Drivers', self.content); self.box.addWidget(drivers)
        for name in ('WinUHid', 'ViGEmBus', 'USBIP', 'HidHide'):
            button = PushButton(tr('Check status'), drivers)
            row = drivers.row(name, button, 'Unknown')
            self.driver_rows[name] = row, row.captionLabel, button
            button.clicked.connect(lambda checked=False, n=name: self.driver_action(n))
        self.restart_notice = drivers.row('Restart required', BodyLabel(tr('Restart Windows before reinstalling USBIP.'), drivers))
        self.keyboard, self.keyboard_row = self.output_selector(
            drivers, 'Keyboard output backend', 'keyboard_backend', core.drivers.set_keyboard_output)
        self.mouse, self.mouse_row = self.output_selector(
            drivers, 'Mouse output backend', 'mouse_backend', core.drivers.set_mouse_output)
        hardware = SettingsGroup('Hardware', self.content); self.box.addWidget(hardware)
        self.firmware = PushButton(tr('Manage firmware…'), hardware)
        self.esp_row = hardware.row('ESP32-S3 bridge', self.firmware, 'Checking…')
        self.firmware.clicked.connect(self.manage_firmware)
        self.wired = ExpandSettingRow(
            'Wired controller', 'Controller scanning and HidHide', hardware, icon=FluentIcon.GAME)
        hardware.addRow(self.wired)
        self.auto_scan = SwitchButton(self.wired)
        self.manual_scan = PushButton(tr('Manual scan'), self.wired)
        self.hidhide = SwitchButton(self.wired)
        self.hidhide_uninstall = PushButton(tr('Uninstall HidHide'), self.wired)
        for switch in (self.auto_scan, self.hidhide):
            switch.setOnText('')
            switch.setOffText('')
            switch.setFocusPolicy(Qt.StrongFocus)
        self.hidhide_widgets = []
        for title, control in [('Auto scan', self.auto_scan), ('Manual scan', self.manual_scan), ('HidHide active', self.hidhide), ('HidHide driver', self.hidhide_uninstall)]:
            control.setAccessibleName(tr(title))
            row = self.wired.addGroup(QIcon(), tr(title), '', control)
            if control in (self.hidhide, self.hidhide_uninstall):
                separator = self.wired.viewLayout.itemAt(self.wired.viewLayout.indexOf(row) - 1).widget()
                self.hidhide_widgets.extend((separator, row))
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
        self.tray = PushButton(tr('Hide to system tray'), app)
        app.row('System tray', self.tray, 'The close button quits Switch 2 Connect.')
        self.tray.clicked.connect(self.hide_window)
        self.close_behavior = ComboBox(app)
        for value in ('Quit', 'Hide to tray'):
            self.close_behavior.addItem(tr(value), userData=value)
        self.close_behavior.setCurrentIndex(self.close_behavior.findData(ui_theme.preferences().value('close_behavior', 'Quit')))
        app.row('Close button behavior', self.close_behavior)
        self.close_behavior.currentIndexChanged.connect(lambda i: self.set_close_behavior(self.close_behavior.itemData(i)))
        interface = SettingsGroup('Interface', self.content); self.box.addWidget(interface)
        self.classic = PushButton(tr('Use classic interface'), interface)
        interface.row('Switch interface', self.classic, 'Switch 2 Connect will restart.')
        self.classic.clicked.connect(self.switch_classic)
        self.theme = ComboBox(interface)
        for value in ('System', 'Light', 'Dark'):
            self.theme.addItem(tr(value), userData=value)
        self.theme.setCurrentIndex(self.theme.findData(ui_theme.preferences().value('theme', 'System')))
        interface.row('Theme', self.theme)
        self.theme.currentIndexChanged.connect(lambda i: self.change_theme(self.theme.itemData(i)))
        language_controls = QWidget(interface)
        language_layout = QHBoxLayout(language_controls)
        language_layout.setContentsMargins(0, 0, 0, 0)
        self.language = ComboBox(language_controls)
        for code, label in i18n.LANGUAGES.items():
            self.language.addItem(tr(label), userData=code)
        self.startup_language = i18n.preference()
        self.language.setCurrentIndex(self.language.findData(self.startup_language))
        self.language.setAccessibleName(tr('Language'))
        self.restart_language = PushButton(tr('Restart now'), language_controls)
        self.restart_language.hide()
        language_layout.addWidget(self.language)
        language_layout.addWidget(self.restart_language)
        interface.row('Language', language_controls, 'Takes effect the next time Switch 2 Connect starts.')
        self.language.currentIndexChanged.connect(self.change_language)
        self.restart_language.clicked.connect(self.request_restart)
        about = SettingsGroup('About', self.content); self.box.addWidget(about)
        about.row('Switch 2 Connect', BodyLabel(version, about))
        self.kofi = HyperlinkButton('https://ko-fi.com/tagayama', tr('Support on Ko-fi'), about)
        about.row('Support development', self.kofi)
        about.row('License', BodyLabel(tr('GNU General Public License v3'), about))
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
            badge.setText(tr(health.title())); badge.setToolTip(next((s.description for s in state.install_status if s.driver == name), health))
            action = 'Uninstall' if health == 'healthy' else 'Repair' if health == 'partial' else 'Install'
            button.setText(tr(action))
            full_action = tr(action + ' HidHide') if name == 'HidHide' else tr(action + ' {driver} driver', driver=name)
            button.setAccessibleName(full_action)
            button.setToolTip(full_action)
            button.setEnabled(not state.busy and not state.pending_check)
            if name == 'WinUHid': button.setVisible(not state.packaged_build or state.packaged_winuhid_available)
            if name == 'USBIP' and state.usbip_restart_pending:
                button.setText(tr('Restart Required')); button.setEnabled(not state.busy)
                button.setAccessibleName(button.text()); button.setToolTip(button.text())
        self.restart_notice.setVisible(state.usbip_restart_pending)
        self.keyboard.setCurrentIndex(self.keyboard.findData(state.keyboard_backend))
        self.mouse.setCurrentIndex(self.mouse.findData(state.mouse_backend))
        self.keyboard_row.setVisible(state.keyboard_available); self.mouse_row.setVisible(state.mouse_available)
        self.keyboard.setEnabled(not state.busy); self.mouse.setEnabled(not state.busy)
        title = setting_title(state.wired_settings_label)
        self.wired.card.setTitle(title)
        self.wired.card.expandButton.setAccessibleName(title)
        sync(self.auto_scan, 'setChecked', state.wired_auto_scan_enabled)
        sync(self.hidhide, 'setChecked', state.hidhide_active)
        for widget in self.hidhide_widgets:
            widget.setVisible(state.hidhide_service_state is True)
        self.wired._adjustViewSize()
        for control in (self.auto_scan, self.manual_scan, self.hidhide, self.hidhide_uninstall): control.setEnabled(not state.busy)

    def output_selector(self, group, title, state_field, setter):
        combo = ComboBox(group)
        combo.setMinimumWidth(168)
        for backend in (WIN32_BACKEND, WINUHID_BACKEND):
            combo.addItem(tr(backend), userData=backend)
        combo.setAccessibleName(tr(title))
        combo.setEnabled(False)

        def select(index):
            backend = combo.itemData(index)
            current = getattr(self.driver_state, state_field)
            if backend != current:
                apply_change(self, setter, backend, old_args=(current,))

        combo.activated.connect(select)
        return combo, group.row(title, combo)

    def apply_esp32(self, state):
        self.esp_row.captionLabel.setText(' · '.join(filter(None, (tr(state.status), tr(state.firmware_text)))))
        self.esp_row.captionLabel.show()

    def driver_action(self, name):
        methods = {'WinUHid': 'on_winuhid_manager_driver_action', 'ViGEmBus': 'on_vigembus_driver_action',
                   'USBIP': 'on_usbip_btn_clicked', 'HidHide': 'on_hidhide_button'}
        apply_change(self, getattr(self.core.drivers, methods[name]))


    def change_language(self, index):
        code = self.language.itemData(index)
        ui_theme.preferences().setValue('language', code)
        self.restart_language.setVisible(code != self.startup_language)

    def change_theme(self, name):
        old = ui_theme.preferences().value('theme', 'System')
        def write(value):
            ui_theme.preferences().setValue('theme', value)
            ui_theme.apply_theme(value)
            sync(self.theme, 'setCurrentIndex', self.theme.findData(value))
        apply_change(self, write, name, old_args=(old,), label='Theme applied')

    def set_close_behavior(self, name):
        old = ui_theme.preferences().value('close_behavior', 'Quit')
        def write(value):
            ui_theme.preferences().setValue('close_behavior', value)
            sync(self.close_behavior, 'setCurrentIndex', self.close_behavior.findData(value))
        apply_change(self, write, name, old_args=(old,))

    def hide_window(self):
        callback = self.hide_to_tray or getattr(self.window(), 'hide_to_tray', None)
        if callback: callback()

    def switch_classic(self):
        dialog = MessageBox(tr('Switch interface'), tr('Switch to the classic interface? Switch 2 Connect will restart.'), self.window())
        dialog.yesButton.setText(tr('Switch interface now'))
        dialog.cancelButton.setText(tr('Cancel'))
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
