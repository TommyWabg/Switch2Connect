# Switch2Connect - Plug and Play queries via the Configuration Manager API.
# Copyright (C) 2026 TommyWabg
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# pnputil is a CLI wrapper over these same cfgmgr32 entry points, but its command
# surface changed across Windows releases: `/enum-devices ... /properties` only
# exists on Windows 11 21H2 and later, and older builds reject the whole command
# with exit code 1. cfgmgr32 has been stable since Windows 2000, needs no
# elevation, and spawns no process, so driver detection is built on it directly
# and falls back to pnputil only when it is unavailable.
#
# Every public function returns None when the answer could not be determined.
# Callers must treat that as "unknown", never as "none found" - see the tri-state
# handling in driver_install_helper.get_winuhid_status.

import ctypes
from ctypes import wintypes

CR_SUCCESS = 0
CR_NO_SUCH_DEVINST = 0x0000000D

CM_GETIDLIST_FILTER_ENUMERATOR = 0x00000001
CM_GETIDLIST_FILTER_SERVICE = 0x00000002

CM_LOCATE_DEVNODE_NORMAL = 0x00000000

CM_DRP_DEVICEDESC = 0x00000001
CM_DRP_HARDWAREID = 0x00000002
CM_DRP_SERVICE = 0x00000005

_ENTRY_POINTS = (
    "CM_Get_Device_ID_List_SizeW",
    "CM_Get_Device_ID_ListW",
    "CM_Locate_DevNodeW",
    "CM_Get_DevNode_Registry_PropertyW",
)

_library = None
_load_failed = False


def _lib():
    """Load cfgmgr32 once. Returns None when it is not usable."""
    global _library, _load_failed
    if _library is not None or _load_failed:
        return _library
    try:
        candidate = ctypes.WinDLL("cfgmgr32")
        for name in _ENTRY_POINTS:
            getattr(candidate, name)
    except (OSError, AttributeError):
        _load_failed = True
        return None
    _library = candidate
    return _library


def set_library(library):
    """Inject a stand-in for cfgmgr32 (tests). Pass None to force unavailability."""
    global _library, _load_failed
    _library = library
    _load_failed = library is None


def reset_library():
    """Forget the loaded/injected library so the next call probes again."""
    global _library, _load_failed
    _library = None
    _load_failed = False


def is_available():
    return _lib() is not None


def _split_multi_sz(buffer, length):
    """Split a wide-character REG_MULTI_SZ style buffer of `length` characters."""
    return [item for item in buffer[:length].split("\0") if item]


def _id_list(filter_text, flags):
    lib = _lib()
    if lib is None:
        return None
    try:
        size = wintypes.ULONG(0)
        wanted = ctypes.create_unicode_buffer(filter_text)
        # The W variants report and consume a length in characters, not bytes.
        if lib.CM_Get_Device_ID_List_SizeW(
                ctypes.byref(size), wanted, flags) != CR_SUCCESS:
            return None
        if not size.value:
            return []
        buffer = ctypes.create_unicode_buffer(size.value)
        if lib.CM_Get_Device_ID_ListW(
                wanted, buffer, size.value, flags) != CR_SUCCESS:
            return None
        return _split_multi_sz(buffer, size.value)
    except Exception:
        return None


def device_instances(enumerator="ROOT"):
    """Every instance id under an enumerator, including phantom (absent) nodes.

    Returns None when the query failed, [] when there genuinely are none.
    """
    return _id_list(enumerator, CM_GETIDLIST_FILTER_ENUMERATOR)


def instances_by_service(service_name):
    """Instance ids bound to a driver service - the authoritative "bound" list.

    This is what distinguishes a working ViGEmBus node from one that is present
    but not attached to its driver.
    """
    return _id_list(service_name, CM_GETIDLIST_FILTER_SERVICE)


def _locate(instance_id):
    """Return the devinst handle for a present node, or None.

    Phantom nodes cannot be located even with CM_LOCATE_DEVNODE_PHANTOM (verified
    on Windows 11 26200: ROOT\\WINUHID\\0000 returns CR_NO_SUCH_DEVINST for both
    flags while still appearing in the id list), so locating successfully is
    exactly equivalent to DEVPKEY_Device_IsPresent being TRUE.
    """
    lib = _lib()
    if lib is None:
        return None
    try:
        node = wintypes.DWORD()
        wanted = ctypes.create_unicode_buffer(instance_id)
        if lib.CM_Locate_DevNodeW(
                ctypes.byref(node), wanted, CM_LOCATE_DEVNODE_NORMAL) != CR_SUCCESS:
            return None
        return node
    except Exception:
        return None


def is_present(instance_id):
    """True when the node currently exists, False when it is a stale record.

    Returns None only when cfgmgr32 itself is unusable; a node that simply is not
    there is False, not None.
    """
    if _lib() is None:
        return None
    return _locate(instance_id) is not None


def _registry_property(node, property_id):
    lib = _lib()
    if lib is None or node is None:
        return None
    try:
        size = wintypes.ULONG(0)
        kind = wintypes.ULONG(0)
        # First call sizes the buffer; it reports BYTES here, unlike the id list.
        lib.CM_Get_DevNode_Registry_PropertyW(
            node, property_id, ctypes.byref(kind), None, ctypes.byref(size), 0)
        if not size.value:
            return []
        buffer = ctypes.create_unicode_buffer(size.value // 2 + 1)
        if lib.CM_Get_DevNode_Registry_PropertyW(
                node, property_id, ctypes.byref(kind), buffer,
                ctypes.byref(size), 0) != CR_SUCCESS:
            return None
        return _split_multi_sz(buffer, size.value // 2)
    except Exception:
        return None


def hardware_ids(instance_id):
    """Hardware ids of a node. Empty for phantom nodes, which carry no properties."""
    node = _locate(instance_id)
    if node is None:
        return [] if _lib() is not None else None
    return _registry_property(node, CM_DRP_HARDWAREID)


def service_of(instance_id):
    """The driver service a node is bound to, or None/[]."""
    node = _locate(instance_id)
    if node is None:
        return [] if _lib() is not None else None
    return _registry_property(node, CM_DRP_SERVICE)


def instances_by_hardware_id(wanted_hardware_ids, enumerator="ROOT"):
    """Present nodes whose hardware id matches any of the wanted ids.

    This is the `pnputil /enum-devices /deviceid` equivalent, and it is the only
    correct way to find ViGEmBus: its instance id is ROOT\\SYSTEM\\0001, which
    contains no trace of "ViGEmBus" - matching ROOT\\SYSTEM\\NNNN by pattern also
    catches SWENUM and HidHide nodes. Only the hardware id
    "Nefarius\\ViGEmBus\\Gen1" identifies it.
    """
    instances = device_instances(enumerator)
    if instances is None:
        return None
    wanted = {item.upper() for item in wanted_hardware_ids}
    matched = []
    for instance in instances:
        ids = hardware_ids(instance)
        if not ids:
            continue
        if any(item.upper() in wanted for item in ids):
            matched.append(instance)
    return matched
