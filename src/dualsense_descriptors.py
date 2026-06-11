# src/dualsense_descriptors.py

# Device Descriptor
# bcdUSB: 0x0200
# bDeviceClass: 0x00, bDeviceSubClass: 0x00, bDeviceProtocol: 0x00
# bMaxPacketSize0: 64
# idVendor: 0x054C, idProduct: 0x0CE6
# bcdDevice: 0x0100
# iManufacturer: 1, iProduct: 2, iSerialNumber: 3
# bNumConfigurations: 1
DUALSENSE_DEVICE_DESCRIPTOR = bytes.fromhex(
    "12 01 00 02 00 00 00 40 4C 05 E6 0C 00 01 01 02"
    "03 01"
)

# Configuration Descriptor
# Total Length: 227 bytes
# 4 Interfaces:
#  - Interface 0: Audio Control (UAC1)
#  - Interface 1 (Alt 0, Alt 1): Audio Streaming (OUT, 4-channel, 16-bit, 48000Hz, Isochronous ep 0x01)
#  - Interface 2: HID (DualSense, ep 0x84 IN, ep 0x03 OUT)
DUALSENSE_CONFIGURATION_DESCRIPTOR = bytes.fromhex(
    "09 02 90 00 03 01 00 C0 FA 09 04 00 00 00 01 01"
    "00 04 09 24 01 00 01 2B 00 01 01 0C 24 02 01 01"
    "01 06 04 33 00 00 00 0C 24 06 02 01 01 03 00 00"
    "00 00 00 09 24 03 03 02 03 04 02 00 09 04 01 00"
    "00 01 02 00 04 09 04 01 01 01 01 02 00 04 07 24"
    "01 01 01 01 00 0B 24 02 01 04 02 10 01 80 BB 00"
    "09 05 01 0D 88 01 01 00 00 07 25 01 00 00 00 00"
    "09 04 02 00 02 03 00 00 05 09 21 11 01 00 01 22"
    "51 01 07 05 84 03 40 00 01 07 05 03 03 40 00 01"
)

# DualSense HID Report Descriptor (337 bytes, Windows expects this length)
# We pad the genuine 273 byte USB descriptor with 64 bytes of valid HID tags (Vendor Collection)
DUALSENSE_HID_REPORT_DESCRIPTOR = bytes.fromhex(
    "05 01 09 05 A1 01 85 01 09 30 09 31 09 32 09 35"
    "09 33 09 34 15 00 26 FF 00 75 08 95 06 81 02 06"
    "00 FF 09 20 95 01 81 02 05 01 09 39 15 00 25 07"
    "35 00 46 3B 01 65 14 75 04 95 01 81 42 65 00 05"
    "09 19 01 29 0F 15 00 25 01 75 01 95 0F 81 02 06"
    "00 FF 09 21 95 0D 81 02 06 00 FF 09 22 15 00 26"
    "FF 00 75 08 95 34 81 02 85 02 09 23 95 2F 91 02"
    "85 05 09 33 95 28 B1 02 85 08 09 34 95 2F B1 02"
    "85 09 09 24 95 13 B1 02 85 0A 09 25 95 1A B1 02"
    "85 20 09 26 95 3F B1 02 85 21 09 27 95 04 B1 02"
    "85 22 09 40 95 3F B1 02 85 80 09 28 95 3F B1 02"
    "85 81 09 29 95 3F B1 02 85 82 09 2A 95 09 B1 02"
    "85 83 09 2B 95 3F B1 02 85 84 09 2C 95 3F B1 02"
    "85 85 09 2D 95 02 B1 02 85 A0 09 2E 95 01 B1 02"
    "85 E0 09 2F 95 3F B1 02 85 F0 09 30 95 3F B1 02"
    "85 F1 09 31 95 3F B1 02 85 F2 09 32 95 0F B1 02"
    "85 F4 09 35 95 3F B1 02 85 F5 09 36 95 03 B1 02"
    # We pad with exactly 64 bytes of valid HID tags INSIDE the main collection
    "85 10 B1 03 85 11 B1 03 85 12 B1 03 85 13 B1 03"
    "85 14 B1 03 85 15 B1 03 85 16 B1 03 85 17 B1 03"
    "85 18 B1 03 85 19 B1 03 85 1A B1 03 85 1B B1 03"
    "85 1C B1 03 85 1D B1 03 85 1E B1 03 85 1F B1 03"
    "C0"
)

# String Descriptors
DUALSENSE_STRING_LANG = bytes.fromhex("04 03 09 04") # 0x0409 English (US)

def build_string_descriptor(s):
    encoded = s.encode("utf-16le")
    return bytes([len(encoded) + 2, 3]) + encoded

DUALSENSE_STRING_MANUFACTURER = build_string_descriptor("Sony Interactive Entertainment")
DUALSENSE_STRING_PRODUCT = build_string_descriptor("DualSense Wireless Controller")
DUALSENSE_STRING_AUDIO = build_string_descriptor("Wireless Controller Audio")
DUALSENSE_STRING_HID = build_string_descriptor("Wireless Controller")
