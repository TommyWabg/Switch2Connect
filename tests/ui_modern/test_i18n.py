from dataclasses import replace
from string import Formatter
import json

from PySide6.QtWidgets import QDialogButtonBox
from qfluentwidgets import TitleLabel

from app_core.models import Option, OptionCategory
from conftest import FakeCore
from ui_modern import i18n
from ui_modern.main_window import MainWindow
from ui_modern.resources import resource_path
from ui_modern.widgets.display import input_display
from ui_modern.widgets.pickers import OptionPicker


def test_language_resolution():
    assert i18n.resolve_language("system", ["zh-Hans-CN"]) == "zh_CN"
    assert i18n.resolve_language("system", ["de-DE"]) == "en"
    assert i18n.resolve_language("en", ["zh-CN"]) == "en"
    assert i18n.resolve_language("zh_CN", ["en-US"]) == "zh_CN"


def test_catalog_preserves_format_fields():
    catalog = json.loads(resource_path("resources/i18n/zh_CN.json").read_text(encoding="utf-8"))
    def fields(text):
        return sorted(name for _, name, _, _ in Formatter().parse(text) if name)
    for source, translation in catalog.items():
        assert translation.strip() and fields(source) == fields(translation), source


def test_chinese_settings_and_lazy_pages(app, pump, isolated_preferences):
    prefs = isolated_preferences
    prefs.setValue("language", "zh_CN")
    i18n.initialize(app)
    core = FakeCore(scenario="three")
    core.profiles_state = replace(core.profiles_state, active="Settings", names=("Settings",))
    window = MainWindow(core)
    try:
        pump()
        assert window.devices.findChild(TitleLabel).text() == "设备"
        assert window.devices.cards[0].title.text() == "玩家 1"
        assert window.status.text() == "● 就绪 · ESP32-S3"
        assert window.profile.text() == "Settings"
        for name, title in (("mapping", "按键映射"), ("gyro", "陀螺仪"), ("rumble", "振动"), ("profiles", "配置方案")):
            window.navigate(name)
            assert window.pages[name].findChild(TitleLabel).text() == title
        assert input_display("Custom[Tap]:VK_CONTROL+MB_1") == "Ctrl + 鼠标左键"
        options = (OptionCategory("General", ((Option("Default", "Default"), Option("None", "None")),)),)
        chosen = []
        picker = OptionPicker(options, choose=chosen.append, parent=window)
        picker.show_at(window.profile)
        picker.search.setText("默认")
        assert picker.buttons["Default"].isEnabled()
        assert not picker.buttons["None"].isEnabled()
        picker.buttons["Default"].click()
        assert chosen == ["Default"]
        assert i18n.tr("Unrecognized diagnostic") == "Unrecognized diagnostic"
        assert QDialogButtonBox(QDialogButtonBox.Close).button(QDialogButtonBox.Close).text() != "Close"
        page = window.settings
        assert page.language.currentText() == "简体中文"
        page.close_behavior.setCurrentText("最小化到托盘")
        pump(lambda: prefs.value("close_behavior") == "Hide to tray")
        assert prefs.value("close_behavior") == "Hide to tray"
        page.language.setCurrentText("English")
        assert prefs.value("language") == "en"
        assert not page.restart_language.isHidden()
        restarted = []
        window.quit_requested.connect(restarted.append)
        page.restart_language.click()
        assert restarted == [True]
    finally:
        window.dispose()
        window.deleteLater()
        prefs.setValue("language", "en")
        i18n.initialize(app)
    assert i18n.tr("Settings") == "Settings"
