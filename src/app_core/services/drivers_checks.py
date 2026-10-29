"""Executor adapters for the shared classic readiness checks."""
from .. import driver_readiness
from ..executor import on_core


class DriverChecksMixin:
    @on_core
    def check_vigembus_installation(self, save=True):
        return self._readiness(driver_readiness.check_vigembus_installation, save)

    @on_core
    def check_driver_installation(self, save=True):
        return self._readiness(driver_readiness.check_driver_installation, save)

    def _readiness(self, check, save):
        def steps():
            try:
                return (yield from check(
                    self._config, self._ui, self._module,
                    lambda name, **kwargs: getattr(self, name)(**kwargs),
                    self._publish, self._set_packaged_available, lambda: None,
                    self._status, lambda driver: None,
                    self._usbip_restart_pending, save))
            finally:
                self._publish()
        return self._flow(steps())

    def _set_packaged_available(self, available):
        self._packaged_available = available
