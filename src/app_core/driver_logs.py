"""Classic driver operation diagnostics."""
import ctypes
import os

def ps_hidden_args(script_path):
    """PowerShell args that run a script with no visible console window."""
    return f'-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "{script_path}"'

def read_winuhid_operation_log(operation="uninstall"):
    filename = ("Switch2Connect_WinUHid_install.log" if operation == "install"
                else "Switch2Connect_WinUHid_uninstall.log")
    log_path = os.path.join(os.environ.get("TEMP", ""), filename)
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as stream:
            content = stream.read().strip()
        lines = content.splitlines()
        keywords = ("error", "failed", "incomplete", "not recognized",
                    "could not be resolved", "fallback")
        important = [line for line in lines if any(word in line.lower() for word in keywords)]
        # Preserve order while suppressing repeated PowerShell command-not-found
        # blocks that otherwise fill the application error dialog.
        summary = []
        for line in important or lines:
            if line not in summary:
                summary.append(line)
        return "\n".join(summary[-6:])[-900:] + f"\nLog: {log_path}"
    except Exception:
        return f"Operation log was not available.\nLog: {log_path}"

def read_vigembus_uninstall_log():
    log_path = os.path.join(os.environ.get("TEMP", ""), "Switch2Connect_ViGEmBus_uninstall.log")
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as stream:
            lines = stream.read().strip().splitlines()
        keywords = ("error", "failed", "incomplete", "does not exist")
        important = [line for line in lines if any(word in line.lower() for word in keywords)]
        return "\n".join((important or lines)[-12:])[-1400:]
    except Exception:
        return "Uninstaller log was not available."



def launch_elevated(lp_file, lp_params, hwnd, prepare_foreground, lp_dir=None, hide=True):
    """Launch a process elevated (UAC runas), bringing the consent prompt to the
    foreground and hiding the child window. Used for every driver install/uninstall
    so the PowerShell console never pops up (progress is shown in the app's own
    dialog) and the UAC prompt doesn't just flash in the taskbar.
    Returns hProcess (int) or None if the launch failed / UAC was declined.
    """
    from .helpers import SHELLEXECUTEINFOW
    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x00000040
    info.hwnd = hwnd
    info.lpVerb = "runas"
    info.lpFile = lp_file
    info.lpParameters = lp_params
    info.lpDirectory = lp_dir
    info.nShow = 0 if hide else 1  # SW_HIDE vs SW_SHOWNORMAL
    # Grant foreground rights + drop any modal grab so the UAC consent prompt comes
    # to the front instead of only flashing in the taskbar.
    try:
        prepare_foreground()
        ctypes.windll.user32.AllowSetForegroundWindow(-1)  # ASFW_ANY
    except Exception:
        pass
    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(info)):
        return None
    return info.hProcess
