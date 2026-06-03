import socket
import struct
import threading
import logging
import time
import queue

logger = logging.getLogger(__name__)

# Switch 2 Pro Controller - HID Report Descriptor (31 Bytes)
SWITCH_PRO_REPORT_DESCRIPTOR = bytes([
    0x06, 0x00, 0xff, 0x09, 0x01, 0xa1, 0x01, 0x15, 0x00, 0x26, 0xff, 0x00,
    0x75, 0x08, 0x85, 0x05, 0x95, 0x3f, 0x09, 0x01, 0x81, 0x02,
    0x85, 0x02, 0x95, 0x3f, 0x09, 0x01, 0x91, 0x02, 0xc0
])

# USBIP Protocol Constants
USBIP_VERSION = 0x0111
OP_REQ_DEVLIST = 0x8005
OP_REP_DEVLIST = 0x0005
OP_REQ_IMPORT = 0x8003
OP_REP_IMPORT = 0x0003

USBIP_CMD_SUBMIT = 0x00000001
USBIP_RET_SUBMIT = 0x00000003
USBIP_CMD_UNLINK = 0x00000002
USBIP_RET_UNLINK = 0x00000004

class USBIPServer:
    def __init__(self, host="127.0.0.1", port=3240, on_rumble_callback=None, bus_id="1-1", mac_address=None):
        self.host = host
        self.port = port
        self.on_rumble_callback = on_rumble_callback
        self.bus_id = bus_id
        
        # Extract devnum from bus_id
        try:
            self.devnum = int(self.bus_id.split("-")[-1])
        except Exception:
            self.devnum = 1
        
        self.mac_bytes = None
        if mac_address:
            try:
                cleaned = mac_address.replace(":", "").replace("-", "").strip()
                if len(cleaned) == 12:
                    val_bytes = bytes.fromhex(cleaned)
                    self.mac_bytes = val_bytes[::-1] # LSB-first
            except Exception as e:
                logger.error(f"Error parsing MAC address {mac_address}: {e}")
        
        if self.mac_bytes is None:
            # Fallback unique MAC based on devnum: 38:C6:CE:27:FC:2D -> 2d fc 27 ce c6 38
            mac_array = bytearray(b"\x2d\xfc\x27\xce\xc6\x38")
            mac_array[0] = (mac_array[0] + self.devnum) & 0xff
            self.mac_bytes = bytes(mac_array)
            
        self.server_socket = None
        self.running = False
        self.server_thread = None
        self.client_thread = None
        
        # State queue for inputs and bulk responses
        self.input_queue = queue.Queue(maxsize=1)
        self.bulk_response_queue = queue.Queue()
        self.pending_in_urbs = queue.Queue()
        self.last_state = bytearray(64)
        self.last_state[0] = 0x05 # Report ID
        self.last_state[2] = 0x12 # Switch 2 status byte
        self.seq_num = 0
        
        # Lock for thread safety
        self.lock = threading.Lock()
        self.send_lock = threading.Lock()

    def start(self):
        self.running = True
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_socket.bind((self.host, self.port))
        self.server_socket.listen(1)
        
        self.server_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self.in_thread = threading.Thread(target=self._in_urb_loop, daemon=True)
        self.server_thread.start()
        logger.info(f"USBIP Server started on {self.host}:{self.port}")

    def stop(self):
        self.running = False
        if self.server_socket:
            try:
                # Force close to wake up accept()
                self.server_socket.close()
            except Exception:
                pass
        
        # Wake up any blocked queues
        self.update_state(self.last_state)
        self.pending_in_urbs.put(None)
        
        if self.server_thread:
            self.server_thread.join(timeout=2.0)
        logger.info("USBIP Server stopped.")

    def update_state(self, state_bytes):
        with self.lock:
            self.last_state = bytearray(state_bytes)
            self.last_state[1] = self.seq_num & 0xff
            self.seq_num += 1
            
        # Push to queue to wake up any pending read
        try:
            # Clear old if not consumed
            if self.input_queue.full():
                self.input_queue.get_nowait()
            self.input_queue.put_nowait(bytes(self.last_state))
        except Exception:
            pass

    def _recvexact(self, sock, n):
        """Read exactly n bytes from socket. Raises ConnectionError on disconnect."""
        buf = b""
        while len(buf) < n:
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError(f"Connection closed while reading {n} bytes (got {len(buf)})")
            buf += chunk
        return buf

    def _accept_loop(self):
        while self.running:
            try:
                client_sock, client_addr = self.server_socket.accept()
                logger.info(f"USBIP connection accepted from {client_addr}")
                self._handle_client(client_sock)
            except Exception as e:
                if self.running:
                    logger.debug(f"Accept loop error: {e}")
                time.sleep(0.5)

    def _handle_client(self, sock):
        try:
            # 1. Handle handshakes (OP_REQ_DEVLIST or OP_REQ_IMPORT)
            while self.running:
                header = sock.recv(8)
                if not header or len(header) < 8:
                    break
                
                version, code, status = struct.unpack("!HHI", header)
                if version != USBIP_VERSION:
                    logger.warning(f"Unsupported USBIP version: {version:#x}")
                    break
                
                if code == OP_REQ_DEVLIST:
                    logger.info("Handling OP_REQ_DEVLIST request")
                    self._send_devlist(sock)
                elif code == OP_REQ_IMPORT:
                    busid = sock.recv(32).decode('ascii').strip('\x00')
                    logger.info(f"Handling OP_REQ_IMPORT request for busid {busid}")
                    self._send_import_reply(sock)
                    # Enter Data Phase
                    self._data_phase_loop(sock)
                    break
                else:
                    logger.warning(f"Unknown OP_REQ code: {code:#x}")
                    break
        except Exception as e:
            logger.error(f"Error in client handler: {e}")
        finally:
            if self.on_rumble_callback:
                try:
                    self.on_rumble_callback(bytearray(64))
                except Exception:
                    pass
            try:
                sock.close()
            except Exception:
                pass
            logger.info("USBIP connection closed.")

    def _get_device_desc(self):
        # 312 bytes USBIP device description block for OP_REP_DEVLIST / OP_REP_IMPORT
        # USBIP speed codes: 1=Low(1.5Mbps), 2=Full(12Mbps), 3=High(480Mbps)
        # CRITICAL: Must match our bulk endpoint wMaxPacketSize=512.
        #   - Full Speed bulk max = 64 bytes -> 512 is INVALID -> Windows marks device Unknown!
        #   - High Speed bulk max = 512 bytes -> VALID
        path = f"/sys/devices/virtual/usbip/{self.bus_id}".encode('ascii')
        busid_bytes = self.bus_id.encode('ascii')
        devnum = self.devnum
        return struct.pack(
            "!256s32sIIIHHHBBBBBB",
            path,
            busid_bytes,
            1,      # busnum
            devnum, # devnum
            3,      # speed = High Speed (480Mbps) -- was 2 (Full Speed) which is WRONG
            0x057e, # idVendor = Nintendo
            0x2069, # idProduct = Switch 2 Pro Controller
            0x0104, # bcdDevice = 1.04
            0x00,   # bDeviceClass (Defined at interface level)
            0x00,   # bDeviceSubClass
            0x00,   # bDeviceProtocol
            0x01,   # bConfigurationValue
            0x01,   # bNumConfigurations
            0x02    # bNumInterfaces (HID + WinUSB bulk)
        )

    def _send_devlist(self, sock):
        # Reply header: version, code (OP_REP_DEVLIST), status, device count (1)
        reply_header = struct.pack("!HHI I", USBIP_VERSION, OP_REP_DEVLIST, 0, 1)
        dev_desc = self._get_device_desc()
        
        # 2 Interfaces for the device
        iface0 = struct.pack("!BBBB", 0x03, 0x00, 0x00, 0x00) # Interface 0: HID
        iface1 = struct.pack("!BBBB", 0xFF, 0x00, 0x00, 0x00) # Interface 1: Vendor Specific (WinUSB)
        
        sock.sendall(reply_header + dev_desc + iface0 + iface1)

    def _send_import_reply(self, sock):
        reply_header = struct.pack("!HHI", USBIP_VERSION, OP_REP_IMPORT, 0)
        dev_desc = self._get_device_desc()
        sock.sendall(reply_header + dev_desc)

    def _data_phase_loop(self, sock):
        logger.info("Entered Data Phase loop")
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        
        while self.running:
            # 1. Read common header: EXACTLY 20 bytes
            # CRITICAL: TCP is a stream protocol; recv() may return fewer bytes than requested.
            # We must loop until we have exactly 20 bytes or the connection closes.
            try:
                common_header = self._recvexact(sock, 20)
            except ConnectionError as e:
                logger.info(f"Data loop exit: {e}")
                break
            
            command, seqnum, devid, direction, ep = struct.unpack("!IIIII", common_header)
            logger.debug(f"CMD: {command:#010x}, seq={seqnum}, devid={devid:#010x}, dir={direction}, ep={ep}")
            
            if command == USBIP_CMD_SUBMIT:
                # Read SUBMIT specific payload: EXACTLY 28 bytes
                try:
                    submit_payload = self._recvexact(sock, 28)
                except ConnectionError as e:
                    logger.info(f"Data loop exit reading SUBMIT payload: {e}")
                    break
                
                transfer_flags, transfer_length, start_frame, num_packets, interval, setup = struct.unpack(
                    "!IIIII 8s", submit_payload
                )
                
                out_data = b""
                if direction == 0 and transfer_length > 0: # OUT transfer (host -> device)
                    # Read transfer data with exact byte count guarantee
                    try:
                        out_data = self._recvexact(sock, transfer_length)
                    except ConnectionError as e:
                        logger.info(f"Data loop exit reading OUT data: {e}")
                        break
                
                # Handle the USB Request
                self._handle_submit(sock, seqnum, devid, direction, ep, transfer_length, setup, out_data)
                
            elif command == USBIP_CMD_UNLINK:
                # Read UNLINK specific payload: EXACTLY 28 bytes
                try:
                    unlink_payload = self._recvexact(sock, 28)
                except ConnectionError as e:
                    logger.info(f"Data loop exit reading UNLINK: {e}")
                    break
                
                seqnum_to_unlink = struct.unpack("!I", unlink_payload[:4])[0]
                logger.debug(f"Unlink requested for seqnum {seqnum_to_unlink}")
                
                # Send RET_UNLINK
                ret_header = struct.pack("!IIIII i 28s", USBIP_RET_UNLINK, seqnum, devid, direction, ep, 0, b"\x00" * 28)
                sock.sendall(ret_header)
            else:
                logger.warning(f"Unknown USBIP Command: {command:#010x}")
                break

    def _handle_submit(self, sock, seqnum, devid, direction, ep, transfer_length, setup, out_data):
        status = 0
        actual_length = 0
        reply_data = b""
        
        if ep == 0: # Control Endpoint
            reply_data = self._handle_control_request(setup, transfer_length)
            actual_length = len(reply_data)
        elif ep == 1: # HID Interface Endpoint
            if direction == 1: # IN (Read input state)
                now = time.perf_counter()
                elapsed = now - getattr(self, 'last_ep1_in_time', 0)
                if elapsed < 0.004:
                    time.sleep(0.004 - elapsed)
                self.last_ep1_in_time = time.perf_counter()
                
                try:
                    reply_data = self.input_queue.get_nowait()
                except queue.Empty:
                    with self.lock:
                        reply_data = bytes(self.last_state)
                actual_length = len(reply_data)
            else: # OUT (Rumble output report)
                if len(out_data) > 0:
                    if self.on_rumble_callback:
                        self.on_rumble_callback(out_data)
                    actual_length = len(out_data)
        elif ep == 2: # WinUSB Bulk Interface Endpoint
            if direction == 1: # IN
                try:
                    reply_data = self.bulk_response_queue.get_nowait()
                except queue.Empty:
                    reply_data = b""
                actual_length = len(reply_data)
            else: # OUT
                if len(out_data) > 0:
                    self._handle_bulk_command(out_data)
                    actual_length = len(out_data)
        else:
            logger.warning(f"Submit request to unknown endpoint: {ep}")
            status = -1
            
        ret_header = struct.pack(
            "!IIIII i IIII 8s",
            USBIP_RET_SUBMIT, seqnum, devid, direction, ep, status, actual_length,
            0, 0, 0, b"\x00" * 8
        )
        
        with self.send_lock:
            if direction == 1 and actual_length > 0:
                sock.sendall(ret_header + reply_data)
            else:
                sock.sendall(ret_header)

    def _in_urb_loop(self):
        """專門處理 IN URB 的背景執行緒，以智慧限速消除 Burst (防止 dt=0)，同時避免延遲堆疊"""
        last_send_time = time.perf_counter()
        while self.running:
            try:
                urb = self.pending_in_urbs.get()
                if urb is None:
                    break
                sock, seqnum, devid, direction, ep = urb
                
                now = time.perf_counter()
                elapsed = now - last_send_time
                if elapsed < 0.001:
                    time.sleep(0.001 - elapsed)
                
                self._process_deferred_in_urb(sock, seqnum, devid, direction, ep)
                last_send_time = time.perf_counter()
            except Exception as e:
                if self.running:
                    logger.debug(f"IN URB worker error: {e}")

    def _process_deferred_in_urb(self, sock, seqnum, devid, direction, ep):
        status = 0
        reply_data = b""
        
        if ep == 1:
            try:
                reply_data = self.subcmd_reply_queue.get_nowait()
            except queue.Empty:
                try:
                    reply_data = self.input_queue.get_nowait()
                except queue.Empty:
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


    def _handle_control_request(self, setup, transfer_length):
        req_type, request, value, index, length = struct.unpack("<BBHHH", setup)
        logger.info(f"Control Request: req_type={req_type:#04x}, req={request:#04x}, value={value:#06x}, index={index:#06x}, len={length}")
        
        # Standard Device-to-Host Get Descriptor
        if req_type == 0x80 and request == 0x06:
            desc_type = value >> 8
            desc_idx = value & 0xff
            
            if desc_type == 0x01: # Device Descriptor
                desc = struct.pack(
                    "<BBHBBBBHHHBBBB",
                    18,     # bLength
                    1,      # bDescriptorType = Device
                    0x0200, # bcdUSB = USB 2.0
                    0x00,   # bDeviceClass = Defined at interface level
                    0x00,   # bDeviceSubClass
                    0x00,   # bDeviceProtocol
                    0x40,   # bMaxPacketSize0 = 64 bytes
                    0x057e, # idVendor = Nintendo
                    0x2069, # idProduct = Switch 2 Pro Controller
                    0x0104, # bcdDevice = 1.04
                    1,      # iManufacturer
                    2,      # iProduct
                    3,      # iSerialNumber
                    1       # bNumConfigurations
                )
                logger.info(f"  -> Device Descriptor ({len(desc)} bytes)")
                return desc[:length]
                
            elif desc_type == 0x02: # Configuration Descriptor (HID + Bulk)
                # Interface 0: HID (class 0x03, subclass 0x00, protocol 0x00)
                iface0 = struct.pack("<BBBBBBBBB",
                    9, 4,       # bLength, bDescriptorType=Interface
                    0,          # bInterfaceNumber
                    0,          # bAlternateSetting
                    2,          # bNumEndpoints
                    0x03,       # bInterfaceClass = HID
                    0x00,       # bInterfaceSubClass (0=no boot)
                    0x00,       # bInterfaceProtocol (0=none)
                    0           # iInterface
                )
                # HID class descriptor
                hid = struct.pack("<BBHBBBH",
                    9,      # bLength
                    0x21,   # bDescriptorType = HID
                    0x0111, # bcdHID = 1.11
                    0x00,   # bCountryCode
                    1,      # bNumDescriptors
                    0x22,   # bDescriptorType = Report
                    len(SWITCH_PRO_REPORT_DESCRIPTOR)      # wDescriptorLength (must match report_desc below)
                )
                # Endpoint Descriptor format: bLength(7) bDescType(5) bEndpointAddress bmAttributes wMaxPacketSize bInterval
                # Correct struct: <BBBBHB = 1+1+1+1+2+1 = 7 bytes total
                ep1_in  = struct.pack("<BBBBHB", 7, 5, 0x81, 0x03, 64, 8)  # Interrupt IN
                ep1_out = struct.pack("<BBBBHB", 7, 5, 0x01, 0x03, 64, 8)  # Interrupt OUT
                
                # Interface 1: Vendor Specific Bulk (WinUSB)
                iface1 = struct.pack("<BBBBBBBBB",
                    9, 4,       # bLength, bDescriptorType=Interface
                    1,          # bInterfaceNumber
                    0,          # bAlternateSetting
                    2,          # bNumEndpoints
                    0xFF,       # bInterfaceClass = Vendor Specific
                    0x00,       # bInterfaceSubClass
                    0x00,       # bInterfaceProtocol
                    0           # iInterface
                )
                ep2_in  = struct.pack("<BBBBHB", 7, 5, 0x82, 0x02, 512, 0)  # Bulk IN
                ep2_out = struct.pack("<BBBBHB", 7, 5, 0x02, 0x02, 512, 0)  # Bulk OUT
                
                interfaces = iface0 + hid + ep1_in + ep1_out + iface1 + ep2_in + ep2_out
                total_len = 9 + len(interfaces)
                # Configuration descriptor
                cfg = struct.pack("<BBHBBBBB",
                    9,          # bLength
                    2,          # bDescriptorType = Configuration
                    total_len,  # wTotalLength (dynamic)
                    2,          # bNumInterfaces
                    1,          # bConfigurationValue
                    0,          # iConfiguration
                    0xC0,       # bmAttributes = Self-powered
                    250         # bMaxPower = 500mA
                )
                desc = cfg + interfaces
                logger.info(f"  -> Config Descriptor ({len(desc)} bytes, requested {length})")
                return desc[:length]
                
            elif desc_type == 0x03: # String Descriptor
                if desc_idx == 0: # Supported Languages
                    desc = struct.pack("<BBH", 4, 3, 0x0409) # English (US)
                elif desc_idx == 1: # iManufacturer
                    s = "Nintendo Co., Ltd.".encode("utf-16le")
                    desc = struct.pack("<BB", 2 + len(s), 3) + s
                elif desc_idx == 2: # iProduct
                    s = "Nintendo Switch Pro Controller".encode("utf-16le")
                    desc = struct.pack("<BB", 2 + len(s), 3) + s
                elif desc_idx == 3: # iSerialNumber
                    # Use a unique serial distinct from the physical controller (HA2F83JF)
                    # to prevent Windows PnP from confusing the two devices.
                    serial_str = f"SWITCH2EMU_{self.bus_id}"
                    s = serial_str.encode("utf-16le")
                    desc = struct.pack("<BB", 2 + len(s), 3) + s
                elif desc_idx == 0xEE: # MSFT100 OS String Descriptor
                    # Must be exactly 18 bytes
                    s = "MSFT100".encode("utf-16le") # 14 bytes
                    desc = struct.pack("<BB", 18, 3) + s + bytes([0xcd, 0x00]) # bVendorCode=0xcd, bPad=0
                    logger.info("  -> MSFT100 OS String Descriptor")
                else:
                    logger.debug(f"  -> Unknown string index {desc_idx:#x}")
                    desc = b""
                return desc[:length]
            
            elif desc_type == 0x22: # HID Report Descriptor (from Interface descriptor request context)
                # Vendor-defined: report 0x09 = 64-byte input, report 0x02 = 64-byte output
                report_desc = SWITCH_PRO_REPORT_DESCRIPTOR
                logger.info(f"  -> HID Report Descriptor ({len(report_desc)} bytes)")
                return report_desc[:length]
        
        # Interface-specific Get Descriptor (for HID report descriptor)
        if req_type == 0x81 and request == 0x06:
            desc_type = value >> 8
            if desc_type == 0x22: # HID Report Descriptor
                report_desc = SWITCH_PRO_REPORT_DESCRIPTOR
                logger.info(f"  -> Interface HID Report Descriptor ({len(report_desc)} bytes)")
                return report_desc[:length]
        
        # Vendor-specific MS OS Descriptors (device-to-host)
        if (req_type & 0x40) == 0x40 and request == 0xcd:
            if index == 0x0004: # Extended Compat ID Descriptor -> WINUSB for Interface 1
                # Header: dwLength(4) + bcdVersion(2) + wIndex(2) + bCount(1) + reserved(7) = 16 bytes
                # Section: bFirstInterfaceNumber(1) + reserved(1) + CompatibleID(8) + SubCompatibleID(8) + reserved(6) = 24 bytes
                # Total = 40 bytes
                header = struct.pack("<IHHB7s", 40, 0x0100, 4, 1, b"\x00" * 7)
                # Interface 1 gets WINUSB, Interface 0 (HID) gets no override
                section = struct.pack("<BB8s8s6s", 1, 1, b"WINUSB\x00\x00", b"\x00" * 8, b"\x00" * 6)
                result = (header + section)[:length]
                logger.info(f"  -> MS OS ExtCompatID Descriptor ({len(result)} bytes)")
                return result
                
            elif index == 0x0005: # Extended Properties Descriptor -> DeviceInterfaceGUID
                prop_name = "DeviceInterfaceGUID".encode("utf-16le") + b"\x00\x00"
                prop_data = "{6F13725E-EF0E-4FD3-AE5F-B2DE989EC825}".encode("utf-16le") + b"\x00\x00"
                prop_size = 4 + 4 + 2 + len(prop_name) + 4 + len(prop_data)
                total_size = 10 + prop_size
                header = struct.pack("<IHHH", total_size, 0x0100, 5, 1)
                prop = struct.pack("<IIH", prop_size, 1, len(prop_name)) + prop_name + struct.pack("<I", len(prop_data)) + prop_data
                result = (header + prop)[:length]
                logger.info(f"  -> MS OS ExtProperties Descriptor ({len(result)} bytes)")
                return result

        # GET_STATUS (request=0x00): return 2 bytes - bit0=self-powered, bit1=remote-wakeup
        if request == 0x00 and req_type in (0x80, 0x81, 0x82):
            # Device status: self-powered=1, remote-wakeup=0
            status_bytes = struct.pack("<H", 0x0001)
            logger.info(f"  -> GET_STATUS ({length} bytes requested) -> {status_bytes.hex()}")
            return status_bytes[:length]

        # Class requests (handled but no data returned)
        if req_type in (0x00, 0x01, 0x21) and request in (0x09, 0x0a, 0x0b):
            req_names = {0x09: "SetConfiguration", 0x0a: "SetIdle", 0x0b: "SetInterface"}
            logger.info(f"  -> {req_names.get(request, f'ClassReq {request:#x}')} (no data)")
            return b""

        logger.warning(f"  -> UNHANDLED: req_type={req_type:#04x}, req={request:#04x}, value={value:#06x}, index={index:#06x}, len={length}")
        return b""

    def _handle_bulk_command(self, cmd):
        c0 = cmd[0]
        arg1Hi = cmd[3] if len(cmd) > 3 else 0
        
        # Log all bulk commands to help diagnose gyro / init issues
        logger.info(f"Bulk CMD: {cmd[:min(len(cmd),16)].hex()} c0={c0:#04x} arg1Hi={arg1Hi:#04x}")
        
        reply = None
        
        if len(cmd) >= 16 and c0 == 0x02: # Flash Read Command
            reply = self._build_flash_read_reply(cmd)
        elif c0 == 0x03 and arg1Hi == 0x0d:
            reply = self._build_ack(cmd, 12)
            reply[8] = 0x01
        elif c0 == 0x15 and arg1Hi == 0x01: # MAC Address Query
            reply = self._build_ack(cmd, 17)
            reply[8] = 0x01
            reply[9] = 0x04
            reply[10] = 0x01
            reply[11:17] = self.mac_bytes
        elif c0 == 0x15 and (arg1Hi == 0x02 or arg1Hi == 0x03):
            reply = self._build_ack(cmd, 25 if arg1Hi == 0x02 else 9)
            reply[8] = 0x01
        elif c0 == 0x11:
            reply = self._build_ack(cmd, 37)
            reply[8] = 0x01
            reply[9:37] = bytes.fromhex("20 03 00 00 0a e8 1c 3b 79 7d 8b 3a 0a e8 9c 42 58 a0 0b 42 0a e8 9c 41 58 a0 0b 41")
        elif c0 == 0x01 and arg1Hi == 0x0c:
            reply = self._build_ack(cmd, 12)
            reply[8:12] = bytes.fromhex("61 12 50 10")
        elif c0 == 0x03 and arg1Hi == 0x01:
            reply = self._build_ack(cmd, 16)
            reply[10] = 0x40
            reply[11] = 0xf0
            reply[14] = 0x60
        else:
            reply = self._build_ack(cmd, 8)
            
        if reply:
            logger.info(f"Bulk REPLY: {bytes(reply).hex()}")
            self.bulk_response_queue.put(bytes(reply))


    def _build_ack(self, cmd, length):
        reply = bytearray(length)
        reply[0] = cmd[0]
        reply[1] = 0x01
        if length > 2 and len(cmd) > 2: reply[2] = cmd[2]
        if length > 3 and len(cmd) > 3: reply[3] = cmd[3]
        if length > 4 and len(cmd) > 4: reply[4] = cmd[4]
        if length > 5: reply[5] = 0xf8
        return reply

    def _build_flash_read_reply(self, cmd):
        # Decode read address
        address = cmd[12] | (cmd[13] << 8) | (cmd[14] << 16) | (cmd[15] << 24)
        
        data_len = 0x40
        if address == 0x13040:
            data_len = 0x10
        elif address == 0x13100:
            data_len = 0x18
        elif address == 0x13060:
            data_len = 0x20
            
        reply = bytearray(16 + data_len)
        data = bytearray(data_len)
        
        if address == 0x13000: # Serial number
            serial_str = f"HA2F83J{self.devnum}"
            serial = serial_str.encode("ascii")
            data[2:2+len(serial)] = serial
        elif address in [0x13080, 0x130c0]: # Stick calibration
            data[:] = [0xff] * data_len
            # Neutral 2048, Max 2048, Min 2048 calibration
            calib = bytes.fromhex("00 08 80 00 08 80 00 08 80") # packed pairs
            data[0x28:0x28+len(calib)] = calib
        elif address in [0x1fc040, 0x1fc080, 0x13060]:
            data[:] = [0xff] * data_len
        elif address == 0x13040:
            data[:] = bytes.fromhex("16 f4 d3 41 48 ce 85 ba f1 05 71 ba 1f 27 cb 3b")
        elif address == 0x13100:
            data[:] = bytes.fromhex("00 00 00 00 00 00 00 00 00 00 00 00 2d 10 a7 3d e7 49 35 3c a4 2d 20 41")
            
        reply[0] = 0x02
        reply[1] = 0x01
        reply[2] = cmd[2]
        reply[3] = cmd[3]
        reply[5] = 0xf8
        reply[8] = data_len
        reply[12:16] = cmd[12:16] # copy address
        reply[16:] = data
        return reply


