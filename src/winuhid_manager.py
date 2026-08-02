"""Standalone WinUHid Driver Manager.

This executable intentionally does not import the main Switch 2 Connect GUI. It
only manages the separately distributed WinUHid driver package and verifies the
resulting Windows device state.
"""

import ctypes
import logging
import os
import sys
import tkinter as tk
from tkinter import messagebox
from ctypes import wintypes

from driver_install_helper import (
    WINUHID_HEALTHY,
    WINUHID_PARTIAL,
    get_winuhid_status,
    invalidate_driver_status_cache,
)


logger = logging.getLogger("WinUHid_Manager")
POPUP_BACKGROUND = "#1E1E1E"
POPUP_BUTTON = "#4B4B4B"
POPUP_TEXT = "#FFFFFF"
POPUP_DETAILS = "#AAAAAA"
SEE_MASK_NOCLOSEPROCESS = 0x00000040
SEE_MASK_NOASYNC = 0x00000100
SW_HIDE = 0
WAIT_TIMEOUT = 0x00000102


class SHELLEXECUTEINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("fMask", ctypes.c_ulong),
        ("hwnd", wintypes.HWND),
        ("lpVerb", wintypes.LPCWSTR),
        ("lpFile", wintypes.LPCWSTR),
        ("lpParameters", wintypes.LPCWSTR),
        ("lpDirectory", wintypes.LPCWSTR),
        ("nShow", ctypes.c_int),
        ("hInstApp", wintypes.HINSTANCE),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", wintypes.LPCWSTR),
        ("hkeyClass", wintypes.HKEY),
        ("dwHotKey", wintypes.DWORD),
        ("hIconOrMonitor", wintypes.HANDLE),
        ("hProcess", wintypes.HANDLE),
    ]


def package_root():
    root = os.path.abspath(getattr(sys, "_MEIPASS", os.path.dirname(__file__)))
    if os.path.basename(root).lower() == "src":
        root = os.path.dirname(root)
    return root


def driver_file(name):
    return os.path.join(package_root(), "drivers", name)


def scale_font(font_tuple):
    """Match gui.py's physical-pixel font scaling for the driver popup."""
    family, size = font_tuple[0], font_tuple[1]
    weight = font_tuple[2] if len(font_tuple) > 2 else ""
    return (family, -max(8, int(size * (96.0 / 72.0))), weight)


def center_window(window, width, height):
    """Center a standalone popup using the same 450x130 geometry as gui.py."""
    window.update_idletasks()
    screen_w = window.winfo_screenwidth()
    screen_h = window.winfo_screenheight()
    x = max(0, int((screen_w - width) / 2))
    y = max(0, int((screen_h - height) / 2))
    window.geometry(f"{width}x{height}+{x}+{y}")


def powershell_arguments(script_path):
    escaped = script_path.replace('"', '`"')
    return f'-NoProfile -ExecutionPolicy Bypass -File "{escaped}"'


def launch_elevated(script_path):
    """Launch a bundled PowerShell script with UAC and return its process handle."""
    if not os.path.isfile(script_path):
        raise FileNotFoundError(script_path)
    info = SHELLEXECUTEINFO()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC
    info.lpVerb = "runas"
    info.lpFile = "powershell.exe"
    info.lpParameters = powershell_arguments(script_path)
    info.lpDirectory = os.path.dirname(script_path)
    info.nShow = SW_HIDE
    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(info)):
        raise ctypes.WinError(ctypes.get_last_error())
    return info.hProcess


