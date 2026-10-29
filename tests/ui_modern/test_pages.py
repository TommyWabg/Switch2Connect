from ui_modern.widgets.changes import apply_change
from dataclasses import replace
import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from app_core import events
from app_core.models import Option, Esp32State, FirmwareSession, PortMode, PortChoice, ImportPreview, ExportPreview, ProfileResult
from conftest import FakeCore, FakeService, ready
from ui_modern.pages.rumble import RumblePage
from ui_modern.pages.profiles import ProfilesPage
from ui_modern.pages.profiles.dialogs import ChecklistDialog
from ui_modern.pages.settings_full import SettingsPage
from ui_modern.pages.settings_full.firmware import FirmwareManagerDialog


class T10Service(FakeService):
    def state(self):
        if self.name == 'esp32': return self.core.esp_state
        return super().state()

    def __getattr__(self, method):
        if self.name == 'settings' and method not in ('set_rumble_mode', 'set_rumble_setting'):
            return super().__getattr__(method)
        def call(*args, **kwargs):
            self.core.calls.append((f'{self.name}.{method}', args, kwargs))
            if self.name == 'esp32':
                value = {'port_mode': PortMode(), 'ports': self.core.ports, 'list_ports': self.core.ports,
                         'firmware_session': self.core.session}.get(method, ProfileResult(True))
                return ready(value)
            if self.name == 'settings' and method in ('set_rumble_mode', 'set_rumble_setting'):
                view = self.core.settings_state.rumble
                key = 'mode' if method == 'set_rumble_mode' else {'vibration_strength': 'strength', 'vibration_frequency': 'frequency', 'rumble_delay_ms': 'delay_ms'}.get(args[0], args[0])
                value = args[0] if method == 'set_rumble_mode' else args[1]
                view = replace(view, **{key: value})
                if key == 'mode': view = replace(view, frequency_visible=value == 'Xbox')
                self.core.settings_state = replace(self.core.settings_state, rumble=view)
                self.core.bus.publish(events.SETTINGS_CHANGED, events.SettingsChanged(frozenset({'rumble'})))
                return ready(ProfileResult(True))
            if self.name == 'profiles':
                if method == 'export_preview': return ready(ExportPreview(self.core.profiles_state.names, 'Default', True))
                if method == 'inspect_import': return ready(ImportPreview(args[0], ('Imported',), True))
                if method == 'begin_controller_combo_capture': self.core.capture = args[0]
                if method == 'set_profile_combo' and self.core.duplicate:
                    self.core.duplicate = False
                    return ready(ProfileResult(False, 'duplicate_combo', restart_recording=True))
            return ready(ProfileResult(True))
        return call


class T10Core(FakeCore):
    def __init__(self):
        super().__init__()
        self.esp_state = Esp32State(detected=True, status='Ready', firmware_text='Current', firmware_installed=True)
        self.session = FirmwareSession()
        self.ports = (PortChoice('COM3', 'COM3 · ESP32-S3', verified=True), PortChoice('COM4', 'COM4 · USB serial'))
        self.duplicate = False
        for name in ('settings', 'profiles', 'esp32'): setattr(self, name, T10Service(self, name))
        self.driver_state = replace(self.driver_state, keyboard_available=True, mouse_available=True,
            hidhide_service_state=True, hidhide_installed=True, install_states=(*self.driver_state.install_states, ('HidHide', 'healthy')))
        self.settings_state = replace(self.settings_state, rumble=replace(self.settings_state.rumble,
            mode='Xbox', options=(Option('Xbox', 'Xbox'), Option('Switch', 'Switch')), frequency_visible=True,
            strength=5, frequency=5, impulse_trigger_available=True, impulse_trigger_enabled=True,
            impulse_trigger_strength=5, impulse_trigger_frequency=5, impulse_trigger_dynamic_frequency=True))
        self.profiles_state = replace(self.profiles_state, assigned_apps=(('Default', (('C:/Games/game.exe', 'Example Game'),)),), switching_combos=(('Gaming', 'VK_F2'),))


@pytest.fixture
def setup_page(app, pump):
    pages = []; toasts = []
    def create(cls, **kwargs):
        core = T10Core(); page = cls(core, lambda *a, **k: toasts.append((a, k)), **kwargs)
        page.resize(700, 480) if cls is FirmwareManagerDialog else page.resize(1000, 900)
        page.show(); pump(timeout=.1); pages.append(page)
        return page, core, toasts
    yield create
    for page in pages:
        if isinstance(page, FirmwareManagerDialog): page.can_close = True; page.reject()
        else: page.bridge.close(); page.hide()
        page.deleteLater()
    pump()


