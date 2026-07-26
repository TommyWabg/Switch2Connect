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
import threading
import time
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
# "We could not determine the state" is deliberately distinct from "it is half
# installed": only the latter may offer the user a repair.
WINUHID_UNKNOWN = "unknown"
VIGEMBUS_HEALTHY = "healthy"
VIGEMBUS_PARTIAL = "partial"
VIGEMBUS_ABSENT = "absent"
VIGEMBUS_UNKNOWN = "unknown"

# `pnputil /enum-devices` gained the `/properties` and `/drivers` sub-options in
# Windows 11 21H2. Older builds reject them with exit code 1 and print usage text,
# so every enumeration degrades to the plain listing (which pre-dates them) and,
# for presence, to the equally old `/connected` filter. Probe once per process.
PROBE_CFGMGR = "cfgmgr"
PROBE_RICH = "properties"
PROBE_PLAIN = "plain"
PROBE_FAILED = "failed"

# Hardware ids that identify a ViGEmBus bus node. The instance id is ROOT\SYSTEM\
# NNNN and contains no trace of "ViGEmBus", so only these identify it.
VIGEMBUS_HARDWARE_IDS = (r"Root\ViGEmBus", r"Nefarius\ViGEmBus\Gen1")

USBIP_HEALTHY = "healthy"
USBIP_PARTIAL = "partial"
USBIP_ABSENT = "absent"
USBIP_UNKNOWN = "unknown"
HIDHIDE_HEALTHY = "healthy"
HIDHIDE_PARTIAL = "partial"
HIDHIDE_ABSENT = "absent"
HIDHIDE_UNKNOWN = "unknown"

# Same lesson as ViGEmBus: the instance ids (ROOT\USB\0000, ROOT\SYSTEM\NNNN) are
# generic and shared with unrelated devices, so only the hardware id identifies
# these drivers. Verified against a live install.
USBIP_HARDWARE_IDS = (r"ROOT\USBIP_WIN2\UDE",)
HIDHIDE_HARDWARE_IDS = (r"root\HidHide",)

USBIP_DIR = r"C:\Program Files\USBip"
USBIP_FILES = ("usbip.exe", "devnode.exe", "unins000.exe")
USBIP_SERVICES = ("usbip2_ude", "usbip2_filter")
USBIP_INF_NAMES = ("usbip2_ude.inf", "usbip2_filter.inf")

_PNPUTIL_RICH_ENUM = None
_PNPUTIL_LOCK = threading.Lock()

# Status queries spawn two or three processes each and are called from UI refresh
# paths; short-lived caching keeps those paths cheap without hiding real changes.
_STATUS_TTL = 5.0
_status_cache = {}
_status_lock = threading.Lock()


def reset_pnputil_capability():
    """Forget the probed pnputil capability (used by tests)."""
    global _PNPUTIL_RICH_ENUM
    with _PNPUTIL_LOCK:
        _PNPUTIL_RICH_ENUM = None


def invalidate_driver_status_cache(key=None):
    """Drop cached driver status, e.g. right after an install or uninstall."""
    with _status_lock:
        if key is None:
            _status_cache.clear()
        else:
            _status_cache.pop(key, None)


def _cached_status(key):
    with _status_lock:
        entry = _status_cache.get(key)
    if entry and entry[0] > time.monotonic():
        return entry[1]
    return None


def _store_status(key, status):
    with _status_lock:
        _status_cache[key] = (time.monotonic() + _STATUS_TTL, status)
    return status


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
    probe_mode: str = ""
    undetermined: tuple = ()

    @property
    def installed(self):
        return self.state == WINUHID_HEALTHY

    @property
    def absent(self):
        return self.state == WINUHID_ABSENT

    @property
    def unknown(self):
        return self.state == WINUHID_UNKNOWN

    def describe(self):
        parts = []
        if self.unknown:
            parts.append(
                "Installation state could not be determined"
                + (f" ({', '.join(self.undetermined)} unreadable)" if self.undetermined else "")
                + ". This Windows build's pnputil may not support /properties."
            )
        if self.device_instances and not self.present_instances:
            parts.append("stale/disconnected device node: " + ", ".join(self.device_instances))
        elif self.present_instances:
            parts.append("present device node: " + ", ".join(self.present_instances))
        if self.driver_packages:
            parts.append("Driver Store package: " + ", ".join(self.driver_packages))
        elif self.driver_packages is None:
            parts.append("Driver Store package: unknown")
        else:
            parts.append("Driver Store package missing")
        if self.registry_exists is None:
            parts.append("WUDF service registry: unknown")
        else:
            parts.append("WUDF service registry present" if self.registry_exists else "WUDF service registry missing")
        if self.errors:
            parts.append("query errors: " + "; ".join(self.errors))
        return "\n".join(parts)


