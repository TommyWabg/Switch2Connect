# Switch2Connect - Driver download helper for one-click / packaged (MSIX) installation.
# Copyright (C) 2026 TommyWabg
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This module centralizes the download sources and silent-install commands for
# the external drivers the app relies on. It is used by:
#   * ViGEmBus one-click install (both the standalone .exe build and MSIX build).
#   * The MSIX-packaged build, which cannot ship driver installers inside the
#     package, so it downloads them from the project GitHub drivers folder
#     (and ViGEmBus from nefarius' official release) and installs them at runtime.
#
# The standalone .exe build still installs WinUHid / USBIP / HIDHide from the
# bundled files; only ViGEmBus is routed through here for both builds.

import os
import re
import subprocess
import tempfile
import urllib.request
from dataclasses import dataclass

# Raw base for files hosted in the project's own drivers/ folder on GitHub.
GITHUB_DRIVERS_RAW = "https://raw.githubusercontent.com/TommyWabg/Switch2Connect/main/drivers"

# ViGEmBus is not in the project repo; use nefarius' official signed release.
VIGEMBUS_URL = (
    "https://github.com/nefarius/ViGEmBus/releases/download/v1.22.0/"
    "ViGEmBus_1.22.0_x64_x86_arm64.exe"
)

# The usbip-win2 installer is not committed to the project drivers/ folder; use
# the upstream vadimgrn/usbip-win2 official release asset.
USBIP_EXE_URL = (
    "https://github.com/vadimgrn/usbip-win2/releases/download/v.0.9.7.7/"
    "USBip-0.9.7.7-x64.exe"
)


class DriverSpec:
    """Describes how to download and silently install one driver.

    files:   list of (url, filename) downloaded into a shared temp folder.
    run:     ("exe", filename, params) to run an installer directly, or
             ("ps1", filename) to run a PowerShell script (from the temp folder).
             The command is executed elevated (UAC) by the GUI layer.
    """

    def __init__(self, display_name, files, run):
        self.display_name = display_name
        self.files = files
        self.run = run


WINUHID_HEALTHY = "healthy"
WINUHID_PARTIAL = "partial"
WINUHID_ABSENT = "absent"
VIGEMBUS_HEALTHY = "healthy"
VIGEMBUS_PARTIAL = "partial"
VIGEMBUS_ABSENT = "absent"


@dataclass(frozen=True)
class WinUHidStatus:
    """Observed WinUHid kernel-driver state.

    A stale ROOT device is deliberately not considered an installation.  The
    driver is usable only when its package, WUDF service registration, and a
    present ROOT device all exist.
    """

    state: str
    device_instances: tuple = ()
    present_instances: tuple = ()
    driver_packages: tuple = ()
    registry_exists: bool = False
    errors: tuple = ()

    @property
    def installed(self):
        return self.state == WINUHID_HEALTHY

    @property
    def absent(self):
        return self.state == WINUHID_ABSENT

    def describe(self):
        parts = []
        if self.device_instances and not self.present_instances:
            parts.append("stale/disconnected device node: " + ", ".join(self.device_instances))
        elif self.present_instances:
            parts.append("present device node: " + ", ".join(self.present_instances))
        if self.driver_packages:
            parts.append("Driver Store package: " + ", ".join(self.driver_packages))
        else:
            parts.append("Driver Store package missing")
        parts.append("WUDF service registry present" if self.registry_exists else "WUDF service registry missing")
        if self.errors:
            parts.append("query errors: " + "; ".join(self.errors))
        return "\n".join(parts)


def _run_pnputil(args):
    return subprocess.run(
        ["pnputil", *args], capture_output=True, text=True, errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
    )


