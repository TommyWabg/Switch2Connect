import socket
import threading
import struct
import time
import logging
from dataclasses import dataclass
from zlib import crc32

from config import CONFIG

logger = logging.getLogger(__name__)

MAX_PROTOCOL_VERSION = 1001

class MessageType:
    DSUC_VersionReq = 0x100000
    DSUS_VersionRsp = 0x100000
    DSUC_ListPorts = 0x100001
    DSUS_PortInfo = 0x100001
    DSUC_PadDataReq = 0x100002
    DSUS_PadDataRsp = 0x100002

class ClientRequestTimes:
    def __init__(self):
        self.all_pads = 0.0
        self.pad_ids = [0.0] * 4
        self.pad_macs = {}

    def request_pad_info(self, reg_flags: int, id_to_reg: int, mac_to_reg: bytes):
        now = time.time()
        if reg_flags == 0:
            self.all_pads = now
        else:
            if reg_flags & 0x01:  # id valid
                if id_to_reg < len(self.pad_ids):
                    self.pad_ids[id_to_reg] = now
            if reg_flags & 0x02:  # mac valid
                self.pad_macs[mac_to_reg] = now

class CemuHookUDPServer:
    def __init__(self, host='0.0.0.0', port=26760):
        self.host = host
        self.port = port
        self.sock = None
        self.server_id = 12345678  # arbitrary 32-bit int
        self.running = False
        self.thread = None
        self.clients = {}  # (ip, port) -> ClientRequestTimes
        self.packet_counter = 0
        self.active_pads = {}
        self.mac_to_pad_id = {bytes.fromhex(k): v for k, v in getattr(CONFIG, 'cemuhook_mac_to_pad', {}).items()}

    def start(self):
        if self.running:
            return
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.bind((self.host, self.port))
            # Set non-blocking or a timeout so we can exit cleanly
            self.sock.settimeout(1.0)
            self.running = True
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()
            logger.info(f"CemuHook UDP server started on {self.host}:{self.port}")
        except Exception as e:
            logger.error(f"Failed to start CemuHook UDP server: {e}")
            self.running = False

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=2.0)
        if self.sock:
            self.sock.close()
            self.sock = None
        logger.info("CemuHook UDP server stopped.")

    def _run(self):
        while self.running:
            try:
                data, addr = self.sock.recvfrom(1024)
                self._process_incoming(data, addr)
            except socket.timeout:
                continue
            except Exception as e:
                if self.running:
                    logger.error(f"UDP server recv error: {e}")

    def _begin_packet(self, req_protocol_version=MAX_PROTOCOL_VERSION) -> bytearray:
        # Header: Magic "DSUS" (4), Protocol version (2), Packet size (2), CRC32 (4), Server ID (4)
        packet = bytearray(16)
        packet[0:4] = b'DSUS'
        struct.pack_into('<H', packet, 4, req_protocol_version)
        struct.pack_into('<I', packet, 12, self.server_id)
        return packet

    def _finish_packet(self, packet: bytearray):
        struct.pack_into('<H', packet, 6, len(packet) - 16)
        # CRC32 is calculated with the CRC field zeroed out
        struct.pack_into('<I', packet, 8, 0)
        crc_calc = crc32(packet) & 0xFFFFFFFF
        struct.pack_into('<I', packet, 8, crc_calc)

    def _send_packet(self, client_addr, useful_data: bytes):
        packet = self._begin_packet()
        packet.extend(useful_data)
        self._finish_packet(packet)
        try:
            self.sock.sendto(packet, client_addr)
        except Exception:
            pass

    def _process_incoming(self, data: bytes, addr):
        if len(data) < 16:
            return
        if data[0:4] != b'DSUC':
            return
        
        protocol_ver = struct.unpack_from('<H', data, 4)[0]
        if protocol_ver > MAX_PROTOCOL_VERSION:
            return
        
        packet_size = struct.unpack_from('<H', data, 6)[0]
        if len(data) < packet_size + 16:
            return
            
        # Verify CRC
        packet = bytearray(data[:packet_size + 16])
        crc_value = struct.unpack_from('<I', packet, 8)[0]
        struct.pack_into('<I', packet, 8, 0)
        crc_calc = crc32(packet) & 0xFFFFFFFF
        if crc_value != crc_calc:
            return

        message_type = struct.unpack_from('<I', packet, 16)[0]
        
        if message_type == MessageType.DSUC_VersionReq:
            output = bytearray(8)
            struct.pack_into('<I', output, 0, MessageType.DSUS_VersionRsp)
            struct.pack_into('<H', output, 4, MAX_PROTOCOL_VERSION)
            self._send_packet(addr, output)
            
        elif message_type == MessageType.DSUC_ListPorts:
            num_requests = struct.unpack_from('<i', packet, 20)[0]
            if num_requests < 0 or num_requests > 4:
                return
            
            now = time.time()
            self.active_pads = {k: v for k, v in self.active_pads.items() if now - v['time'] < 5.0}
            
            for i in range(num_requests):
                output = bytearray(16)
                struct.pack_into('<I', output, 0, MessageType.DSUS_PortInfo)
                
                if i in self.active_pads:
                    pad = self.active_pads[i]
                    output[4] = i # pad id
                    output[5] = 2 # connected
                    output[6] = pad['model']
                    output[7] = 2 # bluetooth
                    output[8:14] = pad['mac'][:6].ljust(6, b'\x00')
                    output[14] = pad['battery']
                    output[15] = 0 
                else:
                    output[4] = i
                    output[5] = 0 # disconnected
                    output[6] = 0
                    output[7] = 0
                    output[8:14] = b'\x00' * 6
                    output[14] = 0
                    output[15] = 0
                    
                self._send_packet(addr, output)
            
        elif message_type == MessageType.DSUC_PadDataReq:
            reg_flags = packet[20]
            id_to_reg = packet[21]
            mac_to_reg = bytes(packet[22:28])
            
            if addr not in self.clients:
                self.clients[addr] = ClientRequestTimes()
            self.clients[addr].request_pad_info(reg_flags, id_to_reg, mac_to_reg)

    def report_controller_data(self, model: int, mac_address: bytes, battery_level: int, input_data, ds4_accel, ds4_gyro):
        if not self.running:
            return
            
        # Get or assign a persistent pad_id based on MAC address
        if mac_address in self.mac_to_pad_id:
            pad_id = self.mac_to_pad_id[mac_address]
        else:
            used_pads = set(self.mac_to_pad_id.values())
            available_pads = [i for i in range(4) if i not in used_pads]
            if available_pads:
                pad_id = min(available_pads)
            else:
                # All 4 pads are taken. Overwrite using the overwrite index.
                pad_id = getattr(CONFIG, "cemuhook_pad_overwrite_idx", 0)
                # Find the old MAC address that has this pad_id and remove it
                old_mac = None
                for mac, pid in self.mac_to_pad_id.items():
                    if pid == pad_id:
                        old_mac = mac
                        break
                if old_mac:
                    del self.mac_to_pad_id[old_mac]
                
                # Increment the overwrite index
                CONFIG.cemuhook_pad_overwrite_idx = (pad_id + 1) % 4
            
            self.mac_to_pad_id[mac_address] = pad_id
            
            # Save to CONFIG
            CONFIG.cemuhook_mac_to_pad = {k.hex().upper(): v for k, v in self.mac_to_pad_id.items()}
            CONFIG.save_config()

        now = time.time()
        timeout_limit = 5.0
        
        active_clients = []
        clients_to_delete = []
        
        for addr, client in self.clients.items():
            if (now - client.all_pads) < timeout_limit:
                active_clients.append(addr)
            elif (0 <= pad_id <= 3) and (now - client.pad_ids[pad_id]) < timeout_limit:
                active_clients.append(addr)
            elif mac_address in client.pad_macs and (now - client.pad_macs[mac_address]) < timeout_limit:
                active_clients.append(addr)
            else:
                # Check if client is entirely dead
                client_ok = False
                for pid_time in client.pad_ids:
                    if (now - pid_time) < timeout_limit:
                        client_ok = True
                        break
                if not client_ok:
                    for m_time in client.pad_macs.values():
                        if (now - m_time) < timeout_limit:
                            client_ok = True
                            break
                if not client_ok:
                    clients_to_delete.append(addr)
                    
        for addr in clients_to_delete:
            del self.clients[addr]
            
        if not active_clients:
            return
            
        self.active_pads[pad_id] = {
            'model': model,
            'mac': mac_address,
            'battery': battery_level,
            'time': now
        }
            
        self.packet_counter += 1
        
        # Build DSUS_PadDataRsp payload
        output = bytearray()
        output.extend(struct.pack('<I', MessageType.DSUS_PadDataRsp))
        output.append(pad_id) # pad id
        output.append(2) # state: 2 = connected
        output.append(model) # model: 2 = DS4, 3 = Joycon
        output.append(2) # connection type: 2 = bluetooth
        output.extend(mac_address[:6].ljust(6, b'\x00'))
        output.append(battery_level) # battery
        output.append(1) # is_active
        output.extend(struct.pack('<I', self.packet_counter))
        
        # Buttons (2 bytes), PS button, Touchpad button
        # Map input_data.buttons to Cemuhook layout (DS4 mapped)
        btn1 = 0
        btn2 = 0
        
        # Cemuhook button mapping (DS4):
        # btn1: 
        # 0x80 D-Pad West
        # 0x40 D-Pad South
        # 0x20 D-Pad East
        # 0x10 D-Pad North
        # 0x08 Options (Plus)
        # 0x04 R3
        # 0x02 L3
        # 0x01 Share (Minus)
        
        # btn2:
        # 0x80 Square (Y)
        # 0x40 Cross (B)
        # 0x20 Circle (A)
        # 0x10 Triangle (X)
        # 0x08 R1 (R)
        # 0x04 L1 (L)
        # 0x02 R2 (ZR)
        # 0x01 L2 (ZL)
        
        # Mapping from Switch Buttons
        if input_data.buttons & 0x00080000: btn1 |= 0x80 # Left
        if input_data.buttons & 0x00010000: btn1 |= 0x40 # Down
        if input_data.buttons & 0x00040000: btn1 |= 0x20 # Right
        if input_data.buttons & 0x00020000: btn1 |= 0x10 # Up
        if input_data.buttons & 0x00000200: btn1 |= 0x08 # Plus
        if input_data.buttons & 0x00000400: btn1 |= 0x04 # R3
        if input_data.buttons & 0x00000800: btn1 |= 0x02 # L3
        if input_data.buttons & 0x00000100: btn1 |= 0x01 # Minus
        
        if input_data.buttons & 0x00000001: btn2 |= 0x80 # Y -> Square
        if input_data.buttons & 0x00000004: btn2 |= 0x40 # B -> Cross
        if input_data.buttons & 0x00000008: btn2 |= 0x20 # A -> Circle
        if input_data.buttons & 0x00000002: btn2 |= 0x10 # X -> Triangle
        if input_data.buttons & 0x00000040: btn2 |= 0x08 # R -> R1
        if input_data.buttons & 0x00400000: btn2 |= 0x04 # L -> L1
        if input_data.buttons & 0x00000080: btn2 |= 0x02 # ZR -> R2
        if input_data.buttons & 0x00800000: btn2 |= 0x01 # ZL -> L2
        
        output.append(btn1)
        output.append(btn2)
        
        output.append(1 if input_data.buttons & 0x00001000 else 0) # PS button (Home)
        output.append(1 if input_data.buttons & 0x00002000 else 0) # Touchpad (Capture)
        
        # Stick X/Y (0-255)
        # input_data.left_stick, right_stick are 0-4095
        lx = min(255, max(0, int(input_data.left_stick[0] / 4095.0 * 255.0)))
        ly = min(255, max(0, int((4095 - input_data.left_stick[1]) / 4095.0 * 255.0)))
        rx = min(255, max(0, int(input_data.right_stick[0] / 4095.0 * 255.0)))
        ry = min(255, max(0, int((4095 - input_data.right_stick[1]) / 4095.0 * 255.0)))
        
        output.extend([lx, ly, rx, ry])
        
        # Analog buttons (12 bytes) Dpad L,D,R,U, Sq,Cr,Ci,Tr, R1,L1, R2,L2
        output.extend([0] * 10) # 10 dummy analog buttons for 0/255
        
        zl_analog = 255 if (input_data.buttons & 0x00800000) else 0
        zr_analog = 255 if (input_data.buttons & 0x00000080) else 0
        output.append(zr_analog)
        output.append(zl_analog)
        
        # Touchpad data: 1 byte (active), 6 byte touch 1, 6 byte touch 2
        # BetterJoy: outIdx++; for(int i=0; i<2; i++) { outIdx += 6; } -> 12 bytes
        output.extend(b'\x00' * 12)
        
        # Motion Timestamp (uint64)
        output.extend(struct.pack('<Q', int(time.time() * 1000000)))
        
        # Accel in Gs. 1G = 4096.0
        acc_x = ds4_accel[0] / 4096.0
        acc_y = ds4_accel[1] / 4096.0
        acc_z = ds4_accel[2] / 4096.0
        
        # Cemuhook expects X, Y, Z for Accel
        output.extend(struct.pack('<f', acc_x))
        output.extend(struct.pack('<f', acc_y))
        output.extend(struct.pack('<f', acc_z))
        
        # Gyro in deg/s. 
        # 根據實測反推的完美經驗值倍率：
        # Joy-Con: 0.0535 (修正了前一版 0.0523 導致的 350度 些微不足)
        # Pro Controller: 0.061
        gyr_multiplier = 0.0535 if model == 3 else 0.061
        gyr_pitch = ds4_gyro[0] * gyr_multiplier
        gyr_yaw   = ds4_gyro[1] * gyr_multiplier
        gyr_roll  = ds4_gyro[2] * gyr_multiplier
        
        # Cemuhook expects Pitch, Yaw, Roll
        output.extend(struct.pack('<f', gyr_pitch))
        output.extend(struct.pack('<f', gyr_yaw))
        output.extend(struct.pack('<f', gyr_roll))
        
        for addr in active_clients:
            self._send_packet(addr, output)

# Global Instance
cemuhook_server = CemuHookUDPServer()
