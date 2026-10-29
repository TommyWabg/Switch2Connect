"""Resolve the same asset tree in source checkouts and PyInstaller bundles."""
from pathlib import Path
import sys


def resource_path(relative):
    root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[2]))
    return root / relative
