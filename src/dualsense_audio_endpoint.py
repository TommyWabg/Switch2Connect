"""Automatic enable/disable of the virtual DualSense audio (haptic) endpoint.

Why this exists
---------------
The virtual DualSense exposes a USB Audio Streaming OUT endpoint (ep 0x01,
4-channel/16-bit/48 kHz).  PS5 audio-haptics are delivered as channels 2 & 3 of
that stream.  Empirically, when this endpoint is ENABLED as a normal Windows
playback device, the Windows audio engine (audiodg) claims and mixes it, which
prevents the game from delivering clean 4-channel haptic data.  DISABLING the
"Wireless Controller Audio" playback device releases it so the game can drive the
endpoint directly, and haptics work.

The virtual DualSense uses a FIXED USB serial (see usbip_dualsense_server.py), so
the endpoint GUID is stable and Windows REMEMBERS the disabled state across
reconnects.

Mechanism
---------
We shell out to NirSoft's svcl.exe (console build) or SoundVolumeView.exe (GUI
build) — either accepts the same /Disable //Enable options.  CRUCIALLY we do NOT
trust the exit code (these tools often return 0 even when nothing matched).
Instead we dump the endpoint list (/scomma to a temp CSV), locate the render
endpoint by name, issue the disable, then re-dump and VERIFY the Device State
actually became "Disabled".  Enable/disable normally needs no admin (it goes
through the audio policy service); we retry once elevated only if verification
fails.

Place the tool at drivers/tools/svcl.exe (or SoundVolumeView.exe).  If it is
missing this module logs a warning and becomes a no-op.
"""

import csv
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time

logger = logging.getLogger(__name__)

# Friendly name of the virtual DualSense audio device as shown by Windows.
_DEFAULT_NAME = "Wireless Controller Audio"

# Either NirSoft binary works: svcl.exe (console) or SoundVolumeView.exe (GUI).
_EXE_NAMES = ("svcl.exe", "SoundVolumeView.exe")

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0

_warned_missing = False
_task_reg_attempted = False  # one registration attempt per process (avoid UAC spam)


def _is_admin():
    """True if the current process is already elevated (admin)."""
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Scheduled-task elevation (silent admin after a one-time UAC at registration)
# ---------------------------------------------------------------------------
# Windows re-enables the virtual DualSense audio endpoint on every USBIP re-attach,
# so we must re-disable on every connect — and disabling the endpoint needs admin
# on most systems, which would pop UAC every single time.  A Scheduled Task that
# runs with Highest privileges lets a NON-elevated process trigger the elevated
# disable silently (no UAC) once the task is registered.  Registering the task
# needs admin exactly ONCE.
_TASK_NAME = "Switch2Controllers_DisableDualSenseAudio"
_FROZEN_FLAG = "--disable-dualsense-audio-endpoint"


def _disable_entry_argv():
    """The command (argv list) that performs the elevated disable when executed."""
    if getattr(sys, "frozen", False):
        return [sys.executable, _FROZEN_FLAG]
    py = sys.executable
    pyw = os.path.join(os.path.dirname(py), "pythonw.exe")
    if os.path.exists(pyw):
        py = pyw
    return [py, os.path.abspath(__file__), "disable"]


def _task_exists():
    try:
        r = subprocess.run(["schtasks", "/query", "/tn", _TASK_NAME, "/xml"],
                           capture_output=True, text=True, creationflags=_NO_WINDOW)
        if r.returncode != 0:
            return False
        # Ensure the scheduled task is pointing to our CURRENT executable, not an old one
        exe_path = _disable_entry_argv()[0]
        if exe_path not in r.stdout:
            return False
        return True
    except Exception:
        return False


def _register_task():
    """Register the elevated disable task.  One UAC prompt.  Returns True on success.

    Uses PowerShell's Register-ScheduledTask (clean argument quoting, unlike the
    schtasks /tr nested-quote minefield).  The task runs as the current user with
    RunLevel Highest, so a later `schtasks /run` triggers it elevated with NO UAC.
    """
    argv = _disable_entry_argv()
    execute = argv[0]
    argument = " ".join(f'"{a}"' if " " in a else a for a in argv[1:])

    def _ps_lit(s):  # single-quoted PowerShell literal (double any embedded quote)
        return "'" + s.replace("'", "''") + "'"

    ps = (
        "$ErrorActionPreference='Stop';"
        f"$a=New-ScheduledTaskAction -Execute {_ps_lit(execute)} -Argument {_ps_lit(argument)};"
        "$p=New-ScheduledTaskPrincipal -UserId $env:UserName -RunLevel Highest -LogonType Interactive;"
        "$s=New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries;"
        f"Register-ScheduledTask -TaskName {_ps_lit(_TASK_NAME)} -Action $a -Principal $p -Settings $s -Force | Out-Null"
    )
    ps1 = os.path.join(tempfile.gettempdir(), "sw2_reg_audio_task.ps1")
    try:
        with open(ps1, "w", encoding="utf-8") as f:
            f.write(ps)
        try:
            import ctypes
            ctypes.windll.shell32.ShellExecuteW(
                None, "runas", "powershell.exe",
                f'-NoProfile -ExecutionPolicy Bypass -File "{ps1}"', None, 0)
        except Exception as e:
            logger.debug("task registration runas failed: %s", e)
        # ShellExecute returns once the elevated process starts; poll for the task.
        for _ in range(12):
            if _task_exists():
                logger.info("Registered scheduled task %r for silent elevated "
                            "audio-endpoint disable.", _TASK_NAME)
                return True
            time.sleep(0.5)
    finally:
        try:
            os.remove(ps1)
        except OSError:
            pass
    logger.warning("Could not register scheduled task %r (UAC declined?).", _TASK_NAME)
    return False