def called(core, method, args=None):
    return any(name == method and (args is None or values == args) for name, values, kw in core.calls)


def test_rumble_ranges_batch_and_undo(setup_page, pump):
    page, core, toasts = setup_page(RumblePage)
    for key, low in [('vibration_strength', 0), ('vibration_frequency', 1), ('impulse_trigger_strength', 1), ('impulse_trigger_frequency', 1)]:
        slider = page.controls[key]; assert (slider.minimum(), slider.maximum(), slider.singleStep()) == (low, 10, 1)
        page.begin_drag(key); slider.setValue(8); slider.setValue(9); page.end_drag(key); pump()
        assert called(core, 'settings.set_rumble_setting', (key, 9))
        toasts[-1][1]['undo'](); pump()
    assert called(core, 'settings.begin_batch') and called(core, 'settings.end_batch')
    page.controls['rumble_delay_ms'].setValue(123); pump()
    assert called(core, 'settings.set_rumble_setting', ('rumble_delay_ms', 123))
    page.controls['rumble_delay_ms'].setValue(10**30); pump()
    assert called(core, 'settings.set_rumble_setting', ('rumble_delay_ms', 2**31 - 1))
    page.controls['rumble_delay_ms'].setValue(0); pump()
    assert called(core, 'settings.set_rumble_setting', ('rumble_delay_ms', 0))
    page.mode.setCurrentItem('Switch'); pump(); assert page.rows['vibration_frequency'].isHidden()
    assert called(core, 'settings.set_rumble_mode', ('Switch',))


@pytest.mark.parametrize('key', ['impulse_trigger_enabled', 'impulse_trigger_dynamic_frequency', 'audio_haptics_enabled', 'adaptive_triggers_enabled'])
def test_rumble_toggles(setup_page, pump, key):
    page, core, _ = setup_page(RumblePage)
    control = page.controls[key]; control.setChecked(not control.isChecked()); pump()
    assert called(core, 'settings.set_rumble_setting', (key, control.isChecked()))


def test_rumble_visibility_and_in_place(setup_page, pump):
    page, core, _ = setup_page(RumblePage)
    identities = {k: id(v) for k, v in page.controls.items()}
    assert not page.impulse.isHidden() and page.audio.isHidden()
    assert page.rows['impulse_trigger_frequency'].isHidden()
    core.settings_state = replace(core.settings_state, rumble=replace(core.settings_state.rumble,
        impulse_trigger_available=False, audio_haptics_available=True))
    core.bus.publish(events.SETTINGS_CHANGED, events.SettingsChanged()); pump()
    assert page.impulse.isHidden() and not page.audio.isHidden()
    assert identities == {k: id(v) for k, v in page.controls.items()}
    core.calls.clear(); core.bus.publish(events.SETTINGS_CHANGED, events.SettingsChanged(frozenset({'gyro'}))); pump()
    assert not core.calls


def test_profiles_controls_and_stable_rows(setup_page, pump):
    page, core, toasts = setup_page(ProfilesPage)
    row, select, include, menu = page.profile_rows['Gaming']
    select.click(); include.setChecked(True); page.clear_combo('Gaming'); page.clear_trigger.setEnabled(True); page.clear_trigger.click(); page.mode.setCurrentItem('Manual'); pump()
    assert called(core, 'profiles.select', ('Gaming',))
    assert called(core, 'profiles.set_in_change_list', ('Gaming', True))
    assert called(core, 'profiles.clear_profile_combo', ('Gaming',))
    assert called(core, 'profiles.clear_profile_switching_combo_trigger')
    page.remove_app(0); pump(); assert called(core, 'profiles.remove_assigned_app', (0,))
    toasts[-1][1]['undo'](); pump(); assert called(core, 'profiles.set_profile_assigned_apps')
    page.apply_profiles(core.profiles_state); assert page.profile_rows['Gaming'][0] is row
    page.rename('Default'); page.rename_dialog.viewLayout.itemAt(1).widget().setText('Gaming')
    assert not page.rename_dialog.yesButton.isEnabled(); page.rename_dialog.reject()


