svcl.exe — required for automatic DualSense audio-haptic endpoint control
=========================================================================

Place NirSoft's svcl.exe (command-line build) OR SoundVolumeView.exe (GUI build)
in THIS folder.  Either works — both accept the same /Disable //Enable options:

    drivers/tools/svcl.exe            (preferred, console build)
    drivers/tools/SoundVolumeView.exe (also accepted)

readme.txt is NirSoft's license/readme — keep it for redistribution compliance.

Download (free):
    https://www.nirsoft.net/utils/sound_volume_view.html
    -> "Download SoundVolumeView (command-line version) - svcl"
    Prefer the 32-bit (x86) build for the widest compatibility: a 32-bit exe runs
    on BOTH 32-bit and 64-bit Windows, while a 64-bit exe only runs on 64-bit
    Windows.  svcl is launched as a separate process (not loaded into the app), so
    its architecture does NOT need to match the app, and the 32-bit build controls
    audio endpoints correctly on 64-bit Windows (it uses COM, not raw registry).

What it is used for
-------------------
When a PS5/DualSense virtual controller (USBIP) connects, the app runs:

    svcl.exe /Disable "Wireless Controller Audio\Device\*\Render"

to DISABLE the virtual DualSense audio playback endpoint in Windows.  This frees
the USB Audio ISO-OUT endpoint so the game can deliver the 4-channel haptic
stream directly; while the endpoint is ENABLED, the Windows audio engine claims
and mixes it and the haptic channels arrive empty.

The virtual DualSense uses a fixed USB serial, so Windows remembers the disabled
state across reconnects — one disable is normally enough, but the app re-applies
it on every connect (idempotent) to be safe.

Notes
-----
- Enable/disable via svcl normally does NOT need administrator rights; the app
  retries elevated only if the normal call fails.
- If svcl.exe is missing, the app logs a warning and continues — haptics will
  then require disabling the "Wireless Controller Audio" playback device by hand
  in mmsys.cpl (Sound control panel -> Playback).
- To restore the device: svcl.exe /Enable "Wireless Controller Audio\Device\*\Render"
  (or re-enable it in mmsys.cpl).
- If auto-disable never matches, find the exact endpoint name by calling
  dualsense_audio_endpoint.list_audio_endpoints() (dumps the device list to the
  log) and set CONFIG.dualsense_audio_endpoint_pattern accordingly.
