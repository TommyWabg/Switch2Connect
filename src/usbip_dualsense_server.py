import struct
import logging
import time
import queue
import ctypes
import os
import random

# We previously used a random serial to bust the Windows HID cache during development.
# Now that the descriptors are finalized, we use a FIXED serial number.
# This allows Windows to remember the user's Sound Control Panel settings
# (such as disabling the audio endpoint) across program restarts!
_SESSION_SERIAL = "DS5E0000"
logger_boot = logging.getLogger(__name__)
logger_boot.info(f"DualSense USBIP session serial: {_SESSION_SERIAL} (Fixed serial for audio state persistence)")

from usbip_server import USBIPServer, USBIP_VERSION, OP_REP_DEVLIST, OP_REP_IMPORT, USBIP_RET_SUBMIT
from dualsense_descriptors import (
    DUALSENSE_DEVICE_DESCRIPTOR, 
    DUALSENSE_CONFIGURATION_DESCRIPTOR, 
    DUALSENSE_HID_REPORT_DESCRIPTOR,
    DUALSENSE_STRING_LANG,
    DUALSENSE_STRING_MANUFACTURER,
    DUALSENSE_STRING_PRODUCT,
    DUALSENSE_STRING_AUDIO,
    DUALSENSE_STRING_HID
)
from dualsense_haptic import DualSenseHapticProcessor
from dualsense_structs import DualSenseInputReport01, DualSenseOutputReport02

logger = logging.getLogger(__name__)

