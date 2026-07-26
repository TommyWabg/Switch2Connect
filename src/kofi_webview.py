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

"""Standalone child process that shows the Ko-fi donation widget in a frameless
WebView2 window via pywebview.

Runs in its own process (spawned from the GUI) so pywebview owns the main thread
and its event loop never collides with the Tkinter main loop. The window is
frameless (no OS title bar) to visually match the app's in-window floating
panels (e.g. Impulse Trigger Settings). Its lifetime is owned by the parent GUI.
"""

import os
import sys
import argparse
import ctypes
import json
import time
from ctypes import wintypes

# Match the background shown around the embedded Ko-fi page. Keeping every layer
# the same colour prevents a dark flash or outline while WebView2 is loading.
_PAGE_BG = "#f9f9f9"
_WINDOW_WIDTH = 460
# Render a tall slice of the Ko-fi page inside the iframe. Made taller than the
# visible panel so the whole payment form (card, billing address, mobile, submit)
# is reachable by scrolling; the extra (_IFRAME_HEIGHT - _VIEW_HEIGHT) pixels are
# the scroll range.
_IFRAME_HEIGHT = 1200
_CROP_LEFT = 32
_CROP_TOP = 0
_CROP_RIGHT = 32
# Bottom crop kept so the visible panel height stays exactly as before (507):
# only what is shown at once is unchanged — the scrollable content grew.
_CROP_BOTTOM = _IFRAME_HEIGHT - _CROP_TOP - 507
_VIEW_WIDTH = _WINDOW_WIDTH - _CROP_LEFT - _CROP_RIGHT
_VIEW_HEIGHT = _IFRAME_HEIGHT - _CROP_TOP - _CROP_BOTTOM

# Keep Ko-fi's original tall viewport so its responsive layout does not reflow
# or grow an internal scrollbar. A scroll container lets the shorter native
# window reveal the taller iframe by scrolling vertically, while the horizontal
# crop stays fixed. The scrollbar itself is hidden so the panel stays chromeless.
_KOFI_HTML = """<!doctype html>
<html>
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
      html, body {{ margin:0; width:{view_width}px; height:{view_height}px;
        background:{bg}; overflow:hidden;
        font-family:"Segoe UI", Arial, sans-serif; }}
      /* Scroll the tall iframe vertically; clip (do not scroll) the horizontal
         crop. The scrollbar is hidden on every engine so no chrome appears. */
      #scroller {{ position:relative; width:{view_width}px; height:{view_height}px;
        overflow-x:hidden; overflow-y:auto;
        scrollbar-width:none; -ms-overflow-style:none; }}
      #scroller::-webkit-scrollbar {{ width:0; height:0; display:none; }}
      iframe {{ position:absolute; left:-{crop_left}px; top:-{crop_top}px; border:0;
        width:{width}px; height:{iframe_height}px;
        display:block; background:{bg}; }}
    </style>
  </head>
  <body>
    <div id="scroller">
      <iframe id="kofiframe"
        src="https://ko-fi.com/tagayama/?hidefeed=true&widget=true&embed=true&preview=true"
        title="tagayama" scrolling="no"></iframe>
    </div>
  </body>
</html>""".format(
    bg=_PAGE_BG,
    width=_WINDOW_WIDTH,
    view_width=_VIEW_WIDTH,
    view_height=_VIEW_HEIGHT,
    crop_left=_CROP_LEFT,
    crop_top=_CROP_TOP,
    iframe_height=_IFRAME_HEIGHT,
)


def _find_native_window():
    """Return the largest top-level HWND owned by this child process."""
    if sys.platform != "win32":
        return None

    user32 = ctypes.windll.user32
    current_pid = os.getpid()
    candidates = []

    enum_proc_type = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
    )

    def visit(hwnd, _lparam):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value != current_pid:
            return True
        rect = wintypes.RECT()
        if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            width = rect.right - rect.left
            height = rect.bottom - rect.top
            if width > 0 and height > 0:
                candidates.append((width * height, hwnd))
        return True

    callback = enum_proc_type(visit)
    user32.EnumWindows(callback, 0)
    if not candidates:
        return None

    return max(candidates, key=lambda item: item[0])[1]