JOYCON_REPORT_DESC = bytes.fromhex(
    "05010905a1010601ff8521092175089531810285300930750895318102853109"
    "317508966901810285320932750896690181028533093375089669018102853f"
    "0509190129101500250175019510810205010939150025077504950181420509"
    "7504950181010501093009310933093416000027ffff00007510950481020601"
    "ff85010901750895319102851009107508953191028511091175089531910285"
    "120912750895319102c0"
)


SWITCH1_PRO_REPORT_DESCRIPTOR = bytes.fromhex(
    "050115000904a1018530050105091901290a150025017501950a550065008102"
    "0509190b290e150025017501950481027501950281030b01000100a1000b3000"
    "01000b310001000b320001000b35000100150027ffff0000751095048102c00b"
    "39000100150025073500463b0165147504950181020509190f29121500250175"
    "01950481027508953481030600ff852109017508953f8103858109027508953f"
    "8103850109037508953f9183851009047508953f9183858009057508953f9183"
    "858209067508953f9183c0"
)


class USBIPJoyConServer(USBIPServer):
    def __init__(self, device_type="L", host="127.0.0.1", port=3240, on_rumble_callback=None, bus_id="1-1", mac_address=None):
        super().__init__(host=host, port=port, on_rumble_callback=on_rumble_callback, bus_id=bus_id, mac_address=mac_address)
        self.device_type = device_type
        self.product_id = 0x2006 if device_type == "L" else 0x2007
        self._timer = 0
        self._device_info_queried = False
        self._vibration_enabled = False
        self._imu_enabled = False
        
        # Subcommand reply queue
        self.subcmd_reply_queue = queue.Queue(maxsize=4)
        
        # Default standard 50-byte input report (0x30)
        self.last_state = bytearray(50)
        self.last_state[0] = 0x30
        self.last_state[2] = 0x9E if device_type == "L" else 0x8E
        # Neutral sticks: left=2048 (0x800), right=2048 (0x800)
        # Left stick (bytes 6-8):
        self.last_state[6] = 0x00
        self.last_state[7] = 0x08
        self.last_state[8] = 0x80
        # Right stick (bytes 9-11):
        self.last_state[9] = 0x00
        self.last_state[10] = 0x08
        self.last_state[11] = 0x80

    def start(self):
        super().start()
        self.in_thread.start()
        
    def update_state(self, state_bytes):
        """JoyCon override: 不遞增 seq_num（由 EP1 IN 真正送出時遞增）"""
        with self.lock:
            self.last_state = bytearray(state_bytes)
        try:
            if self.input_queue.full():
                self.input_queue.get_nowait()
            self.input_queue.put_nowait(bytes(self.last_state))
        except Exception:
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
            3,      # speed = High Speed (480Mbps) -- bulk EP2 requires High Speed
            0x057e, # idVendor = Nintendo
            self.product_id, # idProduct
            0x0200, # bcdDevice = 2.00
            0x00,   # bDeviceClass
            0x00,   # bDeviceSubClass
            0x00,   # bDeviceProtocol
            0x01,   # bConfigurationValue
            0x01,   # bNumConfigurations
            0x02    # bNumInterfaces (HID + WinUSB bulk)
        )

    def _send_devlist(self, sock):
        reply_header = struct.pack("!HHI I", USBIP_VERSION, OP_REP_DEVLIST, 0, 1)
        dev_desc = self._get_device_desc()
        
        # 2 Interfaces
        iface0 = struct.pack("!BBBB", 0x03, 0x00, 0x00, 0x00) # Interface 0: HID
        iface1 = struct.pack("!BBBB", 0xFF, 0x00, 0x00, 0x00) # Interface 1: Vendor Specific (WinUSB)
        
        sock.sendall(reply_header + dev_desc + iface0 + iface1)

    def _handle_submit(self, sock, seqnum, devid, direction, ep, transfer_length, setup, out_data):
        status = 0
        actual_length = 0
        reply_data = b""
        
        if ep == 0: # Control
            reply_data = self._handle_control_request(setup, transfer_length)
            actual_length = len(reply_data)
        elif ep == 1: # HID
            if direction == 1: # IN
                # Defer IN URB to background thread to avoid blocking Socket recv
                self.pending_in_urbs.put((sock, seqnum, devid, direction, ep))
                return
            else: # OUT
                if len(out_data) > 0:
                    self._handle_output_report(out_data)
                    actual_length = len(out_data)
                
                # Immediate ACK for OUT without data payload
                ret_header = struct.pack(
                    "!IIIII i IIII 8s",
                    USBIP_RET_SUBMIT, seqnum, devid, direction, ep, status, actual_length,
                    0, 0, 0, b"\x00" * 8
                )
                with self.send_lock:
                    sock.sendall(ret_header)
                return
        elif ep == 2: # WinUSB Bulk Interface
            if direction == 1: # IN
                try:
                    reply_data = self.bulk_response_queue.get_nowait()
                except queue.Empty:
                    reply_data = b""
                actual_length = len(reply_data)
            else: # OUT
                if len(out_data) > 0:
                    self._handle_bulk_command(out_data)
                    actual_length = len(out_data)
        else:
            status = -1
            
        ret_header = struct.pack(
            "!IIIII i IIII 8s",
            USBIP_RET_SUBMIT, seqnum, devid, direction, ep, status, actual_length,
            0, 0, 0, b"\x00" * 8
        )
        
        with self.send_lock:
            if direction == 1 and actual_length > 0:
                sock.sendall(ret_header + reply_data)
            else:
                sock.sendall(ret_header)

    def _process_deferred_in_urb(self, sock, seqnum, devid, direction, ep):
        status = 0
        reply_data = b""
        
        if ep == 1:
            try:
                reply_data = self.subcmd_reply_queue.get_nowait()
            except queue.Empty:
                # 節流 input（限速 250Hz）
                now = time.perf_counter()
                elapsed = now - getattr(self, 'last_ep1_in_time', 0)
                if elapsed < 0.004:
                    time.sleep(0.004 - elapsed)
                self.last_ep1_in_time = time.perf_counter()

                try:
                    reply_data_bytes = self.input_queue.get_nowait()
                    reply_data = bytearray(reply_data_bytes)
                    if len(reply_data) > 0 and reply_data[0] == 0x30:
                        reply_data[1] = self.seq_num & 0xff
                        self.seq_num += 1
                    reply_data = bytes(reply_data)
                except queue.Empty:
                    with self.lock:
                        if self.last_state[0] == 0x30:
                            self.last_state[1] = self.seq_num & 0xff
                            self.seq_num += 1
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

    def _handle_control_request(self, setup, transfer_length):
        req_type, request, value, index, length = struct.unpack("<BBHHH", setup)
        logger.info(f"Joy-Con Control Request: req_type={req_type:#04x}, req={request:#04x}, value={value:#06x}, index={index:#06x}, len={length}")
        
        # Standard Device-to-Host Get Descriptor
        if req_type == 0x80 and request == 0x06:
            desc_type = value >> 8
            desc_idx = value & 0xff
            
            if desc_type == 0x01: # Device Descriptor
                desc = struct.pack(
                    "<BBHBBBBHHHBBBB",
                    18,     # bLength
                    1,      # bDescriptorType = Device
                    0x0200, # bcdUSB = USB 2.0
                    0x00,   # bDeviceClass
                    0x00,   # bDeviceSubClass
                    0x00,   # bDeviceProtocol
                    0x40,   # bMaxPacketSize0 = 64 bytes
                    0x057e, # idVendor = Nintendo
                    self.product_id, # idProduct
                    0x0200, # bcdDevice = 2.00
                    1,      # iManufacturer
                    2,      # iProduct
                    3,      # iSerialNumber
                    1       # bNumConfigurations
                )
                return desc[:length]
                
            elif desc_type == 0x02: # Config Descriptor (HID + WinUSB Bulk)
                iface0 = struct.pack("<BBBBBBBBB",
                    9, 4,       # bLength, bDescriptorType=Interface
                    0,          # bInterfaceNumber
                    0,          # bAlternateSetting
                    2,          # bNumEndpoints
                    0x03,       # bInterfaceClass = HID
                    0x00,       # bInterfaceSubClass (0=no boot)
                    0x00,       # bInterfaceProtocol (0=none)
                    0           # iInterface
                )
                hid = struct.pack("<BBHBBBH",
                    9,      # bLength
                    0x21,   # bDescriptorType = HID
                    0x0111, # bcdHID = 1.11
                    0x00,   # bCountryCode
                    1,      # bNumDescriptors
                    0x22,   # bDescriptorType = Report
                    len(JOYCON_REPORT_DESC) # wDescriptorLength
                )
                ep1_in  = struct.pack("<BBBBHB", 7, 5, 0x81, 0x03, 64, 8)  # Interrupt IN
                ep1_out = struct.pack("<BBBBHB", 7, 5, 0x01, 0x03, 64, 8)  # Interrupt OUT
                
                # Interface 1: Vendor Specific (WinUSB Bulk)
                iface1 = struct.pack("<BBBBBBBBB",
                    9, 4,       # bLength, bDescriptorType=Interface
                    1,          # bInterfaceNumber
                    0,          # bAlternateSetting
                    2,          # bNumEndpoints
                    0xFF,       # bInterfaceClass = Vendor Specific
                    0x00,       # bInterfaceSubClass
                    0x00,       # bInterfaceProtocol
                    0           # iInterface
                )
                ep2_in  = struct.pack("<BBBBHB", 7, 5, 0x82, 0x02, 512, 0)  # Bulk IN
                ep2_out = struct.pack("<BBBBHB", 7, 5, 0x02, 0x02, 512, 0)  # Bulk OUT
                
                interfaces = iface0 + hid + ep1_in + ep1_out + iface1 + ep2_in + ep2_out
                total_len = 9 + len(interfaces)
                
                cfg = struct.pack("<BBHBBBBB",
                    9,          # bLength
                    2,          # bDescriptorType = Configuration
                    total_len,  # wTotalLength
                    2,          # bNumInterfaces (HID + WinUSB)
                    1,          # bConfigurationValue
                    0,          # iConfiguration
                    0xA0,       # bmAttributes
                    250         # bMaxPower
                )
                desc = cfg + interfaces
                return desc[:length]
                
            elif desc_type == 0x03: # String Descriptor
                if desc_idx == 0: # Supported Languages
                    desc = struct.pack("<BBH", 4, 3, 0x0409)
                elif desc_idx == 1: # iManufacturer
                    s = "Nintendo Co., Ltd.".encode("utf-16le")
                    desc = struct.pack("<BB", 2 + len(s), 3) + s
                elif desc_idx == 2: # iProduct
                    if self.device_type == "Pro":
                        name = "Pro Controller"
                    else:
                        name = "Joy-Con (L)" if self.device_type == "L" else "Joy-Con (R)"
                    s = name.encode("utf-16le")
                    desc = struct.pack("<BB", 2 + len(s), 3) + s
                elif desc_idx == 3: # iSerialNumber
                    serial_str = f"PRO_{self.bus_id}" if self.device_type == "Pro" else f"JOYCON_{self.device_type}_{self.bus_id}"
                    s = serial_str.encode("utf-16le")
                    desc = struct.pack("<BB", 2 + len(s), 3) + s
                elif desc_idx == 0xEE: # MSFT100 OS String Descriptor (enables MS OS Descriptors)
                    # Must be exactly 18 bytes
                    s = "MSFT100".encode("utf-16le")  # 14 bytes
                    desc = struct.pack("<BB", 18, 3) + s + bytes([0xcd, 0x00])  # bVendorCode=0xcd, bPad=0
                    logger.info("  -> Joy-Con MSFT100 OS String Descriptor")
                else:
                    desc = b""
                return desc[:length]
            
            elif desc_type == 0x22: # HID Report Descriptor
                if self.device_type == "Pro":
                    return SWITCH1_PRO_REPORT_DESCRIPTOR[:length]
                else:
                    return JOYCON_REPORT_DESC[:length]
        
        if req_type == 0x81 and request == 0x06:
            desc_type = value >> 8
            if desc_type == 0x22: # HID Report Descriptor
                return JOYCON_REPORT_DESC[:length]
                
        if request == 0x00 and req_type in (0x80, 0x81, 0x82):
            return struct.pack("<H", 0x0001)[:length]
            
        if req_type in (0x00, 0x01, 0x21) and request in (0x09, 0x0a, 0x0b):
            return b""

        # Vendor-specific MS OS Descriptors (device-to-host) — enables WinUSB for Interface 1
        if (req_type & 0x40) == 0x40 and request == 0xcd:
            if index == 0x0004: # Extended Compat ID Descriptor -> WINUSB for Interface 1
                header  = struct.pack("<IHHB7s", 40, 0x0100, 4, 1, b"\x00" * 7)
                section = struct.pack("<BB8s8s6s", 1, 1, b"WINUSB\x00\x00", b"\x00" * 8, b"\x00" * 6)
                result  = (header + section)[:length]
                logger.info(f"  -> Joy-Con MS OS ExtCompatID Descriptor ({len(result)} bytes)")
                return result
            elif index == 0x0005: # Extended Properties Descriptor -> DeviceInterfaceGUID
                prop_name = "DeviceInterfaceGUID".encode("utf-16le") + b"\x00\x00"
                prop_data = "{6F13725E-EF0E-4FD3-AE5F-B2DE989EC825}".encode("utf-16le") + b"\x00\x00"
                prop_size  = 4 + 4 + 2 + len(prop_name) + 4 + len(prop_data)
                total_size = 10 + prop_size
                header = struct.pack("<IHHH", total_size, 0x0100, 5, 1)
                prop   = struct.pack("<IIH", prop_size, 1, len(prop_name)) + prop_name + struct.pack("<I", len(prop_data)) + prop_data
                result = (header + prop)[:length]
                logger.info(f"  -> Joy-Con MS OS ExtProperties Descriptor ({len(result)} bytes)")
                return result

        logger.warning(f"Joy-Con Control Request UNHANDLED: req_type={req_type:#04x}, req={request:#04x}")
        return b""

    def _handle_bulk_command(self, cmd):
        """Override: simple ACK for all WinUSB Bulk OUT commands on Interface 1.
        Joy-Con does not use the Switch2 Bulk protocol — return a generic ACK.
        """
        logger.info(f"Joy-Con Bulk CMD: {cmd[:min(len(cmd), 16)].hex()}")
        reply = bytearray(8)
        if len(cmd) > 0: reply[0] = cmd[0]
        reply[1] = 0x01
        if len(cmd) > 2: reply[2] = cmd[2]
        if len(cmd) > 3: reply[3] = cmd[3]
        if len(cmd) > 4: reply[4] = cmd[4]
        reply[5] = 0xf8
        self.bulk_response_queue.put(bytes(reply))

    def _build_subcommand_reply(self, subcmd, ack=0x80, reply_data=None):
        r = bytearray(50)
        r[0] = 0x21
        self._timer = (self._timer + 1) & 0xFF
        r[1] = self._timer
        r[2] = 0x9E if self.device_type == "L" else 0x8E
        
        # Buttons
        r[3] = 0x00; r[4] = 0x00; r[5] = 0x00
        
        # Sticks neutral calibration
        r[6] = 0x00; r[7] = 0x08; r[8] = 0x80
        r[9] = 0x00; r[10] = 0x08; r[11] = 0x80
        
        r[12] = 0x00
        r[13] = ack
        r[14] = subcmd
        if reply_data:
            for i, b in enumerate(reply_data):
                if 15 + i < 50:
                    r[15 + i] = b
        return bytes(r)

    def _handle_output_report(self, data):
        logger.info(f"Joy-Con Output Report: len={len(data)}, data={data.hex()}")
        if len(data) < 2:
            return
        report_id = data[0]
        if report_id == 0x01 and len(data) >= 11:
            if self.on_rumble_callback:
                self.on_rumble_callback(data)
            subcmd = data[10]
            self._dispatch_subcommand(subcmd, data)
        elif report_id == 0x10:
            if self.on_rumble_callback:
                self.on_rumble_callback(data)

    def _dispatch_subcommand(self, subcmd, data):
        reply = None
        if subcmd == 0x00:
            reply = self._build_subcommand_reply(0x00)
        elif subcmd == 0x01:
            reply = self._build_subcommand_reply(0x01)
        elif subcmd == 0x02:
            self._device_info_queried = True
            
            if self.device_type == "L":
                dev_byte = 0x01
            elif self.device_type == "R":
                dev_byte = 0x02
            else: # Pro
                dev_byte = 0x03
                
            info = [
                0x03, 0x8B,
                dev_byte,
                0x02,
                0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
                0x01,
                0x01,
            ]
            if self.mac_bytes:
                info[4:10] = self.mac_bytes[::-1]
            reply = self._build_subcommand_reply(0x02, ack=0x82, reply_data=info)
        elif subcmd == 0x03:
            reply = self._build_subcommand_reply(0x03)
        elif subcmd == 0x04:
            reply = self._build_subcommand_reply(0x04, ack=0x83)
        elif subcmd == 0x08:
            reply = self._build_subcommand_reply(0x08)
        elif subcmd == 0x10:
            reply = self._handle_spi_read(data)
        elif subcmd == 0x21:
            nfc_reply = [0x01, 0x00, 0xFF, 0x00, 0x08, 0x00, 0x1B, 0x01]
            r = self._build_subcommand_reply(0x21, ack=0xA0, reply_data=nfc_reply)
            r[49] = 0xC8
            reply = r
        elif subcmd == 0x22:
            reply = self._build_subcommand_reply(0x22)
        elif subcmd == 0x30:
            reply = self._build_subcommand_reply(0x30)
        elif subcmd == 0x40:
            self._imu_enabled = (len(data) > 11 and data[11] == 0x01)
            reply = self._build_subcommand_reply(0x40)
        elif subcmd == 0x41:
            reply = self._build_subcommand_reply(0x41)
        elif subcmd == 0x42:
            reply = self._build_subcommand_reply(0x42)
        elif subcmd == 0x43:
            reply = self._build_subcommand_reply(0x43)
        elif subcmd == 0x48:
            self._vibration_enabled = (len(data) > 11 and data[11] == 0x01)
            reply = self._build_subcommand_reply(0x48, ack=0x82)
        else:
            reply = self._build_subcommand_reply(subcmd)

        if reply:
            logger.info(f"Joy-Con Subcommand REPLY: subcmd={subcmd:#04x}, data={reply.hex()}")
            if self.subcmd_reply_queue.full():
                try: self.subcmd_reply_queue.get_nowait()
                except queue.Empty: pass
            try: self.subcmd_reply_queue.put_nowait(reply)
            except queue.Full: pass

    def _handle_spi_read(self, data):
        if len(data) < 16:
            return self._build_subcommand_reply(0x10, ack=0x90)
        addr = data[11] | (data[12] << 8)
        length = data[15]
        reply_data = [data[11], data[12], 0x00, 0x00, length]
        spi = [0xFF] * length

        # Factory stick calibration (9 bytes, 3x 12-bit pairs packed little-endian):
        #   [max_offset_x/y][center_x/y][min_offset_x/y]
        # center = 2048 (0x800) → [0x00, 0x08, 0x80]  matches float_to_12bit(0.0)
        # max_offset = min_offset = 816 (0x330) → [0x30, 0x03, 0x33]  symmetric, realistic Joy-Con value
        # Verify: 0x30 | ((0x03 & 0x0F) << 8) = 0x330 = 816 ✓
        #         (0x03 >> 4) | (0x33 << 4) = 0x330 = 816 ✓
        stick_calib = [
            0x30, 0x03, 0x33,  # max offset (816, 816)
            0x00, 0x08, 0x80,  # center     (2048, 2048)
            0x30, 0x03, 0x33,  # min offset (816, 816)
        ]
        stick_params = [
            0x0F, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
            0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00
        ]

        if addr == 0x6050:
            spi = ([0x32, 0x32, 0x32, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF])[:length]
        elif addr == 0x603D:
            spi = (stick_calib + [0xFF]*20)[:length]
        elif addr == 0x6046:
            spi = (stick_calib + [0xFF]*20)[:length]
        elif addr == 0x6080:
            val = 0xF1 if self.device_type == "L" else (0x0F if self.device_type == "R" else 0x00)
            sensor_calib = [0x5E, 0x01, 0x00, 0x00, val, 0x0F if self.device_type == "L" else (0xF0 if self.device_type == "R" else 0x00)]
            spi = (sensor_calib + stick_params + stick_params + [0xFF]*20)[:length]
        elif addr == 0x6086:
            spi = (stick_params + [0xFF]*20)[:length]
        elif addr == 0x6098:
            spi = (stick_params + [0xFF]*20)[:length]
        elif addr == 0x8010:
            # Return invalid magic (0xFF 0xFF) → Eden/Yuzu fall back to factory calibration (0x603D).
            # Previously returned 0xB2 0xA1 (valid) with factory-format data, but User Cal uses
            # a different field order (Center first) vs Factory Cal (Max Offset first), causing
            # Eden to compute center=(1792,1792) instead of (2048,2048) → diagonal lock bug.
            spi = ([0xFF] * length)
        elif addr == 0x801B:
            # Same fix as 0x8010 for the right stick user calibration.
            spi = ([0xFF] * length)
        elif addr == 0x6020:
            spi = ([0xD3, 0xFF, 0xD5, 0xFF, 0x55, 0x01,
                    0x00, 0x40, 0x00, 0x40, 0x00, 0x40,
                    0x19, 0x00, 0xDD, 0xFF, 0xDC, 0xFF,
                    0x3B, 0x34, 0x3B, 0x34, 0x3B, 0x34])[:length]

        reply_data += spi
        return self._build_subcommand_reply(0x10, ack=0x90, reply_data=reply_data)


class USBIPJoyConLServer(USBIPJoyConServer):
    def __init__(self, host="127.0.0.1", port=3240, on_rumble_callback=None, bus_id="1-1", mac_address=None):
        super().__init__(device_type="L", host=host, port=port, on_rumble_callback=on_rumble_callback, bus_id=bus_id, mac_address=mac_address)


class USBIPJoyConRServer(USBIPJoyConServer):
    def __init__(self, host="127.0.0.1", port=3240, on_rumble_callback=None, bus_id="1-1", mac_address=None):
        super().__init__(device_type="R", host=host, port=port, on_rumble_callback=on_rumble_callback, bus_id=bus_id, mac_address=mac_address)


class USBIPProControllerServer(USBIPJoyConServer):
    def __init__(self, host="127.0.0.1", port=3240, on_rumble_callback=None, bus_id="1-1", mac_address=None):
        super().__init__(device_type="Pro", host=host, port=port, on_rumble_callback=on_rumble_callback, bus_id=bus_id, mac_address=mac_address)
        self.product_id = 0x2009

