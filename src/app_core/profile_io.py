"""Profile import/export shared by the classic and modern UIs.

Moved from gui.py (ControllerWindow.on_import_profiles / on_export_profiles); the
UIs keep only their dialogs and messages. ``config`` is the live Config object.
"""
import copy
import os

import yaml

from .helpers import (CONTROLLER_RELATED_CONFIG_KEYS, MACHINE_LOCAL_CONFIG_KEYS,
                      get_exe_display_name, has_controller_related_data, normalize_app_path,
                      strip_controller_related, strip_controller_related_from_profile)

_YamlLoader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
_YamlDumper = getattr(yaml, "CDumper", yaml.Dumper)


def unique_profile_name(existing, name):
    """Return ``name``, or ``name (2)`` / ``name (3)`` ... when it is taken."""
    if name not in existing:
        return name
    index = 2
    while f"{name} ({index})" in existing:
        index += 1
    return f"{name} ({index})"


def normalize_imported_profile(config, profile_data):
    """Fill an imported profile out to the canonical profile shape."""
    normalized = config.get_default_profile_dict()
    if isinstance(profile_data, dict):
        normalized.update(copy.deepcopy(profile_data))
        # Profile exports from older versions have only the per-category,
        # per-side IR Mouse flag.  Preserve an enabled flag when importing
        # them into the unified WinUHid Manager setting.
        if "mouse_output_mode" not in profile_data:
            legacy_raw = False
            for cat in ("xbox", "ps4", "ps5_winuhid", "ps5_usbip", "switch1", "switch2"):
                cat_data = profile_data.get(cat, {})
                sensor = cat_data.get("joycon_ir_sensor", {}) if isinstance(cat_data, dict) else {}
                for side in ("left", "right"):
                    side_data = sensor.get(side, {}) if isinstance(sensor, dict) else {}
                    mouse = side_data.get("ir_mouse", {}) if isinstance(side_data, dict) else {}
                    value = mouse.get("raw_input", False) if isinstance(mouse, dict) else False
                    if isinstance(value, str):
                        value = value.strip().lower() in ("true", "1", "yes", "on")
                    legacy_raw = legacy_raw or bool(value)
            normalized["mouse_output_mode"] = "Raw Input" if legacy_raw else None
    if not isinstance(normalized.get("assigned_apps"), list):
        normalized["assigned_apps"] = []
    normalized["change_profile_list"] = bool(normalized.get("change_profile_list", False))
    if not isinstance(normalized.get("profile_switching_combo"), str):
        normalized["profile_switching_combo"] = ""
    normalized.pop("assigned_app", None)
    return normalized


def dedupe_assigned_apps(profiles, priority_names):
    """An exe may only be assigned to one profile; ``priority_names`` win."""
    seen = set()
    ordered = list(priority_names) + [n for n in profiles if n not in priority_names]
    for profile_name in ordered:
        profile_data = profiles.get(profile_name)
        if not isinstance(profile_data, dict):
            continue
        kept = []
        for app in profile_data.get("assigned_apps") or []:
            app_path = app.get("path") if isinstance(app, dict) else str(app)
            normalized_path = normalize_app_path(app_path)
            if not normalized_path or normalized_path in seen:
                continue
            seen.add(normalized_path)
            kept.append({
                "path": os.path.normpath(app_path),
                "name": (app.get("name") if isinstance(app, dict) else None) or get_exe_display_name(app_path),
            })
        profile_data["assigned_apps"] = kept


def write_config_yaml(config, updates):
    """Merge ``updates`` into config.yaml on disk under the Config save lock."""
    with config._save_lock:
        data = {}
        if os.path.exists(config.config_file_path):
            try:
                with open(config.config_file_path, 'r', encoding='utf-8') as f:
                    data = yaml.load(f, Loader=_YamlLoader) or {}
            except Exception:
                data = {}
        data.update(updates)
        with open(config.config_file_path, 'w', encoding='utf-8') as f:
            yaml.dump(data, f, Dumper=_YamlDumper, default_flow_style=False)