def _run_pnputil(args):
    return subprocess.run(
        ["pnputil", *args], capture_output=True, text=True, errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
    )


_WINUHID_ID_RE = re.compile(r"ROOT\\WINUHID\\[^\s\r\n]+", re.IGNORECASE)
_VIGEMBUS_ID_RE = re.compile(r"ROOT\\(?:SYSTEM|VIGEMBUS)\\\d+", re.IGNORECASE)


def _resolve_cfgmgr(cfgmgr, pnputil_injected):
    """Pick the PnP backend, preferring cfgmgr32 over shelling out to pnputil.

    A caller that injects a pnputil runner is exercising the pnputil path, so the
    real cfgmgr32 must not silently take over and answer from this machine's own
    devices. Returns None when the backend is unavailable or deliberately bypassed.
    """
    if cfgmgr is not None:
        return cfgmgr
    if pnputil_injected:
        return None
    try:
        import pnp_cfgmgr
    except Exception:
        return None
    return pnp_cfgmgr if pnp_cfgmgr.is_available() else None


def _pnputil_enum_devices(runner, device_id, with_drivers=False, remember=True):
    """Enumerate one device id, degrading gracefully on older pnputil.

    Returns (stdout, mode, error) where mode is PROBE_RICH / PROBE_PLAIN /
    PROBE_FAILED. Exit code 259 (ERROR_NO_MORE_ITEMS) means "nothing matched" and
    counts as success. Degrading to a plainer form is not an error; only running
    out of forms is.
    """
    global _PNPUTIL_RICH_ENUM
    base = ["/enum-devices", "/deviceid", device_id]

    attempts = []
    with _PNPUTIL_LOCK:
        rich_known_bad = remember and _PNPUTIL_RICH_ENUM is False
    if not rich_known_bad:
        rich = base + ["/properties"]
        if with_drivers:
            rich = rich + ["/drivers"]
        attempts.append((PROBE_RICH, rich))
    attempts.append((PROBE_PLAIN, base))

    last_error = None
    for mode, args in attempts:
        try:
            result = runner(args)
        except Exception as exc:
            last_error = f"{' '.join(args)} failed: {exc}"
            continue
        code = getattr(result, "returncode", 0)
        if code in (0, 259):
            if mode == PROBE_RICH and remember:
                with _PNPUTIL_LOCK:
                    _PNPUTIL_RICH_ENUM = True
            return (result.stdout or ""), mode, None
        if mode == PROBE_RICH and remember:
            with _PNPUTIL_LOCK:
                _PNPUTIL_RICH_ENUM = False
        last_error = f"{' '.join(args)} exit code {code}"
    return "", PROBE_FAILED, last_error


def _pnputil_connected_instances(runner, id_pattern):
    """Instance ids of every present device.

    `/connected` cannot be combined with `/deviceid` (pnputil rejects it with exit
    code 87), so this listing is unfiltered and callers must intersect it with the
    ids their filtered query returned. Returns None when it could not be produced.
    """
    try:
        result = runner(["/enum-devices", "/connected"])
    except Exception:
        return None
    if getattr(result, "returncode", 0) not in (0, 259):
        return None
    return _parse_instance_ids(result.stdout or "", id_pattern)


def _parse_instance_ids(output, id_pattern):
    """All instance ids in a plain listing (which carries no Siblings lists)."""
    return tuple(dict.fromkeys(m.group(0).upper() for m in id_pattern.finditer(output)))


