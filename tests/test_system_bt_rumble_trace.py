import importlib
import importlib.util
import pathlib
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
import json


ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC = str(ROOT / "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)


class _BleClient:
    def write_gatt_char(self, *args, **kwargs):
        pass


class SystemBTRumbleTraceTests(unittest.TestCase):
    def setUp(self):
        self.old_config = sys.modules.get("config")
        self.cfg = SimpleNamespace(
            system_bt_rumble_trace_enabled=False,
            system_bt_rumble_trace_path="",
            system_bt_rumble_trace_dry_run=False,
            system_bt_rumble_interval_ms=None,
        )
        sys.modules["config"] = SimpleNamespace(CONFIG=self.cfg)
        self.trace = importlib.import_module("system_bt_rumble_trace")
        self.trace._close_writer()
        self.trace._writer_key = None

    def tearDown(self):
        self.trace._close_writer()
        if self.old_config is None:
            sys.modules.pop("config", None)
        else:
            sys.modules["config"] = self.old_config

    @staticmethod
    def _controller(vc, bridge=False):
        return SimpleNamespace(
            virtual_controller=vc,
            is_esp32s3_bridge=bridge,
            client=_BleClient(),
        )

    def test_scope_requires_system_bluetooth_but_accepts_all_virtual_modes(self):
        vc = SimpleNamespace(driver_type="WinUHid", mode="PS5", controllers=[])
        controller = self._controller(vc)
        self.assertFalse(self.trace._eligible(controller, vc))

        self.cfg.system_bt_rumble_trace_enabled = True
        self.assertTrue(self.trace._eligible(controller, vc))

        vc.driver_type = "USBIP"
        vc.mode = "Switch2"
        self.assertTrue(self.trace._eligible(controller, vc))
        context = self.trace._test_context(controller, vc)
        self.assertEqual("System Bluetooth", context["connection_mode"])
        self.assertEqual("USBIP", context["driver_type"])
        self.assertEqual("Switch2", context["emulation_mode"])
        vc.controllers = [controller]
        self.assertTrue(self.trace._eligible(virtual_controller=vc))

        controller.is_esp32s3_bridge = True
        self.assertFalse(self.trace._eligible(controller, vc))
        self.assertFalse(self.trace._eligible(virtual_controller=vc))

        controller.is_esp32s3_bridge = False
        controller.is_wired_usb = True
        self.assertFalse(self.trace._eligible(controller, vc))

        controller.is_wired_usb = False
        vc.driver_type = "ViGEmBus"
        vc.mode = "Xbox360"
        self.assertTrue(self.trace._eligible(controller, vc))

    def test_dry_run_and_interval_are_opt_in(self):
        vc = SimpleNamespace(driver_type="WinUHid", mode="PS5", controllers=[])
        controller = self._controller(vc)
        self.cfg.system_bt_rumble_trace_enabled = True
        self.assertFalse(self.trace.dry_run_enabled(controller))
        self.assertIsNone(self.trace.interval_override_ms(controller))

        self.cfg.system_bt_rumble_trace_dry_run = True
        self.cfg.system_bt_rumble_interval_ms = 16.6
        self.assertTrue(self.trace.dry_run_enabled(controller))
        self.assertAlmostEqual(16.6, self.trace.interval_override_ms(controller))

    def test_events_are_written_without_blocking_producer(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "trace.txt"
            self.cfg.system_bt_rumble_trace_enabled = True
            self.cfg.system_bt_rumble_trace_path = str(path)
            vc = SimpleNamespace(driver_type="WinUHid", mode="PS5", controllers=[])
            controller = self._controller(vc)
            self.assertTrue(self.trace.trace_event(
                "TEST_EVENT", controller=controller, value=7))
            deadline = time.time() + 2.0
            while time.time() < deadline and not path.exists():
                time.sleep(0.01)
            self.assertTrue(path.exists())
            text = path.read_text(encoding="utf-8")
            self.assertIn('"event":"TEST_EVENT"', text)
            self.assertIn('"value":7', text)
            self.assertIn('"connection_mode":"System Bluetooth"', text)
            self.assertIn('"driver_type":"WinUHid"', text)
            self.assertIn('"emulation_mode":"PS5"', text)
            self.assertIn('"rumble_mode":"On"', text)
            self.trace._close_writer()

    def test_relative_trace_path_is_resolved_beside_active_config(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = pathlib.Path(directory) / "config.yaml"
            self.cfg.config_file_path = str(config_path)
            self.cfg.system_bt_rumble_trace_path = self.trace.DEFAULT_TRACE_PATH

            resolved = self.trace.resolve_trace_path(cfg=self.cfg)
            self.assertEqual(
                pathlib.Path(directory) / "logs" / "system_bt_rumble_trace.txt",
                resolved,
            )
            self.cfg.system_bt_rumble_trace_enabled = True
            vc = SimpleNamespace(driver_type="USBIP", mode="Switch2", controllers=[])
            controller = self._controller(vc)
            self.assertTrue(self.trace.trace_event(
                "PACKAGED_PATH_TEST", controller=controller))
            deadline = time.time() + 2.0
            while time.time() < deadline and not resolved.exists():
                time.sleep(0.01)
            self.assertTrue(resolved.exists())
            self.trace._close_writer()
            self.trace._writer_key = None

            self.cfg.system_bt_rumble_trace_dry_run = True
            resolved = self.trace.resolve_trace_path(cfg=self.cfg)
            self.assertEqual(
                pathlib.Path(directory) / "logs" / "system_bt_rumble_trace_no_rumble.txt",
                resolved,
            )

    def test_analyzer_keeps_appended_runs_separate_and_reports_state_age(self):
        analyzer_path = ROOT / "tools" / "analyze_system_bt_rumble_trace.py"
        spec = importlib.util.spec_from_file_location("system_bt_trace_analyzer_test", analyzer_path)
        analyzer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(analyzer)
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "trace.txt"
            events = [
                {"run_id": "a", "ts_ns": 10, "event": "BT_INPUT_NOTIFY_ENTER", "subject": "s"},
                {"run_id": "a", "ts_ns": 20, "event": "BT_INPUT_STATE", "subject": "s",
                 "connection_mode": "System Bluetooth", "driver_type": "USBIP",
                 "emulation_mode": "Switch2", "rumble_mode": "On",
                 "raw_crc32": "aa", "state_changed": True, "state_age_ms": 0.0},
                {"run_id": "a", "ts_ns": 25, "event": "VIRTUAL_INPUT_SUBMIT_START",
                 "physical_state_age_ms": 5.0},
                {"run_id": "a", "ts_ns": 26, "event": "WINUHID_INPUT_SUBMIT_START",
                 "physical_state_age_ms": 5.0},
                {"run_id": "a", "ts_ns": 30, "event": "BT_INPUT_NOTIFY_ENTER", "subject": "s"},
                {"run_id": "a", "ts_ns": 40, "event": "BT_INPUT_STATE", "subject": "s",
                 "raw_crc32": "aa", "state_changed": False, "state_age_ms": 20.0},
                {"run_id": "b", "ts_ns": 100, "event": "BT_INPUT_NOTIFY_ENTER", "subject": "s"},
                {"run_id": "b", "ts_ns": 200, "event": "BT_INPUT_NOTIFY_ENTER", "subject": "s"},
            ]
            path.write_text("\n".join(json.dumps(item) for item in events), encoding="utf-8")
            summary = analyzer.analyse(path)
            self.assertEqual(2, summary["run_count"])
            self.assertEqual(1, summary["runs"]["a"]["input"]["s"]["state_changes"])
            self.assertEqual(1, summary["runs"]["a"]["input"]["s"]["consecutive_raw_crc_repeats"])
            self.assertEqual(1, summary["runs"]["a"]["virtual_input_state_age_ms"]["n"])
            self.assertEqual(0.0001, summary["runs"]["b"]["input"]["s"]["interval_ms"]["max"])
            self.assertIn({
                "connection_mode": "System Bluetooth",
                "driver_type": "USBIP",
                "emulation_mode": "Switch2",
                "rumble_mode": "On",
            }, summary["runs"]["a"]["test_contexts"])


if __name__ == "__main__":
    unittest.main()
