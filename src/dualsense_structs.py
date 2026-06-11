import ctypes

class DualSenseTouchPoint(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("ContactSeq", ctypes.c_uint8),
        ("XLowPart", ctypes.c_uint8),
        ("XHighPart", ctypes.c_uint8, 4),
        ("YLowPart", ctypes.c_uint8, 4),
        ("YHighPart", ctypes.c_uint8)
    ]

class DualSenseTouchReport(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("TouchPoints", DualSenseTouchPoint * 2),
        ("Timestamp", ctypes.c_uint8)
    ]

class DualSenseInputReport01(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("ReportId", ctypes.c_uint8), # 0x01
        ("LeftStickX", ctypes.c_uint8),
        ("LeftStickY", ctypes.c_uint8),
        ("RightStickX", ctypes.c_uint8),
        ("RightStickY", ctypes.c_uint8),
        ("LeftTrigger", ctypes.c_uint8),
        ("RightTrigger", ctypes.c_uint8),
        ("SequenceNumber", ctypes.c_uint8),
        
        # Byte 8
        ("Hat", ctypes.c_uint8, 4),
        ("ButtonSquare", ctypes.c_uint8, 1),
        ("ButtonCross", ctypes.c_uint8, 1),
        ("ButtonCircle", ctypes.c_uint8, 1),
        ("ButtonTriangle", ctypes.c_uint8, 1),
        
        # Byte 9
        ("ButtonL1", ctypes.c_uint8, 1),
        ("ButtonR1", ctypes.c_uint8, 1),
        ("ButtonL2", ctypes.c_uint8, 1),
        ("ButtonR2", ctypes.c_uint8, 1),
        ("ButtonShare", ctypes.c_uint8, 1),
        ("ButtonOptions", ctypes.c_uint8, 1),
        ("ButtonL3", ctypes.c_uint8, 1),
        ("ButtonR3", ctypes.c_uint8, 1),
        
        # Byte 10
        ("ButtonHome", ctypes.c_uint8, 1),
        ("ButtonTouchpad", ctypes.c_uint8, 1),
        ("ButtonMute", ctypes.c_uint8, 1),
        ("UNK1", ctypes.c_uint8, 1),
        ("ButtonLeftFunction", ctypes.c_uint8, 1),
        ("ButtonRightFunction", ctypes.c_uint8, 1),
        ("ButtonLeftPaddle", ctypes.c_uint8, 1),
        ("ButtonRightPaddle", ctypes.c_uint8, 1),
        
        ("UNK2", ctypes.c_uint8),           # Byte 11
        ("UNK_COUNTER", ctypes.c_uint32),   # Bytes 12,13,14,15 (Wait, struct size is uint32_t which is 4 bytes? offset is 11, Gyro is 15. So 11..14 = 4 bytes)
        
        ("AngularVelocityX", ctypes.c_int16), # Byte 15,16
        ("AngularVelocityZ", ctypes.c_int16), # Byte 17,18
        ("AngularVelocityY", ctypes.c_int16), # Byte 19,20
        ("AccelerometerX", ctypes.c_int16),   # Byte 21,22
        ("AccelerometerY", ctypes.c_int16),   # Byte 23,24
        ("AccelerometerZ", ctypes.c_int16),   # Byte 25,26
        
        ("SensorTimestamp", ctypes.c_uint32), # Byte 27,28,29,30
        ("Temperature", ctypes.c_int8),       # Byte 31
        
        ("TouchReport", DualSenseTouchReport),  # Byte 32..40 (9 bytes)
        
        # Byte 41
        ("TriggerRightStopLocation", ctypes.c_uint8, 4),
        ("TriggerRightStatus", ctypes.c_uint8, 4),
        
        # Byte 42
        ("TriggerLeftStopLocation", ctypes.c_uint8, 4),
        ("TriggerLeftStatus", ctypes.c_uint8, 4),
        
        ("HostTimestamp", ctypes.c_uint32),   # Byte 43..46
        
        # Byte 47
        ("TriggerRightEffect", ctypes.c_uint8, 4),
        ("TriggerLeftEffect", ctypes.c_uint8, 4),
        
        ("DeviceTimeStamp", ctypes.c_uint32), # Byte 48..51
        
        # Byte 52
        ("PowerPercent", ctypes.c_uint8, 4),
        ("PowerState", ctypes.c_uint8, 4),
        
        # Byte 53
        ("PluggedHeadphones", ctypes.c_uint8, 1),
        ("PluggedMic", ctypes.c_uint8, 1),
        ("MicMuted", ctypes.c_uint8, 1),
        ("PluggedUsbData", ctypes.c_uint8, 1),
        ("PluggedUsbPower", ctypes.c_uint8, 1),
        ("UsbPowerOnBT", ctypes.c_uint8, 1), # Fandom says DockDetect is 53.5? Wait, Fandom says 53.5 for UsbPowerOnBT, 53.5 DockDetect, 53.5 PluggedUnk. I'll make the remaining 3 bits reserved.
        ("PluggedReserved", ctypes.c_uint8, 2),
        
        # Byte 54
        ("PluggedExternalMic", ctypes.c_uint8, 1),
        ("HapticLowPassFilter", ctypes.c_uint8, 1),
        ("PluggedUnk3", ctypes.c_uint8, 6),
        
        ("AesCmac", ctypes.c_uint8 * 8),      # Byte 56..63
    ]

