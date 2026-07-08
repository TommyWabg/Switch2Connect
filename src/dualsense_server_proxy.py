# Switch2Connect - A Python and ESP32-S3 bridge utility for Switch 2 controller inputs.
# Copyright (C) 2026 TommyWabg
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# Contact Information:
# Electronic Mail: tommyw9318@gmail.com

import logging
import os
import socket
import struct
import subprocess
import sys
import threading
import time

logger = logging.getLogger(__name__)

MSG_INPUT = 0x01
MSG_STOP = 0x02
MSG_HAPTIC_FRAME = 0x82
MSG_OUTPUT_REPORT = 0x83
MSG_STATUS = 0x84


class DualSenseServerProxy:
    def __init__(
        self,
        host="127.0.0.1",
        port=3240,
        on_rumble_callback=None,
        bus_id="1-1",
        mac_address=None,
        on_audio_data_callback=None,
        on_disconnect_callback=None,
        on_haptic_callback=None,
        enable_audio=True,
    ):
        self.host = host
        self.port = int(port)
        self.enable_audio = enable_audio
        self.bus_id = bus_id
        self.mac_address = mac_address or ""
        self.on_rumble_callback = on_rumble_callback
        self.on_audio_data_callback = on_audio_data_callback
        self.on_disconnect_callback = on_disconnect_callback
        self.on_haptic_callback = on_haptic_callback
        self._proc = None
        self._running = False
        self._rx_thread = None
        self._watch_thread = None
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.settimeout(0.1)
        self._parent_port = self._sock.getsockname()[1]
        self._child_port = self._allocate_udp_port()
        self._child_addr = ("127.0.0.1", self._child_port)

    def _allocate_udp_port(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]
        finally:
            s.close()

    def _child_command(self):
        args = [
            "--host", self.host,
            "--port", str(self.port),
            "--bus-id", self.bus_id,
            "--mac-address", self.mac_address,
            "--ctrl-port", str(self._child_port),
            "--parent-port", str(self._parent_port),
        ]
        if not getattr(self, "enable_audio", True):
            args.append("--disable-audio")
        if getattr(sys, "frozen", False):
            return [sys.executable, "--dualsense-server"] + args
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dualsense_server_process.py")
        return [sys.executable, script] + args

    def start(self):
        if self._running:
            return
        self._running = True
        creationflags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
        self._proc = subprocess.Popen(
            self._child_command(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        self._rx_thread = threading.Thread(target=self._rx_loop, name="DualSenseProxyRx", daemon=True)
        self._watch_thread = threading.Thread(target=self._watch_loop, name="DualSenseProxyWatch", daemon=True)
        self._rx_thread.start()
        self._watch_thread.start()
        time.sleep(0.15)
        if self._proc.poll() is not None:
            rc = self._proc.returncode
            self._running = False
            raise RuntimeError(f"DualSense USBIP child exited during startup rc={rc}")
        logger.info(
            "DualSense USBIP server running out-of-process pid=%s ctrl=%d port=%d bus=%s",
            getattr(self._proc, "pid", None),
            self._child_port,
            self.port,
            self.bus_id,
        )

    def stop(self):
        self._running = False
        try:
            self._sock.sendto(bytes([MSG_STOP]), self._child_addr)
        except Exception:
            pass
        proc = self._proc
        if proc is not None:
            try:
                proc.wait(timeout=1.0)
            except Exception:
                try:
                    proc.terminate()
                except Exception:
                    pass
        try:
            self._sock.close()
        except Exception:
            pass

    def update_input(self, report):
        data = bytes(report)
        if len(data) < 64:
            return
        try:
            self._sock.sendto(bytes([MSG_INPUT]) + data[:64], self._child_addr)
        except Exception:
            logger.debug("DualSense proxy input send failed", exc_info=True)

    def update_state(self, report):
        self.update_input(report)

    def _rx_loop(self):
        while self._running:
            try:
                packet, _addr = self._sock.recvfrom(8192)
            except socket.timeout:
                continue
            except OSError:
                break
            if not packet:
                continue
            msg_type = packet[0]
            payload = packet[1:]
            try:
                if msg_type == MSG_OUTPUT_REPORT:
                    if self.on_rumble_callback:
                        self.on_rumble_callback(payload)
                elif msg_type == MSG_HAPTIC_FRAME:
                    if self.on_haptic_callback and len(payload) >= struct.calcsize("!BhhHHHHHHHH"):
                        (
                            mode_code,
                            left_intensity,
                            right_intensity,
                            left_lf_freq,
                            left_lf_amp,
                            left_hf_freq,
                            left_hf_amp,
                            right_lf_freq,
                            right_lf_amp,
                            right_hf_freq,
                            right_hf_amp,
                        ) = struct.unpack("!BhhHHHHHHHH", payload[:struct.calcsize("!BhhHHHHHHHH")])
                        mode = "SPECTRAL" if mode_code == 1 else ("SILENCE" if mode_code == 2 else "CONTINUOUS")
                        self.on_haptic_callback(
                            left_intensity,
                            right_intensity,
                            mode,
                            spectral={
                                "left_lf_freq": left_lf_freq,
                                "left_lf_amp": left_lf_amp,
                                "left_hf_freq": left_hf_freq,
                                "left_hf_amp": left_hf_amp,
                                "right_lf_freq": right_lf_freq,
                                "right_lf_amp": right_lf_amp,
                                "right_hf_freq": right_hf_freq,
                                "right_hf_amp": right_hf_amp,
                            },
                        )
                elif msg_type == MSG_STATUS and payload == b"disconnect":
                    if self.on_disconnect_callback:
                        self.on_disconnect_callback()
            except Exception:
                logger.debug("DualSense proxy callback failed", exc_info=True)

    def _watch_loop(self):
        while self._running:
            proc = self._proc
            if proc is not None and proc.poll() is not None:
                logger.warning("DualSense USBIP child exited rc=%s", proc.returncode)
                if self.on_disconnect_callback:
                    try:
                        self.on_disconnect_callback()
                    except Exception:
                        pass
                return
            time.sleep(0.5)
