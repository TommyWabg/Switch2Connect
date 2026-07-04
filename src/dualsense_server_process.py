import argparse
import logging
import os
import socket
import struct
import sys
import threading
import time

logger = logging.getLogger(__name__)

MSG_INPUT = 0x01
MSG_STOP = 0x02
MSG_OUTPUT_REPORT = 0x83
MSG_HAPTIC_FRAME = 0x82
MSG_STATUS = 0x84


def _send_datagram(sock, addr, msg_type, payload=b""):
    try:
        sock.sendto(bytes([msg_type]) + payload, addr)
    except Exception:
        logger.debug("DualSense child IPC send failed", exc_info=True)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--bus-id", required=True)
    parser.add_argument("--mac-address", default="")
    parser.add_argument("--ctrl-port", type=int, required=True)
    parser.add_argument("--parent-port", type=int, required=True)
    args = parser.parse_args(argv)

    log_handlers = [logging.StreamHandler()]
    try:
        log_dir = os.path.join(
            os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
            "Switch2Controllers",
        )
        os.makedirs(log_dir, exist_ok=True)
        log_handlers.append(
            logging.FileHandler(
                os.path.join(log_dir, "dualsense_server.log"), encoding="utf-8"
            )
        )
    except Exception:
        pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s.%(msecs)03d %(levelname)s:%(name)s:%(message)s",
        datefmt="%H:%M:%S",
        handlers=log_handlers,
    )
    logger.info("DualSense server child starting (frozen=%s, pid=%s)", getattr(sys, "frozen", False), os.getpid())
    from usbip_dualsense_server import USBIPDualSenseServer
    from dualsense_haptic import DualSenseHapticProcessor

    ctrl_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    ctrl_sock.bind(("127.0.0.1", args.ctrl_port))
    ctrl_sock.settimeout(0.1)
    parent_addr = ("127.0.0.1", args.parent_port)
    stop_event = threading.Event()

    def on_haptic_frame(left_intensity, right_intensity, mode="CONTINUOUS", spectral=None):
        spectral = spectral or {}
        mode_code = 1 if mode == "SPECTRAL" else (2 if mode == "SILENCE" else 0)
        payload = struct.pack(
            "!BhhHHHHHHHH",
            mode_code,
            int(left_intensity),
            int(right_intensity),
            int(spectral.get("left_lf_freq", 0)),
            int(spectral.get("left_lf_amp", 0)),
            int(spectral.get("left_hf_freq", 0)),
            int(spectral.get("left_hf_amp", 0)),
            int(spectral.get("right_lf_freq", 0)),
            int(spectral.get("right_lf_amp", 0)),
            int(spectral.get("right_hf_freq", 0)),
            int(spectral.get("right_hf_amp", 0)),
        )
        _send_datagram(ctrl_sock, parent_addr, MSG_HAPTIC_FRAME, payload)

    haptic_processor = DualSenseHapticProcessor(on_haptic_frame)

    def on_output_report(data):
        if data:
            _send_datagram(ctrl_sock, parent_addr, MSG_OUTPUT_REPORT, bytes(data))

    def on_audio_data(data):
        if data is None:
            haptic_processor.reset()
            on_haptic_frame(0, 0, "SILENCE", {})
            return
        if data:
            haptic_processor.process_audio_packet(bytes(data))

    def on_disconnect():
        _send_datagram(ctrl_sock, parent_addr, MSG_STATUS, b"disconnect")

    server = USBIPDualSenseServer(
        host=args.host,
        port=args.port,
        on_rumble_callback=on_output_report,
        bus_id=args.bus_id,
        mac_address=args.mac_address or None,
        on_audio_data_callback=on_audio_data,
        on_disconnect_callback=on_disconnect,
    )

    try:
        server.start()
        _send_datagram(ctrl_sock, parent_addr, MSG_STATUS, b"started")
        last_heartbeat = time.perf_counter()
        while not stop_event.is_set():
            try:
                packet, _addr = ctrl_sock.recvfrom(2048)
            except socket.timeout:
                now = time.perf_counter()
                if now - last_heartbeat >= 1.0:
                    _send_datagram(ctrl_sock, parent_addr, MSG_STATUS, b"heartbeat")
                    last_heartbeat = now
                continue
            except OSError:
                break
            if not packet:
                continue
            msg_type = packet[0]
            payload = packet[1:]
            if msg_type == MSG_INPUT and len(payload) >= 64:
                server.update_input(payload[:64])
            elif msg_type == MSG_STOP:
                stop_event.set()
    finally:
        try:
            server.stop()
        except Exception:
            pass
        try:
            ctrl_sock.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
