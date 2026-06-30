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
endpoint by name, issue the disable, then re-dump and VERIFY by polling the real
Device State.  Note a DISABLED render endpoint vanishes from the enumeration
entirely, so for Disable "no matched render endpoint still enabled" IS the success
condition (see Lessons learned below).  Enable/disable normally needs no admin (it
goes through the audio policy service), so we try a direct call first and only
fall back to an elevated scheduled task if that verifiably fails.

Place the tool at drivers/tools/svcl.exe (or SoundVolumeView.exe).  If it is
missing this module logs a warning and becomes a no-op.

Lessons learned (2026-07 — "Audio Device still not disabled" investigation)
---------------------------------------------------------------------------
Symptom: "Could not auto-disable ... after 5 attempts", and the playback device
stayed ENABLED in Sound Settings.  Live diagnostics on the actual virtual
DualSense ("10- Wireless Controller Audio", render jack named "耳機") exposed
THREE separate bugs, all now fixed in _apply():

  1. The non-elevated path never even TRIED a direct disable.  On this hardware a
     plain `SoundVolumeView /Disable <id>` succeeds with NO admin (verified:
     rc=0, IsUserAnAdmin()=False, device went away).  The old code skipped that
     and forced everything through the scheduled-task/UAC path, which was the one
     thing failing.  FIX: attempt a direct (non-elevated) /Disable FIRST; only
     fall back to the elevated scheduled task if that genuinely doesn't take.

  2. Verification could never report success.  A render endpoint that is
     successfully DISABLED DISAPPEARS from the /scomma enumeration entirely (only
     its "...\\Application\\..." sessions linger) — it does NOT remain as a row
     with Device State "Disabled".  The old _verify_all waited for the endpoint to
     still be present AND read "disabled", which is impossible, so every attempt
     was scored as a failure even though the disable had worked.  FIX: for Disable,
     success == no matched *render* endpoint is still enabled (gone OR "Disabled").

  3. "Already disabled" was misread as "not found -> failure".  When the endpoint
     is absent (because it is already disabled), _find_all_render returns nothing;
     the old code logged "endpoint not found" and returned False, burning all 5
     retries and emitting the warning.  FIX: an absent render endpoint on Disable
     is the desired end state -> return True immediately (idempotent, silent).

Non-issue ruled out during the hunt: the CJK jack name showing as mojibake
("耳機" -> "?v?") in piped console output is purely an OUTPUT-encoding artifact.
The /scomma CSV is valid UTF-8 (BOM + correct bytes) and CreateProcessW passes the
Unicode selector to SoundVolumeView intact, so matching/selecting was always fine.
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

    def _verify(timeout=3.0, poll=0.4):
        """Poll the real device state until `action` is satisfied (or timeout).

        Device State is the single source of truth no matter which process applied
        the change (direct call, runas, or the elevated scheduled task — the latter
        boots a fresh interpreter / frozen exe and can take several seconds).

        IMPORTANT: a successfully DISABLED render endpoint normally DISAPPEARS from
        the endpoint enumeration entirely (or reports state 'Disabled'); it does not
        linger as an 'Active' row.  So for Disable, success = no matched render
        endpoint is still enabled.  For Enable, success = the endpoint is present
        and active.
        """
        deadline = time.monotonic() + max(0.0, timeout)
        first = True
        while True:
            time.sleep(0.6 if first else poll)
            first = False
            cur = _find_all_render(_dump(svcl), name)
            if action == "Disable":
                still_on = [r for r in cur
                            if "disabled" not in r.get("Device State", "").strip().lower()]
                if not still_on:
                    return True
            else:  # Enable
                if cur and all("active" in r.get("Device State", "").strip().lower()
                               for r in cur):
                    return True
            if time.monotonic() >= deadline:
                return False

    # Snapshot current state.
    rows = _dump(svcl)
    targets = _find_all_render(rows, name)

    # An ABSENT physical render endpoint means it is not a usable playback device.
    # For Disable that is already the desired end state (the endpoint vanishes when
    # disabled, and only reappears — enabled — on the next USBIP re-attach), so do
    # NOT treat it as a failure and retry/warn.
    if not targets:
        if action == "Disable":
            logger.info("No active %r render endpoint present; already disabled/absent.", name)
            return True
        logger.warning("Audio endpoint %r (render) not found — is the virtual DualSense "
                       "connected yet?", name)
        return False

    want = "disabled" if action == "Disable" else "active"
    targets_to_change = [t for t in targets
                         if want not in t.get("Device State", "").strip().lower()]
    if not targets_to_change:
        logger.info("All matching audio endpoints %r (%d) already %s.", name, len(targets), want)
        return True

    def _direct_apply():
        for t in targets_to_change:
            selector = t.get("Command-Line Friendly ID", "").strip() or name
            _run(svcl, [f"/{action}", selector])

    # 1) Direct (non-elevated) attempt FIRST.  On many systems enable/disable goes
    #    through the audio policy service and needs no admin at all, so this is the
    #    fast path and skips the whole UAC / scheduled-task machinery.  (If we are
    #    already elevated this is also the right call — svcl just runs directly.)
    _direct_apply()
    if _verify():
        logger.info("svcl /%s succeeded (direct) for all targets.", action)
        return True

    # Already elevated and the direct call still didn't take: nothing more to try.
    if _is_admin():
        logger.warning("svcl /%s did not change all targets (elevated).", action)
        return False

    # 2) Non-elevated DISABLE that needs admin on this system: drive it through the
    #    Scheduled Task so elevation is SILENT.  UAC appears only once — when the
    #    task is first registered.  We do NOT fall back to a per-call runas here
    #    (that would re-prompt every connect, exactly what the task avoids).
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
        # Trigger the elevated task.  schtasks /run returns non-zero if a previous
        # instance is still running — that is NOT a failure here, the running task
        # is still doing the work, so we poll the device state either way.  The
        # elevated process (esp. a frozen exe) can take several seconds to boot, so
        # give verification a generous window.
        _run_task()
        if _verify(timeout=8.0):
            logger.info("svcl /Disable succeeded via scheduled task for all targets.")
            return True
        logger.warning("Scheduled-task disable did not change all targets yet.")
        return False

    # 3) ENABLE from a non-elevated session (manual/cleanup, rare): one runas prompt.
    for t in targets_to_change:
        selector = t.get("Command-Line Friendly ID", "").strip() or name
        _run(svcl, [f"/{action}", selector], elevated=True)

    if _verify():
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
