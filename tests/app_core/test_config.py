import json
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize("value,expected", [
    (None, "classic"), ("classic", "classic"), ("modern", "modern"),
    ("invalid", "classic"), (False, "classic"), (["modern"], "classic"),
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
