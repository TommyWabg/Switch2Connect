from dataclasses import replace
from qfluentwidgets import SwitchButton
from app_core import events
from app_core.models import CalibrationSession
from ui_modern.widgets.display import input_display


def test_driver_banner_hidden_when_healthy_or_pending(window, pump):
    host, core = window
    page = host.pages['devices']
    healthy = replace(core.driver_state, action_kind='uninstall', needs_attention=False)
    core.bus.publish(events.DRIVER_STATE_CHANGED, healthy)
    pump()
    assert page.attention.isHidden()
    pending = replace(healthy, pending_check=True, action_kind='install')
    core.bus.publish(events.DRIVER_STATE_CHANGED, pending)
    pump()
    assert page.attention.isHidden()
    absent = replace(pending, pending_check=False, needs_attention=True)
    core.bus.publish(events.DRIVER_STATE_CHANGED, absent)
    pump()
    assert not page.attention.isHidden()


def test_pages_built_once_and_shared_input_display(window, pump):
    window, core = window
    for name in ('mapping', 'gyro', 'rumble', 'profiles'):
        window.navigate(name); pump()
        page = window.pages[name].content_page
        window.navigate('devices'); window.navigate(name); pump()
        assert window.pages[name].content_page is page
        assert page.objectName() == name
    assert input_display('CONTROL+KBA') == 'Ctrl + A'
    assert input_display('Custom[Tap]:VK_CONTROL+VK_F2+MB_1+BTN_A') == 'Ctrl + F2 + Left click + A'
    assert input_display('m2 Middle Click') == 'Middle click'
    assert input_display('VK_RETURN+VK_BACK+VK_KP_5+MW_LEFT') == 'Enter + Backspace + Numpad 5 + Wheel left'
    assert input_display('KB Arrow Keys') == 'Keyboard arrow keys'
    for switch in window.findChildren(SwitchButton):
        assert not switch.onText and not switch.offText


def test_toast_routing_profile_overlay_and_shutdown(window, pump, monkeypatch):
    window, core = window
    monkeypatch.setattr(window, 'isActiveWindow', lambda: True)
    core.bus.publish(events.TOAST, events.Toast('Foreground')); pump()
    assert window._toast.content == 'Foreground'
    assert not window.overlays.notification.isVisible()
    window.hide()
    core.bus.publish(events.TOAST, events.Toast('Background', auto_close_seconds=None)); pump()
    assert window.overlays.notification.message.text() == 'Background'
    assert window.overlays.notification.isVisible()
    core.bus.publish(events.PROFILE_OVERLAY, events.ProfileOverlayEvent('show', 'Default', 'Gaming', 'Racing', manual=True)); pump()
    assert window.overlays.profile.manual_active
    window.dispose()
    assert not window.overlays.notification.isVisible()


def test_calibration_event_opens_wizard_without_starting_again(window, pump):
    window, core = window
    session = CalibrationSession('from-core', 'joystick', (0, 100), 'rotate', 'Joystick calibration', 'Keep rotating')
    core.calibration.state = lambda: __import__('app_core.models', fromlist=['CalibrationState']).CalibrationState((session,))
    core.bus.publish(events.CALIBRATION_SESSION, session); pump()
    assert 'from-core' in window.overlays.wizards
    assert not any(name == 'calibration.start_joystick' for name, _, _ in core.calls)
    dialog = window.overlays.wizards['from-core']; dialog.reject(); pump()
    assert any(name == 'calibration.cancel' for name, _, _ in core.calls)


def test_close_to_tray_and_unbounded_duration(window, pump, isolated_preferences):
    window, core = window
    isolated_preferences.setValue('close_behavior', 'Hide to tray')
    quit_requests = []; window.quit_requested.connect(quit_requests.append)
    window.close(); pump()
    assert not window.isVisible() and not quit_requests
    for spin, value in zip(window.devices.duration_spins, (999999, 48, 120)):
        spin.setValue(value); pump()
    assert ('settings.set_auto_disconnect_time', (999999, 48, 120), {}) in core.calls


def test_startup_switch(app, pump):
    from test_overlays import T11Core
    from ui_modern.pages.settings_full import SettingsPage
    core = T11Core()
    page = SettingsPage(core, lambda *a, **kw: None); page.show(); pump()
    page.startup.setChecked(True); pump()
    assert page.startup.isChecked()
    assert any(name == 'settings.set_run_at_startup' for name, _, _ in core.calls)
    page.close(); page.bridge.close(); pump()


def test_inactive_profile_text_follows_theme(window, pump):
    from qfluentwidgets import Theme, setTheme
    window, core = window
    window.navigate('profiles'); pump()
    page = window.pages['profiles'].content_page
    setTheme(Theme.DARK); pump()
    assert '#ffffff' in page.profile_rows['Gaming'][1].styleSheet()
    setTheme(Theme.LIGHT); pump()
    assert '#202020' in page.profile_rows['Gaming'][1].styleSheet()


def test_source_and_frozen_asset_paths(monkeypatch, core_tmp_path):
    import sys
    from ui_modern.resources import resource_path
    assert resource_path('resources/images/icon.ico').is_file()
    monkeypatch.setattr(sys, '_MEIPASS', str(core_tmp_path), raising=False)
    assert resource_path('resources/images/pair.svg') == core_tmp_path / 'resources/images/pair.svg'