def _parse_plain_devices(output, id_pattern):
    """Parse a `/enum-devices` listing that carries no DEVPKEY properties.

    Devices are blank-line separated blocks. The `Status:` label is localized, so
    presence is never read here - the caller re-queries with `/connected`. Driver
    binding comes from the `oemNN.inf` value, which is not localized.
    Returns (all instances, instances bound to a driver package).
    """
    instances = []
    bound = []
    for block in re.split(r"(?:\r?\n){2,}", output):
        match = id_pattern.search(block)
        if not match:
            continue
        instance = match.group(0).upper()
        instances.append(instance)
        if re.search(r"\boem\d+\.inf\b", block, re.IGNORECASE):
            bound.append(instance)
    return tuple(dict.fromkeys(instances)), tuple(dict.fromkeys(bound))


def _driver_database_packages(original_inf):
    r"""Map an original INF name to its published oemNN.inf names.

    HKLM\SYSTEM\DriverDatabase\DriverPackages holds one subkey per imported
    package, named "<original>.inf_<arch>_<hash>", whose default value is the
    published oemNN.inf. It answers the same question as `pnputil /enum-drivers`
    but is readable without elevation. Returns None when the database itself could
    not be read, () when the driver is simply not there.
    """
    import winreg
    prefix = original_inf.lower() + "_"
    readable = False
    packages = []
    for sam in (winreg.KEY_READ, winreg.KEY_READ | winreg.KEY_WOW64_64KEY):
        try:
            root = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\DriverDatabase\DriverPackages", 0, sam)
        except OSError:
            continue
        readable = True
        try:
            for index in range(winreg.QueryInfoKey(root)[0]):
                try:
                    name = winreg.EnumKey(root, index)
                except OSError:
                    break
                if not name.lower().startswith(prefix):
                    continue
                try:
                    entry = winreg.OpenKey(root, name, 0, sam)
                except OSError:
                    continue
                try:
                    value, _ = winreg.QueryValueEx(entry, "")
                except OSError:
                    continue
                finally:
                    winreg.CloseKey(entry)
                match = re.search(r"\boem\d+\.inf\b", str(value), re.IGNORECASE)
                if match:
                    packages.append(match.group(0).lower())
        finally:
            winreg.CloseKey(root)
    if not readable:
        return None
    return tuple(dict.fromkeys(packages))


def _service_bound_instances(service_name):
    r"""Instance ids currently bound to a service, from Services\<svc>\Enum.

    Used as the plain-mode binding signal. Returns None when unreadable.
    """
    import winreg
    try:
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            rf"SYSTEM\CurrentControlSet\Services\{service_name}\Enum")
    except FileNotFoundError:
        return ()
    except OSError:
        return None
    instances = []
    try:
        count, _ = winreg.QueryValueEx(key, "Count")
        for index in range(int(count)):
            try:
                value, _ = winreg.QueryValueEx(key, str(index))
            except OSError:
                continue
            instances.append(str(value).upper())
    except OSError:
        return None
    finally:
        winreg.CloseKey(key)
    return tuple(dict.fromkeys(instances))


def _parse_driver_packages(output, inf_name):
    packages = []
    for block in re.split(r"(?:\r?\n){2,}", output):
        if inf_name not in block.lower():
            continue
        match = re.search(r"\boem\d+\.inf\b", block, re.IGNORECASE)
        if match:
            packages.append(match.group(0).lower())
    return tuple(dict.fromkeys(packages))


def _enum_driver_packages(runner, inf_name, database_name, errors):
    """Driver Store lookup with a registry fallback.

    `pnputil /enum-drivers` requires elevation, so an unelevated GUI gets a
    nonzero exit code; the driver database answers the same question as a plain
    user. Returns None only when neither source could answer.
    """
    try:
        result = runner(["/enum-drivers"])
        code = getattr(result, "returncode", 0)
        if code == 0:
            return _parse_driver_packages(result.stdout or "", inf_name)
        enum_error = f"driver query exit code {code}"
    except Exception as exc:
        enum_error = f"driver query failed: {exc}"

    try:
        fallback = _driver_database_packages(database_name)
    except Exception as exc:
        errors.append(f"{enum_error}; driver database lookup failed: {exc}")
        return None
    if fallback is None:
        errors.append(enum_error)
        return None
    return fallback


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


