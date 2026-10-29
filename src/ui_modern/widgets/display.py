"""Presentation over the classic formatter; persisted tokens stay unchanged."""
from app_core.helpers import format_input_display


def input_display(value):
    text = str(value)
    if text.startswith("Custom") and ":" in text:
        text = text.split(":", 1)[1]
    text = format_input_display(text)
    mouse_options = {"m1 Left Click": "Left click", "m2 Middle Click": "Middle click",
        "m3 Right Click": "Right click", "m4 Backward": "Back click", "m5 Forward": "Forward click"}
    if text in mouse_options:
        return mouse_options[text]
    names = {"CONTROL": "Ctrl", "LCONTROL": "Ctrl", "RCONTROL": "Ctrl",
        "SHIFT": "Shift", "MENU": "Alt", "LWIN": "Win", "RWIN": "Win",
        "RETURN": "Enter", "BACK": "Backspace", "BACKSPACE": "Backspace",
        "TAB": "Tab", "SPACE": "Space", "ESCAPE": "Esc", "DELETE": "Delete", "INSERT": "Insert",
        "HOME": "Home", "END": "End", "PRIOR": "Page up", "NEXT": "Page down",
        "LEFT": "Left", "RIGHT": "Right", "UP": "Up", "DOWN": "Down",
        "CAPITAL": "Caps lock", "NUM_LOCK": "Num lock", "SCROLL_LOCK": "Scroll lock",
        "SNAPSHOT": "Print screen", "PRINT": "Print screen", "PAUSE": "Pause",
        "LSHIFT": "Shift", "RSHIFT": "Shift", "LMENU": "Alt", "RMENU": "Alt", "ALT": "Alt",
        "MINUS": "-", "EQUAL": "=", "BRACKETLEFT": "[", "BRACKETRIGHT": "]",
        "BACKSLASH": "\\", "SEMICOLON": ";", "APOSTROPHE": "'", "GRAVE": "`",
        "COMMA": ",", "PERIOD": ".", "SLASH": "/", "ADD": "Numpad +",
        "SUBTRACT": "Numpad -", "MULTIPLY": "Numpad *", "DIVIDE": "Numpad /", "DECIMAL": "Numpad .",
        "MW_LEFT": "Wheel left", "MW_RIGHT": "Wheel right",
        "M1": "Left click", "M2": "Right click", "M3": "Middle click",
        "M4": "Back click", "M5": "Forward click", "MW_UP": "Wheel up", "MW_DOWN": "Wheel down",
        "GYRO_LOCK": "Gyro lock", "MODE_SHIFT": "Mode shift", "INAPPGYRO": "In-app gyro"}
    def label(part):
        part = part.strip()
        if part == "KB Arrow Keys":
            return "Keyboard arrow keys"
        if part.startswith("KB"):
            part = part[2:].strip()
        if part.startswith("KP_") and part[3:].isdigit():
            return "Numpad " + part[3:]
        return names.get(part, part)
    return setting_title(" + ".join(label(p) for p in text.split("+")))


def setting_title(text):
    replacements = {"Activation Threshold": "Activation threshold", "Left Click": "Left click",
        "Right Click": "Right click", "Middle Click": "Middle click", "Trigger Deadzone": "Trigger deadzone",
        "Trigger Dampening": "Trigger dampening", "Deadzone Amount": "Deadzone amount",
        "Gyro Pause After Pressed": "Gyro pause after pressed", "Gyro Pause After Released": "Gyro pause after released",
        "Deadzone Effect After Released": "Deadzone effect after released",
        "Dampening Amount": "Dampening amount", "Dampening Effect After Released": "Dampening effect after released",
        "Simultaneous Input": "Simultaneous input", "Switch Input": "Switch input", "Mouse Click": "Mouse click",
        "Joystick Click": "Joystick click", "L Click": "L click", "R Click": "R click",
        "Switch Dominant Side": "Switch dominant side", "Single Side Toggle": "Single side toggle", "Switch Gyro Side": "Switch gyro side",
        "Double Roll": "Double roll", "Direct Merge": "Direct merge",
        "9-Axis": "9-axis", "6-Axis": "6-axis", "In-app Gyro": "In-app gyro",
        "Hair Trigger": "Hair trigger", "Before Click": "Before click", "Fully Clicked": "Fully clicked",
        "Scroll Wheel": "Scroll wheel", "R Joystick": "Right joystick", "L Joystick": "Left joystick",
        "Change Profile": "Change profile", "Mode Shift": "Mode shift", "Gyro Lock": "Gyro lock", "Joystick Calibration": "Joystick calibration", "Wired Controller": "Wired controller"}
    for source, target in replacements.items():
        text = text.replace(source, target)
    return text
