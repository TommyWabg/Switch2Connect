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

"""Find which process holds our virtual HID device open, and watch it go away.

Used for the 15 ms motion compensation in usbip_server.py.  Subcommand 0x41 reaches us
as plain USB data with no process identity, so when it arrives we look at the processes
that have hid.dll loaded, scan only their handles (like Sysinternals handle.exe) for the
virtual Pro's HID device, and pick the sender: an Eden/yuzu-family exe name if present,
otherwise the opener started most recently.  We then watch that process; when it exits,
hides its window, or closes the device, the compensation is switched off.

Everything Windows-specific is in WinBackend; the selection logic and the watch loop are
plain Python so they can be unit-tested anywhere.
"""

import logging
import os
import sys
import threading
import time
from collections import namedtuple

logger = logging.getLogger(__name__)

# Emulators that inherited yuzu's direct Joy-Con/Pro driver (fixed 15 ms per report).
EDEN_FAMILY_EXES = frozenset({
    "eden.exe", "yuzu.exe", "citron.exe", "suyu.exe", "sudachi.exe", "torzu.exe",
})

ProcInfo = namedtuple("ProcInfo", "pid exe create_time")


def choose_owners(openers, own_pid=None, family=EDEN_FAMILY_EXES):
    """Pick the process(es) that sent 0x41 from the current openers of the device.

    SDL emulators commonly keep the same device open, so prefer Eden-family exe names;
    if none match, take the opener started most recently (it is the one that just
    initialised the device).
    """
    openers = [o for o in openers if o.pid != own_pid]
    named = [o for o in openers if (o.exe or "").lower() in family]
    if named:
        return named
    if openers:
        return [max(openers, key=lambda o: o.create_time or 0)]
    return []