def _parse_rich_devices(output, id_pattern):
    """(all, present) from `/properties` output, anchored on locale-stable keys.

    Instance ids also appear inside other devices' Siblings lists, so sections are
    anchored on DEVPKEY_Device_InstanceId rather than searched for directly.
    """
    instances = []
    present = []
    for section in re.finditer(
        r"DEVPKEY_Device_InstanceId[^\r\n]*[\r\n]+\s*(\S+)(.*?)"
        r"(?=DEVPKEY_Device_InstanceId|\Z)",
        output, re.IGNORECASE | re.DOTALL,
    ):
        instance = section.group(1).upper()
        if not id_pattern.fullmatch(instance):
            continue
        instances.append(instance)
        match = re.search(
            r"DEVPKEY_Device_IsPresent[^\r\n]*[\r\n]+\s*(TRUE|FALSE)",
            section.group(2), re.IGNORECASE)
        if match and match.group(1).upper() == "TRUE":
            present.append(instance)
    return tuple(dict.fromkeys(instances)), tuple(dict.fromkeys(present))


def _enumerate_device_layer(runner, backend, hardware_ids, id_pattern, injected):
    """Resolve (all instances, present instances, probe_mode, errors) for a driver.

    cfgmgr32 first - hardware-id matching is the only correct filter here. Falls
    back to pnputil's `/deviceid` filter, which takes the same hardware ids.
    `present` is None when it could not be determined at all.
    """
    errors = []
    if backend is not None:
        matched = backend.instances_by_hardware_id(hardware_ids)
        if matched is not None:
            # Hardware ids are only readable on present nodes, so a match is
            # simultaneously the "all" and the "present" answer.
            found = tuple(dict.fromkeys(i.upper() for i in matched))
            return found, found, PROBE_CFGMGR, errors

    all_instances = []
    present_instances = []
    probe_mode = PROBE_RICH
    connected = _UNSET = object()
    for device_id in hardware_ids:
        text, mode, error = _pnputil_enum_devices(
            runner, device_id, remember=not injected)
        if mode == PROBE_FAILED:
            errors.append(f"device query failed for {device_id}: {error}")
            return tuple(dict.fromkeys(all_instances)), None, PROBE_FAILED, errors
        if mode == PROBE_RICH:
            found, present = _parse_rich_devices(text, id_pattern)
        else:
            probe_mode = PROBE_PLAIN
            found, _ = _parse_plain_devices(text, id_pattern)
            if connected is _UNSET:
                connected = _pnputil_connected_instances(runner, id_pattern)
            if connected is None:
                errors.append("connected device query failed")
                return tuple(dict.fromkeys(all_instances)), None, probe_mode, errors
            present = tuple(i for i in found if i in connected)
        all_instances.extend(found)
        present_instances.extend(present)
    return (tuple(dict.fromkeys(all_instances)),
            tuple(dict.fromkeys(present_instances)), probe_mode, errors)


def _service_key_state(service_name):
    """True registered / False absent / None unreadable."""
    import winreg
    try:
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            rf"SYSTEM\CurrentControlSet\Services\{service_name}")
        winreg.CloseKey(key)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return None


def _scan_uninstall_entries(*needles):
    """DisplayName values in either Uninstall hive matching any needle."""
    import winreg
    entries = []
    for path in (
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
        r"SOFTWARE\Wow6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
    ):
        try:
            root = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path)
        except OSError:
            continue
        try:
            for index in range(winreg.QueryInfoKey(root)[0]):
                try:
                    name = winreg.EnumKey(root, index)
                    subkey = winreg.OpenKey(root, name)
                    display, _ = winreg.QueryValueEx(subkey, "DisplayName")
                    winreg.CloseKey(subkey)
                except (FileNotFoundError, OSError):
                    continue
                lowered = str(display).lower()
                if any(needle in lowered for needle in needles):
                    entries.append(str(display))
        finally:
            winreg.CloseKey(root)
    return tuple(dict.fromkeys(entries))


def _parse_winuhid_driver_packages(output):
    return _parse_driver_packages(output, "winuhiddriver.inf")