def _run_task():
    """Trigger the elevated disable task (no UAC).  Returns True if it launched."""
    try:
        r = subprocess.run(["schtasks", "/run", "/tn", _TASK_NAME],
                           capture_output=True, text=True, creationflags=_NO_WINDOW)
        return r.returncode == 0
    except Exception as e:
        logger.debug("schtasks /run failed: %s", e)
        return False


def _svcl_path():
    """Locate the bundled svcl.exe / SoundVolumeView.exe.  Returns None if absent."""
    dirs = []
    try:
        from config import get_driver_path  # type: ignore[import]
        dirs.append(os.path.dirname(get_driver_path(os.path.join("tools", "x"))))
    except Exception:
        pass
    here = os.path.dirname(os.path.abspath(__file__))
    dirs.append(here)
    dirs.append(os.path.join(os.path.dirname(here), "drivers", "tools"))
    if hasattr(sys, "frozen"):
        dirs.append(os.path.dirname(sys.executable))
        
    for d in dirs:
        for name in _EXE_NAMES:
            c = os.path.join(d, name)
            if os.path.exists(c):
                return c
    return None


def _target_name():
    try:
        from config import CONFIG as _cfg  # type: ignore[import]
        return getattr(_cfg, "dualsense_audio_endpoint_name", None) or _DEFAULT_NAME
    except Exception:
        return _DEFAULT_NAME


def _feature_enabled():
    try:
        from config import CONFIG as _cfg  # type: ignore[import]
        return bool(getattr(_cfg, "auto_disable_dualsense_audio_endpoint", True))
    except Exception:
        return True


def _run(svcl, args, elevated=False):
    """Run the tool.  Returns True if the process launched/exited without error."""
    if elevated:
        try:
            import ctypes
            params = " ".join(f'"{a}"' if (" " in a or a == "") else a for a in args)
            rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", svcl, params, None, 0)
            return int(rc) > 32
        except Exception as e:
            logger.debug("elevated svcl launch failed: %s", e)
            return False
    try:
        subprocess.run([svcl] + list(args), capture_output=True, text=True,
                       stdin=subprocess.DEVNULL, timeout=8, creationflags=_NO_WINDOW)
        return True
    except Exception as e:
        logger.debug("svcl run failed: %s", e)
        return False


def _dump(svcl):
    """Return the audio endpoints as a list of dict rows (via /scomma temp CSV)."""
    tmp = os.path.join(tempfile.gettempdir(), "sw2_audio_endpoints.csv")
    try:
        os.remove(tmp)
    except OSError:
        pass
    _run(svcl, ["/scomma", tmp, "/Columns",
                "Name,Type,Direction,Device Name,Device State,Command-Line Friendly ID"])
    rows = []
    try:
        with open(tmp, "r", encoding="utf-8", errors="replace", newline="") as f:
            rows = list(csv.DictReader(f))
    except Exception as e:
        logger.debug("could not read endpoint CSV: %s", e)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    return rows


def _find_all_render(rows, name):
    """Return all render *device* endpoint rows matching `name`.

    Identify a physical render endpoint by "\\Device\\...\\Render" in its
    Command-Line Friendly ID (this excludes the per-application \\Application\\
    sessions and capture endpoints) rather than the "Type" column, which can be
    blank in the CSV.  Match `name` against Name / Device Name / friendly ID.
    """
    name_l = name.lower()
    matches = []
    for r in rows:
        fid = r.get("Command-Line Friendly ID", "")
        fid_l = fid.lower()
        if (r.get("Direction", "").strip().lower() == "render"
                and "\\device\\" in fid_l and fid_l.endswith("\\render")
                and (name_l in r.get("Name", "").lower()
                     or name_l in r.get("Device Name", "").lower()
                     or name_l in fid_l)):
            matches.append(r)
    return matches