def read_import(file_path):
    """Return ``(imported, None)`` or ``(None, error message)`` for the Import dialog."""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            imported = yaml.load(f, Loader=_YamlLoader)
    except Exception as e:
        return None, f"Failed to read the file:\n{e}"
    if not isinstance(imported, dict):
        return None, "This file is not a valid config file."
    imported_profiles = imported.get("profiles")
    if not isinstance(imported_profiles, dict):
        imported_profiles = {}
    if not imported_profiles and not has_controller_related_data(imported):
        return None, "This file contains nothing to import."
    return imported, None


def imported_profiles(imported):
    profiles = imported.get("profiles")
    return profiles if isinstance(profiles, dict) else {}


def import_updates(config, imported, selected, keep_current, include_calibration):
    """Return ``(config.yaml updates, imported profile names)`` for a confirmed import.

    The caller saves the active profile's driver/emu mode into ``config.profiles`` first."""
    if not selected:
        # Nothing to import into the profile store - never drop the local profiles.
        keep_current = True
    profiles = imported_profiles(imported)
    merged = copy.deepcopy(config.profiles) if keep_current else {}
    imported_names = []
    for name in selected:
        target_name = unique_profile_name(merged, name) if keep_current else name
        merged[target_name] = normalize_imported_profile(config, profiles.get(name))
        imported_names.append(target_name)

    dedupe_assigned_apps(merged, imported_names)

    # Keep using the current profile; only fall back when it no longer exists.
    active = config.active_profile if config.active_profile in merged else next(iter(merged))

    if selected:
        updates = {
            key: value for key, value in imported.items()
            if key not in ("profiles", "active_profile") and key not in MACHINE_LOCAL_CONFIG_KEYS
        }
        # The profile switching combo trigger only gets overwritten by a real
        # input: an unset value in the imported file must not clear a local one.
        if not str(updates.get("profile_switching_combo_trigger") or "").strip():
            updates.pop("profile_switching_combo_trigger", None)
        if not include_calibration:
            # Scrub only what is coming in: dropping these keys from ``updates``
            # leaves the local values in config.yaml untouched, and only the newly
            # imported profiles are cleaned so kept profiles keep their entries.
            strip_controller_related(updates)
            for name in imported_names:
                strip_controller_related_from_profile(merged[name])
        updates["profiles"] = merged
        updates["active_profile"] = active
    else:
        # Controller related data only - leave the profile store and every other
        # setting exactly as they are.
        updates = {
            key: imported[key] for key in CONTROLLER_RELATED_CONFIG_KEYS
            if key in imported and key not in MACHINE_LOCAL_CONFIG_KEYS
        }
    return updates, imported_names


def import_summary(imported_names, include_calibration):
    summary = f"Imported {len(imported_names)} profile(s)."
    if include_calibration:
        summary = (
            "Imported controller related data."
            if not imported_names else summary + "\nController related data imported."
        )
    return summary


def export_file_path(file_path):
    if not file_path.lower().endswith(".yaml"):
        file_path = os.path.splitext(file_path)[0] + ".yaml"
    return file_path


def export_profiles(config, file_path, selected, include_calibration):
    """Write the selected profiles (or only controller related data) to ``file_path``."""
    with config._save_lock:
        data = {}
        if os.path.exists(config.config_file_path):
            with open(config.config_file_path, 'r', encoding='utf-8') as f:
                data = yaml.load(f, Loader=_YamlLoader) or {}
        if selected:
            # The in-memory profile store is authoritative (saves are async).
            data["profiles"] = {
                name: copy.deepcopy(config.profiles[name])
                for name in selected if name in config.profiles
            }
            if not include_calibration:
                strip_controller_related(data)
            if config.active_profile in data["profiles"]:
                data["active_profile"] = config.active_profile
            else:
                data["active_profile"] = next(iter(data["profiles"]), config.active_profile)
            for key in MACHINE_LOCAL_CONFIG_KEYS:
                data.pop(key, None)
        else:
            # Controller related data only - no profiles, no other settings.
            data = {
                key: value for key, value in data.items()
                if key in CONTROLLER_RELATED_CONFIG_KEYS
            }
        with open(file_path, 'w', encoding='utf-8') as f:
            yaml.dump(data, f, Dumper=_YamlDumper, default_flow_style=False)


def export_summary(selected, include_calibration):
    summary = f"Exported {len(selected)} profile(s)." if selected else "Exported controller related data."
    if selected and include_calibration:
        summary += "\nController related data included."
    return summary