class WinUHidManager:
    def __init__(self, root):
        self.root = root
        self.root.title("WinUHid Driver Manager")
        self.root.config(bg=POPUP_BACKGROUND)
        self.root.tk.call("tk", "scaling", 1.3333333333333333)
        center_window(self.root, 450, 180)
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self.root.destroy)

        self.status_label = tk.Label(
            root, text="", justify=tk.CENTER, anchor="center",
            bg=POPUP_BACKGROUND, fg=POPUP_TEXT,
            font=scale_font(("Arial", 11, "bold")),
        )
        self.status_label.pack(fill=tk.X, padx=16, pady=(12, 4))
        self.details_label = tk.Label(
            root, text="", justify=tk.CENTER, anchor="center",
            wraplength=410, bg=POPUP_BACKGROUND, fg=POPUP_DETAILS,
            font=scale_font(("Arial", 9)),
        )
        self.details_label.pack(fill=tk.X, padx=16, pady=(0, 8))
        button_frame = tk.Frame(root, bg=POPUP_BACKGROUND)
        button_frame.pack(pady=2)
        self.action_button = tk.Button(
            button_frame, text="", width=22,
            bg=POPUP_BUTTON, fg=POPUP_TEXT, activebackground=POPUP_BUTTON,
            activeforeground=POPUP_TEXT, bd=0, relief=tk.FLAT,
            font=scale_font(("Arial", 10, "bold")), command=self.on_action,
        )
        self.action_button.pack(side=tk.LEFT, padx=3)
        self.close_button = tk.Button(
            button_frame, text="Close", width=10,
            bg=POPUP_BUTTON, fg=POPUP_TEXT, activebackground=POPUP_BUTTON,
            activeforeground=POPUP_TEXT, bd=0, relief=tk.FLAT,
            font=scale_font(("Arial", 10, "bold")), command=self.root.destroy,
        )
        self.close_button.pack(side=tk.LEFT, padx=3)
        self.refresh()

    def refresh(self):
        status = get_winuhid_status(use_cache=False)
        self._status = status
        if status.state == WINUHID_HEALTHY:
            self.status_label.config(text="WinUHid Driver: Installed and Ready")
            self.action_button.config(text="Uninstall WinUHid Driver", state=tk.NORMAL)
        elif status.state == WINUHID_PARTIAL:
            self.status_label.config(text="WinUHid Driver: Repair Required")
            self.action_button.config(text="Repair WinUHid Driver", state=tk.NORMAL)
        else:
            self.status_label.config(text="WinUHid Driver: Not Installed")
            self.action_button.config(text="Install WinUHid Driver", state=tk.NORMAL)
        self.details_label.config(text=status.describe() or "Administrator approval is required for driver changes.")

    def on_action(self):
        status = self._status
        if status.state == WINUHID_HEALTHY:
            if not messagebox.askyesno(
                "Uninstall WinUHid Driver",
                "Uninstall WinUHid and remove its trusted driver certificate?\n\n"
                "Administrator approval is required.", parent=self.root):
                return
            script = driver_file("uninstall_driver.ps1")
            action = "uninstall"
        else:
            title = "Repair WinUHid Driver" if status.state == WINUHID_PARTIAL else "Install WinUHid Driver"
            if not messagebox.askyesno(
                title,
                "WinUHid will install a virtual HID driver and add its driver certificate "
                "to the Windows trusted stores.\n\nAdministrator approval is required.\n\nContinue?",
                parent=self.root):
                return
            script = driver_file("install_driver.ps1")
            action = "install"
        self.run_script(script, action)

    def run_script(self, script, action):
        self.action_button.config(state=tk.DISABLED)
        self.close_button.config(state=tk.DISABLED)
        self.progress_win = tk.Toplevel(self.root)
        self.progress_win.title("Driver Installation" if action == "install" else "Driver Uninstallation")
        self.progress_win.geometry("450x130")
        self.progress_win.resizable(False, False)
        self.progress_win.config(bg=POPUP_BACKGROUND)
        self.progress_win.transient(self.root)
        self.progress_win.grab_set()
        center_window(self.progress_win, 450, 130)
        tk.Label(
            self.progress_win,
            text=("Installing" if action == "install" else "Uninstalling")
                 + " WinUHid Driver...\nPlease authorize the UAC prompt if asked.",
            fg=POPUP_TEXT, bg=POPUP_BACKGROUND,
            font=scale_font(("Arial", 11, "bold")),
        ).pack(pady=40)
        try:
            handle = launch_elevated(script)
        except Exception as exc:
            self.finish(action, None, f"Could not start the elevated installer: {exc}")
            return
        self.poll_process(handle, action)

    def poll_process(self, handle, action):
        result = ctypes.windll.kernel32.WaitForSingleObject(handle, 0)
        if result == WAIT_TIMEOUT:
            self.root.after(200, lambda: self.poll_process(handle, action))
            return
        exit_code = wintypes.DWORD()
        ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
        ctypes.windll.kernel32.CloseHandle(handle)
        invalidate_driver_status_cache("winuhid")
        status = get_winuhid_status(use_cache=False)
        success = exit_code.value == 0 and (
            status.state == WINUHID_HEALTHY if action == "install" else status.state == "absent"
        )
        self.finish(action, success, status.describe(), exit_code.value)

    def finish(self, action, success, details, exit_code=None):
        progress_win = getattr(self, "progress_win", None)
        if progress_win is not None and progress_win.winfo_exists():
            try:
                progress_win.grab_release()
            except tk.TclError:
                pass
            progress_win.destroy()
        self.progress_win = None
        self.action_button.config(state=tk.NORMAL)
        self.close_button.config(state=tk.NORMAL)
        self.refresh()
        if success:
            messagebox.showinfo(
                "Success",
                "WinUHid driver " + ("installed" if action == "install" else "uninstalled")
                + " successfully.", parent=self.root)
        else:
            suffix = f"\n\nExit code: {exit_code}" if exit_code is not None else ""
            messagebox.showerror("Error", "WinUHid operation failed." + suffix
                                 + (f"\n\n{details}" if details else ""), parent=self.root)


def main():
    if os.name != "nt":
        raise SystemExit("WinUHid Manager requires Windows.")
    logging.basicConfig(level=logging.INFO)
    root = tk.Tk()
    WinUHidManager(root)
    root.mainloop()


if __name__ == "__main__":
    main()
