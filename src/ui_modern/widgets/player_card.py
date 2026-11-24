from ..i18n import tr
from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QAction, QColor, QPainter, QPen, QPixmap
from .common import FitImage
from ..resources import resource_path
from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget, QSizePolicy
from .controls import ComboBox
from qfluentwidgets import CardWidget, BodyLabel, CaptionLabel, TransparentPushButton, ToolButton, CheckableMenu, FluentIcon, themeColor, qconfig, isDarkTheme

_ASSETS = resource_path("resources/images")
CARD_HEIGHT = 250
_BARS = {"high": 3, "medium": 2, "low": 1}


class BatteryGlyph(QWidget):
    """One classic-tier battery per physical controller; unknown readings draw an empty outline."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.levels = ()
        self.setFixedHeight(12)
        qconfig.themeChangedFinished.connect(self.update)
    def set_levels(self, levels):
        self.levels = tuple(levels)
        self.setFixedWidth(max(1, len(self.levels)) * 28 - 6)
        self.setToolTip(" / ".join(tr((level or "unknown").capitalize()) for level in self.levels))
        self.update()
    def paintEvent(self, event):
        painter = QPainter(self); painter.setRenderHint(QPainter.Antialiasing)
        ink = QColor("#bfbfbf" if isDarkTheme() else "#606060")
        for i, level in enumerate(self.levels):
            x = i * 28 + 0.75
            painter.setPen(QPen(ink, 1.5)); painter.setBrush(Qt.NoBrush)
            painter.drawRoundedRect(QRectF(x, 0.75, 18, 10.5), 2.5, 2.5)
            painter.fillRect(QRectF(x + 19.25, 3.5, 2, 5), ink)
            fill = QColor("#c42b1c") if level == "low" else ink
            for bar in range(_BARS.get(level, 0)):
                painter.fillRect(QRectF(x + 2.5 + bar * 5, 3, 4, 6), fill)

class PlayerCard(CardWidget):
    def __init__(self, invoke, parent=None):
        super().__init__(parent)
        self.invoke = invoke
        self.snapshot = None
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.setMinimumWidth(155)
        self.setFixedHeight(CARD_HEIGHT)
        box = QVBoxLayout(self); box.setContentsMargins(14, 10, 14, 8); box.setSpacing(3)
        header = QHBoxLayout()
        self.title = BodyLabel("", self); header.addWidget(self.title); header.addStretch()
        self.leds = CaptionLabel("", self); header.addWidget(self.leds); box.addLayout(header)
        self.illustration = FitImage(_ASSETS / "procontroller2.png", self); self.illustration.setFixedHeight(76); box.addWidget(self.illustration)
        self.model = BodyLabel("", self); self.model.setWordWrap(True); self.model.setAlignment(Qt.AlignCenter)
        box.addWidget(self.model, 1)
        self.battery = BatteryGlyph(self)
        self.transport = CaptionLabel("", self)
        status = QHBoxLayout()
        status.addStretch(); status.addWidget(self.battery); status.addWidget(self.transport); status.addStretch()
        box.addLayout(status)
        row = QHBoxLayout(); self.ping = ToolButton(FluentIcon.VOLUME, self)
        self.ping.setToolTip(tr("Ping")); self.ping.setAccessibleName(tr("Ping"))
        self.merge = TransparentPushButton(tr("Merge"), self)
        self.merge.clicked.connect(lambda: self.action("split" if self.snapshot.split_available else "merge_select"))
        self.hold = ComboBox(self)
        for mode in ("Vertical", "Horizontal"):
            self.hold.addItem(tr(mode), userData=mode)
        self.hold.setAccessibleName(tr("Joy-Con orientation"))
        self.hold.setToolTip(tr("Joy-Con orientation"))
        box.addWidget(self.hold, 0, Qt.AlignHCenter)
        self.hold.activated.connect(lambda index: self.action("set_hold_mode", self.hold.itemData(index)))
        self.merge_hint = CaptionLabel(tr("Choose the other Joy-Con"), self); box.addWidget(self.merge_hint, 0, Qt.AlignHCenter)
        self.more = ToolButton(FluentIcon.MORE, self)
        self.more.setAccessibleName(tr("More"))
        self.more.setToolTip(tr("More"))
        row.setSpacing(2); row.addWidget(self.ping); row.addWidget(self.merge); row.addStretch(); row.addWidget(self.more); box.addLayout(row)
        self.ping.clicked.connect(lambda: self.action("ping"))
        self.more.clicked.connect(self.open_menu)
    def action(self, method, *args):
        if self.snapshot and self.snapshot.slot_token:
            self.invoke(method, self.snapshot.slot_token, *args)
    def update_snapshot(self, slot, first_empty=False):
        if slot == self.snapshot and getattr(self, "first_empty", None) == first_empty:
            return
        self.snapshot, self.first_empty = slot, first_empty
        self.title.setText(tr("Player {number}", number=slot.player_number))
        self.leds.setText(" ".join("■" if led else "□" for led in (slot.leds or (False,)*4)))
        self.leds.setVisible(not slot.is_empty)
        self.leds.setStyleSheet(f"color: {themeColor().name()}")
        self.ping.setVisible(not slot.is_empty and slot.ping_available)
        self.more.setVisible(not slot.is_empty)
        self.battery.setVisible(not slot.is_empty); self.transport.setVisible(not slot.is_empty)
        self.illustration.setVisible(not slot.is_empty)
        pending = not slot.is_empty and slot.pending_merge_index == slot.player_number - 1
        self.merge.setVisible(not slot.is_empty and (slot.merge_available or slot.split_available))
        self.merge.setText(tr("Split") if slot.split_available else tr("Selecting...") if pending else tr("Merge"))
        self.merge.setMinimumWidth(95 if pending else 0)
        self.merge_hint.setVisible(pending)
        self.hold.setVisible(not slot.is_empty and slot.hold_mode_available)
        self.hold.setCurrentIndex(self.hold.findData(slot.hold_mode))
        border = themeColor().name() if pending else "#e88724" if slot.merge_target else None
        self.setStyleSheet(f"PlayerCard {{border: 2px solid {border}; border-radius: 8px;}}" if border else "PlayerCard {border: 1px dashed #888; border-radius: 8px;}" if slot.is_empty else "")
        if slot.is_empty:
            self.model.setText(tr("Press a button on a paired controller, or hold sync to pair") if first_empty else "")
            return
        controllers = slot.controllers
        self.model.setText(" + ".join(c.display_name for c in controllers))
        self.battery.set_levels(c.battery_level for c in controllers)
        self.transport.setText(" / ".join(dict.fromkeys({"ble": tr("Bluetooth"), "esp32": "ESP32-S3", "wired": "USB", "usbip": "USB"}.get(c.transport, c.transport) for c in controllers)))
        kind = (slot.kind or controllers[0].kind).lower()
        image = "joycon2leftandright" if slot.is_merged_pair else ("joycon2right" if "right" in kind else "joycon2left") + ("_sideway" if slot.hold_mode == "Horizontal" else "") if "joy" in kind else "nsogamecubecontroller" if "gamecube" in kind else "procontroller2"
        self.illustration.path = _ASSETS / (image + ".png")
        self.illustration.pixmap = QPixmap(str(self.illustration.path)); self.illustration.update()
    def open_menu(self):
        s = self.snapshot
        menu = CheckableMenu(parent=self)
        menu.setAttribute(Qt.WA_DeleteOnClose)
        self.menu_actions = {}
        def add(label, fn, checked=None):
            action = QAction(tr(label), menu); action.triggered.connect(fn)
            if checked is not None:
                action.setCheckable(True); action.setChecked(checked)
            menu.addAction(action); self.menu_actions[label] = action
        add("Calibrate sticks", lambda: self.action("calibrate_sticks"))
        for i, controller in enumerate(s.controllers):
            if controller.mag_calibration_available:
                side = ("Left" if "left" in controller.kind.lower() else "Right") if s.is_merged_pair else None
                label = tr("Stop magnetometer calibration" if controller.is_mag_calibrating else "Calibrate magnetometer") + (f" · {tr(side)}" if side else "")
                add(label, lambda checked=False, side=side: self.action("toggle_mag_calibration", side))
        if s.gyro_side_available:
            for side in ("Left", "Right"):
                active = (s.djg_left_active if side == "Left" else s.djg_right_active) if s.independent_gyro_sides else s.active_gyro_side == side
                add(tr("Gyro side {side}", side=tr(side)),
                    lambda checked=False, side=side: self.action("toggle_gyro_side", side), checked=active)
        menu.addSeparator()
        add("Disconnect", lambda: self.action("disconnect"))
        self.menu = menu
        menu.exec(self.more.mapToGlobal(self.more.rect().bottomLeft()))
