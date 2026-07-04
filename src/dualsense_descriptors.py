# src/dualsense_descriptors.py
#
# Virtual DualSense USB descriptors.
#
# These reproduce a *genuine* Sony DualSense (CFI-ZCT1) USB identity, ported from
# the DS5_Bridge reference firmware (reference/DS5_Bridge-main/src/usb_descriptors.c),
# so that games with strict DualSense format/device requirements accept the device
# and deliver audio-based haptics.  Key points versus the older descriptor set:
#
#   * Full 289-byte DualSense HID report descriptor (the real one) instead of the
#     genuine-273-padded-to-337 variant.  This restores feature report IDs 0x0B/0x0C
#     and removes the 64 bytes of dummy Vendor Collection padding, so titles that
#     parse the report descriptor to validate the pad and locate feature reports see
#     an exact DualSense.
#   * Complete full-duplex USB Audio topology: speaker OUT (4-ch, ch2/3 = haptics)
#     AND headset microphone IN — matching a real DualSense's 4-interface layout
#     (Audio Control, Audio Streaming OUT, Audio Streaming IN, HID).  The previous
#     descriptor advertised the speaker only.
#   * Haptic OUT endpoint is Isochronous *Adaptive* (0x09), the sink type a real
#     DualSense uses for the host-clocked haptic stream (was Synchronous 0x0D).
#   * No USB serial number (iSerialNumber = 0), matching the reference.  A PlayStation
#     HID serial makes strict tools (e.g. SpecialK) treat the pad as a *Bluetooth*
#     DualSense and expect BT-format reports, breaking haptics.
#
# The total configuration length is 227 bytes and restores the complete DS5_Bridge
# audio function: speaker OUT (EP 0x01) AND headset microphone IN (EP 0x82), with the
# full 6-terminal Audio Control topology.  (An earlier build shipped a hand-trimmed
# speaker-only variant; that left the AC header length off-by-one and dangling
# bAssocTerminal references, and dropped the mic.  This is byte-faithful to the
# reference instead.)

# Device Descriptor
# bcdUSB: 0x0200
# bDeviceClass/SubClass/Protocol: 0x00 (composite; class declared per-interface)
# bMaxPacketSize0: 64
# idVendor: 0x054C (Sony), idProduct: 0x0CE6 (DualSense)
# bcdDevice: 0x0100
# iManufacturer: 1, iProduct: 2, iSerialNumber: 0 (no serial — see note above)
# bNumConfigurations: 1
DUALSENSE_DEVICE_DESCRIPTOR = bytes.fromhex(
    "12 01 00 02 00 00 00 40 4C 05 E6 0C 00 01 01 02"
    "00 01"
)