def test_checklists_import_export(setup_page, pump, monkeypatch):
    page, core, _ = setup_page(ProfilesPage)
    monkeypatch.setattr('ui_modern.pages.profiles.pick_file', lambda page, title, callback, **kw: callback('profiles.yaml'))
    page.import_profiles(); pump(); dialog = page.checklist
    assert dialog.keep.isChecked(); dialog.controller.setChecked(True); dialog.accept(); pump(timeout=.3)
    assert called(core, 'profiles.do_import', ('profiles.yaml', ('Imported',), True, True))
    page.export_profiles(); pump(); page.checklist.accept(); pump(timeout=.3)
    assert called(core, 'profiles.export', ('profiles.yaml', ('Default', 'Gaming'), False))
    dialog = ChecklistDialog(ImportPreview('p.yaml', ('A',), True), True, page)
    dialog.select_all.setChecked(False); assert not dialog.yesButton.isEnabled()
    dialog.controller.setChecked(True); assert dialog.yesButton.isEnabled()
    dialog.reject()


def test_recorder_keyboard_controller_duplicate_and_cancel(setup_page, pump):
    page, core, _ = setup_page(ProfilesPage)
    page.record('Gaming'); pump(); recorder = page.recorder
    QTest.keyClick(recorder, Qt.Key_F3); pump(timeout=.3)
    assert called(core, 'profiles.set_profile_combo', ('Gaming', 'VK_F3'))
    assert not recorder.isVisible()
    core.duplicate = True; page.record('Gaming'); pump(); recorder = page.recorder
    core.capture(('BTN_A',)); pump(timeout=.2)
    assert recorder.isVisible() and not recorder.tokens
    core.capture(('BTN_B',)); pump(timeout=.3); assert not recorder.isVisible()
    assert called(core, 'profiles.set_profile_combo', ('Gaming', 'BTN_B'))
    page.record(); pump(); page.recorder.reject(); pump()
    assert called(core, 'profiles.set_profile_switching_combo_trigger', ('',))


def test_settings_driver_controls_and_app(setup_page, pump):
    page, core, toasts = setup_page(SettingsPage)
    for name in ('WinUHid', 'USBIP', 'HidHide'): page.driver_rows[name][2].click()
    core.driver_state = replace(core.driver_state, driver_type='ViGEmBus'); page.apply_driver(core.driver_state)
    page.driver_rows['ViGEmBus'][2].click(); page.keyboard.click(); page.mouse.click()
    page.auto_scan.setChecked(True); page.manual_scan.click(); page.hidhide.setChecked(True); page.hidhide_uninstall.click()
    page.startup.setChecked(True); page.minimized.setChecked(True); page.power.setCurrentItem('Auto'); pump()
    for method in ('on_winuhid_manager_driver_action', 'on_vigembus_driver_action', 'on_usbip_btn_clicked', 'on_hidhide_button',
                   'toggle_keyboard_output', 'toggle_mouse_output', 'toggle_wired_auto_scan', 'manual_wired_scan', 'toggle_hidhide_active'):
        assert called(core, 'drivers.' + method)
    assert called(core, 'settings.set_run_at_startup', (True,)) and called(core, 'settings.set_start_minimized', (True,))
    assert called(core, 'settings.set_power_saving_mode', ('Auto',))
    page.apply_driver(replace(core.driver_state, usbip_restart_pending=True, keyboard_available=False, mouse_available=False, busy=True))
    assert not page.restart_notice.isHidden() and page.keyboard_row.isHidden()
    assert not page.manual_scan.isEnabled()


def test_firmware_port_actions_close_and_replug(setup_page, pump):
    page, core, _ = setup_page(FirmwareManagerDialog)
    assert 'Unverified' in page.ports.itemText(1)
    page.mode.setCurrentItem('manual'); page.ports.setCurrentIndex(1); pump()
    assert called(core, 'esp32.set_port_mode', (core.ports[1],))
    for action in ('install', 'repair', 'delete'): page.start(action)
    pump()
    for action in ('install', 'repair', 'delete'): assert called(core, 'esp32.' + action, (core.ports[1],))
    core.bus.publish(events.ESP32_FIRMWARE_SESSION, FirmwareSession('flashing', 42, 'Writing', False)); pump()
    page.reject(); assert page.isVisible() and not page.close_button.isEnabled()
    assert page.progress.value() == 42
    core.bus.publish(events.ESP32_FIRMWARE_SESSION, FirmwareSession('awaiting_replug', 100, 'Reconnect', True)); pump()
    assert 'Unplug' in page.guidance.text()
    core.bus.publish(events.ESP32_FIRMWARE_SESSION, FirmwareSession('done', 100, 'Verified', True)); pump()
    assert not page.isVisible()