class OwnerWatch:
    """Background watch: calls on_gone(reason, watch) once when the owner is gone."""

    def __init__(self, vid, pid, serial, on_gone, backend=None, interval=2.0,
                 holder_recheck_every=3, family=EDEN_FAMILY_EXES):
        self.vid = vid
        self.pid = pid
        self.serial = serial
        self.on_gone = on_gone
        self.backend = backend if backend is not None else default_backend()
        self.interval = interval
        self.holder_recheck_every = max(1, int(holder_recheck_every))
        self.family = family
        self.owners = []
        self.mode = None  # "handle" or "name"
        self.check_holders = True
        # Eden hides its window at once on exit but the process can take ~30 s to end,
        # so a vanished window counts as closed (only if it had one when identified).
        self.window_watch = set()
        self._window_misses = {}
        self._stuck_pids = set()
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, name="EdenOwnerWatch", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    @property
    def stopped(self):
        return self._stop.is_set()

    def _openers(self, only_pids, timeout=4.0):
        """Scan the given processes' handles for our device, one helper thread per
        process so a handle that blocks NtQueryObject only loses that one process.
        Returns the openers found, or None if every scan failed or timed out."""
        pids = sorted(only_pids or [])
        if not pids:
            return []
        results = {}

        def work(p):
            try:
                results[p] = self.backend.find_openers(self.vid, self.pid, self.serial, {p})
            except Exception as e:
                results[p] = e

        t0 = time.perf_counter()
        threads = []
        for p in pids:
            t = threading.Thread(target=work, args=(p,), name=f"OwnerScan-{p}", daemon=True)
            t.start()
            threads.append((p, t))
        deadline = t0 + timeout
        found, ok = [], False
        for p, t in threads:
            t.join(max(0.0, deadline - time.perf_counter()))
            r = results.get(p)
            if t.is_alive():
                logger.warning("0x41 owner watch: handle scan of pid %d timed out (stuck at %s)",
                               p, getattr(self.backend, "progress", "?"))
                self._stuck_pids.add(p)
            elif isinstance(r, Exception):
                logger.warning("0x41 owner watch: handle scan of pid %d failed: %r", p, r)
            elif r is not None:
                ok = True
                found.extend(r)
        logger.debug("0x41 owner watch: scanned %d process(es) in %.0f ms", len(pids),
                     (time.perf_counter() - t0) * 1000.0)
        return found if ok else None

    def _family_running(self):
        try:
            return self.backend.any_process_named(self.family)
        except Exception as e:
            logger.warning("0x41 owner watch: process list failed: %r", e)
            return True

    def _family_processes(self):
        try:
            return list(self.backend.processes_named(self.family))
        except Exception as e:
            logger.warning("0x41 owner watch: process list failed: %r", e)
            return []

    def _hid_processes(self):
        try:
            return self.backend.hid_processes()
        except Exception as e:
            logger.warning("0x41 owner watch: hid.dll process list failed: %r", e)
            return None

    def _identify(self):
        # Any app that sends 0x41 talks HID through hid.dll, so only processes that have
        # it loaded can hold our device.  Scanning just those is fast and stays away
        # from unrelated handles that can block NtQueryObject.
        own = os.getpid()
        candidates = self._hid_processes()
        family = self._family_processes()
        if candidates is None:
            candidates = family
        candidates = [c for c in candidates if c.pid != own]
        logger.info("0x41 owner watch: candidate processes (hid.dll): %s",
                    ", ".join(f"{o.exe} pid {o.pid}" for o in candidates) or "none")
        openers = self._openers({c.pid for c in candidates})
        if openers:
            # Known Eden-family names first (SDL hosts often hold the device too),
            # otherwise the opener started most recently: it just initialised it.
            self.owners = choose_owners(openers, own, self.family)
        if not self.owners and family:
            self.owners = family
            self.check_holders = False
        if self.owners:
            self.mode = "handle"
            for o in self.owners:
                if self._has_window(o) is True:
                    self.window_watch.add(o.pid)
            logger.info("0x41 owner: %s (device openers: %s, holder check %s, window watch %s)",
                        ", ".join(f"{o.exe} pid {o.pid}" for o in self.owners),
                        ", ".join(f"{o.exe} pid {o.pid}" for o in (openers or [])) or "unknown",
                        "on" if self.check_holders else "off",
                        "on" if self.window_watch else "off")
        else:
            self.mode = "unknown"
            logger.warning("0x41 owner not found; compensation stays on until the USBIP "
                           "connection is reset (backend %s)", type(self.backend).__name__)

    def _has_window(self, o):
        try:
            return self.backend.has_visible_window(o.pid)
        except Exception:
            return None

    def check_once(self, tick):
        """One watch step.  Returns a reason string when the owner is gone, else None."""
        if self.mode == "handle":
            alive = []
            for o in self.owners:
                try:
                    if not self.backend.is_alive(o):
                        continue
                except Exception:
                    pass
                if o.pid in self.window_watch:
                    if self._has_window(o) is False:
                        self._window_misses[o.pid] = self._window_misses.get(o.pid, 0) + 1
                        if self._window_misses[o.pid] >= 2:
                            continue  # window gone twice in a row: closing
                    else:
                        self._window_misses[o.pid] = 0
                alive.append(o)
            if not alive:
                return "owner process exited or its window closed (%s)" % ", ".join(o.exe for o in self.owners)
            if tick % 15 == 0:
                logger.info("0x41 owner watch: still running: %s",
                            ", ".join(f"{o.exe} pid {o.pid}" for o in alive))
            scan = {o.pid for o in alive} - self._stuck_pids
            if self.check_holders and scan and tick % self.holder_recheck_every == 0:
                openers = self._openers(scan)
                if openers is not None:
                    holders = {o.pid for o in openers}
                    still = [o for o in alive if o.pid in holders or o.pid not in scan]
                    if not still:
                        return "owner closed the device (%s)" % ", ".join(o.exe for o in alive)
                    alive = still
            self.owners = alive
            return None
        return None

    def _run(self):
        self._identify()
        tick = 0
        while not self._stop.wait(self.interval):
            tick += 1
            reason = self.check_once(tick)
            if reason and not self._stop.is_set():
                self._stop.set()
                try:
                    self.on_gone(reason, self)
                except Exception:
                    logger.debug("Owner-gone callback failed", exc_info=True)
                return


class _NullBackend:
    def find_openers(self, vid, pid, serial, only_pids=None):
        return None

    def is_alive(self, info):
        return True

    def any_process_named(self, names):
        return True

    def processes_named(self, names):
        return []

    def has_visible_window(self, pid):
        return None

    def hid_processes(self):
        return None


def default_backend():
    if sys.platform == "win32":
        try:
            return WinBackend()
        except Exception:
            logger.warning("0x41 owner watch: WinBackend unavailable", exc_info=True)
    return _NullBackend()


