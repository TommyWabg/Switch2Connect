"""UI-neutral dispatch and relaunch helpers (safe for classic imports)."""
import os
from pathlib import Path
import subprocess
import sys


def resolve_ui_mode(argv, config_value):
    mode = config_value if config_value in ("classic", "modern") else "classic"
    for arg in argv:
        if arg in ("--ui=modern", "--ui=classic"):
            mode = arg.split("=", 1)[1]
    return mode


def relaunch_args():
    args = [arg for arg in sys.argv[1:] if not arg.startswith("--ui=")]
    if getattr(sys, "frozen", False):
        return [sys.executable, *args]
    return [sys.executable, str(Path(__file__).resolve().parents[1] / "gui.py"), *args]


def schedule_relaunch():
    options = {"close_fds": True}
    if os.name == "nt":
        options["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    return subprocess.Popen(relaunch_args(), **options)