def _apply(action):
    """action is 'Disable' or 'Enable'.  Verified by re-reading the device state."""
    import tempfile, os
    if sys.platform != "win32":
        return False
    if not _feature_enabled():
        logger.info("DualSense audio-endpoint auto-%s disabled by config; skipping.",
                    action.lower())
        return False
        
    svcl = _svcl_path()
    if not svcl:
        global _warned_missing
        if not _warned_missing:
            _warned_missing = True
            logger.warning(
                "svcl.exe / SoundVolumeView.exe not found (expected in drivers/tools/); "
                "cannot auto-%s the DualSense audio endpoint.  PS5 audio-haptics need the "
                "'%s' playback device %sd manually in mmsys.cpl.",
                action.lower(), _target_name(), action.lower())
        return False

    name = _target_name()
    want = "disabled" if action == "Disable" else "active"

    # Get current state via /scomma
    rows = _dump(svcl)
    targets = _find_all_render(rows, name)
    
    if not targets:
        logger.warning("Audio endpoint %r (render) not found — is the virtual DualSense "
                       "connected yet?", name)
        return False

    targets_to_change = []
    for target in targets:
        state = target.get("Device State", "").strip().lower()
        if want not in state:
            targets_to_change.append(target)

    if not targets_to_change:
        logger.info("All matching audio endpoints %r (%d) already %s.", name, len(targets), want)
        return True

    def _verify_all(expected_state):
        time.sleep(0.8)
        current_targets = _find_all_render(_dump(svcl), name)
        for ct in current_targets:
            if expected_state not in ct.get("Device State", "").strip().lower():
                return False
        return True

    # 1) Already elevated (incl. the schtasks-launched copy): run svcl directly, no UAC.
    if _is_admin():
        for t in targets_to_change:
            selector = t.get("Command-Line Friendly ID", "").strip() or name
            _run(svcl, [f"/{action}", selector])
        
        if _verify_all(want):
            logger.info("svcl /%s succeeded (elevated) for all targets.", action)
            return True
        logger.warning("svcl /%s did not change all targets.", action)
        return False

    # 2) Non-elevated DISABLE: drive it through the Scheduled Task so elevation is
    #    SILENT.  UAC appears only once — when the task is first registered.  We do
    #    NOT fall back to a per-call runas here (that would re-prompt every connect,
    #    exactly what the task is meant to avoid).
    if action == "Disable":
        global _task_reg_attempted
        if not _task_exists():
            if _task_reg_attempted:
                return False  # registration already declined/failed this session
            _task_reg_attempted = True
            if not _register_task():   # one-time UAC
                logger.warning(
                    "Audio-endpoint auto-disable needs a one-time admin approval to "
                    "register a Scheduled Task.  Accept the UAC prompt next time, or "
                    "disable the '%s' playback device manually in mmsys.cpl.", name)
                return False
        if _run_task():
            time.sleep(1.5)            # task re-discovers + disables with admin
            if _verify_all(want):
                logger.info("svcl /Disable succeeded via scheduled task for all targets.")
                return True
        logger.warning("Scheduled-task disable did not change all targets.")
        return False

    # 3) ENABLE from a non-elevated session (manual/cleanup, rare): one runas prompt.
    # Note: runas prompts UAC for each launch. To avoid multiple UACs, we just pass the wildcard or the first one, but wildcard is better if supported.
    # However, svcl /Enable "Wireless Controller Audio" usually works for all if wildcard is not used?
    # We will just iterate. If there are multiple, there might be multiple UAC prompts unfortunately.
    for t in targets_to_change:
        selector = t.get("Command-Line Friendly ID", "").strip() or name
        _run(svcl, [f"/{action}", selector], elevated=True)
        
    if _verify_all(want):
        logger.info("svcl /%s succeeded (runas) for all targets.", action)
        return True
    logger.warning("svcl /%s did not change all targets.", action)
    return False


def list_audio_endpoints():
    """Dump the current audio endpoints to the log (diagnostic helper)."""
    svcl = _svcl_path()
    if not svcl:
        logger.warning("svcl/SoundVolumeView not found; cannot list audio endpoints.")
        return
    rows = _dump(svcl)
    lines = ["%-30s | %-8s | %-9s | %-20s | %s" % (
        r.get("Name", ""), r.get("Direction", ""), r.get("Device State", ""),
        r.get("Device Name", ""), r.get("Command-Line Friendly ID", "")) for r in rows]
    logger.info("Audio endpoints (%d):\n%s", len(rows), "\n".join(lines))


def disable_dualsense_audio_endpoint_async(delay=2.5, retries=5):
    """Disable the endpoint in the background.

    The endpoint only appears a moment after the USBIP DualSense attaches, so we
    retry a few times.  Non-blocking: returns immediately.
    """
    if sys.platform != "win32" or not _feature_enabled():
        return

    def _worker():
        time.sleep(delay)
        for _ in range(max(1, retries)):
            if _apply("Disable"):
                return
            time.sleep(2.0)
        logger.warning(
            "Could not auto-disable the DualSense audio endpoint after %d attempts. "
            "Disable the '%s' playback device manually (mmsys.cpl) if PS5 audio-haptics "
            "are missing.", retries, _target_name())

    threading.Thread(target=_worker, daemon=True).start()


def enable_dualsense_audio_endpoint():
    """Re-enable the endpoint (restore Windows default).  Manual/cleanup use."""
    return _apply("Enable")


if __name__ == "__main__":
    # Manual diagnostics:  python src/dualsense_audio_endpoint.py [list|disable|enable]
    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(message)s")
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    if cmd == "disable":
        print("disable ->", _apply("Disable"))
    elif cmd == "enable":
        print("enable ->", _apply("Enable"))
    else:
        list_audio_endpoints()