# Configuration Descriptor (227 bytes total / 0x00E3, 4 interfaces):
#  - Interface 0:          Audio Control (UAC1) — full-duplex topology
#      IT1 (USB streaming, 4-ch) -> FU2 -> OT3 (Speaker)
#      IT4 (Headset Mic, 1-ch)   -> FU5 -> OT6 (USB streaming)
#  - Interface 1 (alt 0/1): Audio Streaming OUT (4-ch/16-bit/48kHz, iso EP 0x01, Adaptive)
#  - Interface 2 (alt 0/1): Audio Streaming IN  (1-ch/16-bit/48kHz, iso EP 0x82, Async)
#  - Interface 3:          HID (DualSense, EP 0x84 IN, EP 0x03 OUT)
# Byte-faithful to reference/DS5_Bridge-main/src/usb_descriptors.c (audio function).
DUALSENSE_CONFIGURATION_DESCRIPTOR = bytes.fromhex(
    # --- Configuration header (wTotalLength 0x00E3 = 227, 4 interfaces) ---
    "09 02 E3 00 04 01 00 C0 FA"
    # --- Interface 0: Audio Control ---
    # iInterface = 4 ("Wireless Controller Audio").  The reference zeroes audio
    # iInterface strings, but that text is cosmetic (games never fingerprint it).
    # Keep the name stable so Windows does not rename the playback endpoint.
    "09 04 00 00 00 01 01 00 04"
    # AC Interface Header (wTotalLength = 73 / 0x49, 2 streaming interfaces: 1, 2)
    "0A 24 01 00 01 49 00 02 01 02"
    # Input Terminal 1: USB Streaming, 4 channels (L/R front + L/R surround)
    "0C 24 02 01 01 01 06 04 33 00 00 00"
    # Feature Unit 2 (<- Terminal 1)
    "0C 24 06 02 01 01 03 00 00 00 00 00"
    # Output Terminal 3: Speaker (<- Unit 2)
    "09 24 03 03 01 03 04 02 00"
    # Input Terminal 4: Headset Mic, 1 channel
    "0C 24 02 04 02 04 03 01 00 00 00 00"
    # Feature Unit 5 (<- Terminal 4)
    "09 24 06 05 04 01 03 00 00"
    # Output Terminal 6: USB Streaming (<- Unit 5)
    "09 24 03 06 01 01 01 05 00"
    # --- Interface 1.0: Audio Streaming OUT (zero-bandwidth alt) ---
    "09 04 01 00 00 01 02 00 04"
    # --- Interface 1.1: Audio Streaming OUT (active alt) ---
    "09 04 01 01 01 01 02 00 04"
    # AS General (linked to Terminal 1)
    "07 24 01 01 01 01 00"
    # Format Type I: 4-ch, 2 bytes/sample, 16-bit, 48000 Hz
    "0B 24 02 01 04 02 10 01 80 BB 00"
    # Endpoint 0x01 OUT: Isochronous Adaptive, wMaxPacketSize 392 (0x0188), bInterval 1 (1ms at Full Speed)
    "09 05 01 09 88 01 01 00 00"
    # CS Audio Streaming Endpoint
    "07 25 01 00 00 00 00"
    # --- Interface 2.0: Audio Streaming IN (zero-bandwidth alt) ---
    "09 04 02 00 00 01 02 00 00"
    # --- Interface 2.1: Audio Streaming IN (active alt) ---
    "09 04 02 01 01 01 02 00 00"
    # AS General (linked to Terminal 6)
    "07 24 01 06 01 01 00"
    # Format Type I: 1-ch, 2 bytes/sample, 16-bit, 48000 Hz
    "0B 24 02 01 01 02 10 01 80 BB 00"
    # Endpoint 0x82 IN: Isochronous Async, wMaxPacketSize 98 (0x0062), bInterval 1
    "09 05 82 05 62 00 01 00 00"
    # CS Audio Streaming Endpoint
    "07 25 01 00 00 00 00"
    # --- Interface 3: HID (DualSense gamepad) ---
    "09 04 03 00 02 03 00 00 00"
    # HID descriptor (bcdHID 1.11, report descriptor length 289 / 0x0121)
    "09 21 11 01 00 01 22 21 01"
    # Endpoint 0x84 IN: Interrupt, 64 bytes, bInterval 1
    "07 05 84 03 40 00 01"
    # Endpoint 0x03 OUT: Interrupt, 64 bytes, bInterval 1
    "07 05 03 03 40 00 01"
)

# Genuine 289-byte DualSense HID report descriptor (ported verbatim from the
# DS5_Bridge reference, desc_hid_report_ds).  Report ID 0x01 input layout is
# byte-identical to the old descriptor, so DualSenseInputReport01 is unchanged;
# the differences are the restored feature reports 0x0B/0x0C and the removal of
# the old dummy padding.
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
    "85 0B 09 41 95 29 B1 02 85 0C 09 42 95 29 B1 02"
    "85 20 09 26 95 3F B1 02 85 21 09 27 95 04 B1 02"
    "85 22 09 40 95 3F B1 02 85 80 09 28 95 3F B1 02"
    "85 81 09 29 95 3F B1 02 85 82 09 2A 95 09 B1 02"
    "85 83 09 2B 95 3F B1 02 85 84 09 2C 95 3F B1 02"
    "85 85 09 2D 95 02 B1 02 85 A0 09 2E 95 01 B1 02"
    "85 E0 09 2F 95 3F B1 02 85 F0 09 30 95 3F B1 02"
    "85 F1 09 31 95 3F B1 02 85 F2 09 32 95 0F B1 02"
    "85 F4 09 35 95 3F B1 02 85 F5 09 36 95 03 B1 02"
    "C0"
)

# String Descriptors
DUALSENSE_STRING_LANG = bytes.fromhex("04 03 09 04") # 0x0409 English (US)

def build_string_descriptor(s):
    encoded = s.encode("utf-16le")
    return bytes([len(encoded) + 2, 3]) + encoded

DUALSENSE_STRING_MANUFACTURER = build_string_descriptor("Sony Interactive Entertainment")
DUALSENSE_STRING_PRODUCT = build_string_descriptor("DualSense Wireless Controller")
# Kept for compatibility; the audio/HID interfaces expose no iInterface string
# (matching a real DualSense), so these are no longer referenced by the config.
DUALSENSE_STRING_AUDIO = build_string_descriptor("Wireless Controller Audio")
DUALSENSE_STRING_HID = build_string_descriptor("Wireless Controller")
