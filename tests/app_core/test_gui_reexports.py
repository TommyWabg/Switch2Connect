import ast
from pathlib import Path

import pytest

MOVED = {
    "app_core.helpers": """
        normalize_app_path get_exe_display_name check_driver_registry check_driver_pnputil
        is_driver_installed verify_winuhid_runtime hidhide_service_state removal_verified
        check_vigembus_registry check_vigembus_pnputil is_vigembus_installed
        verify_vigembus_runtime verify_vigembus_ready wired_controller_label
        MACHINE_LOCAL_CONFIG_KEYS CONTROLLER_RELATED_CONFIG_KEYS CONTROLLER_RELATED_PRESENCE_KEYS
        CONTROLLER_RELATED_NESTED_KEYS strip_controller_related_from_profile strip_controller_related
        _profile_has_controller_related_data has_controller_related_data format_input_display
        _INPUT_MODIFIER_TOKENS MOUSE_CLICK_CUSTOM_TOKENS parse_mouse_click_mapping
        _set_current_thread_priority _get_current_thread_priority SHELLEXECUTEINFOW WINDOWPLACEMENT
    """.split(),
    "app_core.win_listeners": """
        PowerListener WiredDeviceChangeListener GUID DEV_BROADCAST_HDR
        DEV_BROADCAST_DEVICEINTERFACE_W
    """.split(),
}


@pytest.mark.parametrize("module,name", [(m, n) for m, names in MOVED.items() for n in names])
def test_gui_reexports_under_same_name(module, name):
    src = Path(__file__).resolve().parents[2] / "src"
    tree = ast.parse((src / "gui.py").read_text(encoding="utf-8"))
    assert any(
        isinstance(node, ast.ImportFrom) and node.module == module
        and any(alias.name == name and alias.asname in (None, name) for alias in node.names)
        for node in tree.body
    )
    assert not any(isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name == name for node in tree.body)
    moved_tree = ast.parse((src / (module.replace(".", "/") + ".py")).read_text(encoding="utf-8"))
    assert any(
        getattr(node, "name", None) == name or
        isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets)
        for node in moved_tree.body
    )
