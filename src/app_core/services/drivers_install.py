"""Worker-backed elevation/download/install flows.

Imported only by DriverService; no UI or hardware imports here.
"""
from .. import driver_actions
from .. import driver_logs
import ctypes
import logging
import os

from ..executor import on_core
from .. import events

logger = logging.getLogger(__name__)


class DriverInstallMixin:
    def _launch_elevated(self, lp_file, lp_params, progress_win=None, lp_dir=None, hide=True):
        if self._launch_override is not None:
            return self._launch_override(lp_file, lp_params, lp_dir=lp_dir, hide=hide)
        return driver_logs.launch_elevated(
            lp_file, lp_params, self._ui.native_window_handle() or 0, lambda: None, lp_dir, hide)
    def _wait_process(self, handle):
        if self._wait_override is not None:
            return self._wait_override(handle)
        return driver_actions.wait_process(handle, ctypes.windll.kernel32, self._sleep)


    def _process_job(self, file, params, verify=None, directory=None):
        handle = self._launch_elevated(file, params, lp_dir=directory)
        if not handle:
            return None, None
        code = self._wait_process(handle)
        return code, verify(code) if verify else None

    def _operation(self, title, message, job, finish):
        if self._busy:
            return None
        return self._flow(self._operation_steps(title, message, job, finish))

    def _operation_steps(self, title, message, job, finish):
        self._busy = True
        self._publish()
        progress = None
        try:
            progress = self._ui.progress(title, message)
            self._bus.publish(events.TASK_STATE, events.TaskState("drivers", title, True, message))
            try:
                result = yield self._worker(lambda: job(progress))
            except Exception as exc:
                self._ui.show_message("Error", f"Failed to start the installer: {exc}")
                return False
            self._last_driver_action_launched = isinstance(result, tuple) and result[0] not in (None, "download_error", "missing")
            progress.close()
            progress = None
            result = yield finish(result)
            return result
        finally:
            if progress is not None:
                progress.close()
            self._busy = False
            self._bus.publish(events.TASK_STATE, events.TaskState("drivers", title, False))
            self._publish()


    @on_core
    def install_vigembus_driver(self, show_success_msg=True):
        if self._busy:
            return None
        if self._packaged():
            return self.run_vigembus_install(show_success_msg)
        return self.download_and_install_driver("ViGEmBus", self._module("app_core.helpers").verify_vigembus_ready, show_success_msg)

    @on_core
    def run_vigembus_install(self, show_success_msg=True):
        if self._busy:
            return None
        return self._flow(driver_actions.run_vigembus_install(
            self._driver_context(), show_success_msg), bool)

    @on_core
    def download_and_install_driver(self, driver_key, verify_fn, show_success_msg=True):
        if self._busy:
            return None
        return self._download_and_run_driver_action(driver_key, verify_fn, "install", show_success_msg)

    def _download_and_run_driver_action(self, driver_key, verify_fn, action, show_success_msg=True):
        return driver_actions.download_driver(
            self._driver_context(), driver_key, verify_fn, action, show_success_msg,
            self._operation, self._process_job, self._sleep)

    @on_core
    def run_driver_install(self, show_success_msg=True):
        if self._busy:
            return None
        return self._flow(driver_actions.run_driver_install(
            self._driver_context(), show_success_msg), bool)

    @on_core
    def run_driver_uninstall(self):
        if self._busy:
            return None
        return self._flow(driver_actions.run_driver_uninstall(
            self._driver_context()), bool)

    @on_core
    def run_vigembus_uninstall(self):
        if self._busy:
            return None
        return self._flow(driver_actions.run_vigembus_uninstall(
            self._driver_context()), bool)


    @on_core
    def run_usbip_install(self, show_success_msg=True):
        if self._busy:
            return None
        return self._flow(driver_actions.run_usbip_install(
            self._driver_context(), show_success_msg))

    @on_core
    def run_usbip_uninstall(self):
        if self._busy:
            return None
        return self._flow(driver_actions.run_usbip_uninstall(
            self._driver_context()))


    def _prompt_usbip_restart_required(self):
        driver_actions._prompt_usbip_restart_required(self._ui, self._module)

    def _run_hidhide_script(self, script_name, wait_text):
        return self._flow(driver_actions._run_hidhide_script(
            self._driver_context(), script_name, wait_text))

    @on_core
    def run_hidhide_install(self, prompt_restart=True):
        if self._busy:
            return False
        return self._flow(driver_actions.hidhide_install(
            self._ui, self._module, self._run_hidhide_script,
            self._sync_hidhide_installed, self._refresh_hidhide_action, self._invalidate, self._status, prompt_restart))


    @on_core
    def run_hidhide_uninstall(self):
        if self._busy:
            return False
        return self._flow(driver_actions.hidhide_uninstall(
            self._ui, self._module, self._run_hidhide_script,
            self._sync_hidhide_installed, self._refresh_hidhide_action, self._invalidate, self._status), bool)


    def _refresh_hidhide_action(self):
        self._invalidate("hidhide")
        self._status("HidHide")
        self._publish()

    def _driver_context(self):
        from types import SimpleNamespace
        def switch(value):
            self._busy = False
            return self.switch_driver(value)
        return SimpleNamespace(
            owner=self, config=self._config, ui=self._ui, module=self._module, process=self._driver_process,
            source_directory=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            running=self._runtime.is_discoverer_running, stop=self._runtime.stop_discoverer,
            start=self._runtime.start_discoverer, invalidate=self._invalidate,
            wake=self.wake_controller_mouse_output_reconcile, output=self._refresh_output,
            refresh=self.refresh_state, switch=switch, pending=self._set_usbip_pending,
            refresh_usbip=self.refresh_state, restart_prompt=self._prompt_usbip_restart_required)

    def _driver_process(self, title, message, file, params, directory=None, collect_exit=True, verify=None):
        def steps():
            self._busy = True
            progress = self._ui.progress(title, message)
            self._bus.publish(events.TASK_STATE, events.TaskState("drivers", title, True, message))
            self._publish()
            try:
                def verification(code):
                    progress.update("Finalizing USBIP-win2 Driver installation...")
                    steps = verify()
                    try:
                        while True:
                            self._sleep(next(steps) / 1000.)
                    except StopIteration as done:
                        return done.value
                result = yield self._worker(lambda: self._process_job(
                    file, params, verification if verify is not None else None, directory))
                self._last_driver_action_launched = result[0] is not None
                return result if verify is not None else result[0]
            finally:
                progress.close()
                self._busy = False
                self._bus.publish(events.TASK_STATE, events.TaskState("drivers", title, False))
                self._publish()
        return self._flow(steps())

    def _set_usbip_pending(self, value):
        self._usbip_restart_pending = value
        self._publish()
