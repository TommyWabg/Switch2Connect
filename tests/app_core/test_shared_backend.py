from types import SimpleNamespace as NS

from app_core import calibration_data, driver_readiness, esp32_status, settings_writes
from app_core.ui_port import ScriptedUiPort


def test_partial_vigem_repair_waits_before_install_and_second_save():
    trace = []
    config = NS(vigembus_installed=True, save_config=lambda: trace.append(("save", config.vigembus_installed)))
    status = NS(unknown=False, installed=False, state="partial", describe=lambda: "broken nodes")
    ui = ScriptedUiPort([True])
    modules = {"utils": NS(is_packaged=lambda: False)}
    def action(name, **kwargs):
        trace.append((name, kwargs))
        return name
    steps = driver_readiness.check_vigembus_installation(
        config, ui, modules.__getitem__, action, lambda: None, lambda value: None,
        lambda: None, lambda driver: status, lambda driver: None)
    assert next(steps) == "run_vigembus_uninstall"
    assert trace == [("save", False), ("run_vigembus_uninstall", {})]
    assert steps.send(True) == "install_vigembus_driver"
    assert trace[-1] == ("install_vigembus_driver", {"show_success_msg": True})
    try:
        steps.send(True)
    except StopIteration as result:
        assert result.value is True
    assert trace[-1] == ("save", True)
    assert ui.calls[0].args[0] == "Repair ViGEmBus Driver"


def test_power_mode_priority_callback_precedes_rescan_and_controller_wakes():
    trace = []
    config = NS(save_config=lambda: trace.append(("save", config.power_saving_mode)))
    controller = NS(_poke_rumble_scheduler=lambda: trace.append("rumble"),
                    on_power_saving_mode_changed=lambda old, new: trace.append((old, new)))
    vc = NS(_update_wake=NS(set=lambda: trace.append("wake")), controllers=[controller])
    modules = {
        "power_saving": NS(notify_mode_changed=lambda: trace.append("notify")),
        "discoverer": NS(request_wired_rescan=lambda reason, **kwargs: trace.append((reason, kwargs))),
    }
    settings_writes.apply_power_saving_mode(
        config, "Off", "Full", [vc], modules.__getitem__, lambda mode: trace.append(("priority", mode)))
    assert trace == [("save", "Full"), "notify", ("priority", "Full"),
                     ("power_saving_mode_changed", {"manual": False}), "wake", "rumble", ("Off", "Full")]


def test_gyro_control_refresh_runs_before_sync_and_save():
    trace = []
    config = NS(gyro_mode="Yaw", sync_active_in_app_gyro_activation=lambda: trace.append("sync"),
                save_config=lambda: trace.append("save"))
    settings_writes.update_gyro_control_mode(
        config, "Steering", lambda: trace.append("sensitivity"), "Yaw",
        lambda action, value: trace.append((action, value, config.gyro_mode)))
    assert trace == ["sensitivity", ("control", "Steering", "Roll"), "sync", "save",
                     ("mapping", "Steering", "Roll"), ("ir", "Steering", "Roll")]


def test_release_calibration_read_seam_resets_stationary_interval_on_touch():
    item = dict(source={}, release_done=False, prev=(2048, 2048), anchor=(2048, 2048),
                still_since=0., release_elapsed=0., idle_samples=[])
    distance = lambda a, b: max(abs(a[0] - b[0]), abs(a[1] - b[1]))
    calibration_data.tick_release([item], .05, 2., lambda source: (2200, 2048), distance, 5, 40, 1., 3.)
    assert item["still_since"] is None
    assert item["release_elapsed"] == 0.
    assert item["anchor"] == (2200, 2048)
    calibration_data.tick_release([item], .05, 3., lambda source: (2200, 2048), distance, 5, 40, 1., 3.)
    assert item["still_since"] == 3.
    calibration_data.tick_release([item], .05, 4., lambda source: (2200, 2048), distance, 5, 40, 1., 3.)
    assert item["release_elapsed"] == .05
    assert item["idle_samples"] == [(2200, 2048)]


def test_esp32_probe_policy_distinguishes_cdc_result_otg_and_failed_probe():
    cdc = NS(board_present=True, firmware_installed=False, otg_only=False)
    otg = NS(board_present=True, firmware_installed=False, otg_only=True)
    assert esp32_status.keep_previous_probe(True, cdc)
    assert not esp32_status.keep_previous_probe(True, otg)
    assert not esp32_status.keep_previous_probe(True, None)
    assert not esp32_status.should_restart_discovery(True, False, True, False, True, [object()])
    assert esp32_status.should_restart_discovery(True, False, True, False, True, [None])


