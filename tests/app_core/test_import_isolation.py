from pathlib import Path
import subprocess
import sys


def test_imports_do_not_load_ui_toolkits(core_tmp_path):
    tmp_path = core_tmp_path
    # Config's constructor ignores its path argument and resolves the app root.
    # Redirect that root before any imports so no user configuration is touched.
    (tmp_path / "config.yaml").write_text("{}", encoding="utf-8")
    src = str(Path(__file__).resolve().parents[2] / "src")
    script = f"""
import sys
sys.path.insert(0, {src!r})
sys._MEIPASS = {str(tmp_path)!r}
import app_core, pkgutil, importlib
for module in pkgutil.walk_packages(app_core.__path__, app_core.__name__ + "."):
    importlib.import_module(module.name)
import app_core.runtime, app_core.snapshots, app_core.services.players
import app_core.bus, app_core.events, app_core.models, app_core.executor, app_core.ui_port
assert not any(n in sys.modules for n in ('gui', 'config', 'controller', 'virtual_controller', 'gyro'))
assert not any(n == 'tkinter' or n.startswith('tkinter.') for n in sys.modules)
assert not any(n == 'PySide6' or n.startswith('PySide6.') for n in sys.modules)
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