def _get_window_rect(hwnd):
    rect = wintypes.RECT()
    if not ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    return {
        "left": rect.left,
        "top": rect.top,
        "right": rect.right,
        "bottom": rect.bottom,
        "width": rect.right - rect.left,
        "height": rect.bottom - rect.top,
    }


def _get_client_rect(hwnd):
    """Return the WebView client area in physical desktop coordinates."""
    user32 = ctypes.windll.user32
    rect = wintypes.RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        return None
    origin = wintypes.POINT(0, 0)
    if not user32.ClientToScreen(hwnd, ctypes.byref(origin)):
        return None
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    return {
        "left": origin.x,
        "top": origin.y,
        "right": origin.x + width,
        "bottom": origin.y + height,
        "width": width,
        "height": height,
    }


def _native_hwnd(window):
    """Get the WinForms handle exposed by pywebview 5.3+ when available."""
    try:
        handle = window.native.Handle
        value = handle.ToInt64() if hasattr(handle, "ToInt64") else int(handle)
        return wintypes.HWND(value)
    except Exception:
        return None


def _hwnd_number(hwnd):
    value = getattr(hwnd, "value", hwnd)
    return int(value or 0)


def _position_native_window(anchor_center_x, anchor_bottom_y, hwnd=None):
    """Size the client in CSS/DPI pixels, then place it below the Ko-fi button."""
    hwnd = hwnd or _find_native_window()
    if hwnd is None:
        return None

    _log_event("position_get_rect", f"hwnd={_hwnd_number(hwnd)}")
    outer = _get_window_rect(hwnd)
    client = _get_client_rect(hwnd)
    if outer is None or client is None:
        return None

    try:
        get_dpi = getattr(ctypes.windll.user32, "GetDpiForWindow", None)
        dpi_ratio = float(get_dpi(hwnd)) / 96.0 if get_dpi is not None else 1.0
    except Exception:
        dpi_ratio = 1.0
    desired_client_width = int(round(_VIEW_WIDTH * dpi_ratio))
    desired_client_height = int(round(_VIEW_HEIGHT * dpi_ratio))
    desired_outer_width = desired_client_width + outer["width"] - client["width"]
    desired_outer_height = desired_client_height + outer["height"] - client["height"]
    left = int(round(anchor_center_x - desired_outer_width / 2.0))
    top = int(anchor_bottom_y)
    flags = 0x0004 | 0x0010  # NOZORDER | NOACTIVATE
    _log_event(
        "position_set",
        f"left={left}, top={top}, width={desired_outer_width}, "
        f"height={desired_outer_height}, dpi_ratio={dpi_ratio}",
    )
    if not ctypes.windll.user32.SetWindowPos(
            hwnd, 0, left, top, desired_outer_width, desired_outer_height, flags):
        return None
    _log_event("position_complete")
    return hwnd


def _set_owner(hwnd, owner_hwnd):
    """Make the popup an owned window of the main GUI window.

    Owned windows are always kept above their owner in the z-order by Windows,
    regardless of process or foreground state. This is what actually guarantees
    the popup is visible when re-shown: a background child process cannot raise
    its window above the foreground main window with SetWindowPos alone.
    """
    if hwnd is None or not owner_hwnd or sys.platform != "win32":
        return
    try:
        user32 = ctypes.windll.user32
        GWLP_HWNDPARENT = -8
        set_long = getattr(user32, "SetWindowLongPtrW", None) or user32.SetWindowLongW
        set_long(hwnd, GWLP_HWNDPARENT, wintypes.HWND(int(owner_hwnd)))
    except Exception:
        _log_event("set_owner_error")


