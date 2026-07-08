# Switch2Connect - A Python and ESP32-S3 bridge utility for Switch 2 controller inputs.
# Copyright (C) 2026 TommyWabg
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# Contact Information:
# Electronic Mail: tommyw9318@gmail.com

import os
import sys
import ctypes
import re
import subprocess
import winreg
from tkinter import messagebox, Tk

def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except:
        return False

def run_uninstall():
    # Hide the main Tkinter root window
    root = Tk()
    root.withdraw()
    
    # 0. Remove ViGEmBus device node
    try:
        subprocess.run("pnputil /remove-device /deviceid \"Root\\ViGEmBus\"", shell=True)
    except Exception:
        pass
        
    # 1. Search registry for ViGEmBus
    uninstall_keys = [
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
        r"SOFTWARE\Wow6432Node\Microsoft\Windows\CurrentVersion\Uninstall"
    ]
    
    found_any = False
    for path in uninstall_keys:
        try:
            key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path)
            subkeys_count, _, _ = winreg.QueryInfoKey(key)
            for i in range(subkeys_count):
                try:
                    subkey_name = winreg.EnumKey(key, i)
                    subkey = winreg.OpenKey(key, subkey_name)
                    try:
                        display_name, _ = winreg.QueryValueEx(subkey, "DisplayName")
                        if "ViGEm" in display_name or "Virtual Gamepad Emulation Bus" in display_name:
                            found_any = True
                            # Check if we can find uninstall string or GUID
                            uninstall_string, _ = winreg.QueryValueEx(subkey, "UninstallString")
                            match = re.search(r"\{[0-9a-fA-F\-]+\}", uninstall_string)
                            if match:
                                guid = match.group(0)
                                subprocess.run(f"msiexec.exe /X{guid} /qb", shell=True, check=True)
                            else:
                                # Run uninstall string directly
                                subprocess.run(uninstall_string, shell=True, check=True)
                    except FileNotFoundError:
                        pass
                    finally:
                        winreg.CloseKey(subkey)
                except Exception:
                    pass
            winreg.CloseKey(key)
        except FileNotFoundError:
            pass

    # 2. Cleanup using pnputil
    try:
        result = subprocess.run("pnputil /enum-drivers", capture_output=True, text=True, shell=True)
        if result.returncode == 0:
            drivers_output = result.stdout
            oem_infs = []
            # Parse output chunks
            chunks = re.split(r'\r?\n\r?\n', drivers_output)
            for chunk in chunks:
                if "vigembus.inf" in chunk.lower():
                    # Find Published name (language independent)
                    match = re.search(r"\b(oem\d+\.inf)\b", chunk, re.IGNORECASE)
                    if match:
                        oem_infs.append(match.group(1))
            
            for inf in oem_infs:
                subprocess.run(f"pnputil /delete-driver {inf} /uninstall /force", shell=True)
    except Exception:
        pass

    # 3. Clean up service and registry service key
    try:
        subprocess.run("sc.exe delete ViGEmBus", shell=True)
    except Exception:
        pass
        
    try:
        key_path = r"SYSTEM\CurrentControlSet\Services\ViGEmBus"
        winreg.DeleteKey(winreg.HKEY_LOCAL_MACHINE, key_path)
    except Exception:
        pass

    # Show completion message
    messagebox.showinfo(
        "ViGEmBus Uninstaller",
        "ViGEmBus has been successfully uninstalled from your system.\n\nA system reboot is highly recommended."
    )

if __name__ == "__main__":
    if not is_admin():
        # Re-run with admin privileges
        ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, " ".join(sys.argv), None, 1)
        sys.exit(0)
    
    run_uninstall()