def get_winuhid_status(pnputil_runner=None, registry_checker=None, cfgmgr=None,
                       use_cache=False):
    """Inspect all installation layers without treating a phantom node as healthy.

    Any layer that could not be resolved is reported as None and yields
    WINUHID_UNKNOWN, never WINUHID_PARTIAL - a query that failed is not evidence
    of a broken install, and offering a repair for it only wastes the user's time.
    """
    injected = (pnputil_runner is not None or registry_checker is not None
                or cfgmgr is not None)
    runner = pnputil_runner or _run_pnputil
    if use_cache and not injected:
        cached = _cached_status("winuhid")
        if cached is not None:
            return cached

    errors = []
    probe_mode = ""
    device_instances = ()
    present_instances = None

    backend = _resolve_cfgmgr(cfgmgr, pnputil_runner is not None)
    if backend is not None:
        instances = backend.device_instances("ROOT")
        if instances is not None:
            device_instances = tuple(
                i.upper() for i in instances if i.upper().startswith("ROOT\\WINUHID\\"))
            present_instances = tuple(
                i for i in device_instances if backend.is_present(i))
            probe_mode = PROBE_CFGMGR

    if probe_mode != PROBE_CFGMGR:
        text, probe_mode, error = _pnputil_enum_devices(
            runner, r"Root\WinUHid", remember=not injected)
        if probe_mode == PROBE_FAILED:
            device_instances, present_instances = (), None
            errors.append(f"device query failed: {error}")
        elif probe_mode == PROBE_RICH:
            device_instances, present_instances = _parse_winuhid_devices(text)
        else:
            device_instances, _ = _parse_plain_devices(text, _WINUHID_ID_RE)
            connected = _pnputil_connected_instances(runner, _WINUHID_ID_RE)
            if connected is None:
                present_instances = None
                errors.append("connected device query failed")
            else:
                present_instances = tuple(i for i in device_instances if i in connected)

    driver_packages = _enum_driver_packages(
        runner, "winuhiddriver.inf", "WinUHidDriver.inf", errors)

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
        registry_exists = None
        errors.append(f"registry query failed: {exc}")

    layers = {
        "device": present_instances,
        "driver package": driver_packages,
        "WUDF registry": registry_exists,
    }
    undetermined = tuple(name for name, value in layers.items() if value is None)
    if undetermined:
        state = WINUHID_UNKNOWN
    elif present_instances and driver_packages and registry_exists:
        state = WINUHID_HEALTHY
    elif not present_instances and not driver_packages and not registry_exists:
        state = WINUHID_ABSENT
    else:
        state = WINUHID_PARTIAL
    status = WinUHidStatus(
        state=state,
        device_instances=device_instances,
        present_instances=present_instances,
        driver_packages=driver_packages,
        registry_exists=registry_exists,
        errors=tuple(errors),
        probe_mode=probe_mode,
        undetermined=undetermined,
    )
    if not injected:
        _store_status("winuhid", status)
    return status


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
    probe_mode: str = ""
    undetermined: tuple = ()

    @property
    def installed(self):
        return self.state == VIGEMBUS_HEALTHY

    @property
    def absent(self):
        return self.state == VIGEMBUS_ABSENT

    @property
    def unknown(self):
        return self.state == VIGEMBUS_UNKNOWN

    def describe(self):
        parts = []
        if self.unknown:
            parts.append(
                "Installation state could not be determined"
                + (f" ({', '.join(self.undetermined)} unreadable)" if self.undetermined else "")
                + ". This Windows build's pnputil may not support /properties."
            )
        parts += [
            "PnP nodes: {} total, {} present, {} bound to ViGEmBus".format(
                len(self.device_instances),
                "unknown" if self.present_instances is None else len(self.present_instances),
                "unknown" if self.bound_instances is None else len(self.bound_instances),
            ),
            "Driver Store package: " + (
                "unknown" if self.driver_packages is None
                else (", ".join(self.driver_packages) if self.driver_packages else "missing")),
            "ViGEmBus service: unknown" if self.service_exists is None else (
                "ViGEmBus service present" if self.service_exists else "ViGEmBus service missing"),
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
    return _parse_driver_packages(output, "vigembus.inf")


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
    except OSError:
        service_exists = None

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


def get_vigembus_status(pnputil_runner=None, registry_query=None, service_enum=None,
                        cfgmgr=None, use_cache=False):
    """Mirror of get_winuhid_status for ViGEmBus, including the returncode checks.

    Layers that could not be resolved become None and yield VIGEMBUS_UNKNOWN.
    """
    injected = (pnputil_runner is not None or registry_query is not None
                or service_enum is not None or cfgmgr is not None)
    runner = pnputil_runner or _run_pnputil
    if use_cache and not injected:
        cached = _cached_status("vigembus")
        if cached is not None:
            return cached

    errors = []
    all_instances = []
    present_instances = []
    bound_instances = []
    plain_seen = False
    probe_mode = PROBE_RICH
    connected = _UNSET = object()

    backend = _resolve_cfgmgr(cfgmgr, pnputil_runner is not None)
    if backend is not None:
        bound = backend.instances_by_service("ViGEmBus")
        present = backend.instances_by_hardware_id(VIGEMBUS_HARDWARE_IDS)
        if bound is not None and present is not None:
            # A node bound to the service is present by definition, so the union
            # cannot under-report; the hardware-id scan alone would miss nothing
            # here but the service list is the stricter binding signal.
            bound_instances = [i.upper() for i in bound]
            present_instances = list(dict.fromkeys(
                [i.upper() for i in present] + bound_instances))
            all_instances = list(present_instances)
            probe_mode = PROBE_CFGMGR

    device_ids = () if probe_mode == PROBE_CFGMGR else VIGEMBUS_HARDWARE_IDS
    for device_id in device_ids:
        text, mode, error = _pnputil_enum_devices(
            runner, device_id, with_drivers=True, remember=not injected)
        if mode == PROBE_FAILED:
            present_instances = None
            bound_instances = None
            probe_mode = mode
            errors.append(f"device query failed for {device_id}: {error}")
            break
        if mode == PROBE_RICH:
            found, present, bound = _parse_vigembus_devices(text)
        else:
            plain_seen = True
            probe_mode = PROBE_PLAIN
            found, bound = _parse_plain_devices(text, _VIGEMBUS_ID_RE)
            if connected is _UNSET:
                connected = _pnputil_connected_instances(runner, _VIGEMBUS_ID_RE)
            if connected is None:
                present_instances = None
                bound_instances = None
                errors.append("connected device query failed")
                break
            # The connected listing is unfiltered, so intersect: ROOT\SYSTEM\NNNN
            # also matches plenty of devices that have nothing to do with ViGEmBus.
            present = tuple(instance for instance in found if instance in connected)
            bound = tuple(instance for instance in bound if instance in present)
        all_instances.extend(found)
        present_instances.extend(present)
        bound_instances.extend(bound)

    packages = _enum_driver_packages(runner, "vigembus.inf", "vigembus.inf", errors)

    try:
        service_exists, msi_entries = (registry_query or _query_vigembus_registry)()
    except Exception as exc:
        service_exists, msi_entries = None, ()
        errors.append(f"registry query failed: {exc}")

    all_instances = tuple(dict.fromkeys(all_instances))
    if present_instances is not None:
        present_instances = tuple(dict.fromkeys(present_instances))
    if bound_instances is not None:
        bound_instances = tuple(dict.fromkeys(bound_instances))
        # Plain output only shows a node's own oemNN.inf; the service's Enum key is
        # the authoritative binding list when that column is missing.
        if plain_seen and not bound_instances and present_instances:
            from_service = (service_enum or _service_bound_instances)("ViGEmBus")
            if from_service is None:
                bound_instances = None
                errors.append("service binding lookup failed")
            else:
                bound_instances = tuple(
                    instance for instance in present_instances if instance in from_service)

    layers = {
        "device": present_instances if bound_instances is not None else None,
        "driver package": packages,
        "ViGEmBus service": service_exists,
    }
    undetermined = tuple(name for name, value in layers.items() if value is None)
    if undetermined:
        state = VIGEMBUS_UNKNOWN
    elif bound_instances and packages and service_exists:
        state = VIGEMBUS_HEALTHY
    elif not present_instances and not packages and not service_exists and not msi_entries:
        state = VIGEMBUS_ABSENT
    else:
        state = VIGEMBUS_PARTIAL
    status = ViGEmBusStatus(
        state=state,
        device_instances=all_instances,
        present_instances=present_instances,
        bound_instances=bound_instances,
        driver_packages=packages,
        service_exists=service_exists,
        msi_entries=tuple(msi_entries),
        errors=tuple(errors),
        probe_mode=probe_mode,
        undetermined=undetermined,
    )
    if not injected:
        _store_status("vigembus", status)
    return status


_USBIP_ID_RE = re.compile(r"ROOT\\USB\\\d+", re.IGNORECASE)
_HIDHIDE_ID_RE = re.compile(r"ROOT\\SYSTEM\\\d+", re.IGNORECASE)


@dataclass(frozen=True)
class USBIPStatus:
    state: str
    device_instances: tuple = ()
    present_instances: tuple = ()
    driver_packages: tuple = ()
    services: tuple = ()
    files: tuple = ()
    errors: tuple = ()
    probe_mode: str = ""
    undetermined: tuple = ()

    @property
    def installed(self):
        return self.state == USBIP_HEALTHY

    @property
    def absent(self):
        return self.state == USBIP_ABSENT

    @property
    def unknown(self):
        return self.state == USBIP_UNKNOWN

    def describe(self):
        parts = []
        if self.unknown:
            parts.append(
                "Installation state could not be determined"
                + (f" ({', '.join(self.undetermined)} unreadable)" if self.undetermined else "")
                + ".")
        parts += [
            "UDE device node: " + (
                "unknown" if self.present_instances is None
                else (", ".join(self.present_instances) if self.present_instances else "missing")),
            "Driver Store packages: " + (
                "unknown" if self.driver_packages is None
                else (", ".join(self.driver_packages) if self.driver_packages else "missing")),
            "Services: " + (
                "unknown" if self.services is None
                else (", ".join(self.services) if self.services else "missing")),
            "Program Files: " + (", ".join(self.files) if self.files else "missing"),
        ]
        if self.errors:
            parts.append("query errors: " + "; ".join(self.errors))
        return "\n".join(parts)


def _usbip_present_files():
    return tuple(name for name in USBIP_FILES
                 if os.path.exists(os.path.join(USBIP_DIR, name)))


def get_usbip_status(pnputil_runner=None, registry_query=None, cfgmgr=None,
                     file_lister=None, use_cache=False):
    """Inspect every USBIP layer, not just whether usbip.exe happens to exist.

    A layer that could not be resolved is None and yields USBIP_UNKNOWN, never
    USBIP_PARTIAL - the same rule as the WinUHid/ViGEmBus status functions.
    """
    injected = (pnputil_runner is not None or registry_query is not None
                or cfgmgr is not None or file_lister is not None)
    runner = pnputil_runner or _run_pnputil
    if use_cache and not injected:
        cached = _cached_status("usbip")
        if cached is not None:
            return cached

    backend = _resolve_cfgmgr(cfgmgr, pnputil_runner is not None)
    device_instances, present_instances, probe_mode, errors = _enumerate_device_layer(
        runner, backend, USBIP_HARDWARE_IDS, _USBIP_ID_RE, injected)

    packages = []
    for inf_name in USBIP_INF_NAMES:
        found = _enum_driver_packages(runner, inf_name, inf_name, errors)
        if found is None:
            packages = None
            break
        packages.extend(found)
    if packages is not None:
        packages = tuple(dict.fromkeys(packages))

    try:
        states = (registry_query or (
            lambda: tuple(_service_key_state(name) for name in USBIP_SERVICES)))()
        services = None if any(state is None for state in states) else tuple(
            name for name, state in zip(USBIP_SERVICES, states) if state)
    except Exception as exc:
        services = None
        errors.append(f"service query failed: {exc}")

    files = (file_lister or _usbip_present_files)()

    layers = {
        "device": present_instances,
        "driver package": packages,
        "service": services,
    }
    undetermined = tuple(name for name, value in layers.items() if value is None)
    if undetermined:
        state = USBIP_UNKNOWN
    elif present_instances and packages and services and files:
        state = USBIP_HEALTHY
    elif not present_instances and not packages and not services and not files:
        state = USBIP_ABSENT
    else:
        state = USBIP_PARTIAL
    status = USBIPStatus(
        state=state,
        device_instances=device_instances,
        present_instances=present_instances,
        driver_packages=packages,
        services=services,
        files=files,
        errors=tuple(errors),
        probe_mode=probe_mode,
        undetermined=undetermined,
    )
    if not injected:
        _store_status("usbip", status)
    return status


@dataclass(frozen=True)
class HidHideStatus:
    state: str
    device_instances: tuple = ()
    present_instances: tuple = ()
    driver_packages: tuple = ()
    service_exists: bool = False
    msi_entries: tuple = ()
    errors: tuple = ()
    probe_mode: str = ""
    undetermined: tuple = ()

    @property
    def installed(self):
        return self.state == HIDHIDE_HEALTHY

    @property
    def absent(self):
        return self.state == HIDHIDE_ABSENT

    @property
    def unknown(self):
        return self.state == HIDHIDE_UNKNOWN

    def describe(self):
        parts = []
        if self.unknown:
            parts.append(
                "Installation state could not be determined"
                + (f" ({', '.join(self.undetermined)} unreadable)" if self.undetermined else "")
                + ".")
        parts += [
            "Device nodes: " + (
                "unknown" if self.present_instances is None
                else (", ".join(self.present_instances) if self.present_instances else "missing")),
            "Driver Store package: " + (
                "unknown" if self.driver_packages is None
                else (", ".join(self.driver_packages) if self.driver_packages else "missing")),
            "HidHide service: unknown" if self.service_exists is None else (
                "HidHide service present" if self.service_exists else "HidHide service missing"),
            "MSI registration: " + (", ".join(self.msi_entries) if self.msi_entries else "missing"),
        ]
        if self.errors:
            parts.append("query errors: " + "; ".join(self.errors))
        return "\n".join(parts)


def _query_hidhide_registry():
    """(service state, MSI entries). Service state is tri-state."""
    try:
        import hidhide
        service_state = hidhide.service_state()
    except Exception:
        service_state = _service_key_state("HidHide")
    return service_state, _scan_uninstall_entries("hidhide")


def get_hidhide_status(pnputil_runner=None, registry_query=None, cfgmgr=None,
                       use_cache=False):
    """Inspect every HidHide layer, not just the service registry key.

    Note: HidHide is a filter driver attached to nearly every HID device on the
    machine (198 nodes here), so its service device list is NOT an installation
    signal. Only the ROOT nodes carrying hardware id "root\\HidHide" count.
    """
    injected = (pnputil_runner is not None or registry_query is not None
                or cfgmgr is not None)
    runner = pnputil_runner or _run_pnputil
    if use_cache and not injected:
        cached = _cached_status("hidhide")
        if cached is not None:
            return cached

    backend = _resolve_cfgmgr(cfgmgr, pnputil_runner is not None)
    device_instances, present_instances, probe_mode, errors = _enumerate_device_layer(
        runner, backend, HIDHIDE_HARDWARE_IDS, _HIDHIDE_ID_RE, injected)

    packages = _enum_driver_packages(runner, "hidhide.inf", "hidhide.inf", errors)

    try:
        service_exists, msi_entries = (registry_query or _query_hidhide_registry)()
    except Exception as exc:
        service_exists, msi_entries = None, ()
        errors.append(f"registry query failed: {exc}")

    layers = {
        "device": present_instances,
        "driver package": packages,
        "HidHide service": service_exists,
    }
    undetermined = tuple(name for name, value in layers.items() if value is None)
    if undetermined:
        state = HIDHIDE_UNKNOWN
    elif present_instances and packages and service_exists:
        state = HIDHIDE_HEALTHY
    elif not present_instances and not packages and not service_exists and not msi_entries:
        state = HIDHIDE_ABSENT
    else:
        state = HIDHIDE_PARTIAL
    status = HidHideStatus(
        state=state,
        device_instances=device_instances,
        present_instances=present_instances,
        driver_packages=packages,
        service_exists=service_exists,
        msi_entries=tuple(msi_entries),
        errors=tuple(errors),
        probe_mode=probe_mode,
        undetermined=undetermined,
    )
    if not injected:
        _store_status("hidhide", status)
    return status


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