def test_profile_application_waits_for_driver_before_keyboard_and_refresh(monkeypatch):
    from app_core import profile_store
    trace = []
    monkeypatch.setattr(profile_store, "prepare_profile_switch",
                        lambda config: ("USBIP", "PS5", True, True, False))
    operation = object()
    def switch(driver, **kwargs):
        trace.append((driver, kwargs))
        return operation
    steps = profile_store.apply_profile_switch(
        NS(), lambda action, value=None: trace.append(action), switch,
        lambda mode: trace.append(mode), lambda: trace.append("keyboard"),
        lambda: trace.append("wake"), lambda: trace.append("cancel"))
    assert next(steps) is operation
    assert "keyboard" not in trace
    outcome = NS(ok=True)
    try:
        steps.send(outcome)
    except StopIteration as done:
        assert done.value is outcome
    assert trace[-6:] == ["keyboard", "wake", "refresh_profile_switching_combo_trigger_ui",
                          "refresh_driver_manager", "refresh_ui_for_profile", "cancel"]
    assert ("USBIP", {"persist_preference": False}) in trace


def test_shifted_joystick_mouse_callback_precedes_final_save():
    trace = []
    config = NS(set_joystick_setting_scoped=lambda *args: trace.append(args),
                save_config=lambda: trace.append("save"))
    settings_writes.set_joystick_setting(
        config, "l_joystick", "mouse_sensitivity", "4.2", "in_app_gyro_mode_mappings",
        lambda value: trace.append(("stick_scale", value)))
    assert trace == [("l_joystick", "mouse_sensitivity", 4.2, "in_app_gyro_mode_mappings"),
                     ("stick_scale", 4.2), "save"]


def test_installer_poll_seam_keeps_first_poll_and_retry_at_200_ms():
    from app_core import driver_actions
    trace, pending, results = [], [], []
    statuses = iter((258, 0))
    def wait(handle, timeout):
        trace.append(("wait", handle, timeout))
        return next(statuses)
    def exit_code(handle, pointer):
        pointer._obj.value = 3010
        trace.append(("exit", handle))
    kernel = NS(WaitForSingleObject=wait, GetExitCodeProcess=exit_code,
                CloseHandle=lambda handle: trace.append(("close", handle)))
    def schedule(ms, callback):
        trace.append(("after", ms))
        pending.append(callback)
    driver_actions.poll_process(7, kernel, schedule, results.append)
    assert trace == [("after", 200)]
    pending.pop(0)()
    assert trace[-1] == ("after", 200)
    pending.pop(0)()
    assert results == [3010]
    assert trace[-2:] == [("exit", 7), ("close", 7)]


def test_gyro_countdown_scheduler_keeps_classic_order():
    from app_core.player_actions import gyro_countdown
    trace, pending = [], []
    controller = NS(start_calibration=lambda: trace.append("start"))
    gyro_countdown(lambda: [None, controller], lambda ms, fn: pending.append((ms, fn)),
                   lambda text, phase: trace.append((text, phase)))
    assert [ms for ms, _ in pending] == [1000, 2000, 3000]
    assert trace == [("Starting in 3..", "starting")]
    pending.pop()[1]()
    assert trace[-2:] == ["start", ("Calibrating 5..", "starting")]
    assert [ms for ms, _ in pending] == [1000, 2000, 1000, 2000, 3000, 4000, 5000]
    pending[-1][1]()
    assert trace[-1] == ("Calibration Done", "done")


def test_backend_context_alias_and_mapping_write_seam():
    from app_core.lifecycle import BackendContext
    owner, writes = {"busy": False}, []
    context = BackendContext(owner, fields={"firmware_busy": "busy"},
                             on_write=lambda name, value: writes.append((name, value)))
    context.firmware_busy = True
    assert context.firmware_busy and owner == {"busy": True}
    assert writes == [("firmware_busy", True)]


def test_classic_ui_port_preserves_custom_dialog_buttons():
    import ast
    from pathlib import Path
    tree = ast.parse((Path(__file__).resolve().parents[2] / "src/gui.py").read_text(encoding="utf-8"))
    adapter = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "TkUiPort")
    namespace = {}
    exec(compile(ast.Module(body=[adapter], type_ignores=[]), "gui-adapter", "exec"), namespace)
    calls = []
    window = NS(custom_messagebox=lambda *args, **kwargs: calls.append((args, kwargs)) or True)
    port = namespace["TkUiPort"](window, custom=True)
    assert port.ask_yes_no("Reset Profile", "Reset?", yes="Yes", no="No")
    assert calls == [(("Reset Profile", "Reset?"), {"type": "yesno", "confirm_text": "Yes", "cancel_text": "No"})]