def _parse_winuhid_devices(output):
    """Return (all instances, present instances) using locale-stable PnP keys."""
    instances = tuple(dict.fromkeys(
        match.upper() for match in re.findall(r"ROOT\\WINUHID\\[^\s\r\n]+", output, re.IGNORECASE)
    ))
    present = []
    # Anchor sections on the locale-stable property key. Instance IDs can also
    # appear in a previous device's Siblings list, so a plain first-occurrence
    # search incorrectly associates a later present node with the stale node.
    property_sections = re.finditer(
        r"DEVPKEY_Device_InstanceId[^\r\n]*[\r\n]+\s*"
        r"(ROOT\\WINUHID\\[^\s\r\n]+)(.*?)"
        r"(?=DEVPKEY_Device_InstanceId|\Z)",
        output, re.IGNORECASE | re.DOTALL,
    )
    for section in property_sections:
        instance = section.group(1).upper()
        match = re.search(
            r"DEVPKEY_Device_IsPresent[^\r\n]*[\r\n]+\s*(TRUE|FALSE)",
            section.group(2), re.IGNORECASE,
        )
        if match and match.group(1).upper() == "TRUE":
            present.append(instance)
    return instances, tuple(present)


def _parse_winuhid_driver_packages(output):
    packages = []
    for block in re.split(r"(?:\r?\n){2,}", output):
        if "winuhiddriver.inf" not in block.lower():
            continue
        match = re.search(r"\boem\d+\.inf\b", block, re.IGNORECASE)
        if match:
            packages.append(match.group(0).lower())
    return tuple(dict.fromkeys(packages))


def get_winuhid_status(pnputil_runner=None, registry_checker=None):
    """Inspect all installation layers without treating a phantom node as healthy."""
    runner = pnputil_runner or _run_pnputil
    errors = []
    device_instances = ()
    present_instances = ()
    driver_packages = ()

    try:
        result = runner(["/enum-devices", "/deviceid", r"Root\WinUHid", "/properties"])
        device_instances, present_instances = _parse_winuhid_devices(result.stdout or "")
        if result.returncode not in (0, 259) and not device_instances:
            errors.append(f"device query exit code {result.returncode}")
    except Exception as exc:
        errors.append(f"device query failed: {exc}")

    try:
        result = runner(["/enum-drivers"])
        driver_packages = _parse_winuhid_driver_packages(result.stdout or "")
        if result.returncode != 0:
            errors.append(f"driver query exit code {result.returncode}")
    except Exception as exc:
        errors.append(f"driver query failed: {exc}")

    if registry_checker is None:
        def registry_checker():
            import winreg
            for sam in (winreg.KEY_READ, winreg.KEY_READ | winreg.KEY_WOW64_64KEY):
                try:
                    key = winreg.OpenKey(
                        winreg.HKEY_LOCAL_MACHINE,
                        r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\WUDF\Services\WinUHidDriver",
                        0, sam,
                    )
                    winreg.CloseKey(key)
                    return True
                except FileNotFoundError:
                    continue
            return False
    try:
        registry_exists = bool(registry_checker())
    except Exception as exc:
        registry_exists = False
        errors.append(f"registry query failed: {exc}")

    if present_instances and driver_packages and registry_exists and not errors:
        state = WINUHID_HEALTHY
    elif not present_instances and not driver_packages and not registry_exists and not errors:
        state = WINUHID_ABSENT
    else:
        state = WINUHID_PARTIAL
    return WinUHidStatus(
        state=state,
        device_instances=device_instances,
        present_instances=present_instances,
        driver_packages=driver_packages,
        registry_exists=registry_exists,
        errors=tuple(errors),
    )


@dataclass(frozen=True)
class ViGEmBusStatus:
    state: str
    device_instances: tuple = ()
    present_instances: tuple = ()
    bound_instances: tuple = ()
    driver_packages: tuple = ()
    service_exists: bool = False
    msi_entries: tuple = ()
    errors: tuple = ()

    @property
    def installed(self):
        return self.state == VIGEMBUS_HEALTHY

    @property
    def absent(self):
        return self.state == VIGEMBUS_ABSENT

    def describe(self):
        parts = [
            f"PnP nodes: {len(self.device_instances)} total, {len(self.present_instances)} present, "
            f"{len(self.bound_instances)} bound to ViGEmBus",
            "Driver Store package: " + (", ".join(self.driver_packages) if self.driver_packages else "missing"),
            "ViGEmBus service present" if self.service_exists else "ViGEmBus service missing",
            "MSI registration: " + (", ".join(self.msi_entries) if self.msi_entries else "missing"),
        ]
        if self.present_instances and not self.bound_instances:
            parts.append("unbound/stopped nodes: " + ", ".join(self.present_instances))
        if self.errors:
            parts.append("query errors: " + "; ".join(self.errors))
        return "\n".join(parts)


