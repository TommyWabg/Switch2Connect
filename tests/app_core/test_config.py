import json
from pathlib import Path
import subprocess
import sys

import pytest
import yaml


def test_pro2_desktop_source_seed_matches_importable_profile():
    root = Path(__file__).resolve().parents[2]
    preset = yaml.safe_load((root / "resources/profiles/Pro2-GL-Desktop.yaml").read_bytes())
    seed = yaml.safe_load((root / "config.yaml").read_bytes())
    assert set(preset) == {"profiles"}
    assert list(preset["profiles"]) == ["Pro 2 GL Desktop"]
    assert seed["profiles"]["Pro 2 GL Desktop"] == preset["profiles"]["Pro 2 GL Desktop"]


@pytest.mark.parametrize("value,expected", [
    (None, "modern"), ("classic", "classic"), ("modern", "modern"),
    ("invalid", "modern"), (False, "modern"), (["modern"], "modern"),
])
def test_ui_mode_load_and_round_trip(core_tmp_path, value, expected):
    tmp_path = core_tmp_path
    initial = {} if value is None else {"ui_mode": value}
    # JSON is valid YAML and needs no application imports in this test process.
    (tmp_path / "config.yaml").write_text(json.dumps(initial), encoding="utf-8")
    src = str(Path(__file__).resolve().parents[2] / "src")
    script = f"""
import sys, threading
sys.path.insert(0, {src!r})
sys._MEIPASS = {str(tmp_path)!r}
threads = []
real_thread = threading.Thread
def tracked_thread(*a, **kw):
    thread = real_thread(*a, **kw)
    threads.append(thread)
    return thread
threading.Thread = tracked_thread
import config
from app_core.helpers import MACHINE_LOCAL_CONFIG_KEYS
assert 'ui_mode' in MACHINE_LOCAL_CONFIG_KEYS
assert config.CONFIG.config_file_path == {str(tmp_path / 'config.yaml')!r}
assert config.CONFIG.ui_mode == {expected!r}
for mode in ('modern', 'classic'):
    config.CONFIG.ui_mode = mode
    config.CONFIG.save_config()
    for thread in threads:
        thread.join(5)
        assert not thread.is_alive()
    reloaded = config.Config('this_argument_is_ignored.yaml')
    assert reloaded.config_file_path == {str(tmp_path / 'config.yaml')!r}
    assert reloaded.ui_mode == mode
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_shifted_joystick_mouse_sensitivity(core_tmp_path):
    src = Path(__file__).resolve().parents[2] / "src"
    script = f"""
import sys
sys.path.insert(0, {str(src)!r})
sys._MEIPASS = {str(core_tmp_path)!r}
from config import CONFIG, MAPPING_SCOPE_IN_APP_GYRO
for mode in ("R Joystick", "Steering"):
    CONFIG.gyro_control_mode = mode
    layer = CONFIG.get_mapping_scope_dict(MAPPING_SCOPE_IN_APP_GYRO)
    for stick in ("l_joystick", "r_joystick"):
        key = stick + "_mouse_sensitivity"
        assert layer[key] == 5.0
        layer[key] = "Default"
        assert CONFIG.get_joystick_setting_scoped(stick, "mouse_sensitivity", scope=MAPPING_SCOPE_IN_APP_GYRO) == 5.0
        CONFIG.set_joystick_setting_scoped(stick, "mouse_sensitivity", 0.0, MAPPING_SCOPE_IN_APP_GYRO)
        assert CONFIG.get_joystick_setting_scoped(stick, "mouse_sensitivity", scope=MAPPING_SCOPE_IN_APP_GYRO) == 0.0
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