class WinBackend:
    """ctypes implementation for Windows (no admin needed for non-elevated targets)."""

    SystemExtendedHandleInformation = 64
    ObjectNameInformation = 1
    STATUS_INFO_LENGTH_MISMATCH = 0xC0000004
    PROCESS_DUP_HANDLE = 0x0040
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    DUPLICATE_SAME_ACCESS = 0x2
    STILL_ACTIVE = 259
    FILE_TYPE_DISK = 1
    FILE_TYPE_PIPE = 3
    OPEN_EXISTING = 3
    FILE_SHARE_RW = 0x3
    TH32CS_SNAPPROCESS = 0x2

    def __init__(self):
        self.progress = "start"
        self._path_cache = {}
        import ctypes
        from ctypes import wintypes
        self.ct = ctypes
        self.wt = wintypes
        self.k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.ntdll = ctypes.WinDLL("ntdll")
        self.INVALID = ctypes.c_void_p(-1).value

        class ENTRY(ctypes.Structure):
            _fields_ = [("Object", ctypes.c_void_p),
                        ("UniqueProcessId", ctypes.c_size_t),
                        ("HandleValue", ctypes.c_size_t),
                        ("GrantedAccess", wintypes.ULONG),
                        ("CreatorBackTraceIndex", wintypes.USHORT),
                        ("ObjectTypeIndex", wintypes.USHORT),
                        ("HandleAttributes", wintypes.ULONG),
                        ("Reserved", wintypes.ULONG)]

        class UNICODE_STRING(ctypes.Structure):
            _fields_ = [("Length", wintypes.USHORT),
                        ("MaximumLength", wintypes.USHORT),
                        ("Buffer", ctypes.c_void_p)]

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD),
                        ("cntUsage", wintypes.DWORD),
                        ("th32ProcessID", wintypes.DWORD),
                        ("th32DefaultHeapID", ctypes.c_size_t),
                        ("th32ModuleID", wintypes.DWORD),
                        ("cntThreads", wintypes.DWORD),
                        ("th32ParentProcessID", wintypes.DWORD),
                        ("pcPriClassBase", wintypes.LONG),
                        ("dwFlags", wintypes.DWORD),
                        ("szExeFile", wintypes.WCHAR * 260)]

        class MODULEENTRY32W(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD),
                        ("th32ModuleID", wintypes.DWORD),
                        ("th32ProcessID", wintypes.DWORD),
                        ("GlblcntUsage", wintypes.DWORD),
                        ("ProccntUsage", wintypes.DWORD),
                        ("modBaseAddr", ctypes.c_void_p),
                        ("modBaseSize", wintypes.DWORD),
                        ("hModule", wintypes.HMODULE),
                        ("szModule", wintypes.WCHAR * 256),
                        ("szExePath", wintypes.WCHAR * 260)]

        self.MODULEENTRY32W = MODULEENTRY32W
        self.ENTRY = ENTRY
        self.UNICODE_STRING = UNICODE_STRING
        self.PROCESSENTRY32W = PROCESSENTRY32W

        k = self.k32
        k.CreateFileW.restype = wintypes.HANDLE
        k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                  wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        k.OpenProcess.restype = wintypes.HANDLE
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.GetCurrentProcess.restype = wintypes.HANDLE
        k.DuplicateHandle.argtypes = [wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE,
                                      ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD,
                                      wintypes.BOOL, wintypes.DWORD]
        k.GetFileType.argtypes = [wintypes.HANDLE]
        k.GetFileType.restype = wintypes.DWORD
        k.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        k.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        k.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        k.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        k.Module32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MODULEENTRY32W)]
        k.Module32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MODULEENTRY32W)]
        self.ntdll.NtQuerySystemInformation.argtypes = [wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG,
                                                        ctypes.POINTER(wintypes.ULONG)]
        self.ntdll.NtQuerySystemInformation.restype = wintypes.LONG
        self.ntdll.NtQueryObject.argtypes = [wintypes.HANDLE, wintypes.ULONG, ctypes.c_void_p,
                                             wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)]
        self.ntdll.NtQueryObject.restype = wintypes.LONG

    # -- helpers ---------------------------------------------------------------
    def _close(self, h):
        if h and h != self.INVALID:
            self.k32.CloseHandle(h)

    def _object_name(self, handle):
        ct = self.ct
        size = ct.c_ulong(0)
        buf = ct.create_string_buffer(1024)
        status = self.ntdll.NtQueryObject(handle, self.ObjectNameInformation, buf, len(buf), ct.byref(size))
        if status != 0:
            return None
        us = self.UNICODE_STRING.from_buffer(buf)
        if not us.Buffer or not us.Length:
            return ""
        return ct.wstring_at(us.Buffer, us.Length // 2)

    def _handle_table(self):
        ct = self.ct
        size = 1 << 20
        while size <= (1 << 28):
            buf = ct.create_string_buffer(size)
            ret = ct.c_ulong(0)
            status = self.ntdll.NtQuerySystemInformation(self.SystemExtendedHandleInformation,
                                                         buf, size, ct.byref(ret))
            if (status & 0xFFFFFFFF) == self.STATUS_INFO_LENGTH_MISMATCH:
                size = max(size * 2, ret.value + (1 << 16))
                continue
            if status != 0:
                return []
            count = ct.c_size_t.from_buffer(buf).value
            offset = 2 * ct.sizeof(ct.c_size_t)
            return (self.ENTRY * count).from_buffer(buf, offset)
        return []

    def _processes(self):
        ct = self.ct
        snap = self.k32.CreateToolhelp32Snapshot(self.TH32CS_SNAPPROCESS, 0)
        if not snap or snap == self.INVALID:
            return {}
        out = {}
        try:
            pe = self.PROCESSENTRY32W()
            pe.dwSize = ct.sizeof(pe)
            ok = self.k32.Process32FirstW(snap, ct.byref(pe))
            while ok:
                out[pe.th32ProcessID] = pe.szExeFile
                ok = self.k32.Process32NextW(snap, ct.byref(pe))
        finally:
            self._close(snap)
        return out

    def _create_time(self, pid):
        ct, wt = self.ct, self.wt
        h = self.k32.OpenProcess(self.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return None
        try:
            c, e, k, u = wt.FILETIME(), wt.FILETIME(), wt.FILETIME(), wt.FILETIME()
            if not self.k32.GetProcessTimes(h, ct.byref(c), ct.byref(e), ct.byref(k), ct.byref(u)):
                return None
            return (c.dwHighDateTime << 32) | c.dwLowDateTime
        finally:
            self._close(h)

    def _device_paths(self, vid, pid, serial):
        import hid
        paths = []
        for d in hid.enumerate(vid, pid) or []:
            if serial and d.get("serial_number") not in (serial, None, ""):
                continue
            p = d.get("path")
            if isinstance(p, bytes):
                p = p.decode("utf-8", "replace")
            if p:
                paths.append(p)
        return paths

    # -- backend API -----------------------------------------------------------
    def find_openers(self, vid, pid, serial, only_pids=None):
        """ProcInfo for every other process holding the device open, or None on failure.

        only_pids limits the (relatively slow) per-handle check to those processes."""
        ct, wt = self.ct, self.wt
        # hid.enumerate makes Windows re-read the device's string descriptors, so only
        # do it once per device and reuse the paths for the periodic holder checks.
        key = (vid, pid, serial)
        paths = self._path_cache.get(key) or self._device_paths(vid, pid, serial)
        self._path_cache[key] = paths
        if not paths:
            logger.info("0x41 owner watch: no HID path for %04x:%04x serial %s", vid, pid, serial)
            return None
        own_pid = os.getpid()
        own_handles = []
        names = set()
        try:
            for p in paths:
                h = self.k32.CreateFileW(p, 0, self.FILE_SHARE_RW, None, self.OPEN_EXISTING, 0, None)
                if h and h != self.INVALID:
                    own_handles.append(h)
                    n = self._object_name(h)
                    if n:
                        names.add(n)
            if not names:
                self._path_cache.pop(key, None)
                logger.info("0x41 owner watch: could not name our own device handle (%s)", paths)
                return None
            table = self._handle_table()
            own_values = {int(h) for h in own_handles}
            type_index = None
            for e in table:
                if e.UniqueProcessId == own_pid and e.HandleValue in own_values:
                    type_index = e.ObjectTypeIndex
                    break
            if type_index is None:
                logger.info("0x41 owner watch: own handle not in handle table (%d entries)", len(table))
                return None
            by_pid = {}
            for e in table:
                pid_ = e.UniqueProcessId
                if only_pids is not None and pid_ not in only_pids:
                    continue
                if e.ObjectTypeIndex == type_index and pid_ not in (own_pid, 0, 4):
                    by_pid.setdefault(pid_, []).append(e.HandleValue)
        finally:
            for h in own_handles:
                self._close(h)

        me = self.k32.GetCurrentProcess()
        found = set()
        for pid_, handles in by_pid.items():
            hp = self.k32.OpenProcess(self.PROCESS_DUP_HANDLE, False, pid_)
            if not hp:
                continue  # elevated/protected process
            try:
                for hv in handles:
                    dup = wt.HANDLE()
                    if not self.k32.DuplicateHandle(hp, hv, me, ct.byref(dup), 0, False,
                                                    self.DUPLICATE_SAME_ACCESS):
                        continue
                    try:
                        # Pipes can block NtQueryObject forever; disk files are never ours.
                        self.progress = f"pid {pid_} handle {hv:#x} GetFileType"
                        if self.k32.GetFileType(dup) in (self.FILE_TYPE_PIPE, self.FILE_TYPE_DISK):
                            continue
                        self.progress = f"pid {pid_} handle {hv:#x} NtQueryObject"
                        if self._object_name(dup) in names:
                            found.add(pid_)
                            break
                    finally:
                        self._close(dup)
            finally:
                self._close(hp)

        procs = self._processes()
        return [ProcInfo(p, procs.get(p, "?"), self._create_time(p)) for p in sorted(found)]

    def is_alive(self, info):
        ct, wt = self.ct, self.wt
        h = self.k32.OpenProcess(self.PROCESS_QUERY_LIMITED_INFORMATION, False, info.pid)
        if not h:
            return False
        try:
            code = wt.DWORD(0)
            if not self.k32.GetExitCodeProcess(h, ct.byref(code)) or code.value != self.STILL_ACTIVE:
                return False
        finally:
            self._close(h)
        # Guard against PID reuse.
        return info.create_time is None or self._create_time(info.pid) == info.create_time

    def _has_module(self, pid, module):
        ct = self.ct
        snap = self.k32.CreateToolhelp32Snapshot(0x8 | 0x10, pid)  # SNAPMODULE | SNAPMODULE32
        if not snap or snap == self.INVALID:
            return False  # elevated/protected, or exited
        try:
            me = self.MODULEENTRY32W()
            me.dwSize = ct.sizeof(me)
            ok = self.k32.Module32FirstW(snap, ct.byref(me))
            while ok:
                if me.szModule.lower() == module:
                    return True
                ok = self.k32.Module32NextW(snap, ct.byref(me))
            return False
        finally:
            self._close(snap)

    def hid_processes(self):
        """Processes (other than system ones) that have hid.dll loaded."""
        own = os.getpid()
        out = []
        for pid, exe in self._processes().items():
            if pid in (0, 4, own):
                continue
            if self._has_module(pid, "hid.dll"):
                out.append(ProcInfo(pid, exe, self._create_time(pid)))
        return out

    def has_visible_window(self, pid):
        """True if the process owns a visible top-level window (minimised counts)."""
        ct, wt = self.ct, self.wt
        u32 = getattr(self, "_u32", None)
        if u32 is None:
            u32 = self._u32 = ct.WinDLL("user32", use_last_error=True)
            u32.GetWindowThreadProcessId.argtypes = [wt.HWND, ct.POINTER(wt.DWORD)]
            u32.IsWindowVisible.argtypes = [wt.HWND]
            self._enum_proto = ct.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
            u32.EnumWindows.argtypes = [self._enum_proto, wt.LPARAM]
        found = []

        def cb(hwnd, _):
            owner = wt.DWORD(0)
            u32.GetWindowThreadProcessId(hwnd, ct.byref(owner))
            if owner.value == pid and u32.IsWindowVisible(hwnd):
                found.append(hwnd)
                return False
            return True

        u32.EnumWindows(self._enum_proto(cb), 0)
        return bool(found)

    def processes_named(self, names):
        lowered = {n.lower() for n in names}
        return [ProcInfo(pid, exe, self._create_time(pid))
                for pid, exe in self._processes().items() if (exe or "").lower() in lowered]

    def any_process_named(self, names):
        lowered = {n.lower() for n in names}
        return any((exe or "").lower() in lowered for exe in self._processes().values())
