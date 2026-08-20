"""Global Standard/WinUHid keyboard output selection and key ownership."""

import logging
import threading

logger = logging.getLogger(__name__)

STANDARD = "Standard"
RAW_INPUT = "Raw Input"

# USB HID Keyboard/Keypad usages for Tk/Win32-style VK token names produced by
# the mapping UI. Generic modifiers intentionally resolve to their left side.
_NAMED_USAGES = {
    "RETURN": 0x28, "ENTER": 0x28, "ESCAPE": 0x29, "ESC": 0x29,
    "BACKSPACE": 0x2A, "BACK": 0x2A, "TAB": 0x2B, "SPACE": 0x2C,
    "MINUS": 0x2D, "EQUAL": 0x2E, "BRACKETLEFT": 0x2F,
    "BRACKETRIGHT": 0x30, "BACKSLASH": 0x31, "SEMICOLON": 0x33,
    "APOSTROPHE": 0x34, "GRAVE": 0x35, "COMMA": 0x36,
    "PERIOD": 0x37, "SLASH": 0x38, "CAPS_LOCK": 0x39,
    "CAPITAL": 0x39, "PRINT": 0x46, "SNAPSHOT": 0x46,
    "SCROLL_LOCK": 0x47, "PAUSE": 0x48, "INSERT": 0x49,
    "HOME": 0x4A, "PRIOR": 0x4B, "PAGE_UP": 0x4B, "DELETE": 0x4C,
    "END": 0x4D, "NEXT": 0x4E, "PAGE_DOWN": 0x4E, "RIGHT": 0x4F,
    "LEFT": 0x50, "DOWN": 0x51, "UP": 0x52, "NUM_LOCK": 0x53,
    "DIVIDE": 0x54, "MULTIPLY": 0x55, "SUBTRACT": 0x56,
    "ADD": 0x57, "SEPARATOR": 0x58, "DECIMAL": 0x63,
}
_MODIFIERS = {
    "CONTROL": 0xE0, "CONTROL_L": 0xE0, "LCONTROL": 0xE0,
    "SHIFT": 0xE1, "SHIFT_L": 0xE1, "LSHIFT": 0xE1,
    "MENU": 0xE2, "ALT": 0xE2, "ALT_L": 0xE2, "LMENU": 0xE2,
    "LWIN": 0xE3, "WIN": 0xE3, "WIN_L": 0xE3,
    "CONTROL_R": 0xE4, "RCONTROL": 0xE4,
    "SHIFT_R": 0xE5, "RSHIFT": 0xE5,
    "ALT_R": 0xE6, "RMENU": 0xE6,
    "RWIN": 0xE7, "WIN_R": 0xE7,
}


def token_to_hid_usage(token):
    if not isinstance(token, str) or not token.startswith("VK_"):
        return None
    name = token[3:].upper()
    if name in _MODIFIERS:
        return _MODIFIERS[name]
    if len(name) == 1 and "A" <= name <= "Z":
        return 0x04 + ord(name) - ord("A")
    if len(name) == 1 and "1" <= name <= "9":
        return 0x1E + ord(name) - ord("1")
    if name == "0":
        return 0x27
    if name.startswith("F") and name[1:].isdigit():
        number = int(name[1:])
        if 1 <= number <= 12:
            return 0x3A + number - 1
        if 13 <= number <= 24:
            return 0x68 + number - 13
    if name.startswith("KP_") and name[3:].isdigit() and len(name[3:]) == 1:
        digit = int(name[3:])
        return 0x62 if digit == 0 else 0x58 + digit
    return _NAMED_USAGES.get(name)


