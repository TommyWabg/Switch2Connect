import ctypes
import logging
import threading

logger = logging.getLogger(__name__)

_VT_EMPTY = 0
_VT_LPWSTR = 31


_DEVICE_STATE_ACTIVE = 0x00000001
_STGM_READ = 0x00000000
_ROLE_NAMES = {0: "Console", 1: "Multimedia", 2: "Communications"}
_ROLE_FALLBACK_ORDER = (1, 0, 2)

_PROTECTED_NAME_TOKENS = (
    "wireless controller audio",
    "dualsense wireless controller",
    "headset earphone (wireless controller)",
    "headset microphone (wireless controller)",
    "wireless controller",
    "dualsense",
    "vid_054c",
    "pid_0ce6",
    "054c&0ce6",
)

_AUDIO_ENDPOINT_HINTS = (
    "audio",
    "earphone",
    "headset",
    "microphone",
    "speakers",
    "speaker",
    "headphones",
    "\u8033\u6a5f",
    "\u9ea5\u514b\u98a8",
    "\u626c\u58f0\u5668",
    "\u9ea6\u514b\u98ce",
)


class DualSenseAudioEndpointGuard:
    """Keep DualSense/Wireless Controller audio endpoints away from Windows defaults."""

    def __init__(self, poll_interval=0.5):
        self.poll_interval = poll_interval
        self._stop_event = threading.Event()
        self._thread = None
        self._defaults = {}
        self._init_error_logged = False
        self._interfaces_ready = False
        self._comtypes = None
        self._guard_lock = threading.Lock()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="DSAudioDefaultGuard", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        self._thread = None

    def restore_now(self):
        # One-shot restore, possibly from a different thread than _run (e.g. the
        # controller-disconnect path).  COM objects are apartment-bound, so this runs
        # its own CoInitialize + fresh enumerator/policy rather than reusing the guard
        # thread's.
        try:
            self._run_tick_isolated()
        except Exception:
            logger.warning("DualSense audio default guard restore failed", exc_info=True)

    def _run(self):
        if not self._ensure_interfaces_ready():
            return
        logger.info("DualSense audio default guard started")
        comtypes = self._comtypes
        initialized = self._co_initialize()
        enumerator = policy = None
        try:
            while not self._stop_event.wait(self.poll_interval):
                try:
                    if enumerator is None or policy is None:
                        enumerator, policy = self._create_com_objects()
                    with self._guard_lock:
                        self._restore_protected_defaults(enumerator, policy)
                except Exception:
                    logger.warning("DualSense audio default guard tick failed", exc_info=True)
                    enumerator = policy = None  # rebuild on the next tick
        finally:
            if initialized:
                comtypes.CoUninitialize()

    def _run_tick_isolated(self):
        if not self._ensure_interfaces_ready():
            return
        comtypes = self._comtypes
        initialized = self._co_initialize()
        try:
            enumerator, policy = self._create_com_objects()
            with self._guard_lock:
                self._restore_protected_defaults(enumerator, policy)
        finally:
            if initialized:
                comtypes.CoUninitialize()

    def _co_initialize(self):
        """CoInitialize the current thread; return True only if we own the init.

        Returns False (and skips CoUninitialize later) if COM was already initialized
        on this thread in another apartment mode, so we don't tear down someone else's.
        """
        try:
            self._comtypes.CoInitialize()
            return True
        except OSError:
            return False

    def _create_com_objects(self):
        comtypes = self._comtypes
        enumerator = comtypes.client.CreateObject(
            comtypes.GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}"),
            interface=self._IMMDeviceEnumerator,
            clsctx=comtypes.CLSCTX_ALL,
        )
        policy = comtypes.client.CreateObject(
            comtypes.GUID("{870AF99C-171D-4F9E-AF0D-E63DF40C2BC9}"),
            interface=self._IPolicyConfig,
            clsctx=comtypes.CLSCTX_ALL,
        )
        return enumerator, policy

    def _ensure_interfaces_ready(self):
        if self._interfaces_ready:
            return True
        try:
            import comtypes
            import comtypes.client
            from comtypes import COMMETHOD, GUID, HRESULT, IUnknown, POINTER
            from ctypes import Structure, Union, c_void_p, c_wchar_p, c_ushort, c_ulonglong
            from ctypes.wintypes import DWORD, UINT
        except Exception as exc:
            if not self._init_error_logged:
                logger.warning("DualSense audio default guard disabled: comtypes is unavailable (%s)", exc)
                self._init_error_logged = True
            return False

        # comtypes has no PROPVARIANT (its VARIANT can't carry a VT_LPWSTR, which is
        # how PKEY_Device_FriendlyName comes back).  Declare the minimal PROPVARIANT
        # ourselves — the friendly name lives in the pwszVal arm of the union.
        class _PROPVARIANT_UNION(Union):
            _fields_ = [("pwszVal", c_wchar_p), ("ullVal", c_ulonglong)]

        class PROPVARIANT(Structure):
            _fields_ = [
                ("vt", c_ushort),
                ("wReserved1", c_ushort),
                ("wReserved2", c_ushort),
                ("wReserved3", c_ushort),
                ("union", _PROPVARIANT_UNION),
            ]

        class PROPERTYKEY(Structure):
            _fields_ = [("fmtid", GUID), ("pid", DWORD)]

        class IPropertyStore(IUnknown):
            _iid_ = GUID("{886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99}")
            _methods_ = [
                COMMETHOD([], HRESULT, "GetCount", (["out"], POINTER(DWORD), "cProps")),
                COMMETHOD([], HRESULT, "GetAt", (["in"], DWORD, "iProp"), (["out"], POINTER(PROPERTYKEY), "pkey")),
                COMMETHOD([], HRESULT, "GetValue", (["in"], POINTER(PROPERTYKEY), "key"), (["out"], POINTER(PROPVARIANT), "pv")),
                COMMETHOD([], HRESULT, "SetValue", (["in"], POINTER(PROPERTYKEY), "key"), (["in"], POINTER(PROPVARIANT), "propvar")),
                COMMETHOD([], HRESULT, "Commit"),
            ]

        class IMMDevice(IUnknown):
            _iid_ = GUID("{D666063F-1587-4E43-81F1-B948E807363F}")
            _methods_ = [
                COMMETHOD([], HRESULT, "Activate", (["in"], POINTER(GUID), "iid"), (["in"], DWORD, "dwClsCtx"), (["in"], c_void_p, "pActivationParams"), (["out"], POINTER(c_void_p), "ppInterface")),
                COMMETHOD([], HRESULT, "OpenPropertyStore", (["in"], DWORD, "stgmAccess"), (["out"], POINTER(POINTER(IPropertyStore)), "ppProperties")),
                COMMETHOD([], HRESULT, "GetId", (["out"], POINTER(c_wchar_p), "ppstrId")),
                COMMETHOD([], HRESULT, "GetState", (["out"], POINTER(DWORD), "pdwState")),
            ]

        class IMMDeviceCollection(IUnknown):
            _iid_ = GUID("{0BD7A1BE-7A1A-44DB-8397-CC5392387B5E}")
            _methods_ = [
                COMMETHOD([], HRESULT, "GetCount", (["out"], POINTER(UINT), "pcDevices")),
                COMMETHOD([], HRESULT, "Item", (["in"], UINT, "nDevice"), (["out"], POINTER(POINTER(IMMDevice)), "ppDevice")),
            ]

        class IMMDeviceEnumerator(IUnknown):
            _iid_ = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
            _methods_ = [
                COMMETHOD([], HRESULT, "EnumAudioEndpoints", (["in"], DWORD, "dataFlow"), (["in"], DWORD, "dwStateMask"), (["out"], POINTER(POINTER(IMMDeviceCollection)), "ppDevices")),
                COMMETHOD([], HRESULT, "GetDefaultAudioEndpoint", (["in"], DWORD, "dataFlow"), (["in"], DWORD, "role"), (["out"], POINTER(POINTER(IMMDevice)), "ppEndpoint")),
                COMMETHOD([], HRESULT, "GetDevice", (["in"], c_wchar_p, "pwstrId"), (["out"], POINTER(POINTER(IMMDevice)), "ppDevice")),
                COMMETHOD([], HRESULT, "RegisterEndpointNotificationCallback", (["in"], c_void_p, "pClient")),
                COMMETHOD([], HRESULT, "UnregisterEndpointNotificationCallback", (["in"], c_void_p, "pClient")),
            ]

        class IPolicyConfig(IUnknown):
            _iid_ = GUID("{F8679F50-850A-41CF-9C72-430F290290C8}")
            _methods_ = [
                COMMETHOD([], HRESULT, "GetMixFormat"),
                COMMETHOD([], HRESULT, "GetDeviceFormat"),
                COMMETHOD([], HRESULT, "ResetDeviceFormat"),
                COMMETHOD([], HRESULT, "SetDeviceFormat"),
                COMMETHOD([], HRESULT, "GetProcessingPeriod"),
                COMMETHOD([], HRESULT, "SetProcessingPeriod"),
                COMMETHOD([], HRESULT, "GetShareMode"),
                COMMETHOD([], HRESULT, "SetShareMode"),
                COMMETHOD([], HRESULT, "GetPropertyValue"),
                COMMETHOD([], HRESULT, "SetPropertyValue"),
                COMMETHOD([], HRESULT, "SetDefaultEndpoint", (["in"], c_wchar_p, "wszDeviceId"), (["in"], DWORD, "role")),
                COMMETHOD([], HRESULT, "SetEndpointVisibility"),
            ]

        self._comtypes = comtypes
        self._IMMDeviceEnumerator = IMMDeviceEnumerator
        self._IPolicyConfig = IPolicyConfig
        self._PKEY_Device_FriendlyName = PROPERTYKEY(GUID("{A45C254E-DF1C-4EFD-8020-67D146A850E0}"), 14)
        self._interfaces_ready = True
        return True

    def _restore_protected_defaults(self, enumerator, policy):
        for flow in (0, 1):
            active = self._enumerate_active_endpoints(enumerator, flow)
            fallback = self._choose_fallback_endpoint(enumerator, flow, active)
            for role in (0, 1, 2):
                current = self._get_default_endpoint(enumerator, flow, role)
                if not current:
                    logger.info("Audio guard default %s/%s unavailable", self._flow_name(flow), _ROLE_NAMES[role])
                    continue

                current_id, current_name = current
                protected = self._is_protected_id_or_name(current_id, name=current_name)
                if protected:
                    logger.info(
                        "Audio guard default %s/%s=%r protected=True id=%s",
                        self._flow_name(flow),
                        _ROLE_NAMES[role],
                        current_name,
                        self._short_id(current_id),
                    )
                else:
                    self._defaults[(flow, role)] = current_id
                    continue

                target_id = self._defaults.get((flow, role))
                target_name = ""
                if target_id and self._is_protected_id_or_name(
                    target_id,
                    name=self._get_device_name_by_id(enumerator, target_id),
                ):
                    target_id = None
                if not target_id and fallback:
                    target_id, target_name = fallback
                if not target_id or target_id == current_id:
                    logger.warning(
                        "Audio guard cannot move default %s/%s: no non-DualSense fallback endpoint",
                        self._flow_name(flow),
                        _ROLE_NAMES[role],
                    )
                    continue

                self._set_default_endpoint(policy, enumerator, flow, role, current_name, target_id, target_name)

    def _set_default_endpoint(self, policy, enumerator, flow, role, current_name, target_id, target_name):
        if not target_name:
            target_name = self._get_device_name_by_id(enumerator, target_id)
        try:
            policy.SetDefaultEndpoint(target_id, role)
        except Exception:
            logger.warning(
                "Audio guard SetDefaultEndpoint failed %s/%s from %r to %r id=%s",
                self._flow_name(flow),
                _ROLE_NAMES[role],
                current_name,
                target_name,
                self._short_id(target_id),
                exc_info=True,
            )
            return

        verify = self._get_default_endpoint(enumerator, flow, role)
        verify_name = verify[1] if verify else ""
        verify_id = verify[0] if verify else ""
        if verify and not self._is_protected_id_or_name(verify_id, name=verify_name):
            logger.info(
                "Audio guard moved default %s/%s from %r to %r",
                self._flow_name(flow),
                _ROLE_NAMES[role],
                current_name,
                verify_name,
            )
        else:
            logger.warning(
                "Audio guard SetDefaultEndpoint returned but verify still protected %s/%s now=%r id=%s target=%r",
                self._flow_name(flow),
                _ROLE_NAMES[role],
                verify_name,
                self._short_id(verify_id),
                target_name,
            )

    def _choose_fallback_endpoint(self, enumerator, flow, active):
        for role in _ROLE_FALLBACK_ORDER:
            endpoint = self._get_default_endpoint(enumerator, flow, role)
            if endpoint and not self._is_protected_id_or_name(endpoint[0], name=endpoint[1]):
                return endpoint
        return active[0] if active else None

    def _enumerate_active_endpoints(self, enumerator, flow):
        endpoints = []
        try:
            collection = enumerator.EnumAudioEndpoints(flow, _DEVICE_STATE_ACTIVE)
            count = collection.GetCount()
            for idx in range(count):
                device = collection.Item(idx)
                dev_id = device.GetId()
                name = self._get_device_name(device)
                protected = self._is_protected_id_or_name(dev_id, name=name)
                logger.debug(
                    "Audio guard endpoint %s name=%r protected=%s id=%s",
                    self._flow_name(flow),
                    name,
                    protected,
                    self._short_id(dev_id),
                )
                if dev_id and not protected:
                    endpoints.append((dev_id, name))
        except Exception:
            logger.warning("Audio guard failed to enumerate %s endpoints", self._flow_name(flow), exc_info=True)
        return endpoints

    def _get_default_endpoint(self, enumerator, flow, role):
        try:
            device = enumerator.GetDefaultAudioEndpoint(flow, role)
            return (device.GetId(), self._get_device_name(device))
        except Exception:
            return None

    def _get_device_name_by_id(self, enumerator, dev_id):
        try:
            return self._get_device_name(enumerator.GetDevice(dev_id))
        except Exception:
            return ""

    def _get_device_name(self, device):
        value = None
        try:
            store = device.OpenPropertyStore(_STGM_READ)
            value = store.GetValue(self._PKEY_Device_FriendlyName)
            if value.vt == _VT_LPWSTR:
                text = value.union.pwszVal
                return str(text) if text else ""
            return ""
        except Exception:
            return ""
        finally:
            # PropVariantClear frees the CoTaskMem string PKEY_Device_FriendlyName
            # allocates; the guard reads every endpoint's name twice a second, so
            # skipping this leaks memory steadily.
            if value is not None:
                try:
                    ctypes.windll.ole32.PropVariantClear(ctypes.byref(value))
                except Exception:
                    pass

    def _is_protected_id_or_name(self, dev_id, name=None):
        text = (dev_id or "").lower()
        name_text = (name or "").lower()
        joined = text + " " + name_text
        if any(token in joined for token in _PROTECTED_NAME_TOKENS):
            if "wireless controller" not in joined:
                return True
            return any(hint in joined for hint in _AUDIO_ENDPOINT_HINTS) or "dualsense" in joined or "054c" in joined
        return "054c" in joined and "0ce6" in joined

    @staticmethod
    def _flow_name(flow):
        return "capture" if flow == 1 else "render"

    @staticmethod
    def _short_id(dev_id):
        if not dev_id:
            return ""
        return dev_id if len(dev_id) <= 96 else dev_id[:93] + "..."
