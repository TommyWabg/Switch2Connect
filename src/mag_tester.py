"""Build-gated helpers for the diagnostic Mag Tester UI."""

from pathlib import Path
import shutil
import sys


MAG_TESTER_MARKER = "mag_tester_enabled.marker"


def mag_tester_build_enabled(bundle_root=None, argv=None) -> bool:
    """Enable a marked diagnostic build or an explicit source-only test run."""
    if bundle_root is None:
        if not getattr(sys, "frozen", False):
            arguments = sys.argv[1:] if argv is None else list(argv)
            return "--mag-tester" in arguments
        bundle_root = getattr(sys, "_MEIPASS", "")
    try:
        return (Path(bundle_root) / MAG_TESTER_MARKER).is_file()
    except (OSError, TypeError, ValueError):
        return False


def export_recorded_file(source, destination) -> Path:
    """Copy a completed recording, verify it, then remove its temporary file."""
    source_path = Path(source).resolve()
    destination_path = Path(destination).resolve()
    if source_path == destination_path:
        raise ValueError("Choose a location outside the temporary recording path.")
    source_size = source_path.stat().st_size
    shutil.copy2(source_path, destination_path)
    if (not destination_path.is_file()
            or destination_path.stat().st_size != source_size):
        raise OSError("The exported recording could not be verified.")
    source_path.unlink()
    return destination_path


__all__ = [
    "MAG_TESTER_MARKER", "export_recorded_file",
    "mag_tester_build_enabled",
]