class DualSenseOutputReport02(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("ReportID", ctypes.c_uint8), # 0x02
        
        # Set flags
        ("EnableRumbleEmulation", ctypes.c_uint8, 1),
        ("UseRumbleNotHaptics", ctypes.c_uint8, 1),
        ("AllowRightTriggerFFB", ctypes.c_uint8, 1),
        ("AllowLeftTriggerFFB", ctypes.c_uint8, 1),
        ("AllowHeadphoneVolume", ctypes.c_uint8, 1),
        ("AllowSpeakerVolume", ctypes.c_uint8, 1),
        ("AllowMicVolume", ctypes.c_uint8, 1),
        ("AllowAudioControl", ctypes.c_uint8, 1),
        
        ("AllowMuteLight", ctypes.c_uint8, 1),
        ("AllowAudioMute", ctypes.c_uint8, 1),
        ("AllowLedColor", ctypes.c_uint8, 1),
        ("ResetLights", ctypes.c_uint8, 1),
        ("AllowPlayerIndicators", ctypes.c_uint8, 1),
        ("AllowHapticLowPassFilter", ctypes.c_uint8, 1),
        ("AllowMotorPowerLevel", ctypes.c_uint8, 1),
        ("AllowAudioControl2", ctypes.c_uint8, 1),
        
        ("RumbleEmulationRight", ctypes.c_uint8),
        ("RumbleEmulationLeft", ctypes.c_uint8),
        
        ("VolumeHeadphones", ctypes.c_uint8),
        ("VolumeSpeaker", ctypes.c_uint8),
        ("VolumeMic", ctypes.c_uint8),
        
        # Audio Control
        ("MicSelect", ctypes.c_uint8, 2),
        ("EchoCancelEnable", ctypes.c_uint8, 1),
        ("NoiseCancelEnable", ctypes.c_uint8, 1),
        ("OutputPathSelect", ctypes.c_uint8, 2),
        ("InputPathSelect", ctypes.c_uint8, 2),
        
        ("MuteLightMode", ctypes.c_uint8),
        
        # Mute Control
        ("TouchPowerSave", ctypes.c_uint8, 1),
        ("MotionPowerSave", ctypes.c_uint8, 1),
        ("HapticPowerSave", ctypes.c_uint8, 1),
        ("AudioPowerSave", ctypes.c_uint8, 1),
        ("MicMute", ctypes.c_uint8, 1),
        ("SpeakerMute", ctypes.c_uint8, 1),
        ("HeadphoneMute", ctypes.c_uint8, 1),
        ("HapticMute", ctypes.c_uint8, 1),
        
        ("RightTriggerFFB", ctypes.c_uint8 * 11),
        ("LeftTriggerFFB", ctypes.c_uint8 * 11),
        
        ("HostTimestamp", ctypes.c_uint32),
        
        # MotorPowerLevel
        ("TriggerMotorPowerReduction", ctypes.c_uint8, 4),
        ("RumbleMotorPowerReduction", ctypes.c_uint8, 4),
        
        # AudioControl2
        ("SpeakerCompPreGain", ctypes.c_uint8, 3),
        ("BeamformingEnable", ctypes.c_uint8, 1),
        ("UnkAudioControl2", ctypes.c_uint8, 4),
        
        # Flags for light
        ("AllowLightBrightnessChange", ctypes.c_uint8, 1),
        ("AllowColorLightFadeAnimation", ctypes.c_uint8, 1),
        ("EnableImprovedRumbleEmulation", ctypes.c_uint8, 1),
        ("UNKBITC", ctypes.c_uint8, 5),
        
        ("HapticLowPassFilter", ctypes.c_uint8, 1),
        ("UNKBIT", ctypes.c_uint8, 7),
        
        ("UNKBYTE", ctypes.c_uint8),
        
        ("LightFadeAnimation", ctypes.c_uint8),
        ("LightBrightness", ctypes.c_uint8),
        
        # PlayerIndicators
        ("PlayerLight1", ctypes.c_uint8, 1),
        ("PlayerLight2", ctypes.c_uint8, 1),
        ("PlayerLight3", ctypes.c_uint8, 1),
        ("PlayerLight4", ctypes.c_uint8, 1),
        ("PlayerLight5", ctypes.c_uint8, 1),
        ("PlayerLightFade", ctypes.c_uint8, 1),
        ("PlayerLightUNK", ctypes.c_uint8, 2),
        
        ("LedRed", ctypes.c_uint8),
        ("LedGreen", ctypes.c_uint8),
        ("LedBlue", ctypes.c_uint8),
    ]