def _apply_tool_window_style(hwnd):
    """Give the popup a tool-window style so Windows keeps it out of the taskbar.

    Mirrors the app's in-window dialogs (e.g. Uninstall Driver), which never add
    a separate taskbar button. Must run while the window is still hidden so no
    taskbar entry ever flashes. WS_EX_APPWINDOW is cleared as well because it
    would otherwise force a taskbar button back on.
    """
    if hwnd is None or sys.platform != "win32":
        return
    try:
        user32 = ctypes.windll.user32
        GWL_EXSTYLE = -20
        WS_EX_TOOLWINDOW = 0x00000080
        WS_EX_APPWINDOW = 0x00040000
        get_long = getattr(user32, "GetWindowLongPtrW", None) or user32.GetWindowLongW
        set_long = getattr(user32, "SetWindowLongPtrW", None) or user32.SetWindowLongW
        ex_style = get_long(hwnd, GWL_EXSTYLE)
        ex_style = (ex_style | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW
        set_long(hwnd, GWL_EXSTYLE, ex_style)
    except Exception:
        _log_event("tool_window_style_error")


# Parking spot far off any monitor. The window stays genuinely shown here (so
# WebView2 keeps rendering and the page never reloads) but is invisible to the
# user. Hiding by ShowWindow(SW_HIDE) desyncs the WebView2 render surface and
# leaves the content blank on the next show, so we move instead of hide.
_OFFSCREEN_X = -32000
_OFFSCREEN_Y = -32000


def _move_offscreen(hwnd):
    """'Hide' by parking the still-visible window far off-screen."""
    if hwnd is None or sys.platform != "win32":
        return
    SWP_NOSIZE = 0x0001
    SWP_NOZORDER = 0x0004
    SWP_NOACTIVATE = 0x0010
    try:
        ctypes.windll.user32.SetWindowPos(
            hwnd, 0, _OFFSCREEN_X, _OFFSCREEN_Y, 0, 0,
            SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE,
        )
    except Exception:
        _log_event("move_offscreen_error")


def _bring_to_top(hwnd):
    """Raise the popup above the main window without activating it."""
    if hwnd is None or sys.platform != "win32":
        return
    HWND_TOP = 0
    SWP_NOSIZE = 0x0001
    SWP_NOMOVE = 0x0002
    SWP_NOACTIVATE = 0x0010
    try:
        ctypes.windll.user32.SetWindowPos(
            hwnd, HWND_TOP, 0, 0, 0, 0,
            SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE,
        )
    except Exception:
        _log_event("bring_to_top_error")


def _calibration_path():
    log_dir = os.path.join(
        os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
        "Switch 2 Connect",
    )
    os.makedirs(log_dir, exist_ok=True)
    return os.path.join(log_dir, "kofi_crop_calibration.json")


def _log_event(event, detail=""):
    """Persist startup milestones, including exceptions swallowed by callbacks."""
    try:
        log_dir = os.path.dirname(_calibration_path())
        with open(os.path.join(log_dir, "kofi_webview.log"), "a", encoding="utf-8") as output:
            output.write(f"{time.time():.3f} {event}")
            if detail:
                output.write(f": {detail}")
            output.write("\n")
    except Exception:
        pass


class _CropCalibration:
    """Keep the iframe fixed on-screen while the native edges form a crop box."""

    def __init__(self, window, hwnd):
        self.window = window
        self.hwnd = hwnd
        try:
            get_dpi = getattr(ctypes.windll.user32, "GetDpiForWindow", None)
            self.device_pixel_ratio = (
                float(get_dpi(hwnd)) / 96.0 if get_dpi is not None else 1.0
            )
        except Exception:
            self.device_pixel_ratio = 1.0
        client = _get_client_rect(hwnd)
        # The page has a stable blank strip before the actual Ko-fi panel. Start
        # calibration with that strip already removed and reduce the viewport by
        # the same amount so the calibrated right edge does not move outward.
        self.content_origin_x = client["left"] - _CROP_LEFT * self.device_pixel_ratio
        self.content_origin_y = client["top"]
        self.refresh()

    def refresh(self, *_event_args):
        client = _get_client_rect(self.hwnd)
        outer = _get_window_rect(self.hwnd)
        if client is None or outer is None:
            return

        ratio = self.device_pixel_ratio or 1.0
        iframe_left_css = (self.content_origin_x - client["left"]) / ratio
        iframe_top_css = (self.content_origin_y - client["top"]) / ratio
        try:
            self.window.evaluate_js(
                "(function(){var f=document.getElementById('kofiframe');"
                f"if(f){{f.style.left='{iframe_left_css:.4f}px';"
                f"f.style.top='{iframe_top_css:.4f}px';}}}})();"
            )
        except Exception:
            pass

        crop_left = client["left"] - self.content_origin_x
        crop_top = client["top"] - self.content_origin_y
        iframe_width_physical = _WINDOW_WIDTH * ratio
        iframe_height_physical = _IFRAME_HEIGHT * ratio
        crop_right = iframe_width_physical - crop_left - client["width"]
        crop_bottom = iframe_height_physical - crop_top - client["height"]

        def css(value):
            return round(value / ratio, 4)

        data = {
            "updated_at_unix": time.time(),
            "device_pixel_ratio": ratio,
            "window_rect_physical": outer,
            "client_rect_physical": client,
            "content_origin_physical": {
                "x": self.content_origin_x,
                "y": self.content_origin_y,
            },
            "crop_physical": {
                "left": crop_left,
                "top": crop_top,
                "right": crop_right,
                "bottom": crop_bottom,
                "width": client["width"],
                "height": client["height"],
            },
            "crop_css": {
                "left": css(crop_left),
                "top": css(crop_top),
                "right": css(crop_right),
                "bottom": css(crop_bottom),
                "width": css(client["width"]),
                "height": css(client["height"]),
            },
            "iframe_css": {"width": _WINDOW_WIDTH, "height": _IFRAME_HEIGHT},
            "pre_crop_css": {"left": _CROP_LEFT},
        }
        try:
            with open(_calibration_path(), "w", encoding="utf-8") as output:
                json.dump(data, output, ensure_ascii=False, indent=2)
        except Exception:
            pass


def _log_crash(exc_text):
    """Mirror the DualSense child's crash-logging so failures are diagnosable."""
    try:
        log_dir = os.path.join(
            os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
            "Switch 2 Connect",
        )
        os.makedirs(log_dir, exist_ok=True)
        with open(os.path.join(log_dir, "kofi_webview.log"), "a", encoding="utf-8") as f:
            f.write("Ko-fi webview child crashed:\n")
            f.write(exc_text)
            f.write("\n")
    except Exception:
        pass


def main(argv=None):
    try:
        _log_event("process_started", f"argv={argv or []}")
        import webview
        parser = argparse.ArgumentParser(add_help=False)
        parser.add_argument("--anchor-center-x", type=int)
        parser.add_argument("--anchor-bottom-y", type=int)
        parser.add_argument("--owner-hwnd", type=int)
        args, _unknown = parser.parse_known_args(argv or [])

        window_options = dict(
            html=_KOFI_HTML,
            width=_VIEW_WIDTH,
            height=_VIEW_HEIGHT,
            frameless=True,
            easy_drag=False,
            background_color=_PAGE_BG,
            resizable=False,
            # Born hidden so the window is never drawn at pywebview's default
            # screen-centre position. We reveal it only after it is moved below
            # the Ko-fi button, which removes the centre flash on open.
            hidden=True,
        )
        window = webview.create_window(
            "Support me on Ko-fi",
            **window_options,
        )
        _log_event("window_created")

        positioned = {"done": False}
        # Cache the native handle so later show/hide commands can reposition the
        # already-loaded window without recreating it (see _command_loop below).
        state = {"hwnd": None}

        def _reveal_window(stage):
            """Show the window that was created hidden, exactly once."""
            try:
                window.show()
                _log_event(f"{stage}_shown")
            except Exception:
                import traceback
                _log_event(f"{stage}_show_error", traceback.format_exc())

        def position_final_window(stage):
            if positioned["done"]:
                return True
            try:
                _log_event(stage)
                # At `shown`, pywebview's own native handle is available and is
                # the only reliable choice. Selecting the largest process-owned
                # HWND can pick a WebView2 helper/controller window instead.
                hwnd = _native_hwnd(window) or _find_native_window()
                _log_event(f"{stage}_hwnd", f"hwnd={_hwnd_number(hwnd)}")
                if (hwnd is not None and args.anchor_center_x is not None and
                        args.anchor_bottom_y is not None):
                    hwnd = _position_native_window(
                        args.anchor_center_x, args.anchor_bottom_y, hwnd
                    )
                    _log_event(f"{stage}_positioned", f"hwnd={_hwnd_number(hwnd)}")
                if hwnd is None:
                    _log_event(f"{stage}_no_hwnd")
                    return False
                state["hwnd"] = hwnd
                # Own the popup to the main window so it is always kept above it
                # in the z-order (this is what makes a re-show actually visible).
                _set_owner(hwnd, args.owner_hwnd)
                # Keep the popup out of the taskbar while it is still hidden, so
                # no taskbar button ever flashes before we reveal it.
                _apply_tool_window_style(hwnd)
                positioned["done"] = True
                # Only now that the window sits below the Ko-fi button do we
                # reveal it, so it never flashes at the screen centre.
                _reveal_window(stage)
                _log_event(f"{stage}_complete", f"hwnd={_hwnd_number(hwnd)}")
                return True
            except Exception:
                import traceback
                _log_event(f"{stage}_error", traceback.format_exc())
                return False

        def _command_loop():
            """Obey hide/show/quit commands from the parent GUI over stdin.

            Keeping this child alive and just hiding the window means reopening
            never reloads the Ko-fi page. `show` also carries a fresh anchor so
            the popup re-attaches under the button even if the main window moved
            while it was hidden.
            """
            stream = sys.stdin
            if stream is None:
                return
            try:
                for raw in stream:
                    parts = raw.strip().split()
                    if not parts:
                        continue
                    command = parts[0]
                    _log_event("command", command)
                    if command == "hide":
                        _move_offscreen(state["hwnd"] or _native_hwnd(window))
                    elif command == "show":
                        try:
                            hwnd = state["hwnd"] or _native_hwnd(window)
                            if (hwnd is not None and len(parts) >= 3):
                                _position_native_window(
                                    int(parts[1]), int(parts[2]), hwnd
                                )
                            # The window was only parked off-screen, so it is
                            # still shown; just raise it above the main window.
                            _bring_to_top(hwnd)
                        except Exception:
                            _log_event("show_command_error")
                    elif command == "move":
                        # Follow the main window: reposition only, keep visibility.
                        try:
                            hwnd = state["hwnd"] or _native_hwnd(window)
                            if (hwnd is not None and len(parts) >= 3):
                                _position_native_window(
                                    int(parts[1]), int(parts[2]), hwnd
                                )
                        except Exception:
                            _log_event("move_command_error")
                    elif command == "quit":
                        try:
                            window.destroy()
                        except Exception:
                            _log_event("quit_error")
                        break
            except Exception:
                import traceback
                _log_event("command_loop_error", traceback.format_exc())

        def reveal_fallback():
            """Guarantee the hidden window is never stuck invisible.

            If `loaded` never fires (or positioning keeps failing), force one
            positioning attempt and reveal the window regardless, so the popup
            always becomes visible.
            """
            if positioned["done"]:
                return
            _log_event("timeout_fallback")
            if not position_final_window("timeout_fallback"):
                _reveal_window("timeout_fallback")

        def after_show(*_event_args):
            _log_event("shown")
            position_final_window("shown_ui")

        def after_load(*_event_args):
            _log_event("loaded")
            if not positioned["done"]:
                position_final_window("loaded_ui_fallback")

        window.events.shown += after_show
        window.events.loaded += after_load

        # Safety net: if `loaded` never arrives the window would stay hidden
        # forever. Reveal it after a short grace period no matter what.
        import threading
        _fallback_timer = threading.Timer(3.0, reveal_fallback)
        _fallback_timer.daemon = True
        _fallback_timer.start()

        # Listen for parent hide/show/quit commands on a background thread so the
        # main thread stays free for pywebview's event loop.
        _command_thread = threading.Thread(target=_command_loop, daemon=True)
        _command_thread.start()

        _log_event("webview_start")
        webview.start()
    except Exception:
        import traceback
        _log_crash(traceback.format_exc())
        raise


if __name__ == "__main__":
    main(sys.argv[1:])