def test_profile_crud_app_picker_and_replacement_confirmation(setup_page, pump, monkeypatch):
    page, core, _ = setup_page(ProfilesPage)
    monkeypatch.setattr('ui_modern.pages.profiles.pick_file', lambda page, title, callback, **kw: callback('C:/Games/new.exe'))
    page.choose_app(0); pump()
    assert ('profiles.add_assigned_app', ('C:/Games/new.exe',), {'index': 0, 'profile_name': 'Default'}) in core.calls
    apply_change(page, core.profiles.add); page.active_action('Default', core.profiles.reset); page.active_action('Default', core.profiles.delete); pump()
    for method in ('add', 'reset', 'delete'): assert called(core, 'profiles.' + method)
    page.rename('Default'); dialog = page.rename_dialog; dialog.viewLayout.itemAt(1).widget().setText('Renamed')
    dialog.accept(); pump(timeout=.3)
    assert called(core, 'profiles.rename', ('Renamed',))
    page.show_import(ImportPreview('replace.yaml', ('Imported',)))
    page.checklist.keep.setChecked(False); page.checklist.accept(); pump(timeout=.3)
    assert not called(core, 'profiles.do_import')
    page.import_confirmation.accept(); pump(timeout=.3)
    assert called(core, 'profiles.do_import', ('replace.yaml', ('Imported',), False, False))


def test_file_picker_focus_guard_and_yaml_suffix(setup_page, pump, monkeypatch):
    from ui_modern.pages.profiles.dialogs import pick_file
    page, core, _ = setup_page(ProfilesPage)
    files = []
    pick_file(page, 'Export profiles', files.append, save=True)
    pump(lambda: hasattr(page, 'file_dialog'), timeout=1)
    assert called(core, 'profiles.suspend_enter')
    page.file_dialog.selectFile('test-export.txt')
    monkeypatch.setattr(page.file_dialog, 'selectedFiles', lambda: ['test-export.txt'])
    page.file_dialog.done(1)
    pump(lambda: called(core, 'profiles.suspend_exit'), timeout=1)
    assert files and files[0].endswith('test-export.txt.yaml')
    assert called(core, 'profiles.suspend_exit')


def test_recorder_mouse_and_wheel_tokens(setup_page, pump):
    from PySide6.QtCore import QPoint, QPointF
    from PySide6.QtGui import QWheelEvent
    page, core, _ = setup_page(ProfilesPage)
    page.record('Gaming'); pump(timeout=.3)
    QTest.mouseClick(page.recorder.value, Qt.RightButton); pump()
    assert called(core, 'profiles.set_profile_combo', ('Gaming', 'MB_3'))
    page.record('Gaming'); pump(timeout=.3)
    event = QWheelEvent(QPointF(10, 10), QPointF(10, 10), QPoint(), QPoint(0, 120), Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False)
    page.recorder.eventFilter(page.recorder.value, event); pump()
    assert called(core, 'profiles.set_profile_combo', ('Gaming', 'MW_UP'))


def test_firmware_errors_boot_and_absence(setup_page, pump):
    page, core, _ = setup_page(FirmwareManagerDialog)
    page.apply_state(replace(core.esp_state, transport='OTG'))
    assert not page.actions['install'].isEnabled() and 'BOOT' in page.guidance.text()
    page.apply_session(FirmwareSession('error', 10, 'Flash failed', True))
    assert page.error.text() == 'Flash failed'
    page.apply_state(Esp32State())
    page.apply_session(FirmwareSession('awaiting_replug', 100, '', True))
    page.apply_session(FirmwareSession('done', 100, '', True))
    assert page.isVisible()
    page.apply_state(replace(core.esp_state, detected=True)); pump()
    assert not page.isVisible()


def test_settings_interface_theme_tray_and_about(setup_page, pump, monkeypatch):
    from ui_modern.theme import preferences
    restarted = []; hidden = []; links = []
    page, core, toasts = setup_page(SettingsPage, request_restart=lambda: restarted.append(True), hide_to_tray=lambda: hidden.append(True))
    monkeypatch.setattr('ui_modern.pages.settings_full.QDesktopServices.openUrl', lambda url: links.append(url.toString()))
    page.tray.click(); page.kofi.click()
    assert hidden and links == ['https://ko-fi.com/tagayama']
    page.theme.setCurrentText('Dark'); pump(); assert preferences().value('theme') == 'Dark'
    toasts[-1][1]['undo'](); pump(); assert preferences().value('theme') == 'System'
    page.close_behavior.setCurrentText('Hide to tray'); pump(); assert preferences().value('close_behavior') == 'Hide to tray'
    toasts[-1][1]['undo'](); pump(); assert preferences().value('close_behavior') == 'Quit'
    page.switch_classic(); page.confirmation.accept(); pump(timeout=.3)
    assert called(core, 'settings.set_ui_mode', ('classic',))
    assert restarted
