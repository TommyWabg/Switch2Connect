from dataclasses import replace

from PySide6.QtGui import QGuiApplication, QCursor
from PySide6.QtWidgets import QWidget
from qfluentwidgets import Theme, setTheme

from app_core import events
from app_core.models import SlotInfo
from ui_modern.main_window import MainWindow
from ui_modern.widgets.common import SettingRow, ExpandSettingRow
from conftest import FakeCore


def test_empty_state_transitions_keep_cards(window, pump):
    w, core = window
    page = w.devices
    cards = dict(page.cards)
    core.bus.publish(events.CONTROLLERS_CHANGED, tuple(SlotInfo(i, i+1) for i in range(4)))
    pump()
    assert page.controller_stack.currentWidget() is page.empty
    assert page.connection_route.text() == core.status.connection_route
    assert page.pair_image.height() == 200 and page.pair_image.width() > 400
    assert not page.pair_image.pixmap.isNull()
    core.bus.publish(events.CONTROLLERS_CHANGED, core.slots[:1])
    pump()
    assert page.controller_stack.currentWidget() is page.card_panel
    assert page.cards == cards
    assert page.cards[1].model.text()
    assert not page.cards[2].model.text()
    assert len({card.height() for card in cards.values()}) == 1
    assert page.cards[0].battery.isVisible()
    assert page.cards[0].transport.text() == 'Bluetooth'


def test_inline_warning_and_gyro_copy(window, pump):
    w, core = window
    page = w.devices
    page.apply_driver(replace(core.driver_state, needs_attention=True))
    pump()
    assert page.attention.isVisible()
    assert page.attention.titleLabel.isVisible()
    assert page.attention.titleLabel.text() == "WinUHid driver isn't installed"
    assert 'unavailable' in page.attention.contentLabel.text()
    assert page.driver_action.text() == 'Install'
    page.apply_driver(replace(core.driver_state, needs_attention=True, action_kind='repair'))
    assert page.driver_action.text() == 'Repair'
    page.apply_driver(core.driver_state)
    assert not page.attention.isVisible()
    assert page.gyro_row.titleLabel.text() == 'Calibrate gyro'
    assert page.gyro_status.text() == 'Place controllers on a flat surface and keep them still'
    assert page.gyro_button.text() == 'Calibrate'


def test_setting_row_alignment_and_disclosure(app, pump):
    control = QWidget(); control.setFixedSize(100, 30)
    row = SettingRow('Title', 'Two-line setting', control)
    row.resize(600, 64); row.show(); pump()
    assert abs(control.geometry().center().y() - row.rect().center().y()) <= 1
    assert row.titleLabel.font().pixelSize() == 14
    assert row.captionLabel.font().pixelSize() == 12
    row.close()
    disclosure = ExpandSettingRow('Advanced')
    content = QWidget(); disclosure.addWidget(content)
    assert disclosure.body.isHidden()
    disclosure.setExpand(True)
    assert not disclosure.body.isHidden()
    disclosure.setExpand(False)
    assert disclosure.body.isHidden()
    assert disclosure.viewLayout.itemAt(0).widget() is content
    disclosure.deleteLater()


def test_window_placement_and_hidden_persistence(app, pump, isolated_preferences, monkeypatch):
    prefs = isolated_preferences
    screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
    w = MainWindow(FakeCore())
    assert (w.geometry().center() - screen.availableGeometry().center()).manhattanLength() <= 2
    assert not w.windowIcon().isNull()
    assert w.titleBar.iconLabel.width() == 16
    w.show(); pump(); w.save_placement()
    saved = prefs.value('geometry')
    assert saved is not None
    w.hide(); w.move(99999, 99999); w.save_placement()
    assert prefs.value('geometry') == saved
    # qframelesswindow calls Win32 on state changes; offscreen has no HWND.
    w.show(); pump()
    with monkeypatch.context() as patch:
        patch.setattr(w, "isMinimized", lambda: True)
        w.save_placement()
    assert prefs.value('geometry') == saved
    w.hide(); w.move(99999, 99999)
    prefs.setValue('geometry', w.saveGeometry())
    restored = MainWindow(FakeCore())
    assert any(restored.frameGeometry().intersects(s.availableGeometry()) for s in QGuiApplication.screens())
    for window in (w, restored):
        window.bridge.close(); window.hide(); window.deleteLater()
    pump()


def test_theme_art_and_real_options(window, pump):
    w, core = window
    assert dict(core.driver_state.mode_options) == {
        'WinUHid': ('Xbox One', 'PS4', 'PS5'),
        'ViGEmBus': ('Xbox360', 'PS4'),
        'USBIP': ('Switch1', 'Switch2', 'PS5'),
    }
    for theme in (Theme.DARK, Theme.LIGHT):
        setTheme(theme); pump()
        assert not w.devices.pair_image.pixmap.isNull()
        assert w.devices.cards[0].illustration.renderer().isValid()
    w.devices.apply_slots((replace(core.slots[0], controllers=(replace(core.slots[0].controllers[0], battery_level=None),)),))
    assert w.devices.cards[0].battery.levels == (None,)
