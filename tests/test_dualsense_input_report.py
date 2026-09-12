import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from dualsense_structs import DualSenseInputReport01


class DualSenseInputReportTests(unittest.TestCase):
    def test_wired_report_advertises_no_headset_on_the_wire(self):
        report = DualSenseInputReport01.wired_without_headset()
        packet = bytes(report)
        self.assertEqual(len(packet), 64)
        self.assertEqual(packet[0], 0x01)
        # USB report byte 54: HP detect bit 0, mic detect bit 1, USB data bit 3.
        self.assertEqual(packet[54] & 0x03, 0)
        self.assertEqual(packet[54] & 0x08, 0x08)
        self.assertEqual(packet[55] & 0x01, 0)  # No external microphone.

    def test_input_updates_preserve_usb_without_headset(self):
        report = DualSenseInputReport01.wired_without_headset()
        report.LeftStickX = 42
        report.ButtonCross = 1
        report.LeftTrigger = 255
        report.SensorTimestamp = 123456
        received = DualSenseInputReport01.from_buffer_copy(bytes(report))
        self.assertEqual(received.PluggedHeadphones, 0)
        self.assertEqual(received.PluggedMic, 0)
        self.assertEqual(received.PluggedUsbData, 1)
        self.assertEqual(received.LeftStickX, 42)
        self.assertEqual(received.ButtonCross, 1)


if __name__ == "__main__":
    unittest.main()