def _parse_vigembus_devices(output):
    instances = []
    present = []
    bound = []
    for section in re.finditer(
        r"DEVPKEY_Device_InstanceId[^\r\n]*[\r\n]+\s*"
        r"(ROOT\\(?:SYSTEM|VIGEMBUS)\\\d+)(.*?)"
        r"(?=DEVPKEY_Device_InstanceId|\Z)",
        output, re.IGNORECASE | re.DOTALL,
    ):
        instance = section.group(1).upper()
        instances.append(instance)
        body = section.group(2)
        is_present = re.search(
            r"DEVPKEY_Device_IsPresent[^\r\n]*[\r\n]+\s*TRUE", body, re.IGNORECASE)
        if is_present:
            present.append(instance)
            if re.search(r"DEVPKEY_Device_DriverInfPath[^\r\n]*[\r\n]+\s*oem\d+\.inf", body, re.IGNORECASE):
                bound.append(instance)
    return tuple(dict.fromkeys(instances)), tuple(dict.fromkeys(present)), tuple(dict.fromkeys(bound))


def _parse_vigembus_driver_packages(output):
    packages = []
    for block in re.split(r"(?:\r?\n){2,}", output):
        if "vigembus.inf" not in block.lower():
            continue
        match = re.search(r"\boem\d+\.inf\b", block, re.IGNORECASE)
        if match:
            packages.append(match.group(0).lower())
    return tuple(dict.fromkeys(packages))


def _query_vigembus_registry():
    import winreg
    service_exists = False
    try:
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Services\ViGEmBus")
        winreg.CloseKey(key)
        service_exists = True
    except FileNotFoundError:
        pass

    entries = []
    for path in (
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
        r"SOFTWARE\Wow6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
    ):
        try:
            root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path)
        except FileNotFoundError:
            continue
        try:
            for index in range(winreg.QueryInfoKey(root)[0]):
                name = winreg.EnumKey(root, index)
                try:
                    subkey = winreg.OpenKey(root, name)
                    display, _ = winreg.QueryValueEx(subkey, "DisplayName")
                    winreg.CloseKey(subkey)
                    if "vigem" in str(display).lower() or "virtual gamepad emulation" in str(display).lower():
                        entries.append(str(display))
                except (FileNotFoundError, OSError):
                    continue
        finally:
            winreg.CloseKey(root)
    return service_exists, tuple(dict.fromkeys(entries))


def get_vigembus_status(pnputil_runner=None, registry_query=None):
    runner = pnputil_runner or _run_pnputil
    errors = []
    all_instances = []
    present_instances = []
    bound_instances = []
    for device_id in (r"Root\ViGEmBus", r"Nefarius\ViGEmBus\Gen1"):
        try:
            result = runner(["/enum-devices", "/deviceid", device_id, "/properties", "/drivers"])
            found, present, bound = _parse_vigembus_devices(result.stdout or "")
            all_instances.extend(found)
            present_instances.extend(present)
            bound_instances.extend(bound)
        except Exception as exc:
            errors.append(f"device query failed for {device_id}: {exc}")

    try:
        result = runner(["/enum-drivers"])
        packages = _parse_vigembus_driver_packages(result.stdout or "")
    except Exception as exc:
        packages = ()
        errors.append(f"driver query failed: {exc}")

    try:
        service_exists, msi_entries = (registry_query or _query_vigembus_registry)()
    except Exception as exc:
        service_exists, msi_entries = False, ()
        errors.append(f"registry query failed: {exc}")

    all_instances = tuple(dict.fromkeys(all_instances))
    present_instances = tuple(dict.fromkeys(present_instances))
    bound_instances = tuple(dict.fromkeys(bound_instances))
    if bound_instances and packages and service_exists and not errors:
        state = VIGEMBUS_HEALTHY
    elif not present_instances and not packages and not service_exists and not msi_entries and not errors:
        state = VIGEMBUS_ABSENT
    else:
        state = VIGEMBUS_PARTIAL
    return ViGEmBusStatus(
        state=state,
        device_instances=all_instances,
        present_instances=present_instances,
        bound_instances=bound_instances,
        driver_packages=packages,
        service_exists=service_exists,
        msi_entries=tuple(msi_entries),
        errors=tuple(errors),
    )