def _legacy_vk_code(token):
    if not isinstance(token, str) or not token.startswith("VK_"):
        return None
    name = token[3:].upper()
    try:
        import win32con
        value = getattr(win32con, f"VK_{name}", None)
        if value is not None:
            return value
        if len(name) == 1:
            return ord(name)
        aliases = {
            "CONTROL_L": win32con.VK_CONTROL, "CONTROL_R": win32con.VK_CONTROL,
            "SHIFT_L": win32con.VK_SHIFT, "SHIFT_R": win32con.VK_SHIFT,
            "ALT": win32con.VK_MENU, "ALT_L": win32con.VK_MENU,
            "ALT_R": win32con.VK_MENU, "RETURN": win32con.VK_RETURN,
            "ESCAPE": win32con.VK_ESCAPE, "BACKSPACE": win32con.VK_BACK,
        }
        return aliases.get(name)
    except Exception:
        return None


class KeyboardOutputManager:
    def __init__(self):
        self._lock = threading.RLock()
        self._keyboard = None
        self._effective_mode = STANDARD
        self._owners = {}  # HID usage -> set(source ids)
        self._standard_down = set()  # (source id, token)

    def _driver_available(self):
        try:
            from driver_install_helper import (
                WINUHID_HEALTHY, get_winuhid_status, is_winuhid_usable)
            status = get_winuhid_status(use_cache=False)
            if status.state == WINUHID_HEALTHY:
                return True
            if status.unknown:
                if is_winuhid_usable(use_cache=False):
                    return True
                try:
                    from winuhid_client import driver_interface_version
                    return driver_interface_version() > 0
                except Exception:
                    return False
        except Exception as exc:
            logger.debug("WinUHid keyboard capability probe failed: %s", exc)
        return False

    def requested_mode(self):
        from config import CONFIG
        mode = getattr(CONFIG, "keyboard_output_mode", None)
        if mode in (STANDARD, RAW_INPUT):
            return mode
        return RAW_INPUT if self._driver_available() else STANDARD

    def initialize(self):
        from config import CONFIG
        requested = self.requested_mode()
        if requested == RAW_INPUT:
            # Persist the capability-derived Raw Input default only after the
            # device really exists.  Leave an unavailable first run unset so a
            # later driver installation can still receive the Raw Input default.
            self.activate_raw_input(save=getattr(CONFIG, "keyboard_output_mode", None) is None)
        else:
            self.activate_standard(save=False)
        return self.effective_mode()

    def effective_mode(self):
        with self._lock:
            if self._effective_mode == RAW_INPUT and (
                    self._keyboard is None or self._keyboard.device is None):
                return STANDARD
            return self._effective_mode

    def activate_raw_input(self, save=True):
        with self._lock:
            if self._keyboard is not None and self._keyboard.device is not None:
                self._effective_mode = RAW_INPUT
                if save:
                    self._save_mode(RAW_INPUT)
                return True
            if not self._driver_available():
                self._effective_mode = STANDARD
                return False
            try:
                from winuhid_client import VKeyboard
                keyboard = VKeyboard()
            except Exception:
                logger.exception("Failed to create WinUHid virtual keyboard")
                self._effective_mode = STANDARD
                return False
            if keyboard.device is None:
                keyboard.close()
                self._effective_mode = STANDARD
                return False
            # Never move a held Win32 key across backends. Release the old sink
            # first so no key-down can be stranded during a live mode switch.
            self._release_all_locked()
            stale_keyboard = self._keyboard
            self._keyboard = keyboard
            self._owners.clear()
            self._effective_mode = RAW_INPUT
            if stale_keyboard is not None and stale_keyboard is not keyboard:
                stale_keyboard.close()
            if save:
                self._save_mode(RAW_INPUT)
            logger.info("WinUHid virtual keyboard enabled")
            return True

    def activate_standard(self, save=True):
        with self._lock:
            self._release_all_locked()
            keyboard = self._keyboard
            self._keyboard = None
            self._effective_mode = STANDARD
            if keyboard is not None:
                keyboard.close()
            if save:
                self._save_mode(STANDARD)
            logger.info("Standard Win32 keyboard output enabled")
            return True

    @staticmethod
    def _save_mode(mode):
        from config import CONFIG
        CONFIG.keyboard_output_mode = mode
        CONFIG.save_config()

    def key_event(self, token, down, source_id):
        if not isinstance(token, str) or not token.startswith("VK_"):
            return False
        with self._lock:
            if self._effective_mode == STANDARD and self._keyboard is None:
                try:
                    from config import CONFIG
                    if getattr(CONFIG, "keyboard_output_mode", None) == RAW_INPUT:
                        self.activate_raw_input(save=False)
                except Exception:
                    pass
            if self.effective_mode() == RAW_INPUT:
                usage = token_to_hid_usage(token)
                if usage is None:
                    # Unsupported Consumer/media usages retain their established
                    # Win32 behaviour until a Consumer Control collection is added.
                    return self._standard_event(token, down, source_id)
                owners = self._owners.setdefault(usage, set())
                if down:
                    owners.add(source_id)
                else:
                    owners.discard(source_id)
                    if not owners:
                        self._owners.pop(usage, None)
                if not self._submit_locked():
                    logger.error("WinUHid keyboard report failed; reverting to Standard")
                    self.activate_standard(save=False)
                    return False
                return True
            return self._standard_event(token, down, source_id)

    def _standard_event(self, token, down, source_id):
        vk_code = _legacy_vk_code(token)
        if vk_code is None:
            return False
        identity = (source_id, token)
        already_down = identity in self._standard_down
        if down:
            self._standard_down.add(identity)
        else:
            self._standard_down.discard(identity)
        token_still_down = any(held_token == token for _owner, held_token in self._standard_down)
        # Repeated down from the same source is intentional (legacy auto-repeat).
        # A second owner must not synthesize another transition, and one owner
        # releasing must not lift a key still held by somebody else.
        if down and not already_down and token_still_down and any(
                owner != source_id and held_token == token
                for owner, held_token in self._standard_down):
            return True
        if not down and token_still_down:
            return True
        try:
            import win32api
            import win32con
            flags = 0 if down else win32con.KEYEVENTF_KEYUP
            win32api.keybd_event(vk_code, 0, flags, 0)
            return True
        except Exception:
            logger.exception("Standard keyboard output failed for %s", token)
            return False

    def _submit_locked(self):
        modifiers = 0
        keys = []
        for usage, owners in self._owners.items():
            if not owners:
                continue
            if 0xE0 <= usage <= 0xE7:
                modifiers |= 1 << (usage - 0xE0)
            else:
                keys.append(usage)
        return bool(self._keyboard and self._keyboard.report_state(modifiers, keys))

    def release_source(self, source_prefix):
        with self._lock:
            owner_prefix = f"{source_prefix}:"
            changed = False
            for usage in list(self._owners):
                owners = self._owners[usage]
                removed = {owner for owner in owners if str(owner).startswith(owner_prefix)}
                if removed:
                    owners.difference_update(removed)
                    changed = True
                if not owners:
                    self._owners.pop(usage, None)
            if changed and self._keyboard is not None:
                self._submit_locked()
            for owner, token in list(self._standard_down):
                if str(owner).startswith(owner_prefix):
                    self._standard_event(token, False, owner)

    def _release_all_locked(self):
        self._owners.clear()
        if self._keyboard is not None and self._keyboard.device is not None:
            try:
                self._keyboard.report_state(0, ())
            except Exception:
                pass
        for owner, token in list(self._standard_down):
            self._standard_event(token, False, owner)
        self._standard_down.clear()

    def release_all(self):
        with self._lock:
            self._release_all_locked()

    def shutdown(self):
        self.activate_standard(save=False)


MANAGER = KeyboardOutputManager()


def initialize():
    return MANAGER.initialize()


def effective_mode():
    return MANAGER.effective_mode()


def activate_raw_input(save=True):
    return MANAGER.activate_raw_input(save=save)


def activate_standard(save=True):
    return MANAGER.activate_standard(save=save)


def key_event(token, down, source_id):
    return MANAGER.key_event(token, down, source_id)


def release_source(source_prefix):
    MANAGER.release_source(source_prefix)


def release_all():
    MANAGER.release_all()


def shutdown():
    MANAGER.shutdown()