class USBIPDualSenseServer(USBIPServer):
    def __init__(self, host="127.0.0.1", port=3240, on_rumble_callback=None, bus_id="1-1", mac_address=None, on_audio_data_callback=None):
        super().__init__(host=host, port=port, on_rumble_callback=on_rumble_callback, bus_id=bus_id, mac_address=mac_address, on_audio_data_callback=on_audio_data_callback)
        
        # 徹底拆分 IN (麥克風) 與 OUT (喇叭/震動) 的佇列，避免互相阻塞
        self.pending_iso_out_urbs = queue.Queue()
        self.pending_iso_in_urbs = queue.Queue()
        self.last_audio_log = 0
        self.rumble_queue = queue.Queue(maxsize=3)
        
        self.last_state = DualSenseInputReport01()
        self.last_state.ReportId = 0x01
        self.last_state.LeftStickX = 128
        self.last_state.LeftStickY = 128
        self.last_state.RightStickX = 128
        self.last_state.RightStickY = 128
        self.last_state.Hat = 0x08
        self.last_state.PowerPercent = 10
        self.last_state.PowerState = 2
        
        self.last_state.PluggedUsbData = 1
        self.last_state.PluggedMic = 0
        self.last_state.PluggedHeadphones = 0
        self.last_state.MicMuted = 1
        self.audio_active = False

    def start(self):
        import threading
        super().start()
        self.iso_out_thread = threading.Thread(target=self._iso_out_loop, daemon=True)
        self.iso_in_thread = threading.Thread(target=self._iso_in_loop, daemon=True)
        self.rumble_thread = threading.Thread(target=self._rumble_worker, daemon=True)
        # self.iso_out_thread.start() # Disabled: ISO OUT processed inline
        self.iso_in_thread.start()
        self.rumble_thread.start()

    def _iso_out_loop(self):
        """音訊播放：全速回覆，依賴 usbip-win 的硬體排程，避免卡死"""
        while getattr(self, 'running', False):
            try:
                urb = self.pending_iso_out_urbs.get()
                if urb is None: break
                sock, seqnum, devid, direction, ep, transfer_length, out_data, start_frame, num_packets, iso_descriptors = urb
                
                # --- 更積極的 Log 捕捉：只要有聲音就印 (限流 0.5s) ---
                if len(out_data) > 0:
                    non_zero = sum(1 for b in out_data if b != 0)
                    if non_zero > 0:
                        now_time = time.perf_counter()
                        if now_time - getattr(self, 'last_audio_log', 0) > 0.5:
                            logger.info(f"Audio OUT: {len(out_data)} bytes, {non_zero} non-zero bytes")
                            self.last_audio_log = now_time
                            
                    if getattr(self, 'on_audio_data_callback', None):
                        self.on_audio_data_callback(out_data)
                # -----------------------------------------------------
                
                # 移除任何 time.sleep() 或延遲，立刻回覆！
                self._send_iso_reply(sock, seqnum, devid, direction, ep, transfer_length, out_data, start_frame, num_packets, iso_descriptors)
            except Exception as e:
                logger.debug(f"ISO OUT error: {e}")

    def _iso_in_loop(self):
        """麥克風錄音：全速回覆空白資料"""
        while getattr(self, 'running', False):
            try:
                urb = self.pending_iso_in_urbs.get()
                if urb is None: break
                sock, seqnum, devid, direction, ep, transfer_length, out_data, start_frame, num_packets, iso_descriptors = urb
                
                # 移除任何 time.sleep() 或延遲，立刻回覆！
                self._send_iso_reply(sock, seqnum, devid, direction, ep, transfer_length, out_data, start_frame, num_packets, iso_descriptors)
            except Exception as e:
                logger.debug(f"ISO IN error: {e}")

    def _send_iso_reply(self, sock, seqnum, devid, direction, ep, transfer_length, out_data, start_frame, num_packets, iso_descriptors):
        """共用的等時傳輸回覆封裝"""
        status = 0
        actual_length = transfer_length if direction == 1 else len(out_data)
        reply_data = b"\x00" * transfer_length if direction == 1 else b""
        
        ret_header = struct.pack(
            "!IIIII i IiiI 8s",
            USBIP_RET_SUBMIT, seqnum, devid, direction, ep, status, actual_length,
            start_frame, num_packets, 0, b"\x00" * 8
        )
        
        reply_iso_descriptors = b""
        if num_packets > 0 and len(iso_descriptors) == num_packets * 16:
            for i in range(num_packets):
                chunk = iso_descriptors[i*16:(i+1)*16]
                offset, length, act_len, pkt_status = struct.unpack("!IIII", chunk)
                if direction == 0 or direction == 1:
                    act_len = length
                reply_iso_descriptors += struct.pack("!IIII", offset, length, act_len, 0)
                
        with self.send_lock:
            payload = ret_header
            if num_packets > 0:
                payload += reply_iso_descriptors
            if direction == 1 and actual_length > 0:
                payload += reply_data
            sock.sendall(payload)

    def update_input(self, report):
        """Update the 64-byte input report payload"""
        if isinstance(report, bytes):
            if len(report) == 64:
                ctypes.memmove(ctypes.addressof(self.last_state), report, 64)
        else:
            # Assuming it's a DualSenseInputReport01 object
            ctypes.memmove(ctypes.addressof(self.last_state), ctypes.addressof(report), 64)

    def _process_deferred_in_urb(self, sock, seqnum, devid, direction, ep):
        """DualSense HID input is on endpoint 4; the base Nintendo server only accepts EP1."""
        status = 0
        reply_data = b""

        if ep == 4:
            with self.lock:
                reply_data = bytes(self.last_state)
        else:
            status = -1

        actual_length = len(reply_data)
        ret_header = struct.pack(
            "!IIIII i IIII 8s",
            USBIP_RET_SUBMIT, seqnum, devid, direction, ep, status, actual_length,
            0, 0, 0, b"\x00" * 8
        )

        try:
            with self.send_lock:
                sock.sendall(ret_header + reply_data)
        except Exception:
            pass

    def _process_output_report(self, out_data):
        if len(out_data) >= 48 and out_data[0] == 0x02:
            report = DualSenseOutputReport02.from_buffer_copy(out_data[:48])
            
            log_parts = []
            if report.AllowRightTriggerFFB:
                log_parts.append(f"RT: {out_data[11:22].hex()}")
            if report.AllowLeftTriggerFFB:
                log_parts.append(f"LT: {out_data[22:33].hex()}")
            if report.AllowLedColor:
                log_parts.append(f"RGB: ({report.LedRed},{report.LedGreen},{report.LedBlue})")
            if report.AllowHeadphoneVolume:
                log_parts.append(f"HPVol: {report.VolumeHeadphones}")
            if report.AllowAudioMute:
                log_parts.append(f"MicMute: {report.MicMute}")
                # Sync mic mute state back to input report
                self.last_state.MicMuted = report.MicMute
                
            if log_parts:
                now_time = time.perf_counter()
                if now_time - getattr(self, 'last_feature_log', 0) > 0.5:
                    logger.info("DS5 Output Features: " + " | ".join(log_parts))
                    self.last_feature_log = now_time

        if self.on_rumble_callback:
            self.on_rumble_callback(out_data)

    def _rumble_worker(self):
        while getattr(self, 'running', False):
            try:
                # 等待新的震動封包
                payload = self.rumble_queue.get(timeout=1.0)
                if self.on_rumble_callback:
                    self.on_rumble_callback(payload)
            except queue.Empty:
                pass
            except Exception as e:
                logger.debug(f"Rumble dispatch error: {e}")

    def _on_translated_rumble(self, left_intensity, right_intensity):
        """取代原本的 callback 邏輯，改為將封包放入非同步佇列"""
        payload = bytearray(3)
        payload[0] = 0x11
        payload[1] = right_intensity
        payload[2] = left_intensity
        
        # 使用 put_nowait，如果實體搖桿來不及消化，就直接丟棄該幀以保住影片播放
        try:
            self.rumble_queue.put_nowait(payload)
        except queue.Full:
            pass

    def _get_device_desc(self):
        path = f"/sys/devices/virtual/usbip/{self.bus_id}".encode('ascii')
        busid_bytes = self.bus_id.encode('ascii')
        devnum = self.devnum
        
        return struct.pack(
            "!256s32sIIIHHHBBBBBB",
            path,
            busid_bytes,
            1,      # busnum
            devnum, # devnum
            3,      # speed = High Speed (480Mbps)
            0x054c, # idVendor = Sony
            0x0ce6, # idProduct = DualSense
            0x0100, # bcdDevice = 1.00
            0x00,   # bDeviceClass
            0x00,   # bDeviceSubClass
            0x00,   # bDeviceProtocol
            0x01,   # bConfigurationValue
            0x01,   # bNumConfigurations
            0x03    # bNumInterfaces (UAC1 Control, UAC1 Streaming OUT, HID)
        )

    def _send_devlist(self, sock):
        reply_header = struct.pack("!HHI I", USBIP_VERSION, OP_REP_DEVLIST, 0, 1)
        dev_desc = self._get_device_desc()
        
        # 3 Interfaces
        iface0 = struct.pack("!BBBB", 0x01, 0x01, 0x00, 0x00) # Interface 0: Audio Control
        iface1 = struct.pack("!BBBB", 0x01, 0x02, 0x00, 0x00) # Interface 1: Audio Streaming OUT
        iface2 = struct.pack("!BBBB", 0x03, 0x00, 0x00, 0x00) # Interface 2: HID
        
        sock.sendall(reply_header + dev_desc + iface0 + iface1 + iface2)

    def _handle_submit(self, sock, seqnum, devid, direction, ep, transfer_length, setup, out_data, start_frame=0, num_packets=0, iso_descriptors=b""):
        status = 0
        actual_length = 0
        reply_data = b""
        
        if ep == 0: # Control
            reply_data = self._handle_control_request(setup, transfer_length, out_data)
            actual_length = len(reply_data)
            if direction == 0 and len(out_data) > 0:
                actual_length = len(out_data)
        elif ep == 4: # HID IN
            if direction == 1: # IN (Read input state)
                # Defer to background thread
                self.pending_in_urbs.put((sock, seqnum, devid, direction, ep))
                return
        elif ep == 3: # HID OUT
            if direction == 0: # OUT
                if len(out_data) > 0:
                    self._process_output_report(out_data)
                actual_length = len(out_data)
        elif ep == 1 and direction == 0: # Audio Streaming OUT (Haptic/Speaker)
            if len(out_data) > 0:
                # Diagnostic: the _iso_out_loop (which logged audio arrival) is disabled
                # and this inline path was silent, so there was no way to tell whether
                # Windows is actually streaming haptic audio to ep=1.  Log arrival here,
                # throttled to 0.5s, distinguishing real audio (non-zero) from silence.
                non_zero = sum(1 for b in out_data if b != 0)
                now_time = time.perf_counter()
                if now_time - getattr(self, 'last_audio_log', 0) > 0.5:
                    logger.info(
                        "Audio OUT (ep1 ISO): %d bytes, %d non-zero (audio_active=%s)",
                        len(out_data), non_zero, getattr(self, 'audio_active', False),
                    )
                    self.last_audio_log = now_time
                if getattr(self, 'on_audio_data_callback', None):
                    self.on_audio_data_callback(out_data)
            actual_length = len(out_data)
        elif ep == 2 and direction == 1: # Audio Streaming IN (Microphone)
            self.pending_iso_in_urbs.put((sock, seqnum, devid, direction, ep, transfer_length, out_data, start_frame, num_packets, iso_descriptors))
            return
        else:
            status = -1
            
        ret_header = struct.pack(
            "!IIIII i IiiI 8s",
            USBIP_RET_SUBMIT, seqnum, devid, direction, ep, status, actual_length,
            start_frame, num_packets, 0, b"\x00" * 8
        )
        
        reply_iso_descriptors = b""
        if num_packets > 0 and len(iso_descriptors) == num_packets * 16:
            # Reconstruct iso_descriptors with actual_length = length for OUT transfers
            for i in range(num_packets):
                chunk = iso_descriptors[i*16:(i+1)*16]
                offset, length, act_len, pkt_status = struct.unpack("!IIII", chunk)
                if direction == 0 or direction == 1: # OUT or IN
                    act_len = length
                reply_iso_descriptors += struct.pack("!IIII", offset, length, act_len, 0)
        
        with self.send_lock:
            payload = ret_header
            if num_packets > 0 and direction == 1:
                payload += reply_iso_descriptors
            if direction == 1 and actual_length > 0:
                payload += reply_data
            sock.sendall(payload)

    def _handle_control_request(self, setup, transfer_length, out_data=b""):
        req_type, request, value, index, length = struct.unpack("<BBHHH", setup)
        logger.info(f"Control Req: type={req_type:#04x}, req={request:#04x}, val={value:#06x}, idx={index:#06x}, len={length}")
        
        # Standard Device-to-Host Get Descriptor
        if req_type == 0x80 and request == 0x06:
            desc_type = value >> 8
            desc_idx = value & 0xff
            
            if desc_type == 0x01: # Device Descriptor
                return DUALSENSE_DEVICE_DESCRIPTOR[:length]
                
            elif desc_type == 0x02: # Configuration Descriptor
                return DUALSENSE_CONFIGURATION_DESCRIPTOR[:length]
                
            elif desc_type == 0x03: # String Descriptor
                if desc_idx == 0:
                    return DUALSENSE_STRING_LANG[:length]
                elif desc_idx == 1:
                    return DUALSENSE_STRING_MANUFACTURER[:length]
                elif desc_idx == 2:
                    return DUALSENSE_STRING_PRODUCT[:length]
                elif desc_idx == 3:
                    # Unique serial per controller instance (mirrors Switch2: f"SWITCH2EMU_{bus_id}").
                    # Windows uses VID+PID+Serial to identify devices; a shared serial prevents
                    # a second DualSense from being enumerated alongside the first.
                    # bus_id is unique per USBIPAllocator slot, so each player gets a distinct device.
                    serial_str = f"DS5E_{self.bus_id}"
                    encoded = serial_str.encode("utf-16le")
                    return (bytes([len(encoded) + 2, 3]) + encoded)[:length]
                elif desc_idx == 4:
                    return DUALSENSE_STRING_AUDIO[:length]
                elif desc_idx == 5:
                    return DUALSENSE_STRING_HID[:length]
            
            elif desc_type == 0x22: # HID Report Descriptor
                data = DUALSENSE_HID_REPORT_DESCRIPTOR
                if len(data) < length:
                    data = data + b"\x00" * (length - len(data))
                return data[:length]
        
        # Interface-specific Get Descriptor (HID)
        if req_type == 0x81 and request == 0x06:
            desc_type = value >> 8
            if desc_type == 0x22: # HID Report Descriptor
                return DUALSENSE_HID_REPORT_DESCRIPTOR[:length]
                
        # HID Class Requests
        elif req_type == 0x21 and request == 0x09: # SET_REPORT
            # Steam sends SET_REPORT to initialize features.
            # But it can also send Output Report 0x02 via SET_REPORT!
            report_id = value & 0xff
            if (report_id == 0x01 or report_id == 0x02) and len(out_data) > 0:
                self._process_output_report(out_data)
            return b""
        
        elif req_type == 0xA1 and request == 0x01: # GET_REPORT
            report_type = value >> 8
            report_id = value & 0xFF
            if report_type == 3: # Feature Report
                if report_id == 0x05:
                    # Calibration Data: Prevent Division By Zero in SDL2 / Sony SDK
                    calib = bytearray(max(41, length))
                    calib[0] = 0x05
                    # Format: 3 biases, 6 gyro plus/minus, 2 gyro speed plus/minus, 6 acc plus/minus (all little-endian shorts)
                    struct.pack_into("<hhh hhhhhh hh hhhhhh", calib, 1, 
                        0, 0, 0,                               # Gyro Biases (Pitch, Yaw, Roll)
                        8192, -8192, 8192, -8192, 8192, -8192, # Gyro Plus/Minus
                        500, 500,                              # Gyro Speed Plus/Minus (test speed in deg/s)
                        8192, -8192, 8192, -8192, 8192, -8192  # Acc Plus/Minus
                    )
                    return bytes(calib)[:length]
                elif report_id == 0x09:
                    # MAC Address: Required by SDL2 / Sony SDK to uniquely identify the controller
                    mac = bytearray(max(20, length))
                    mac[0] = 0x09
                    if getattr(self, 'mac_address', None):
                        try:
                            mac_parts = [int(x, 16) for x in self.mac_address.split(':')]
                            mac[1:7] = mac_parts[:6]
                        except:
                            mac[1:7] = b'\x00\x11\x22\x33\x44\x55'
                    else:
                        mac[1:7] = b'\x00\x11\x22\x33\x44\x55'
                    return bytes(mac)[:length]
                elif report_id == 0x20:
                    # Firmware Version (Structured as DualSense)
                    fw = bytearray(max(64, length))
                    fw[0] = 0x20
                    # Build Date at 28-39 (12 bytes)
                    fw[28:40] = b'Aug 20 2020\0'
                    # Build Time at 40-48 (9 bytes)
                    fw[40:49] = b'12:00:00\0'
                    # fw_type at 49
                    fw[49] = 0x00
                    # fw_version at 50-53: embed devnum in low byte so each instance
                    # reports a distinct version; prevents Steam/WGI from treating
                    # two virtual DualSense devices as the same physical controller.
                    struct.pack_into("<I", fw, 50, 0x01000000 | (self.devnum & 0xFF))
                    # hw_version at 54-57
                    struct.pack_into("<I", fw, 54, 0x01000000)
                    return bytes(fw)[:length]

                elif report_id == 0x03:
                    # Capabilities
                    cap = bytearray(max(48, length))
                    cap[0] = 0x03
                    cap[2] = 0x28 # Magic for capabilities
                    cap[4] = 0xFF # All features supported (sensors, lightbar, vibration, touchpad)
                    cap[5] = 0x00 # Device type
                    return bytes(cap)[:length]
                elif report_id == 0x81:
                    # Bluetooth MAC Address (Often requested by games expecting BT connection)
                    mac_bt = bytearray(max(64, length))
                    mac_bt[0] = 0x81
                    if getattr(self, 'mac_address', None):
                        try:
                            mac_bt[1:7] = [int(x, 16) for x in self.mac_address.split(':')][:6]
                        except:
                            mac_bt[1:7] = b'\x00\x11\x22\x33\x44\x55'
                    else:
                        mac_bt[1:7] = b'\x00\x11\x22\x33\x44\x55'
                    return bytes(mac_bt)[:length]

                # Default for other feature reports: non-zero buffer to look like a real initialized device
                buf = bytearray(max(1, length))
                buf[0] = report_id
                for i in range(1, len(buf)):
                    buf[i] = 0x11 # non-zero dummy pattern
                return bytes(buf)[:length]
            elif report_type == 1: # Input Report
                if report_id == 0x01:
                    with self.lock:
                        return bytes(self.last_state)[:length]
            return b""
            
        if req_type == 0x21 and request == 0x09: # SET_REPORT
            return b""
                
        # UAC1 Audio Class Requests
        if (req_type & 0x60) == 0x20: # Class request (IN or OUT)
            # AUDIO_CS_REQ_CUR = 0x01, AUDIO_CS_REQ_MIN = 0x02, AUDIO_CS_REQ_MAX = 0x03, AUDIO_CS_REQ_RES = 0x04
            # We just mock the responses to keep Windows happy
            if req_type in (0xA1, 0xA2): # GET_CUR / GET_MIN / etc.
                if length == 1:
                    return b"\x00"
                elif length == 2:
                    if request == 0x82: # GET_MIN
                        return b"\x00\x80" # -32768
                    elif request == 0x83: # GET_MAX
                        return b"\x00\x00" # 0
                    elif request == 0x84: # GET_RES
                        return b"\x00\x01" # 1
                    else: # GET_CUR
                        return b"\x00\x00"
                elif length == 3:
                    return b"\x80\xBB\x00" # 48000
                elif length == 4:
                    return struct.pack("<I", 48000)
                else:
                    return b"\x00" * length
            else: # SET_CUR (OUT)
                if request == 0x0B: # SET_INTERFACE
                    # Setting alt setting activates audio streaming
                    self.audio_active = (value != 0)
                return b""

        if request == 0x0B and req_type in (0x01, 0x11): # Standard Set Interface
            self.audio_active = (value != 0)
            return b""

        # GET_STATUS
        if request == 0x00 and req_type in (0x80, 0x81, 0x82):
            return struct.pack("<H", 0x0001)[:length]

        if req_type in (0x00, 0x01, 0x21) and request in (0x09, 0x0a):
            return b""
            
        # Fallback for unhandled Device-to-Host (GET) requests to return zeroed data.
        # This acts as a default response for Audio Class GET_MIN/MAX/CUR for Volume/Mute,
        # which effectively reports fixed maximum volume (0dB) and unmuted (0).
        if req_type & 0x80:
            return b"\x00" * transfer_length
            
        return b""