def _gh(name):
    return f"{GITHUB_DRIVERS_RAW}/{name}"


DRIVER_SPECS = {
    "ViGEmBus": DriverSpec(
        "ViGEmBus",
        files=[(VIGEMBUS_URL, "ViGEmBus_1.22.0_x64_x86_arm64.exe")],
        run=("exe", "ViGEmBus_1.22.0_x64_x86_arm64.exe", "/quiet /norestart"),
    ),
    "WinUHid": DriverSpec(
        "WinUHid",
        files=[
            (_gh("install_driver.ps1"), "install_driver.ps1"),
            (_gh("WinUHidDriver.inf"), "WinUHidDriver.inf"),
            (_gh("WinUHidDriver.dll"), "WinUHidDriver.dll"),
            (_gh("WinUHidDriver.cer"), "WinUHidDriver.cer"),
            (_gh("winuhiddriver.cat"), "winuhiddriver.cat"),
        ],
        run=("ps1", "install_driver.ps1"),
    ),
    "USBIP": DriverSpec(
        "USBIP",
        files=[
            (_gh("install_usbip.ps1"), "install_usbip.ps1"),
            (USBIP_EXE_URL, "USBip-0.9.7.7-x64.exe"),
        ],
        run=("ps1", "install_usbip.ps1"),
    ),
    "HidHide": DriverSpec(
        "HidHide",
        files=[(_gh("hidhide/HidHide_x64.exe"), "HidHide_x64.exe")],
        run=("exe", "HidHide_x64.exe", "/quiet /norestart"),
    ),
}


# Uninstall scripts hosted in the project drivers/ folder. Each is self-contained
# (pnputil / certutil / registry / the driver's own uninstaller under Program Files)
# and references no bundled binary, so the packaged build just downloads the single
# .ps1 and runs it elevated. Mirrors DRIVER_SPECS but for removal.
UNINSTALL_SPECS = {
    "WinUHid": DriverSpec(
        "WinUHid",
        files=[(_gh("uninstall_driver.ps1"), "uninstall_driver.ps1")],
        run=("ps1", "uninstall_driver.ps1"),
    ),
    "ViGEmBus": DriverSpec(
        "ViGEmBus",
        files=[(_gh("uninstall_vigembus.ps1"), "uninstall_vigembus.ps1")],
        run=("ps1", "uninstall_vigembus.ps1"),
    ),
    "USBIP": DriverSpec(
        "USBIP",
        files=[(_gh("uninstall_usbip.ps1"), "uninstall_usbip.ps1")],
        run=("ps1", "uninstall_usbip.ps1"),
    ),
    "HidHide": DriverSpec(
        "HidHide",
        files=[(_gh("hidhide/uninstall_hidhide.ps1"), "uninstall_hidhide.ps1")],
        run=("ps1", "uninstall_hidhide.ps1"),
    ),
}


def make_download_dir(driver_key):
    """Create (and return) a clean temp folder to hold a driver's downloaded files."""
    base = os.path.join(tempfile.gettempdir(), "Switch2Connect_drivers", driver_key)
    os.makedirs(base, exist_ok=True)
    return base


def download_spec_files(spec, dest_dir, progress_cb=None):
    """Download every file for a DriverSpec into dest_dir.

    progress_cb(filename, downloaded_bytes, total_bytes) is called during download
    (total_bytes may be -1 when the server does not report a length).
    Returns the full path to the file named by spec.run.
    Raises on any network / IO error.
    """
    for url, filename in spec.files:
        dest = os.path.join(dest_dir, filename)
        req = urllib.request.Request(url, headers={"User-Agent": "Switch2Connect"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            total = int(resp.headers.get("Content-Length", -1))
            downloaded = 0
            with open(dest, "wb") as f:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if progress_cb:
                        progress_cb(filename, downloaded, total)
    return os.path.join(dest_dir, spec.run[1])
